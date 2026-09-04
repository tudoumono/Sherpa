"""Slice 4: 認証・ユーザー管理・会話共有・監査の API 層回帰テスト。

要 Postgres。ローカル Codex 環境では DB に到達できない前提のため、DB 不可は各テストで
graceful SKIP する。親ランナーが DB 付きで実行する。

このモジュールは既定のログイン必須モードで動かす。
完全な「SHERPA_AUTH_DISABLED 未設定」通常モードは別プロセスの plain runner sweep 向けだが、同一プロセスでは
SHERPA_AUTH_DISABLED=1 が優先され cookie なし admin 互換になることを短く確認する。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from _test_users import register_test_uid

IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import auth, keys, store
    from sherpa.api import app
except Exception as e:  # pragma: no cover - infra/import 不足時の graceful skip 用
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]
    auth = None  # type: ignore[assignment]
    keys = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]
    app = None  # type: ignore[assignment]


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _try_init() -> bool:
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")   # 不可なら可視の skip（silent-green 根絶）


@pytest.fixture
def _personal_keys_allowed_in_db():
    """`store.update_settings()` の個人キー書込みは、A6（`personal_api_keys_allowed`）を
    関数呼出し時点で実 DB から直接（`sherpa.store.get_system_settings` の monkeypatch を経由せず）
    再確認する（advisory lock 付き）。個人キーを実際に PUT するテストのために実 DB にも
    `personal_api_keys_allowed=True` を書き、テスト終了後は元の値へ復元する（他テストと共有する
    実 DB の状態を汚さない・順序依存を排除）。DB 不可はテスト側の `_try_init()` が別途 skip する。"""
    if IMPORT_ERROR is not None:
        yield
        return
    try:
        store.init_schema()
        with store._connect() as c:
            prev_row = c.execute(
                "SELECT value FROM system_settings WHERE key='personal_api_keys_allowed'").fetchone()
    except Exception:
        yield
        return
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    try:
        yield
    finally:
        store.set_system_settings(
            "admin-uid", {"personal_api_keys_allowed": bool(prev_row["value"]) if prev_row else None})


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _mk_user(uid: str, password: str, *, role: str = "user", status: str = "active") -> None:
    store.upsert_user(
        uid,
        email=f"{uid}@slice4.local",
        display_name=uid.upper(),
        password_hash=auth.hash_password(password),
        role=role,
        status=status,
    )
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）


def _mk_admin(sfx: str) -> tuple[str, str]:
    uid = f"s4adm{sfx}"
    pw = f"s4-admin-pw-{sfx}"
    _mk_user(uid, pw, role="admin")
    return uid, pw


def _login(uid: str, password: str, *, raise_server_exceptions: bool = True) -> TestClient:
    c = _client(raise_server_exceptions=raise_server_exceptions)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed for {uid}: {r.status_code} {r.text}"
    return c


def _new_conversation(owner_uid: str, sfx: str) -> int:
    conv = store.create_conversation(user_id=owner_uid, world="v1", title=f"slice4-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", f"question-{sfx}")
    store.add_message(cid, "assistant", f"answer-{sfx}")
    return cid


def _create_share(owner: TestClient, cid: int, invitees: list[str], expires_at: datetime) -> tuple[int, str]:
    r = owner.post(
        f"/conversations/{cid}/shares",
        json={"invitee_user_ids": invitees, "expires_at": _iso(expires_at)},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("/share/conversations/")
    return data["share_id"], data["url"]


def _received_for(uid: str) -> list[dict]:
    return [c for c in store.list_conversations(uid) if c.get("origin") == "received_share"]


def _click_share(c: TestClient, share_url: str):
    return c.get(share_url, follow_redirects=False)


def _assert_no_secret_in_rows(rows: list[dict], secrets: list[str]) -> None:
    payload = json.dumps(
        [
            {
                "action": r.get("action"),
                "resource_type": r.get("resource_type"),
                "resource_id": r.get("resource_id"),
                "detail": r.get("detail"),
                "before_state": r.get("before_state"),
                "after_state": r.get("after_state"),
            }
            for r in rows
        ],
        ensure_ascii=False,
        default=str,
    )
    for secret in secrets:
        assert secret not in payload, f"secret leaked into audit payload: {secret}"


def test_expired_share_click_denied_and_no_active_wrapper():
    """期限切れ share は招待ユーザーでも click 403。受領 wrapper は作られない。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    owner_uid, owner_pw = f"s4expown{sfx}", f"owner-exp-pw-{sfx}"
    invitee_uid, invitee_pw = f"s4expinv{sfx}", f"invitee-exp-pw-{sfx}"
    _mk_user(owner_uid, owner_pw)
    _mk_user(invitee_uid, invitee_pw)
    cid = _new_conversation(owner_uid, sfx)

    owner = _login(owner_uid, owner_pw)
    _, share_url = _create_share(
        owner,
        cid,
        [invitee_uid],
        datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    invitee = _login(invitee_uid, invitee_pw)
    r = _click_share(invitee, share_url)
    assert r.status_code == 403, r.text

    wrappers = _received_for(invitee_uid)
    assert wrappers == [], f"expired share should not create received wrapper: {wrappers}"
    for wrapper in wrappers:
        gr = invitee.get(f"/conversations/{wrapper['id']}")
        assert gr.status_code == 200
        assert gr.json().get("share_status") != "active"


def test_revoked_share_click_denied_before_acceptance():
    """取消済み share は click 403。accept 前取消では受領 wrapper も作られない。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    owner_uid, owner_pw = f"s4revown{sfx}", f"owner-rev-pw-{sfx}"
    invitee_uid, invitee_pw = f"s4revinv{sfx}", f"invitee-rev-pw-{sfx}"
    _mk_user(owner_uid, owner_pw)
    _mk_user(invitee_uid, invitee_pw)
    cid = _new_conversation(owner_uid, sfx)

    owner = _login(owner_uid, owner_pw)
    share_id, share_url = _create_share(
        owner,
        cid,
        [invitee_uid],
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    rv = owner.post(f"/conversation-shares/{share_id}/revoke")
    assert rv.status_code == 200, rv.text

    invitee = _login(invitee_uid, invitee_pw)
    r = _click_share(invitee, share_url)
    assert r.status_code == 403, r.text
    assert _received_for(invitee_uid) == []


def test_uninvited_direct_get_and_received_append_rejected():
    """招待外 click と他人 cid 直 GET は拒否。受領 wrapper への /chat 追記も 403。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    owner_uid, owner_pw = f"s4isoown{sfx}", f"owner-iso-pw-{sfx}"
    invitee_uid, invitee_pw = f"s4isoinv{sfx}", f"invitee-iso-pw-{sfx}"
    other_uid, other_pw = f"s4isooth{sfx}", f"other-iso-pw-{sfx}"
    _mk_user(owner_uid, owner_pw)
    _mk_user(invitee_uid, invitee_pw)
    _mk_user(other_uid, other_pw)
    cid = _new_conversation(owner_uid, sfx)

    owner = _login(owner_uid, owner_pw)
    _, share_url = _create_share(
        owner,
        cid,
        [invitee_uid],
        datetime.now(timezone.utc) + timedelta(days=1),
    )

    other = _login(other_uid, other_pw)
    denied = _click_share(other, share_url)
    assert denied.status_code == 403, denied.text
    assert other.get(f"/conversations/{cid}").status_code in (403, 404)

    invitee = _login(invitee_uid, invitee_pw)
    accepted = _click_share(invitee, share_url)
    assert accepted.status_code == 302, accepted.text
    wrappers = _received_for(invitee_uid)
    assert len(wrappers) == 1, wrappers
    wid = wrappers[0]["id"]

    # 受領者以外の third user は wrapper id も読めない。
    assert other.get(f"/conversations/{wid}").status_code in (403, 404)

    gr = invitee.get(f"/conversations/{wid}")
    assert gr.status_code == 200, gr.text
    assert gr.json()["conversation"]["origin"] == "received_share"

    append = invitee.post(
        "/chat",
        json={"message": "append should fail", "world": "v1", "conversation_id": wid},
    )
    assert append.status_code == 403, append.text


def test_admin_users_patch_disable_roles_and_password_reset():
    """PATCH /admin/users/{uid}: disable/re-enable、role 昇格/降格、password reset を通す。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4patch{sfx}"
    old_pw = f"old-patch-pw-{sfx}"
    new_pw = f"new-patch-pw-{sfx}"

    admin = _login(admin_uid, admin_pw)
    created = admin.post(
        "/admin/users",
        json={
            "uid": target_uid,
            "display_name": "Slice4 Patch User",
            "role": "user",
            "password": old_pw,
            "email": f"{target_uid}@slice4.local",
        },
    )
    assert created.status_code == 200, created.text
    register_test_uid(target_uid)   # API 経由で作成した uid もテストユーザー残骸防止の対象にする

    # PW-1: API 作成の初期パスワードは must_change_password=True＝アプリ利用前に本人変更が要る。
    # このテストは以降 target 本人が /admin/users を叩くため、先に自己変更してフラグを外す。
    self_pw = f"slice4-self-pw-{sfx}"
    first = _login(target_uid, old_pw)
    chg = first.post("/auth/change-password",
                     json={"current_password": old_pw, "new_password": self_pw,
                           "confirm_password": self_pw})
    assert chg.status_code == 200, chg.text
    old_pw = self_pw   # 以降の本人ログインは変更後のパスワード

    disabled = admin.patch(f"/admin/users/{target_uid}", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    blocked = _client().post("/auth/login", json={"username": target_uid, "password": old_pw})
    assert blocked.status_code == 401, blocked.text

    enabled = admin.patch(f"/admin/users/{target_uid}", json={"status": "active"})
    assert enabled.status_code == 200, enabled.text

    promoted = admin.patch(f"/admin/users/{target_uid}", json={"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    target_admin = _login(target_uid, old_pw)
    assert target_admin.get("/admin/users").status_code == 200

    demoted = admin.patch(f"/admin/users/{target_uid}", json={"role": "user"})
    assert demoted.status_code == 200, demoted.text
    assert target_admin.get("/admin/users").status_code == 403

    reset = admin.patch(f"/admin/users/{target_uid}", json={"password": new_pw})
    assert reset.status_code == 200, reset.text
    old_login = _client().post("/auth/login", json={"username": target_uid, "password": old_pw})
    assert old_login.status_code == 401, old_login.text
    assert _client().post("/auth/login", json={"username": target_uid, "password": new_pw}).status_code == 200


def test_admin_users_patch_display_name_updates_and_audits():
    """PATCH /admin/users/{uid} は実際に値が変わったフィールドだけを更新・監査する。

    契約: キー省略・JSON null・現在値と同じ値の再送はいずれも「変更なし」として扱われ、
    upsert 対象にも監査対象にもならない（変更が0件なら 422）。空文字だけは display_name の
    明示的なクリアとして上書きされる（store.upsert_user の COALESCE は NULL のみ既存維持で
    空文字は通過するため）。複数フィールドが同時に実際に変わった場合、監査は既存の action 名
    （user.disabled/user.created・user.role_changed・user.display_name_changed・
    user.password_reset）ごとに行を分けて出し、同一 PATCH 内の複数行は request_id で
    対応付ける。
    """
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4dispname{sfx}"

    admin = _login(admin_uid, admin_pw)
    created = admin.post(
        "/admin/users",
        json={
            "uid": target_uid,
            "display_name": "誤字太郎",
            "role": "user",
            "password": f"dispname-pw-{sfx}",
            "email": f"{target_uid}@slice4.local",
        },
    )
    assert created.status_code == 200, created.text
    register_test_uid(target_uid)
    # POST /admin/users 自体が既に1件 user.created を記録する（作成の監査）ため、以降は
    # 「PATCH で新たに増えたか」を件数差分で見る（0件のはず、と単純に断定できない）。
    created_count = len(store.list_audit(action="user.created", actor=admin_uid,
                                         resource_id=f"user:{target_uid}", limit=50))

    # 変更フィールドが1件もない PATCH は 422（UI 側は無編集保存でこの呼び出し自体をしない）。
    empty_patch = admin.patch(f"/admin/users/{target_uid}", json={})
    assert empty_patch.status_code == 422, empty_patch.text

    # UI と同一 payload を模す: 現在値と同じ role/status を一緒に送っても、実際に変わった
    # display_name だけが更新・監査される（role_changed 等の偽の action は記録されない）。
    renamed = admin.patch(f"/admin/users/{target_uid}",
                          json={"display_name": "正字太郎", "role": "user", "status": "active"})
    assert renamed.status_code == 200, renamed.text
    assert store.get_user(target_uid)["display_name"] == "正字太郎"

    rows = store.list_audit(action="user.display_name_changed", actor=admin_uid,
                            resource_id=f"user:{target_uid}", limit=5)
    assert rows, "display_name 変更の監査行が見つからない"
    row = rows[0]
    assert row["before_state"] == {"display_name": "誤字太郎"}
    assert row["after_state"] == {"display_name": "正字太郎"}
    assert not store.list_audit(
        action="user.role_changed", actor=admin_uid, resource_id=f"user:{target_uid}", limit=5
    ), "role が変わっていないのに user.role_changed が記録されている"
    created_count_after = len(store.list_audit(action="user.created", actor=admin_uid,
                                               resource_id=f"user:{target_uid}", limit=50))
    assert created_count_after == created_count, "status が変わっていないのに再有効化の監査が記録されている"

    # 同じ値（role="user"）の再送は変更なし＝422（無編集保存の server 側での再確認）。
    no_op = admin.patch(f"/admin/users/{target_uid}", json={"role": "user"})
    assert no_op.status_code == 422, no_op.text

    # JSON null は「キー省略」と同じ＝変更なし。role の実変更と一緒に送っても display_name は動かない。
    name_rows_before = len(store.list_audit(action="user.display_name_changed", actor=admin_uid,
                                            resource_id=f"user:{target_uid}", limit=50))
    role_promoted = admin.patch(f"/admin/users/{target_uid}",
                                json={"role": "admin", "display_name": None})
    assert role_promoted.status_code == 200, role_promoted.text
    assert store.get_user(target_uid)["display_name"] == "正字太郎"
    name_rows_after = len(store.list_audit(action="user.display_name_changed", actor=admin_uid,
                                           resource_id=f"user:{target_uid}", limit=50))
    assert name_rows_after == name_rows_before, "display_name が null なのに display_name_changed が新規記録された"
    role_row = store.list_audit(action="user.role_changed", actor=admin_uid,
                                resource_id=f"user:{target_uid}", limit=5)[0]
    assert role_row["before_state"] == {"role": "user"}
    assert role_row["after_state"] == {"role": "admin"}

    # 空文字＝クリア（キー省略との違い＝「無視」ではなく明示的な上書き）。監査にも反映される。
    cleared = admin.patch(f"/admin/users/{target_uid}", json={"display_name": ""})
    assert cleared.status_code == 200, cleared.text
    assert store.get_user(target_uid)["display_name"] == ""
    clear_row = store.list_audit(action="user.display_name_changed", actor=admin_uid,
                                 resource_id=f"user:{target_uid}", limit=5)[0]
    assert clear_row["before_state"] == {"display_name": "正字太郎"}
    assert clear_row["after_state"] == {"display_name": ""}

    # 複数フィールドが同時に実際に変わった場合: action ごとに監査行を分け、同一 request_id で
    # 対応付ける（1行に集約して他の action の検索結果から欠落することがないように）。
    new_pw = f"multi-pw-{sfx}"
    multi = admin.patch(f"/admin/users/{target_uid}",
                        json={"status": "disabled", "role": "user",
                              "display_name": "複数太郎", "password": new_pw})
    assert multi.status_code == 200, multi.text
    row_disabled = store.list_audit(action="user.disabled", actor=admin_uid,
                                    resource_id=f"user:{target_uid}", limit=5)[0]
    row_role = store.list_audit(action="user.role_changed", actor=admin_uid,
                                resource_id=f"user:{target_uid}", limit=5)[0]
    row_name = store.list_audit(action="user.display_name_changed", actor=admin_uid,
                                resource_id=f"user:{target_uid}", limit=5)[0]
    row_pw = store.list_audit(action="user.password_reset", actor=admin_uid,
                              resource_id=f"user:{target_uid}", limit=5)[0]
    req_ids = {row_disabled["request_id"], row_role["request_id"],
              row_name["request_id"], row_pw["request_id"]}
    assert len(req_ids) == 1 and None not in req_ids, (
        "同一 PATCH 内の監査行が request_id で対応付けられていない")
    assert row_disabled["before_state"] == {"status": "active"}
    assert row_disabled["after_state"] == {"status": "disabled"}
    assert row_role["before_state"] == {"role": "admin"}
    assert row_role["after_state"] == {"role": "user"}
    assert row_name["before_state"] == {"display_name": ""}
    assert row_name["after_state"] == {"display_name": "複数太郎"}


def test_admin_users_patch_audit_batch_atomic_on_partial_failure(monkeypatch):
    """PATCH の主変更は監査バッチの失敗に関わらず成功したまま（best-effort・応答も 200 のまま）だが、
    複数の監査行自体は「全部残るか、1件も残らないか」（all-or-none・部分確定しない）。
    2件目（role_changed）の書き込みで例外を起こしても、1件目（disabled）だけが残ることはない。
    """
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4auditatomic{sfx}"

    admin = _login(admin_uid, admin_pw)
    created = admin.post(
        "/admin/users",
        json={
            "uid": target_uid,
            "display_name": "初期太郎",
            "role": "user",
            "password": f"atomic-pw-{sfx}",
            "email": f"{target_uid}@slice4.local",
        },
    )
    assert created.status_code == 200, created.text
    register_test_uid(target_uid)

    actions = ["user.disabled", "user.role_changed", "user.display_name_changed", "user.password_reset"]
    before_counts = {a: len(store.list_audit(action=a, actor=admin_uid,
                                             resource_id=f"user:{target_uid}", limit=50))
                     for a in actions}

    real_insert = store._audit_insert
    call_count = {"n": 0}

    def flaky_insert(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated audit failure (2nd row)")
        return real_insert(*a, **kw)

    monkeypatch.setattr(store, "_audit_insert", flaky_insert)
    patched = admin.patch(
        f"/admin/users/{target_uid}",
        json={"status": "disabled", "role": "admin", "display_name": "後太郎",
              "password": f"atomic-new-pw-{sfx}"},
    )
    monkeypatch.undo()

    # best-effort: 監査バッチが失敗しても主変更（upsert_user）は成功したまま（200・実際に反映済み）。
    assert patched.status_code == 200, patched.text
    row = store.get_user(target_uid)
    assert row["status"] == "disabled"
    assert row["role"] == "admin"
    assert row["display_name"] == "後太郎"

    # all-or-none: 2件目で失敗＝1件目（disabled）だけが残る部分確定はしない（4action とも新規0件）。
    for a in actions:
        after = len(store.list_audit(action=a, actor=admin_uid,
                                     resource_id=f"user:{target_uid}", limit=50))
        assert after == before_counts[a], f"{a} が部分確定している（before={before_counts[a]} after={after}）"


def test_admin_users_patch_rejects_invalid_status_and_role_values():
    """USR-1 RV3: status="pending" や空文字の role/status は422で拒否され、状態は不変のまま
    （実サーバの allowlist 検証。pending は PATCH で選べる値ではない・空文字は「未指定」ではなく
    範囲外の値として拒否される＝role/status の判定が is not None ベースであることの確認）。
    """
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4invalid{sfx}"

    admin = _login(admin_uid, admin_pw)
    created = admin.post(
        "/admin/users",
        json={
            "uid": target_uid,
            "display_name": "元太郎",
            "role": "user",
            "password": f"invalid-pw-{sfx}",
            "email": f"{target_uid}@slice4.local",
        },
    )
    assert created.status_code == 200, created.text
    register_test_uid(target_uid)

    before = store.get_user(target_uid)
    for payload in (
        {"status": "pending"},
        {"role": "", "display_name": "変更名"},
        {"status": "", "display_name": "変更名"},
    ):
        r = admin.patch(f"/admin/users/{target_uid}", json=payload)
        assert r.status_code == 422, (payload, r.text)
        after = store.get_user(target_uid)
        assert after == before, f"{payload} で状態が変わってしまった: before={before} after={after}"


def test_admin_gate_management_endpoints_for_user_and_admin():
    """world/ingest/admin search/users/audit の管理 endpoint は user 403、admin は認証 gate 通過。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    user_uid, user_pw = f"s4gate{sfx}", f"user-gate-pw-{sfx}"
    _mk_user(user_uid, user_pw)

    user = _login(user_uid, user_pw, raise_server_exceptions=False)
    admin = _login(admin_uid, admin_pw, raise_server_exceptions=False)

    endpoints = [
        ("get", "/worlds", {}),
        ("get", "/ingest/preview", {}),
        ("get", "/ingest/runs", {}),
        ("get", "/admin/es/search", {"params": {"query": "slice4"}}),
        ("get", "/admin/users", {}),
        ("get", "/admin/audit", {"params": {"limit": 1}}),
    ]
    for method, path, kwargs in endpoints:
        user_r = getattr(user, method)(path, **kwargs)
        assert user_r.status_code == 403, f"{path} should be 403 for user, got {user_r.status_code}"

        admin_r = getattr(admin, method)(path, **kwargs)
        assert admin_r.status_code not in (401, 403), (
            f"{path} should pass admin gate, got {admin_r.status_code}: {admin_r.text}"
        )


def test_settings_isolation_and_api_keys_not_returned(monkeypatch, _personal_keys_allowed_in_db):
    """settings は user 別。API key は他 user に漏れず、本人にも値は返らない。"""
    if not _try_init():
        pytest.skip("infra down")
    # 個人 API キーの保存には personal_api_keys_allowed が要る（既定 false）。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    sfx = _sfx()
    u1, p1 = f"s4seta{sfx}", f"set-a-pw-{sfx}"
    u2, p2 = f"s4setb{sfx}", f"set-b-pw-{sfx}"
    openai_key = f"sk-slice4-openai-{sfx}"
    gemini_key = f"gemini-slice4-key-{sfx}"
    _mk_user(u1, p1)
    _mk_user(u2, p2)

    c1 = _login(u1, p1)
    c2 = _login(u2, p2)
    # openai_model/ollama_model は個人設定に無い（管理者のカタログだけで決まる）ため、user 別の
    # 隔離は他の個人設定項目（system_prompt）でも確認する。
    r1 = c1.put(
        "/settings",
        json={
            "agent": "openai",
            "openai_api_key": openai_key,
            "gemini_api_key": gemini_key,
            "system_prompt": f"u1-prompt-{sfx}",
        },
    )
    assert r1.status_code == 200, r1.text
    r2 = c2.put("/settings", json={"agent": "ollama", "system_prompt": f"u2-prompt-{sfx}"})
    assert r2.status_code == 200, r2.text

    s1 = c1.get("/settings")
    s2 = c2.get("/settings")
    assert s1.status_code == 200 and s2.status_code == 200
    d1, d2 = s1.json(), s2.json()
    assert d1["openai_key_set"] is True
    assert d2["openai_key_set"] is False
    assert d1["system_prompt"] == f"u1-prompt-{sfx}"
    assert d2["system_prompt"] == f"u2-prompt-{sfx}"

    all_public = json.dumps({"u1": d1, "u2": d2, "config2": c2.get("/config").json()}, default=str)
    assert openai_key not in all_public
    assert gemini_key not in all_public
    assert "openai_api_key" not in d1
    assert "gemini_api_key" not in d1


def test_settings_openai_key_set_is_false_for_placeholder_value(monkeypatch, _personal_keys_allowed_in_db):
    """RV MED（2026-08-18 Codex RV 2巡目 指摘3）: `openai_key_set` は真偽値のみだとプレースホルダ
    （`sk-REPLACE_ME`）でも「設定済み」と表示し得た。判定を `agent_constructs.is_real_api_key` に
    揃え、プレースホルダは未設定として、実キーはこれまでどおり設定済みとして返すことを固定する。"""
    if not _try_init():
        pytest.skip("infra down")
    # 個人 API キーの保存には personal_api_keys_allowed が要る（既定 false）。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    sfx = _sfx()
    uid, pw = f"s4setph{sfx}", f"set-ph-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json={"openai_api_key": "sk-REPLACE_ME"})
    assert r.status_code == 200, r.text
    d = c.get("/settings").json()
    assert d["openai_key_set"] is False, "プレースホルダなのに設定済みと表示されている"

    # 利用者が入れた実キーの扱いは変えない（プレースホルダ文字列と一致しない限り常に真）。
    r2 = c.put("/settings", json={"openai_api_key": f"sk-real-{sfx}"})
    assert r2.status_code == 200, r2.text
    d2 = c.get("/settings").json()
    assert d2["openai_key_set"] is True


def test_settings_test_openai_rejects_env_placeholder_without_probing(monkeypatch):
    """RV MED（2026-08-18 指摘3）: `POST /settings/test`（provider=openai）で保存済み/入力中のキーが
    無く env の OPENAI_API_KEY だけがプレースホルダのままだと、以前は真偽値判定を通り抜けて実 API へ
    probe しに行った（分かりにくい 401）。プレースホルダは `keys.NO_CENTRAL_KEY_MESSAGE` で早期に
    弾かれることを固定する（実 API へは到達しない＝ネットワーク不要でテストできる）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"s4settp{sfx}", f"set-tp-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-REPLACE_ME")
    r = c.post("/settings/test", json={"provider": "openai"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["detail"] == keys.NO_CENTRAL_KEY_MESSAGE


def test_audit_rows_for_security_ops_and_secret_redaction(monkeypatch, _personal_keys_allowed_in_db):
    """主要 security op の audit row と、password/token/API key の非記録を確認する。"""
    if not _try_init():
        pytest.skip("infra down")
    # 個人 API キーの保存には personal_api_keys_allowed が要る（既定 false）。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    owner_uid, owner_pw = f"s4auditown{sfx}", f"audit-owner-pw-{sfx}"
    invitee_uid, invitee_pw = f"s4auditinv{sfx}", f"audit-invitee-pw-{sfx}"
    bad_uid = f"s4auditmissing{sfx}"
    bad_pw = f"audit-bad-pw-{sfx}"
    reset_pw = f"audit-reset-pw-{sfx}"
    openai_key = f"sk-audit-openai-{sfx}"
    gemini_key = f"gemini-audit-key-{sfx}"

    admin = _login(admin_uid, admin_pw)
    failed = _client().post("/auth/login", json={"username": bad_uid, "password": bad_pw})
    assert failed.status_code == 401

    for uid, pw in ((owner_uid, owner_pw), (invitee_uid, invitee_pw)):
        created = admin.post(
            "/admin/users",
            json={
                "uid": uid,
                "display_name": uid.upper(),
                "role": "user",
                "password": pw,
                "email": f"{uid}@slice4.local",
            },
        )
        assert created.status_code == 200, created.text
        register_test_uid(uid)   # API 経由で作成した uid もテストユーザー残骸防止の対象にする

    role_changed = admin.patch(f"/admin/users/{owner_uid}", json={"role": "admin"})
    assert role_changed.status_code == 200, role_changed.text
    password_reset = admin.patch(f"/admin/users/{invitee_uid}", json={"password": reset_pw})
    assert password_reset.status_code == 200, password_reset.text

    # PW-1: API 作成の初期パスワード・管理者リセットはいずれも must_change_password=True＝
    # 本人が変更するまでアプリ API を使えない。以降のアプリ操作の前に本人変更を済ませる。
    owner_final_pw = f"audit-owner-final-{sfx}"
    owner = _login(owner_uid, owner_pw)
    chg = owner.post("/auth/change-password",
                     json={"current_password": owner_pw, "new_password": owner_final_pw,
                           "confirm_password": owner_final_pw})
    assert chg.status_code == 200, chg.text
    settings = owner.put(
        "/settings",
        json={"openai_api_key": openai_key, "gemini_api_key": gemini_key, "agent": "openai"},
    )
    assert settings.status_code == 200, settings.text

    cid = _new_conversation(owner_uid, sfx)
    share_id, share_url = _create_share(
        owner,
        cid,
        [invitee_uid],
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    share_token = share_url.rsplit("/", 1)[-1]

    invitee_final_pw = f"audit-invitee-final-{sfx}"
    invitee = _login(invitee_uid, reset_pw)
    chg = invitee.post("/auth/change-password",
                       json={"current_password": reset_pw, "new_password": invitee_final_pw,
                             "confirm_password": invitee_final_pw})
    assert chg.status_code == 200, chg.text
    accepted = _click_share(invitee, share_url)
    assert accepted.status_code == 302, accepted.text

    revoked = owner.post(f"/conversation-shares/{share_id}/revoke")
    assert revoked.status_code == 200, revoked.text

    expectations = [
        ("auth.login_failed", {"resource_id": f"user:{bad_uid}"}),
        ("user.created", {"actor": admin_uid, "resource_id": f"user:{owner_uid}"}),
        ("user.role_changed", {"actor": admin_uid, "resource_id": f"user:{owner_uid}"}),
        ("user.password_reset", {"actor": admin_uid, "resource_id": f"user:{invitee_uid}"}),
        ("settings.updated", {"actor": owner_uid, "resource_id": f"settings:{owner_uid}"}),
        ("share.created", {"actor": owner_uid, "resource_id": f"share:{share_id}"}),
        ("share.accepted", {"actor": invitee_uid, "resource_id": f"share:{share_id}"}),
        ("share.revoked", {"actor": owner_uid, "resource_id": f"share:{share_id}"}),
    ]
    for action, filters in expectations:
        rows = store.list_audit(action=action, limit=20, **filters)
        assert rows, f"missing audit row for {action} with {filters}"

    rows = []
    for action, filters in expectations:
        rows.extend(store.list_audit(action=action, limit=20, **filters))
    _assert_no_secret_in_rows(
        rows,
        [admin_pw, owner_pw, invitee_pw, bad_pw, reset_pw, owner_final_pw, invitee_final_pw,
         openai_key, gemini_key, share_token],
    )


def test_auth_disabled_compat_mode_without_login(auth_disabled):
    """同一プロセスでは SHERPA_AUTH_DISABLED=1 優先で cookie なし admin 互換を確認する。"""
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    r = _client().get("/auth/me")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["uid"] == "admin"
    assert data["role"] == "admin"
