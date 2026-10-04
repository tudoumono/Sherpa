"""認証・ユーザー管理・会話共有・監査の API 層の契約（ログイン必須モード・要 Postgres）。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, keys, store
from sherpa.api import app


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")


@pytest.fixture
def _personal_keys_allowed(monkeypatch):
    """個人 API キーの保存には personal_api_keys_allowed が要る。store.update_settings は実 DB を
    直接再確認するため、実 DB にも True を書き、終了時に元へ戻す。"""
    store.init_schema()
    with store._connect() as c:
        prev_row = c.execute("SELECT value FROM system_settings WHERE key='personal_api_keys_allowed'").fetchone()
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    yield
    monkeypatch.undo()
    store.set_system_settings(
        "admin-uid", {"personal_api_keys_allowed": bool(prev_row["value"]) if prev_row else None})


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _mk_user(uid: str, password: str, *, role: str = "user", status: str = "active") -> None:
    store.upsert_user(uid, email=f"{uid}@slice4.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(password), role=role, status=status)
    register_test_uid(uid)


def _mk_admin(sfx: str) -> tuple[str, str]:
    uid, pw = f"s4adm{sfx}", f"s4-admin-pw-{sfx}"
    _mk_user(uid, pw, role="admin")
    return uid, pw


def _login(uid: str, password: str, *, raise_server_exceptions: bool = True) -> TestClient:
    c = _client(raise_server_exceptions=raise_server_exceptions)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed for {uid}: {r.status_code} {r.text}"
    return c


def _api_create_user(admin: TestClient, uid: str, pw: str, display_name: str | None = None) -> None:
    r = admin.post("/admin/users", json={"uid": uid, "display_name": display_name or uid.upper(),
                                         "role": "user", "password": pw, "email": f"{uid}@slice4.local"})
    assert r.status_code == 200, r.text
    register_test_uid(uid)


def _change_pw(c: TestClient, current: str, new: str) -> None:
    """API 作成・管理者リセットの初期パスワードは must_change_password＝本人変更が要る。"""
    r = c.post("/auth/change-password",
               json={"current_password": current, "new_password": new, "confirm_password": new})
    assert r.status_code == 200, r.text


def _new_conversation(owner_uid: str, sfx: str) -> int:
    cid = store.create_conversation(user_id=owner_uid, world="v1", title=f"slice4-{sfx}")["id"]
    store.add_message(cid, "user", f"question-{sfx}")
    store.add_message(cid, "assistant", f"answer-{sfx}")
    return cid


def _create_share(owner: TestClient, cid: int, invitees: list[str], expires_at: datetime) -> tuple[int, str]:
    r = owner.post(f"/conversations/{cid}/shares",
                   json={"invitee_user_ids": invitees,
                         "expires_at": expires_at.astimezone(timezone.utc).isoformat()})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("/share/conversations/")
    return data["share_id"], data["url"]


def _received_for(uid: str) -> list[dict]:
    return [c for c in store.list_conversations(uid) if c.get("origin") == "received_share"]


def _click_share(c: TestClient, share_url: str):
    return c.get(share_url, follow_redirects=False)


def _audit(action: str, actor: str, target: str, limit: int = 50) -> list[dict]:
    return store.list_audit(action=action, actor=actor, resource_id=f"user:{target}", limit=limit)


def _assert_no_secret_in_rows(rows: list[dict], secrets: list[str]) -> None:
    keys_ = ("action", "resource_type", "resource_id", "detail", "before_state", "after_state")
    payload = json.dumps([{k: r.get(k) for k in keys_} for r in rows], ensure_ascii=False, default=str)
    for secret in secrets:
        assert secret not in payload, f"secret leaked into audit payload: {secret}"


# ===== 共有リンク =====

@pytest.mark.parametrize("how", ["expired", "revoked"])
def test_dead_share_click_denied_and_no_wrapper(how):
    """期限切れ・取消済み（受領前）の共有は招待ユーザーでも click 403・受領 wrapper は作られない。"""
    sfx = _sfx()
    owner_uid, owner_pw = f"s4{how[:3]}own{sfx}", f"owner-pw-{sfx}"
    invitee_uid, invitee_pw = f"s4{how[:3]}inv{sfx}", f"invitee-pw-{sfx}"
    _mk_user(owner_uid, owner_pw)
    _mk_user(invitee_uid, invitee_pw)
    cid = _new_conversation(owner_uid, sfx)
    owner = _login(owner_uid, owner_pw)
    delta = timedelta(minutes=-5) if how == "expired" else timedelta(days=1)
    share_id, share_url = _create_share(owner, cid, [invitee_uid], datetime.now(timezone.utc) + delta)
    if how == "revoked":
        rv = owner.post(f"/conversation-shares/{share_id}/revoke")
        assert rv.status_code == 200, rv.text

    invitee = _login(invitee_uid, invitee_pw)
    r = _click_share(invitee, share_url)
    assert r.status_code == 403, r.text
    assert _received_for(invitee_uid) == []


def test_uninvited_direct_get_and_received_append_rejected():
    """招待外 click と他人 cid 直 GET は拒否。受領 wrapper への /chat 追記も 403。"""
    sfx = _sfx()
    owner_uid, owner_pw = f"s4isoown{sfx}", f"owner-iso-pw-{sfx}"
    invitee_uid, invitee_pw = f"s4isoinv{sfx}", f"invitee-iso-pw-{sfx}"
    other_uid, other_pw = f"s4isooth{sfx}", f"other-iso-pw-{sfx}"
    for uid, pw in ((owner_uid, owner_pw), (invitee_uid, invitee_pw), (other_uid, other_pw)):
        _mk_user(uid, pw)
    cid = _new_conversation(owner_uid, sfx)
    owner = _login(owner_uid, owner_pw)
    _, share_url = _create_share(owner, cid, [invitee_uid], datetime.now(timezone.utc) + timedelta(days=1))

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

    assert other.get(f"/conversations/{wid}").status_code in (403, 404)
    gr = invitee.get(f"/conversations/{wid}")
    assert gr.status_code == 200, gr.text
    assert gr.json()["conversation"]["origin"] == "received_share"

    append = invitee.post("/chat/turns", json={"message": "append should fail", "world": "v1",
                                               "conversation_id": wid})
    assert append.status_code == 403, append.text


# ===== /admin/users PATCH =====

def test_admin_users_patch_disable_roles_and_password_reset():
    """disable/re-enable、role 昇格/降格、password reset。"""
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4patch{sfx}"
    old_pw, new_pw = f"old-patch-pw-{sfx}", f"new-patch-pw-{sfx}"
    admin = _login(admin_uid, admin_pw)
    _api_create_user(admin, target_uid, old_pw, "Slice4 Patch User")
    self_pw = f"slice4-self-pw-{sfx}"
    _change_pw(_login(target_uid, old_pw), old_pw, self_pw)
    old_pw = self_pw

    assert admin.patch(f"/admin/users/{target_uid}", json={"status": "disabled"}).status_code == 200
    blocked = _client().post("/auth/login", json={"username": target_uid, "password": old_pw})
    assert blocked.status_code == 401, blocked.text
    assert admin.patch(f"/admin/users/{target_uid}", json={"status": "active"}).status_code == 200

    assert admin.patch(f"/admin/users/{target_uid}", json={"role": "admin"}).status_code == 200
    target_admin = _login(target_uid, old_pw)
    assert target_admin.get("/admin/users").status_code == 200
    assert admin.patch(f"/admin/users/{target_uid}", json={"role": "user"}).status_code == 200
    assert target_admin.get("/admin/users").status_code == 403

    reset = admin.patch(f"/admin/users/{target_uid}", json={"password": new_pw})
    assert reset.status_code == 200, reset.text
    old_login = _client().post("/auth/login", json={"username": target_uid, "password": old_pw})
    assert old_login.status_code == 401, old_login.text
    assert _client().post("/auth/login", json={"username": target_uid, "password": new_pw}).status_code == 200


def test_admin_users_patch_display_name_updates_and_audits():
    """PATCH は実際に値が変わったフィールドだけ更新・監査する（省略・null・同値の再送は変更なし＝全部
    同値なら 422、空文字だけが明示クリア）。複数同時変更は action ごとに監査行を分け request_id で対応付ける。"""
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4dispname{sfx}"
    admin = _login(admin_uid, admin_pw)
    _api_create_user(admin, target_uid, f"dispname-pw-{sfx}", "誤字太郎")
    url = f"/admin/users/{target_uid}"
    created_count = len(_audit("user.created", admin_uid, target_uid))

    assert admin.patch(url, json={}).status_code == 422

    # UI と同一 payload（現在値と同じ role/status 付き）でも display_name だけが更新・監査される。
    renamed = admin.patch(url, json={"display_name": "正字太郎", "role": "user", "status": "active"})
    assert renamed.status_code == 200, renamed.text
    assert store.get_user(target_uid)["display_name"] == "正字太郎"
    row = _audit("user.display_name_changed", admin_uid, target_uid, 5)[0]
    assert row["before_state"] == {"display_name": "誤字太郎"}
    assert row["after_state"] == {"display_name": "正字太郎"}
    assert not _audit("user.role_changed", admin_uid, target_uid, 5)
    assert len(_audit("user.created", admin_uid, target_uid)) == created_count

    assert admin.patch(url, json={"role": "user"}).status_code == 422

    # JSON null はキー省略と同じ＝display_name は動かない。
    name_rows_before = len(_audit("user.display_name_changed", admin_uid, target_uid))
    promoted = admin.patch(url, json={"role": "admin", "display_name": None})
    assert promoted.status_code == 200, promoted.text
    assert store.get_user(target_uid)["display_name"] == "正字太郎"
    assert len(_audit("user.display_name_changed", admin_uid, target_uid)) == name_rows_before
    role_row = _audit("user.role_changed", admin_uid, target_uid, 5)[0]
    assert role_row["before_state"] == {"role": "user"}
    assert role_row["after_state"] == {"role": "admin"}

    # 空文字＝クリア。
    cleared = admin.patch(url, json={"display_name": ""})
    assert cleared.status_code == 200, cleared.text
    assert store.get_user(target_uid)["display_name"] == ""
    clear_row = _audit("user.display_name_changed", admin_uid, target_uid, 5)[0]
    assert clear_row["before_state"] == {"display_name": "正字太郎"}
    assert clear_row["after_state"] == {"display_name": ""}

    multi = admin.patch(url, json={"status": "disabled", "role": "user",
                                   "display_name": "複数太郎", "password": f"multi-pw-{sfx}"})
    assert multi.status_code == 200, multi.text
    rows = {a: _audit(a, admin_uid, target_uid, 5)[0]
            for a in ("user.disabled", "user.role_changed", "user.display_name_changed", "user.password_reset")}
    req_ids = {r["request_id"] for r in rows.values()}
    assert len(req_ids) == 1 and None not in req_ids
    assert rows["user.disabled"]["before_state"] == {"status": "active"}
    assert rows["user.disabled"]["after_state"] == {"status": "disabled"}
    assert rows["user.role_changed"]["before_state"] == {"role": "admin"}
    assert rows["user.role_changed"]["after_state"] == {"role": "user"}
    assert rows["user.display_name_changed"]["before_state"] == {"display_name": ""}
    assert rows["user.display_name_changed"]["after_state"] == {"display_name": "複数太郎"}


def test_admin_users_patch_audit_batch_atomic_on_partial_failure(monkeypatch):
    """監査バッチが失敗しても主変更は 200 で反映済み（best-effort）だが、監査行は all-or-none
    （2 件目で失敗しても 1 件目だけが残る部分確定はしない）。"""
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4auditatomic{sfx}"
    admin = _login(admin_uid, admin_pw)
    _api_create_user(admin, target_uid, f"atomic-pw-{sfx}", "初期太郎")
    actions = ["user.disabled", "user.role_changed", "user.display_name_changed", "user.password_reset"]
    before = {a: len(_audit(a, admin_uid, target_uid)) for a in actions}

    real_insert = store._audit_insert
    calls = {"n": 0}

    def flaky_insert(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated audit failure (2nd row)")
        return real_insert(*a, **kw)

    monkeypatch.setattr(store, "_audit_insert", flaky_insert)
    patched = admin.patch(f"/admin/users/{target_uid}",
                          json={"status": "disabled", "role": "admin", "display_name": "後太郎",
                                "password": f"atomic-new-pw-{sfx}"})
    monkeypatch.undo()

    assert patched.status_code == 200, patched.text
    row = store.get_user(target_uid)
    assert (row["status"], row["role"], row["display_name"]) == ("disabled", "admin", "後太郎")
    for a in actions:
        assert len(_audit(a, admin_uid, target_uid)) == before[a], f"{a} が部分確定している"


def test_admin_users_patch_rejects_invalid_status_and_role_values():
    """status="pending"・空文字の role/status は 422 で拒否され状態は不変（空文字は未指定ではなく範囲外）。"""
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    target_uid = f"s4invalid{sfx}"
    admin = _login(admin_uid, admin_pw)
    _api_create_user(admin, target_uid, f"invalid-pw-{sfx}", "元太郎")
    before = store.get_user(target_uid)
    for payload in ({"status": "pending"},
                    {"role": "", "display_name": "変更名"},
                    {"status": "", "display_name": "変更名"}):
        r = admin.patch(f"/admin/users/{target_uid}", json=payload)
        assert r.status_code == 422, (payload, r.text)
        assert store.get_user(target_uid) == before, payload


def test_admin_gate_management_endpoints_for_user_and_admin():
    """管理 endpoint は user 403、admin は認証 gate 通過。"""
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    user_uid, user_pw = f"s4gate{sfx}", f"user-gate-pw-{sfx}"
    _mk_user(user_uid, user_pw)
    user = _login(user_uid, user_pw, raise_server_exceptions=False)
    admin = _login(admin_uid, admin_pw, raise_server_exceptions=False)

    for path, kwargs in [
        ("/worlds", {}),
        ("/ingest/preview", {}),
        ("/ingest/runs", {}),
        ("/admin/es/search", {"params": {"query": "slice4"}}),
        ("/admin/users", {}),
        ("/admin/audit", {"params": {"limit": 1}}),
    ]:
        user_r = user.get(path, **kwargs)
        assert user_r.status_code == 403, f"{path} should be 403 for user, got {user_r.status_code}"
        admin_r = admin.get(path, **kwargs)
        assert admin_r.status_code not in (401, 403), f"{path} admin gate: {admin_r.status_code}: {admin_r.text}"


# ===== 個人設定・API キー =====

def test_settings_isolation_and_api_keys_not_returned(_personal_keys_allowed):
    """settings は user 別。API key は他 user に漏れず、本人にも値は返らない。"""
    sfx = _sfx()
    u1, p1 = f"s4seta{sfx}", f"set-a-pw-{sfx}"
    u2, p2 = f"s4setb{sfx}", f"set-b-pw-{sfx}"
    openai_key, gemini_key = f"sk-slice4-openai-{sfx}", f"gemini-slice4-key-{sfx}"
    _mk_user(u1, p1)
    _mk_user(u2, p2)
    c1, c2 = _login(u1, p1), _login(u2, p2)
    r1 = c1.put("/settings", json={"agent": "simple", "openai_api_key": openai_key,
                                   "gemini_api_key": gemini_key, "codex_model_provider": "ollama"})
    assert r1.status_code == 200, r1.text
    r2 = c2.put("/settings", json={"agent": "simple", "codex_model_provider": "openai"})
    assert r2.status_code == 200, r2.text

    s1, s2 = c1.get("/settings"), c2.get("/settings")
    assert s1.status_code == 200 and s2.status_code == 200
    d1, d2 = s1.json(), s2.json()
    assert d1["openai_key_set"] is True
    assert d2["openai_key_set"] is False
    assert d1["codex_model_provider"] == "ollama"
    assert d2["codex_model_provider"] == "openai"

    all_public = json.dumps({"u1": d1, "u2": d2, "config2": c2.get("/config").json()}, default=str)
    assert openai_key not in all_public
    assert gemini_key not in all_public
    assert "openai_api_key" not in d1
    assert "gemini_api_key" not in d1


def test_settings_openai_key_set_is_false_for_placeholder_value(_personal_keys_allowed):
    """openai_key_set はプレースホルダ（sk-REPLACE_ME）を未設定として返し、実キーは設定済みとして返す。"""
    sfx = _sfx()
    uid, pw = f"s4setph{sfx}", f"set-ph-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"openai_api_key": "sk-REPLACE_ME"}).status_code == 200
    assert c.get("/settings").json()["openai_key_set"] is False
    assert c.put("/settings", json={"openai_api_key": f"sk-real-{sfx}"}).status_code == 200
    assert c.get("/settings").json()["openai_key_set"] is True


def test_settings_test_openai_rejects_env_placeholder_without_probing(monkeypatch):
    """POST /settings/test（openai）は env キーがプレースホルダのままなら実 API へ probe せず早期に弾く。"""
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


# ===== 監査 =====

def test_audit_rows_for_security_ops_and_secret_redaction(_personal_keys_allowed):
    """主要 security op の audit row があり、password/token/API key は記録されない。"""
    sfx = _sfx()
    admin_uid, admin_pw = _mk_admin(sfx)
    owner_uid, owner_pw = f"s4auditown{sfx}", f"audit-owner-pw-{sfx}"
    invitee_uid, invitee_pw = f"s4auditinv{sfx}", f"audit-invitee-pw-{sfx}"
    bad_uid, bad_pw = f"s4auditmissing{sfx}", f"audit-bad-pw-{sfx}"
    reset_pw = f"audit-reset-pw-{sfx}"
    openai_key, gemini_key = f"sk-audit-openai-{sfx}", f"gemini-audit-key-{sfx}"

    admin = _login(admin_uid, admin_pw)
    failed = _client().post("/auth/login", json={"username": bad_uid, "password": bad_pw})
    assert failed.status_code == 401
    for uid, pw in ((owner_uid, owner_pw), (invitee_uid, invitee_pw)):
        _api_create_user(admin, uid, pw)
    assert admin.patch(f"/admin/users/{owner_uid}", json={"role": "admin"}).status_code == 200
    assert admin.patch(f"/admin/users/{invitee_uid}", json={"password": reset_pw}).status_code == 200

    owner_final_pw = f"audit-owner-final-{sfx}"
    owner = _login(owner_uid, owner_pw)
    _change_pw(owner, owner_pw, owner_final_pw)
    settings = owner.put("/settings", json={"openai_api_key": openai_key, "gemini_api_key": gemini_key,
                                            "agent": "simple"})
    assert settings.status_code == 200, settings.text

    cid = _new_conversation(owner_uid, sfx)
    share_id, share_url = _create_share(owner, cid, [invitee_uid], datetime.now(timezone.utc) + timedelta(days=1))
    share_token = share_url.rsplit("/", 1)[-1]

    invitee_final_pw = f"audit-invitee-final-{sfx}"
    invitee = _login(invitee_uid, reset_pw)
    _change_pw(invitee, reset_pw, invitee_final_pw)
    assert _click_share(invitee, share_url).status_code == 302
    assert owner.post(f"/conversation-shares/{share_id}/revoke").status_code == 200

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
    rows = []
    for action, filters in expectations:
        found = store.list_audit(action=action, limit=20, **filters)
        assert found, f"missing audit row for {action} with {filters}"
        rows.extend(found)
    _assert_no_secret_in_rows(
        rows, [admin_pw, owner_pw, invitee_pw, bad_pw, reset_pw, owner_final_pw, invitee_final_pw,
               openai_key, gemini_key, share_token])


def test_auth_disabled_compat_mode_without_login(auth_disabled):
    """SHERPA_AUTH_DISABLED=1 は cookie なしで admin 互換。"""
    r = _client().get("/auth/me")
    assert r.status_code == 200, r.text
    assert (r.json()["uid"], r.json()["role"]) == ("admin", "admin")
