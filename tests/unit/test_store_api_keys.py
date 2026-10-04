"""api_keys（外部連携 API キー・sherpa/store/api_keys.py）と init_schema の接続予算の unit テスト。
DB 接続を要する（DB 到達不能なら `pytest.skip`）。
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from sherpa import auth, store
from sherpa.store import db as _db_mod


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


# スキーマ/デコイ名はプロセスごとに一意にする（共有テスト DB で並走する別プロセスと衝突しない）。
_SFX = _sfx()


def _try_init() -> None:
    """接続プローブ（`SELECT 1`）だけを「DB 到達不能」の skip 対象にし、`init_schema()` 本体の
    失敗は skip せず落とす（migration のバグを「DB down」として隠さない）。"""
    try:
        with store._connect(connect_timeout=5) as probe_conn:
            probe_conn.execute("SELECT 1")
    except psycopg.OperationalError as e:
        pytest.skip(f"DB down: {e}")
    store.init_schema()


def _ins(sfx, tag, created_by="admin", **kw):
    return store.insert_api_key(f"hash-{tag}-{sfx}", f"pfx{tag}{sfx}"[:12], f"{tag}-{sfx}",
                                created_by, **kw)


def _listed(key_id, **kw):
    return next(r for r in store.list_api_keys(**kw) if r["id"] == key_id)


def _make_user(uid, status="active"):
    store.upsert_user(uid, email=f"{uid}@t.local", display_name=uid,
                      password_hash=auth.hash_password("Passw0rd!"), role="user", status=status)


@contextmanager
def _user_keys_allowed(**extra):
    """自己発行トグルを ON にし、終了時に未設定へ戻す。"""
    store.set_system_settings("admin", {"user_api_keys_allowed": True, **extra})
    try:
        yield
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None,
                                            **{k: None for k in extra}})


def _spy_psycopg_connect(monkeypatch):
    seen: list = []
    original = _db_mod.psycopg.connect

    def _spy(dsn, **kw):
        if "connect_timeout" in kw:
            seen.append(kw["connect_timeout"])
        return original(dsn, **kw)
    monkeypatch.setattr(_db_mod.psycopg, "connect", _spy)
    return seen


# ===== 発行・照会・失効 =====

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

    row = store.api_key_by_hash(key_hash)
    assert row["id"] == key_id
    assert row["key_hash"] == key_hash
    assert row["revoked_at"] is None

    found = _listed(key_id)
    assert found["label"] == label
    assert found["last_used_at"] is None

    store.touch_api_key(key_id)
    assert _listed(key_id)["last_used_at"] is not None

    revoked = store.revoke_api_key(key_id, "admin")
    assert revoked["revoked_at"] is not None
    assert revoked["revoked_by"] == "admin"

    # 冪等: 二重失効は revoked_at/revoked_by を変えない。
    again = store.revoke_api_key(key_id, "someone-else")
    assert again["revoked_at"] == revoked["revoked_at"]
    assert again["revoked_by"] == "admin"

    # 失効済みでも by_hash は返す（呼び出し側が revoked_at を見て 401 にする）。
    assert store.api_key_by_hash(key_hash)["revoked_at"] is not None


def test_revoke_api_key_unknown_id_returns_none():
    _try_init()
    assert store.revoke_api_key(-1, "admin") is None


def test_optional_columns_round_trip_none_and_set():
    """allowed_worlds／expires_at／daily_quota／owner_uid は None（既定）と指定値の両方を保持する。"""
    _try_init()
    sfx = _sfx()

    unset = _ins(sfx, "pu")
    assert unset["allowed_worlds"] is None
    assert unset["expires_at"] is None
    assert unset["daily_quota"] is None
    assert unset["owner_uid"] is None
    assert store.api_key_by_hash(f"hash-pu-{sfx}")["allowed_worlds"] is None

    exp = datetime(2099, 1, 1, tzinfo=timezone.utc)
    scoped = _ins(sfx, "ps", allowed_worlds=["v1", "v2"], expires_at=exp, daily_quota=10)
    assert scoped["allowed_worlds"] == ["v1", "v2"]
    assert scoped["expires_at"] == exp
    assert scoped["daily_quota"] == 10
    row = store.api_key_by_hash(f"hash-ps-{sfx}")
    assert row["allowed_worlds"] == ["v1", "v2"]
    assert row["expires_at"] == exp
    assert row["daily_quota"] == 10
    assert _listed(scoped["id"])["allowed_worlds"] == ["v1", "v2"]


# ===== 自己発行キー（owner_uid） =====

def test_self_issued_key_requires_user_api_keys_allowed():
    """owner_uid 付きの発行は user_api_keys_allowed を同一トランザクションで再確認する。"""
    _try_init()
    sfx = _sfx()
    store.set_system_settings("admin", {"user_api_keys_allowed": False})
    with pytest.raises(store.UserApiKeysDisallowedError):
        _ins(sfx, "dis", f"user-{sfx}", owner_uid=f"user-{sfx}")

    with _user_keys_allowed():
        row = _ins(sfx, "ok", f"user-{sfx}", owner_uid=f"user-{sfx}")
        assert row["owner_uid"] == f"user-{sfx}"

    # admin 発行（owner_uid=None）はトグルと無関係に発行できる。
    assert _ins(sfx, "adm")["owner_uid"] is None


def test_list_and_revoke_api_keys_owner_scoped():
    """`owner_uid` 指定は一覧/失効を本人のキーだけへ絞る（他人/admin 発行キーは対象外）。"""
    _try_init()
    sfx = _sfx()
    uid = f"owner-{sfx}"
    with _user_keys_allowed():
        mine = _ins(sfx, "mine", uid, owner_uid=uid)
        others = _ins(sfx, "oth", f"other-{sfx}", owner_uid=f"other-{sfx}")
        admin_key = _ins(sfx, "adm2")

        my_ids = {r["id"] for r in store.list_api_keys(owner_uid=uid)}
        assert mine["id"] in my_ids
        assert others["id"] not in my_ids
        assert admin_key["id"] not in my_ids

        assert store.revoke_api_key(others["id"], uid, owner_uid=uid) is None
        assert store.revoke_api_key(admin_key["id"], uid, owner_uid=uid) is None
        revoked = store.revoke_api_key(mine["id"], uid, owner_uid=uid)
        assert revoked["id"] == mine["id"]


def test_revoke_self_issued_api_keys_purges_only_owned_active_keys():
    """トグル OFF 時の一括失効は owner_uid 非 NULL の未失効キーだけが対象（admin 発行は対象外）。"""
    _try_init()
    sfx = _sfx()
    uid = f"purge-{sfx}"
    with _user_keys_allowed():
        self_issued = _ins(sfx, "pg", uid, owner_uid=uid)
        admin_key = _ins(sfx, "pg2")

        store.revoke_self_issued_api_keys(actor="admin")

        assert _listed(self_issued["id"])["revoked_at"] is not None
        assert _listed(admin_key["id"])["revoked_at"] is None
        assert store.revoke_self_issued_api_keys(actor="admin") == 0   # 冪等


def test_count_self_issued_active_api_keys_excludes_revoked_and_expired():
    _try_init()
    sfx = _sfx()
    uid = f"cnt-{sfx}"
    with _user_keys_allowed():
        before = store.count_self_issued_active_api_keys()
        row = _ins(sfx, "cnt", uid, owner_uid=uid)
        assert store.count_self_issued_active_api_keys() == before + 1

        past = datetime.now(timezone.utc) - timedelta(days=1)
        expired = _ins(sfx, "cntexp", uid, owner_uid=uid, expires_at=past)
        assert expired["id"]   # 発行は成功する（期限切れは認証時に拒否される）
        assert store.count_self_issued_active_api_keys() == before + 1

        store.revoke_api_key(row["id"], uid, owner_uid=uid)
        assert store.count_self_issued_active_api_keys() == before


def test_owner_status_joined_for_self_issued_keys_only():
    """`api_key_by_hash` は自己発行キーにだけ所有者の `users.status` を同梱する。"""
    _try_init()
    sfx = _sfx()
    uid = f"ownstat-{sfx}"
    _make_user(uid)
    try:
        with _user_keys_allowed():
            _ins(sfx, "os1", uid, owner_uid=uid)
            assert store.api_key_by_hash(f"hash-os1-{sfx}")["owner_status"] == "active"

            store.upsert_user(uid, role="user", status="disabled")
            assert store.api_key_by_hash(f"hash-os1-{sfx}")["owner_status"] == "disabled"

            _ins(sfx, "os2")
            assert store.api_key_by_hash(f"hash-os2-{sfx}")["owner_status"] is None
    finally:
        store.upsert_user(uid, role="user", status="active")


# ===== Webhook 通知先 =====

def test_list_webhook_keys_for_world_excludes_expired_and_inactive_owner():
    """失効・`webhook_url` 無し・期限切れ・所有者非 active のキーは対象外（admin 発行は所有者チェック外）。"""
    _try_init()
    sfx = _sfx()
    world = f"whworld-{sfx}"
    uid = f"whowner-{sfx}"
    _make_user(uid)
    try:
        with _user_keys_allowed():
            admin_key = _ins(sfx, "whadm", allowed_worlds=[world],
                             webhook_url="https://wh-admin.example/hook", webhook_secret="s-admin")
            active_self = _ins(sfx, "whact", uid, owner_uid=uid, allowed_worlds=[world],
                               webhook_url="https://wh-active.example/hook", webhook_secret="s-active")
            expired = _ins(sfx, "whexp", allowed_worlds=[world],
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                           webhook_url="https://wh-expired.example/hook", webhook_secret="s-expired")
            no_webhook = _ins(sfx, "whnone", allowed_worlds=[world])

            got = {row["id"] for row in store.list_webhook_keys_for_world(world)}
            assert admin_key["id"] in got
            assert active_self["id"] in got
            assert expired["id"] not in got
            assert no_webhook["id"] not in got

            store.upsert_user(uid, role="user", status="disabled")
            got = {row["id"] for row in store.list_webhook_keys_for_world(world)}
            assert active_self["id"] not in got
            assert admin_key["id"] in got
    finally:
        store.upsert_user(uid, role="user", status="active")


def test_get_api_key_webhook_none_for_revoked_cleared_or_inactive_owner():
    """通知先の解決は、失効・自己発行キーの所有者非 active で None（認証と同じ規則）。"""
    _try_init()
    sfx = _sfx()
    uid = f"whgowner-{sfx}"
    _make_user(uid)
    try:
        with _user_keys_allowed():
            k = _ins(sfx, "whg", uid, owner_uid=uid,
                     webhook_url="https://wh.example/hook", webhook_secret="s")
            assert store.get_api_key_webhook(k["id"])["webhook_url"] == "https://wh.example/hook"
            store.upsert_user(uid, role="user", status="disabled")
            assert store.get_api_key_webhook(k["id"]) is None
            store.upsert_user(uid, role="user", status="active")
            assert store.get_api_key_webhook(k["id"]) is not None
            store.revoke_api_key(k["id"], "admin")
            assert store.get_api_key_webhook(k["id"]) is None
    finally:
        store.upsert_user(uid, role="user", status="active")


def test_list_webhook_keys_for_world_scopes_by_allowed_worlds():
    """`allowed_worlds` が None（全 world）または対象 world を含む場合のみ対象になる。"""
    _try_init()
    sfx = _sfx()
    world = f"whscope-{sfx}"
    hook = {"webhook_url": "https://wh.example/hook", "webhook_secret": "s"}
    unscoped = _ins(sfx, "whun", **hook)
    scoped_in = _ins(sfx, "whin", allowed_worlds=[world], **hook)
    scoped_out = _ins(sfx, "whout", allowed_worlds=[f"whother-{sfx}"], **hook)

    got = {row["id"] for row in store.list_webhook_keys_for_world(world)}
    assert unscoped["id"] in got
    assert scoped_in["id"] in got
    assert scoped_out["id"] not in got


# ===== 設定トグルと一括失効 =====

def test_apply_system_settings_and_revoke_if_disabled_is_atomic_and_covers_explicit_null():
    """OFF（明示 false・明示 null とも）で設定変更と一括失効が同一トランザクションで行われ、
    再度 ON にしても失効済みキーは復活しない。"""
    _try_init()
    sfx = _sfx()
    uid_false = f"atmf-{sfx}"
    uid_null = f"atmn-{sfx}"
    store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": True})
    try:
        key_false = _ins(sfx, "atmf", uid_false, owner_uid=uid_false)
        key_null = _ins(sfx, "atmn", uid_null, owner_uid=uid_null)

        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": False})
        assert _listed(key_false["id"])["revoked_at"] is not None

        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": True})
        assert _listed(key_false["id"])["revoked_at"] is not None   # 復活しない

        # 明示 null（未設定＝実効 false）も一括失効の対象。ON であることは insert の成功で確認する
        # （get_system_settings は unit conftest で固定 dict に差し替わっている）。
        key_null_2 = _ins(sfx, "atmn2", uid_null, owner_uid=uid_null)
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": None})
        with pytest.raises(store.UserApiKeysDisallowedError):
            _ins(sfx, "atmn3", uid_null, owner_uid=uid_null)
        assert _listed(key_null["id"])["revoked_at"] is not None
        assert _listed(key_null_2["id"])["revoked_at"] is not None
    finally:
        store.set_system_settings("admin", {"user_api_keys_allowed": None})


def test_apply_system_settings_and_revoke_if_disabled_rolls_back_on_audit_failure(monkeypatch):
    """監査 INSERT が失敗すると設定変更・一括失効の両方がロールバックされる（fail-closed）。"""
    _try_init()
    sfx = _sfx()
    uid = f"rbk-{sfx}"
    store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": True})
    key = _ins(sfx, "rbk", uid, owner_uid=uid)
    try:
        def _boom(*a, **kw):
            raise RuntimeError("simulated audit failure")

        monkeypatch.setattr(store, "_audit_insert", _boom)
        with pytest.raises(RuntimeError):
            store.apply_system_settings_and_revoke_if_disabled(
                "admin", {"user_api_keys_allowed": False})
        monkeypatch.undo()

        # 設定は OFF になっておらず（次の insert が成功する）、キーも失効していない。
        still_on = _ins(sfx, "rbk2", uid, owner_uid=uid)
        assert _listed(still_on["id"])["revoked_at"] is None
        assert store.api_key_by_hash(f"hash-rbk-{sfx}")["revoked_at"] is None, (
            "監査失敗時は一括失効もロールバックされるはず")
        assert key["id"]
    finally:
        store.apply_system_settings_and_revoke_if_disabled("admin", {"user_api_keys_allowed": None})


# ===== 日次クォータ =====

def test_self_issued_quota_reread_at_write_time_not_stale_caller_value():
    """TOCTOU 対策: `insert_api_key` は DB の現在の上限を再読して確定する。呼び出し側が古い大きな
    値を渡しても、現在の上限を超えていれば拒否され、未指定なら現在の上限が既定になる。"""
    _try_init()
    sfx = _sfx()
    uid = f"toctou-{sfx}"
    with _user_keys_allowed(user_api_keys_daily_quota_default=100):
        # 呼び出し側は上限 100 のつもりだが、書込み直前に admin が 5 へ引き下げた。
        store.set_system_settings("admin", {"user_api_keys_daily_quota_default": 5})
        with pytest.raises(store.SelfIssuedQuotaExceededError):
            _ins(sfx, "toctou", uid, daily_quota=100, owner_uid=uid)

        assert _ins(sfx, "toctouok", uid, daily_quota=5, owner_uid=uid)["daily_quota"] == 5
        assert _ins(sfx, "toctoudef", uid, owner_uid=uid)["daily_quota"] == 5


def test_self_issued_quota_is_non_retroactive():
    """既定/上限を引き下げても発行済みキーの `daily_quota` は変わらない。"""
    _try_init()
    sfx = _sfx()
    uid = f"retro-{sfx}"
    with _user_keys_allowed(user_api_keys_daily_quota_default=100):
        assert _ins(sfx, "retro", uid, owner_uid=uid)["daily_quota"] == 100
        store.set_system_settings("admin", {"user_api_keys_daily_quota_default": 5})
        assert store.api_key_by_hash(f"hash-retro-{sfx}")["daily_quota"] == 100


def test_count_ext_api_calls_by_key_separation_and_window():
    """指定した key_ids だけを集計し（空/None は空 dict）、直近 30 日より前は除外する。
    監査行は不変のため、基準時刻（`now=`）を未来へずらして窓の外を再現する。"""
    _try_init()
    sfx = _sfx()
    key_a = _ins(sfx, "cca")
    key_b = _ins(sfx, "ccb")
    for k in (key_a, key_a, key_b):
        store.audit(f"ext:{k['id']}", "ext_api.search", "ext_search", None, detail={})

    only_a = store.count_ext_api_calls_by_key([key_a["id"]])
    assert only_a.get(key_a["id"]) == 2
    assert key_b["id"] not in only_a

    both = store.count_ext_api_calls_by_key([key_a["id"], key_b["id"]])
    assert both[key_a["id"]] == 2
    assert both[key_b["id"]] == 1

    assert store.count_ext_api_calls_by_key([]) == {}
    assert store.count_ext_api_calls_by_key(None) == {}

    far_future = datetime.now(timezone.utc) + timedelta(days=31)
    assert store.count_ext_api_calls_by_key([key_a["id"]], now=far_future).get(key_a["id"], 0) == 0


# ===== 並行ロック（_USER_KEY_LOCK） =====

def _poll_blocking(stop, on_rows, state):
    """別接続で pg_blocking_pids をポーリングし on_rows(rows) へ渡す。失敗は state に残す。"""
    try:
        with store._connect() as c:
            while not stop.is_set():
                rows = c.execute(
                    "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                    "WHERE pid <> pg_backend_pid() AND pg_blocking_pids(pid) != '{}'"
                ).fetchall()
                state["polls"] += 1
                on_rows(rows)
                time.sleep(0.02)
    except Exception as e:
        state["errors"].append(repr(e))


def test_no_deadlock_between_settings_toggle_and_standalone_revoke_concurrently():
    """設定トグルと単体の一括失効を並行実行してもデッドロックせず、`pg_blocking_pids()` で
    相互待ち（サイクル）も観測されない（両経路は `_USER_KEY_LOCK`→`_AUDIT_CHAIN_LOCK` の順）。"""
    _try_init()
    sfx = _sfx()
    uid = f"race-{sfx}"
    iterations = 25
    errors: list[tuple[str, int, str]] = []
    cycle_observed: list[tuple[int, int]] = []
    state = {"polls": 0, "errors": []}
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
                continue   # トグルが一時的に OFF＝想定内
            except Exception as e:
                errors.append(("b-insert", i, repr(e)))
                continue
            try:
                store.revoke_self_issued_api_keys(actor="admin")
            except Exception as e:
                errors.append(("b-revoke", i, repr(e)))

    def on_rows(rows):
        blocked_by = {r["pid"]: set(r["blockers"]) for r in rows}
        for pid, blockers in blocked_by.items():
            for b in blockers:
                if pid in blocked_by.get(b, set()):
                    cycle_observed.append((pid, b))

    t_mon = threading.Thread(target=_poll_blocking, args=(stop, on_rows, state), daemon=True)
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
        assert not state["errors"], f"監視スレッドで例外が発生した（監視が機能していない）: {state['errors']}"
        assert state["polls"] > 0, "監視スレッドが一度もポーリングできなかった（監視が機能していない）"
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
    """admin の設定更新トランザクションが `_USER_KEY_LOCK` を取ることを、実際のロック競合を
    `pg_blocking_pids()` で観測して確認する。待ち手の同定は文言一致ではなく、admin 更新スレッドが
    受け取った接続の backend pid（`settings._connect` をそのスレッド中だけラップして記録）の一致で行う
    （共有 DB の無関係なセッションを証拠に拾わない）。"""
    from sherpa.store import settings as _settings_mod
    from sherpa.store.api_keys import _USER_KEY_LOCK

    _try_init()
    holder_pid: dict = {}
    admin_pid: dict = {}
    holder_ready = threading.Event()
    release = threading.Event()
    samples: list[tuple[int, list]] = []
    state = {"polls": 0, "errors": []}
    stop = threading.Event()
    admin_thread_name = "lock-evidence-admin-thread"

    def holder():
        with store._connect() as c:
            holder_pid["pid"] = c.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_USER_KEY_LOCK,))
            holder_ready.set()
            release.wait(timeout=10)

    t_holder = threading.Thread(target=holder)
    t_holder.start()
    assert holder_ready.wait(timeout=5), "ロック保持スレッドの準備がタイムアウトした"

    original_settings_connect = _settings_mod._connect

    def _pid_capturing_connect(*a, **kw):
        conn = original_settings_connect(*a, **kw)
        if threading.current_thread().name == admin_thread_name and "pid" not in admin_pid:
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

    def on_rows(rows):
        for r in rows:
            if holder_pid["pid"] in r["blockers"]:
                samples.append((r["pid"], list(r["blockers"])))

    t_admin = threading.Thread(target=admin_update, name=admin_thread_name)
    t_mon = threading.Thread(target=_poll_blocking, args=(stop, on_rows, state))
    t_mon.start()
    _settings_mod._connect = _pid_capturing_connect
    try:
        t_admin.start()
        # DB 負荷時でも観測が間に合う長さの窓（短いと偽陰性）。
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
        assert not state["errors"], f"監視スレッドで例外が発生した: {state['errors']}"
        assert state["polls"] > 0, "監視スレッドが一度もポーリングできなかった（監視が機能していない）"
        assert result.get("ok"), f"admin 更新が失敗した: {result}"
        assert admin_pid.get("pid") is not None, (
            f"admin 更新スレッド自身の backend pid を捕捉できなかった: {admin_pid}")
        matching = [s for s in samples if s[0] == admin_pid["pid"]]
        assert matching, (f"admin の更新（{updates}）自身の接続（pid={admin_pid.get('pid')}）が"
                          f"_USER_KEY_LOCK で実際にブロックされる様子を観測できなかった"
                          f"（全サンプル: {samples}）")
        assert holder_pid["pid"] in matching[0][1]
    finally:
        store.apply_system_settings_and_revoke_if_disabled("admin", {k: None for k in updates})


# ===== client_op_id =====

def test_client_op_id_unique_constraint_and_scoped_recovery_prevents_cross_owner_effects():
    """`client_op_id` は非 NULL に限り一意（衝突は ClientOpIdConflictError）。NULL は何件でも衝突しない。
    回復用の `revoke_unconfirmed_key_by_client_op_id` は所有条件を同一 SQL の WHERE で照合するため、
    別の所有者 B が A の client_op_id で回復を試みても A のキーは無傷（反転テスト）。"""
    _try_init()
    sfx = _sfx()
    uid_a = f"cop-a-{sfx}"
    uid_b = f"cop-b-{sfx}"
    shared_cop = str(uuid.uuid4())
    with _user_keys_allowed():
        _ins(sfx, "copA", uid_a, owner_uid=uid_a, client_op_id=shared_cop)
        with pytest.raises(store.ClientOpIdConflictError):
            _ins(sfx, "copB", uid_b, owner_uid=uid_b, client_op_id=shared_cop)

        assert store.revoke_unconfirmed_key_by_client_op_id(
            shared_cop, "system", owner_uid=uid_b) is None
        key_a = next(r for r in store.list_api_keys(owner_uid=uid_a)
                     if r["client_op_id"] == shared_cop)
        assert key_a["revoked_at"] is None

        result_for_a = store.revoke_unconfirmed_key_by_client_op_id(
            shared_cop, "system", owner_uid=uid_a)
        assert result_for_a["id"] == key_a["id"]

    assert _ins(sfx, "nullcop1")["client_op_id"] is None
    assert _ins(sfx, "nullcop2")["client_op_id"] is None


def test_client_op_id_case_insensitive_conflict_and_recovery_match():
    """大小文字違いの client_op_id は同じ UUID とみなす。保存前に小文字へ正規化され、正規化を
    経由しない直接 SQL も `lower()` 部分一意索引が拒否し、回復時の照合も大小文字を区別しない。"""
    _try_init()
    sfx = _sfx()
    op_lower = str(uuid.uuid4())
    op_upper = op_lower.upper()

    row1 = _ins(sfx, "ci1", client_op_id=op_upper)
    assert row1["client_op_id"] == op_lower

    with store._connect() as c:
        with pytest.raises(Exception) as exc_info:
            c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, client_op_id) "
                "VALUES (%s,%s,%s,%s,%s)",
                (f"hash-ci2-{sfx}", f"pfxci2{sfx}"[:12], "ci2", "admin", op_upper))
    assert ("api_keys_client_op_id_unique" in str(exc_info.value)
            or "duplicate" in str(exc_info.value).lower())

    result = store.revoke_unconfirmed_key_by_client_op_id(op_upper, "system", created_by="admin")
    assert result["id"] == row1["id"]


# ===== init_schema の接続予算 =====

def test_init_schema_forwards_connect_timeout_to_both_connections(monkeypatch):
    """未初期化状態で `init_schema(connect_timeout=...)` が lock 取得用・DDL 実行用の両方の接続へ、
    整数秒へ切り上げ・最小 1 秒でクランプした値を渡す。省略時は `_INIT_CONNECT_TIMEOUT` のまま。"""
    _try_init()
    saved_inited = _db_mod._inited
    try:
        seen = _spy_psycopg_connect(monkeypatch)
        _db_mod._inited = False
        _db_mod.init_schema(connect_timeout=0.3)
        assert _db_mod._inited is True
        assert len(seen) >= 2, seen
        assert all(ct == 1 for ct in seen), seen

        seen.clear()
        _db_mod._inited = False
        _db_mod.init_schema()
        assert len(seen) >= 2, seen
        assert all(ct == _db_mod._INIT_CONNECT_TIMEOUT for ct in seen), seen
    finally:
        _db_mod._inited = saved_inited


def test_init_schema_deducts_lock_wait_elapsed_from_ddl_connect_timeout(monkeypatch):
    """lock 接続の確立＋`pg_advisory_lock` 待ちで経過した分を DDL 用接続の `connect_timeout` から
    差し引く（満額を 2 回使うと実時間が約 2 倍に伸びる）。"""
    _try_init()
    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        return 100.0 if calls["n"] <= 1 else 103.0   # 2 回目以降は 3 秒経過

    saved_inited = _db_mod._inited
    try:
        seen = _spy_psycopg_connect(monkeypatch)
        monkeypatch.setattr(_db_mod.time, "monotonic", _clock)
        _db_mod._inited = False
        _db_mod.init_schema(connect_timeout=10)
        assert _db_mod._inited is True
    finally:
        _db_mod._inited = saved_inited
    assert len(seen) >= 2, seen
    assert seen[0] == 10, seen   # lock 用: 満額
    assert seen[1] == 7, seen    # DDL 用: 10-3


def test_init_schema_raises_without_ddl_connect_when_lock_wait_exhausts_budget(monkeypatch):
    """lock 待ちだけで予算を使い切ったら、DDL 用の新規接続を開始せず `TimeoutError`（lock は
    `finally` で解放され残らない）。"""
    _try_init()
    connect_calls = {"n": 0}
    original_psycopg_connect = _db_mod.psycopg.connect

    def _spy_connect(dsn, **kw):
        connect_calls["n"] += 1
        return original_psycopg_connect(dsn, **kw)

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        return 100.0 if calls["n"] <= 1 else 110.0   # connect_timeout=5 を超える

    saved_inited = _db_mod._inited
    try:
        _db_mod._inited = False
        monkeypatch.setattr(_db_mod.psycopg, "connect", _spy_connect)
        monkeypatch.setattr(_db_mod.time, "monotonic", _clock)
        with pytest.raises(TimeoutError):
            _db_mod.init_schema(connect_timeout=5)
        assert _db_mod._inited is False
    finally:
        _db_mod._inited = saved_inited
    assert connect_calls["n"] == 1, "DDL 用の 2 回目の接続を試みてはいけない"
    with _db_mod._connect() as c:
        key = int.from_bytes(_db_mod.hashlib.sha1(
            f"schema:{_db_mod._KB_ID}".encode("utf-8")).digest()[:8], "big", signed=True)
        assert c.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()["ok"] is True, (
            "advisory lock が解放されずに残っている")
        c.execute("SELECT pg_advisory_unlock(%s)", (key,))
