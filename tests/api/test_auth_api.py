"""認証・ユーザー管理・会話共有 API 層のテスト（スライス2・docs/proposals/2026-07-01-認証実装計画.md）。

要 Postgres。DB 不可は graceful SKIP。
既定（conftest）のログイン必須モードで実際の認証フローを通す。
SHERPA_AUTH_DISABLED=1 の既存フローが壊れないことも末尾テストで確認する。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app

client = TestClient(app, raise_server_exceptions=True)


# ---- ヘルパ ----

def _sfx():
    """テスト実行ごとに一意な suffix（衝突防止）。"""
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> bool:
    """DB 初期化を試みる。失敗したら False を返す（SKIP 判定用）。"""
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")   # 不可なら可視の skip（silent-green 根絶）


def _mk_admin(sfx: str) -> tuple[str, str]:
    """テスト用 admin ユーザーを作成し (uid, password) を返す。"""
    uid = f"adm{sfx}"
    pw = f"pw-adm{sfx}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name="Admin Test",
                      password_hash=auth.hash_password(pw), role="admin", status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）
    return uid, pw


def _mk_user(sfx: str, role: str = "user") -> tuple[str, str]:
    uid = f"usr{sfx}"
    pw = f"pw-usr{sfx}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name="User Test",
                      password_hash=auth.hash_password(pw), role=role, status="active")
    register_test_uid(uid)
    return uid, pw


def _login(uid: str, pw: str) -> TestClient:
    """ログインして cookie 付き client を返す。"""
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"
    return client


# ===== テスト群 =====

def test_login_and_me():
    """ログイン→/auth/me→ログアウト の基本フロー。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_admin(sfx)

    # 正常ログイン
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    assert "sherpa_session" in r.cookies

    # /auth/me
    me = client.get("/auth/me", cookies=r.cookies)
    assert me.status_code == 200
    assert me.json()["uid"] == uid
    assert me.json()["role"] == "admin"

    # ログアウト
    lo = client.post("/auth/logout", cookies=r.cookies)
    assert lo.status_code == 200

    # ログアウト後は /auth/me が 401
    me2 = client.get("/auth/me", cookies=r.cookies)
    assert me2.status_code == 401


def test_wrong_password_401():
    """パスワード誤りは汎用 401（ユーザー存在を漏らさない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)

    r = client.post("/auth/login", json={"username": uid, "password": "wrong"})
    assert r.status_code == 401
    # 汎用メッセージ（ユーザー存在を漏らさない）
    assert "パスワード" in r.json().get("detail", "")

    # 存在しないユーザーも同じ 401
    r2 = client.post("/auth/login", json={"username": "no_such_user_xyz", "password": "x"})
    assert r2.status_code == 401


def test_must_change_password_blocks_app_until_changed():
    """初回変更待ちユーザーは通常APIへ進めず、変更後に解除される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"chg{sfx}"
    pw = f"pw-chg{sfx}"
    new_pw = f"BetterPass{sfx}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name="Change Required",
                      password_hash=auth.hash_password(pw), role="user", status="active",
                      must_change_password=True)
    register_test_uid(uid)

    lr = client.post("/auth/login", json={"username": uid, "password": pw})
    assert lr.status_code == 200, lr.text
    assert lr.json()["must_change_password"] is True

    me = client.get("/auth/me", cookies=lr.cookies)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True

    blocked = client.get("/settings", cookies=lr.cookies)
    assert blocked.status_code == 403

    weak = client.post("/auth/change-password",
                       json={"current_password": pw, "new_password": "password123",
                             "confirm_password": "password123"},
                       cookies=lr.cookies)
    assert weak.status_code == 422

    ok = client.post("/auth/change-password",
                     json={"current_password": pw, "new_password": new_pw,
                           "confirm_password": new_pw},
                     cookies=lr.cookies)
    assert ok.status_code == 200, ok.text
    assert ok.json()["must_change_password"] is False

    after = client.get("/settings", cookies=lr.cookies)
    assert after.status_code == 200, after.text
    row = store.get_user_by_uid(uid)
    assert row["must_change_password"] is False
    assert auth.verify_password(new_pw, row["password_hash"])


def test_admin_creates_user():
    """admin が /admin/users でユーザーを作成できる。non-admin は 403。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    usr_uid, usr_pw = _mk_user(sfx)

    # admin ログイン
    lr = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw})
    adm_cookies = lr.cookies

    # ユーザー一覧
    lu = client.get("/admin/users", cookies=adm_cookies)
    assert lu.status_code == 200
    uids = [u["uid"] for u in lu.json()["users"]]
    assert adm_uid in uids

    # 新規ユーザー作成
    new_uid = f"new{sfx}"
    cr = client.post("/admin/users",
                     json={"uid": new_uid, "display_name": "New", "role": "user", "password": "pass1234"},
                     cookies=adm_cookies)
    assert cr.status_code == 200, cr.text
    assert cr.json()["user"]["uid"] == new_uid
    register_test_uid(new_uid)   # API 経由で作成した uid もテストユーザー残骸防止の対象にする

    # non-admin が /admin/* → 403
    ulr = client.post("/auth/login", json={"username": usr_uid, "password": usr_pw})
    usr_cookies = ulr.cookies
    for path in ("/admin/users",):
        res = client.get(path, cookies=usr_cookies)
        assert res.status_code == 403, f"{path} should be 403 for non-admin"


def test_admin_create_user_rejects_duplicate_uid_and_does_not_overwrite():
    """RV「バッチ2」4番（2026-07-03）: 既存 uid（無効化済み含む）へ「作成」すると 409 で拒否し、
    既存ユーザーのパスワード/権限は変更されない（従来は POST が store.upsert_user 経由で
    黙って上書きしていた事故の再発防止・修正前に落ちるテストで実証する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    lr = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw})
    adm_cookies = lr.cookies

    # 既存ユーザー（無効化済み）を用意。
    victim_uid = f"victim{sfx}"
    original_pw = f"orig-pw-{sfx}"
    store.upsert_user(victim_uid, email=f"{victim_uid}@ex.local", display_name="Victim",
                      password_hash=auth.hash_password(original_pw), role="user", status="disabled")
    register_test_uid(victim_uid)
    before = store.get_user_by_uid(victim_uid)

    # 同じ uid で admin 権限として「作成」を試みる（乗っ取り試行を模す）。
    cr = client.post("/admin/users",
                     json={"uid": victim_uid, "display_name": "Attacker", "role": "admin",
                           "password": "attacker-pass-1"},
                     cookies=adm_cookies)
    assert cr.status_code == 409, cr.text
    assert "既に存在します" in cr.json()["detail"]

    after = store.get_user_by_uid(victim_uid)
    assert after["role"] == "user", "role が上書きされてしまっている"
    assert after["status"] == "disabled", "status が上書きされてしまっている"
    assert after["password_hash"] == before["password_hash"], "パスワードが上書きされてしまっている"
    assert auth.verify_password(original_pw, after["password_hash"])
    assert not auth.verify_password("attacker-pass-1", after["password_hash"])


def test_store_create_user_returns_none_on_existing_uid_without_upserting():
    """store.create_user 単体: 既存 uid では None を返し、行は一切変更しない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    before = store.get_user_by_uid(uid)

    result = store.create_user(uid, display_name="Should Not Apply",
                               password_hash=auth.hash_password("should-not-apply"), role="admin")
    assert result is None

    after = store.get_user_by_uid(uid)
    assert after["role"] == before["role"]
    assert after["password_hash"] == before["password_hash"]
    assert after["display_name"] == before["display_name"]


def test_share_create_click_wrapper_and_read():
    """共有作成→invitee がクリック→受領ラッパー生成→本文読取→append 拒否。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"o{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"i{sfx}")

    # オーナーが会話を作成
    conv = store.create_conversation(user_id=owner_uid, world="v1", title="テスト会話")
    cid = conv["id"]
    store.add_message(cid, "user", "テスト質問")
    store.add_message(cid, "assistant", "テスト回答")

    # オーナーログイン
    olr = client.post("/auth/login", json={"username": owner_uid, "password": owner_pw})
    owner_cookies = olr.cookies

    # 共有作成
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    sr = client.post(f"/conversations/{cid}/shares",
                     json={"invitee_user_ids": [invitee_uid], "expires_at": expires},
                     cookies=owner_cookies)
    assert sr.status_code == 200, sr.text
    share_url = sr.json()["url"]
    assert share_url.startswith("/share/conversations/")
    share_id = sr.json()["share_id"]
    token = share_url.split("/")[-1]

    # invitee ログイン
    ilr = client.post("/auth/login", json={"username": invitee_uid, "password": invitee_pw})
    inv_cookies = ilr.cookies

    # invitee がリンクをクリック（redirect → /ui/chat.html）
    cr = client.get(share_url, cookies=inv_cookies, follow_redirects=False)
    assert cr.status_code in (200, 302), f"share click failed: {cr.status_code} {cr.text}"
    # 受領ラッパーが invitee の会話一覧に出る
    convs = store.list_conversations(invitee_uid)
    received = [c for c in convs if c["origin"] == "received_share"]
    assert len(received) == 1, f"expected 1 received_share, got {received}"
    wid = received[0]["id"]

    # 受領ラッパーから本文が読める（GET /conversations/{wid}）
    gr = client.get(f"/conversations/{wid}", cookies=inv_cookies)
    assert gr.status_code == 200, gr.text
    msgs = gr.json()["messages"]
    assert len(msgs) == 2

    # received_share への追記は 403
    append_r = client.post("/chat",
                           json={"message": "追記", "world": "v1", "conversation_id": wid},
                           cookies=inv_cookies)
    assert append_r.status_code == 403, f"append to received_share should be 403, got {append_r.status_code}"


def test_uninvited_click_denied():
    """招待外ユーザーがリンクをクリック→403。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"ow{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"iv{sfx}")
    other_uid, other_pw = _mk_user(f"ot{sfx}")

    conv = store.create_conversation(user_id=owner_uid, world="v1", title="共有会話")
    cid = conv["id"]

    olr = client.post("/auth/login", json={"username": owner_uid, "password": owner_pw})
    owner_cookies = olr.cookies

    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    sr = client.post(f"/conversations/{cid}/shares",
                     json={"invitee_user_ids": [invitee_uid], "expires_at": expires},
                     cookies=owner_cookies)
    assert sr.status_code == 200
    share_url = sr.json()["url"]

    # 招待外ユーザーがクリック→403
    otlr = client.post("/auth/login", json={"username": other_uid, "password": other_pw})
    other_cookies = otlr.cookies
    cr = client.get(share_url, cookies=other_cookies, follow_redirects=False)
    assert cr.status_code == 403, f"uninvited click should be 403, got {cr.status_code}"


def test_revoke_makes_share_unavailable():
    """共有取消後は accepted invitee も開けなくなる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"ro{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"ri{sfx}")

    conv = store.create_conversation(user_id=owner_uid, world="v1", title="取消テスト")
    cid = conv["id"]
    store.add_message(cid, "user", "メッセージ")

    olr = client.post("/auth/login", json={"username": owner_uid, "password": owner_pw})
    owner_cookies = olr.cookies
    ilr = client.post("/auth/login", json={"username": invitee_uid, "password": invitee_pw})
    inv_cookies = ilr.cookies

    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    sr = client.post(f"/conversations/{cid}/shares",
                     json={"invitee_user_ids": [invitee_uid], "expires_at": expires},
                     cookies=owner_cookies)
    assert sr.status_code == 200
    share_url = sr.json()["url"]
    share_id = sr.json()["share_id"]

    # クリックして受領
    client.get(share_url, cookies=inv_cookies, follow_redirects=False)

    # 取消
    rv = client.post(f"/conversation-shares/{share_id}/revoke", cookies=owner_cookies)
    assert rv.status_code == 200

    # 取消後: invitee が受領ラッパーを読もうとすると unavailable（share_status = unavailable）
    convs = store.list_conversations(invitee_uid)
    received = [c for c in convs if c["origin"] == "received_share"]
    if received:
        wid = received[0]["id"]
        gr = client.get(f"/conversations/{wid}", cookies=inv_cookies)
        data = gr.json()
        # 取消後は share_status = unavailable でメッセージ空
        assert data.get("share_status") == "unavailable" or data.get("messages") == []


def test_share_create_without_expires_at_is_unlimited_and_survives_source_delete():
    """2026-07-02-共有の無期限と永続化.md: expires_at 省略（無期限）で作成→click 成功、
    かつ共有元を削除しても受領側は読める（API 経由のエンドツーエンド確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"ueo{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"uei{sfx}")

    conv = store.create_conversation(user_id=owner_uid, world="v1", title="無期限共有API")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    store.add_message(cid, "assistant", "回答")

    olr = client.post("/auth/login", json={"username": owner_uid, "password": owner_pw})
    owner_cookies = olr.cookies

    # expires_at を省略（Optional・省略時は None=無期限）。
    sr = client.post(f"/conversations/{cid}/shares",
                     json={"invitee_user_ids": [invitee_uid]},
                     cookies=owner_cookies)
    assert sr.status_code == 200, sr.text
    share_url = sr.json()["url"]

    ilr = client.post("/auth/login", json={"username": invitee_uid, "password": invitee_pw})
    inv_cookies = ilr.cookies
    cr = client.get(share_url, cookies=inv_cookies, follow_redirects=False)
    assert cr.status_code in (200, 302), f"unlimited share click failed: {cr.status_code} {cr.text}"

    convs = store.list_conversations(invitee_uid)
    received = [c for c in convs if c["origin"] == "received_share"]
    assert received and received[0]["share_status"] == "active"
    wid = received[0]["id"]

    # 共有元（owner の元会話）を削除しても、生きたラッパーがあるため soft delete され受領側は読める。
    dr = client.delete(f"/conversations/{cid}", cookies=owner_cookies)
    assert dr.status_code == 200, dr.text

    gr = client.get(f"/conversations/{wid}", cookies=inv_cookies)
    assert gr.status_code == 200, gr.text
    assert len(gr.json()["messages"]) == 2, "元会話削除後に受領側が読めなくなった"

    # 所有者の一覧からは消える。
    owner_list = client.get("/conversations", cookies=owner_cookies)
    assert owner_list.status_code == 200
    assert cid not in [c["id"] for c in owner_list.json()], "soft delete 後も所有者一覧に残っている"


def test_soft_deleted_conversation_rejects_all_owner_operations():
    """RV ラウンド2 MEDIUM: soft-delete 後（生きた受領ラッパーがあり物理削除されない会話）は
    GET・PIN・タイトルPATCH・共有作成の全ての owner 操作が拒否される（deleted_at フィルタ横串確認）。
    set_pinned は以前 deleted_at を見ておらず PIN だけ 200 を返していた不具合の回帰テスト。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"sdo{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"sdi{sfx}")
    stranger_uid, stranger_pw = _mk_user(f"sds{sfx}")

    conv = store.create_conversation(user_id=owner_uid, world="v1", title="soft-delete後は全拒否")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    store.add_message(cid, "assistant", "回答")

    olr = client.post("/auth/login", json={"username": owner_uid, "password": owner_pw})
    owner_cookies = olr.cookies

    # 生きた受領ラッパーを作る（削除時に soft-delete 分岐に入る条件）。
    sr = client.post(f"/conversations/{cid}/shares", json={"invitee_user_ids": [invitee_uid]},
                     cookies=owner_cookies)
    assert sr.status_code == 200, sr.text
    ilr = client.post("/auth/login", json={"username": invitee_uid, "password": invitee_pw})
    client.get(sr.json()["url"], cookies=ilr.cookies, follow_redirects=False)

    dr = client.delete(f"/conversations/{cid}", cookies=owner_cookies)
    assert dr.status_code == 200, dr.text

    # GET: 404
    gr = client.get(f"/conversations/{cid}", cookies=owner_cookies)
    assert gr.status_code == 404, f"soft-delete 後の GET が 404 でない: {gr.status_code}"

    # PIN: 404（RV ラウンド2 の本題 — set_pinned に deleted_at IS NULL が無いと 200 になっていた）。
    pr = client.post(f"/conversations/{cid}/pin", json={"pinned": True}, cookies=owner_cookies)
    assert pr.status_code == 404, f"soft-delete 後の PIN が 404 でない（deleted_at フィルタ漏れ）: {pr.status_code}"

    # タイトル PATCH: 404
    rr = client.patch(f"/conversations/{cid}", json={"title": "改ざんタイトル"}, cookies=owner_cookies)
    assert rr.status_code == 404, f"soft-delete 後の rename が 404 でない: {rr.status_code}"

    # 共有作成: 403（owns_conversation が deleted_at IS NULL で弾く）
    sr2 = client.post(f"/conversations/{cid}/shares", json={"invitee_user_ids": [stranger_uid]},
                      cookies=owner_cookies)
    assert sr2.status_code in (403, 409), \
        f"soft-delete 後の共有作成が拒否されない: {sr2.status_code} {sr2.text}"


def test_settings_isolated_per_user():
    """settings はユーザーごとに独立している。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    u1, p1 = _mk_user(f"s1{sfx}")
    u2, p2 = _mk_user(f"s2{sfx}")

    l1 = client.post("/auth/login", json={"username": u1, "password": p1})
    l2 = client.post("/auth/login", json={"username": u2, "password": p2})

    # u1 の settings を更新（system_prompt は個人設定の自由入力欄・値の一意性で隔離を確認できる）。
    put1 = client.put("/settings",
                      json={"system_prompt": f"prompt-{sfx}"},
                      cookies=l1.cookies)
    assert put1.status_code == 200

    # u2 の settings は影響なし
    get2 = client.get("/settings", cookies=l2.cookies)
    assert get2.status_code == 200
    assert get2.json()["system_prompt"] != f"prompt-{sfx}"


def test_audit_row_written_for_login_and_share():
    """login と share.created の audit レコードが DB に書かれている。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"au{sfx}")
    inv_uid, inv_pw = _mk_user(f"av{sfx}")

    lr = client.post("/auth/login", json={"username": owner_uid, "password": owner_pw})
    assert lr.status_code == 200

    conv = store.create_conversation(user_id=owner_uid, world="v1", title="監査テスト会話")
    cid = conv["id"]
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    sr = client.post(f"/conversations/{cid}/shares",
                     json={"invitee_user_ids": [inv_uid], "expires_at": expires},
                     cookies=lr.cookies)
    assert sr.status_code == 200

    # DB で audit_log を確認（store.list_audit 経由・専用の生 DSN 接続は使わない）。
    rows = store.list_audit(actor=owner_uid, limit=10)
    actions = [r["action"] for r in rows]
    assert "auth.login" in actions, f"auth.login not in audit: {actions}"
    assert "share.created" in actions, f"share.created not in audit: {actions}"


def test_admin_audit_endpoint():
    """GET /admin/audit: admin は閲覧可・non-admin は 403・閲覧自体が admin.audit_viewed として記録される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    usr_uid, usr_pw = _mk_user(sfx)

    # admin ログイン
    adm_lr = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw})
    assert adm_lr.status_code == 200, adm_lr.text
    adm_cookies = adm_lr.cookies

    # admin は /admin/audit にアクセスできる（少なくとも HTTP 200 を返す）。
    r = client.get("/admin/audit?limit=10", cookies=adm_cookies)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "rows" in data
    assert isinstance(data["rows"], list)

    # non-admin は 403
    usr_lr = client.post("/auth/login", json={"username": usr_uid, "password": usr_pw})
    assert usr_lr.status_code == 200
    usr_cookies = usr_lr.cookies
    deny_r = client.get("/admin/audit?limit=10", cookies=usr_cookies)
    assert deny_r.status_code == 403, f"non-admin should be 403, got {deny_r.status_code}"

    # 閲覧後に admin.audit_viewed 行が DB に書かれている。
    rows = store.list_audit(actor=adm_uid, action="admin.audit_viewed", limit=10)
    assert any(r["action"] == "admin.audit_viewed" for r in rows), \
        f"admin.audit_viewed not found in audit_log for {adm_uid}: {rows}"


def test_auth_disabled_compat(auth_disabled):
    """SHERPA_AUTH_DISABLED=1 で既存の admin フローが動く（regression）。"""
    # cookie なしでも /auth/me が admin を返す
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["uid"] == "admin"
    assert me.json()["role"] == "admin"


def test_admin_created_user_must_change_initial_password():
    """PW-1（実環境指摘 2026-09-02）: 管理者が作成したユーザーは初期パスワードのままログイン
    すると must_change_password=True＝初回変更が強制される（従来は FALSE 固定で強制されず、
    変更ページ側も「フラグ無しは即リダイレクト」だったため任意変更すらできなかった）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    adm_cookies = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw}).cookies
    new_uid = f"pwinit{sfx}"
    cr = client.post("/admin/users",
                     json={"uid": new_uid, "display_name": "PwInit", "role": "user",
                           "password": "init-pass-1"},
                     cookies=adm_cookies)
    assert cr.status_code == 200, cr.text
    register_test_uid(new_uid)
    lr = client.post("/auth/login", json={"username": new_uid, "password": "init-pass-1"})
    assert lr.status_code == 200, lr.text
    assert lr.json()["must_change_password"] is True


def test_admin_password_reset_forces_change_on_next_login():
    """PW-1: 管理者によるパスワード再設定（PATCH /admin/users/{uid}）でも次回ログインで
    変更を強制する（新パスワードを管理者が知っている状態を放置しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    usr_uid, usr_pw = _mk_user(sfx)
    assert store.get_user_by_uid(usr_uid)["must_change_password"] is False
    adm_cookies = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw}).cookies
    pr = client.patch(f"/admin/users/{usr_uid}", json={"password": f"reset-pass-{sfx}"},
                      cookies=adm_cookies)
    assert pr.status_code == 200, pr.text
    row = store.get_user_by_uid(usr_uid)
    assert row["must_change_password"] is True
    lr = client.post("/auth/login", json={"username": usr_uid, "password": f"reset-pass-{sfx}"})
    assert lr.status_code == 200, lr.text
    assert lr.json()["must_change_password"] is True
