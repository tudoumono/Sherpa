"""外部連携 API キーの台帳（`api_keys`）。プレーンキーは DB に残さず key_hash のみ持つ。認証・監査は `sherpa/ext_api.py`。
`expires_at`/`daily_quota`（NULL＝無期限/無制限）・`owner_uid`（自己発行キーの所有者・NULL＝admin 発行）を持つ。
設計: docs/design/users.md「外部 API の鍵（`/ext/v1/*`）」
"""
from __future__ import annotations

import psycopg

from .db import _connect, _ensure

# `user_api_keys_allowed` の判定と自己発行キーの書込みを直列化する固定 advisory lock key（"KEYU"）。
_USER_KEY_LOCK = 0x4B455955

# 自己発行キーの1日あたり呼び出し上限の既定/上限（管理者設定までのフォールバック）。自己発行キーは空欄（無制限）を選べない。
SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK = 100


def resolve_self_issued_daily_quota_cap(system_settings: dict) -> int:
    """自己発行キーの日次クォータの既定値/上限（管理者設定・未設定はフォールバック定数）。"""
    configured = system_settings.get("user_api_keys_daily_quota_default")
    return int(configured) if configured else SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK


class UserApiKeysDisallowedError(Exception):
    """自己発行キーの書込み直前の再確認で `user_api_keys_allowed` が偽だった。"""


class SelfIssuedQuotaExceededError(Exception):
    """自己発行キーの `daily_quota` が、書込み直前にロック内で再読した現在の上限を超えていた。"""


class ClientOpIdConflictError(Exception):
    """`client_op_id`（非NULL部分一意制約）が既存行と衝突した（呼び出し側は 409 を返す）。"""


def insert_api_key(key_hash: str, key_prefix: str, label: str, created_by: str,
                    allowed_worlds: list | None = None, expires_at=None,
                    daily_quota: int | None = None, owner_uid: str | None = None,
                    client_op_id: str | None = None, webhook_url: str | None = None,
                    webhook_secret: str | None = None) -> dict:
    """発行済みキーのハッシュを台帳登録する。返値 {id, key_prefix, label, created_by, created_at, allowed_worlds, expires_at, daily_quota, owner_uid, client_op_id, webhook_url, webhook_secret}。
    `allowed_worlds`: None＝全 world 許可、空リスト＝どの world にも不可。
    `webhook_url`/`webhook_secret`: 両方 None＝Webhook 無効。宛先検証・secret 生成は呼び出し側の責務で、ここでは保存のみ。
    `expires_at`: None＝無期限。`owner_uid`: None＝admin 発行（`daily_quota` は指定どおり）。
    非 None（自己発行）のときは、同一トランザクション・`_USER_KEY_LOCK` の下で次を再確認する。
      1. `user_api_keys_allowed` が真（偽なら `UserApiKeysDisallowedError`）。
      2. `daily_quota`（未指定なら現在の既定・指定ありなら現在の上限以下。超えれば `SelfIssuedQuotaExceededError`）。
    発行済みキーの `daily_quota` は発行時点の値で固定され、後から変えても遡及しない。
    `client_op_id`: 発行 UI の操作トークン（UUID）。応答が失われた場合の回復（`revoke_unconfirmed_key_by_client_op_id`）の相関 ID。小文字の正準形へ正規化して保存する。
    """
    _ensure()
    if client_op_id:
        client_op_id = client_op_id.lower()
    with _connect() as c:
        if owner_uid is not None:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_USER_KEY_LOCK,))
            row = c.execute(
                "SELECT value FROM system_settings WHERE key='user_api_keys_allowed'").fetchone()
            if not bool(row["value"] if row else False):
                raise UserApiKeysDisallowedError(
                    "利用者による API キー発行は許可されていません（管理者が許可するまで発行できません）")
            cap_row = c.execute(
                "SELECT value FROM system_settings WHERE key='user_api_keys_daily_quota_default'"
            ).fetchone()
            cap = int(cap_row["value"]) if cap_row and cap_row["value"] else                 SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK
            if daily_quota is None:
                daily_quota = cap
            elif daily_quota > cap:
                raise SelfIssuedQuotaExceededError(
                    f"1日あたりの呼び出し上限は{cap}件以下で指定してください（管理者の上限）")
        try:
            return c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, allowed_worlds, "
                "  expires_at, daily_quota, owner_uid, client_op_id, webhook_url, webhook_secret) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id, key_prefix, label, created_by, created_at, allowed_worlds, "
                "  expires_at, daily_quota, owner_uid, client_op_id, webhook_url, webhook_secret",
                (key_hash, key_prefix, label, created_by, allowed_worlds, expires_at,
                 daily_quota, owner_uid, client_op_id, webhook_url, webhook_secret),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            # `key_hash` にも UNIQUE があるため、衝突が client_op_id 由来かを制約名で見分ける。
            if getattr(getattr(e, "diag", None), "constraint_name", None) == \
                    "api_keys_client_op_id_unique":
                raise ClientOpIdConflictError(
                    "この操作は既に処理されています（client_op_id が重複しています）") from e
            raise


def api_key_by_hash(key_hash: str) -> dict | None:
    """X-API-Key 検証用（DB 1回引き）。失効済みも返す（呼び出し側で revoked_at を見て 401）。
    `allowed_worlds`/`expires_at`/`daily_quota`/`owner_uid` も返す。`owner_status` は自己発行キーの所有者の現在の `users.status`（admin 発行キーは NULL）で、呼び出し側が非 active/不在を 401 にする。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT k.id, k.key_hash, k.key_prefix, k.label, k.revoked_at, k.allowed_worlds, "
            "  k.expires_at, k.daily_quota, k.owner_uid, u.status AS owner_status "
            "FROM api_keys k LEFT JOIN users u ON u.uid = k.owner_uid "
            "WHERE k.key_hash=%s",
            (key_hash,),
        ).fetchone()


def list_api_keys(owner_uid: str | None = None) -> list:
    """API キー一覧（admin 用は `owner_uid` 省略＝全件・個人設定用は本人の uid＝自分のキーのみ）。
    `webhook_url` は返すが `webhook_secret` は選択しない（平文 secret を一覧に出さない）。
    """
    _ensure()
    where = "WHERE owner_uid=%s " if owner_uid is not None else ""
    params = (owner_uid,) if owner_uid is not None else ()
    with _connect() as c:
        return c.execute(
            "SELECT id, key_prefix, label, created_by, created_at, revoked_at, revoked_by, "
            "  last_used_at, allowed_worlds, expires_at, daily_quota, owner_uid, client_op_id, "
            "  webhook_url "
            f"FROM api_keys {where}"
            "ORDER BY id DESC",
            params,
        ).fetchall()


def list_webhook_keys_for_world(world: str) -> list:
    """`world` を許可する、Webhook 宛先が登録済みの有効キー一覧。
    有効＝失効しておらず・期限切れでなく・所有ユーザーが active（admin 発行は判定なし）で、`allowed_worlds` が `world` を許可する（`ext_api._enforce_world_scope` と同じ判定）。
    `webhook_secret`（署名生成用）も返す。呼び出し側は送信直後に破棄し、ログ/監査に残さない。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT k.id, k.webhook_url, k.webhook_secret FROM api_keys k "
            "LEFT JOIN users u ON u.uid = k.owner_uid "
            "WHERE k.revoked_at IS NULL AND k.webhook_url IS NOT NULL "
            "  AND (k.expires_at IS NULL OR k.expires_at > now()) "
            "  AND (k.owner_uid IS NULL OR u.status = 'active') "
            "  AND (k.allowed_worlds IS NULL OR %s = ANY(k.allowed_worlds))",
            (world,),
        ).fetchall()


def get_api_key_webhook(key_id: int) -> dict | None:
    """鍵の通知先 `{webhook_url, webhook_secret}`。失効・期限切れ・未登録・所有者が active でない鍵は None。
    Codex ジョブの受付可否判定と送信の各試行直前の宛先解決が使う。`webhook_secret` はログ・応答に出さない。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT k.webhook_url, k.webhook_secret FROM api_keys k "
            "LEFT JOIN users u ON u.uid = k.owner_uid "
            "WHERE k.id=%s AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now()) "
            "  AND (k.owner_uid IS NULL OR u.status = 'active') "
            "  AND k.webhook_url IS NOT NULL AND k.webhook_secret IS NOT NULL",
            (key_id,),
        ).fetchone()


def revoke_api_key(key_id: int, revoked_by: str, *, owner_uid: str | None = None) -> dict | None:
    """失効（冪等）。未知 id は None、既に失効済みなら行をそのまま返す。
    `owner_uid` を指定すると、その uid が所有する行だけを対象にする（他人・admin 発行キーは None）。省略時は所有者を問わない。
    """
    _ensure()
    cond = "id=%s"
    params: list = [key_id]
    if owner_uid is not None:
        cond += " AND owner_uid=%s"
        params.append(owner_uid)
    with _connect() as c:
        return c.execute(
            "UPDATE api_keys SET revoked_at=COALESCE(revoked_at, now()), "
            f"  revoked_by=COALESCE(revoked_by, %s) WHERE {cond} "
            "RETURNING id, key_prefix, label, revoked_at, revoked_by",
            [revoked_by, *params],
        ).fetchone()


def revoke_unconfirmed_key_by_client_op_id(client_op_id: str, revoked_by: str, *,
                                           created_by: str | None = None,
                                           owner_uid: str | None = None) -> dict | None:
    """曖昧な発行結果（POST 応答が失われた）の回復専用。
    認証済みの本人が試みた `client_op_id` に一致する未失効キーだけを、単一の原子的 UPDATE（所有条件も同じ WHERE）で失効する。
    `created_by`（admin 発行・`owner_uid IS NULL` の行のみ）と `owner_uid`（自己発行）は排他で、どちらか一方だけを渡す。一致しなければ None。`client_op_id` は `lower()` で照合する。
    """
    if not client_op_id:
        return None
    assert (created_by is None) != (owner_uid is None), "created_by と owner_uid は排他"
    _ensure()
    cond = "lower(client_op_id)=lower(%s) AND revoked_at IS NULL"
    params: list = [client_op_id]
    if created_by is not None:
        cond += " AND created_by=%s AND owner_uid IS NULL"
        params.append(created_by)
    else:
        cond += " AND owner_uid=%s"
        params.append(owner_uid)
    with _connect() as c:
        return c.execute(
            f"UPDATE api_keys SET revoked_at=now(), revoked_by=%s WHERE {cond} "
            "RETURNING id, revoked_at",
            [revoked_by, *params],
        ).fetchone()


def _revoke_self_issued_api_keys_in_tx(conn, actor: str) -> int:
    """`revoke_self_issued_api_keys` の本体（呼び出し側の接続/トランザクションに載る）。
    `_USER_KEY_LOCK` は呼び出し側が取得済みであること。冪等で、実際に失効した行数だけが `RETURNING` に乗り、0件なら監査行も作らない。
    """
    from sherpa import store as _facade
    rows = conn.execute(
        "UPDATE api_keys SET revoked_at=now(), revoked_by=%s "
        "WHERE owner_uid IS NOT NULL AND revoked_at IS NULL "
        "RETURNING id", (actor,)).fetchall()
    count = len(rows)
    if count:
        _facade._audit_insert(conn, actor, "ext_api.user_keys_purged", "api_key", None,
                      detail={"count": count}, severity="warning")
    return count


def revoke_self_issued_api_keys(actor: str = "system") -> int:
    """`user_api_keys_allowed` が偽へ戻ったとき、利用者発行キー（`owner_uid` が非 NULL）を一括失効する（単独呼び出し用）。
    設定変更と同一トランザクションで行うなら `apply_system_settings_and_revoke_if_disabled` を使う。
    """
    _ensure()
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_USER_KEY_LOCK,))
        return _revoke_self_issued_api_keys_in_tx(c, actor)


def count_self_issued_active_api_keys() -> int:
    """有効な（失効しておらず期限切れでもない）利用者発行キーの件数（`user_api_keys_allowed` を OFF にする前の確認ダイアログ用）。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM api_keys WHERE owner_uid IS NOT NULL AND revoked_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > now())"
        ).fetchone()
    return int(row["n"]) if row else 0


# 呼び出し数の集計クエリの statement_timeout（ms）。
_CALL_COUNT_STATEMENT_TIMEOUT_MS = 3000
# 集計対象の期間（日）。
_CALL_COUNT_WINDOW_DAYS = 30


def count_ext_api_calls_by_key(key_ids, *, days: int = _CALL_COUNT_WINDOW_DAYS, now=None) -> dict:
    """指定した API キー（`key_ids`）の直近 `days` 日分の呼び出し回数（監査台帳から集計）を key_id -> 件数で返す。
    `key_ids` が空/None なら空 dict（全キー集計はしない）。0件のキーは含まれない（呼び出し側で `.get(id, 0)`）。
    `now`（テスト用）: 集計の基準時刻。窓の境界を検証するときは `audit_log.created_at` を書き換えず（ハッシュ対象）、`now` を未来へ注入する。
    """
    if not key_ids:
        return {}
    _ensure()
    actors = [f"ext:{kid}" for kid in key_ids]
    with _connect(options=f"-c statement_timeout={_CALL_COUNT_STATEMENT_TIMEOUT_MS}") as c:
        if now is not None:
            rows = c.execute(
                "SELECT actor_user_id AS actor, COUNT(*) AS n FROM audit_log "
                "WHERE actor_user_id = ANY(%s) AND created_at >= %s - make_interval(days => %s) "
                "GROUP BY actor_user_id",
                (actors, now, days),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT actor_user_id AS actor, COUNT(*) AS n FROM audit_log "
                "WHERE actor_user_id = ANY(%s) AND created_at >= now() - make_interval(days => %s) "
                "GROUP BY actor_user_id",
                (actors, days),
            ).fetchall()
    out: dict = {}
    for r in rows:
        try:
            key_id = int(r["actor"].split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        out[key_id] = int(r["n"])
    return out


def apply_system_settings_and_revoke_if_disabled(uid, updates: dict,
                                                 secret_keys: frozenset | None = None) -> dict:
    """全体設定の部分更新（`settings.set_system_settings` に委譲）を行い、更新後に `user_api_keys_allowed` が実効 OFF になる場合は、設定の適用・利用者発行キーの一括失効・両方の監査を同一トランザクションで行う（`in_txn` フック）。
    `user_api_keys_allowed`/`user_api_keys_daily_quota_default` を含む更新は、値に関わらず `_USER_KEY_LOCK` を取ってから適用する（`insert_api_key` の再確認と同じロックで排他する）。
    """
    from . import settings as _settings_mod

    turning_off = "user_api_keys_allowed" in updates and not updates["user_api_keys_allowed"]
    touches_user_key_settings = ("user_api_keys_allowed" in updates
                                  or "user_api_keys_daily_quota_default" in updates)

    def _hook(conn, hook_uid, _updates):
        if touches_user_key_settings:
            # `insert_api_key` と同じロックを取ってから適用する。
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_USER_KEY_LOCK,))
        if turning_off:
            _revoke_self_issued_api_keys_in_tx(conn, hook_uid)

    return _settings_mod.set_system_settings(uid, updates, secret_keys=secret_keys, in_txn=_hook)


def touch_api_key(key_id: int) -> None:
    """last_used_at を更新する（best-effort・認証成功時に呼ぶ）。"""
    _ensure()
    with _connect() as c:
        c.execute("UPDATE api_keys SET last_used_at=now() WHERE id=%s", (key_id,))
