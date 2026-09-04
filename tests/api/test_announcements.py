"""運営掲示板 API（2026-07-02-利用統計とホーム掲示板.md Feature 2）テスト。

- GET /announcements: ログイン必須・published のみ・pinned優先→新着順
- POST/PATCH/DELETE /admin/announcements: admin 専用（非admin 403・未ログイン 401）・バリデーション
- 監査: announcement.created / announcement.updated / announcement.deleted
- 互換モード（SHERPA_AUTH_DISABLED=1）でも動く

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


from _common import _login, _sfx, _try_init


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@ann.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）


def test_announcements_gates():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"annusr{sfx}", f"AnnUser{sfx}"
    _mk_user(uid, pw, role="user")

    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/announcements").status_code == 401
    assert anon.post("/admin/announcements", json={"title": "x", "body": "y"}).status_code == 401
    assert anon.patch("/admin/announcements/1", json={"title": "x"}).status_code == 401
    assert anon.delete("/admin/announcements/1").status_code == 401

    u = _login(uid, pw)
    assert u.get("/announcements").status_code == 200   # ログイン済みなら閲覧は誰でも可
    assert u.post("/admin/announcements", json={"title": "x", "body": "y"}).status_code == 403
    assert u.patch("/admin/announcements/1", json={"title": "x"}).status_code == 403
    assert u.delete("/admin/announcements/1").status_code == 403


def test_announcements_include_unpublished_requires_admin():
    """include_unpublished=1 は admin 専用（非公開記事の再発見・再公開のため）。
    非 admin は 403、admin は非公開記事も取得できる（既定=省略時は誰でも公開済みのみ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"anniu1{sfx}", f"AnnIu1{sfx}"
    user_uid, user_pw = f"anniu2{sfx}", f"AnnIu2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")
    admin = _login(admin_uid, admin_pw)
    viewer = _login(user_uid, user_pw)

    title = f"iu-hidden-{sfx}"
    rc = admin.post("/admin/announcements", json={"title": title, "body": "本文", "published": False})
    assert rc.status_code == 200, rc.text
    aid = rc.json()["announcement"]["id"]
    # limit=100（上限）で照会: 共有 dev DB に他テスト由来の記事があっても 1 ページ目から押し出されない。
    try:
        # 非 admin が include_unpublished=1 を付けると 403（閲覧自体は通常どおり可能・パラメータだけ拒否）。
        r_denied = viewer.get("/announcements?include_unpublished=1")
        assert r_denied.status_code == 403, r_denied.text

        # 非 admin の既定閲覧では非公開記事は見えない。
        r_viewer_default = viewer.get("/announcements?limit=100")
        assert r_viewer_default.status_code == 200
        assert title not in [a["title"] for a in r_viewer_default.json()["announcements"]]

        # admin は include_unpublished=1 で非公開記事も見える（再発見・再公開できる）。
        r_admin = admin.get("/announcements?include_unpublished=1&limit=100")
        assert r_admin.status_code == 200, r_admin.text
        assert title in [a["title"] for a in r_admin.json()["announcements"]]

        # admin でも include_unpublished を付けなければ既定どおり公開済みのみ。
        r_admin_default = admin.get("/announcements?limit=100")
        assert r_admin_default.status_code == 200
        assert title not in [a["title"] for a in r_admin_default.json()["announcements"]]
    finally:
        store.delete_announcement(aid)   # 後始末（残すと以後の実行で1ページ目を圧迫する）


def test_announcement_create_validation():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annadm{sfx}", f"AnnAdmin{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    assert admin.post("/admin/announcements", json={"title": "", "body": "本文"}).status_code == 422
    assert admin.post("/admin/announcements", json={"title": "件名", "body": ""}).status_code == 422
    assert admin.post("/admin/announcements",
                      json={"title": "件名", "body": "本文", "category": "invalid"}).status_code == 422


def test_announcement_crud_and_ordering_and_audit():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annadm2{sfx}", f"AnnAdmin2{sfx}"
    user_uid, user_pw = f"annview{sfx}", f"AnnView{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")
    admin = _login(admin_uid, admin_pw)
    viewer = _login(user_uid, user_pw)

    marker = f"掲示板テスト-{sfx}"
    made: list[int] = []   # 後始末用（残すと共有 dev DB にピン付き残骸が溜まり他テストの1ページ目を圧迫する）
    try:
        # 1件目（非ピン・お知らせ）
        r1 = admin.post("/admin/announcements", json={
            "title": f"{marker}-お知らせ", "body": "本文1\n2行目", "category": "notice"})
        assert r1.status_code == 200, r1.text
        a1 = r1.json()["announcement"]
        made.append(a1["id"])
        assert a1["author_uid"] == admin_uid and a1["pinned"] is False and a1["published"] is True

        # 2件目（ピン留め・メンテナンス）。作成は後だが pinned なので先頭に来るはず。
        r2 = admin.post("/admin/announcements", json={
            "title": f"{marker}-メンテ", "body": "メンテ本文", "category": "maintenance", "pinned": True})
        assert r2.status_code == 200, r2.text
        a2 = r2.json()["announcement"]
        made.append(a2["id"])
        assert a2["pinned"] is True

        # 3件目（活用事例・非公開で作成）
        r3 = admin.post("/admin/announcements", json={
            "title": f"{marker}-事例", "body": "事例本文", "category": "case", "published": False})
        assert r3.status_code == 200, r3.text
        a3 = r3.json()["announcement"]
        made.append(a3["id"])
        assert a3["published"] is False

        # 一般ユーザーの閲覧: published のみ・pinned 優先→新着順。
        lst = viewer.get("/announcements?limit=100").json()["announcements"]
        ids = [a["id"] for a in lst]
        assert a3["id"] not in ids, "非公開のお知らせが一般ユーザーに見えている"
        assert a1["id"] in ids and a2["id"] in ids
        assert ids.index(a2["id"]) < ids.index(a1["id"]), "pinned が新着より先頭に来ていない"
        by_id = {a["id"]: a for a in lst}
        assert by_id[a1["id"]]["body"] == "本文1\n2行目"   # 本文はそのまま返る（改行変換はフロント側の責務）

        # PATCH: タイトル・本文・カテゴリ更新
        rp = admin.patch(f"/admin/announcements/{a1['id']}",
                         json={"title": f"{marker}-更新後", "body": "更新本文", "category": "case"})
        assert rp.status_code == 200, rp.text
        updated = rp.json()["announcement"]
        assert updated["title"] == f"{marker}-更新後" and updated["category"] == "case"

        # PATCH: published=false で非公開化 → 一覧から消える
        rp2 = admin.patch(f"/admin/announcements/{a1['id']}", json={"published": False})
        assert rp2.status_code == 200, rp2.text
        assert rp2.json()["announcement"]["published"] is False
        lst2 = viewer.get("/announcements?limit=100").json()["announcements"]
        assert a1["id"] not in [a["id"] for a in lst2]

        # PATCH バリデーション: 空文字タイトル/不正カテゴリは拒否
        assert admin.patch(f"/admin/announcements/{a2['id']}", json={"title": ""}).status_code == 422
        assert admin.patch(f"/admin/announcements/{a2['id']}", json={"category": "bogus"}).status_code == 422

        # 存在しない id は 404
        assert admin.patch("/admin/announcements/999999999", json={"title": "x"}).status_code == 404
        assert admin.delete("/admin/announcements/999999999").status_code == 404

        # DELETE
        rd = admin.delete(f"/admin/announcements/{a3['id']}")
        assert rd.status_code == 200, rd.text
        assert store.get_announcement(a3["id"]) is None

        # 監査ログ
        created_rows = store.list_audit(actor=admin_uid, action="announcement.created", limit=10)
        assert len(created_rows) >= 3
        updated_rows = store.list_audit(actor=admin_uid, action="announcement.updated", limit=10)
        assert len(updated_rows) >= 2
        deleted_rows = store.list_audit(actor=admin_uid, action="announcement.deleted", limit=10)
        assert any(r["resource_id"] == f"announcement:{a3['id']}" for r in deleted_rows)
    finally:
        for _aid in made:                     # 冪等（削除済み id は no-op）
            store.delete_announcement(_aid)


def test_announcements_compat_mode(auth_disabled):
    if not _try_init():
        pytest.skip("DB down")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/admin/announcements", json={"title": "compat件名", "body": "compat本文"})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]
    try:                                     # RV LOW: 途中の assert 失敗でも記事を残さない（自己清掃）
        assert c.get("/announcements?limit=50").status_code == 200
        assert c.patch(f"/admin/announcements/{aid}", json={"pinned": True}).status_code == 200
        assert c.delete(f"/admin/announcements/{aid}").status_code == 200
    finally:
        store.delete_announcement(aid)       # 冪等（削除済み id は no-op）


# ===== RV HIGH 対応: 監査 fail-closed（監査に失敗した変更が成功したまま残らないこと） =====

def _boom(*_a, **_kw):
    raise RuntimeError("simulated audit failure")


def test_announcement_create_fail_closed_on_audit_failure(monkeypatch):
    """POST 時に監査書込が失敗したら、作成を取り消して 500 を返す（share.created と同じ compensate パターン）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annfc1{sfx}", f"AnnFc1{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr(store, "audit", _boom)
    title = f"fail-closed-create-{sfx}"
    r = admin.post("/admin/announcements", json={"title": title, "body": "本文"})
    assert r.status_code == 500, r.text

    all_rows = store.list_announcements(limit=500, offset=0, published_only=False)
    assert not any(a["title"] == title for a in all_rows), \
        "監査失敗時に作成が取り消されていない（fail-closed 違反: 監査できない変更が残っている）"


def test_announcement_update_fail_closed_restores_before_state(monkeypatch):
    """PATCH 時に監査書込が失敗したら、更新前の状態（updated_at まで含めて）へ完全に復元して 500 を返す。

    RV ラウンド2 MEDIUM: 以前の実装は update_announcement 経由で復元しており updated_at が
    now() に進んでしまっていた。restore_announcement_state で updated_at も元のまま戻ることを確認する。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annfc2{sfx}", f"AnnFc2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    orig_title = f"fail-closed-orig-{sfx}"
    rc = admin.post("/admin/announcements", json={"title": orig_title, "body": "元の本文", "category": "notice"})
    assert rc.status_code == 200, rc.text
    aid = rc.json()["announcement"]["id"]
    before = store.get_announcement(aid)
    assert before is not None

    try:
        monkeypatch.setattr(store, "audit", _boom)
        rp = admin.patch(f"/admin/announcements/{aid}",
                         json={"title": f"改ざん後-{sfx}", "body": "改ざん後本文", "category": "maintenance"})
        assert rp.status_code == 500, rp.text

        monkeypatch.undo()
        restored = store.get_announcement(aid)
        assert restored is not None
        assert restored["title"] == orig_title, "監査失敗時に更新前タイトルへ復元されていない"
        assert restored["body"] == "元の本文"
        assert restored["category"] == "notice"
        assert restored["id"] == before["id"], "補償後に id が変わった"
        assert restored["updated_at"] == before["updated_at"], \
            f"補償後に updated_at が元のまま復元されていない: {restored['updated_at']} != {before['updated_at']}"
        assert restored["created_at"] == before["created_at"]
    finally:
        store.delete_announcement(aid)   # 後始末（共有 dev DB に残骸を残さない）


def test_announcement_delete_fail_closed_restores_content(monkeypatch):
    """DELETE 時に監査書込が失敗したら、id/created_at/updated_at を含めて完全に復元して 500 を返す。

    RV ラウンド2 MEDIUM: 以前の実装は create_announcement で再作成しており id/created_at が
    新規発番されてしまっていた。store.restore_announcement で元の id/created_at/updated_at が
    そのまま戻ることを確認する。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annfc3{sfx}", f"AnnFc3{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    title = f"fail-closed-delete-{sfx}"
    rc = admin.post("/admin/announcements", json={"title": title, "body": "削除される予定の本文"})
    assert rc.status_code == 200, rc.text
    aid = rc.json()["announcement"]["id"]
    before = store.get_announcement(aid)
    assert before is not None

    try:
        monkeypatch.setattr(store, "audit", _boom)
        rd = admin.delete(f"/admin/announcements/{aid}")
        assert rd.status_code == 500, rd.text

        monkeypatch.undo()
        restored = store.get_announcement(aid)
        assert restored is not None, "監査失敗時に削除が取り消されていない（id がそのまま復元されていない）"
        assert restored["title"] == title and restored["body"] == "削除される予定の本文"
        assert restored["id"] == before["id"], "補償後に id が変わった（旧実装は create_announcement で新規採番していた）"
        assert restored["created_at"] == before["created_at"], "補償後に created_at が変わった"
        assert restored["updated_at"] == before["updated_at"], "補償後に updated_at が変わった"
    finally:
        store.delete_announcement(aid)   # 後始末（共有 dev DB に残骸を残さない）


# ===== S4: 掲示板の公開/削除タイマー（publish_at/expire_at） =====

def test_announcement_publish_at_and_expire_at_visibility_boundaries():
    """publish_at 未到来／expire_at 経過は一般ユーザーの一覧から消える。admin には status 付きで見える。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"anntm1{sfx}", f"AnnTm1{sfx}"
    user_uid, user_pw = f"anntm2{sfx}", f"AnnTm2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")
    admin = _login(admin_uid, admin_pw)
    viewer = _login(user_uid, user_pw)

    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=1)).isoformat()
    past = (now - timedelta(hours=1)).isoformat()
    marker = f"タイマー境界-{sfx}"
    made: list[int] = []
    try:
        r1 = admin.post("/admin/announcements", json={
            "title": f"{marker}-予約", "body": "本文", "publish_at": future})
        assert r1.status_code == 200, r1.text
        a1 = r1.json()["announcement"]; made.append(a1["id"])
        assert a1["status"] == "scheduled"

        r2 = admin.post("/admin/announcements", json={
            "title": f"{marker}-終了済", "body": "本文", "expire_at": past})
        assert r2.status_code == 200, r2.text
        a2 = r2.json()["announcement"]; made.append(a2["id"])
        assert a2["status"] == "expired"

        r3 = admin.post("/admin/announcements", json={
            "title": f"{marker}-公開中", "body": "本文", "publish_at": past, "expire_at": future})
        assert r3.status_code == 200, r3.text
        a3 = r3.json()["announcement"]; made.append(a3["id"])
        assert a3["status"] == "active"

        # 一般ユーザー: scheduled/expired は見えない・active だけ見える。
        lst = viewer.get("/announcements?limit=100").json()["announcements"]
        ids = [a["id"] for a in lst]
        assert a1["id"] not in ids, "publish_at 未到来なのに一般ユーザーに見えている"
        assert a2["id"] not in ids, "expire_at 経過なのに一般ユーザーに見えている"
        assert a3["id"] in ids

        # admin（include_unpublished=1）: 全部見える・status も正しい。
        admin_lst = admin.get("/announcements?include_unpublished=1&limit=100").json()["announcements"]
        by_id = {a["id"]: a for a in admin_lst}
        assert by_id[a1["id"]]["status"] == "scheduled"
        assert by_id[a2["id"]]["status"] == "expired"
        assert by_id[a3["id"]]["status"] == "active"
    finally:
        for _aid in made:
            store.delete_announcement(_aid)


def test_announcement_publish_after_expire_is_422_on_create_and_patch():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"anntm3{sfx}", f"AnnTm3{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    now = datetime.now(timezone.utc)
    later = (now + timedelta(hours=2)).isoformat()
    sooner = (now + timedelta(hours=1)).isoformat()

    r = admin.post("/admin/announcements", json={
        "title": f"bad-order-{sfx}", "body": "本文", "publish_at": later, "expire_at": sooner})
    assert r.status_code == 422, r.text
    assert "公開日時" in r.json()["detail"]

    r2 = admin.post("/admin/announcements", json={"title": f"patch-order-base-{sfx}", "body": "本文"})
    assert r2.status_code == 200, r2.text
    aid = r2.json()["announcement"]["id"]
    try:
        r3 = admin.patch(f"/admin/announcements/{aid}", json={"publish_at": later, "expire_at": sooner})
        assert r3.status_code == 422, r3.text
    finally:
        store.delete_announcement(aid)


def test_announcement_patch_empty_string_clears_expire_at_but_omitted_leaves_unchanged():
    """PATCH の publish_at/expire_at は書込専用キーと同じ流儀: 未指定=変更しない・""=NULLへクリア。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"anntm4{sfx}", f"AnnTm4{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = admin.post("/admin/announcements", json={"title": f"clear-me-{sfx}", "body": "本文", "expire_at": past})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]
    assert r.json()["announcement"]["status"] == "expired"
    try:
        # expire_at キー自体を送らない PATCH は変更しない。
        r2 = admin.patch(f"/admin/announcements/{aid}", json={"title": f"clear-me-2-{sfx}"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["announcement"]["expire_at"] is not None

        # 空文字は明示的に NULL へクリア（無期限へ戻る）。
        r3 = admin.patch(f"/admin/announcements/{aid}", json={"expire_at": ""})
        assert r3.status_code == 200, r3.text
        assert r3.json()["announcement"]["expire_at"] is None
        assert r3.json()["announcement"]["status"] == "active"
    finally:
        store.delete_announcement(aid)


def test_sweep_expired_announcements_deletes_and_audits_as_system():
    """S4: 期限切れ sweep が物理削除し、system:sweep 名義で announcement.expired_deleted を監査する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import api as api_mod

    sfx = _sfx()
    admin_uid, admin_pw = f"anntm5{sfx}", f"AnnTm5{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    title = f"sweep-me-{sfx}"
    r = admin.post("/admin/announcements", json={"title": title, "body": "本文", "expire_at": past})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]
    try:
        result = api_mod._sweep_expired_announcements()
        assert result.get("deleted", 0) >= 1
        assert store.get_announcement(aid) is None, "sweep が期限切れ記事を削除していない"

        rows = store.list_audit(actor="system:sweep", action="announcement.expired_deleted", limit=20)
        assert any(row["resource_id"] == f"announcement:{aid}" for row in rows), \
            "sweep の削除が system:sweep 名義で監査されていない"
    finally:
        store.delete_announcement(aid)   # 冪等（既に sweep で消えていれば no-op）


def test_sweep_expired_announcements_is_fail_open_on_audit_failure(monkeypatch):
    """RV 指示どおり sweep の監査は fail-open: 監査書込が失敗しても削除自体は成功したまま残る
    （announcement.created/updated/deleted の fail-closed とは意図的に異なる挙動）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import api as api_mod

    sfx = _sfx()
    admin_uid, admin_pw = f"anntm6{sfx}", f"AnnTm6{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    title = f"sweep-fail-open-{sfx}"
    r = admin.post("/admin/announcements", json={"title": title, "body": "本文", "expire_at": past})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]

    monkeypatch.setattr(store, "audit", _boom)
    try:
        result = api_mod._sweep_expired_announcements()
        assert result.get("deleted", 0) >= 1
        assert store.get_announcement(aid) is None, \
            "監査失敗時に sweep の削除が取り消されている（fail-open であるべき）"
    finally:
        monkeypatch.undo()
        store.delete_announcement(aid)   # 冪等（既に消えていれば no-op）


# ===== RV ラウンド2: 並行 PATCH の行ロック直列化・sweep の TOCTOU 対策・境界の等号 =====
# 別コネクションで対象行を明示的に FOR UPDATE ロックしたまま保持し、その間に本物の
# update_announcement / delete_expired_announcements がブロックされること（＝ロックが本当に
# 効いていること）と、ロック解放後の最終状態が「先に commit された方」を正しく反映することを
# 決定的に確認する（tests/api/test_auth_sharing.py の accept_share×delete_conversation と同じ流儀）。

def test_concurrent_patch_publish_and_expire_serializes_via_row_lock():
    """RV1 HIGH: 2つの PATCH が publish_at/expire_at を別々に同時更新しても、行ロックにより
    直列化され、後発側は先発側の commit 後の値で順序検証する（矛盾した状態を永続化しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annrl1{sfx}", f"AnnRl1{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    r = admin.post("/admin/announcements", json={"title": f"row-lock-{sfx}", "body": "本文"})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]

    now = datetime.now(timezone.utc)
    later_publish = (now + timedelta(hours=2)).isoformat()
    sooner_expire = (now + timedelta(hours=1)).isoformat()

    holder = psycopg.connect(store._dsn())
    try:
        holder.execute("SELECT id FROM announcements WHERE id=%s FOR UPDATE", (aid,))

        result: dict = {}

        def _run_patch():
            try:
                result["row"] = store.update_announcement(
                    aid, publish_at=datetime.fromisoformat(later_publish))
            except store.AnnouncementOrderError as e:
                result["error"] = e

        th = threading.Thread(target=_run_patch)
        th.start()
        th.join(timeout=0.5)
        assert th.is_alive(), (
            "update_announcement が対象行のロック中にブロックされていない"
            "（SELECT...FOR UPDATE 未導入 or 効いていない＝競合防止が機能していない）"
        )

        # ロック保持側が「別の PATCH が先に expire_at を確定させた」状況を模して commit。
        # sooner_expire は th が設定しようとしている later_publish より前＝矛盾する組み合わせになる。
        holder.execute("UPDATE announcements SET expire_at=%s WHERE id=%s",
                       (datetime.fromisoformat(sooner_expire), aid))
        holder.commit()
    finally:
        holder.close()

    th.join(timeout=5)
    assert not th.is_alive(), "update_announcement がロック解放後も完了しない"
    assert "error" in result, (
        "行ロック解放後、先発 PATCH が確定させた expire_at と矛盾する publish_at が "
        "AnnouncementOrderError を投げずに通ってしまった（RV1 の並行競合が再発している）"
    )
    assert isinstance(result["error"], store.AnnouncementOrderError)

    # 最終状態: th の publish_at 更新はロールバックされ、holder の expire_at だけが残る
    # （矛盾した組み合わせが永続化されていない）。
    final = store.get_announcement(aid)
    assert final["publish_at"] is None, "拒否されたはずの publish_at が永続化されている"
    assert final["expire_at"] is not None

    store.delete_announcement(aid)


def test_check_constraint_rejects_direct_sql_bypassing_the_api():
    """RV1 HIGH: アプリ層（FOR UPDATE 直列化）を経由しない直接 SQL でも、DB CHECK 制約
    （announcements_publish_before_expire）が最後の砦として不正な組み合わせを拒否する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    now = datetime.now(timezone.utc)
    later_publish = now + timedelta(hours=2)
    sooner_expire = now + timedelta(hours=1)

    with psycopg.connect(store._dsn()) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "INSERT INTO announcements (author_uid, title, body, publish_at, expire_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                ("admin", f"check-constraint-{sfx}", "本文", later_publish, sooner_expire))


def test_sweep_does_not_delete_row_whose_expiry_was_extended_concurrently():
    """RV2 HIGH: 列挙〜削除の間に admin が expire_at を延長した行は消えない（DELETE 文自体が
    削除の瞬間に条件を再評価するため・以前の「列挙してから無条件削除」だと消えていた）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annrl2{sfx}", f"AnnRl2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = admin.post("/admin/announcements", json={
        "title": f"toctou-{sfx}", "body": "本文", "expire_at": past})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    holder = psycopg.connect(store._dsn())
    try:
        holder.execute("SELECT id FROM announcements WHERE id=%s FOR UPDATE", (aid,))

        result: dict = {}

        def _run_sweep():
            from sherpa import api as api_mod
            result["rows"] = api_mod._sweep_expired_announcements()

        th = threading.Thread(target=_run_sweep)
        th.start()
        th.join(timeout=0.5)
        assert th.is_alive(), (
            "sweep（DELETE）が対象行のロック中にブロックされていない"
            "（DELETE...WHERE の行ロック取得が効いていない＝TOCTOU 対策が機能していない）"
        )

        # ロック保持側が「admin が期限を延長した」状況を模して commit（sweep より先に確定させる）。
        holder.execute("UPDATE announcements SET expire_at=%s WHERE id=%s",
                       (datetime.fromisoformat(future), aid))
        holder.commit()
    finally:
        holder.close()

    th.join(timeout=5)
    assert not th.is_alive(), "sweep がロック解放後も完了しない"

    try:
        assert store.get_announcement(aid) is not None, (
            "期限を延長した直後の行が sweep に巻き添えで削除された（RV2 の TOCTOU が再発している）"
        )
    finally:
        store.delete_announcement(aid)


def test_announcement_visibility_boundary_equals_now_uses_sql_clock():
    """RV6 LOW: publish_at==now は表示、expire_at==now は非表示（クエリの <= / > と一致）を
    DB 固定 timestamp（SQL 側 now()）を direct SQL で挿入して厳密に検証する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"annrl3{sfx}", f"AnnRl3{sfx}"
    user_uid, user_pw = f"annrl4{sfx}", f"AnnRl4{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")
    viewer = _login(user_uid, user_pw)

    title_pub_eq = f"boundary-publish-eq-{sfx}"
    title_exp_eq = f"boundary-expire-eq-{sfx}"
    made: list[int] = []
    try:
        with psycopg.connect(store._dsn(), row_factory=psycopg.rows.dict_row) as c:
            row1 = c.execute(
                "INSERT INTO announcements (author_uid, title, body, publish_at) "
                "VALUES ('admin', %s, '本文', now()) RETURNING id",
                (title_pub_eq,)).fetchone()
            row2 = c.execute(
                "INSERT INTO announcements (author_uid, title, body, expire_at) "
                "VALUES ('admin', %s, '本文', now()) RETURNING id",
                (title_exp_eq,)).fetchone()
        made = [row1["id"], row2["id"]]

        lst = viewer.get("/announcements?limit=100").json()["announcements"]
        titles = {a["title"] for a in lst}
        assert title_pub_eq in titles, "publish_at == now() は表示されるべき（クエリは publish_at<=now()）"
        assert title_exp_eq not in titles, "expire_at == now() は非表示のはず（クエリは expire_at>now()）"
    finally:
        for _aid in made:
            store.delete_announcement(_aid)
