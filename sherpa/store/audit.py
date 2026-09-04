"""監査ログ（2026-07-01-監査ログ強化.md §3・§4 MVP・hash-chain §Phase2）。

フェーズ4 S6（2026-07-02-リファクタリング計画.md）: `sherpa/store/__init__.py` から純移動。
チェーン一式（redaction・insert・verify・hash 計算）は不可分のため1モジュールにまとめる
（insert と verify が `_AUDIT_CHAIN_LOCK`・`_AUDIT_CANON_FIELDS`・`_audit_canonical`・
`_audit_entry_hash` を共有する）。

不変条件（変更禁止）: `_audit_insert(conn, …)` は**呼び出し側の接続/トランザクションを受ける**契約
（`pg_advisory_xact_lock` は呼び出し側 tx で取得・解放）。同一トランザクションで先行の更新と
一緒に監査を記録したい呼び出し側（例: `settings.set_system_settings`）はこの関数を直接、
自分の `with _connect() as c:` 内で呼ぶ。`audit()` は自前接続で呼ぶ薄いラッパー
（`_audit_insert` は facade 属性経由で実行時解決＝monkeypatch シーム維持・`audit()` の
docstring 参照）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import timezone

from psycopg.types.json import Json

from .db import _connect, _ensure

# ---- 監査ログ（2026-07-01-監査ログ強化.md §3・§4 MVP）----

# redaction: これらのキーは detail/before_state/after_state に平文・hash いずれも保存しない。
# bedrock_api_key は 2026-07-13-横断レビュー対応.md R2b で追加（保護境界の補修・欠落していた）。
_REDACT_KEYS = frozenset({
    "password", "password_hash", "new_password", "old_password", "plaintext",
    "token", "token_hash", "session_token", "share_token",
    "openai_api_key", "gemini_api_key", "bedrock_api_key", "api_key", "secret",
})


def _redact(obj):
    """detail/state JSONB に渡す dict から秘密キーを再帰的に除去（呼出側の redaction に加えて store で二重処理）。"""
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
    """監査ログを絞り込み検索（admin 閲覧用・時系列降順）。
    SQL はプレースホルダのみ使用（SQL injection なし）。
    """
    _ensure()
    conds = []
    params = []
    if actor:
        conds.append("actor_user_id = %s"); params.append(actor)
    if action:
        # prefix マッチ（例: auth.* → LIKE 'auth.%'）をサポート。
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
    """id → {content, personal, conv_deleted} の辞書を返す（S5・監査エクスポートの本文 join 専用）。

    N+1 を避けるため呼び出し側で id を一括収集し、1回の `= ANY(%s)` で取得する。
    存在しない id（会話ごと物理削除・メッセージも FK CASCADE で消滅）は結果に含まれない＝
    呼出側で「（削除済み）」に落とす。

    RV HIGH（2026-07-03）: 受領共有ラッパーが生きている会話の `delete_conversation` は
    **soft delete**（`conversations.deleted_at` のみ・messages 行は物理的に残る＝受領側が
    引き続き読めるようにするため）に留まる。そのため「存在しない id」だけでは削除済みを
    判定できず、本文が export に漏れる。conversations を join し `deleted_at IS NOT NULL` を
    `conv_deleted` として返す（**行を返さないのではなくフラグで返す**＝呼出側でプレースホルダ化・
    物理削除の「存在しない id」経路と挙動を統一する）。
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
    """監査ログ1行の INSERT 本体（`conn`＝呼び出し側が開いた接続/トランザクションに載せる内部ヘルパー）。

    `audit()` はこれを自前接続で呼ぶ薄いラッパー（既存呼び出し互換）。**同一トランザクションで
    先行の更新と一緒に監査を記録したい呼び出し側**（例: `set_system_settings`）はこの関数を直接、
    自分の `with _connect() as c:` 内で呼ぶ（2026-07-08 RV High 対応: commit 後の別接続 audit＋
    失敗時 compensate 方式は (a) commit〜補償の間に未監査値が見える (b) その間のプロセス停止で
    未監査変更が残留 (c) 並行更新を補償が上書き、の3穴を抱えていた。同一トランザクション化で
    「設定変更と監査は両方成功か両方失敗」の原子性に置き換え、穴を構造的に閉じる）。
    detail/before_state/after_state は redaction を通す（呼出側＋store 層の二重）。
    §Phase2: hash-chain（entry_hash = SHA256(prev_hash || canonical_json(row)))で改ざん検知。
    並列 insert は advisory (xact) lock で順序を直列化する（lock は conn の commit/rollback で解放）。
    """
    _rid = str(resource_id) if resource_id is not None else None
    _detail = _redact(detail) if detail is not None else None
    _before = _redact(before_state) if before_state is not None else None
    _after = _redact(after_state) if after_state is not None else None
    _ua = (user_agent or "")[:512] if user_agent else None
    # hash-chain の順序を直列化（xact lock は commit/rollback で自動解放）。
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_CHAIN_LOCK,))
    # prev_hash は head アンカーから取る（生の末尾行でなく＝NULL 尾行で新セグメントが始まる問題を回避・RV MEDIUM）。
    head = conn.execute("SELECT last_hash, cnt FROM audit_chain_head WHERE singleton").fetchone()
    prev_hash = head["last_hash"] if head else None
    cnt = head["cnt"] if head else 0
    # RV HIGH: ハッシュは **DB 格納後の値**（created_at=DB now()・JSONB round-trip）で計算するため、
    #   INSERT ... RETURNING で確定値を受け取り、その値でハッシュして UPDATE する（verify との一致を保証）。
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
    """監査ログを1行 insert（2026-07-01-監査ログ強化.md §4 推奨 helper）。自前接続で `_audit_insert` を呼ぶ薄いラッパー
    （既存呼び出し互換）。同一トランザクションで先行の変更と一緒に監査したい場合は `_audit_insert` を
    直接呼ぶこと（`set_system_settings` 参照）。

    RV HIGH（Codex 2026-07-13 フェーズ4）: `_audit_insert` は同一モジュール内の直接束縛では**なく**
    facade（`sherpa.store` パッケージ）属性経由で実行時解決する。旧 monolith では
    `monkeypatch.setattr(store, "_audit_insert", …)` が本関数経由の insert にも効いた（モジュール
    グローバル＝patch 先が同一名前空間）が、分割後にローカル束縛のまま呼ぶとテストの patch が
    素通りして実 DB に書き込んでしまう。`settings.set_system_settings` と同じ方式
    （settings.py の docstring 参照・関数内 import は初期化循環回避）。
    """
    _ensure()
    from sherpa import store as _facade   # 上記 docstring 参照: 実行時解決（monkeypatch シーム維持）
    with _connect() as c:
        _facade._audit_insert(c, actor, action, resource_type, resource_id, detail,
                      outcome=outcome, reason=reason, severity=severity, request_id=request_id,
                      session_id=session_id, ip_hash=ip_hash, user_agent=user_agent,
                      before_state=before_state, after_state=after_state)


# ==== 監査ログ hash-chain（2026-07-01-監査ログ強化.md §Phase2・改ざん検知）====
_AUDIT_CHAIN_LOCK = 0x53485241            # 固定 advisory lock key（"SHRA"）＝audit insert の直列化
# hash 対象の論理フィールド（順序・集合とも insert/verify で完全一致させる）。
_AUDIT_CANON_FIELDS = (
    "actor_user_id", "action", "resource_type", "resource_id", "detail", "outcome",
    "reason", "severity", "request_id", "session_id", "ip_hash", "user_agent",
    "before_state", "after_state", "created_at",
)


def _audit_canonical(vals: dict) -> str:
    """行の論理値を決定的 JSON（sorted keys・空白なし）に。created_at は UTC ISO に正規化。
    JSONB は往復で論理値を保つので、insert 時（Python dict）と verify 時（DB からの dict）で一致する。"""
    d = {}
    for k in _AUDIT_CANON_FIELDS:
        v = vals.get(k)
        if k == "created_at" and v is not None:
            v = v.astimezone(timezone.utc).isoformat()   # tz を UTC ISO に固定（session tz 非依存）
        d[k] = v
    return json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _audit_entry_hash(prev_hash, vals: dict) -> str:
    return hashlib.sha256(((prev_hash or "") + _audit_canonical(vals)).encode("utf-8")).hexdigest()


def verify_audit_chain() -> dict:
    """audit_log の hash-chain を検証。改ざん・欠落・並べ替え・**末尾削除(truncation)** を検出する。
    移行前の legacy 行（entry_hash IS NULL）は chain 対象外＝スキップ。末尾は head アンカーと照合する。
    ※head アンカー自体も同一 DB 内なので、DB superuser への完全防御には外部への head 署名/エクスポートが別途必要
      （2026-07-01-監査ログ強化.md §6・follow-up）。
    returns {"ok": bool, "checked": int, "broken_at": id|None, "reason": str|None}
    """
    _ensure()
    with _connect() as c:
        # RV HIGH: audit() と同じ advisory lock を取ってから head/rows を1トランザクションで読む
        #   （head 読取と rows 読取の間に concurrent audit() が commit して count_mismatch 誤検知するのを防ぐ）。
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_CHAIN_LOCK,))
        head = c.execute(
            "SELECT last_id, last_hash, cnt, chain_start_id FROM audit_chain_head WHERE singleton").fetchone()
        rows = c.execute(
            "SELECT id, actor_user_id, action, resource_type, resource_id, detail, outcome, "
            "  reason, severity, request_id, session_id, ip_hash, user_agent, before_state, "
            "  after_state, created_at, prev_hash, entry_hash FROM audit_log "
            "WHERE entry_hash IS NOT NULL ORDER BY id ASC"
        ).fetchall()
        # RV HIGH: chain 開始後（id>=chain_start_id）の総行数。hashed 行数(cnt)と一致しなければ
        #   NULL-hash 偽行が chain の後ろに注入されている（legacy skip は移行前行だけに限定）。
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
        if r["prev_hash"] != prev_hash:                  # 直前行の entry_hash と繋がっているか（欠落/並べ替え検出）
            return broken(r["id"], "prev_hash_mismatch")
        expect = _audit_entry_hash(r["prev_hash"], {k: r[k] for k in _AUDIT_CANON_FIELDS})
        if r["entry_hash"] != expect:                    # 行内容の改ざん検出
            return broken(r["id"], "entry_hash_mismatch")
        prev_hash = r["entry_hash"]
        last_id, last_hash = r["id"], r["entry_hash"]
        checked += 1
    # head アンカー照合（RV HIGH: 末尾削除／head 欠落・cnt=0 バイパス／NULL 注入 を検出）。
    if checked > 0 and not head:
        return broken(last_id, "missing_head")           # hashed 行があるのに anchor が無い＝anchor 削除
    if head:
        if checked != head["cnt"]:
            return broken(head["last_id"], "count_mismatch")
        if head["cnt"] == 0:                             # cnt=0 は「hashed 行ゼロ＋anchor 全 NULL」でなければ改ざん
            if (checked != 0 or head["last_id"] is not None or head["last_hash"] is not None
                    or head["chain_start_id"] is not None):
                return broken(head["last_id"], "head_mismatch")
        else:
            if head["chain_start_id"] is None:          # cnt>0 なのに chain_start 未設定＝anchor 改ざん/未 backfill
                return broken(head["last_id"], "missing_chain_start")
            if last_id != head["last_id"] or last_hash != head["last_hash"]:
                return broken(head["last_id"], "head_mismatch")
            if rows and rows[0]["id"] != head["chain_start_id"]:   # 先頭 hashed 行が chain_start と一致
                return broken(rows[0]["id"], "chain_start_mismatch")
            if total_after_start is not None and total_after_start != head["cnt"]:
                return broken(head["chain_start_id"], "null_row_injected")   # chain 開始後に NULL 偽行
    return {"ok": True, "checked": checked, "broken_at": None, "reason": None}
