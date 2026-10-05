"""チャットターンのバックグラウンド実行（覗き窓方式）。送信で開始した思考イベントをプロセス内の有界バッファへ追記しながら、
サーバ側の background thread で完走する。
設計: docs/design/chat.md「停止と同時実行」

- HTTP（SSE）購読の有無に関わらず最後まで走り、messages/trace/監査は `chat_service` の既存ロジックで DB に永続する（薄いラッパー）。
- SSE 購読（`GET /chat/turns/{id}/stream`）はバッファを cursor から replay→追従するだけ。切断してもターンは止まらない。
  明示停止は `stop_turn`（turn_id 宛て）。
- レジストリ/バッファはプロセス内（uvicorn workers=1 前提）。完了ターンは TTL（`COMPLETED_TTL_SECONDS`）後に破棄する。
- ロック順序: `_REGISTRY_LOCK` → `TurnBuffer._cond` の順のみ。`TurnBuffer` のメソッドは `_REGISTRY_LOCK` を取らない。
- 外部ジョブ（`codex_jobs_worker.py`）はレジストリに登録しないが、global 同時実行上限はチャットと合算で数える
  （`try_reserve_external`/`release_external`・`_EXTERNAL_RUNNING`）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

_log = logging.getLogger("sherpa")

# 同時実行の上限。超過は呼び出し側で 429 に変換する（TurnLimitError）。管理画面（system_settings）で上書きでき、下の 2 定数は未設定/DB 不達時の既定（`effective_limits()`）。
MAX_TURNS_PER_USER = 2
MAX_TURNS_GLOBAL = 8

# バッファの有界化（件数・バイト）。
# 先頭 1 件と直近 1 件は必ず保持するため、実効上限は `max(MAX_BUFFER_BYTES, 先頭イベント + 直近 1 イベントのサイズ)` になり得る（切り詰めはしない）。
MAX_BUFFER_EVENTS = 2000
MAX_BUFFER_BYTES = 2_000_000

# 完了ターンの buffer 保持時間（秒）。再接続の猶予のみ。
COMPLETED_TTL_SECONDS = 600

class TurnLimitError(Exception):
    """同時実行数の上限超過（呼び出し側で 429 に変換する）。"""

    def __init__(self, scope: str):
        self.scope = scope   # "user" | "global"
        super().__init__(f"chat turn limit exceeded: {scope}")


@dataclass
class _Event:
    seq: int
    payload: dict
    nbytes: int


class TurnBuffer:
    """1 ターン分の思考イベントの有界ログ（append-only・cursor=seq で範囲取得する）。"""

    def __init__(self):
        self._cond = threading.Condition()
        self._events: list[_Event] = []
        self._next_seq = 1
        self._total_bytes = 0
        self.done = False
        self.completed_at: float | None = None

    def append(self, payload: dict) -> None:
        """イベントを追記する（完了後の追記は無視）。"""
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        nbytes = len(raw.encode("utf-8"))
        with self._cond:
            if self.done:
                return
            self._events.append(_Event(self._next_seq, payload, nbytes))
            self._next_seq += 1
            self._total_bytes += nbytes
            # 有界化: 件数/バイトが上限を超えたら古い方から捨てる。
            # 先頭イベント（seq=1・ターンの種別を伝えるマーカー）は削らず、直近 1 件も必ず残す。件数が 2 件になったら打ち切る。
            # `payload` の中身は解釈しない。
            while len(self._events) > 2 and (
                    len(self._events) > MAX_BUFFER_EVENTS or self._total_bytes > MAX_BUFFER_BYTES):
                dropped = self._events.pop(1)
                self._total_bytes -= dropped.nbytes
            self._cond.notify_all()

    def mark_done(self) -> None:
        with self._cond:
            self.done = True
            self.completed_at = time.time()
            self._cond.notify_all()

    def replay_from(self, cursor: int) -> list[_Event]:
        """cursor（seq）より後のイベントを返す（剪定で欠落があっても現存分をそのまま返す）。"""
        with self._cond:
            return [e for e in self._events if e.seq > cursor]

    def wait_for_more(self, cursor: int, timeout: float) -> bool:
        """cursor より後のイベントがあるか完了済みなら即 True。無ければ追記/完了まで待ち、進展があれば True、タイムアウトなら False。
        False は呼び出し側（`iter_sse`）が keepalive を流す合図。
        """
        with self._cond:
            if self.done or any(e.seq > cursor for e in self._events):
                return True
            self._cond.wait(timeout=timeout)
            return self.done or any(e.seq > cursor for e in self._events)


@dataclass
class TurnRecord:
    turn_id: str
    uid: str
    stop_event: threading.Event
    # 予約方式（`start_turn`）のため、登録時は会話が未確定（None）のことがある。確定後に `start_turn` が代入する。
    conversation_id: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    buffer: TurnBuffer = field(default_factory=TurnBuffer)


_REGISTRY: dict[str, TurnRecord] = {}
_REGISTRY_LOCK = threading.Lock()

# 外部ジョブ用の実行枠の予約数。global 上限は「チャットの未完了ターン数＋この予約数」で数える。`_REGISTRY_LOCK` の下でだけ読み書きする。
_EXTERNAL_RUNNING = 0


def _sweep_expired_locked() -> None:
    """完了後 TTL を過ぎたターンをレジストリから外す（`_REGISTRY_LOCK` 保持済み前提）。経過時間でターンを打ち切ることはしない。"""
    now = time.time()
    expired: list[str] = []
    for tid, rec in _REGISTRY.items():
        if rec.buffer.done:
            if rec.buffer.completed_at is not None and (now - rec.buffer.completed_at) > COMPLETED_TTL_SECONDS:
                expired.append(tid)
    for tid in expired:
        _REGISTRY.pop(tid, None)


def _clamped_setting_int(raw, lo: int, hi: int) -> int | None:
    """system_settings の生値を整数として検証する（循環 import 回避のため別実装）。型不正・範囲外は None（組み込みの既定へ倒す）。"""
    if raw is None:
        return None
    try:
        iv = int(raw)
    except (TypeError, ValueError):
        return None
    return iv if lo <= iv <= hi else None


def effective_limits() -> tuple[int, int]:
    """同時実行上限の実効値 `(per_user, global)`。ターン受付のたびに解決し、`start_turn` が `_REGISTRY_LOCK` を取る前に呼ぶ。
    system_settings の `chat_max_turns_per_user`／`chat_max_turns_global` → 無効・未設定・DB 不達なら組み込みの既定（fail-open）。
    """
    try:
        from . import store
        sysset = store.get_system_settings() or {}
    except Exception:
        sysset = {}
    per_user = _clamped_setting_int(sysset.get("chat_max_turns_per_user"), 1, 16)
    glob = _clamped_setting_int(sysset.get("chat_max_turns_global"), 1, 64)
    return (per_user if per_user is not None else MAX_TURNS_PER_USER,
            glob if glob is not None else MAX_TURNS_GLOBAL)


def _raise_if_over_limit_locked(uid: str, max_per_user: int, max_global: int) -> None:
    """上限超過なら `TurnLimitError` を送出する（`_REGISTRY_LOCK` 保持済み前提）。
    `max_per_user`/`max_global` は呼び出し側がロック取得前に解決して渡す（ここでは DB に触れない）。
    会話未確定（予約中）のレコードも未完了として数える。global は未完了ターン数＋外部ジョブの予約数（`_EXTERNAL_RUNNING`）の合計で判定し、per_user は対象外。
    """
    running = [r for r in _REGISTRY.values() if not r.buffer.done]
    if len(running) + _EXTERNAL_RUNNING >= max_global:
        raise TurnLimitError("global")
    if sum(1 for r in running if r.uid == uid) >= max_per_user:
        raise TurnLimitError("user")


def try_reserve_external(max_global: int) -> bool:
    """外部ジョブ用の実行枠を 1 件予約する。未完了ターン数＋既存の外部予約数が `max_global` 未満なら予約して True、そうでなければ False。
    `max_global` は呼び出し側がロック取得前に解決して渡す（DB I/O なし）。
    """
    global _EXTERNAL_RUNNING
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        running = sum(1 for r in _REGISTRY.values() if not r.buffer.done)
        if running + _EXTERNAL_RUNNING >= max_global:
            return False
        _EXTERNAL_RUNNING += 1
        return True


def release_external() -> None:
    """`try_reserve_external` の予約を 1 件返す。ジョブの全終了経路・使わなかった予約・スレッド起動失敗のいずれでも呼ぶこと（0 未満にはしない）。"""
    global _EXTERNAL_RUNNING
    with _REGISTRY_LOCK:
        if _EXTERNAL_RUNNING > 0:
            _EXTERNAL_RUNNING -= 1


def _raise_if_conversation_busy_locked(conversation_id: int) -> None:
    """既存会話への継続ターンで、同じ conversation_id の未完了ターンが既にあれば `TurnLimitError("conversation")` を送出する
    （`_REGISTRY_LOCK` 保持済み前提）。会話単位でターン受付を直列化する。
    """
    for r in _REGISTRY.values():
        if not r.buffer.done and r.conversation_id == conversation_id:
            raise TurnLimitError("conversation")


def start_turn(*, uid: str,
              conversation_factory: Callable[[], int],
              run_fn_factory: Callable[[int], Callable[[threading.Event, Callable[[dict], None]], None]],
              known_conversation_id: int | None = None,
              ) -> TurnRecord:
    """新規ターンを background thread で開始する（予約方式）。上限超過で弾かれるリクエストが会話だけ作る副作用を避ける。
    ① `_REGISTRY_LOCK` の中で枠を予約する（上限判定と登録は atomic・この時点で conversation_id は None）。上限超過は `TurnLimitError`（会話は作られない）。
    ② ロックの外で `conversation_factory()` を呼んで会話を確定する。失敗したら予約を取り消して re-raise する。
    ③ conversation_id を予約レコードへ書き、`run_fn_factory(conversation_id)` で実処理を組み立てて thread を起動する。
    `known_conversation_id` は既存会話への継続と分かっているときだけ渡す。同じ会話の未完了ターンがあれば `TurnLimitError("conversation")`。
    `effective_limits()` はロック取得前に呼ぶ（DB I/O をロック保持中に行わない）。
    """
    max_per_user, max_global = effective_limits()
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        # 会話単位の排他を集約上限より先に判定する（会話継続専用の文言を出すため）。
        if known_conversation_id is not None:
            _raise_if_conversation_busy_locked(known_conversation_id)
        _raise_if_over_limit_locked(uid, max_per_user, max_global)
        turn_id = uuid.uuid4().hex
        # `known_conversation_id` は予約時点で確定させる（factory 実行中に同じ会話の 2 本目がすり抜けないように）。
        rec = TurnRecord(turn_id=turn_id, uid=uid, stop_event=threading.Event(),
                         conversation_id=known_conversation_id)
        _REGISTRY[turn_id] = rec

    try:
        conversation_id = conversation_factory()
    except Exception:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(turn_id, None)  # 予約取消
        raise

    rec.conversation_id = conversation_id
    # 経過時間による予約の強制解放はしない（`mark_done()` は `_run()` 自身の finally だけが呼ぶ）。
    try:
        run_fn = run_fn_factory(conversation_id)
    except Exception:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(turn_id, None)  # 予約取消（`_run` が起動しなければ `mark_done` は誰も呼ばない）
        raise

    def _run():
        try:
            run_fn(rec.stop_event, rec.buffer.append)
        except Exception:
            # 未捕捉の例外でも枠を占有したまま残さない。DB への永続は呼び出し側（run_fn）の責務で、ここは buffer にエラーを積んで枠を解放するだけ。
            _log.exception("chat turn crashed: turn_id=%s uid=%s", turn_id, uid)
            rec.buffer.append({"type": "error", "message": "内部エラーが発生しました。もう一度お試しください。"})
        finally:
            rec.buffer.mark_done()

    try:
        threading.Thread(target=_run, daemon=True, name=f"sherpa-turn-{turn_id[:8]}").start()
    except Exception:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(turn_id, None)  # スレッド起動失敗の予約取消
        raise
    return rec


def get_turn(turn_id: str) -> TurnRecord | None:
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        return _REGISTRY.get(turn_id)


def list_running(uid: str, *, all_users: bool = False) -> list[TurnRecord]:
    """指定ユーザーの実行中（未完了）ターン一覧。`all_users` は管理者向けで全員分を返す（呼び出し側で管理者判定済みのときだけ True）。
    会話未確定（予約中）のレコードは skip する。
    """
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        return [r for r in _REGISTRY.values()
                if (all_users or r.uid == uid) and not r.buffer.done and r.conversation_id is not None]


def stop_turn(turn_id: str, uid: str, *, is_admin: bool = False) -> bool:
    """本人の実行中ターンのみ停止できる（存在しない/他人/完了済みは False＝存在有無を教えない）。`is_admin` は本人一致を外す。"""
    rec = get_turn(turn_id)
    if rec is None or (rec.uid != uid and not is_admin) or rec.buffer.done:
        return False
    rec.stop_event.set()
    return True


def iter_sse(turn_id: str, uid: str, cursor: int, *, wait_timeout: float = 15.0) -> Iterator[str] | None:
    """SSE 本文（`data: ...\n\n`）を cursor から replay→追従で yield する。
    所有者不一致/存在しないターンは呼び出し側が先に `get_turn` で認可して 404 にする（本関数はレコード確定後のみ呼ばれる）。
    `wait_for_more` がタイムアウトしたら SSE コメント行（`: keepalive`）を yield し、切断済みなら generator を解放する。
    """
    rec = get_turn(turn_id)
    if rec is None or rec.uid != uid:
        return None

    def gen() -> Iterator[str]:
        c = cursor
        while True:
            for e in rec.buffer.replay_from(c):
                c = e.seq
                yield f"data: {json.dumps(e.payload, ensure_ascii=False, default=str)}\n\n"
            if rec.buffer.done:
                return  # 完了済み＝これ以上イベントは増えない
            progressed = rec.buffer.wait_for_more(c, timeout=wait_timeout)
            if not progressed:
                yield ": keepalive\n\n"  # 切断検知のための keepalive（EventSource はコメント行を無視）

    return gen()
