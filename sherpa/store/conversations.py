"""会話・メッセージ（M8・DATA-MODEL conversations/messages の MVP 部分集合）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S10）。ロジックは一切変更していない。
Postgres に会話とメッセージを保存する。結果カードは assistant メッセージの `answer`(JSONB) に
格納し、`route`/`trace`/`lens` も持つ（経路チップ R2・トレース R8）。

`accept_share`（shares.py）と `delete_conversation`（本モジュール）は同一 conversations 行を
`SELECT ... FOR UPDATE` でロックすることで競合を直列化する契約がある（両関数の docstring 参照）。
この2関数はモジュールをまたぐが、Python の関数呼び出しで結合しているわけではなく、どちらも
同じ Postgres トランザクション機構（行ロック）を経由して直列化されるため、モジュール間の
import は不要（純移動でこの契約は変わらない）。
"""
from __future__ import annotations

from psycopg.types.json import Json

from .db import _connect, _ensure


def create_conversation(user_id="admin", world="v1", title=None) -> dict:
    # 列名 `version` は歴史的（DB 不変・語彙統一のスコープ外）。引数/値は world 用語。
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO conversations (user_id, version, title) VALUES (%s,%s,%s) "
            "RETURNING id, user_id, version, title, codex_session_id, created_at, updated_at",
            (user_id, world, title),
        ).fetchone()


def add_message(conversation_id, role, content="", lens=None,
                route=None, trace=None, answer=None, personal=False) -> dict:
    """メッセージを1件追加し、会話の updated_at を進める。personal=True＝そのターンが個人利用（sanitized share 用）。"""
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
        return row


def recent_messages(conversation_id, limit) -> list:
    """直近 `limit` 件のメッセージを軽量に返す（id/role/content のみ・時系列昇順）。

    R1a（会話継続・履歴 priming）: `get_conversation` は answer/trace の JSONB まで全件取得するため、
    毎ターンの履歴読みに使うには重い。本関数は列を絞った `ORDER BY id DESC LIMIT` で取得し、
    呼び出し側（chat_service）が使いやすい昇順（古い→新しい）に戻して返す。
    """
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT id, role, content FROM messages WHERE conversation_id=%s "
            "ORDER BY id DESC LIMIT %s", (conversation_id, limit),
        ).fetchall()
    return list(reversed(rows))


def set_message_personal(message_id) -> None:
    """指定メッセージを個人利用ターンとしてマーク（sanitized share の redaction 対象にする）。"""
    _ensure()
    with _connect() as c:
        c.execute("UPDATE messages SET personal=TRUE WHERE id=%s", (message_id,))


def get_conversation(conversation_id) -> dict | None:
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


def list_conversations(user_id="admin", limit=50) -> list:
    """自分の会話＋受領共有ラッパーを返す（origin/read_only/shared_by/share_status 付き・削除済みは除外）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT c.id, c.title, c.version, c.pinned, c.updated_at, c.origin, c.read_only, c.received_at, "
            "c.shared_by_user_id, u.display_name AS shared_by_name, "
            "CASE WHEN c.origin='received_share' THEN "
            "  (SELECT CASE WHEN s.revoked_at IS NOT NULL THEN 'revoked' "
            "               WHEN s.expires_at IS NOT NULL AND s.expires_at<=now() THEN 'expired' "
            "               ELSE 'active' END "   # expires_at IS NULL = 無期限（常に active 側）
            "   FROM conversation_shares s WHERE s.id=c.share_id) ELSE NULL END AS share_status "
            "FROM conversations c LEFT JOIN users u ON u.uid=c.shared_by_user_id "
            "WHERE c.user_id=%s AND c.deleted_at IS NULL AND c.origin<>'sanitized_snapshot' "  # snapshot は内部成果物＝非表示
            "ORDER BY c.origin, c.pinned DESC, c.updated_at DESC LIMIT %s",   # own が先・ピンは上部（#8）
            (user_id, limit),
        ).fetchall()


def delete_conversation(conversation_id, user_id="admin") -> bool:
    """会話を削除。所有者一致のみ。

    生きた受領共有ラッパー（origin='received_share' AND deleted_at IS NULL）がこの会話を
    source_conversation_id として参照している場合、物理削除すると受領側が読めなくなるため
    **soft delete**（deleted_at=now()）にとどめる（所有者の一覧/操作からは既存の
    `deleted_at IS NULL` フィルタで消える・受領側はこれまで通り読める）。
    参照するラッパーが無ければ従来どおり物理削除（messages は FK ON DELETE CASCADE で一緒に消える・#6）。
    取消(revoke)・期限切れによるアクセス遮断は本関数と無関係で、これまで通り有効
    （docs/proposals/2026-07-02-共有の無期限と永続化.md）。

    RV HIGH: `accept_share` との競合防止。対象行を `SELECT ... FOR UPDATE` で先にロックしてから
    wrapper 有無を判定・削除する（ロック無しだと「wrapper 無し→物理削除」の判定と同時に
    accept_share が新規 wrapper を INSERT し、その wrapper の source_conversation_id が
    FK SET NULL で即座に壊れる TOCTOU が起きる）。accept_share 側も同じ行を FOR UPDATE で
    ロックするため、どちらが先に行ロックを取っても、そのトランザクションが commit するまで
    もう一方は待たされ、判定がズレない。
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
                # soft delete は messages 行を物理的には残す（受領側が読み続けるため）が、
                # message_feedback は FK CASCADE の対象外（会話ではなく messages にぶら下がる）
                # なので明示的に消す。所有者本人が削除した会話へのフィードバックを残す理由が無い。
                c.execute(
                    "DELETE FROM message_feedback WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=%s)",
                    (conversation_id,))
        else:
            n = c.execute("DELETE FROM conversations WHERE id=%s AND user_id=%s",
                          (conversation_id, user_id)).rowcount
    return n > 0


def set_pinned(conversation_id, pinned: bool, user_id="admin") -> bool:
    """会話のピン止めを設定/解除（#8）。所有者一致のみ。soft-delete 済み（deleted_at 設定済み）は対象外
    （RV MEDIUM: 削除済み会話への操作が 200 を返していたため deleted_at IS NULL を明示）。"""
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE conversations SET pinned=%s WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
            (bool(pinned), conversation_id, user_id)).rowcount
    return n > 0


def rename_conversation(conversation_id, title, user_id="admin") -> bool:
    """会話のタイトルを変更。所有者一致のみ。並び順は変えない（updated_at は触らない）。
    soft-delete 済みは対象外（呼出側 API は owns_conversation で既に弾くが、多層防御として本関数側でも確認）。"""
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
    """会話に紐づく直近の `codex_session_id`（R1b: Codex ネイティブ resume の判定用）。

    行が無い/未設定なら None（chat_service はこれを `Ctx.codex_session_id` に渡すだけで、
    None なら CodexProvider は resume を試みず新規セッションを開始する）。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT codex_session_id FROM conversations WHERE id=%s", (conversation_id,)).fetchone()
        return row["codex_session_id"] if row else None


def owns_conversation(uid, cid) -> bool:
    """current user が書き込み可能な所有会話か（origin='own'）。"""
    _ensure()
    with _connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM conversations WHERE id=%s AND user_id=%s AND origin='own' AND deleted_at IS NULL",
            (cid, uid)).fetchone())


def owns_assistant_message(uid, conversation_id, message_id) -> bool:
    """`message_id` が `conversation_id`（自分の所有会話・origin='own'）に属する assistant メッセージか。
    フィードバック投稿対象の検証専用（他人の会話・受領共有・非 assistant・存在しないメッセージは False）。"""
    _ensure()
    with _connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id=%s AND m.conversation_id=%s AND m.role='assistant' "
            "AND c.user_id=%s AND c.origin='own' AND c.deleted_at IS NULL",
            (message_id, conversation_id, uid)).fetchone())


def is_personal_tainted(message: dict) -> bool:
    """メッセージ1件が個人情報由来か。`messages.personal` 列を優先し、無ければ（列導入前の
    未バックフィル行）`answer` 内の旧マーカー（personal_sources／_personal_facts／
    codex_wrote_files）を見る。`shares.py::create_sanitized_snapshot` の taint 判定と同じ基準
    （共通ヘルパへ集約・両方が個別に判定基準を持つと片方だけ更新されてズレる）。
    """
    if message.get("personal"):
        return True
    answer = message.get("answer")
    if not isinstance(answer, dict):
        return False
    return bool(answer.get("personal_sources") or answer.get("_personal_facts")
               or answer.get("codex_wrote_files"))


def list_export_messages(*, time_from, cursor_id: int | None, limit: int) -> list[dict]:
    """改善ログエクスポート用: `time_from` 以降の assistant メッセージを新しい順（id 降順）で
    ページング取得する（`cursor_id` 指定時はそれより小さい id のみ＝呼び出し側が前ページ最後の
    id を渡してキーセット方式で進める）。

    質問（対応する user メッセージ）は `chat.turn` 監査ログの `message_id_user`/
    `message_id_assistant` で対応付ける（「直前の user 行」を推測しない）。**対応付けられる
    監査行が無いターンは fail-closed でこの一覧から除外する**（`JOIN`＝内部結合。ターン処理が
    例外でクラッシュした場合の復旧経路（`routers/chat.py::_persist_turn_crash`）は監査に
    message_id_user/message_id_assistant を残すが、それでも書き込み自体が失敗した/対応付け不能な
    ターンは「個人情報の有無が確認できない」として出さない——`chat.turn` 監査自体は fail-open
    （`chat_service.py::_audit_chat_turn`）なので、対応付けが無いことは「非個人と確認できた」を
    意味しない）。個人情報の判定に使えるよう、対応する質問側の `personal`/`answer` も同梱する
    （呼び出し側で `is_personal_tainted` を質問・回答の両方に適用する契約）。

    sanitized share の複製（`conversations.origin='sanitized_snapshot'`）は元会話の内容を
    複製したものなので除外する（同じ内容が二重に出る・sanitize 後の伏字状態は改善ログの
    分析対象として不適切）。論理削除済み（`deleted_at IS NOT NULL`）の会話も除外する。
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
    """会話に個人ターン（messages.personal=TRUE）が1件でもあるか（通常共有 guard の多層防御）。
    会話フラグ contains_personal_workspace とズレても（部分失敗/手動修復）漏らさないための保険。"""
    _ensure()
    with _connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM messages WHERE conversation_id=%s AND personal=TRUE LIMIT 1",
            (cid,)).fetchone())


def set_contains_personal_workspace(conversation_id: int) -> None:
    """会話に個人 workspace 参照フラグを立てる（冪等・FALSE→TRUE のみ）。

    個人ファイルを参照/生成した会話を共有不可にするためのフラグ。
    不変条件: このフラグが TRUE の会話は POST /conversations/{cid}/shares が 409 を返す
    （既存ガード・2026-07-01-認証実装計画.md スライス1）。
    """
    _ensure()
    with _connect() as c:
        c.execute(
            "UPDATE conversations SET contains_personal_workspace=TRUE WHERE id=%s",
            (conversation_id,),
        )
