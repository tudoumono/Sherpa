"""外部連携 API キー（docs/proposals/2026-07-07-外部API化とDify.md E1）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S2）。不変条件: プレーンキーは DB に残さない
（key_hash のみ）。認証・監査は sherpa/ext_api.py が行う。

`expires_at`/`daily_quota`（オプトイン・NULL=既存キーと同じ後方互換の無期限/無制限）・
`owner_uid`（利用者による自己発行キーの所有者 uid・NULL=admin 発行の従来キー）を持つ。
"""
from __future__ import annotations

import psycopg

from .db import _connect, _ensure

# system_settings.user_api_keys_allowed の判定と、自己発行キーの書込みを直列化する固定
# advisory lock key（"KEYU"）。`_PERSONAL_KEY_LOCK` と同型の競合対策: 「事前チェック後に
# admin が無効化した」競合窓を、書込み直前の同一トランザクション再確認で閉じる。
_USER_KEY_LOCK = 0x4B455955

# 利用者自己発行キーの1日あたり呼び出し上限の既定/上限（管理者が system_settings で
# 上書きするまでのフォールバック）。自己発行キーは常にこれ以下のクォータを持つ＝空欄で
# 無制限を選べない（admin 発行キーはこの上限の対象外・引き続き空欄=無制限を選べる）。
SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK = 100


def resolve_self_issued_daily_quota_cap(system_settings: dict) -> int:
    """自己発行キーの日次クォータの既定値／上限（管理者設定・未設定はフォールバック定数）。"""
    configured = system_settings.get("user_api_keys_daily_quota_default")
    return int(configured) if configured else SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK


class UserApiKeysDisallowedError(Exception):
    """自己発行キーの書込み直前に再確認した結果、`system_settings.user_api_keys_allowed` が
    偽だった（事前チェックの後に admin が無効化した競合・A6 の `PersonalKeysDisallowedError` と同型）。"""


class SelfIssuedQuotaExceededError(Exception):
    """自己発行キーの `daily_quota` 指定が、書込み直前にロック内で再読した現在の上限を超えていた
    （TOCTOU 対策: 利用者が上限を読んでから admin が引き下げた競合を、書込み直前の同一トランザクション
    再確認で閉じる・`UserApiKeysDisallowedError` と同型）。"""


class ClientOpIdConflictError(Exception):
    """`client_op_id`（非NULL部分一意制約・`api_keys_client_op_id_unique`）が既存行と衝突した
    （同じ操作トークンで2回目の発行を試みた・呼び出し側は409を返すこと）。"""


def insert_api_key(key_hash: str, key_prefix: str, label: str, created_by: str,
                    allowed_worlds: list | None = None, expires_at=None,
                    daily_quota: int | None = None, owner_uid: str | None = None,
                    client_op_id: str | None = None, webhook_url: str | None = None,
                    webhook_secret: str | None = None) -> dict:
    """発行済みキーのハッシュを台帳登録。返値 {id, key_prefix, label, created_by, created_at,
    allowed_worlds, expires_at, daily_quota, owner_uid, client_op_id, webhook_url, webhook_secret}。

    `allowed_worlds`（world スコープ・オプトイン）: None＝全 world 許可（既定・既存キーと同じ
    後方互換の挙動）。空リストは「どの world にもアクセスできない」キーになる（呼び出し側の
    意図的な選択・拒否はしない）。

    `webhook_url`/`webhook_secret`（PART-6・オプトイン）: 両方 None＝Webhook 無効（既定）。
    宛先検証（`sherpa.webhooks.assert_webhook_url_allowed`）・secret 生成は呼び出し側
    （`routers/system_extras.py`）の責務——ここではそのまま保存するだけ。

    `expires_at`（有効期限・オプトイン）: None＝無期限（既存キーと同じ後方互換）。

    `owner_uid`（利用者による自己発行キーの所有者 uid）: None＝admin 発行（従来どおり
    「誰でも使える」システムキー・`daily_quota` は呼び出し側の指定をそのまま使う＝None なら無制限）。
    非 None（自己発行）のときは、同一トランザクション・`_USER_KEY_LOCK` の下で以下を**両方**
    再確認する（事前チェック（router 側）はあくまで早期リターン用の best-effort・ここが唯一の
    正本）:
      1. `system_settings.user_api_keys_allowed` が真であること（偽なら `UserApiKeysDisallowedError`）。
      2. `daily_quota`（未指定なら管理者の現在の既定を適用・指定ありなら現在の上限を超えないこと・
         超えていれば `SelfIssuedQuotaExceededError`）。既存キーの `daily_quota` は発行時点の値の
         まま固定される（**非遡及**——admin が既定/上限を後から変えても、発行済みキーの実際の
         クォータは変わらない。認証時（`ext_api._verify_key_sync`）は行に保存された値だけを見る）。

    `client_op_id`（オプトイン）: 発行 UI が生成するクライアント側の操作トークン（UUID）。POST
    応答がタイムアウト/通信断/不正な形で失われた場合に、専用の回復エンドポイント
    （`revoke_unconfirmed_key_by_client_op_id`）がこの値で照合して自動失効できるようにするための
    相関 ID（秘密ではない・機能的な用途のみ）。大小文字表記の違いは同一の UUID を指すため、
    保存前に標準の小文字正準形へ正規化する（router 側の `_validate_client_op_id_format` が
    通常は既に正規化済みだが、store 層を直接呼ぶ経路（テスト等）向けの独立した最後の砦）。
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
            # `key_hash` にも UNIQUE があるため、衝突が client_op_id 由来かを制約名で見分ける
            # （key_hash 衝突＝暗号学的ハッシュの偶発衝突は実質起こらないが、誤って
            # ClientOpIdConflictError にすり替えないよう制約名を確認する）。
            if getattr(getattr(e, "diag", None), "constraint_name", None) == \
                    "api_keys_client_op_id_unique":
                raise ClientOpIdConflictError(
                    "この操作は既に処理されています（client_op_id が重複しています）") from e
            raise


def api_key_by_hash(key_hash: str) -> dict | None:
    """X-API-Key 検証用（DB 1回引き）。失効済みも返す（呼び出し側で revoked_at を見て 401）。

    `allowed_worlds`／`expires_at`／`daily_quota`／`owner_uid` も返す
    （None＝各々 全world許可／無期限／無制限／admin発行）。

    `owner_status`: `owner_uid` が非 NULL（利用者自己発行キー）のとき、所有者の現在の
    `users.status` を同じ引きで返す（`owner_uid` が NULL の admin 発行キーは常に NULL）。
    Cookie セッションは毎回 `users.status='active'` を確認する（`session_user` 参照）ため、
    自己発行キーもこれに揃える——所有者を無効化してもキー自体は失効しないままだと、
    アカウント停止をキー経由で迂回できてしまう（呼び出し側で非 active/不在を 401 にする）。
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
    """API キー一覧（admin 用は `owner_uid` 省略＝全件・個人設定用は本人の uid を渡す＝自分のキーのみ）。

    `webhook_url` も返す（一覧は host:port の表示までは可・呼び出し側が `webhook_url` から
    導く・`webhook_secret` はここでは選択しない＝一覧に平文 secret を絶対に出さない契約）。
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
    """`world` を許可する、Webhook 宛先が登録済みの有効キー一覧（PART-6・RV是正#2）。

    対象: 失効しておらず（`revoked_at IS NULL`）・期限切れでなく（`expires_at IS NULL OR
    expires_at > now()`・`_verify_key_sync` と同じ判定規則）・所有ユーザーが有効
    （`owner_uid IS NULL`＝admin 発行キーは対象外の判定なし・非 NULL は `users.status='active'`
    ——`api_key_by_hash` の `owner_status` 判定と同型）・`webhook_url` を持ち、`allowed_worlds` が
    `world` を許可する（None＝全 world 許可、または `world` を含む）キー全部——
    `ext_api._enforce_world_scope`（`allowed is None or world not in allowed` の否定）と
    同じ判定規則を SQL 側で再現する。`webhook_secret`（署名生成に必須・平文）も返す——
    呼び出し側（`sherpa.webhooks`）は送信直後に破棄し、ログ/監査には残さない。
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


def revoke_api_key(key_id: int, revoked_by: str, *, owner_uid: str | None = None) -> dict | None:
    """失効（冪等）。未知 id は None。既に失効済みなら行をそのまま返す（revoked_at は変えない）。

    `owner_uid` を指定すると、その uid が所有する行だけを対象にする（利用者が自分のキーだけを
    失効できるようにするための絞り込み・他人/admin発行キーは対象外＝None を返す＝呼出側で404）。
    省略時（admin 発行/失効の既存フロー）は所有者を問わず任意のキーを失効できる。
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
    """曖昧な発行結果（POST 応答がタイムアウト/通信断/不正な形で失われた）の回復専用。

    認証済みの本人が発行操作を試みた `client_op_id` に一致する**未失効**キーだけを、単一の
    原子的 UPDATE で照合・失効する。一覧を取得してから別リクエストで DELETE する2段構成
    （旧設計）は (a) 一覧に本人以外の行も混じりうる (b) 取得と失効の間に別の変更が起こりうる、
    という2つの隙があった——ここでは `client_op_id` と所有条件を**同一 SQL の WHERE 句**で
    照合するため、`client_op_id` が（万一）他人の値と衝突していても他人のキーには触れない。

    `created_by`（admin 発行の回復・`owner_uid IS NULL` の行のみ対象）と `owner_uid`
    （自己発行の回復）は排他——呼び出し側はどちらか一方だけを渡す。一致しなければ None
    （POST がサーバーに届かなかった、またはまだコミットされていない可能性——呼び出し側で
    有界に再試行すること）。`client_op_id` の照合は `lower()` で行う（大小文字表記の違いで
    一致し損ねない・DB 側の一意インデックスと同じ規則）。
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
    """`revoke_self_issued_api_keys` の本体（呼び出し側が開いた接続/トランザクションに載せる）。

    `_USER_KEY_LOCK` は呼び出し側が既に取得している前提（`revoke_self_issued_api_keys` は自分で
    取る・`apply_system_settings_and_revoke_if_disabled` は設定変更と共通のトランザクションで
    先に取る）。冪等: 既に失効済みの行は WHERE 句で対象外＝実際に失効した行数だけが
    `RETURNING` に乗る。変更が無い（0件）ときは監査行も作らない。
    """
    from sherpa import store as _facade   # set_system_settings と同じ理由（monkeypatch シーム維持）
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
    """`user_api_keys_allowed` が偽へ戻ったとき、利用者発行キー（`owner_uid` が非 NULL）を
    一括失効する（`purge_personal_api_keys` と同型）。単独呼び出し用（起動時の backstop 等）。
    設定変更と同一トランザクションで行いたい場合は `apply_system_settings_and_revoke_if_disabled`
    を使うこと（設定 commit 後に失効だけ失敗すると、再度 ON にした時に失効し損ねた旧キーが
    有効なまま復活してしまうため）。
    """
    _ensure()
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_USER_KEY_LOCK,))
        return _revoke_self_issued_api_keys_in_tx(c, actor)


def count_self_issued_active_api_keys() -> int:
    """有効な（失効しておらず、期限切れでもない）利用者発行キーの件数。管理画面が
    `user_api_keys_allowed` を OFF で保存する前に、失効対象件数を確認ダイアログへ表示する
    ためのプレビュー用（`count_users_with_personal_keys` と同型）。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM api_keys WHERE owner_uid IS NOT NULL AND revoked_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > now())"
        ).fetchone()
    return int(row["n"]) if row else 0


# 呼び出し数の集計クエリに掛ける statement_timeout（ms）。監査台帳が肥大しても一覧表示全体を
# 無期限にブロックしないための上限（`ext_api._audit_db_connect` の考え方と同じ）。
_CALL_COUNT_STATEMENT_TIMEOUT_MS = 3000
# 集計対象の期間（日）。「累計」ではなく直近の呼び出し傾向を見せれば十分という判断
# （無期限の集計は監査行が増えるほど遅くなる・書込み時カウンタ表への移行は将来課題）。
_CALL_COUNT_WINDOW_DAYS = 30


def count_ext_api_calls_by_key(key_ids, *, days: int = _CALL_COUNT_WINDOW_DAYS, now=None) -> dict:
    """指定した API キー（`key_ids`）の直近 `days` 日分の呼び出し回数（監査台帳から集計）。
    key_id -> 件数。`key_ids` が空/None なら空 dict を返す（全キー無制限集計はしない——
    呼び出し側は「今から一覧に出す行の id」だけを渡すこと。本人一覧は本人のキーだけを渡すため
    自然に本人キーのみの集計になる）。

    `actor_user_id` は X-API-Key 認証ルート（`ext_api.require_api_key`）が常に `f"ext:{key_id}"`
    の形で書く（成功・401・429 のいずれも）——admin のキー発行/一覧/失効操作は admin 本人の uid が
    actor になるため混入しない。0件のキーはこの辞書に含まれない（呼び出し側で `.get(id, 0)` する）。

    `now`（省略可・テスト専用の注入口）: 集計の基準時刻（tz-aware datetime）。省略時は DB の
    `now()`（実時刻）を使う。窓の境界（例:「31日前の呼び出しは除外される」）を検証するテストは、
    監査行の `created_at` を直接 UPDATE してはならない（`audit_log` はハッシュチェーンで完全性を
    保証しており、`created_at` はハッシュ算出対象のフィールド——直接書き換えると
    `entry_hash` と実際の値が食い違い、チェーンの完全性検証が壊れる）。代わりにここで基準時刻を
    未来へ注入し、実際の（不変の）`created_at` を窓の外へ押し出すことで境界を再現する。
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
    """全体設定の部分更新（`settings.set_system_settings` に委譲）を行いつつ、更新後に
    `user_api_keys_allowed` が実効 OFF（明示 false、または明示 null＝既定 false へ戻る）になる
    場合は、設定の適用・利用者発行キーの一括失効・両方の監査を**同一トランザクション**で行う
    （`set_system_settings` の `in_txn` フックに載せる）。

    設定 commit 後に別トランザクションで失効すると、その失効が失敗した場合に「OFF なのに
    revoked_at が空の旧キーが残る」状態になり、再度 ON にした瞬間その旧キーが復活してしまう
    （認証時の fail-safe 判定は OFF の間だけ効くため、ON に戻ると素通りする）。同一トランザクション
    にすることで、設定変更と失効は必ず両方成功するか両方ロールバックするかのどちらかになる。

    `user_api_keys_allowed`／`user_api_keys_daily_quota_default` のいずれかを含む更新は、
    値に関わらず（ON/OFF/クォータ変更のみ、いずれも）`_USER_KEY_LOCK` を取ってから適用する。
    これは複数ロックの取得順序の話ではなく（単一ロックのため「順序」は生じない）、
    `insert_api_key` の自己発行 TOCTOU 再確認（同じ2キーを同じロック下で読む）と**同じロックを
    共有して排他する**ことが目的——「admin がこの2キーのどちらかを書いている最中」と「利用者が
    自己発行で読んでいる最中」が同じロックドメインで排他されることを構造的に保証し、
    片方だけロックを取る経路が残って稀に交差読み取りが起こる余地を無くす（`turning_off` の
    時だけ一括失効を追加で行う点は従来どおり）。
    """
    from . import settings as _settings_mod

    turning_off = "user_api_keys_allowed" in updates and not updates["user_api_keys_allowed"]
    touches_user_key_settings = ("user_api_keys_allowed" in updates
                                  or "user_api_keys_daily_quota_default" in updates)

    def _hook(conn, hook_uid, _updates):
        if touches_user_key_settings:
            # 自己発行の書込み（`insert_api_key`）と同じロックを取ってから適用する: 「この2キー
            # への admin 書込み」と「トグルが ON である前提の自己発行（既定/上限の再読を含む）」が
            # 同時に起きても、どちらか一方が完全に先に終わってからもう一方が始まる。
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_USER_KEY_LOCK,))
        if turning_off:
            _revoke_self_issued_api_keys_in_tx(conn, hook_uid)

    return _settings_mod.set_system_settings(uid, updates, secret_keys=secret_keys, in_txn=_hook)


def touch_api_key(key_id: int) -> None:
    """last_used_at 更新（best-effort・認証成功時に呼ぶ）。"""
    _ensure()
    with _connect() as c:
        c.execute("UPDATE api_keys SET last_used_at=now() WHERE id=%s", (key_id,))
