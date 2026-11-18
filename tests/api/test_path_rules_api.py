"""`GET/PUT /admin/path-rules`・`GET /admin/path-rules/files`（資料の優先と除外）の業務ロジック。401/403 は `test_authz_matrix.py` が担保する。
保存は実際の `system_settings`（テスト用 DB）を通し、資料フォルダの登録と置き場所だけ差し替える。名前は架空。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sherpa import path_rules as PR

WORLD = "prapi"


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def env(monkeypatch, tmp_path):
    from sherpa import store, worlds
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)   # 他の試験が子プロセスの印を環境に残していても、親（DB を読む側）として動かす
    monkeypatch.delenv(PR.ENV_KEY, raising=False)
    for rel in ("ZZsysA/ZZspec", "ZZsysA/ZZold", "ZZsysB"):
        (tmp_path / rel).mkdir(parents=True)
    for rel in ("ZZsysA/ZZspec/ZZmain.md", "ZZsysA/ZZold/ZZold.md", "ZZsysA/ZZintro.md", "ZZsysB/ZZnote.md", "ZZtop.md"):
        (tmp_path / rel).write_text("x", encoding="utf-8")
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid} if wid == WORLD else None)
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path if wid == WORLD else None)
    monkeypatch.setattr(worlds, "archives_dir", lambda wid: tmp_path / "_arch")
    monkeypatch.setattr(worlds, "world_label", lambda wid: "架空の資料")
    store.set_system_settings("admin", {PR.SETTINGS_KEY: None})
    yield tmp_path
    store.set_system_settings("admin", {PR.SETTINGS_KEY: None})


def _put(client, rules, world=WORLD):
    return client.put("/admin/path-rules", json={"world": world, "rules": rules})


def test_put_saves_validates_and_drops_children_of_a_ruled_parent(client, env):
    r = _put(client, [{"path": "ZZsysA/ZZold", "kind": "exclude"},
                      {"path": "ZZsysA/ZZold/ZZold.md", "kind": "priority", "outside_scope": True},
                      {"path": "ZZsysA/ZZspec", "kind": "priority", "outside_scope": True},
                      {"path": "ZZtop.md", "kind": "exclude"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["path"] for x in body["rules"]] == ["ZZsysA/ZZold", "ZZsysA/ZZspec", "ZZtop.md"]
    assert [x["path"] for x in body["dropped_children"]] == ["ZZsysA/ZZold/ZZold.md"]
    got = client.get("/admin/path-rules", params={"world": WORLD}).json()
    assert got["world"] == WORLD and got["removed_missing"] == 0
    assert [(x["path"], x["kind"], x["outside_scope"]) for x in got["rules"]] == [
        ("ZZsysA/ZZold", "exclude", False), ("ZZsysA/ZZspec", "priority", True), ("ZZtop.md", "exclude", False)]
    assert {f["path"] for f in got["folders"]} >= {"ZZsysA", "ZZsysA/ZZold", "ZZsysB"}
    # 保存したらすぐ検索の判定に効く（作り直し不要）
    assert PR.rules_for(WORLD).excluded("ZZsysA/ZZold/x.md")
    # 空で保存すると設定が無くなる
    assert _put(client, []).json()["rules"] == []
    assert PR.rules_for(WORLD).empty and client.get("/admin/path-rules", params={"world": WORLD}).json()["rules"] == []


@pytest.mark.parametrize("rules", [
    [{"path": "../x", "kind": "exclude"}],
    [{"path": "/etc", "kind": "exclude"}],
    [{"path": "ZZsysA/../../x", "kind": "exclude"}],
    [{"path": "ZZsysB", "kind": "exclude", "outside_scope": True}],
    [{"path": "ZZsysB", "kind": "bogus"}],
    [{"path": "ZZsysB", "kind": "exclude"}, {"path": "ZZsysB", "kind": "priority"}],
    [{"path": "ZZnone/ZZgone", "kind": "exclude"}],
])
def test_put_rejects_unsafe_or_unknown_places(client, env, rules):
    assert _put(client, rules).status_code == 422
    assert PR.rules_for(WORLD).empty


def test_put_rejects_unknown_world_and_extra_fields(client, env):
    assert _put(client, [], world="nope").status_code == 404
    assert client.put("/admin/path-rules", json={"world": WORLD, "rules": [], "x": 1}).status_code == 422


def test_get_removes_rules_for_places_that_no_longer_exist_and_reports_the_count(client, env):
    import shutil
    assert _put(client, [{"path": "ZZsysA/ZZold", "kind": "exclude"},
                         {"path": "ZZtop.md", "kind": "priority"},
                         {"path": "ZZsysB", "kind": "priority"}]).status_code == 200
    shutil.rmtree(env / "ZZsysA" / "ZZold")
    (env / "ZZtop.md").unlink()
    got = client.get("/admin/path-rules", params={"world": WORLD}).json()
    assert got["removed_missing"] == 2
    assert [x["path"] for x in got["rules"]] == ["ZZsysB"]
    assert [r.path for r in PR.rules_for(WORLD).items] == ["ZZsysB"]       # 取り除いた結果が保存されている
    assert client.get("/admin/path-rules", params={"world": WORLD}).json()["removed_missing"] == 0


def test_get_does_not_prune_when_the_root_cannot_be_checked(client, env, monkeypatch):
    from sherpa import worlds
    assert _put(client, [{"path": "ZZsysB", "kind": "exclude"}]).status_code == 200
    monkeypatch.setattr(worlds, "world_dir", lambda wid: env / "unmounted")
    got = client.get("/admin/path-rules", params={"world": WORLD}).json()
    assert got["removed_missing"] == 0 and [x["path"] for x in got["rules"]] == ["ZZsysB"]


def test_files_lists_direct_children_only(client, env, monkeypatch):
    from sherpa import doc_ledger
    monkeypatch.setattr(doc_ledger, "ledger_rels", lambda w: [
        "ZZsysA/ZZspec/ZZmain.md", "ZZsysA/ZZintro.md", "ZZsysA/ZZold/ZZold.md", "ZZtop.md"])
    assert client.get("/admin/path-rules/files", params={"world": WORLD, "folder": "ZZsysA"}).json() == {
        "folder": "ZZsysA", "files": ["ZZsysA/ZZintro.md"], "truncated": False, "total": 1, "next_offset": None}
    assert client.get("/admin/path-rules/files", params={"world": WORLD}).json()["files"] == ["ZZtop.md"]
    assert client.get("/admin/path-rules/files", params={"world": WORLD, "folder": "../x"}).status_code == 422


def test_put_rejects_a_snapshot_too_big_for_one_environment_variable(client, env):
    """6: 子へ環境変数 1 つで渡せない大きさの設定は、理由つきで 422。"""
    rules = [{"path": f"ZZ{i:04d}/" + "あ" * 200, "kind": "exclude"} for i in range(400)]
    r = _put(client, rules)
    assert r.status_code == 422 and "大きすぎます" in r.json()["detail"]


def test_files_can_be_paged_past_the_first_page(client, env, monkeypatch):
    """4: 直下に多数のファイルがあるフォルダでも、後ろのファイルを続きから取れる。"""
    from sherpa import doc_ledger
    names = [f"ZZbig/f{i:04d}.md" for i in range(2500)]
    monkeypatch.setattr(doc_ledger, "ledger_rels", lambda w: names)
    seen, off = [], 0
    while off is not None:
        d = client.get("/admin/path-rules/files", params={"world": WORLD, "folder": "ZZbig", "offset": off}).json()
        seen += d["files"]
        assert d["total"] == 2500
        off = d["next_offset"]
    assert seen == names
    assert client.get("/admin/path-rules/files", params={"world": WORLD, "folder": "ZZbig", "offset": 2400, "limit": 50}).json()["next_offset"] == 2450


def test_admin_path_rules_work_for_a_world_the_app_can_resolve_without_a_ledger_row(client, env, monkeypatch):
    """5: 台帳（`store.get_world`）に行が無くても、アプリが解決できる資料フォルダ（開発用の試験データ）なら使える。"""
    from sherpa import store
    monkeypatch.setattr(store, "get_world", lambda wid: None)
    assert client.get("/admin/path-rules", params={"world": WORLD}).status_code == 200
    assert _put(client, [{"path": "ZZsysB", "kind": "exclude"}]).status_code == 200
    assert client.get("/admin/path-rules", params={"world": "nope"}).status_code == 404


def test_put_stores_the_target_type_checked_on_disk(client, env):
    """保存のときに、対象が file か folder かを実物で確かめて規則に持つ（申告は使わない）。"""
    (env / "ZZsysB" / "README").write_text("x")
    r = _put(client, [{"path": "ZZsysB/README", "kind": "exclude", "target_type": "folder"},
                      {"path": "ZZsysA/ZZold", "kind": "exclude"}])
    assert r.status_code == 200, r.text
    assert {x["path"]: x["target_type"] for x in r.json()["rules"]} == {"ZZsysB/README": "file", "ZZsysA/ZZold": "folder"}
    assert PR.rules_for(WORLD).file_exclude_paths == ("ZZsysB/README",)


def test_get_prune_does_not_overwrite_a_concurrent_put(client, env, monkeypatch):
    """画面を開いたときの取り除きが、同時の保存（PUT）の変更を旧い値で上書きして規則を消さない。"""
    import threading
    from sherpa import path_rules as P
    (env / "ZZsysB" / "x").write_text("x")
    assert _put(client, [{"path": "ZZsysB", "kind": "exclude"}]).status_code == 200
    P.save_world("admin", WORLD, P.validate_entries([{"path": "ZZsysB", "kind": "exclude"}, {"path": "ZZgone", "kind": "exclude"}]))
    real = P.prune_missing
    spawned = []

    def prune_then_race(world, items):
        out = real(world, items)
        if not spawned:                       # 読み取りの直後に、同じ資料フォルダへの PUT が割り込む
            t = threading.Thread(target=lambda: _put(client, [{"path": "ZZsysB", "kind": "exclude"},
                                                              {"path": "ZZtop.md", "kind": "priority"}]))
            spawned.append(t)
            t.start()
            t.join(timeout=0.5)
        return out
    monkeypatch.setattr(P, "prune_missing", prune_then_race)
    got = client.get("/admin/path-rules", params={"world": WORLD}).json()
    assert got["removed_missing"] == 1
    monkeypatch.setattr(P, "prune_missing", real)
    spawned[0].join(timeout=5)
    assert sorted(r.path for r in P.rules_for(WORLD).items) == ["ZZsysB", "ZZtop.md"]       # PUT の変更が残る


def test_tree_and_files_list_documents_without_extension_filtering(client, env, monkeypatch):
    """木・ファイルの一覧は台帳の資料を拡張子で落とさない（拡張子の無い資料も設定できる）。秘匿名は出さない。"""
    from sherpa import doc_ledger
    monkeypatch.setattr(doc_ledger, "ledger_rels", lambda w: [
        "README", "ZZsysA/NOEXT", "ZZsysA/x.unknownext", "ZZsysA/credentials.xlsx", "ZZsysA/ZZspec/ZZmain.md"])
    tree = client.get("/admin/path-rules", params={"world": WORLD}).json()
    assert {f["path"] for f in tree["folders"]} == {"ZZsysA", "ZZsysA/ZZspec"}
    top = client.get("/admin/path-rules/files", params={"world": WORLD}).json()["files"]
    assert top == ["README"]
    sub = client.get("/admin/path-rules/files", params={"world": WORLD, "folder": "ZZsysA"}).json()["files"]
    assert sub == ["ZZsysA/NOEXT", "ZZsysA/x.unknownext"]                    # 秘匿名の credentials.xlsx は出さない


def test_admin_es_search_keeps_outside_scope_hits(client, env, monkeypatch):
    from sherpa import documents, es_index
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "ZZsysA", "kind": "priority", "outside_scope": True}]}))
    monkeypatch.setattr(es_index, "search", lambda *a, **k: ([
        {"doc_id": "ZZsysA/ZZintro.md", "text": "t", "line": 1}, {"doc_id": "ZZtop.md", "text": "t", "line": 1}], None))
    monkeypatch.setattr(documents, "existing_rels", lambda w, ids: set(ids))
    r = client.get("/admin/es/search", params={"world": WORLD, "query": "q", "scope_paths": ["ZZsysB"]})
    assert r.status_code == 200, r.text
    assert [h["doc_id"] for h in r.json()["hits"]] == ["ZZsysA/ZZintro.md"]


def test_listing_without_a_ledger_does_not_filter_by_extension(client, env, monkeypatch):
    """台帳の無い資料フォルダの代わりの列挙も、拡張子で絞らない（拡張子の無い資料を設定できる）。"""
    from sherpa import doc_ledger
    (env / "ZZsysB" / "README").write_text("x")
    (env / "ZZsysB" / "x.unknownext").write_text("x")
    monkeypatch.setattr(doc_ledger, "ledger_rels", lambda w: None)
    files = client.get("/admin/path-rules/files", params={"world": WORLD, "folder": "ZZsysB"}).json()["files"]
    assert "ZZsysB/README" in files and "ZZsysB/x.unknownext" in files and "ZZsysB/ZZnote.md" in files


def test_put_refuses_rules_whose_worst_case_deny_count_exceeds_the_limit(client, env):
    """Codex の deny の最悪の件数が上限を超える設定は、保存で断る（超えると直読の準備が失敗して全部が読めなくなる）。"""
    rules = []
    for i in range(210):
        (env / f"ZZf{i:03d}.md").write_text("x")
        rules.append({"path": f"ZZf{i:03d}.md", "kind": "exclude"})
    r = _put(client, rules)
    assert r.status_code == 422 and "除外が多すぎます" in r.json()["detail"]
    assert PR.rules_for(WORLD).empty
    assert _put(client, rules[:150]).status_code == 200


def test_prune_ignores_stale_ledger_rows_and_looks_only_at_the_real_files(client, env, monkeypatch):
    """台帳の旧い行があっても、実ファイルが無い場所の設定は取り除く（消した直後に画面を開いても残らない）。"""
    from sherpa import doc_ledger
    assert _put(client, [{"path": "ZZtop.md", "kind": "exclude"}, {"path": "ZZsysB", "kind": "exclude"}]).status_code == 200
    (env / "ZZtop.md").unlink()                                              # Windows 側で消した（台帳はまだ旧い）
    monkeypatch.setattr(doc_ledger, "ledger_rels", lambda w: ["ZZtop.md", "ZZsysB/ZZnote.md"])
    got = client.get("/admin/path-rules", params={"world": WORLD}).json()
    assert got["removed_missing"] == 1 and [r["path"] for r in got["rules"]] == ["ZZsysB"]
    assert PR.rules_for(WORLD).exclude_paths == ("ZZsysB",)


def test_get_does_not_prune_or_save_when_existence_cannot_be_checked(client, env, monkeypatch):
    """実在を確かめられない場所（権限・入出力の失敗）がある間は、取り除かず保存もしない。GET は 503。"""
    import errno
    import os
    from sherpa import path_rules as P
    assert _put(client, [{"path": "ZZsysB", "kind": "exclude"}]).status_code == 200
    P.save_world("admin", WORLD, P.validate_entries([{"path": "ZZsysB", "kind": "exclude"}, {"path": "ZZgone", "kind": "exclude"}]))
    real = os.lstat

    def lstat(p, *a, **k):
        if str(p).endswith("ZZsysB"):
            raise PermissionError(errno.EACCES, "denied")
        return real(p, *a, **k)
    monkeypatch.setattr(os, "lstat", lstat)
    assert client.get("/admin/path-rules", params={"world": WORLD}).status_code == 503
    monkeypatch.setattr(os, "lstat", real)
    assert sorted(r.path for r in P.rules_for(WORLD).items) == ["ZZgone", "ZZsysB"]       # 消えていない
