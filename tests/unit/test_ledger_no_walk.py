"""利用者の操作（範囲の選択肢・範囲の検証・出典の検証・検索の実在確認・根拠種別の判定・対応文書の候補）が、
取り込みの台帳から答え、資料フォルダ全体を歩かないこと。

資料フォルダがネットワーク共有（数十万ファイル）だと、操作のたびの全走査が遅くて選択肢が出ない・時間切れになる。
台帳と歩いた結果が同じ集合になることと、台帳があるときは走査関数が呼ばれないことを固定する。
台帳の書き込みは取り込みと同じ `worker._ledger_rows`、DB は実物（差し替えは資料フォルダの場所の解決と ES 検索の戻りだけ）。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import uuid

import pytest

from sherpa import chat_service, compare_docs, doc_ledger, documents, scope, scope_infer, store, worlds
from sherpa.ingest import importance, worker
from sherpa.parts.read import fused_search
from sherpa.providers import base as providers_base
from sherpa.providers.codex import citations

pytestmark = pytest.mark.unit

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/design/notes.txt": "plain notes\n",
    "gen1/design/sub/deep/memo.md": "# memo\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen1/src/PROG2.cbl": "       PROGRAM-ID. PROG2.\n",
    "gen1/src/lib/Util.java": "class Util {}\n",
    "gen1/common/COPY1.cpy": "       01 REC-A PIC X.\n",
    "gen2/design/spec_a.md": "# spec A v2\n",
    "gen2/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "loose.md": "# loose\n",
    importance.CONTROL_FILENAME: "# control\n",
    "gen1/" + importance.CONTROL_FILENAME: "# control\n",
    "gen1/.git/config": "[core]\n",
    "gen1/design/work.tmp": "tmp\n",
}
_ARCHIVE_FILES = {"zip1/inner/readme.md": "# inner\n", "zip1/inner/Calc.cbl": "       PROGRAM-ID. CALC.\n"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "nowalk_" + uuid.uuid4().hex[:8]
    wd, arch, der = tmp_path / "world", tmp_path / "arch", tmp_path / "derived"
    for base, files in ((wd, _FILES), (arch, _ARCHIVE_FILES)):
        for rel, text in files.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    yield wid
    store.replace_documents(wid, [])


def _ingest(wid):
    store.replace_documents(wid, worker._ledger_rows(wid))


@contextlib.contextmanager
def _no_walk():
    """資料フォルダを歩く入口（`safe_files`・`os.scandir`）が呼ばれたら失敗させる。"""
    def boom(*a, **kw):
        raise AssertionError("台帳があるのに資料フォルダを歩いた")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scope_infer, "safe_files", boom)
        mp.setattr(os, "scandir", boom)
        yield


def _scope_answers(wid):
    return {
        "tree": scope.scope_tree(wid),
        "prefixes": scope.known_scope_prefixes(wid),
        "valid": [scope.valid_scope_paths(wid, sp) for sp in
                  (None, [], ["gen1"], ["gen1/design/sub"], ["gen1/design/sub/deep"], ["zip1/inner"], ["zip1"],
                   ["gen1", "gen2/src"], ["gen1/nothing"], ["gen1/src/PROG1.cbl"], ["gen1/.git"])],
    }


def test_scope_choices_from_ledger_match_walk_and_do_not_walk(world):
    _ingest(world)
    with _no_walk():
        from_ledger = _scope_answers(world)
    store.replace_documents(world, [])           # 台帳が無ければ同じ資料を歩いて答える
    from_walk = _scope_answers(world)
    assert from_ledger == from_walk
    paths = {s["path"] for s in from_ledger["tree"]["scopes"]}
    assert {"gen1", "gen1/design/sub/deep", "gen2/src", "zip1", "zip1/inner"} <= paths   # 比較が空振りでない
    assert "gen1/.git" not in paths


def test_scope_choices_answer_even_when_the_folder_is_unreadable(world, tmp_path):
    _ingest(world)
    shutil.rmtree(tmp_path / "world")
    tree = scope.scope_tree(world)
    assert {"gen1", "gen2", "gen1/src"} <= {s["path"] for s in tree["scopes"]}
    assert scope.valid_scope_paths(world, ["gen1/design"]) is True


def test_scope_choices_count_files_under_each_prefix(world):
    _ingest(world)
    by_path = {s["path"]: s["count"] for s in scope.scope_tree(world)["scopes"]}
    assert by_path["gen1/design"] == 3 and by_path["gen1"] == 7 and by_path["gen1/design/sub"] == 1
    assert by_path["zip1"] == 2


def test_ledger_tree_leaves_out_what_the_ledger_does_not_hold(world, tmp_path):
    """台帳に載らない（秘匿名・テキストにできない画像）だけのフォルダは、選択肢に出さない。"""
    for rel in ("onlyimg/pic.png", "secret/credentials.xlsx"):
        p = tmp_path / "world" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    _ingest(world)
    paths = {s["path"] for s in scope.scope_tree(world)["scopes"]}
    assert "onlyimg" not in paths and "secret" not in paths
    assert "gen1" in paths


def test_no_ledger_walk_has_a_deadline(world, monkeypatch):
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)   # 期限は着手時点で切れている
    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        scope.scope_tree(world)
    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        scope.valid_scope_paths(world, ["gen1"])
    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        documents.known_rels(world)
    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        doc_ledger.public_documents_page(world, limit=None)
    _ingest(world)                                                   # 台帳があれば期限に関係なく答える
    assert scope.valid_scope_paths(world, ["gen1"]) is True
    assert documents.known_rels(world)
    assert doc_ledger.public_documents_page(world, limit=None)[1] > 0


def test_known_rels_from_ledger_match_walk_and_do_not_walk(world):
    _ingest(world)
    with _no_walk():
        from_ledger = documents.known_rels(world)
    store.replace_documents(world, [])
    # 歩く版は本文でないファイルも含む。台帳に載る文書（本文）は同じ
    walked = documents.known_rels(world)
    assert from_ledger <= walked
    assert from_ledger == {r for r in walked if not r.endswith(".tmp")}
    assert "zip1/inner/readme.md" in from_ledger and importance.CONTROL_FILENAME not in from_ledger


@pytest.mark.parametrize("scope_paths,layer", [(None, None), (["gen1"], None), (None, "docs"), (["gen1", "zip1"], "code")])
def test_citation_name_index_from_ledger_matches_walk(world, scope_paths, layer):
    _ingest(world)
    with _no_walk():
        from_ledger = citations._name_index(world, scope_paths, layer)
    store.replace_documents(world, [])
    assert from_ledger == citations._name_index(world, scope_paths, layer)
    assert sorted(citations._name_index(world, None, None)["spec_a.md"]) == ["gen1/design/spec_a.md", "gen2/design/spec_a.md"]


@pytest.mark.parametrize("scope_paths,layer", [(None, None), (["gen1/src"], None), (["gen1/design"], "docs"), (["zip1"], "code"),
                                              (["gen2"], "both")])
def test_scope_evidence_kinds_from_ledger_match_walk(world, monkeypatch, scope_paths, layer):
    monkeypatch.setattr(providers_base, "_scope_kinds_cache", {})
    _ingest(world)
    with _no_walk():
        from_ledger = providers_base._scope_evidence_kinds(world, scope_paths, layer)
    providers_base._scope_kinds_cache.clear()
    store.replace_documents(world, [])
    from_walk = providers_base._scope_evidence_kinds(world, scope_paths, layer)
    assert from_ledger == from_walk and from_ledger is not None


def test_compare_candidates_from_ledger_match_walk_and_do_not_walk(world):
    _ingest(world)
    with _no_walk():
        from_ledger = compare_docs._basename_candidates(world, "gen1/design/spec_a.md", "gen2", None, None)
    store.replace_documents(world, [])
    assert from_ledger == compare_docs._basename_candidates(world, "gen1/design/spec_a.md", "gen2", None, None)
    assert "gen2/design/spec_a.md" in from_ledger[0]


def _fake_es(hits):
    def search(world, query, scope_paths=None, k=8, vector=False, layer=None, **kw):
        return list(hits), None
    return search


def test_chat_es_hits_check_existence_without_walking(world, monkeypatch):
    from sherpa import es_index
    hits = [{"doc_id": "gen1/design/spec_a.md", "text": "a"}, {"doc_id": "zip1/inner/readme.md", "text": "z"},
            {"doc_id": "gone.md", "text": "stale"}, {"doc_id": importance.CONTROL_FILENAME, "text": "ctl"}]
    monkeypatch.setattr(es_index, "search", _fake_es(hits))
    _ingest(world)
    with _no_walk():
        got = chat_service._es_hits(world, "q", None)
    assert [h["doc_id"] for h in got] == ["gen1/design/spec_a.md", "zip1/inner/readme.md"]


def test_fused_search_checks_existence_without_walking(world, monkeypatch):
    def hit(rel):
        return {"key": rel, "doc_id": rel, "path": rel, "line": 1, "snippet": "s", "engine_score": 1.0,
                "judgement": None, "paths": None}
    monkeypatch.setattr(fused_search, "_search_keyword", lambda w, q, sp, k, s, layer=None: (
        [hit("gen1/src/PROG1.cbl"), hit("gone.md"), hit("zip1/inner/Calc.cbl")], None))
    monkeypatch.setattr(fused_search, "_search_vector", lambda w, q, sp, k, s, layer=None: ([hit("loose.md")], None))
    _ingest(world)
    with _no_walk():
        res = fused_search.search(world, "q", engines=["keyword", "vector"], root=worlds.world_dir(world), strict=True)
    assert {h["doc_id"] for h in res["hits"]} == {"gen1/src/PROG1.cbl", "zip1/inner/Calc.cbl", "loose.md"}
    assert res["engines_used"] == ["keyword", "vector"]
