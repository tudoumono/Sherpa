"""テストの準備・検証用に DB を直接読む／書く小さなヘルパ（製品コードは使わない）。"""
from __future__ import annotations

from sherpa.store.db import _connect, _ensure


def get_conversation(conversation_id) -> dict | None:
    """会話行とメッセージ全件を返す。無ければ None。"""
    _ensure()
    with _connect() as c:
        conv = c.execute(
            "SELECT id, user_id, version, title, codex_session_id, created_at, updated_at "
            "FROM conversations WHERE id=%s", (conversation_id,),
        ).fetchone()
        if not conv:
            return None
        msgs = c.execute(
            "SELECT id, role, content, lens, route, trace, answer, personal, created_at "
            "FROM messages WHERE conversation_id=%s ORDER BY id", (conversation_id,),
        ).fetchall()
        return {"conversation": conv, "messages": msgs}


def mark_workspace_file_expired(file_id: int) -> bool:
    """台帳行を status='expired' に強制更新する（競合チェックなし）。"""
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE personal_workspace_files "
            "SET status='expired', deleted_at=now() "
            "WHERE id=%s AND status='uploaded'",
            (file_id,),
        ).rowcount
    return n > 0
