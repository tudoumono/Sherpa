"""チャットターンの実行記録（`chat_turn_runs`）の永続化。読み書きの唯一の窓口。
1 ターン 1 行。状態は `running`/`completed`/`stopped`/`failed`/`interrupted` の 5 値。`progress` は実行中の調査の記録（node イベント）の途中経過で、
プロセスが消えたあとの回収（`chat_run_journal.recover_unfinished`）が会話に残す材料になる。会話を消すと FK の CASCADE で一緒に消える。
表の作成はこのモジュールが持つ（`_SCHEMA` には含めず、最初の利用時に 1 回だけ冪等に作る）。
設計: docs/design/chat.md「停止と同時実行」
"""
from __future__ import annotations

import threading
from datetime import datetime

from psycopg.types.json import Json

from .db import _connect, _dsn, _ensure

STATUSES = ("running", "completed", "stopped", "failed", "interrupted")

_DDL = (
    """CREATE TABLE IF NOT EXISTS chat_turn_runs (
        turn_id TEXT PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        progress JSONB,
        events_dropped INTEGER NOT NULL DEFAULT 0,
        save_failures INTEGER NOT NULL DEFAULT 0,
        end_reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS chat_turn_runs_running ON chat_turn_runs(started_at) WHERE status='running'",
    "CREATE INDEX IF NOT EXISTS chat_turn_runs_conversation ON chat_turn_runs(conversation_id)",
)

_ready_dsn: str | None = None
_ready_lock = threading.Lock()


def _ensure_table() -> None:
    """接続先ごとに 1 回だけ表を作る（`_SCHEMA` の初期化が済んでから）。"""
    global _ready_dsn
    dsn = _dsn()
    if _ready_dsn == dsn:
        return
    with _ready_lock:
        if _ready_dsn == dsn:
            return
        _ensure()
        with _connect() as c:
            for stmt in _DDL:
                c.execute(stmt)
        _ready_dsn = dsn


def save_progress(*, turn_id: str, conversation_id: int, user_id: str, started_at: datetime,
                  nodes: list[dict], events_dropped: int, save_failures: int, partial_text: str = "") -> None:
    """途中経過（調査の記録と、あれば回答の途中の本文）を保存する（行が無ければ作る＝開始時の保存に失敗していても取り戻せる）。終了済みの行は書き換えない。"""
    _ensure_table()
    with _connect() as c:
        c.execute(
            "INSERT INTO chat_turn_runs (turn_id, conversation_id, user_id, status, started_at, "
            "  progress, events_dropped, save_failures) "
            "VALUES (%s,%s,%s,'running',%s,%s,%s,%s) "
            "ON CONFLICT (turn_id) DO UPDATE SET progress=EXCLUDED.progress, "
            "  events_dropped=EXCLUDED.events_dropped, save_failures=EXCLUDED.save_failures "
            "WHERE chat_turn_runs.status='running'",
            (turn_id, conversation_id, user_id, started_at,
             Json({"nodes": nodes, **({"partial_text": partial_text} if partial_text else {})}),
             events_dropped, save_failures))


def finish_run(*, turn_id: str, conversation_id: int, user_id: str, started_at: datetime,
               status: str, reason: str | None, keep_progress: bool, events_dropped: int,
               save_failures: int, progress: dict | None = None) -> bool:
    """実行中の行を終わりの状態にする。`running` のままの行だけを更新し、既に終わった行（停止処理が先に中断にした等）は触らない。
    更新したら True。`keep_progress=False`（完了）は途中経過を消す（回答と記録は `messages` に保存済み）。
    `progress` を渡すと、終わりの状態と同じ書き込みで途中経過をその値にする（失敗・停止の直前の記録を状態と一緒に残す）。
    """
    if status not in STATUSES or status == "running":
        raise ValueError(f"invalid final status: {status!r}")
    _ensure_table()
    with _connect() as c:
        c.execute(
            "INSERT INTO chat_turn_runs (turn_id, conversation_id, user_id, status, started_at) "
            "VALUES (%s,%s,%s,'running',%s) ON CONFLICT (turn_id) DO NOTHING",
            (turn_id, conversation_id, user_id, started_at))
        n = c.execute(
            "UPDATE chat_turn_runs SET status=%s, finished_at=now(), end_reason=%s, "
            "  events_dropped=%s, save_failures=%s, "
            "  progress=CASE WHEN NOT %s THEN NULL WHEN %s::jsonb IS NOT NULL THEN %s::jsonb ELSE progress END "
            "WHERE turn_id=%s AND status='running'",
            (status, reason, events_dropped, save_failures, keep_progress,
             Json(progress) if progress is not None else None, Json(progress) if progress is not None else None,
             turn_id)).rowcount
    return n > 0


def list_running() -> list[dict]:
    """状態が `running` のままの行（古い順）。"""
    _ensure_table()
    with _connect() as c:
        return c.execute(
            "SELECT turn_id, conversation_id, user_id, started_at, progress, events_dropped, save_failures "
            "FROM chat_turn_runs WHERE status='running' ORDER BY started_at").fetchall()


def get_run(turn_id: str) -> dict | None:
    _ensure_table()
    with _connect() as c:
        return c.execute(
            "SELECT turn_id, conversation_id, user_id, status, started_at, finished_at, progress, "
            "  events_dropped, save_failures, end_reason FROM chat_turn_runs WHERE turn_id=%s",
            (turn_id,)).fetchone()


def mark_interrupted(turn_id: str, reason: str, progress: dict | None = None) -> bool:
    """`running` の行を `interrupted` にする。更新したら True（他が先に終わらせていたら False）。
    `progress` を渡すと、同じ書き込みで途中経過をその値にする。
    """
    _ensure_table()
    with _connect() as c:
        n = c.execute(
            "UPDATE chat_turn_runs SET status='interrupted', finished_at=now(), end_reason=%s, "
            "  progress=COALESCE(%s::jsonb, progress) "
            "WHERE turn_id=%s AND status='running'",
            (reason, Json(progress) if progress is not None else None, turn_id)).rowcount
    return n > 0


def has_assistant_message_since(conversation_id: int, since: datetime) -> bool:
    """会話に、`since` 以降の assistant メッセージがあるか（回答は保存済みなのに終了の記録だけが残らなかったターンの見分け）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT 1 FROM messages WHERE conversation_id=%s AND role='assistant' AND created_at >= %s LIMIT 1",
            (conversation_id, since)).fetchone() is not None
