"""api_keys（外部連携 API キー・sherpa/store/api_keys.py）の unit テスト（フェーズ4 S11 新設）。

リファクタリング計画フェーズ4（docs/proposals/2026-07-02-リファクタリング計画.md）の受け入れ基準
「各モジュールに対応する unit テストファイルが1:1で存在する（api_keys は新設）」に対応する。

insert_api_key → api_key_by_hash → list_api_keys → touch_api_key → revoke_api_key の
round-trip を確認する。DB 接続を要するため、既存 store 系テスト（tests/api/test_ext_api.py・
tests/api/test_workspace.py 等）と同じ graceful skip の流儀（DB 不可なら `pytest.skip`）に合わせる。
"""
from __future__ import annotations

import threading
import time
import uuid

import psycopg
import pytest

from sherpa import store


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> None:
    """接続プローブ（軽量 `SELECT 1`）だけを「DB 到達不能」の skip 対象にする。プローブが
    通った後の `store.init_schema()`（DDL 適用・migration を含む本体）はここでは一切 catch
    しない——`init_schema()` 全体を skip 対象にすると、接続はできているのに migration
    ロジック自体にバグがあるケース（`psycopg.OperationalError` を含みうる。例: `init_schema()`
    内部でのデッドロック）まで一律「DB down」として静かに skip してしまい、fail-closed の
    検証が CI ですり抜ける。プローブ（接続の確立可否だけを見る）と本体（その後の実処理）を
    分離することで、「そもそも接続できない」（環境要因・skip が妥当）と「接続はできたが
    処理中に失敗した」（コード/インフラ的異常・fail が正しい）を区別する。プローブの
    `psycopg.OperationalError` は接続不能等の操作レベル障害を広く含む DBAPI の標準的な分類
    ——共有 DB への接続自体が一時的にできない状況はこれに含めて skip 対象のままにする。
    """
    try:
        with store._connect(connect_timeout=5) as probe_conn:
            probe_conn.execute("SELECT 1")
    except psycopg.OperationalError as e:
        pytest.skip(f"DB down: {e}")
    store.init_schema()


def test_api_key_round_trip_insert_by_hash_list_touch_revoke():
    _try_init()
    sfx = _sfx()
    key_hash = f"hash-{sfx}"
    key_prefix = f"pfx{sfx}"[:12]
    label = f"unit-test-key-{sfx}"

    inserted = store.insert_api_key(key_hash, key_prefix, label, "admin")
    assert inserted["key_prefix"] == key_prefix
    assert inserted["label"] == label
    assert inserted["created_by"] == "admin"
    key_id = inserted["id"]

    # by_hash: 未失効の行を返す（key_hash 自体もそのまま含む）。
    row = store.api_key_by_hash(key_hash)
    assert row is not None
    assert row["id"] == key_id
    assert row["key_hash"] == key_hash
    assert row["revoked_at"] is None

    # list: 挿入した行が含まれ、last_used_at はまだ未設定。
    listed = store.list_api_keys()
    found = next((r for r in listed if r["id"] == key_id), None)
    assert found is not None
    assert found["label"] == label
    assert found["last_used_at"] is None

    # touch: last_used_at が更新される（best-effort・認証成功時に呼ぶ）。
    store.touch_api_key(key_id)
    found_after_touch = next(r for r in store.list_api_keys() if r["id"] == key_id)
    assert found_after_touch["last_used_at"] is not None

    # revoke: revoked_at が立つ。
    revoked = store.revoke_api_key(key_id, "admin")
    assert revoked is not None
    assert revoked["revoked_at"] is not None
    assert revoked["revoked_by"] == "admin"
    first_revoked_at = revoked["revoked_at"]

    # 冪等: 二重失効は revoked_at/revoked_by を変えない（COALESCE）。
    revoked_again = store.revoke_api_key(key_id, "someone-else")
    assert revoked_again is not None
    assert revoked_again["revoked_at"] == first_revoked_at
    assert revoked_again["revoked_by"] == "admin"

    # api_key_by_hash は失効済みでも返す（呼び出し側が revoked_at を見て 401 にする設計）。
    row_after_revoke = store.api_key_by_hash(key_hash)
    assert row_after_revoke is not None
    assert row_after_revoke["revoked_at"] is not None


def test_revoke_api_key_unknown_id_returns_none():
    _try_init()
    assert store.revoke_api_key(-1, "admin") is None


def test_allowed_worlds_round_trip():
    """allowed_worlds は None（既定・全 world 許可）と非 None の両方を保持する。"""
    _try_init()
    sfx = _sfx()

    # 未指定（None）＝既存キーと同じ後方互換の挙動。
    unscoped = store.insert_api_key(f"hash-u-{sfx}", f"pfxu{sfx}"[:12], f"unscoped-{sfx}", "admin")
    assert unscoped["allowed_worlds"] is None
    row = store.api_key_by_hash(f"hash-u-{sfx}")
    assert row["allowed_worlds"] is None

    # 指定あり＝world スコープ付き。
    scoped = store.insert_api_key(f"hash-s-{sfx}", f"pfxs{sfx}"[:12], f"scoped-{sfx}", "admin",
                                  allowed_worlds=["v1", "v2"])
    assert scoped["allowed_worlds"] == ["v1", "v2"]
    row = store.api_key_by_hash(f"hash-s-{sfx}")
    assert row["allowed_worlds"] == ["v1", "v2"]
    listed = next(r for r in store.list_api_keys() if r["id"] == scoped["id"])
    assert listed["allowed_worlds"] == ["v1", "v2"]


# ===== 外部連携 API キー: expires_at・daily_quota・owner_uid =====

def test_expires_at_and_daily_quota_round_trip():
    """expires_at／daily_quota は None（既定・既存キーと同じ後方互換）と非 None の両方を保持する。"""
    _try_init()
    sfx = _sfx()
    from datetime import datetime, timezone

    unset = store.insert_api_key(f"hash-pu-{sfx}", f"pfxpu{sfx}"[:12], f"unset-{sfx}", "admin")
    assert unset["expires_at"] is None
    assert unset["daily_quota"] is None
    assert unset["owner_uid"] is None

    exp = datetime(2099, 1, 1, tzinfo=timezone.utc)
    set_row = store.insert_api_key(f"hash-ps-{sfx}", f"pfxps{sfx}"[:12], f"set-{sfx}", "admin",
                                   expires_at=exp, daily_quota=10)
    assert set_row["expires_at"] == exp
    assert set_row["daily_quota"] == 10
    row = store.api_key_by_hash(f"hash-ps-{sfx}")
    assert row["expires_at"] == exp
    assert row["daily_quota"] == 10


def test_self_issued_key_requires_user_api_keys_allowed():
    """owner_uid 付きの発行は system_settings.user_api_keys_allowed を同一トランザクションで
    再確認する（偽なら UserApiKeysDisallowedError・A6 の PersonalKeysDisallowedError と同型）。"""
    _try_init()
    sfx = _sfx()
    store.set_system_settings("admin", {"user_api_keys_allowed": False})
    with pytest.raises(store.UserApiKeysDisallowedError):
        store.insert_api_key(f"hash-dis-{sfx}", f"pfxdis{sfx}"[:12], f"dis-{sfx}", f"user-{sfx}",
                             owner_uid=f"user-{sfx}")

    store.set_system_settings("admin", {"user_api_keys_allowed": True})
    try:
        row = store.insert_api_key(f"hash-ok-{sfx}", f"pfxok{sfx}"[:12], f"ok-{sfx}", f"user-{sfx}",
                                   owner_uid=f"user-{sfx}")
        assert row["owner_uid"] == f"user-{sfx}"
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})

    # admin 発行（owner_uid=None）はトグルと無関係に常に発行できる。
    admin_row = store.insert_api_key(f"hash-adm-{sfx}", f"pfxadm{sfx}"[:12], f"adm-{sfx}", "admin")
    assert admin_row["owner_uid"] is None


def test_list_and_revoke_api_keys_owner_scoped():
    """`owner_uid` 指定は一覧/失効を本人のキーだけへ絞り込む（他人/admin発行キーは対象外）。"""
    _try_init()
    sfx = _sfx()
    uid = f"owner-{sfx}"
    store.set_system_settings("admin", {"user_api_keys_allowed": True})
    try:
        mine = store.insert_api_key(f"hash-mine-{sfx}", f"pfxm{sfx}"[:12], f"mine-{sfx}", uid,
                                    owner_uid=uid)
        others = store.insert_api_key(f"hash-oth-{sfx}", f"pfxo{sfx}"[:12], f"oth-{sfx}",
                                      f"other-{sfx}", owner_uid=f"other-{sfx}")
        admin_key = store.insert_api_key(f"hash-adm2-{sfx}", f"pfxa2{sfx}"[:12], f"adm2-{sfx}", "admin")

        my_list_ids = {r["id"] for r in store.list_api_keys(owner_uid=uid)}
        assert mine["id"] in my_list_ids
        assert others["id"] not in my_list_ids
        assert admin_key["id"] not in my_list_ids

        # 他人のキーは失効できない（owner_uid 不一致＝None＝呼び出し側が404にする）。
        assert store.revoke_api_key(others["id"], uid, owner_uid=uid) is None
        # admin 発行キーも同様に失効できない。
        assert store.revoke_api_key(admin_key["id"], uid, owner_uid=uid) is None
        # 自分のキーは失効できる。
        revoked = store.revoke_api_key(mine["id"], uid, owner_uid=uid)
        assert revoked is not None
        assert revoked["id"] == mine["id"]
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})


def test_revoke_self_issued_api_keys_purges_only_owned_active_keys():
    """トグル OFF 時の一括失効（`revoke_self_issued_api_keys`）は owner_uid が非 NULL の
    未失効キーだけを対象にする（admin 発行キーは対象外＝A6 の purge_personal_api_keys と同型）。"""
    _try_init()
    sfx = _sfx()
    uid = f"purge-{sfx}"
    store.set_system_settings("admin", {"user_api_keys_allowed": True})
    self_issued = store.insert_api_key(f"hash-pg-{sfx}", f"pfxpg{sfx}"[:12], f"pg-{sfx}", uid,
                                       owner_uid=uid)
    admin_key = store.insert_api_key(f"hash-pg2-{sfx}", f"pfxpg2{sfx}"[:12], f"pg2-{sfx}", "admin")

    store.revoke_self_issued_api_keys(actor="admin")

    self_after = next(r for r in store.list_api_keys() if r["id"] == self_issued["id"])
    admin_after = next(r for r in store.list_api_keys() if r["id"] == admin_key["id"])
    assert self_after["revoked_at"] is not None
    assert admin_after["revoked_at"] is None

    # 冪等: 既に失効済みキーが無くても0件で安全に呼べる。
    assert store.revoke_self_issued_api_keys(actor="admin") == 0
    store.set_system_settings("admin", {"user_api_keys_allowed": None})


def test_count_self_issued_active_api_keys_excludes_revoked_and_expired():
    """「有効件数」は失効済みだけでなく期限切れも数えない（未失効かつ未期限のみ）。"""
    _try_init()
    sfx = _sfx()
    uid = f"cnt-{sfx}"
    from datetime import datetime, timedelta, timezone
    store.set_system_settings("admin", {"user_api_keys_allowed": True})
    try:
        before = store.count_self_issued_active_api_keys()
        row = store.insert_api_key(f"hash-cnt-{sfx}", f"pfxcnt{sfx}"[:12], f"cnt-{sfx}", uid,
                                   owner_uid=uid)
        assert store.count_self_issued_active_api_keys() == before + 1

        past = datetime.now(timezone.utc) - timedelta(days=1)
        expired = store.insert_api_key(f"hash-cntexp-{sfx}", f"pfxcex{sfx}"[:12], f"cntexp-{sfx}",
                                       uid, owner_uid=uid, expires_at=past)
        assert expired["id"]   # 発行自体は成功する（期限切れは認証時に拒否されるだけ）
        assert store.count_self_issued_active_api_keys() == before + 1   # 期限切れは数えない

        store.revoke_api_key(row["id"], uid, owner_uid=uid)
        assert store.count_self_issued_active_api_keys() == before
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})


def test_owner_status_joined_for_self_issued_keys_only():
    """`api_key_by_hash` は自己発行キー（owner_uid 非 NULL）にだけ所有者の `users.status` を
    同梱する（admin 発行キーは owner_uid が NULL のため owner_status も常に None）。"""
    _try_init()
    sfx = _sfx()
    uid = f"ownstat-{sfx}"
    from sherpa import auth
    store.upsert_user(uid, email=f"{uid}@t.local", display_name=uid,
                      password_hash=auth.hash_password("Passw0rd!"), role="user", status="active")
    store.set_system_settings("admin", {"user_api_keys_allowed": True})
    try:
        self_row = store.insert_api_key(f"hash-os1-{sfx}", f"pfxos1{sfx}"[:12], f"os1-{sfx}",
                                        uid, owner_uid=uid)
        fetched = store.api_key_by_hash(f"hash-os1-{sfx}")
        assert fetched["owner_status"] == "active"

        store.upsert_user(uid, role="user", status="disabled")
        fetched_after_disable = store.api_key_by_hash(f"hash-os1-{sfx}")
        assert fetched_after_disable["owner_status"] == "disabled"

        admin_row = store.insert_api_key(f"hash-os2-{sfx}", f"pfxos2{sfx}"[:12], f"os2-{sfx}", "admin")
        fetched_admin = store.api_key_by_hash(f"hash-os2-{sfx}")
        assert fetched_admin["owner_status"] is None
        assert self_row["id"] and admin_row["id"]
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})
        store.upsert_user(uid, role="user", status="active")


# ===== Webhook 通知（PART-6）: list_webhook_keys_for_world =====

def test_list_webhook_keys_for_world_excludes_expired_and_inactive_owner():
    """RV是正#2: `list_webhook_keys_for_world` は失効・`webhook_url` 無しに加えて、期限切れ
    （`expires_at` が過去）・所有者が非 active（`owner_uid` 非 NULL かつ `users.status != 'active'`）
    のキーも対象から除く。admin 発行キー（`owner_uid` NULL）は所有者チェックの対象外。"""
    _try_init()
    sfx = _sfx()
    world = f"whworld-{sfx}"
    uid = f"whowner-{sfx}"
    from datetime import datetime, timedelta, timezone
    from sherpa import auth
    store.upsert_user(uid, email=f"{uid}@t.local", display_name=uid,
                      password_hash=auth.hash_password("Passw0rd!"), role="user", status="active")
    store.set_system_settings("admin", {"user_api_keys_allowed": True})
    try:
        admin_key = store.insert_api_key(
            f"hash-whadm-{sfx}", f"pfxwha{sfx}"[:12], "whadm", "admin", allowed_worlds=[world],
            webhook_url="https://wh-admin.example/hook", webhook_secret="s-admin")
        active_self_key = store.insert_api_key(
            f"hash-whact-{sfx}", f"pfxwhc{sfx}"[:12], "whact", uid, owner_uid=uid,
            allowed_worlds=[world],
            webhook_url="https://wh-active.example/hook", webhook_secret="s-active")
        past = datetime.now(timezone.utc) - timedelta(days=1)
        expired_key = store.insert_api_key(
            f"hash-whexp-{sfx}", f"pfxwhe{sfx}"[:12], "whexp", "admin", allowed_worlds=[world],
            expires_at=past,
            webhook_url="https://wh-expired.example/hook", webhook_secret="s-expired")
        no_webhook_key = store.insert_api_key(
            f"hash-whnone-{sfx}", f"pfxwhn{sfx}"[:12], "whnone", "admin", allowed_worlds=[world])

        got_ids = {row["id"] for row in store.list_webhook_keys_for_world(world)}
        assert admin_key["id"] in got_ids
        assert active_self_key["id"] in got_ids
        assert expired_key["id"] not in got_ids       # 期限切れは除外
        assert no_webhook_key["id"] not in got_ids     # webhook_url 未設定は除外

        # 所有者を disabled にすると、以後は列挙対象から外れる（admin 発行キーは影響を受けない）。
        store.upsert_user(uid, role="user", status="disabled")
        got_ids_after_disable = {row["id"] for row in store.list_webhook_keys_for_world(world)}
        assert active_self_key["id"] not in got_ids_after_disable
        assert admin_key["id"] in got_ids_after_disable
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})
        store.upsert_user(uid, role="user", status="active")


def test_list_webhook_keys_for_world_scopes_by_allowed_worlds():
    """`allowed_worlds` が None（全 world 許可）または対象 world を含む場合のみ対象になる。"""
    _try_init()
    sfx = _sfx()
    world = f"whscope-{sfx}"
    other_world = f"whother-{sfx}"
    unscoped = store.insert_api_key(
        f"hash-whun-{sfx}", f"pfxwhu{sfx}"[:12], "whun", "admin",
        webhook_url="https://wh-unscoped.example/hook", webhook_secret="s-un")
    scoped_in = store.insert_api_key(
        f"hash-whin-{sfx}", f"pfxwhi{sfx}"[:12], "whin", "admin", allowed_worlds=[world],
        webhook_url="https://wh-in.example/hook", webhook_secret="s-in")
    scoped_out = store.insert_api_key(
        f"hash-whout-{sfx}", f"pfxwho{sfx}"[:12], "whout", "admin", allowed_worlds=[other_world],
        webhook_url="https://wh-out.example/hook", webhook_secret="s-out")

    got_ids = {row["id"] for row in store.list_webhook_keys_for_world(world)}
    assert unscoped["id"] in got_ids
    assert scoped_in["id"] in got_ids
    assert scoped_out["id"] not in got_ids


def test_apply_system_settings_and_revoke_if_disabled_is_atomic_and_covers_explicit_null():
    """OFF（明示 false・明示 null のいずれも）で保存すると、設定変更と一括失効が同一トランザクション
    で行われる。再度 ON にしても、OFF 時点で失効済みだったキーは復活しない。"""
    _try_init()
    sfx = _sfx()
    uid_false = f"atmf-{sfx}"
    uid_null = f"atmn-{sfx}"
    store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": True})
    try:
        key_false = store.insert_api_key(f"hash-atmf-{sfx}", f"pfxatf{sfx}"[:12], f"atmf-{sfx}",
                                         uid_false, owner_uid=uid_false)
        key_null = store.insert_api_key(f"hash-atmn-{sfx}", f"pfxatn{sfx}"[:12], f"atmn-{sfx}",
                                        uid_null, owner_uid=uid_null)

        # 明示 false。
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": False})
        after_false = next(r for r in store.list_api_keys() if r["id"] == key_false["id"])
        assert after_false["revoked_at"] is not None

        # 再度 ON にしても、OFF 時点で失効済みだったキーは復活しない（revoked_at は消えない）。
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": True})
        after_reenable = next(r for r in store.list_api_keys() if r["id"] == key_false["id"])
        assert after_reenable["revoked_at"] is not None

        # 明示 null（未設定へ戻す＝実効 false）も同様に一括失効の対象になる。
        # `store.get_system_settings()` は tests/unit/conftest.py の autouse fixture が固定 dict
        # に据えているため（DB 非到達の hermetic 化）、ここでは呼ばず `insert_api_key`（実 SQL で
        # 直接 system_settings を読む）が成功することで ON になっていることを確認する。
        key_null_2 = store.insert_api_key(f"hash-atmn2-{sfx}", f"pfxatn2{sfx}"[:12], f"atmn2-{sfx}",
                                          uid_null, owner_uid=uid_null)
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": None})
        with pytest.raises(store.UserApiKeysDisallowedError):
            store.insert_api_key(f"hash-atmn3-{sfx}", f"pfxatn3{sfx}"[:12], f"atmn3-{sfx}",
                                 uid_null, owner_uid=uid_null)
        after_null = next(r for r in store.list_api_keys() if r["id"] == key_null["id"])
        after_null_2 = next(r for r in store.list_api_keys() if r["id"] == key_null_2["id"])
        assert after_null["revoked_at"] is not None
        assert after_null_2["revoked_at"] is not None
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})


def test_count_ext_api_calls_by_key_positive_key_separation_and_window():
    """呼び出し数は指定した key_ids だけを対象に集計する（対象外キーの呼び出しは混入しない）。
    空/None を渡すと空 dict（全キー無制限集計はしない）。"""
    _try_init()
    sfx = _sfx()
    key_a = store.insert_api_key(f"hash-cca-{sfx}", f"pfxcca{sfx}"[:12], f"cca-{sfx}", "admin")
    key_b = store.insert_api_key(f"hash-ccb-{sfx}", f"pfxccb{sfx}"[:12], f"ccb-{sfx}", "admin")
    store.audit(f"ext:{key_a['id']}", "ext_api.search", "ext_search", None, detail={})
    store.audit(f"ext:{key_a['id']}", "ext_api.search", "ext_search", None, detail={})
    store.audit(f"ext:{key_b['id']}", "ext_api.search", "ext_search", None, detail={})

    only_a = store.count_ext_api_calls_by_key([key_a["id"]])
    assert only_a.get(key_a["id"]) == 2
    assert key_b["id"] not in only_a   # key_b を渡していない＝集計に混ざらない（対象キー限定）

    both = store.count_ext_api_calls_by_key([key_a["id"], key_b["id"]])
    assert both[key_a["id"]] == 2
    assert both[key_b["id"]] == 1

    assert store.count_ext_api_calls_by_key([]) == {}
    assert store.count_ext_api_calls_by_key(None) == {}


def test_self_issued_quota_reread_at_write_time_not_stale_caller_value():
    """`daily_quota` の TOCTOU 対策: `insert_api_key` は呼び出し側が渡した値をそのまま使わず、
    `_USER_KEY_LOCK` 下で DB から現在の上限を再読して確定する。呼び出し側が「古い（もう有効で
    ない）上限」のつもりで大きな値を渡しても、現在の上限を超えていれば拒否される。"""
    _try_init()
    sfx = _sfx()
    uid = f"toctou-{sfx}"
    store.set_system_settings("admin", {"user_api_keys_allowed": True,
                                        "user_api_keys_daily_quota_default": 100})
    try:
        # 呼び出し側は「上限は100のはず」で daily_quota=100 を渡すが、書込み直前に admin が
        # 5へ引き下げていたとする（TOCTOU シナリオの再現）。
        store.set_system_settings("admin", {"user_api_keys_daily_quota_default": 5})
        with pytest.raises(store.SelfIssuedQuotaExceededError):
            store.insert_api_key(f"hash-toctou-{sfx}", f"pfxtc{sfx}"[:12], f"toctou-{sfx}",
                                 uid, daily_quota=100, owner_uid=uid)

        # 現在の上限（5）以下なら成功し、格納値は指定どおり。
        row = store.insert_api_key(f"hash-toctouok-{sfx}", f"pfxtco{sfx}"[:12], f"toctouok-{sfx}",
                                   uid, daily_quota=5, owner_uid=uid)
        assert row["daily_quota"] == 5

        # 未指定なら現在の上限がそのまま既定として適用される。
        row2 = store.insert_api_key(f"hash-toctoudef-{sfx}", f"pfxtcd{sfx}"[:12],
                                    f"toctoudef-{sfx}", uid, owner_uid=uid)
        assert row2["daily_quota"] == 5
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None,
                                            "user_api_keys_daily_quota_default": None})


def test_self_issued_quota_is_non_retroactive():
    """管理者が既定/上限を引き下げても、発行済みキーの `daily_quota` は変わらない（非遡及）。"""
    _try_init()
    sfx = _sfx()
    uid = f"retro-{sfx}"
    store.set_system_settings("admin", {"user_api_keys_allowed": True,
                                        "user_api_keys_daily_quota_default": 100})
    try:
        row = store.insert_api_key(f"hash-retro-{sfx}", f"pfxret{sfx}"[:12], f"retro-{sfx}",
                                   uid, owner_uid=uid)
        assert row["daily_quota"] == 100

        store.set_system_settings("admin", {"user_api_keys_daily_quota_default": 5})
        # 既に発行済みのキーの daily_quota は書き換わらない（発行時点の値のまま）。
        after = store.api_key_by_hash(f"hash-retro-{sfx}")
        assert after["daily_quota"] == 100
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None,
                                            "user_api_keys_daily_quota_default": None})


def test_apply_system_settings_and_revoke_if_disabled_rolls_back_on_audit_failure(monkeypatch):
    """監査 INSERT が失敗すると、設定変更・一括失効の両方がロールバックされる（部分適用なし・
    fail-closed）。"""
    _try_init()
    sfx = _sfx()
    uid = f"rbk-{sfx}"
    store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": True})
    key = store.insert_api_key(f"hash-rbk-{sfx}", f"pfxrbk{sfx}"[:12], f"rbk-{sfx}",
                               uid, owner_uid=uid)
    try:
        def _boom(*a, **kw):
            raise RuntimeError("simulated audit failure")

        monkeypatch.setattr(store, "_audit_insert", _boom)
        with pytest.raises(RuntimeError):
            store.apply_system_settings_and_revoke_if_disabled(
                "admin", {"user_api_keys_allowed": False})
        monkeypatch.undo()

        # ロールバックされているので、設定は OFF になっておらず（次の insert が成功する）、
        # キーも失効していない。
        still_on = store.insert_api_key(f"hash-rbk2-{sfx}", f"pfxrbk2{sfx}"[:12], f"rbk2-{sfx}",
                                        uid, owner_uid=uid)
        still_on_row = next(r for r in store.list_api_keys() if r["id"] == still_on["id"])
        assert still_on_row["revoked_at"] is None
        after = store.api_key_by_hash(f"hash-rbk-{sfx}")
        assert after["revoked_at"] is None, "監査失敗時は一括失効もロールバックされるはず"
        assert key["id"]
    finally:
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": None})


def test_count_ext_api_calls_by_key_excludes_calls_older_than_window():
    """直近30日より前の呼び出しは集計対象から除外する。監査行の `created_at` は不変のまま
    （直接 UPDATE するとハッシュチェーンが壊れる）、集計関数に注入する基準時刻（`now=`）の
    方を未来へずらして窓の境界を再現する。"""
    from datetime import datetime, timedelta, timezone

    _try_init()
    sfx = _sfx()
    key = store.insert_api_key(f"hash-win-{sfx}", f"pfxwin{sfx}"[:12], f"win-{sfx}", "admin")
    store.audit(f"ext:{key['id']}", "ext_api.search", "ext_search", None, detail={})

    # 実際の呼び出しは「今」のままだが、基準時刻を31日後に注入すると窓の外＝0件扱いになる。
    far_future = datetime.now(timezone.utc) + timedelta(days=31)
    counts = store.count_ext_api_calls_by_key([key["id"]], now=far_future)
    assert counts.get(key["id"], 0) == 0

    # 基準時刻を省略（実時刻）すれば直近の呼び出しとして数えられる。
    counts_from_real_now = store.count_ext_api_calls_by_key([key["id"]])
    assert counts_from_real_now[key["id"]] == 1


def test_no_deadlock_between_settings_toggle_and_standalone_revoke_concurrently():
    """設定トグル（`apply_system_settings_and_revoke_if_disabled`）と単体の一括失効
    （`revoke_self_issued_api_keys`）を並行実行してもデッドロックしないことを検証する。

    両経路は `_USER_KEY_LOCK`→（監査 INSERT で）`_AUDIT_CHAIN_LOCK` の同じ順序でロックを
    取る契約になっている（`set_system_settings` の `in_txn` は監査 INSERT より前に呼ぶ）。
    ここでは実際に多重競合させ、(a) どちらのスレッドからも `deadlock` を含む例外が出ない
    こと、(b) 監視スレッドが `pg_blocking_pids()` で相互待ち（サイクル）を一度も観測しない
    こと、の両方を確認する。
    """
    _try_init()
    sfx = _sfx()
    uid = f"race-{sfx}"
    iterations = 25
    errors: list[tuple[str, int, str]] = []
    cycle_observed: list[tuple[int, int]] = []
    monitor_errors: list[str] = []
    poll_count = 0
    stop = threading.Event()

    def worker_a():
        for i in range(iterations):
            try:
                store.apply_system_settings_and_revoke_if_disabled(
                    "admin", {"user_api_keys_allowed": bool(i % 2)})
            except Exception as e:
                errors.append(("a", i, repr(e)))

    def worker_b():
        for i in range(iterations):
            try:
                store.insert_api_key(f"hash-race-{sfx}-{i}", f"pfxr{i}"[:12], f"race-{i}",
                                     uid, owner_uid=uid)
            except store.UserApiKeysDisallowedError:
                continue   # トグルが一時的に OFF だった＝想定内（デッドロックとは無関係）
            except Exception as e:
                errors.append(("b-insert", i, repr(e)))
                continue
            try:
                store.revoke_self_issued_api_keys(actor="admin")
            except Exception as e:
                errors.append(("b-revoke", i, repr(e)))

    def monitor():
        # 別接続で pg_stat_activity/pg_blocking_pids をポーリングし、2バックエンドが互いを
        # ブロックするサイクルが一度も成立しないことを確認する（構造的に起こらないはずの検証）。
        # 監視接続自体の失敗を握り潰さない（黙って `pass` すると「一度も競合を観測して
        # いない」のか「監視自体が最初から動いていない」のか区別できず、偽陰性で緑になりうる）。
        # 実際にポーリングできた回数（`poll_count`）を記録し、呼び出し側で 0 でないことを assert
        # することで、監視が本当に機能していたことを保証する。
        nonlocal poll_count
        try:
            with store._connect() as c:
                while not stop.is_set():
                    rows = c.execute(
                        "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                        "WHERE pid <> pg_backend_pid() AND pg_blocking_pids(pid) != '{}'"
                    ).fetchall()
                    poll_count += 1
                    blocked_by = {r["pid"]: set(r["blockers"]) for r in rows}
                    for pid, blockers in blocked_by.items():
                        for b in blockers:
                            if pid in blocked_by.get(b, set()):
                                cycle_observed.append((pid, b))
                    time.sleep(0.05)
        except Exception as e:
            monitor_errors.append(repr(e))

    t_mon = threading.Thread(target=monitor, daemon=True)
    t_a = threading.Thread(target=worker_a)
    t_b = threading.Thread(target=worker_b)
    try:
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": True})
        t_mon.start()
        t_a.start()
        t_b.start()
        t_a.join(timeout=60)
        t_b.join(timeout=60)
        stop.set()
        t_mon.join(timeout=5)

        assert not t_a.is_alive() and not t_b.is_alive(), "ワーカーがタイムアウトした（ハング疑い）"
        deadlock_errors = [e for e in errors if "deadlock" in e[2].lower()]
        assert not deadlock_errors, f"デッドロックを検出した: {deadlock_errors}"
        assert not errors, f"予期しない例外が発生した: {errors}"
        assert not monitor_errors, f"監視スレッドで例外が発生した（監視が機能していない）: {monitor_errors}"
        assert poll_count > 0, "監視スレッドが一度もポーリングできなかった（監視が機能していない）"
        assert not cycle_observed, f"pg_blocking_pids で相互待ちのサイクルを観測した: {cycle_observed}"
    finally:
        stop.set()
        store.revoke_self_issued_api_keys(actor="admin")
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": None})


@pytest.mark.parametrize("updates", [
    {"user_api_keys_daily_quota_default": 7},
    {"user_api_keys_allowed": True},
], ids=["quota_default_only", "allowed_toggle"])
def test_admin_settings_update_forces_lock_conflict_with_user_key_lock(updates):
    """`user_api_keys_daily_quota_default` だけを更新する場合・`user_api_keys_allowed` を
    トグルする場合のどちらの admin トランザクションも `_USER_KEY_LOCK` を取得することを、実際の
    ロック競合を `pg_blocking_pids()` で直接観測して確認する（デッドロック不在のような消極的な
    確認ではなく、自己発行の TOCTOU 再確認（`insert_api_key`）と同じロックドメインで実際に
    排他されるという積極的な証拠を取る＝強制競合順序のテスト）。

    共有 DB は他プロセス（別 worktree のテスト実行等）が同じ固定ロックキー（`_USER_KEY_LOCK` は
    コードベース全体で共有される定数）を同時に取り合っていることがあり、クエリ文言だけで
    「admin 更新スレッド自身の待ち」と断定すると無関係な他セッションを誤って証拠として拾う
    （実測: 別セッションの `SELECT value FROM system_settings ...` が偶然 holder_pid の
    ブロック対象として観測され続けるケースがあった）。そのため `_USER_KEY_LOCK` を実際に取る
    `sherpa.store.settings._connect`（`apply_system_settings_and_revoke_if_disabled` が
    委譲する `set_system_settings` 自身の接続取得点・PG コネクションプール導入後は
    `psycopg.connect` を admin 更新スレッドの実行中だけラップしても、そのスレッドの
    `_connect()` 呼び出しがプールの使い回し接続を受け取るだけで新規 `psycopg.connect` を
    一切呼ばないことがあるため観測できない）を admin 更新スレッドの実行中だけラップし、
    そのスレッドが実際に受け取った接続の backend pid を直接記録した上で、監視スレッドの
    観測が**その pid そのもの**であることを一致させる（間接的な文言一致ではなく、待ち手 PID
    の直接同定）。
    """
    from sherpa.store import settings as _settings_mod
    from sherpa.store.api_keys import _USER_KEY_LOCK

    _try_init()
    holder_pid: dict = {}
    admin_pid: dict = {}
    holder_ready = threading.Event()
    release = threading.Event()
    samples: list[tuple[int, list]] = []
    monitor_errors: list[str] = []
    poll_count = 0
    stop = threading.Event()
    _ADMIN_THREAD_NAME = "rv-lock-evidence-admin-thread"

    def holder():
        with store._connect() as c:
            holder_pid["pid"] = c.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_USER_KEY_LOCK,))
            holder_ready.set()
            release.wait(timeout=10)
        # with を抜けた時点で commit されロックが解放される。

    t_holder = threading.Thread(target=holder)
    t_holder.start()
    assert holder_ready.wait(timeout=5), "ロック保持スレッドの準備がタイムアウトした"

    # `sherpa.store.settings` は `from .db import _connect` で自分の名前空間に直接束縛している
    # ため、ここでの置き換えは `settings.py`（`set_system_settings` の実装）だけに効き、
    # `store._connect` を直接呼ぶ他スレッド（holder/monitor・下記）には影響しない。admin 更新
    # スレッド（スレッド名で判定）が実際に受け取った接続の backend pid だけを記録する
    # （元の `_connect` をそのまま呼ぶだけ）。
    original_settings_connect = _settings_mod._connect

    def _pid_capturing_connect(*a, **kw):
        conn = original_settings_connect(*a, **kw)
        if threading.current_thread().name == _ADMIN_THREAD_NAME and "pid" not in admin_pid:
            try:
                admin_pid["pid"] = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            except Exception as e:
                admin_pid.setdefault("_capture_error", repr(e))
        return conn

    result: dict = {}

    def admin_update():
        try:
            store.apply_system_settings_and_revoke_if_disabled("admin", updates)
            result["ok"] = True
        except Exception as e:
            result["error"] = repr(e)

    t_admin = threading.Thread(target=admin_update, name=_ADMIN_THREAD_NAME)

    def monitor():
        nonlocal poll_count
        try:
            with store._connect() as c:
                while not stop.is_set():
                    rows = c.execute(
                        "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                        "WHERE pid <> pg_backend_pid() AND pg_blocking_pids(pid) != '{}'"
                    ).fetchall()
                    poll_count += 1
                    for r in rows:
                        if holder_pid["pid"] in r["blockers"]:
                            samples.append((r["pid"], list(r["blockers"])))
                    time.sleep(0.02)
        except Exception as e:
            monitor_errors.append(repr(e))

    t_mon = threading.Thread(target=monitor)
    t_mon.start()
    _settings_mod._connect = _pid_capturing_connect
    try:
        t_admin.start()
        # admin 更新スレッド自身の pid（holder にブロックされている）が観測できるまで待つ。
        # 窓はフルスイート実行時の DB 負荷（並行テストのクエリ・バックグラウンド索引作成）でも
        # 観測が間に合う長さにする（短いと観測前に deadline が尽きて偽陰性になる）。
        deadline = time.time() + 20
        while time.time() < deadline and not (
                admin_pid.get("pid") is not None
                and any(admin_pid["pid"] == s[0] for s in samples)):
            time.sleep(0.05)
        release.set()
        t_admin.join(timeout=10)
    finally:
        _settings_mod._connect = original_settings_connect
    t_holder.join(timeout=10)
    stop.set()
    t_mon.join(timeout=5)

    try:
        assert not monitor_errors, f"監視スレッドで例外が発生した: {monitor_errors}"
        assert poll_count > 0, "監視スレッドが一度もポーリングできなかった（監視が機能していない）"
        assert result.get("ok"), f"admin 更新が失敗した: {result}"
        assert admin_pid.get("pid") is not None, (
            f"admin 更新スレッド自身の backend pid を捕捉できなかった: {admin_pid}")
        matching = [s for s in samples if s[0] == admin_pid["pid"]]
        assert matching, (f"admin の更新（{updates}）自身の接続（pid={admin_pid.get('pid')}）が"
                          f"_USER_KEY_LOCK で実際にブロックされる様子を観測できなかった"
                          f"（ロック順序の統一が効いていない可能性・全サンプル: {samples}）")
        blocked_pid, blockers = matching[0]
        assert holder_pid["pid"] in blockers
    finally:
        reset = {k: None for k in updates}
        store.apply_system_settings_and_revoke_if_disabled("admin", reset)


def test_client_op_id_unique_constraint_and_scoped_recovery_prevents_cross_owner_effects():
    """`client_op_id` は非NULLに限り一意（衝突は一意制約違反＝呼び出し側で409に変換する）。
    また回復用の `revoke_unconfirmed_key_by_client_op_id` は所有条件（`owner_uid`/`created_by`）を
    `client_op_id` と同一SQLの WHERE 句で照合するため、別の所有者が同じ `client_op_id` を使って
    回復を試みても他人のキーには触れない（反転テスト: 所有者 B が所有者 A の client_op_id で
    回復を試みても A のキーは無傷のまま）。
    """
    _try_init()
    sfx = _sfx()
    uid_a = f"cop-a-{sfx}"
    uid_b = f"cop-b-{sfx}"
    shared_cop = str(uuid.uuid4())
    store.set_system_settings("admin", {"user_api_keys_allowed": True})
    try:
        store.insert_api_key(f"hash-copA-{sfx}", f"pfxcpa{sfx}"[:12], "A", uid_a,
                             owner_uid=uid_a, client_op_id=shared_cop)

        # 同じ client_op_id で別ユーザー（B）が発行しようとすると一意制約違反になる。
        with pytest.raises(store.ClientOpIdConflictError):
            store.insert_api_key(f"hash-copB-{sfx}", f"pfxcpb{sfx}"[:12], "B", uid_b,
                                 owner_uid=uid_b, client_op_id=shared_cop)

        # B（そもそも該当キーを持たない）が A の client_op_id で回復を試みても、A のキーには
        # 触れない。
        result_for_b = store.revoke_unconfirmed_key_by_client_op_id(
            shared_cop, "system", owner_uid=uid_b)
        assert result_for_b is None
        key_a = next(r for r in store.list_api_keys(owner_uid=uid_a)
                    if r["client_op_id"] == shared_cop)
        assert key_a["revoked_at"] is None   # A のキーは無傷のまま

        # A 自身の回復は正しく効く。
        result_for_a = store.revoke_unconfirmed_key_by_client_op_id(
            shared_cop, "system", owner_uid=uid_a)
        assert result_for_a is not None
        assert result_for_a["id"] == key_a["id"]
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})


def test_client_op_id_null_values_do_not_conflict():
    """`client_op_id` を指定しない（NULL）発行は何個あっても一意制約に抵触しない
    （部分インデックス＝NULL は対象外・後方互換の admin 発行フローを壊さない）。"""
    _try_init()
    sfx = _sfx()
    k1 = store.insert_api_key(f"hash-nullcop1-{sfx}", f"pfxnc1{sfx}"[:12], "n1", "admin")
    k2 = store.insert_api_key(f"hash-nullcop2-{sfx}", f"pfxnc2{sfx}"[:12], "n2", "admin")
    assert k1["client_op_id"] is None
    assert k2["client_op_id"] is None


def _reset_client_op_id_index_to_legacy(c, sfx, *, rows=True):
    """テスト用ヘルパ: 旧来の（大小文字を区別する）単純な単一列 UNIQUE 部分索引へ張り替える
    （本番ではあり得ない状態を意図的に再現する）。`rows=True` なら大小文字違いの重複行
    （old/new の2行）も一緒に作る。"""
    c.execute("DROP INDEX IF EXISTS api_keys_client_op_id_unique")
    c.execute("CREATE UNIQUE INDEX api_keys_client_op_id_unique ON api_keys(client_op_id) "
              "WHERE client_op_id IS NOT NULL")
    if not rows:
        return None, None, None
    shared_lower = str(uuid.uuid4())
    shared_upper = shared_lower.upper()
    old_id = c.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, client_op_id) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (f"hash-migold-{sfx}", f"pfxmo{sfx}"[:12], "old", "admin", shared_lower),
    ).fetchone()["id"]
    new_id = c.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, client_op_id) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (f"hash-mignew-{sfx}", f"pfxmn{sfx}"[:12], "new", "admin", shared_upper),
    ).fetchone()["id"]
    return shared_lower, old_id, new_id


def test_migrate_client_op_id_unique_index_from_old_case_sensitive_with_duplicates():
    """旧来の（大小文字を区別する）`api_keys_client_op_id_unique` からの移行は一度きりで、
    移行前に大小文字違いの重複（旧索引の下でだけ許されていた状態）を検出したら、新しい方
    （id が大きい方）の行の `client_op_id` へ決定的サフィックス（`-dup-<id>`）を付けて退避する
    （古い方の行の値・両方のキー自体は無傷のまま・自動失効しない）。ログメッセージは呼び出し側
    （`init_schema()`）が commit 後に出す設計のため、ここでは関数の**戻り値**（ログ文言の一覧）
    で確認する（`test_migrate_client_op_id_unique_index_defers_log_until_after_commit` が
    実際に commit 後にしかログされないことを別途確認する）。"""
    from sherpa.store import db as _db_mod

    _try_init()
    sfx = _sfx()
    with store._connect() as c:
        shared_lower, old_id, new_id = _reset_client_op_id_index_to_legacy(c, sfx)

    with store._connect() as c:
        messages = _db_mod._migrate_client_op_id_unique_index(c)

    assert messages, "退避メッセージが返らなかった"
    assert any("大小文字違い重複を検出" in m and str(new_id) in m for m in messages), messages

    with store._connect() as c:
        old_row = c.execute("SELECT client_op_id FROM api_keys WHERE id=%s", (old_id,)).fetchone()
        new_row = c.execute("SELECT client_op_id FROM api_keys WHERE id=%s", (new_id,)).fetchone()
        shape = _db_mod._classify_client_op_id_index(c)

    assert old_row["client_op_id"] == shared_lower   # 古い方（id が小さい方）は無傷。
    assert new_row["client_op_id"] == f"{shared_lower.upper()}-dup-{new_id}"   # 新しい方は退避。
    assert shape == "current"   # 索引は新定義へ張り替わっている。


def test_migrate_client_op_id_unique_index_skips_rebuild_when_already_correct():
    """既に `lower()` 定義（構造的な分類が `"current"`）なら索引を DROP→再作成しない
    （毎起動の無条件再構築を廃止）。"""
    from sherpa.store import db as _db_mod

    _try_init()
    with store._connect() as c:
        c.execute("DROP INDEX IF EXISTS api_keys_client_op_id_unique")
        c.execute("CREATE UNIQUE INDEX api_keys_client_op_id_unique "
                  "ON api_keys(lower(client_op_id)) WHERE client_op_id IS NOT NULL")

    calls: list[str] = []
    with store._connect() as c:
        orig_execute = c.execute

        def _spy_execute(query, *a, **kw):
            calls.append(query)
            return orig_execute(query, *a, **kw)
        c.execute = _spy_execute
        try:
            messages = _db_mod._migrate_client_op_id_unique_index(c)
        finally:
            # PG コネクションプール導入（性能台帳#17 QW2）後は `_connect()` が返す接続の実体が
            # 使い回されるため、インスタンス属性の `execute` 差し替えを戻し忘れると別テストへ
            # 漏れる——`with` を抜ける前に必ず元へ戻す。
            c.execute = orig_execute

    assert messages == []
    executed_ddl = [q for q in calls if "DROP INDEX" in q or "CREATE UNIQUE INDEX" in q]
    assert not executed_ddl, f"既に正しい定義なのに索引を再構築した: {executed_ddl}"


def test_migrate_client_op_id_unique_index_defers_log_until_after_commit():
    """退避のログは接続コンテキストが正常終了（commit）した後にだけ出す設計を、
    `init_schema()` と同じ流儀（`with _connect()` を抜けてからログする）で直接確認する。
    ここでは `_migrate_client_op_id_unique_index()` 自身は一切ログを出さない（メッセージを
    返すだけ）ことを、モジュールのロガーへハンドラを挿して監視し確認する——`with` の内側で
    呼んでいる間は一切ログが記録されず、`with` を抜けてからループでログして初めて記録される。"""
    import io
    import logging

    from sherpa.store import db as _db_mod

    _try_init()
    sfx = _sfx()

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.ERROR)
    logger = logging.getLogger("sherpa")
    logger.addHandler(handler)
    try:
        with store._connect() as c:
            _reset_client_op_id_index_to_legacy(c, sfx)
        with store._connect() as c:
            messages = _db_mod._migrate_client_op_id_unique_index(c)
            # `with` ブロックの内側（commit 前）ではまだ一切ログされていない。
            assert stream.getvalue() == "", "commit 前にログが出てしまっている"
        # `with` を抜けた（commit 済み）後、呼び出し側の流儀どおりにログする。
        for msg in messages:
            logger.error(msg)
        assert "大小文字違い重複を検出" in stream.getvalue()
    finally:
        logger.removeHandler(handler)


def test_migrate_client_op_id_unique_index_rollback_on_ddl_failure_then_retry_succeeds():
    """索引の DROP/CREATE 部分が失敗すると、その回のトランザクション全体がロールバックされ
    （退避 UPDATE も巻き戻る・大文字のまま）、次回の呼び出し（再入）は最初からやり直して
    成功する（DDL失敗→rollback→再入）。"""
    from sherpa.store import db as _db_mod

    _try_init()
    sfx = _sfx()
    with store._connect() as c:
        shared_lower, old_id, new_id = _reset_client_op_id_index_to_legacy(c, sfx)
    shared_upper = shared_lower.upper()

    class _InducedDdlFailure(Exception):
        pass

    with pytest.raises(_InducedDdlFailure):
        with store._connect() as c:
            orig_execute = c.execute

            def _failing_execute(query, *a, **kw):
                if isinstance(query, str) and query.strip().startswith("CREATE UNIQUE INDEX"):
                    raise _InducedDdlFailure("induced DDL failure")
                return orig_execute(query, *a, **kw)
            c.execute = _failing_execute
            try:
                _db_mod._migrate_client_op_id_unique_index(c)
            finally:
                # PG コネクションプール導入（性能台帳#17 QW2）後は `_connect()` が返す接続の実体が
                # 使い回される——このブロックは意図的に例外を起こすため、`with` の commit/rollback
                # 処理より前に必ず `execute` を元へ戻し、次に同じ接続を受け取った別テストへ
                # 「CREATE UNIQUE INDEX を常に失敗させる」偽装が漏れないようにする。
                c.execute = orig_execute

    # ロールバックにより退避 UPDATE も巻き戻っている（大文字のまま・-dup- サフィックス無し）。
    with store._connect() as c:
        new_row = c.execute("SELECT client_op_id FROM api_keys WHERE id=%s", (new_id,)).fetchone()
        shape = _db_mod._classify_client_op_id_index(c)
    assert new_row["client_op_id"] == shared_upper
    assert shape == "legacy"   # 索引も旧定義のまま（張り替え自体もロールバックされている）。

    # 再入（2回目）は正常に完走する。
    with store._connect() as c:
        messages = _db_mod._migrate_client_op_id_unique_index(c)
    assert messages
    with store._connect() as c:
        new_row2 = c.execute("SELECT client_op_id FROM api_keys WHERE id=%s", (new_id,)).fetchone()
        shape2 = _db_mod._classify_client_op_id_index(c)
    assert new_row2["client_op_id"] == f"{shared_upper}-dup-{new_id}"
    assert shape2 == "current"


def test_init_schema_defers_log_and_keeps_inited_false_on_failure_then_succeeds_on_retry():
    """`init_schema()`（公開エントリポイント）を直接通して固定する。失敗の注入点は
    `_migrate_client_op_id_unique_index()` がメッセージを**正常に返した後**（schema_version の
    記録処理）にする——helper 自身は成功して退避も完了しているのに、同じトランザクション内の
    「その後」の処理が失敗して rollback される、というより厳しいケースを確認する（helper の
    DDL 自体が失敗するケースは `test_migrate_client_op_id_unique_index_rollback_on_ddl_failure_
    then_retry_succeeds` が別途担保する）。この場合も、ログは1件も出ず（`with _connect()` が
    rollback して例外を伝播するため、呼び出し元のログ出力ループへ到達しない——退避 UPDATE 自体も
    巻き戻る）、`db._inited` は False のままになる（起動失敗として扱われ、次回呼び出しで
    再試行される）。次回の呼び出し（再入）で成功して初めて、commit 後にログが出て `_inited`
    が True になる。"""
    import io
    import logging

    from sherpa.store import db as _db_mod

    _try_init()   # 前提を整える（Postgres 到達可能なことを先に確認・失敗時は skip）。
    sfx = _sfx()
    with store._connect() as c:
        _reset_client_op_id_index_to_legacy(c, sfx)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.ERROR)
    logger = logging.getLogger("sherpa")
    logger.addHandler(handler)

    class _InducedPostMigrationFailure(Exception):
        pass

    original_connect = _db_mod._connect

    def _failing_connect(*a, **kw):
        conn = original_connect(*a, **kw)
        original_execute = conn.execute

        def _wrapped_execute(query, *qa, **qkw):
            # 索引の張替え（DROP/CREATE）は素通しし、_migrate_client_op_id_unique_index() が
            # メッセージを返した**後**の schema_version 記録処理でだけ失敗させる。
            if isinstance(query, str) and query.strip().startswith("INSERT INTO schema_version"):
                raise _InducedPostMigrationFailure("induced post-migration failure")
            return original_execute(query, *qa, **qkw)
        conn.execute = _wrapped_execute
        return conn

    saved_inited = _db_mod._inited
    saved_schema_hash = _db_mod._SCHEMA_HASH
    try:
        _db_mod._inited = False
        _db_mod._connect = _failing_connect
        # schema_version への INSERT は「直近行のハッシュと違う時だけ」実行される（毎起動
        # 行が増え続けないための冪等ガード）。共有 DB では直近行が既に現在のハッシュと一致して
        # いることが多く、それだと INSERT 自体が発火せず注入した失敗が再現しない——ハッシュを
        # 一時的に変えて INSERT が必ず実行されるようにする。
        _db_mod._SCHEMA_HASH = saved_schema_hash + "-induced-mismatch-for-test"
        with pytest.raises(_InducedPostMigrationFailure):
            _db_mod.init_schema()
        assert _db_mod._inited is False, "失敗したのに _inited が True になっている"
        assert "大小文字違い重複を検出" not in stream.getvalue(), (
            "helper がメッセージを返した後の失敗で rollback されたのに、退避成功ログが出ている")

        # ロールバックにより索引の張替え自体も巻き戻っている（旧定義のまま）。
        with store._connect() as c2:
            shape_after_failure = _db_mod._classify_client_op_id_index(c2)
        assert shape_after_failure == "legacy"

        # 再入（2回目）: 誘発を止めた通常の _connect・本来のハッシュで呼び直すと成功する。
        _db_mod._connect = original_connect
        _db_mod._SCHEMA_HASH = saved_schema_hash
        _db_mod.init_schema()
        assert _db_mod._inited is True
        assert "大小文字違い重複を検出" in stream.getvalue()
    finally:
        _db_mod._connect = original_connect
        _db_mod._SCHEMA_HASH = saved_schema_hash
        logger.removeHandler(handler)
        _db_mod._inited = saved_inited


def test_init_schema_forwards_connect_timeout_to_both_connections(monkeypatch):
    """RV9 是正の固定: 未初期化（`_inited=False`）状態で `store.get_world`/
    `store.get_system_settings`/`store.usage_events.add_usage_event` 等（PART-4 が残り時間
    ベースで `connect_timeout` を渡す経路）が `_ensure()` 経由で `init_schema()` を起動させても、
    呼び出し元の予算を迂回しない——`init_schema(connect_timeout=...)` が lock 取得用・DDL 実行用
    の**両方**の接続へ、整数秒へ切り上げ・最小1秒でクランプした値を渡すことを、`_ensure()` を
    no-op化せず実際に `init_schema()` を起動させて固定する（実 Postgres・`_migrate_...` 等の
    本体処理は通常どおり実行される＝冪等 DDL なので安全に再実行できる）。"""
    from sherpa.store import db as _db_mod

    _try_init()   # 前提を整える（Postgres 到達可能なことを先に確認・失敗時は skip）。
    seen_connect_timeouts: list = []
    original_psycopg_connect = _db_mod.psycopg.connect

    def _spy_connect(dsn, **kw):
        if "connect_timeout" in kw:
            seen_connect_timeouts.append(kw["connect_timeout"])
        return original_psycopg_connect(dsn, **kw)

    saved_inited = _db_mod._inited
    try:
        _db_mod._inited = False
        monkeypatch.setattr(_db_mod.psycopg, "connect", _spy_connect)
        _db_mod.init_schema(connect_timeout=0.3)   # 1未満→整数秒へ切り上げ・最小1秒でクランプされるはず
        assert _db_mod._inited is True
    finally:
        _db_mod._inited = saved_inited
    # lock 取得用接続（lock_conn）・DDL 実行用接続（_connect 経由）の両方で観測されるはず。
    assert len(seen_connect_timeouts) >= 2, seen_connect_timeouts
    assert all(ct == 1 for ct in seen_connect_timeouts), seen_connect_timeouts


def test_init_schema_without_connect_timeout_uses_existing_fixed_default(monkeypatch):
    """省略時（既定 None）は従来どおり `_INIT_CONNECT_TIMEOUT`（固定5秒）のまま——既存呼び出し元
    （env 起動時シード・healthz 等）は無変更。"""
    from sherpa.store import db as _db_mod

    _try_init()
    seen_connect_timeouts: list = []
    original_psycopg_connect = _db_mod.psycopg.connect

    def _spy_connect(dsn, **kw):
        if "connect_timeout" in kw:
            seen_connect_timeouts.append(kw["connect_timeout"])
        return original_psycopg_connect(dsn, **kw)

    saved_inited = _db_mod._inited
    try:
        _db_mod._inited = False
        monkeypatch.setattr(_db_mod.psycopg, "connect", _spy_connect)
        _db_mod.init_schema()
    finally:
        _db_mod._inited = saved_inited
    assert len(seen_connect_timeouts) >= 2, seen_connect_timeouts
    assert all(ct == _db_mod._INIT_CONNECT_TIMEOUT for ct in seen_connect_timeouts), seen_connect_timeouts


def test_init_schema_deducts_lock_wait_elapsed_from_ddl_connect_timeout(monkeypatch):
    """RV10 是正の固定: lock 用接続の確立＋`pg_advisory_lock` 待ちで経過した分を、DDL 用接続の
    `connect_timeout` から差し引く——同じ満額をそのまま2回使うと、lock 取得自体に時間がかかった
    上で DDL 用接続へも満額が再付与され、合計の実時間が最大で約2倍まで伸びうる（`store.worlds.
    get_world`等の二重消費是正と同型）。"""
    from sherpa.store import db as _db_mod

    _try_init()
    seen_connect_timeouts: list = []
    original_psycopg_connect = _db_mod.psycopg.connect

    def _spy_connect(dsn, **kw):
        if "connect_timeout" in kw:
            seen_connect_timeouts.append(kw["connect_timeout"])
        return original_psycopg_connect(dsn, **kw)

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        # 1回目（絶対期限の算出）は基準時刻・2回目以降（lock 取得後の残り計算）は3秒経過したことにする。
        return 100.0 if calls["n"] <= 1 else 103.0

    saved_inited = _db_mod._inited
    try:
        _db_mod._inited = False
        monkeypatch.setattr(_db_mod.psycopg, "connect", _spy_connect)
        monkeypatch.setattr(_db_mod.time, "monotonic", _clock)
        _db_mod.init_schema(connect_timeout=10)
        assert _db_mod._inited is True
    finally:
        _db_mod._inited = saved_inited
    assert len(seen_connect_timeouts) >= 2, seen_connect_timeouts
    assert seen_connect_timeouts[0] == 10, seen_connect_timeouts   # lock 用接続: 満額のまま
    assert seen_connect_timeouts[1] == 7, seen_connect_timeouts    # DDL 用接続: 10-3=7（3秒差し引かれる）


def test_init_schema_raises_without_ddl_connect_when_lock_wait_exhausts_budget(monkeypatch):
    """RV11 是正の固定: lock 用接続の確立＋`pg_advisory_lock` 待ちだけで予算を使い切っていたら、
    最低1秒へクランプして DDL 用の新規接続を開始することはせず、`TimeoutError` を送出する
    （lock の unlock/close は `finally` で通常どおり行われ、advisory lock は残らない）。"""
    from sherpa.store import db as _db_mod

    _try_init()
    connect_calls = {"n": 0}
    original_psycopg_connect = _db_mod.psycopg.connect

    def _spy_connect(dsn, **kw):
        connect_calls["n"] += 1
        return original_psycopg_connect(dsn, **kw)

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        # 1回目（絶対期限の算出）は基準時刻・2回目（lock 取得後）は connect_timeout=5 を
        # 優に超える10秒が経過したことにする。
        return 100.0 if calls["n"] <= 1 else 110.0

    saved_inited = _db_mod._inited
    try:
        _db_mod._inited = False
        monkeypatch.setattr(_db_mod.psycopg, "connect", _spy_connect)
        monkeypatch.setattr(_db_mod.time, "monotonic", _clock)
        with pytest.raises(TimeoutError):
            _db_mod.init_schema(connect_timeout=5)
        # `_inited` は立たない（DDL 未実行のまま）。
        assert _db_mod._inited is False
    finally:
        _db_mod._inited = saved_inited
    assert connect_calls["n"] == 1, "DDL 用の2回目の接続を試みてはいけない（lock 用の1回のみのはず）"
    # advisory lock が残っていないこと（別接続で同じ lock を即座に取得できるはず）を確認する。
    with _db_mod._connect() as c:
        key = int.from_bytes(_db_mod.hashlib.sha1(
            f"schema:{_db_mod._KB_ID}".encode("utf-8")).digest()[:8], "big", signed=True)
        got = c.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()["ok"]
        assert got is True, "advisory lock が解放されずに残っている"
        c.execute("SELECT pg_advisory_unlock(%s)", (key,))


@pytest.mark.parametrize("bad_ddl", [
    # 非UNIQUE（同じ式・同じ predicate だが UNIQUE ではない）。
    "CREATE INDEX api_keys_client_op_id_unique ON api_keys(lower(client_op_id)) "
    "WHERE client_op_id IS NOT NULL",
    # 複合キー（2列）。
    "CREATE UNIQUE INDEX api_keys_client_op_id_unique ON api_keys(lower(client_op_id), id) "
    "WHERE client_op_id IS NOT NULL",
    # predicate が違う（NULL を除外していない＝全行対象）。
    "CREATE UNIQUE INDEX api_keys_client_op_id_unique ON api_keys(lower(client_op_id))",
    # INCLUDE 列付き（indnkeyatts=1 だが indnatts=2＝キー列数だけでは見逃す構造の違い）。
    "CREATE UNIQUE INDEX api_keys_client_op_id_unique ON api_keys(lower(client_op_id)) "
    "INCLUDE (id) WHERE client_op_id IS NOT NULL",
], ids=["non_unique", "composite_key", "wrong_predicate", "include_column"])
def test_migrate_client_op_id_unique_index_unknown_shape_fails_closed_without_drop(bad_ddl):
    """`pg_index` の構造が既知のいずれの形（現行/旧来）とも一致しない場合（非UNIQUE・複合キー・
    INCLUDE 列付き・predicate 違い等）は `"unknown"` に分類され、DROP を一切行わず例外で
    fail-closed する（黙って作り直さない・反転テスト）。"""
    from sherpa.store import db as _db_mod

    _try_init()
    with store._connect() as c:
        c.execute("DROP INDEX IF EXISTS api_keys_client_op_id_unique")
        c.execute(bad_ddl)

    calls: list[str] = []
    with pytest.raises(_db_mod.ClientOpIdIndexUnexpectedDefinitionError):
        with store._connect() as c:
            orig_execute = c.execute

            def _spy_execute(query, *a, **kw):
                calls.append(query)
                return orig_execute(query, *a, **kw)
            c.execute = _spy_execute
            try:
                _db_mod._migrate_client_op_id_unique_index(c)
            finally:
                # 使い回される接続に spy を残さない（性能台帳#17 QW2・上記コメント参照）。
                c.execute = orig_execute

    assert not any("DROP INDEX" in q for q in calls if isinstance(q, str)), (
        "unknown 分類なのに DROP INDEX を実行した（fail-closed 違反）")

    # 後始末: 次のテストに影響しないよう、正しい定義へ戻しておく。
    with store._connect() as c:
        c.execute("DROP INDEX IF EXISTS api_keys_client_op_id_unique")
        c.execute("CREATE UNIQUE INDEX api_keys_client_op_id_unique "
                  "ON api_keys(lower(client_op_id)) WHERE client_op_id IS NOT NULL")


def test_classify_client_op_id_index_same_named_index_on_other_table_is_unknown_fail_closed():
    """`api_keys_client_op_id_unique` という名前の索引が（`api_keys` 側の索引が存在しない状態で）
    まったく無関係な別表に存在している場合、これを "absent"（＝新規作成してよい）扱いにしない
    （反転テスト）——`WHERE` 句にまとめて `indrelid` 条件を混ぜて別表の索引ごと除外してしまうと
    "absent" と区別が付かなくなり、その状態で新規 `CREATE` を試みると Postgres の「同名の
    オブジェクトが既に存在する」という生のエラーで不可解に落ちる。ここでは "unknown" に
    分類させ、fail-closed（DROP しない・専用例外を送出）で明示的に止まることを確認する。"""
    from sherpa.store import db as _db_mod

    _try_init()
    with store._connect() as c:
        c.execute("DROP INDEX IF EXISTS api_keys_client_op_id_unique")
        c.execute("CREATE TABLE IF NOT EXISTS _other_table_for_client_op_id_index_test "
                  "(id BIGSERIAL PRIMARY KEY, client_op_id TEXT)")
        c.execute("CREATE UNIQUE INDEX api_keys_client_op_id_unique "
                  "ON _other_table_for_client_op_id_index_test(lower(client_op_id)) "
                  "WHERE client_op_id IS NOT NULL")

    calls: list[str] = []
    try:
        with pytest.raises(_db_mod.ClientOpIdIndexUnexpectedDefinitionError):
            with store._connect() as c:
                shape = _db_mod._classify_client_op_id_index(c)
                assert shape == "unknown", (
                    f"別表の同名索引を api_keys 自身の索引や absent と誤認した: {shape}")
                orig_execute = c.execute

                def _spy_execute(query, *a, **kw):
                    calls.append(query)
                    return orig_execute(query, *a, **kw)
                c.execute = _spy_execute
                try:
                    _db_mod._migrate_client_op_id_unique_index(c)
                finally:
                    # 使い回される接続に spy を残さない（性能台帳#17 QW2・上記コメント参照）。
                    c.execute = orig_execute

        assert not any("DROP INDEX" in q for q in calls if isinstance(q, str)), (
            "別表の同名索引が存在するのに DROP INDEX を実行した（fail-closed 違反）")
        assert not any("CREATE UNIQUE INDEX" in q for q in calls if isinstance(q, str)), (
            "別表の同名索引が存在するのに新規 CREATE を試みた（fail-closed 違反）")
    finally:
        # 後始末: 次のテストに影響しないよう、別表を消してから正しい定義へ戻す
        # （DROP TABLE は所有するインデックスも道連れに消すため、同名衝突は解消される）。
        with store._connect() as c:
            c.execute("DROP TABLE IF EXISTS _other_table_for_client_op_id_index_test")
            c.execute("DROP INDEX IF EXISTS api_keys_client_op_id_unique")
            c.execute("CREATE UNIQUE INDEX api_keys_client_op_id_unique "
                      "ON api_keys(lower(client_op_id)) WHERE client_op_id IS NOT NULL")
    with store._connect() as c:
        assert _db_mod._classify_client_op_id_index(c) == "current"


def test_classify_client_op_id_index_correct_with_non_default_search_path():
    """`search_path` の先頭に（`api_keys` を含まない）別スキーマがあっても、正しい現行定義を
    見落とさない——`current_schema()`（search_path の先頭）に頼る実装だと、この状況で
    `api_keys` の実際のスキーマと一致せず誤判定しうる。`'api_keys'::regclass` は search_path
    全体から解決されるため、先頭に空スキーマがあっても正しく `api_keys` を指す。"""
    from sherpa.store import db as _db_mod

    _try_init()
    with store._connect() as c:
        assert _db_mod._classify_client_op_id_index(c) == "current"   # 前提を確認しておく。
        c.execute("CREATE SCHEMA IF NOT EXISTS _empty_schema_for_search_path_test")
        try:
            c.execute("SET LOCAL search_path TO _empty_schema_for_search_path_test, public")
            shape = _db_mod._classify_client_op_id_index(c)
            assert shape == "current", (
                f"search_path の先頭に空スキーマがあると誤判定した: {shape}")
        finally:
            c.execute("RESET search_path")
            c.execute("DROP SCHEMA IF EXISTS _empty_schema_for_search_path_test CASCADE")


def test_classify_client_op_id_index_ignores_decoy_in_leading_schema_when_current():
    """`api_keys` の実際のスキーマより **前** に search_path 上で見える別スキーマに、無関係な
    別表への同名索引（デコイ）があっても、正しく `"current"` と判定する。

    `_classify_client_op_id_index` の探索は `_resolve_api_keys_schema()` で解決した
    `api_keys` 自身のスキーマへ限定してから名前で照合する（namespace 条件が無いと
    `fetchone()` に `ORDER BY` が無いため、複数スキーマに同名索引が存在する状況でどちらの行が
    返るかは不定になる）。分類クエリが実際に `ns.nspname = %s` を解決済みスキーマでバインド
    していることを、`c.execute` の呼び出し引数を spy して直接固定する——挙動（正しい判定結果）
    だけでなく、それを実現している SQL 側の仕組み（namespace 限定）自体も back-door で
    壊れていないことを担保する。

    セットアップ（デコイ作成・search_path 切替）〜検証〜後始末（デコイ削除）は単一トランザク
    ションで行う——途中の assert が落ちても `with` の例外終了で全体が rollback され、デコイも
    search_path の変更も残らない。"""
    from sherpa.store import db as _db_mod

    _try_init()
    with store._connect() as c:
        assert _db_mod._classify_client_op_id_index(c) == "current"   # 前提を確認しておく。
        target_schema = _db_mod._resolve_api_keys_schema(c)
        c.execute("CREATE SCHEMA IF NOT EXISTS _decoy_schema_for_client_op_id_test")
        c.execute("CREATE TABLE _decoy_schema_for_client_op_id_test._decoy_table "
                  "(id BIGSERIAL PRIMARY KEY, client_op_id TEXT)")
        c.execute("CREATE UNIQUE INDEX api_keys_client_op_id_unique "
                  "ON _decoy_schema_for_client_op_id_test._decoy_table(lower(client_op_id)) "
                  "WHERE client_op_id IS NOT NULL")
        c.execute("SET LOCAL search_path TO _decoy_schema_for_client_op_id_test, public")

        calls: list[tuple[str, tuple]] = []
        orig_execute = c.execute

        def _spy_execute(query, *a, **kw):
            calls.append((query, a))
            return orig_execute(query, *a, **kw)
        c.execute = _spy_execute
        try:
            shape = _db_mod._classify_client_op_id_index(c)
        finally:
            # 使い回される接続に spy を残さない（性能台帳#17 QW2・RV代替 L2・上記コメント参照）。
            c.execute = orig_execute
        assert shape == "current", (
            f"先行スキーマの同名デコイ索引につられて誤判定した: {shape}")

        ns_calls = [(q, a) for q, a in calls if "ns.nspname = %s" in q]
        assert ns_calls, "分類クエリに ns.nspname = %s の namespace 限定が見当たらない"
        for q, a in ns_calls:
            assert a and a[0] == (target_schema,), (
                f"ns.nspname の bind 値が解決済みスキーマと一致しない: {a}")

        c.execute("RESET search_path")
        c.execute("DROP SCHEMA IF EXISTS _decoy_schema_for_client_op_id_test CASCADE")


def test_classify_client_op_id_index_ignores_decoy_in_leading_schema_when_legacy():
    """上記の "current" 版と対の "legacy" 版。先行スキーマの同名デコイに惑わされず `"legacy"`
    と正しく判定し、かつ移行（`_migrate_client_op_id_unique_index`）が実際に `DROP INDEX`
    するのが `api_keys` 自身のスキーマの索引であって、デコイ側ではないことまで確認する——
    `DROP INDEX api_keys_client_op_id_unique`（非修飾）のままだと、search_path の先頭に
    デコイのスキーマがある間はそちらを誤って DROP しかねないため、スキーマ修飾した
    `DROP INDEX` が効いていることの検証を兼ねる。

    legacy への張替え・デコイ作成・search_path 切替・分類・移行・検証・デコイ削除を単一
    トランザクションにまとめる——`with` の外へ分割すると、途中のブロックで assert が落ちた
    場合にそのブロック自身の変更だけ rollback され、別のブロックで既に commit 済みの状態変更
    （legacy への張替え等）が残ってしまい、以後のテスト（"current" を前提にするもの）を巻き
    添えにしうる。単一トランザクションなら途中で失敗しても `with` の例外終了で全体が rollback
    され、legacy への張替えごと痕跡なく消える（成功時はデコイ削除ごと commit されて "current"
    のまま残る）。"""
    from sherpa.store import db as _db_mod

    _try_init()
    sfx = _sfx()
    with store._connect() as c:
        _reset_client_op_id_index_to_legacy(c, sfx, rows=False)
        c.execute("CREATE SCHEMA IF NOT EXISTS _decoy_schema_for_client_op_id_test2")
        c.execute("CREATE TABLE _decoy_schema_for_client_op_id_test2._decoy_table "
                  "(id BIGSERIAL PRIMARY KEY, client_op_id TEXT)")
        c.execute("CREATE UNIQUE INDEX api_keys_client_op_id_unique "
                  "ON _decoy_schema_for_client_op_id_test2._decoy_table(lower(client_op_id)) "
                  "WHERE client_op_id IS NOT NULL")
        c.execute("SET LOCAL search_path TO _decoy_schema_for_client_op_id_test2, public")

        shape = _db_mod._classify_client_op_id_index(c)
        assert shape == "legacy", (
            f"先行スキーマの同名デコイ索引につられて誤判定した: {shape}")
        _db_mod._migrate_client_op_id_unique_index(c)
        # デコイは無傷のまま（別スキーマの別表の索引は移行対象ではない）。
        decoy_still_there = c.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname='_decoy_schema_for_client_op_id_test2' "
            "AND indexname='api_keys_client_op_id_unique'"
        ).fetchone()
        assert decoy_still_there is not None, (
            "スキーマ修飾しない DROP INDEX がデコイ側を巻き添えにした疑い")
        # api_keys 自身は正しく現行定義へ移行済み。
        assert _db_mod._classify_client_op_id_index(c) == "current"

        c.execute("RESET search_path")
        c.execute("DROP SCHEMA IF EXISTS _decoy_schema_for_client_op_id_test2 CASCADE")


def test_client_op_id_case_insensitive_conflict_and_recovery_match():
    """`client_op_id` の大小文字表記が異なっても同じ UUID とみなす。`insert_api_key` は保存前に
    標準小文字正準形へ正規化する。DB の `lower()` 部分一意インデックスは、正規化を経由しない
    直接 SQL（大小文字違いの2行目）も独立した最後の砦として拒否する。回復時の照合（`lower()`）
    も、発行時と異なる大小文字表記の `client_op_id` で一致する。"""
    _try_init()
    sfx = _sfx()
    op_lower = str(uuid.uuid4())
    op_upper = op_lower.upper()

    row1 = store.insert_api_key(f"hash-ci1-{sfx}", f"pfxci1{sfx}"[:12], "ci1", "admin",
                                client_op_id=op_upper)
    assert row1["client_op_id"] == op_lower   # 保存前に正準小文字形へ正規化済み

    # router 正規化を経由しない直接 SQL で「別の大小文字表記」の同じ UUID を書き込もうとすると、
    # DB の lower() 部分一意 index 自体が拒否する（アプリ層の正規化に頼らない最後の砦）。
    with store._connect() as c:
        with pytest.raises(Exception) as exc_info:
            c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, client_op_id) "
                "VALUES (%s,%s,%s,%s,%s)",
                (f"hash-ci2-{sfx}", f"pfxci2{sfx}"[:12], "ci2", "admin", op_upper))
    assert ("api_keys_client_op_id_unique" in str(exc_info.value)
           or "duplicate" in str(exc_info.value).lower())

    # 回復時の照合も大小文字を区別しない: 保存値は小文字だが、大文字で渡しても一致する。
    result = store.revoke_unconfirmed_key_by_client_op_id(op_upper, "system", created_by="admin")
    assert result is not None
    assert result["id"] == row1["id"]
