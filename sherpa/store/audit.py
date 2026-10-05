"""監査ログ（hash-chain による改ざん検知を含む）。
チェーン一式（redaction・insert・verify・hash 計算）は不可分のため 1 モジュールにまとめる。
`_audit_insert(conn, …)` は呼び出し側の接続/トランザクションに載る（advisory xact lock も呼び出し側 tx で取得・解放）。
設計: docs/design/users.md「監査ログ」
"""
from __future__ import annotations

import hashlib
import json
from datetime import timezone

from psycopg.types.json import Json

from .db import _connect, _ensure

# これらのキーは detail/before_state/after_state に平文でも hash でも保存しない。
_REDACT_KEYS = frozenset({
    "password", "password_hash", "new_password", "old_password", "plaintext",
    "token", "token_hash", "session_token", "share_token",
    "openai_api_key", "gemini_api_key", "bedrock_api_key", "api_key", "secret",
})


def _redact(obj):
    """detail/state JSONB に渡す dict から秘密キーを再帰的に除去する（store 層での二重処理）。"""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in _REDACT_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def list_audit(
    actor=None,
    action=None,
    resource_type=None,
    resource_id=None,
    outcome=None,
    severity=None,
    time_from=None,
    time_to=None,
    request_id=None,
    limit=100,
    offset=0,
) -> list:
    """監査ログを絞り込み検索する（admin 閲覧用・時系列降順・SQL はプレースホルダのみ）。"""
    _ensure()
    conds = []
    params = []
    if actor:
        conds.append("actor_user_id = %s"); params.append(actor)
    if action:
        # prefix マッチ（例: auth.* → LIKE 'auth.%'）。
        if action.endswith("*"):
            conds.append("action LIKE %s"); params.append(action[:-1] + "%")
        else:
            conds.append("action = %s"); params.append(action)
    if resource_type:
        conds.append("resource_type = %s"); params.append(resource_type)
    if resource_id:
        conds.append("resource_id = %s"); params.append(resource_id)
    if outcome:
        conds.append("outcome = %s"); params.append(outcome)
    if severity:
        conds.append("severity = %s"); params.append(severity)
    if time_from:
        conds.append("created_at >= %s"); params.append(time_from)
    if time_to:
        conds.append("created_at <= %s"); params.append(time_to)
    if request_id:
        conds.append("request_id = %s"); params.append(request_id)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]
    with _connect() as c:
        return c.execute(
            f"SELECT id, actor_user_id, action, resource_type, resource_id, detail, "
            f"  outcome, reason, severity, request_id, session_id, ip_hash, user_agent, "
            f"  before_state, after_state, created_at "
            f"FROM audit_log {where} "
            f"ORDER BY created_at DESC, id DESC "
            f"LIMIT %s OFFSET %s",
            params,
        ).fetchall()


def get_messages_by_ids(ids: list) -> dict:
    """id → {content, personal, conv_deleted} の辞書を返す（監査エクスポートの本文 join 用・id は一括で `= ANY(%s)`）。
    存在しない id は含まれない。会話が soft delete 済みの行は `conv_deleted` フラグで返す（行は落とさない）。
    """
    if not ids:
        return {}
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT m.id, m.content, m.personal, (c.deleted_at IS NOT NULL) AS conv_deleted "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id = ANY(%s)", (list(ids),)
        ).fetchall()
    return {r["id"]: r for r in rows}


def _audit_insert(
    conn,
    actor,
    action,
    resource_type,
    resource_id=None,
    detail=None,
    *,
    outcome="success",
    reason=None,
    severity="info",
    request_id=None,
    session_id=None,
    ip_hash=None,
    user_agent=None,
    before_state=None,
    after_state=None,
) -> None:
    """監査ログ1行の INSERT 本体（`conn`＝呼び出し側が開いた接続/トランザクションに載せる）。
    設定変更など先行の更新と同一トランザクションで監査したい呼び出し側は、これを自分の `with _connect() as c:` 内で直接呼ぶ。
    detail/before_state/after_state は redaction を通す。entry_hash = SHA256(prev_hash || canonical_json(row)) の hash-chain で、並列 insert は advisory xact lock で直列化する。
    """
    _rid = str(resource_id) if resource_id is not None else None
    _detail = _redact(detail) if detail is not None else None
    _before = _redact(before_state) if before_state is not None else None
    _after = _redact(after_state) if after_state is not None else None
    _ua = (user_agent or "")[:512] if user_agent else None
    # hash-chain の順序を直列化する（commit/rollback で自動解放）。
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_CHAIN_LOCK,))
    # prev_hash は head アンカーから取る。
    head = conn.execute("SELECT last_hash, cnt FROM audit_chain_head WHERE singleton").fetchone()
    prev_hash = head["last_hash"] if head else None
    cnt = head["cnt"] if head else 0
    # ハッシュは DB 格納後の値で計算するため、INSERT ... RETURNING で確定値を受け取ってから UPDATE する。
    row = conn.execute(
        "INSERT INTO audit_log (actor_user_id, action, resource_type, resource_id, detail, "
        "  outcome, reason, severity, request_id, session_id, ip_hash, user_agent, "
        "  before_state, after_state, prev_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING id, actor_user_id, action, resource_type, resource_id, detail, outcome, "
        "  reason, severity, request_id, session_id, ip_hash, user_agent, before_state, "
        "  after_state, created_at",
        (
            actor, action, resource_type, _rid,
            Json(_detail) if _detail is not None else None,
            outcome, reason, severity, request_id, session_id, ip_hash, _ua,
            Json(_before) if _before is not None else None,
            Json(_after) if _after is not None else None,
            prev_hash,
        ),
    ).fetchone()
    entry_hash = _audit_entry_hash(prev_hash, {k: row[k] for k in _AUDIT_CANON_FIELDS})
    conn.execute("UPDATE audit_log SET entry_hash=%s WHERE id=%s", (entry_hash, row["id"]))
    conn.execute(
        "INSERT INTO audit_chain_head (singleton, last_id, last_hash, cnt, chain_start_id) "
        "VALUES (TRUE,%s,%s,%s,%s) "
        "ON CONFLICT (singleton) DO UPDATE SET "
        "  last_id=EXCLUDED.last_id, last_hash=EXCLUDED.last_hash, cnt=EXCLUDED.cnt, "
        "  chain_start_id=COALESCE(audit_chain_head.chain_start_id, EXCLUDED.chain_start_id)",
        (row["id"], entry_hash, cnt + 1, row["id"]))


def audit(
    actor,
    action,
    resource_type,
    resource_id=None,
    detail=None,
    *,
    outcome="success",
    reason=None,
    severity="info",
    request_id=None,
    session_id=None,
    ip_hash=None,
    user_agent=None,
    before_state=None,
    after_state=None,
) -> None:
    """監査ログを1行 insert する。自前接続で `_audit_insert` を呼ぶ薄いラッパー。
    `_audit_insert` は facade（`sherpa.store`）属性経由で実行時に解決する（関数内 import）。
    """
    _ensure()
    from sherpa import store as _facade
    with _connect() as c:
        _facade._audit_insert(c, actor, action, resource_type, resource_id, detail,
                      outcome=outcome, reason=reason, severity=severity, request_id=request_id,
                      session_id=session_id, ip_hash=ip_hash, user_agent=user_agent,
                      before_state=before_state, after_state=after_state)


_AUDIT_CHAIN_LOCK = 0x53485241  # audit insert を直列化する固定 advisory lock key（"SHRA"）。
# hash 対象の論理フィールド（insert と verify で完全一致させる）。
_AUDIT_CANON_FIELDS = (
    "actor_user_id", "action", "resource_type", "resource_id", "detail", "outcome",
    "reason", "severity", "request_id", "session_id", "ip_hash", "user_agent",
    "before_state", "after_state", "created_at",
)


def _audit_canonical(vals: dict) -> str:
    """行の論理値を決定的 JSON（sorted keys・空白なし）にする。created_at は UTC ISO に正規化。"""
    d = {}
    for k in _AUDIT_CANON_FIELDS:
        v = vals.get(k)
        if k == "created_at" and v is not None:
            v = v.astimezone(timezone.utc).isoformat()
        d[k] = v
    return json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _audit_entry_hash(prev_hash, vals: dict) -> str:
    return hashlib.sha256(((prev_hash or "") + _audit_canonical(vals)).encode("utf-8")).hexdigest()


def verify_audit_chain() -> dict:
    """audit_log の hash-chain を検証し、改ざん・欠落・並べ替え・末尾削除を検出する。
    legacy 行（entry_hash IS NULL）はスキップし、末尾は head アンカーと照合する。
    returns {"ok": bool, "checked": int, "broken_at": id|None, "reason": str|None}
    """
    _ensure()
    with _connect() as c:
        # audit() と同じ advisory lock を取ってから head/rows を1トランザクションで読む。
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_CHAIN_LOCK,))
        head = c.execute(
            "SELECT last_id, last_hash, cnt, chain_start_id FROM audit_chain_head WHERE singleton").fetchone()
        rows = c.execute(
            "SELECT id, actor_user_id, action, resource_type, resource_id, detail, outcome, "
            "  reason, severity, request_id, session_id, ip_hash, user_agent, before_state, "
            "  after_state, created_at, prev_hash, entry_hash FROM audit_log "
            "WHERE entry_hash IS NOT NULL ORDER BY id ASC"
        ).fetchall()
        # chain 開始後の総行数。hashed 行数と一致しなければ NULL-hash 偽行の注入。
        total_after_start = None
        if head and head["chain_start_id"] is not None:
            total_after_start = c.execute(
                "SELECT count(*) AS n FROM audit_log WHERE id >= %s", (head["chain_start_id"],)).fetchone()["n"]

    def broken(bid, reason):
        return {"ok": False, "checked": checked, "broken_at": bid, "reason": reason}

    prev_hash = None
    checked = 0
    last_id = last_hash = None
    for r in rows:
        if r["prev_hash"] != prev_hash:  # 直前行の entry_hash と繋がっているか。
            return broken(r["id"], "prev_hash_mismatch")
        expect = _audit_entry_hash(r["prev_hash"], {k: r[k] for k in _AUDIT_CANON_FIELDS})
        if r["entry_hash"] != expect:  # 行内容の改ざん検出。
            return broken(r["id"], "entry_hash_mismatch")
        prev_hash = r["entry_hash"]
        last_id, last_hash = r["id"], r["entry_hash"]
        checked += 1
    # head アンカー照合。
    if checked > 0 and not head:
        return broken(last_id, "missing_head")  # hashed 行があるのに anchor が無い。
    if head:
        if checked != head["cnt"]:
            return broken(head["last_id"], "count_mismatch")
        if head["cnt"] == 0:  # cnt=0 は hashed 行ゼロ＋anchor 全 NULL でなければ改ざん。
            if (checked != 0 or head["last_id"] is not None or head["last_hash"] is not None
                    or head["chain_start_id"] is not None):
                return broken(head["last_id"], "head_mismatch")
        else:
            if head["chain_start_id"] is None:  # cnt>0 なのに chain_start 未設定。
                return broken(head["last_id"], "missing_chain_start")
            if last_id != head["last_id"] or last_hash != head["last_hash"]:
                return broken(head["last_id"], "head_mismatch")
            if rows and rows[0]["id"] != head["chain_start_id"]:  # 先頭 hashed 行が chain_start と一致するか。
                return broken(rows[0]["id"], "chain_start_mismatch")
            if total_after_start is not None and total_after_start != head["cnt"]:
                return broken(head["chain_start_id"], "null_row_injected")  # chain 開始後の NULL 偽行。
    return {"ok": True, "checked": checked, "broken_at": None, "reason": None}
