"""チャットターンの実行記録（プロセスが消えても、どこまで進んだかが残るようにする）。
設計: docs/design/chat.md「停止と同時実行」

- `RunJournal`: 1 ターン分。調査の記録（node イベント）を id ごとの最新で、回答の途中の本文（`answer_partial` イベント）を最新の 1 件で持ち、一定間隔で DB（`store.chat_runs`）へ小分けに保存し、終わったら終わりの状態を書く。
  `answer_partial` は記録だけが受け取り、画面へは流さない。
- `recover_unfinished`: 起動時に「実行中」のまま残った記録を中断にし、会話に途中までの記録・回答の途中の本文・注記を残す。
- `interrupt_run`: 実行中のターンを中断として記録する（起動時の回収と、停止時に終わらなかったターンの両方）。
- 保存の失敗は回答を止めない。記録の `save_failures` に回数を残し、ログに出す。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .store import chat_runs

_log = logging.getLogger("sherpa")

# 途中経過を保存する間隔（秒）。イベントが届いたときに、前回の保存から経っていれば保存する（タイマーのスレッドは持たない）。
FLUSH_INTERVAL_SECONDS = 3.0
# 1 ターンに保存する調査の記録と回答の途中の本文の合計の上限（JSON のバイト数）。超えたら調査の記録を古い順に落とし、落とした数を記録に残す。
# 回答の途中の本文はこのうち `MAX_PARTIAL_BYTES` までで、超えた分は末尾を切る。
MAX_PROGRESS_BYTES = 2_000_000
MAX_PARTIAL_BYTES = 500_000

INTERRUPTED_NOTICE = "Slouch が途中で止まったため、ここまでの回答です。"
_DROPPED_NOTICE = "調査の記録が長いため、古い分を省いています。"


class RunJournal:
    """1 ターンの実行記録。`observe` は実行スレッドから、停止処理の `flush` は別スレッドから呼ばれ得るため、記録の中身は `_lock` で守る。"""

    def __init__(self, *, turn_id: str, conversation_id: int, user_id: str,
                 started_at: datetime | None = None):
        self.turn_id = turn_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.started_at = started_at or datetime.now(timezone.utc)
        self._nodes: dict[str, dict] = {}
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self._partial = ""
        self._partial_bytes = 0
        self.events_dropped = 0
        self.save_failures = 0
        self._dirty = False
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()

    def begin(self) -> None:
        """開始の行を作る（失敗しても回答は止めない。次の保存で取り戻す）。"""
        self._safe(self._save)

    def wrap(self, emit: Callable[[dict], None]) -> Callable[[dict], None]:
        """イベントを記録してから `emit` へ渡す関数を返す。"""
        def _emit(payload: dict) -> None:
            self.observe(payload)
            if payload.get("type") != "answer_partial":
                emit(payload)
        return _emit

    def observe(self, payload: dict) -> None:
        if payload.get("type") == "answer_partial" and isinstance(payload.get("text"), str):
            with self._lock:
                self._put_partial(payload["text"])
            self._touch()
        elif payload.get("type") == "node" and payload.get("id"):
            with self._lock:
                self._put(str(payload["id"]), payload)
            self._touch()

    def _touch(self) -> None:
        self._dirty = True
        if time.monotonic() - self._last_flush >= FLUSH_INTERVAL_SECONDS:
            self.flush()

    def _put_partial(self, text: str) -> None:
        raw = text.encode("utf-8")[:MAX_PARTIAL_BYTES]
        self._partial = raw.decode("utf-8", errors="ignore")
        self._partial_bytes = len(self._partial.encode("utf-8"))
        self._trim_nodes()

    def _put(self, node_id: str, payload: dict) -> None:
        size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        self._bytes += size - self._sizes.get(node_id, 0)
        self._sizes[node_id] = size
        self._nodes[node_id] = payload
        self._trim_nodes()

    def _trim_nodes(self) -> None:
        while self._bytes + self._partial_bytes > MAX_PROGRESS_BYTES and len(self._nodes) > 1:
            oldest = next(iter(self._nodes))
            self._nodes.pop(oldest)
            self._bytes -= self._sizes.pop(oldest, 0)
            self.events_dropped += 1

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        if self._safe(self._save):
            self._dirty = False

    def snapshot(self) -> dict:
        """今の途中経過（調査の記録と回答の途中の本文）。"""
        with self._lock:
            nodes = list(self._nodes.values())
            partial = self._partial
        return {"nodes": nodes, **({"partial_text": partial} if partial else {})}

    def _save(self) -> None:
        snap = self.snapshot()
        chat_runs.save_progress(
            turn_id=self.turn_id, conversation_id=self.conversation_id, user_id=self.user_id,
            started_at=self.started_at, nodes=snap["nodes"],
            events_dropped=self.events_dropped, save_failures=self.save_failures,
            partial_text=snap.get("partial_text", ""))

    def finish(self, status: str, reason: str | None = None) -> None:
        """終わりの状態を書く。完了は途中経過を消す（回答と記録は会話に保存済み）。
        守ること: 完了以外（失敗・停止）は、途中の本文と調査の記録を終わりの状態と同じ書き込みで保存する（直前の小分けの保存からの分を失わない）。
        """
        progress = self.snapshot() if status != "completed" else None
        self._safe(lambda: chat_runs.finish_run(
            turn_id=self.turn_id, conversation_id=self.conversation_id, user_id=self.user_id,
            started_at=self.started_at, status=status, reason=reason,
            keep_progress=(status != "completed"), events_dropped=self.events_dropped,
            save_failures=self.save_failures, progress=progress))

    def _safe(self, fn) -> bool:
        try:
            fn()
            return True
        except Exception as e:
            self.save_failures += 1
            if self.save_failures <= 3:
                _log.warning("チャット実行の記録を保存できませんでした（回答は続けます）: turn_id=%s %s: %s",
                             self.turn_id, type(e).__name__, e)
            return False


def interrupt_run(run: dict, reason: str, progress: dict | None = None) -> bool:
    """実行中の記録 1 件を中断にし、会話に途中までの記録と注記を残す。実際に中断にしたら True。
    会話に、この実行の開始以降の assistant の回答が既にあれば（回答の保存後・終了の記録前に止まった）、注記は足さず状態だけを直す。
    `progress`（まだ動いているプロセスの手元の途中経過）を渡すと、DB の途中経過の代わりにそれを会話に残し、中断の状態と同じ書き込みで記録にも残す。
    """
    if progress is not None:
        run = {**run, "progress": progress}
    turn_id = run["turn_id"]
    cid = run["conversation_id"]
    msg_id = None
    if not chat_runs.has_assistant_message_since(cid, run["started_at"]):
        msg_id = _save_interrupted_message(run)
    changed = chat_runs.mark_interrupted(turn_id, reason, progress=progress)
    if changed and msg_id is not None:
        _audit_interrupted(run, msg_id)
    return changed


def _save_interrupted_message(run: dict) -> int | None:
    from . import answer_shape, store
    from .chat_service import _cap_trace_v2
    cid = run["conversation_id"]
    progress = run.get("progress") or {}
    nodes = {n["id"]: n for n in (progress.get("nodes") or []) if isinstance(n, dict) and n.get("id")}
    env = {"lens": "chat", "headline": str(progress.get("partial_text") or ""), "summary": {"total": 0}, "data": {}, "sources": [],
           "completion": "partial", "trace_version": 2}
    answer_shape.seal(env)
    answer_shape.add_notice(env, "recovered_error", INTERRUPTED_NOTICE)
    if run.get("events_dropped"):
        answer_shape.add_notice(env, "recovered_error", _DROPPED_NOTICE)
    try:
        personal = bool(store.conversation_is_personal_tainted(cid))
    except Exception:
        personal = True   # 判定できなければ個人扱いへ倒す
    msg = store.add_message(cid, "assistant", env["headline"], lens="chat", answer=env,
                            trace=_cap_trace_v2(nodes) if nodes else None, personal=personal)
    return msg["id"]


def _audit_interrupted(run: dict, assistant_msg_id: int) -> None:
    from . import store
    try:
        store.audit(run["user_id"], "chat.turn", "conversation", f"conv:{run['conversation_id']}",
                    detail={"lens": "interrupted", "message_id_assistant": assistant_msg_id},
                    outcome="error", severity="warning")
    except Exception as e:
        _log.warning("中断したターンの監査を残せませんでした（best-effort）: %s", e)


def recover_unfinished() -> list[str]:
    """「実行中」のまま残った記録をすべて中断にして回収する（単一 worker の起動時＝実行中のターンは 1 つも無い）。回収した turn_id を返す。"""
    recovered: list[str] = []
    for run in chat_runs.list_running():
        try:
            if interrupt_run(run, "process_exit"):
                recovered.append(run["turn_id"])
        except Exception as e:
            _log.warning("実行中のまま残ったチャットの回収に失敗しました: turn_id=%s %s: %s",
                         run.get("turn_id"), type(e).__name__, e)
    return recovered
