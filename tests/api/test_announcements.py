"""運営掲示板 API のテスト。

- GET /announcements: ログイン必須・published のみ・pinned優先→新着順
- POST/PATCH/DELETE /admin/announcements: admin 専用（非admin 403・未ログイン 401）・バリデーション
- 監査: announcement.created / updated / deleted（失敗時は fail-closed で元に戻す）
- 公開/削除タイマー（publish_at/expire_at）・行ロックによる直列化
- 互換モード（SHERPA_AUTH_DISABLED=1）でも動く

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import threading
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
    register_test_uid(uid)


@pytest.fixture
def sfx():
    if not _try_init():
        pytest.skip("DB down")
    return _sfx()


@pytest.fixture
def admin_uid(sfx):
    return f"annadm{sfx}"


@pytest.fixture
def admin(admin_uid, sfx):
    _mk_user(admin_uid, f"AnnAdmin{sfx}", role="admin")
    return _login(admin_uid, f"AnnAdmin{sfx}")


@pytest.fixture
def viewer(sfx):
    _mk_user(f"annview{sfx}", f"AnnView{sfx}", role="user")
    return _login(f"annview{sfx}", f"AnnView{sfx}")


@pytest.fixture
def made():
    """作った記事の id。共有 dev DB に残骸を残さないよう終了時に消す（削除済み id は no-op）。"""
    ids: list[int] = []
    yield ids
    for aid in ids:
        store.delete_announcement(aid)


def _create(admin, made, title, body="本文", **extra):
    r = admin.post("/admin/announcements", json={"title": title, "body": body, **extra})
    assert r.status_code == 200, r.text
    a = r.json()["announcement"]
    made.append(a["id"])
    return a


def _ids(client, query="?limit=100"):
    return [a["id"] for a in client.get(f"/announcements{query}").json()["announcements"]]


def _iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _boom(*_a, **_kw):
    raise RuntimeError("simulated audit failure")


# ---- 権限・閲覧 ----

def test_announcements_gates(viewer):
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/announcements").status_code == 401
    assert anon.post("/admin/announcements", json={"title": "x", "body": "y"}).status_code == 401
    assert anon.patch("/admin/announcements/1", json={"title": "x"}).status_code == 401
    assert anon.delete("/admin/announcements/1").status_code == 401

    assert viewer.get("/announcements").status_code == 200   # ログイン済みなら閲覧は誰でも可
    assert viewer.post("/admin/announcements", json={"title": "x", "body": "y"}).status_code == 403
    assert viewer.patch("/admin/announcements/1", json={"title": "x"}).status_code == 403
    assert viewer.delete("/admin/announcements/1").status_code == 403


def test_announcements_include_unpublished_requires_admin(admin, viewer, sfx, made):
    title = f"iu-hidden-{sfx}"
    _create(admin, made, title, published=False)

    def titles(client, query):
        r = client.get(f"/announcements?{query}")
        assert r.status_code == 200, r.text
        return [a["title"] for a in r.json()["announcements"]]

    # limit=100（上限）で照会: 共有 dev DB の他記事に 1 ページ目から押し出されない。
    r_denied = viewer.get("/announcements?include_unpublished=1")
    assert r_denied.status_code == 403, r_denied.text
    assert title not in titles(viewer, "limit=100")
    assert title in titles(admin, "include_unpublished=1&limit=100")
    assert title not in titles(admin, "limit=100")   # admin でも既定は公開済みのみ


def test_announcement_create_validation(admin):
    assert admin.post("/admin/announcements", json={"title": "", "body": "本文"}).status_code == 422
    assert admin.post("/admin/announcements", json={"title": "件名", "body": ""}).status_code == 422
    assert admin.post("/admin/announcements",
                      json={"title": "件名", "body": "本文", "category": "invalid"}).status_code == 422


def test_announcement_crud_and_ordering_and_audit(admin, admin_uid, viewer, sfx, made):
    marker = f"掲示板テスト-{sfx}"
    a1 = _create(admin, made, f"{marker}-お知らせ", "本文1\n2行目", category="notice")
    assert a1["author_uid"] == admin_uid and a1["pinned"] is False and a1["published"] is True
    # 作成は後だが pinned なので先頭に来る
    a2 = _create(admin, made, f"{marker}-メンテ", "メンテ本文", category="maintenance", pinned=True)
    assert a2["pinned"] is True
    a3 = _create(admin, made, f"{marker}-事例", "事例本文", category="case", published=False)
    assert a3["published"] is False

    lst = viewer.get("/announcements?limit=100").json()["announcements"]
    ids = [a["id"] for a in lst]
    assert a3["id"] not in ids, "非公開のお知らせが一般ユーザーに見えている"
    assert a1["id"] in ids and a2["id"] in ids
    assert ids.index(a2["id"]) < ids.index(a1["id"]), "pinned が新着より先頭に来ていない"
    assert {a["id"]: a for a in lst}[a1["id"]]["body"] == "本文1\n2行目"   # 改行変換はフロント側の責務

    rp = admin.patch(f"/admin/announcements/{a1['id']}",
                     json={"title": f"{marker}-更新後", "body": "更新本文", "category": "case"})
    assert rp.status_code == 200, rp.text
    updated = rp.json()["announcement"]
    assert updated["title"] == f"{marker}-更新後" and updated["category"] == "case"

    rp2 = admin.patch(f"/admin/announcements/{a1['id']}", json={"published": False})   # 非公開化→一覧から消える
    assert rp2.status_code == 200, rp2.text
    assert rp2.json()["announcement"]["published"] is False
    assert a1["id"] not in _ids(viewer)

    assert admin.patch(f"/admin/announcements/{a2['id']}", json={"title": ""}).status_code == 422
    assert admin.patch(f"/admin/announcements/{a2['id']}", json={"category": "bogus"}).status_code == 422
    assert admin.patch("/admin/announcements/999999999", json={"title": "x"}).status_code == 404
    assert admin.delete("/admin/announcements/999999999").status_code == 404

    rd = admin.delete(f"/admin/announcements/{a3['id']}")
    assert rd.status_code == 200, rd.text
    assert store.get_announcement(a3["id"]) is None

    assert len(store.list_audit(actor=admin_uid, action="announcement.created", limit=10)) >= 3
    assert len(store.list_audit(actor=admin_uid, action="announcement.updated", limit=10)) >= 2
    deleted_rows = store.list_audit(actor=admin_uid, action="announcement.deleted", limit=10)
    assert any(r["resource_id"] == f"announcement:{a3['id']}" for r in deleted_rows)


def test_announcements_compat_mode(auth_disabled, sfx):
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/admin/announcements", json={"title": "compat件名", "body": "compat本文"})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]
    try:
        assert c.get("/announcements?limit=50").status_code == 200
        assert c.patch(f"/admin/announcements/{aid}", json={"pinned": True}).status_code == 200
        assert c.delete(f"/admin/announcements/{aid}").status_code == 200
    finally:
        store.delete_announcement(aid)


# ---- 監査 fail-closed（監査に失敗した変更が成功したまま残らないこと） ----

def test_announcement_create_fail_closed_on_audit_failure(admin, sfx, monkeypatch):
    monkeypatch.setattr(store, "audit", _boom)
    title = f"fail-closed-create-{sfx}"
    r = admin.post("/admin/announcements", json={"title": title, "body": "本文"})
    assert r.status_code == 500, r.text

    all_rows = store.list_announcements(limit=500, offset=0, published_only=False)
    assert not any(a["title"] == title for a in all_rows), \
        "監査失敗時に作成が取り消されていない（fail-closed 違反）"


def test_announcement_update_fail_closed_restores_before_state(admin, sfx, made, monkeypatch):
    """更新前の状態（updated_at まで含めて）へ完全に復元して 500 を返す。"""
    orig_title = f"fail-closed-orig-{sfx}"
    aid = _create(admin, made, orig_title, "元の本文", category="notice")["id"]
    before = store.get_announcement(aid)
    assert before is not None

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


def test_announcement_delete_fail_closed_restores_content(admin, sfx, made, monkeypatch):
    """id/created_at/updated_at を含めて完全に復元して 500 を返す（再作成で新規採番しない）。"""
    title = f"fail-closed-delete-{sfx}"
    aid = _create(admin, made, title, "削除される予定の本文")["id"]
    before = store.get_announcement(aid)
    assert before is not None

    monkeypatch.setattr(store, "audit", _boom)
    rd = admin.delete(f"/admin/announcements/{aid}")
    assert rd.status_code == 500, rd.text

    monkeypatch.undo()
    restored = store.get_announcement(aid)
    assert restored is not None, "監査失敗時に削除が取り消されていない"
    assert restored["title"] == title and restored["body"] == "削除される予定の本文"
    assert restored["id"] == before["id"], "補償後に id が変わった"
    assert restored["created_at"] == before["created_at"], "補償後に created_at が変わった"
    assert restored["updated_at"] == before["updated_at"], "補償後に updated_at が変わった"


# ---- 公開/削除タイマー（publish_at/expire_at） ----

def test_announcement_publish_at_and_expire_at_visibility_boundaries(admin, viewer, sfx, made):
    """publish_at 未到来／expire_at 経過は一般ユーザーの一覧から消える。admin には status 付きで見える。"""
    marker = f"タイマー境界-{sfx}"
    a1 = _create(admin, made, f"{marker}-予約", publish_at=_iso(hours=1))
    a2 = _create(admin, made, f"{marker}-終了済", expire_at=_iso(hours=-1))
    a3 = _create(admin, made, f"{marker}-公開中", publish_at=_iso(hours=-1), expire_at=_iso(hours=1))
    assert (a1["status"], a2["status"], a3["status"]) == ("scheduled", "expired", "active")

    ids = _ids(viewer)
    assert a1["id"] not in ids, "publish_at 未到来なのに一般ユーザーに見えている"
    assert a2["id"] not in ids, "expire_at 経過なのに一般ユーザーに見えている"
    assert a3["id"] in ids

    by_id = {a["id"]: a for a in
             admin.get("/announcements?include_unpublished=1&limit=100").json()["announcements"]}
    assert [by_id[a["id"]]["status"] for a in (a1, a2, a3)] == ["scheduled", "expired", "active"]


def test_announcement_publish_after_expire_is_422_on_create_and_patch(admin, sfx, made):
    later, sooner = _iso(hours=2), _iso(hours=1)
    r = admin.post("/admin/announcements", json={
        "title": f"bad-order-{sfx}", "body": "本文", "publish_at": later, "expire_at": sooner})
    assert r.status_code == 422, r.text
    assert "公開日時" in r.json()["detail"]

    aid = _create(admin, made, f"patch-order-base-{sfx}")["id"]
    r3 = admin.patch(f"/admin/announcements/{aid}", json={"publish_at": later, "expire_at": sooner})
    assert r3.status_code == 422, r3.text


def test_announcement_patch_empty_string_clears_expire_at_but_omitted_leaves_unchanged(admin, sfx, made):
    """PATCH の publish_at/expire_at: 未指定=変更しない・""=NULLへクリア。"""
    a = _create(admin, made, f"clear-me-{sfx}", expire_at=_iso(hours=-1))
    aid = a["id"]
    assert a["status"] == "expired"

    r2 = admin.patch(f"/admin/announcements/{aid}", json={"title": f"clear-me-2-{sfx}"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["announcement"]["expire_at"] is not None

    r3 = admin.patch(f"/admin/announcements/{aid}", json={"expire_at": ""})
    assert r3.status_code == 200, r3.text
    assert r3.json()["announcement"]["expire_at"] is None
    assert r3.json()["announcement"]["status"] == "active"


def test_sweep_expired_announcements_deletes_and_audits_as_system(admin, sfx, made):
    from sherpa import api as api_mod
    aid = _create(admin, made, f"sweep-me-{sfx}", expire_at=_iso(hours=-1))["id"]

    result = api_mod._sweep_expired_announcements()
    assert result.get("deleted", 0) >= 1
    assert store.get_announcement(aid) is None, "sweep が期限切れ記事を削除していない"

    rows = store.list_audit(actor="system:sweep", action="announcement.expired_deleted", limit=20)
    assert any(row["resource_id"] == f"announcement:{aid}" for row in rows), \
        "sweep の削除が system:sweep 名義で監査されていない"


def test_sweep_expired_announcements_is_fail_open_on_audit_failure(admin, sfx, made, monkeypatch):
    """sweep の監査は fail-open: 監査書込が失敗しても削除自体は成功したまま残る
    （created/updated/deleted の fail-closed とは意図的に異なる）。"""
    from sherpa import api as api_mod
    aid = _create(admin, made, f"sweep-fail-open-{sfx}", expire_at=_iso(hours=-1))["id"]

    monkeypatch.setattr(store, "audit", _boom)
    try:
        result = api_mod._sweep_expired_announcements()
    finally:
        monkeypatch.undo()
    assert result.get("deleted", 0) >= 1
    assert store.get_announcement(aid) is None, \
        "監査失敗時に sweep の削除が取り消されている（fail-open であるべき）"


# ---- 並行 PATCH の行ロック直列化・sweep の TOCTOU 対策・境界の等号 ----
# 別コネクションで対象行を FOR UPDATE ロックしたまま、その間に本物の update_announcement /
# sweep がブロックされること（＝ロックが効いていること）と、解放後の最終状態を決定的に確認する。

def _blocked_while_row_locked(aid, run, commit_sql, commit_args):
    """行を FOR UPDATE ロック中に `run` を別スレッドで走らせ、ブロックされることを確かめてから
    `commit_sql` を commit して解放し、スレッド完了を待つ。"""
    holder = psycopg.connect(store._dsn())
    try:
        holder.execute("SELECT id FROM announcements WHERE id=%s FOR UPDATE", (aid,))
        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=0.5)
        blocked = th.is_alive()
        holder.execute(commit_sql, commit_args)
        holder.commit()
    finally:
        holder.close()
    th.join(timeout=5)
    return blocked, th


def test_concurrent_patch_publish_and_expire_serializes_via_row_lock(admin, sfx, made):
    """2つの PATCH が publish_at/expire_at を別々に同時更新しても行ロックで直列化され、
    後発側は先発側の commit 後の値で順序検証する（矛盾した状態を永続化しない）。"""
    aid = _create(admin, made, f"row-lock-{sfx}")["id"]
    later_publish = datetime.fromisoformat(_iso(hours=2))
    sooner_expire = datetime.fromisoformat(_iso(hours=1))
    result: dict = {}

    def _run_patch():
        try:
            result["row"] = store.update_announcement(aid, publish_at=later_publish)
        except store.AnnouncementOrderError as e:
            result["error"] = e

    # ロック保持側が「別の PATCH が先に expire_at を確定させた」状況を模して commit
    # （sooner_expire は later_publish より前＝矛盾する組み合わせ）。
    blocked, th = _blocked_while_row_locked(
        aid, _run_patch, "UPDATE announcements SET expire_at=%s WHERE id=%s", (sooner_expire, aid))
    assert blocked, "update_announcement が対象行のロック中にブロックされていない（FOR UPDATE が効いていない）"
    assert not th.is_alive(), "update_announcement がロック解放後も完了しない"
    assert "error" in result, (
        "行ロック解放後、先発 PATCH が確定させた expire_at と矛盾する publish_at が "
        "AnnouncementOrderError を投げずに通ってしまった（並行競合が再発している）")
    assert isinstance(result["error"], store.AnnouncementOrderError)

    final = store.get_announcement(aid)
    assert final["publish_at"] is None, "拒否されたはずの publish_at が永続化されている"
    assert final["expire_at"] is not None


def test_check_constraint_rejects_direct_sql_bypassing_the_api(sfx):
    """アプリ層を経由しない直接 SQL でも、DB CHECK 制約（announcements_publish_before_expire）が
    不正な組み合わせを拒否する。"""
    now = datetime.now(timezone.utc)
    with psycopg.connect(store._dsn()) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(
                "INSERT INTO announcements (author_uid, title, body, publish_at, expire_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                ("admin", f"check-constraint-{sfx}", "本文",
                 now + timedelta(hours=2), now + timedelta(hours=1)))


def test_sweep_does_not_delete_row_whose_expiry_was_extended_concurrently(admin, sfx, made):
    """列挙〜削除の間に admin が expire_at を延長した行は消えない（DELETE 文が削除の瞬間に条件を
    再評価する）。"""
    from sherpa import api as api_mod
    aid = _create(admin, made, f"toctou-{sfx}", expire_at=_iso(hours=-1))["id"]
    future = datetime.fromisoformat(_iso(days=1))

    blocked, th = _blocked_while_row_locked(
        aid, api_mod._sweep_expired_announcements,
        "UPDATE announcements SET expire_at=%s WHERE id=%s", (future, aid))
    assert blocked, "sweep（DELETE）が対象行のロック中にブロックされていない（TOCTOU 対策が効いていない）"
    assert not th.is_alive(), "sweep がロック解放後も完了しない"
    assert store.get_announcement(aid) is not None, \
        "期限を延長した直後の行が sweep に巻き添えで削除された（TOCTOU が再発している）"


def test_announcement_visibility_boundary_equals_now_uses_sql_clock(viewer, sfx, made):
    """publish_at==now は表示、expire_at==now は非表示（クエリの <= / > と一致）を SQL 側 now() の
    直接挿入で厳密に検証する。"""
    title_pub_eq = f"boundary-publish-eq-{sfx}"
    title_exp_eq = f"boundary-expire-eq-{sfx}"
    with psycopg.connect(store._dsn(), row_factory=psycopg.rows.dict_row) as c:
        row1 = c.execute(
            "INSERT INTO announcements (author_uid, title, body, publish_at) "
            "VALUES ('admin', %s, '本文', now()) RETURNING id", (title_pub_eq,)).fetchone()
        row2 = c.execute(
            "INSERT INTO announcements (author_uid, title, body, expire_at) "
            "VALUES ('admin', %s, '本文', now()) RETURNING id", (title_exp_eq,)).fetchone()
    made.extend([row1["id"], row2["id"]])

    titles = {a["title"] for a in viewer.get("/announcements?limit=100").json()["announcements"]}
    assert title_pub_eq in titles, "publish_at == now() は表示されるべき（クエリは publish_at<=now()）"
    assert title_exp_eq not in titles, "expire_at == now() は非表示のはず（クエリは expire_at>now()）"
