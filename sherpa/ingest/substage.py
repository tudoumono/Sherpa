"""取り込みの「小さな段」の進み具合。段（stage）の中で長くかかる処理に、名前・件数・生存の知らせを付ける。

小さな段の始まりと終わりをログに 1 行ずつ出し、件数を数えられる段は 1 割ごと・1 分ごとに途中の行を出す。
件数を出せない長い処理でも、一定の間隔（`HEARTBEAT_SEC`）で「まだ動いています」を出し、`ingest_runs.progress` の更新時刻を進める。
同じ見張りが、その間隔でメモリの標本（`resource_meter.sample_if_due`）も取り、直近の標本を「まだ動いています」の行に添える。
進み具合の書き先（`bind`）が無いとき（単体の呼び出し・テスト）はログだけを出す。記録の失敗で処理を止めない。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import resource_meter

_default_log = logging.getLogger("sherpa.ingest.convert")

HEARTBEAT_SEC = 45.0     # 進みが無いまま、これだけ経ったら「まだ動いています」を出して更新時刻を進める
WRITE_EVERY = 100        # 進み具合の書き込みは、この件数ごと
WRITE_MIN_SEC = 2.0      # ……かつ、前の書き込みからこの秒数以上あけて（数万件でも DB を叩きすぎない）
WRITE_MAX_SEC = 15.0     # 件数が進みにくいときは、この秒数ごとに書く
LOG_EVERY_SEC = 60.0     # 途中のログは、1 割進むごと、または最後のログからこの秒数ごと

_lock = threading.RLock()
_sink = None             # (write(info|None), stage_label() -> str)
_cur: "Step | None" = None
_hb_thread: threading.Thread | None = None
_hb_wake = threading.Event()   # 小さな段が始まるたびに立てる（見張りが待ち時間を取り直す）


def _fmt_elapsed(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分"
    return f"{sec // 3600} 時間 {sec % 3600 // 60} 分"


def _stage_label() -> str:
    sink = _sink
    if sink is None or sink[1] is None:
        return ""
    try:
        return sink[1]() or ""
    except Exception:
        return ""


class Step:
    """動いている小さな段 1 つ。`tick(done)` で件数を進め、`end()`（または `with`）で閉じる。"""

    def __init__(self, key: str, label: str, total: int | None, logger: logging.Logger, note=None):
        self.key = key
        self.note = note
        self.label = label
        self.total = total
        self.done = 0
        self.log = logger
        self.ended = False
        self.t0 = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.t_write = self.t0
        self.w_done = 0
        self.t_log = self.t0
        self.decile = 0
        self.t_act = self.t0
        self.write_warned = False

    def info(self) -> dict:
        return {"step": self.key, "step_label": self.label, "step_done": self.done,
                "step_total": self.total, "step_started_at": self.started_at}

    def tick(self, done: int, total: int | None = None) -> None:
        if self.ended:
            return
        if total is not None:
            self.total = total
        if done != self.done:
            self.t_act = time.monotonic()
        self.done = done
        now = time.monotonic()
        since = now - self.t_write
        if (done - self.w_done >= WRITE_EVERY and since >= WRITE_MIN_SEC) or since >= WRITE_MAX_SEC:
            _write(self)
        if self.total:
            decile = min(10, done * 10 // self.total)
            if decile > self.decile or now - self.t_log >= LOG_EVERY_SEC:
                self.decile = max(self.decile, decile)
                self.t_log = now
                self.log.info("取り込みの進み具合: %s %d/%d 件（%d%%・経過 %s%s）",
                              self.label, done, self.total, done * 100 // self.total,
                              _fmt_elapsed(now - self.t0), self._note())
        elif now - self.t_log >= LOG_EVERY_SEC:
            self.t_log = now
            self.log.info("取り込みの進み具合: %s %d 件（経過 %s%s）", self.label, done, _fmt_elapsed(now - self.t0),
                          self._note())

    def _note(self) -> str:
        """途中の行に添える一言（`note()` の戻り値・空なら何も添えない）。失敗しても進み具合のログは止めない。"""
        if self.note is None:
            return ""
        try:
            text = self.note()
        except Exception:
            return ""
        return f"・{text}" if text else ""

    def end(self, *, quiet: bool = False, failed: bool = False) -> None:
        end(self, quiet=quiet, failed=failed)

    def __enter__(self) -> "Step":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.end(failed=exc_type is not None)


def bind(write, stage_label=None) -> None:
    """進み具合の書き先を結ぶ。`write(info)` は小さな段の項目（閉じたときは None）を受け取る。`stage_label()` は「まだ動いています」の段の名前。"""
    global _sink
    with _lock:
        _end_locked(quiet=True)
        _sink = (write, stage_label)
        _ensure_heartbeat()


def unbind(*, failed: bool = False) -> None:
    """動いている小さな段を閉じ、書き先を外す（取り込み 1 回の終わりに必ず呼ぶ。`failed` は例外で抜けたとき）。"""
    global _sink
    with _lock:
        _end_locked(quiet=True, failed=failed)
        _sink = None


def begin(key: str, label: str, total: int | None = None, *, logger: logging.Logger | None = None,
          note=None) -> Step:
    """小さな段を始める。前の小さな段が残っていれば閉じる。`note`（省略可）は途中のログの行に添える一言を返す関数。"""
    global _cur
    st = Step(key, label, total, logger or _default_log, note)
    with _lock:
        _end_locked(quiet=False)
        _cur = st
        _ensure_heartbeat()
    st.log.info("取り込みの進み具合: %s を始めます%s", label, f"（全 {total} 件）" if total else "")
    _write(st)
    return st


def step(key: str, label: str, total: int | None = None, *, logger: logging.Logger | None = None) -> Step:
    """`with substage.step(...) as st:` で使う `begin`。"""
    return begin(key, label, total, logger=logger)


def current(key: str) -> Step | None:
    """今動いている小さな段が `key` のものならそれを返す（呼び出しの形を変えずに、中の処理が件数を進めるため）。"""
    st = _cur
    return st if st is not None and st.key == key else None


def end(st: Step | None = None, *, quiet: bool = False, failed: bool = False) -> None:
    """小さな段を閉じる（`st` が動いている段でなければ何もしない・省略すれば今動いている段）。`quiet` は書き先を更新しない（段の切替で次の書き込みが上書きするとき）。"""
    with _lock:
        if st is not None and st is not _cur:
            return
        _end_locked(quiet=quiet, failed=failed)


def _end_locked(*, quiet: bool, failed: bool = False) -> None:
    global _cur
    st = _cur
    if st is None:
        return
    _cur = None
    st.ended = True
    took = _fmt_elapsed(time.monotonic() - st.t0)
    if failed:
        st.log.info("取り込みの進み具合: %s が途中で止まりました（%d 件・%s）", st.label, st.done, took)
    else:
        st.log.info("取り込みの進み具合: %s が終わりました（%d 件・%s）", st.label, st.done, took)
    if not quiet and _sink is not None:
        _call_sink(st, None)


def _call_sink(st: Step, info) -> None:
    sink = _sink
    if sink is None:
        return
    try:
        sink[0](info)
    except Exception:
        if not st.write_warned:
            st.write_warned = True
            st.log.warning("進み具合の記録に失敗しました（取り込み自体は継続）: %s", st.label, exc_info=True)


def _write(st: Step) -> None:
    with _lock:
        if st.ended or st is not _cur:
            return
        now = time.monotonic()
        st.t_write = now
        st.w_done = st.done
        st.t_act = now
        _call_sink(st, st.info())


def _poll_sec() -> float:
    return max(0.05, min(1.0, HEARTBEAT_SEC / 4))


def _ensure_heartbeat() -> None:
    global _hb_thread
    _hb_wake.set()
    if _hb_thread is not None and _hb_thread.is_alive():
        return
    _hb_thread = threading.Thread(target=_heartbeat_loop, name="substage-heartbeat", daemon=True)
    _hb_thread.start()


def _heartbeat_loop() -> None:
    while True:
        _hb_wake.wait(_poll_sec())
        _hb_wake.clear()
        resource_meter.sample_if_due(HEARTBEAT_SEC)
        try:
            with _lock:
                st = _cur
                if st is None or st.ended:
                    continue
                now = time.monotonic()
                if now - st.t_act < HEARTBEAT_SEC:
                    continue
                stage = _stage_label()
                st.log.info("まだ動いています: %s経過 %s%s",
                            f"{stage}・{st.label}・" if stage else f"{st.label}・", _fmt_elapsed(now - st.t0),
                            resource_meter.latest_note())
                st.t_log = now
                _write(st)
        except Exception:
            pass
