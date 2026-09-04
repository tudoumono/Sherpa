"""回答ごとの利用者フィードバック（`message_feedback`）。

投稿は会話の所有者のみ（認可判定は `conversations.py::owns_assistant_message` 側）。1利用者×
1メッセージにつき最新1件のみ保持する（同一ペアの再送は上書き）。質問/回答本文はここに複製しない
（`message_id` で `messages` を参照するだけ）。
"""
from __future__ import annotations

from .db import _connect, _ensure

# 定型タグの閉じた語彙（画面文言は日本語ラベル・値はこの英語スラッグで保存する）。
MESSAGE_FEEDBACK_TAGS = ("wrong_evidence", "incomplete", "outdated", "slow")

# 一言コメントの文字数上限。
MESSAGE_FEEDBACK_COMMENT_MAX_LEN = 500


def upsert_message_feedback(message_id: int, user_id: str, rating: str,
                            tags: list[str] | None, comment: str | None) -> dict:
    """フィードバックを1件保存する（同一 message_id+user_id の再送は上書き）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO message_feedback (message_id, user_id, rating, tags, comment) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (message_id, user_id) DO UPDATE SET "
            "  rating = EXCLUDED.rating, tags = EXCLUDED.tags, comment = EXCLUDED.comment, "
            "  created_at = now() "
            "RETURNING id, message_id, user_id, rating, tags, comment, created_at",
            (message_id, user_id, rating, list(tags or []), comment),
        ).fetchone()


def get_feedback_by_message_ids(ids: list[int]) -> dict[int, dict]:
    """message_id → フィードバック辞書（改善ログエクスポート/管理者チャット集計の一括 join 専用）。
    1メッセージにつき最新（created_at 降順）の1件を返す。
    """
    if not ids:
        return {}
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT DISTINCT ON (message_id) message_id, rating, tags, comment, created_at "
            "FROM message_feedback WHERE message_id = ANY(%s) "
            "ORDER BY message_id, created_at DESC",
            (list(ids),),
        ).fetchall()
    return {r["message_id"]: r for r in rows}


def get_feedback_by_message_ids_for_user(ids: list[int], user_id: str) -> dict[int, dict]:
    """message_id → `user_id` 自身のフィードバック辞書（会話履歴の復元表示専用）。
    `message_feedback` は `(message_id, user_id)` に UNIQUE 制約があるため、この絞り込みで
    1メッセージにつき高々1件になる。他人のフィードバックは返さない。
    """
    if not ids:
        return {}
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT message_id, rating, tags, comment FROM message_feedback "
            "WHERE message_id = ANY(%s) AND user_id = %s",
            (list(ids), user_id),
        ).fetchall()
    return {r["message_id"]: r for r in rows}
