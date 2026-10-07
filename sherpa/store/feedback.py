"""回答ごとの利用者フィードバック（`message_feedback`）。
1利用者×1メッセージにつき最新1件のみ保持する（再送は上書き）。本文は複製せず `message_id` で参照する。投稿の認可は呼び出し側（`conversations.py::owns_assistant_message`）。
"""
from __future__ import annotations

from .db import _connect, _ensure

# 定型タグの閉じた語彙（保存値は英語スラッグ）。
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
    """message_id → フィードバック辞書（改善ログ・管理者集計の一括 join 用）。1メッセージにつき最新1件。"""
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
    """message_id → `user_id` 自身のフィードバック辞書（会話履歴の復元表示用・他人のものは返さない）。"""
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


def list_feedback_turns(*, time_from, before_id: int | None, limit: int,
                        rating: str | None = None, tag: str | None = None) -> list[dict]:
    """管理者の評価画面用: `time_from` 以降に付いたフィードバックを、対象の回答・質問と一緒に新しい順（フィードバック id 降順）で返す。

    `before_id` 指定時はそれより小さい id のみ。質問の対応付け・論理削除済み会話と共有の複製の除外は `list_export_messages` と同じ
    （`chat.turn` 監査で対応付けられないターンは除く）。個人由来の判定は呼び出し側が行う。
    """
    _ensure()
    where = ["f.created_at >= %s", "m.role = 'assistant'",
             "c.origin <> 'sanitized_snapshot'", "c.deleted_at IS NULL"]
    params: list = [time_from]
    if before_id is not None:
        where.append("f.id < %s")
        params.append(before_id)
    if rating is not None:
        where.append("f.rating = %s")
        params.append(rating)
    if tag is not None:
        where.append("%s = ANY(f.tags)")
        params.append(tag)
    params.append(limit)
    with _connect() as c:
        return c.execute(
            "SELECT f.id AS feedback_id, f.user_id AS feedback_user_id, f.rating, f.tags, f.comment, "
            "  f.created_at AS feedback_created_at, usr.display_name AS feedback_user_name, "
            "  m.id, m.conversation_id, m.created_at, m.content, m.trace, m.answer, m.personal, m.lens, "
            "  u.content AS question, u.personal AS question_personal, u.answer AS question_answer "
            "FROM message_feedback f "
            "JOIN messages m ON m.id = f.message_id "
            "JOIN conversations c ON c.id = m.conversation_id "
            "JOIN LATERAL ( "
            "  SELECT (a.detail->>'message_id_user')::integer AS uid "
            "  FROM audit_log a "
            "  WHERE a.action = 'chat.turn' "
            "    AND (a.detail->>'message_id_assistant')::integer = m.id "
            "  ORDER BY a.id DESC LIMIT 1 "
            ") au ON true "
            "JOIN messages u ON u.id = au.uid "
            "LEFT JOIN users usr ON usr.uid = f.user_id "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY f.id DESC LIMIT %s",
            params,
        ).fetchall()
