"""取り込みの計測。段ごとの「待ちの理由ごとの時間」と、機械全体のメモリ・取り込みのプロセス群の RSS の標本を取る。

- 待ち: `waiting(理由, task=…)` で測る。理由は `REASONS`（キューの待ち・資源の待ち・処理中・外部の応答の待ち・再試行の待ち）。
  今の段（`set_stage`）に積み、段を閉じるとき `stage_summary` が `stage_timings` の項目へ畳む。取り込みの外（`begin` していない）では何もしない。
  並列に走る待ちは足し合わせるので、合計が段の所要時間を超えることがある。
- メモリ: 同じ時刻の機械全体の使用量・空きと、取り込みのワーカーとその子孫の RSS の合計を標本に取る（Linux の /proc のみ）。
  取れない環境では取れないことを記録して続ける。標本は `sample_if_due`（heartbeat の間隔）と、開始・段を閉じるときだけ。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

_log = logging.getLogger("sherpa.ingest.convert")

QUEUE, RESOURCE, WORKING, EXTERNAL, RETRY = "queue", "resource", "working", "external", "retry"
REASONS = (QUEUE, RESOURCE, WORKING, EXTERNAL, RETRY)
NO_PROC = "no_proc"          # /proc/meminfo が無い（Linux 以外など）
_MIB = 1024 * 1024

_lock = threading.RLock()
_run: "_Run | None" = None


def _mib(n: int | None) -> int | None:
    return None if n is None else n // _MIB


class _Peaks:
    """標本の最大・最小。使用量の最大・空きの最小・RSS 合計の最大。"""

    def __init__(self) -> None:
        self.samples = 0
        self.total = None
        self.used_peak = None
        self.avail_min = None
        self.rss_peak = None
        self.procs_peak = None

    def add(self, total: int, avail: int, rss: int | None, procs: int) -> None:
        self.samples += 1
        self.total = total
        used = max(0, total - avail)
        self.used_peak = used if self.used_peak is None else max(self.used_peak, used)
        self.avail_min = avail if self.avail_min is None else min(self.avail_min, avail)
        if rss is not None:
            self.rss_peak = rss if self.rss_peak is None else max(self.rss_peak, rss)
            self.procs_peak = procs if self.procs_peak is None else max(self.procs_peak, procs)

    def as_dict(self) -> dict:
        return {"samples": self.samples, "mem_total_mb": _mib(self.total),
                "used_peak_mb": _mib(self.used_peak), "avail_min_mb": _mib(self.avail_min),
                "rss_peak_mb": _mib(self.rss_peak), "rss_procs_peak": self.procs_peak}


class _Run:
    def __init__(self, proc_root: str, pid: int) -> None:
        self.proc_root = proc_root
        self.pid = pid
        self.stage: str | None = None
        self.waits: dict[str | None, dict[str, dict]] = {}   # 段 → 理由 → {"ms","count","tasks"}
        self.stage_peaks: dict[str | None, _Peaks] = {}
        self.peaks = _Peaks()
        self.unavailable: str | None = None
        self.last_sample = -1e18
        self.latest: dict | None = None


def read_system_memory(proc_root: str = "/proc") -> tuple[int, int]:
    """機械全体のメモリ `(全体, 空き)` をバイトで返す。空きは MemAvailable。読めなければ例外（FileNotFoundError など）。"""
    total = avail = None
    with open(os.path.join(proc_root, "meminfo"), encoding="ascii") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
            if total is not None and avail is not None:
                break
    if total is None or avail is None:
        raise ValueError("meminfo")
    return total, avail


def _ppid_of(proc_root: str, pid: str) -> int | None:
    try:
        with open(os.path.join(proc_root, pid, "stat"), encoding="ascii", errors="replace") as f:
            raw = f.read()
        return int(raw.rsplit(")", 1)[1].split()[1])     # 名前に空白・括弧を含みうるので最後の ")" の後ろを読む
    except (OSError, ValueError, IndexError):
        return None


def _rss_of(proc_root: str, pid: int) -> int:
    try:
        with open(os.path.join(proc_root, str(pid), "status"), encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def read_tree_rss(root_pid: int, proc_root: str = "/proc") -> tuple[int, int]:
    """`root_pid` とその子孫の RSS の合計（バイト）と数。走査の途中で消えたプロセスは数えない。"""
    children: dict[int, list[int]] = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return 0, 0
    for name in names:
        if not name.isdigit():
            continue
        ppid = _ppid_of(proc_root, name)
        if ppid is not None:
            children.setdefault(ppid, []).append(int(name))
    seen, stack, total = {root_pid}, [root_pid], 0
    while stack:
        pid = stack.pop()
        total += _rss_of(proc_root, pid)
        for c in children.get(pid, ()):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return total, len(seen)


def begin(*, proc_root: str = "/proc", pid: int | None = None) -> None:
    """取り込み 1 回の計測を始める（前の計測が残っていれば捨てる）。始めに標本を 1 つ取る。"""
    global _run
    with _lock:
        _run = _Run(proc_root, pid if pid is not None else os.getpid())
    _sample()


def end() -> dict | None:
    """計測を終えて取り込み全体の要約を返す（始めていなければ None）。例外で抜けるときも呼ぶ。"""
    global _run
    with _lock:
        run, _run = _run, None
    return _summary(run) if run is not None else None


def set_stage(stage: str | None) -> None:
    """待ちと標本を積む先の段を切り替える。"""
    with _lock:
        if _run is not None:
            _run.stage = stage


def record_wait(reason: str, seconds: float, task: str | None = None) -> None:
    """`reason` の待ちを `seconds` 秒、今の段に積む。計測を始めていなければ何もしない。"""
    if reason not in REASONS:
        raise ValueError(f"unknown wait reason: {reason}")
    with _lock:
        if _run is None:
            return
        slot = _run.waits.setdefault(_run.stage, {}).setdefault(reason, {"ms": 0.0, "count": 0, "tasks": {}})
        ms = max(0.0, seconds) * 1000
        slot["ms"] += ms
        slot["count"] += 1
        if task:
            slot["tasks"][task] = slot["tasks"].get(task, 0.0) + ms


@contextlib.contextmanager
def waiting(reason: str, task: str | None = None):
    """`with waiting("resource", task="…"):` の中で待った時間を `reason` として記録する。中の処理の流れは変えない。"""
    if reason not in REASONS:
        raise ValueError(f"unknown wait reason: {reason}")
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            record_wait(reason, time.monotonic() - t0, task)
        except Exception:
            _log.debug("待ちの記録に失敗しました（取り込み自体は継続）", exc_info=True)


def _sample() -> None:
    """標本を 1 つ取る。取れない環境では理由を残して以後は読みにいかない。失敗で取り込みを止めない。"""
    with _lock:
        run = _run
        if run is None or run.unavailable is not None:
            return
        run.last_sample = time.monotonic()
        root, pid = run.proc_root, run.pid
    try:
        total, avail = read_system_memory(root)
    except FileNotFoundError:
        _mark_unavailable(run, NO_PROC)
        return
    except Exception as e:
        _mark_unavailable(run, f"read_failed:{e.__class__.__name__}")
        return
    try:
        rss, procs = read_tree_rss(pid, root)
    except Exception:
        rss, procs = None, 0
    with _lock:
        if _run is not run:
            return
        run.peaks.add(total, avail, rss, procs)
        run.stage_peaks.setdefault(run.stage, _Peaks()).add(total, avail, rss, procs)
        run.latest = {"used": max(0, total - avail), "avail": avail, "rss": rss}


def _mark_unavailable(run: _Run, reason: str) -> None:
    with _lock:
        run.unavailable = reason
    _log.info("取り込みの計測: メモリの標本は取れません（%s）", reason)


def sample_if_due(interval_sec: float) -> None:
    """前の標本から `interval_sec` 以上経っていれば標本を取る（heartbeat の見張りから呼ぶ）。例外は投げない。"""
    try:
        with _lock:
            run = _run
            if run is None or run.unavailable is not None or time.monotonic() - run.last_sample < interval_sec:
                return
        _sample()
    except Exception:
        _log.debug("メモリの標本に失敗しました（取り込み自体は継続）", exc_info=True)


def latest_note() -> str:
    """直近の標本を「まだ動いています」の行に添える一言にする（標本が無ければ空）。"""
    with _lock:
        latest = _run.latest if _run is not None else None
    if not latest:
        return ""
    rss = f"・取り込み全体の RSS {_mib(latest['rss'])}MB" if latest["rss"] is not None else ""
    return f"・機械の使用 {_mib(latest['used'])}MB・空き {_mib(latest['avail'])}MB{rss}"


def _waits_dict(by_reason: dict) -> dict:
    return {r: {"ms": round(v["ms"]), "count": v["count"],
                **({"tasks": {t: round(ms) for t, ms in v["tasks"].items()}} if v["tasks"] else {})}
            for r, v in by_reason.items()}


def stage_summary(stage: str | None, *, sample: bool = True) -> dict:
    """`stage` の待ち（`waits`）とメモリの標本（`mem`）を `stage_timings` の項目に足す辞書で返す。段を閉じるとき 1 回呼ぶ。

    `sample` のときは閉じる時点の標本を 1 つ足す。標本が取れていなければ `mem` は `{"available": False, "reason": …}`。
    """
    if sample:
        _sample()
    out: dict = {}
    with _lock:
        run = _run
        if run is None:
            return out
        if run.waits.get(stage):
            out["waits"] = _waits_dict(run.waits[stage])
        peaks = run.stage_peaks.get(stage)
        if peaks is not None and peaks.samples:
            out["mem"] = {"available": True, **peaks.as_dict()}
        elif run.unavailable is not None:
            out["mem"] = {"available": False, "reason": run.unavailable}
    return out


def _summary(run: _Run) -> dict:
    waits: dict[str, dict] = {}
    for by_reason in run.waits.values():
        for r, v in by_reason.items():
            slot = waits.setdefault(r, {"ms": 0.0, "count": 0})
            slot["ms"] += v["ms"]
            slot["count"] += v["count"]
    out: dict = {"waits": {r: {"ms": round(v["ms"]), "count": v["count"]} for r, v in waits.items()}}
    if run.peaks.samples:
        out["mem"] = {"available": True, **run.peaks.as_dict()}
    else:
        out["mem"] = {"available": False, "reason": run.unavailable or "no_sample"}
    return out


def snapshot(*, sample: bool = True) -> dict | None:
    """取り込み全体のここまでの要約（待ちの合計と、メモリの最大・最小）。始めていなければ None。計測は続く。"""
    if sample:
        _sample()
    with _lock:
        run = _run
        return _summary(run) if run is not None else None


def log_summary(label: str, elapsed_ms: int | None, summary: dict) -> None:
    """段（または取り込み全体）の待ちの内訳とメモリの標本を、取り込みのログに 1 行出す。出すものが無ければ何も出さない。"""
    parts = []
    names = {QUEUE: "キュー待ち", RESOURCE: "資源待ち", WORKING: "処理中の待ち", EXTERNAL: "外部の応答待ち",
             RETRY: "再試行待ち"}
    for r in REASONS:
        w = (summary.get("waits") or {}).get(r)
        if w:
            parts.append(f"{names[r]} {w['ms'] / 1000:.1f}秒（{w['count']}回）")
    mem = summary.get("mem") or {}
    if mem.get("available"):
        parts.append(f"機械の使用 最大 {mem['used_peak_mb']}MB・空き 最小 {mem['avail_min_mb']}MB"
                     + (f"・取り込み全体の RSS 最大 {mem['rss_peak_mb']}MB" if mem.get("rss_peak_mb") is not None else ""))
    elif mem:
        parts.append(f"メモリの標本は取れません（{mem.get('reason')}）")
    if parts:
        took = f"・所要 {elapsed_ms / 1000:.1f}秒" if elapsed_ms is not None else ""
        _log.info("取り込みの計測: %s%s・%s", label, took, "・".join(parts))
