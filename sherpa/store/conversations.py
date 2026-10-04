"""会話・メッセージの保存と参照（`conversations`/`messages`）。
結果カードは assistant メッセージの `answer`(JSONB) に格納し、`route`/`trace`/`lens` も持つ。
`accept_share`（shares.py）と `delete_conversation` は同じ conversations 行を `SELECT ... FOR UPDATE` でロックして直列化する（行ロックによるため import で結合しない）。
設計: docs/design/chat.md「会話の保存と継続」
"""
from __future__ import annotations

from psycopg.types.json import Json

from .db import _connect, _ensure
from .turn_metrics import upsert_best_effort as _turn_metrics_upsert_best_effort

# 共有の既定有効日数。`expires_at IS NULL` の行は読み取り時に作成日時＋この日数で失効扱いにする（DB は書き換えない）。
SHARE_DEFAULT_EXPIRY_DAYS = 30
# 共有行（`conversation_shares`・別名つきは `_s` 版）の実効期限 SQL 式。
SHARE_EFFECTIVE_EXPIRES_SQL = f"COALESCE(expires_at, created_at + interval '{SHARE_DEFAULT_EXPIRY_DAYS} days')"


def create_conversation(user_id="admin", world="v1", title=None) -> dict:
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO conversations (user_id, version, title) VALUES (%s,%s,%s) "
            "RETURNING id, user_id, version, title, codex_session_id, created_at, updated_at",
            (user_id, world, title),
        ).fetchone()


def add_message(conversation_id, role, content="", lens=None,
                route=None, trace=None, answer=None, personal=False) -> dict:
    """メッセージを1件追加し、会話の updated_at を進める。personal=True はそのターンが個人利用（sanitized share 用）。
    role='assistant' かつ answer が dict のとき、同じトランザクションで `turn_metrics`/`turn_tool_stats` へも書く。この書込は `upsert_best_effort` が savepoint で保護するため、失敗してもメッセージ保存は失敗させない。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, route, trace, answer, personal) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id, conversation_id, role, content, lens, route, trace, answer, personal, created_at",
            (conversation_id, role, content, lens,
             Json(route) if route is not None else None,
             Json(trace) if trace is not None else None,
             Json(answer) if answer is not None else None, personal),
        ).fetchone()
        c.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (conversation_id,))
        if role == "assistant" and isinstance(answer, dict):
            _turn_metrics_upsert_best_effort(
                c, message_id=row["id"], conversation_id=conversation_id,
                created_at=row["created_at"], lens=lens, personal=personal, answer=answer)
        return row


def recent_messages(conversation_id, limit) -> list:
    """直近 `limit` 件のメッセージを軽量に返す（id/role/content のみ・時系列昇順・履歴の読み込み用）。"""
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT id, role, content FROM messages WHERE conversation_id=%s "
            "ORDER BY id DESC LIMIT %s", (conversation_id, limit),
        ).fetchall()
    return list(reversed(rows))


def set_message_personal(message_id) -> None:
    """指定メッセージを個人利用ターンとしてマークする（sanitized share の redaction 対象）。"""
    _ensure()
    with _connect() as c:
        c.execute("UPDATE messages SET personal=TRUE WHERE id=%s", (message_id,))


_DEFAULT_LIST_LIMIT = 50


def _visible_conversations_rows(c, user_id, limit) -> list:
    """所有会話＋受領共有ラッパーの可視行を取得する（`list_conversations`/`search_conversations` 共通の可視集合判定）。
    公開行に出さない `share_id`/`source_conversation_id` も含み、`_public_conversation_row` が落とす。
    フォークで複製した会話は `forked_from_*`/`forked_at`（出所表示用）を持つ。フォーク判定は `forked_at IS NOT NULL`（`forked_from_share_id` は共有削除で NULL になるため）。
    """
    return c.execute(
        "SELECT c.id, c.title, c.version, c.pinned, c.updated_at, c.origin, c.read_only, c.received_at, "
        "c.shared_by_user_id, u.display_name AS shared_by_name, "
        "c.forked_from_share_id, c.forked_from_user_id, fu.display_name AS forked_from_name, c.forked_at, "
        "c.share_id, c.source_conversation_id, "
        "CASE WHEN c.origin='received_share' THEN "
        "  (SELECT CASE WHEN s.revoked_at IS NOT NULL THEN 'revoked' "
        f"               WHEN COALESCE(s.expires_at, s.created_at + interval '{SHARE_DEFAULT_EXPIRY_DAYS} days')<=now() THEN 'expired' "
        "               ELSE 'active' END "
        "   FROM conversation_shares s WHERE s.id=c.share_id) ELSE NULL END AS share_status, "
        "CASE WHEN c.origin='received_share' THEN "
        f"  (SELECT COALESCE(s.expires_at, s.created_at + interval '{SHARE_DEFAULT_EXPIRY_DAYS} days') "
        "   FROM conversation_shares s WHERE s.id=c.share_id) ELSE NULL END AS share_expires_at "
        "FROM conversations c LEFT JOIN users u ON u.uid=c.shared_by_user_id "
        "LEFT JOIN users fu ON fu.uid=c.forked_from_user_id "
        "WHERE c.user_id=%s AND c.deleted_at IS NULL AND c.origin<>'sanitized_snapshot' "
        "ORDER BY c.origin, c.pinned DESC, c.updated_at DESC LIMIT %s",
        (user_id, limit),
    ).fetchall()


def _public_conversation_row(r) -> dict:
    """`_visible_conversations_rows` の1行を `GET /conversations` の公開形へ変換する（内部専用列を落とし、`forked_from` を組み立てる）。共有削除後も `forked_from.share_id` は NULL を許し、`user_id`/`name`/`at` は残る。"""
    forked_from = None
    if r["forked_at"] is not None:
        forked_from = {"share_id": r["forked_from_share_id"], "user_id": r["forked_from_user_id"],
                      "name": r["forked_from_name"], "at": r["forked_at"]}
    return {k: v for k, v in r.items()
           if k not in ("forked_from_share_id", "forked_from_user_id", "forked_from_name", "forked_at",
                        "share_id", "source_conversation_id")} | {"forked_from": forked_from}


def list_conversations(user_id="admin", limit=_DEFAULT_LIST_LIMIT) -> list:
    """自分の会話＋受領共有ラッパーを返す（origin/read_only/shared_by/share_status 付き・削除済みは除外）。"""
    _ensure()
    with _connect() as c:
        return [_public_conversation_row(r) for r in _visible_conversations_rows(c, user_id, limit)]


def _resolve_received_share_msg_src(c, uid, share_id, source_conversation_id):
    """受領共有ラッパーの本文所在を判定する（`get_conversation_for_read`・`search_conversations` 共通・呼び出し側の接続 `c` 上で実行）。
    共有が有効（取消なし・期限内・招待済み）かつ元会話が個人 workspace 参照でブロックされていなければ `(source_conversation_id, None)`、無効なら `(None, "unavailable")`、個人参照でブロックなら `(None, "personal_blocked")`。
    """
    share = c.execute(
        f"SELECT (revoked_at IS NULL AND {SHARE_EFFECTIVE_EXPIRES_SQL}>now()) AS active "
        "FROM conversation_shares WHERE id=%s", (share_id,)).fetchone()
    invited = c.execute("SELECT 1 FROM conversation_share_invites "
                        "WHERE share_id=%s AND invitee_user_id=%s", (share_id, uid)).fetchone()
    if not share or not share["active"] or not invited:
        return None, "unavailable"
    src_conv = c.execute(
        "SELECT contains_personal_workspace FROM conversations WHERE id=%s",
        (source_conversation_id,)).fetchone()
    if src_conv and src_conv["contains_personal_workspace"]:
        return None, "personal_blocked"
    return source_conversation_id, None


_SEARCH_SNIPPET_RADIUS = 60  # 抜粋は最初の一致位置の前後 60 字。


def _search_snippet(text, q) -> str | None:
    """`text` 内で `q`（大小文字を区別しない）が最初に現れた位置の前後 `_SEARCH_SNIPPET_RADIUS` 字を返す。一致しなければ None。"""
    if not text:
        return None
    idx = text.lower().find(q.lower())
    if idx < 0:
        return None
    start = max(0, idx - _SEARCH_SNIPPET_RADIUS)
    end = min(len(text), idx + len(q) + _SEARCH_SNIPPET_RADIUS)
    return text[start:end]


def search_conversations(user_id, q) -> list:
    """本人が読める会話（自分の会話＋有効な受領共有）のタイトル・本文を検索する。
    可視集合は `_visible_conversations_rows` を再利用する。無効（取消・期限切れ・招待外）・個人ブロックの共有はタイトルだけを対象にする。タイトル一致を本文一致より優先し（`match.where`）、本文は `messages.content`/`answer->>'headline'` への ILIKE 1回で探す。どちらにも一致しない行は返さない。
    """
    _ensure()
    with _connect() as c:
        rows = _visible_conversations_rows(c, user_id, _DEFAULT_LIST_LIMIT)
        msg_src_by_cid: dict = {}  # 可視行 id -> 本文を読みに行く先（own は自分自身・received_share は元会話）。
        for r in rows:
            if r["origin"] == "received_share":
                resolved, _status = _resolve_received_share_msg_src(
                    c, user_id, r["share_id"], r["source_conversation_id"])
                if resolved is not None:
                    msg_src_by_cid[r["id"]] = resolved
            else:
                msg_src_by_cid[r["id"]] = r["id"]
        content_hits: dict = {}  # 本文所在 id -> 抜粋（複数一致は id が最大＝最新を採用）。
        msg_src_ids = sorted(set(msg_src_by_cid.values()))
        if msg_src_ids:
            # ILIKE の `%`/`_` とエスケープ文字自身をリテラル化する。
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            msg_rows = c.execute(
                "SELECT conversation_id, content, answer->>'headline' AS headline FROM messages "
                "WHERE conversation_id = ANY(%s) "
                "  AND (content ILIKE %s ESCAPE '\\' OR answer->>'headline' ILIKE %s ESCAPE '\\') "
                "ORDER BY id DESC",
                (msg_src_ids, like, like),
            ).fetchall()
            for mr in msg_rows:
                cid = mr["conversation_id"]
                if cid in content_hits:
                    continue
                snippet = _search_snippet(mr["content"], q) or _search_snippet(mr["headline"], q)
                if snippet is not None:
                    content_hits[cid] = snippet
        out = []
        for r in rows:
            row = _public_conversation_row(r)
            title_snippet = _search_snippet(row["title"] or "", q)
            if title_snippet is not None:
                row["match"] = {"where": "title", "snippet": title_snippet}
            else:
                msg_src = msg_src_by_cid.get(r["id"])
                snippet = content_hits.get(msg_src) if msg_src is not None else None
                if snippet is None:
                    continue
                row["match"] = {"where": "message", "snippet": snippet}
            out.append(row)
        return out


def delete_conversation(conversation_id, user_id="admin") -> bool:
    """会話を削除する（所有者一致のみ）。
    生きた受領共有ラッパー（origin='received_share' AND deleted_at IS NULL）が `source_conversation_id` として参照している場合は soft delete（deleted_at=now()）にとどめ、無ければ物理削除する（messages は FK CASCADE）。取消・期限切れのアクセス遮断には影響しない。
    `accept_share` との競合防止のため、対象行を `SELECT ... FOR UPDATE` でロックしてから wrapper 有無の判定と削除を行う。
    """
    _ensure()
    with _connect() as c:
        locked = c.execute(
            "SELECT id FROM conversations WHERE id=%s FOR UPDATE", (conversation_id,)).fetchone()
        if not locked:
            return False
        has_live_wrapper = c.execute(
            "SELECT 1 FROM conversations WHERE source_conversation_id=%s "
            "  AND origin='received_share' AND deleted_at IS NULL LIMIT 1",
            (conversation_id,)).fetchone()
        if has_live_wrapper:
            n = c.execute(
                "UPDATE conversations SET deleted_at=now() "
                "WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
                (conversation_id, user_id)).rowcount
            if n > 0:
                # soft delete では messages が残り FK CASCADE が効かないため、message_feedback を明示的に消す。
                c.execute(
                    "DELETE FROM message_feedback WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=%s)",
                    (conversation_id,))
                # 調査の記録も同様に消す。
                c.execute("DELETE FROM investigation_records WHERE conversation_id=%s",
                          (conversation_id,))
        else:
            n = c.execute("DELETE FROM conversations WHERE id=%s AND user_id=%s",
                          (conversation_id, user_id)).rowcount
    return n > 0


def set_pinned(conversation_id, pinned: bool, user_id="admin") -> bool:
    """会話のピン止めを設定/解除する（所有者一致のみ・soft-delete 済みは対象外）。"""
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE conversations SET pinned=%s WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
            (bool(pinned), conversation_id, user_id)).rowcount
    return n > 0


def rename_conversation(conversation_id, title, user_id="admin") -> bool:
    """会話のタイトルを変更する（所有者一致のみ・soft-delete 済みは対象外・updated_at は変えない）。"""
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE conversations SET title=%s WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
            (title, conversation_id, user_id)).rowcount
    return n > 0


def set_session_id(conversation_id, session_id) -> None:
    _ensure()
    with _connect() as c:
        c.execute("UPDATE conversations SET codex_session_id=%s WHERE id=%s",
                  (session_id, conversation_id))


def get_session_id(conversation_id) -> str | None:
    """会話に紐づく直近の `codex_session_id`（Codex の resume 判定用）。無い/未設定なら None。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT codex_session_id FROM conversations WHERE id=%s", (conversation_id,)).fetchone()
        return row["codex_session_id"] if row else None


def get_codex_usage_total(conversation_id) -> dict | None:
    """会話の直近 assistant メッセージが記録した Codex 累計 usage（`answer->'codex_usage_total'`）。無い/未設定なら None。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT answer->'codex_usage_total' AS codex_usage_total FROM messages "
            "WHERE conversation_id=%s AND role='assistant' "
            "ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
        return row["codex_usage_total"] if row else None


def owns_conversation(uid, cid) -> bool:
    """current user が書き込み可能な所有会話か（origin='own'）。"""
    _ensure()
    with _connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM conversations WHERE id=%s AND user_id=%s AND origin='own' AND deleted_at IS NULL",
            (cid, uid)).fetchone())


def owns_assistant_message(uid, conversation_id, message_id) -> bool:
    """`message_id` が `conversation_id`（自分の所有会話・origin='own'）に属する assistant メッセージか（フィードバック投稿対象の検証用）。"""
    _ensure()
    with _connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id=%s AND m.conversation_id=%s AND m.role='assistant' "
            "AND c.user_id=%s AND c.origin='own' AND c.deleted_at IS NULL",
            (message_id, conversation_id, uid)).fetchone())


def is_personal_tainted(message: dict) -> bool:
    """メッセージ1件が個人情報由来か。`messages.personal` 列を優先し、無ければ（旧行）`answer` 内の旧マーカー（personal_sources／_personal_facts／codex_wrote_files）で判定する。`shares.py::create_sanitized_snapshot` の taint 判定と同じ基準。"""
    if message.get("personal"):
        return True
    answer = message.get("answer")
    if not isinstance(answer, dict):
        return False
    return bool(answer.get("personal_sources") or answer.get("_personal_facts")
               or answer.get("codex_wrote_files"))


def list_export_messages(*, time_from, cursor_id: int | None, limit: int) -> list[dict]:
    """改善ログエクスポート用: `time_from` 以降の assistant メッセージを新しい順（id 降順）でページング取得する（`cursor_id` 指定時はそれより小さい id のみ）。
    質問（user メッセージ）は `chat.turn` 監査ログの `message_id_user`/`message_id_assistant` で対応付ける。対応付けられないターンは個人情報の有無が確認できないため内部結合で除外する（fail-closed）。質問側の `personal`/`answer` も同梱し、呼び出し側が `is_personal_tainted` を両方に適用する。
    sanitized share の複製（`origin='sanitized_snapshot'`）と論理削除済みの会話は除外する。
    """
    _ensure()
    cursor_clause = "AND m.id < %s" if cursor_id is not None else ""
    params: list = [time_from]
    if cursor_id is not None:
        params.append(cursor_id)
    params.append(limit)
    with _connect() as c:
        return c.execute(
            "SELECT m.id, m.conversation_id, m.created_at, m.content, m.trace, m.answer, "
            "  m.personal, u.content AS question, u.personal AS question_personal, "
            "  u.answer AS question_answer "
            "FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "JOIN LATERAL ( "
            "  SELECT (a.detail->>'message_id_user')::integer AS uid "
            "  FROM audit_log a "
            "  WHERE a.action = 'chat.turn' "
            "    AND (a.detail->>'message_id_assistant')::integer = m.id "
            "  ORDER BY a.id DESC LIMIT 1 "
            ") au ON true "
            "JOIN messages u ON u.id = au.uid "
            f"WHERE m.role = 'assistant' AND m.created_at >= %s {cursor_clause} "
            "  AND c.origin <> 'sanitized_snapshot' AND c.deleted_at IS NULL "
            "ORDER BY m.id DESC LIMIT %s",
            params,
        ).fetchall()


def conversation_has_personal_message(cid) -> bool:
    """会話に個人ターン（messages.personal=TRUE）が1件でもあるか（会話フラグとズレても漏らさないための多層防御）。"""
    _ensure()
    with _connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM messages WHERE conversation_id=%s AND personal=TRUE LIMIT 1",
            (cid,)).fetchone())


def conversation_is_personal_tainted(cid) -> bool:
    """会話が個人由来か（後続ターンの個人扱い判定用）。会話フラグ `contains_personal_workspace`・`messages.personal=TRUE`・旧行の `answer` マーカーのいずれかで真（`is_personal_tainted` と同じ基準）。"""
    _ensure()
    with _connect() as c:
        row = c.execute("SELECT contains_personal_workspace FROM conversations WHERE id=%s",
                        (cid,)).fetchone()
        if row and row.get("contains_personal_workspace"):
            return True
        rows = c.execute(
            "SELECT personal, answer FROM messages WHERE conversation_id=%s AND "
            "(personal=TRUE OR answer ?| array['personal_sources','_personal_facts','codex_wrote_files'])",
            (cid,)).fetchall()
    return any(is_personal_tainted(dict(r)) for r in rows)


def set_contains_personal_workspace(conversation_id: int) -> None:
    """会話に個人 workspace 参照フラグを立てる（冪等・FALSE→TRUE のみ）。
    このフラグが TRUE の会話は POST /conversations/{cid}/shares が 409 を返す。
    """
    _ensure()
    with _connect() as c:
        c.execute(
            "UPDATE conversations SET contains_personal_workspace=TRUE WHERE id=%s",
            (conversation_id,),
        )
