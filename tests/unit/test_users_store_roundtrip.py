"""sherpa/store/users.py の unit テスト（フェーズ7 S6・25%→引き上げ）。

create_user/upsert_user/get_user*/suggest_users/session ライフサイクルを実 DB（非破壊）で round-trip する。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from sherpa import store


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def test_create_user_then_conflict_is_noop_does_not_overwrite():
    _try_init()
    uid = f"unit-user-{_sfx()}"
    created = store.create_user(uid, email=f"{uid}@example.test", display_name="First",
                                password_hash="ph1", role="user", status="active")
    assert created is not None and created["uid"] == uid and created["display_name"] == "First"

    # 既存 uid に対する create_user は何もしない（None）＝黙って上書きしない（事故防止・RV MEDIUM）。
    again = store.create_user(uid, email=f"{uid}@example.test", display_name="Second",
                              password_hash="ph2", role="admin", status="active")
    assert again is None
    fetched = store.get_user(uid)
    assert fetched["display_name"] == "First" and fetched["role"] == "user"   # 上書きされていない


def test_upsert_user_creates_then_updates_with_coalesce_semantics():
    _try_init()
    uid = f"unit-user-{_sfx()}"
    created = store.upsert_user(uid, email=f"{uid}@example.test", display_name="Name1",
                                password_hash="ph1", role="user", status="active")
    assert created["display_name"] == "Name1" and created["must_change_password"] is False

    # None を渡した項目は既存値を維持（COALESCE）。role/status は明示上書き。
    updated = store.upsert_user(uid, email=None, display_name=None, password_hash=None,
                                role="admin", status="active")
    assert updated["display_name"] == "Name1"      # 維持
    assert updated["role"] == "admin"               # 上書き

    by_uid = store.get_user_by_uid(uid)
    assert by_uid["password_hash"] == "ph1"         # password_hash も維持されている
    by_email = store.get_user_by_email(f"{uid}@example.test")
    assert by_email["uid"] == uid

    listed = store.list_users()
    assert any(u["uid"] == uid for u in listed)


def test_suggest_users_active_only_excludes_self_and_escapes_wildcards():
    _try_init()
    sfx = _sfx()
    active_uid = f"unit-suggest-active-{sfx}"
    disabled_uid = f"unit-suggest-disabled-{sfx}"
    self_uid = f"unit-suggest-self-{sfx}"
    store.upsert_user(active_uid, display_name=f"Active {sfx}", password_hash="x", status="active")
    store.upsert_user(disabled_uid, display_name=f"Disabled {sfx}", password_hash="x", status="disabled")
    store.upsert_user(self_uid, display_name=f"Self {sfx}", password_hash="x", status="active")

    found = store.suggest_users(sfx, self_uid, limit=10)
    found_uids = {u["uid"] for u in found}
    assert active_uid in found_uids
    assert disabled_uid not in found_uids   # 無効化ユーザーは除外
    assert self_uid not in found_uids        # 自分自身は除外

    # RV MEDIUM（2026-07-03再検証）: ILIKE の % はリテラル扱い（ワイルドカードとして全件化しない）。
    literal_query = f"{sfx}%nonexistent"
    assert store.suggest_users(literal_query, "someone-else", limit=10) == []


def test_session_lifecycle_active_expired_revoked_and_disabled_user():
    _try_init()
    sfx = _sfx()
    uid = f"unit-session-{sfx}"
    store.upsert_user(uid, display_name="S", password_hash="x", status="active")

    th_active = f"th-active-{sfx}"
    store.create_session(uid, th_active, datetime.now(timezone.utc) + timedelta(days=1))
    u = store.session_user(th_active)
    assert u is not None and u["uid"] == uid

    th_expired = f"th-expired-{sfx}"
    store.create_session(uid, th_expired, datetime.now(timezone.utc) - timedelta(days=1))
    assert store.session_user(th_expired) is None   # 期限切れ

    th_revoked = f"th-revoked-{sfx}"
    store.create_session(uid, th_revoked, datetime.now(timezone.utc) + timedelta(days=1))
    store.revoke_session(th_revoked)
    assert store.session_user(th_revoked) is None   # 取消済み

    th_disabled = f"th-disabled-{sfx}"
    store.create_session(uid, th_disabled, datetime.now(timezone.utc) + timedelta(days=1))
    store.upsert_user(uid, status="disabled")
    assert store.session_user(th_disabled) is None   # ユーザー無効化後は取得不可

    store.set_last_login(uid)   # 例外なく完走すること


def test_session_user_throttles_last_seen_updates_within_window():
    """QW1（性能台帳 §4）: `last_seen_at` の UPDATE はポーリング毎ではなく、token_hash 単位で
    前回書込から `_LAST_SEEN_THROTTLE_SEC`（既定60秒）未満はスキップする。プロセス内キャッシュ
    （`users_store._last_seen_written_at`）を直接操作して境界を検証する（`health._ai_cache` と同じ
    リセット方式）。"""
    from sherpa.store import users as users_store

    _try_init()
    sfx = _sfx()
    uid = f"unit-throttle-{sfx}"
    store.upsert_user(uid, display_name="T", password_hash="x", status="active")
    th = f"th-throttle-{sfx}"
    store.create_session(uid, th, datetime.now(timezone.utc) + timedelta(days=1))
    users_store._last_seen_written_at.pop(th, None)   # 他テストの残骸と独立させる

    def _last_seen_at():
        with store._connect() as c:
            return c.execute("SELECT last_seen_at FROM auth_sessions WHERE token_hash=%s", (th,)).fetchone()["last_seen_at"]

    assert store.session_user(th) is not None
    first = _last_seen_at()
    assert first is not None

    # 直後の再呼び出し（60秒未満）: user 行は変わらず返るが last_seen_at は更新されない。
    assert store.session_user(th) is not None
    assert _last_seen_at() == first

    # 前回書込を60秒より前に見せかけると、次の呼び出しで実際に UPDATE される。
    users_store._last_seen_written_at[th] = time.monotonic() - (users_store._LAST_SEEN_THROTTLE_SEC + 1)
    assert store.session_user(th) is not None
    assert _last_seen_at() > first

    users_store._last_seen_written_at.pop(th, None)   # 後始末（他テストへ残骸を残さない）
