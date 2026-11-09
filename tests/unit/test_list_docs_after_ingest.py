"""list_docs が、取り込みの後に資料フォルダへ置かれたファイルを `after_ingest` に添えること。

台帳の書き込みは取り込みと同じ `worker._ledger_rows`、DB・資料フォルダは実物（モックは資料フォルダの場所の解決だけ）。
"""
from __future__ import annotations

import uuid

import pytest

from sherpa import store, tool_dispatch, worlds
from sherpa.ingest import worker
from sherpa import documents
from sherpa.parts.read import tools as listing

pytestmark = pytest.mark.unit

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen2/design/spec_b.md": "# spec B\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "afteringest_" + uuid.uuid4().hex[:8]
    wd = tmp_path / "world"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    store.replace_documents(wid, worker._ledger_rows(wid))  # 取り込み済みの状態
    yield wid
    store.replace_documents(wid, [])


def _put(world, rel, text="x\n"):
    p = worlds.world_dir(world) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _list(world, args, sp=None, layer=None):
    return tool_dispatch.run_tool("list_docs", args, world, sp, layer=layer)


def test_file_placed_after_ingest_is_listed_and_cited(world):
    _put(world, "gen1/design/new_spec.md")
    _put(world, "gen1/memo.zzz")
    res, docs, _c, _k = _list(world, {"path_prefix": "gen1"})
    extra = {d["rel_path"]: d for d in res["after_ingest"]}
    assert set(extra) == {"gen1/design/new_spec.md", "gen1/memo.zzz"}
    assert extra["gen1/memo.zzz"]["doctype"] is None
    assert extra["gen1/design/new_spec.md"]["size"] == 2
    assert extra["gen1/design/new_spec.md"]["modified"].endswith("Z")
    assert "には入っていません" in res["after_ingest_notice"]
    assert "gen1/design/new_spec.md" in docs
    assert {d["rel_path"] for d in res["docs"]} == {"gen1/design/spec_a.md", "gen1/src/PROG1.cbl"}


def test_nothing_new_has_no_after_ingest(world):
    res = _list(world, {"path_prefix": "gen1"})[0]
    assert "after_ingest" not in res and "after_ingest_notice" not in res


def test_without_path_prefix_does_not_walk(world, monkeypatch):
    _put(world, "gen1/new.md")
    monkeypatch.setattr(documents, "scope_rel_set",
                        lambda *a, **k: pytest.fail("path_prefix が無いのに歩いた"))
    res = _list(world, {})[0]
    assert "after_ingest" not in res


def test_scope_layer_sensitive_and_vcs_are_excluded(world):
    _put(world, "gen1/design/new_spec.md")
    _put(world, "gen1/src/NEW.cbl")
    _put(world, "gen2/design/other.md")
    _put(world, "gen1/design/.env")
    _put(world, "gen1/design/id_rsa")
    _put(world, "gen1/.git/config")
    res = _list(world, {"path_prefix": "gen1"}, sp=["gen1/design"], layer="docs")[0]
    assert [d["rel_path"] for d in res["after_ingest"]] == ["gen1/design/new_spec.md"]
    res = _list(world, {"path_prefix": "gen1"}, layer="code")[0]
    assert [d["rel_path"] for d in res["after_ingest"]] == ["gen1/src/NEW.cbl"]
    res = _list(world, {"path_prefix": "gen1"})[0]
    assert {d["rel_path"] for d in res["after_ingest"]} == {"gen1/design/new_spec.md", "gen1/src/NEW.cbl"}


def test_filters_apply_and_state_skips(world):
    _put(world, "gen1/design/new_spec.md")
    _put(world, "gen1/src/NEW.cbl")
    res = _list(world, {"path_prefix": "gen1", "name_pattern": "NEW.C"})[0]
    assert [d["rel_path"] for d in res["after_ingest"]] == ["gen1/src/NEW.cbl"]
    doctype = res["after_ingest"][0]["doctype"]
    assert doctype
    res = _list(world, {"path_prefix": "gen1", "doctype": doctype})[0]
    assert [d["rel_path"] for d in res["after_ingest"]] == ["gen1/src/NEW.cbl"]
    res = _list(world, {"path_prefix": "gen1", "state": "ready"})[0]
    assert "after_ingest" not in res


def test_deadline_gives_unchecked_and_keeps_ledger_listing(world, monkeypatch):
    _put(world, "gen1/new.md")
    monkeypatch.setattr(listing, "_AFTER_INGEST_WALK_SECONDS", -1.0)
    res = _list(world, {"path_prefix": "gen1"})[0]
    assert res["after_ingest_unchecked"] is True
    assert "確かめきれませんでした" in res["after_ingest_notice"]
    assert "after_ingest" not in res
    assert {d["rel_path"] for d in res["docs"]} == {"gen1/design/spec_a.md", "gen1/src/PROG1.cbl"}


def test_after_ingest_is_capped_at_50_in_name_order(world):
    for i in range(listing._AFTER_INGEST_MAX + 5):
        _put(world, f"gen1/bulk/f{i:03d}.md")
    res = _list(world, {"path_prefix": "gen1/bulk"})[0]
    names = [d["rel_path"] for d in res["after_ingest"]]
    assert len(names) == listing._AFTER_INGEST_MAX and names == sorted(names)
    assert res["after_ingest_truncated"] is True
    assert res["after_ingest_count"] == listing._AFTER_INGEST_MAX + 5


def test_after_ingest_uses_confirmed_layer_not_extension(world):
    """拡張子では資料に見えても中身がソースのファイルは、資料だけの会話の after_ingest に出さない。"""
    _put(world, "gen1/conf/late.zzz", "A=1\nB=2\n")
    docs_only = _list(world, {"path_prefix": "gen1"}, layer="docs")[0]
    code_only = _list(world, {"path_prefix": "gen1"}, layer="code")[0]
    assert "gen1/conf/late.zzz" not in [d["rel_path"] for d in docs_only.get("after_ingest", [])]
    assert "gen1/conf/late.zzz" in [d["rel_path"] for d in code_only.get("after_ingest", [])]



def test_walk_stays_inside_the_overlap_of_prefix_and_scope(world, monkeypatch):
    from sherpa import scope_infer
    _put(world, "gen1/design/new_spec.md")
    _put(world, "gen1/src/NEW.cbl")
    walked = []
    real = scope_infer.safe_files

    def spy(root, *a, **kw):
        walked.append(str(root))
        return real(root, *a, **kw)
    monkeypatch.setattr(scope_infer, "safe_files", spy)
    res = _list(world, {"path_prefix": "gen1"}, sp=["gen1/design"])[0]
    assert [d["rel_path"] for d in res["after_ingest"]] == ["gen1/design/new_spec.md"]
    assert walked and all(w.endswith("gen1/design") for w in walked)


def test_prefix_outside_scope_does_not_walk(world, monkeypatch):
    _put(world, "gen1/new.md")
    monkeypatch.setattr(documents, "scope_rel_set",
                        lambda *a, **k: pytest.fail("範囲と重ならないのに歩いた"))
    res = _list(world, {"path_prefix": "gen1"}, sp=["gen2"])[0]
    assert "after_ingest" not in res and "after_ingest_unchecked" not in res


def test_deadline_passing_after_the_walk_gives_unchecked(world, monkeypatch):
    import time
    _put(world, "gen1/new.md")
    monkeypatch.setattr(listing, "_AFTER_INGEST_WALK_SECONDS", 0.2)
    real = documents.scope_rel_set

    def slow(*a, **kw):
        out = real(*a, **kw)
        time.sleep(0.3)
        return out
    monkeypatch.setattr(documents, "scope_rel_set", slow)
    res = _list(world, {"path_prefix": "gen1"})[0]
    assert res["after_ingest_unchecked"] is True
    assert "after_ingest" not in res


def test_unreadable_subtree_gives_unchecked(world):
    import os
    if os.geteuid() == 0:
        pytest.skip("root では権限で読めなくできない")
    _put(world, "gen1/new.md")
    _put(world, "gen1/locked/hidden.md")
    locked = worlds.world_dir(world) / "gen1" / "locked"
    locked.chmod(0)
    try:
        res = _list(world, {"path_prefix": "gen1"})[0]
    finally:
        locked.chmod(0o755)
    assert res["after_ingest_unchecked"] is True
    assert "after_ingest" not in res


def test_unreadable_folder_on_the_way_to_scope_gives_unchecked(world):
    import os
    if os.geteuid() == 0:
        pytest.skip("root では権限で読めなくできない")
    _put(world, "gen1/locked/sub/hidden.md")
    locked = worlds.world_dir(world) / "gen1" / "locked"
    locked.chmod(0)
    try:
        skipped: dict = {}
        assert documents.scope_rel_set(world, ["gen1/locked/sub"], skipped=skipped) == set()
        res = _list(world, {"path_prefix": "gen1/locked/sub"})[0]
    finally:
        locked.chmod(0o755)
    assert documents.walk_incomplete(skipped)
    assert res["after_ingest_unchecked"] is True
    assert "after_ingest" not in res


def test_file_the_ingest_would_skip_is_not_listed(world):
    _put(world, "gen1/memo.zzz", "text\n")
    (worlds.world_dir(world) / "gen1" / "blob.zzz").write_bytes(b"\x00\x01\x02\x00binary")
    for layer in (None, "docs"):
        res = _list(world, {"path_prefix": "gen1"}, layer=layer)[0]
        assert [d["rel_path"] for d in res["after_ingest"]] == ["gen1/memo.zzz"]


def test_deadline_passing_during_the_last_candidate_check_gives_unchecked(world, monkeypatch):
    import time
    from sherpa import doc_reach
    _put(world, "gen1/zz_last.md")
    monkeypatch.setattr(listing, "_AFTER_INGEST_WALK_SECONDS", 0.3)
    real = doc_reach.in_layer_confirmed

    def slow(*a, **kw):
        out = real(*a, **kw)
        time.sleep(0.4)
        return out
    monkeypatch.setattr(doc_reach, "in_layer_confirmed", slow)
    res = _list(world, {"path_prefix": "gen1"})[0]
    assert res["after_ingest_unchecked"] is True
    assert "after_ingest" not in res


def test_unreachable_world_dir_gives_unchecked(world, monkeypatch):
    monkeypatch.setattr(worlds, "world_dir", lambda w: None)
    res = _list(world, {"path_prefix": "gen1"})[0]
    assert res["after_ingest_unchecked"] is True


def test_file_placed_after_ingest_is_found_by_name_without_folder(world):
    _put(world, "gen1/design/placed_later.md")
    by_name = _list(world, {"name_pattern": "placed_later"})[0]
    assert [d["rel_path"] for d in by_name["after_ingest"]] == ["gen1/design/placed_later.md"]
    by_glob = tool_dispatch.run_tool("glob_search", {"pattern": "**/placed_later*"}, world, None, layer=None)[0]
    assert [d["rel_path"] for d in by_glob["after_ingest"]] == ["gen1/design/placed_later.md"]
    assert "には入っていません" in by_glob["after_ingest_notice"]


def test_name_search_after_ingest_stays_inside_the_scope(world):
    _put(world, "gen2/design/placed_later.md")
    res = tool_dispatch.run_tool("glob_search", {"pattern": "**/placed_later*"}, world, ["gen1"], layer=None)[0]
    assert "after_ingest" not in res


def _count_scandir(monkeypatch):
    import os
    calls = {"n": 0}
    real = os.scandir

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)
    monkeypatch.setattr(os, "scandir", counting)
    return calls


def test_name_search_with_a_ledger_hit_does_not_walk(world, monkeypatch):
    calls = _count_scandir(monkeypatch)
    res = tool_dispatch.run_tool("glob_search", {"pattern": "**/PROG1*"}, world, None, layer=None)[0]
    assert res["count"] == 1 and "after_ingest" not in res
    assert calls["n"] == 0


def test_tool_process_walks_only_once_per_request_on_misses(world, monkeypatch):
    from sherpa.parts.read import tools as read_tools
    monkeypatch.setenv("SHERPA_MCP_WORLD", world)
    monkeypatch.setattr(read_tools, "_after_ingest_walks", {})
    _put(world, "gen1/design/placed_later.md")
    first = tool_dispatch.run_tool("glob_search", {"pattern": "**/placed_later*"}, world, None, layer=None)[0]
    assert [d["rel_path"] for d in first["after_ingest"]] == ["gen1/design/placed_later.md"]
    calls = _count_scandir(monkeypatch)
    again = _list(world, {"name_pattern": "placed_later"})[0]
    assert [d["rel_path"] for d in again["after_ingest"]] == ["gen1/design/placed_later.md"]
    assert calls["n"] == 0
