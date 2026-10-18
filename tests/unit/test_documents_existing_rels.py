"""`documents.existing_rels`（ヒットの実在確認）と `store.get_world_root`（資料フォルダ解決の狭い SELECT）の回帰テスト。
規模試験（資料 2 万件）で、es_search が呼ぶたびに木を全部歩く・world_dir が worlds 行を丸ごと読む、が時間の大半だった。
どちらも結果は変えない（`world_rel_set` との一致・即反映）ことを固定する。DB・ES 不要。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
from sherpa import documents, es_index, scope_infer, store, tool_dispatch, worlds  # noqa: E402
from sherpa.ingest import importance  # noqa: E402
from sherpa.store import worlds as worlds_store  # noqa: E402

_W = "existrelstest"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "root"
    arch = tmp_path / "arch"
    root.mkdir()
    arch.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("s", encoding="utf-8")
    files = ["a.md", "部署/年度/b.pdf", "部署/年度/~$lock.xlsx", "部署/x.tmp", ".git/config", "部署/.svn/entries",
             importance.CONTROL_FILENAME, "部署/" + importance.CONTROL_FILENAME, "dup/same.md"]
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    (arch / "zip1").mkdir()
    (arch / "zip1" / "inner.md").write_text("x", encoding="utf-8")
    (arch / "dup").mkdir()
    (arch / "dup" / "same.md").write_text("x", encoding="utf-8")
    os.symlink(outside / "secret.md", root / "link.md")
    os.symlink(outside, root / "linkdir")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root if w == _W else None)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    return root


def _probe(extra=()):
    return ["a.md", "部署/年度/b.pdf", "部署/年度/~$lock.xlsx", "部署/x.tmp", ".git/config", "部署/.svn/entries",
            importance.CONTROL_FILENAME, "部署/" + importance.CONTROL_FILENAME, "dup/same.md", "zip1/inner.md",
            "link.md", "linkdir/secret.md", "missing.md", "部署/年度/missing.pdf", "部署", "部署/年度", "../outside/secret.md",
            "/etc/passwd", "a.md/", "./a.md", "部署//年度/b.pdf", "", *extra]


def test_existing_rels_equals_world_rel_set_intersection(tree):
    probe = _probe()
    assert documents.existing_rels(_W, probe) == documents.world_rel_set(_W) & set(probe)


def test_existing_rels_does_not_walk_the_tree_for_well_formed_rels(tree, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("木を歩いてはいけない")
    monkeypatch.setattr(scope_infer, "safe_files", boom)
    assert documents.existing_rels(_W, ["a.md", "部署/年度/b.pdf", "missing.md"]) == {"a.md", "部署/年度/b.pdf"}


def test_existing_rels_falls_back_to_full_walk_for_names_it_cannot_decide(tree):
    odd = "部署/back\\slash.md"
    (tree / "部署" / "back\\slash.md").write_text("x", encoding="utf-8")
    probe = _probe([odd])
    got = documents.existing_rels(_W, probe)
    assert odd in got
    assert got == documents.world_rel_set(_W) & set(probe)


def test_existing_rels_reflects_delete_rename_and_add_immediately(tree):
    assert documents.existing_rels(_W, ["a.md", "new.md", "renamed.md"]) == {"a.md"}
    (tree / "a.md").rename(tree / "renamed.md")
    (tree / "new.md").write_text("x", encoding="utf-8")
    assert documents.existing_rels(_W, ["a.md", "new.md", "renamed.md"]) == {"new.md", "renamed.md"}
    (tree / "new.md").unlink()
    assert documents.existing_rels(_W, ["a.md", "new.md", "renamed.md"]) == {"renamed.md"}


def test_existing_rels_unresolved_world_and_empty_input(tree):
    assert documents.existing_rels("otherworld", ["a.md"]) == set()
    assert documents.existing_rels(_W, []) == set()


def test_existing_rels_raises_when_deadline_already_past(tree):
    import time
    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        documents.existing_rels(_W, (), deadline=time.monotonic() - 1)


def test_es_search_keeps_only_existing_docs_without_walking(tree, monkeypatch):
    hits = [{"doc_id": r, "line": 1, "text": "t", "ext": ".md"} for r in ("a.md", "gone.md", "link.md", "dup/same.md")]
    monkeypatch.setattr(es_index, "search", lambda w, q, scope_paths=None, k=20, layer=None, **kw: (list(hits), None))
    monkeypatch.setattr(scope_infer, "safe_files", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("walk")))
    res, _, _, _ = tool_dispatch.run_tool("es_search", {"query": "q"}, _W, None)
    assert [h["doc_id"] for h in res["hits"]] == ["a.md", "dup/same.md"]
    assert res["excluded_hits"] == {"not_current": 2}


def test_world_dir_reads_only_the_root_row(monkeypatch, tmp_path):
    root = tmp_path / "r"
    root.mkdir()

    def must_not_call(*a, **kw):
        raise AssertionError("worlds 行を丸ごと読んではいけない")
    monkeypatch.delenv("SHERPA_MCP_WORLD_ROOT", raising=False)
    monkeypatch.setattr(store, "get_world", must_not_call)
    monkeypatch.setattr(store, "get_world_root", lambda w: {"world_id": w, "root_path": str(root)})
    assert worlds.world_dir("rootrowworld") == root


class _Cursor:
    def fetchone(self):
        return {"world_id": "w1", "root_path": "/r"}


class _Conn:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Cursor()


def test_get_world_root_select_excludes_last_manifest(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(worlds_store, "_ensure", lambda *a, **kw: None)
    monkeypatch.setattr(worlds_store, "_connect", lambda **kw: conn)
    assert worlds_store.get_world_root("w1") == {"world_id": "w1", "root_path": "/r"}
    sql, _ = conn.calls[0]
    assert "root_path" in sql and "last_manifest" not in sql and "last_" not in sql
