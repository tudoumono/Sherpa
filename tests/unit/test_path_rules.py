"""管理者の優先・除外（`sherpa/path_rules.py`）の契約テスト。

検証・親子の整理・判定（`Rules`）と、効かせる各経路（grep・ES・読み取りの道具・グラフの根拠・出典・調べものの道具の子プロセス・
Codex の直読の deny）が、除外を必ず外し・優先を先に出し・「範囲の外でも読む」を足し・設定が無ければ従来どおりであることを確かめる。
外部の境界（ES の HTTP・system_settings の DB 読み）だけ差し替える。名前は架空。
"""
from __future__ import annotations

import json
import os

import pytest

from sherpa import grep_tool as G
from sherpa import path_rules as PR

pytestmark = pytest.mark.usefixtures("upstream_only_registry")

WORLD = "prw"
NEEDLE = "ZZNEEDLE"

FILES = {
    "ZZsysA/ZZspec/ZZmain.md": f"# 仕様\n{NEEDLE} 正本の仕様\n",
    "ZZsysA/ZZold/ZZold.md": f"# 旧\n{NEEDLE} 古い版\n",
    "ZZsysA/ZZold/ZZkeep.md": f"# 現行\n{NEEDLE} 古い場所にある現行\n",
    "ZZsysB/ZZnote.md": f"# メモ\n{NEEDLE} 別システムのメモ\n",
    "ZZcommon/ZZrule.md": f"# 規約\n{NEEDLE} 共通の規約\n",
    "ZZtop.md": f"# 直下\n{NEEDLE} 直下の資料\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    from sherpa import worlds as W
    root = tmp_path / "world"
    for rel, body in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    der_md, der_rag = tmp_path / "derived" / "md", tmp_path / "derived" / "rag"
    der_md.mkdir(parents=True)
    monkeypatch.setattr(W, "world_dir", lambda w: root)
    monkeypatch.setattr(W, "derived_md_dir", lambda w: der_md)
    monkeypatch.setattr(W, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(W, "archives_dir", lambda w: tmp_path / "arch")
    monkeypatch.setattr(W, "observation_current_dir", lambda w: None)
    monkeypatch.delenv(PR.ENV_KEY, raising=False)
    return root


def _set_rules(monkeypatch, items):
    """親が調べものの道具の子へ渡す env の形で設定を入れる（子の経路そのもの）。"""
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: items}))


def _docs(hits):
    return [h["doc_id"] for h in hits]


def _grep(**kw):
    return G.grep_search(NEEDLE, WORLD, max_hits=50, **kw)


# ---- 検証・親子の整理 ----

@pytest.mark.parametrize("bad", ["../x", "a/../b", "/abs", "C:\\x", "a//b", "", "  ", ".", "a/./b", None, 3])
def test_normalize_path_rejects_unsafe(bad):
    with pytest.raises(PR.PathRulesError):
        PR.normalize_path(bad)


def test_normalize_path_strips_slashes():
    assert PR.normalize_path(" ZZsysA/ZZold/ ") == "ZZsysA/ZZold"


@pytest.mark.parametrize("entries", [
    [{"path": "a", "kind": "bogus"}],
    [{"path": "a", "kind": "exclude", "outside_scope": True}],
    [{"path": "a", "kind": "priority"}, {"path": "a/", "kind": "exclude"}],
    [{"path": "../a", "kind": "exclude"}],
    [{"path": "a", "kind": "priority", "outside_scope": "yes"}],
    "x",
])
def test_validate_entries_rejects(entries):
    with pytest.raises(PR.PathRulesError):
        PR.validate_entries(entries)


def test_collapse_drops_rules_under_a_ruled_parent():
    rules = PR.validate_entries([
        {"path": "A", "kind": "exclude"},
        {"path": "A/B", "kind": "priority", "outside_scope": True},
        {"path": "A/B/c.md", "kind": "exclude"},
        {"path": "AB", "kind": "priority"},   # 名前の接頭が同じだけの別のフォルダは親ではない
    ])
    kept, dropped = PR.collapse(rules)
    assert [r.path for r in kept] == ["A", "AB"]
    assert sorted(r.path for r in dropped) == ["A/B", "A/B/c.md"]


# ---- 判定 ----

def test_rules_judgement_and_scope_extension():
    r = PR.Rules(PR.validate_entries([
        {"path": "ZZold", "kind": "exclude"},
        {"path": "ZZspec", "kind": "priority"},
        {"path": "ZZcommon", "kind": "priority", "outside_scope": True},
        {"path": "ZZone.md", "kind": "priority", "outside_scope": True},
    ]))
    assert r.excluded("ZZold") and r.excluded("ZZold/x/y.md") and not r.excluded("ZZoldish/x.md")
    assert r.priority("ZZspec/a.md") and not r.priority("ZZold/a.md")
    ext = r.extend_scope(["ZZsysB"])
    assert list(ext) == ["ZZsysB", "ZZcommon", "ZZone.md"]
    assert r.extend_scope(ext) is ext                      # 足し込み済みは足し直さない
    assert r.extend_scope(None) is None and r.extend_scope([]) == []   # 範囲なし（全体）はそのまま
    assert list(r.extend_scope(["ZZcommon"])) == ["ZZcommon", "ZZone.md"]  # すでに範囲に入っている分は足さない
    assert r.outside_note("ZZcommon/a.md", ["ZZsysB"]) == PR.NOTE_OUTSIDE_SCOPE
    assert r.outside_note("ZZcommon/a.md", None) is None
    assert r.outside_note("ZZsysB/a.md", ["ZZsysB"]) is None
    assert r.outside_note("ZZspec/a.md", ["ZZsysB"]) is None   # 範囲の外でも読むが付いていない優先には注記しない
    hits = [{"doc_id": "x.md"}, {"doc_id": "ZZspec/a.md"}, {"doc_id": "y.md"}, {"doc_id": "ZZspec/b.md"}]
    assert [h["doc_id"] for h in r.prefer(hits)] == ["ZZspec/a.md", "ZZspec/b.md", "x.md", "y.md"]


def test_empty_rules_change_nothing():
    hits = [{"doc_id": "x.md"}]
    assert PR.EMPTY.prefer(hits) is hits and PR.EMPTY.extend_scope(["a"]) == ["a"]
    assert PR.EMPTY.es_must_not() == [] and PR.EMPTY.es_should() == [] and not PR.EMPTY.excluded("a")


# ---- grep（rg と純 Python 走査の両方） ----

ENGINES = ["auto", "python"]


@pytest.mark.parametrize("engine", ENGINES)
def test_grep_without_rules_is_unchanged(world, monkeypatch, engine):
    expected = ["ZZcommon/ZZrule.md", "ZZsysA/ZZold/ZZkeep.md", "ZZsysA/ZZold/ZZold.md",
                "ZZsysA/ZZspec/ZZmain.md", "ZZsysB/ZZnote.md", "ZZtop.md"]
    base = _docs(_grep(engine=engine))
    assert sorted(base) == expected
    _set_rules(monkeypatch, [])
    assert _docs(_grep(engine=engine)) == base
    # 別の資料フォルダに設定があっても、この資料フォルダの結果は変わらない
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({"other": [{"path": "ZZsysA", "kind": "exclude", "outside_scope": False}]}))
    assert _docs(_grep(engine=engine)) == base


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("scope", [None, ["ZZsysA"], ["ZZsysA/ZZold"], ["ZZsysB"]])
def test_grep_exclude_applies_whatever_the_scope(world, monkeypatch, engine, scope):
    _set_rules(monkeypatch, [{"path": "ZZsysA/ZZold", "kind": "exclude"},
                             {"path": "ZZtop.md", "kind": "exclude"}])
    got = _docs(_grep(engine=engine, scope_paths=scope))
    assert not [d for d in got if d.startswith("ZZsysA/ZZold") or d == "ZZtop.md"]
    if scope in (None, ["ZZsysA"]):
        assert "ZZsysA/ZZspec/ZZmain.md" in got
    if scope == ["ZZsysA/ZZold"]:
        assert got == []


@pytest.mark.parametrize("engine", ENGINES)
def test_grep_outside_scope_priority_is_read_outside_the_scope(world, monkeypatch, engine):
    _set_rules(monkeypatch, [{"path": "ZZcommon", "kind": "priority", "outside_scope": True},
                             {"path": "ZZtop.md", "kind": "priority", "outside_scope": True}])
    got = _docs(_grep(engine=engine, scope_paths=["ZZsysB"]))
    assert sorted(got) == ["ZZcommon/ZZrule.md", "ZZsysB/ZZnote.md", "ZZtop.md"]
    # 範囲の外でも読むが付いていない優先は範囲を広げない
    _set_rules(monkeypatch, [{"path": "ZZcommon", "kind": "priority"}])
    assert _docs(_grep(engine=engine, scope_paths=["ZZsysB"])) == ["ZZsysB/ZZnote.md"]


@pytest.mark.parametrize("engine", ENGINES)
def test_grep_priority_comes_first_in_the_selection(world, monkeypatch, engine):
    base = _docs(_grep(engine=engine))
    assert base[0] != "ZZsysB/ZZnote.md"
    _set_rules(monkeypatch, [{"path": "ZZsysB", "kind": "priority"}])
    got = _docs(_grep(engine=engine))
    assert got[0] == "ZZsysB/ZZnote.md" and sorted(got) == sorted(base)
    one = G.grep_search(NEEDLE, WORLD, max_hits=1, engine=engine)
    assert _docs(one) == ["ZZsysB/ZZnote.md"]       # 発見順の先頭 1 件でも優先が残る
    # 優先どうし・それ以外どうしの並びは従来のまま
    assert [d for d in got if d != "ZZsysB/ZZnote.md"] == [d for d in base if d != "ZZsysB/ZZnote.md"]


# ---- 出典・精読の確かめ ----

def test_verify_doc_exists_and_sources(world, monkeypatch):
    from sherpa import chat_service
    from sherpa.parts.read.tools import verify_doc_exists
    _set_rules(monkeypatch, [{"path": "ZZsysA/ZZold", "kind": "exclude"},
                             {"path": "ZZcommon", "kind": "priority", "outside_scope": True}])
    assert verify_doc_exists("ZZsysA/ZZspec/ZZmain.md", WORLD, ["ZZsysA"])
    assert not verify_doc_exists("ZZsysA/ZZold/ZZold.md", WORLD)
    assert not verify_doc_exists("ZZsysA/ZZold/ZZkeep.md", WORLD, ["ZZsysA"])
    assert verify_doc_exists("ZZcommon/ZZrule.md", WORLD, ["ZZsysB"])         # 範囲の外でも読む
    assert not verify_doc_exists("ZZsysA/ZZspec/ZZmain.md", WORLD, ["ZZsysB"])  # 普通の範囲外は従来どおり
    docs = ["ZZsysA/ZZspec/ZZmain.md", "ZZsysA/ZZold/ZZold.md", "ZZcommon/ZZrule.md"]
    src = chat_service._sources(docs, WORLD, ["ZZsysA"])
    assert [s["doc_id"] for s in src] == ["ZZsysA/ZZspec/ZZmain.md", "ZZcommon/ZZrule.md"]
    assert "note" not in src[0] and src[1]["note"] == PR.NOTE_OUTSIDE_SCOPE
    assert all("note" not in s for s in chat_service._sources(docs, WORLD, None))


# ---- 読み取りの道具 ----

def _run(name, args, scope=None, **kw):
    from sherpa.parts.read import tools
    return tools.run_tool(name, args, WORLD, scope, **kw)[0]


def test_read_tools_refuse_excluded_with_a_reason(world, monkeypatch):
    _set_rules(monkeypatch, [{"path": "ZZsysA/ZZold", "kind": "exclude"},
                             {"path": "ZZcommon", "kind": "priority", "outside_scope": True}])
    for tool in ("read_around", "read_doc", "doc_outline"):
        res = _run(tool, {"doc_id": "ZZsysA/ZZold/ZZold.md", "line": 1})
        assert res["error"] == PR.EXCLUDED_MESSAGE and res["error_code"] == PR.EXCLUDED_ERROR_CODE
    assert "error" not in _run("read_around", {"doc_id": "ZZsysA/ZZspec/ZZmain.md", "line": 1}, ["ZZsysA"])
    # 範囲の外でも読む
    assert "error" not in _run("read_around", {"doc_id": "ZZcommon/ZZrule.md", "line": 1}, ["ZZsysB"])
    assert "error" in _run("read_around", {"doc_id": "ZZsysA/ZZspec/ZZmain.md", "line": 1}, ["ZZsysB"])
    res = _run("file_head", {"doc_id": "ZZsysA/ZZold/ZZold.md"})
    assert res["error"] == PR.EXCLUDED_MESSAGE


def test_ripgrep_tool_hides_excluded_and_reads_outside_scope(world, monkeypatch):
    _set_rules(monkeypatch, [{"path": "ZZsysA/ZZold", "kind": "exclude"},
                             {"path": "ZZcommon", "kind": "priority", "outside_scope": True}])
    res = _run("ripgrep_search", {"query": NEEDLE}, ["ZZsysA", "ZZsysB"])
    docs = [h["doc_id"] for h in res["hits"]]
    assert "ZZsysA/ZZold/ZZold.md" not in docs and "ZZsysA/ZZold/ZZkeep.md" not in docs
    assert "ZZcommon/ZZrule.md" in docs and "ZZsysB/ZZnote.md" in docs
    res = _run("ripgrep_search", {"query": NEEDLE}, ["ZZsysB"])  # 範囲を 1 つだけ選んでいても、範囲の外でも読むは足される
    assert "ZZcommon/ZZrule.md" in [h["doc_id"] for h in res["hits"]]


def test_list_tools_hide_excluded(world, monkeypatch):
    from sherpa import doc_ledger
    from sherpa.parts.read import tools
    rows = [{"name": rel, "branch": "docs", "doctype": "md", "state": "ready"} for rel in FILES]

    from types import SimpleNamespace
    listing = SimpleNamespace(rows=rows, ledger_at=None)
    monkeypatch.setattr(doc_ledger, "listing_documents", lambda w, deadline=None, prefixes=None: listing)
    _set_rules(monkeypatch, [{"path": "ZZsysA/ZZold", "kind": "exclude"}])
    names = [d["rel_path"] for d in _run("list_docs", {})["docs"]]
    assert "ZZsysA/ZZold/ZZold.md" not in names and "ZZsysA/ZZspec/ZZmain.md" in names
    paths = _run("glob_search", {"pattern": "**/*.md"})["paths"]
    assert "ZZsysA/ZZold/ZZkeep.md" not in paths and "ZZsysB/ZZnote.md" in paths
    tree = _run("folder_tree", {"depth": 3})["folders"]
    assert "ZZsysA/ZZold" not in [f["path"] for f in tree]
    assert tools  # import 確認


# ---- グラフ ----

def test_graph_hides_excluded_documents(monkeypatch):
    from sherpa import graph_tools, scope
    token = graph_tools._RULES.set(PR.Rules(PR.validate_entries([{"path": "ZZold", "kind": "exclude"}])))
    try:
        assert graph_tools._sensitive_path("ZZold/a.cbl") and not graph_tools._sensitive_path("ZZnew/a.cbl")
        assert graph_tools._route_doc("ZZold/a.cbl", None) is None
        assert graph_tools._route_doc("ZZnew/a.cbl", None) == "ZZnew/a.cbl"
    finally:
        graph_tools._RULES.reset(token)
    assert not graph_tools._sensitive_path("ZZold/a.cbl")      # 設定が無い間は従来どおり
    rules = PR.Rules(PR.validate_entries([{"path": "ZZold", "kind": "exclude"}]))
    items = [
        {"name": "only-old", "evidence": {"edges": [{"doc": "ZZold/a.md"}], "grep": []}},
        {"name": "mixed", "evidence": {"edges": [{"doc": "ZZold/a.md"}, {"doc": "ZZnew/b.md"}], "grep": []}},
        {"name": "no-evidence"},
    ]
    out = scope.filter_items(items, None, rules)
    assert [i["name"] for i in out] == ["mixed", "no-evidence"]
    assert out[0]["evidence"]["edges"] == [{"doc": "ZZnew/b.md"}]
    assert scope.filter_items(items, None) is items            # 設定が無ければ素通し


# ---- ES ----

@pytest.fixture
def es_calls(monkeypatch):
    from sherpa import es_index
    calls = []
    monkeypatch.setattr(es_index, "available", lambda: True)

    def _req(method, path, body=None, **kw):
        calls.append(body)
        hx = [{"_id": "1", "_score": 1.0, "_source": {"doc_id": "x.md", "line": 1, "text": "t", "ext": ".md"}},
              {"_id": "2", "_score": 0.5, "_source": {"doc_id": "ZZspec/p.md", "line": 1, "text": "t", "ext": ".md"}}]
        if "minimum_should_match" in json.dumps(body.get("query", {})) and "ZZspec" in json.dumps(body.get("query", {})) \
                and '"should": [{"terms": {"scopes": ["ZZspec"]}}' in json.dumps(body.get("query", {})):
            hx = hx[1:]  # 優先の下だけに絞った問い合わせ
        return {"hits": {"hits": hx[:body.get("size", 99)]}}
    monkeypatch.setattr(es_index, "_req", _req)
    return calls


def test_es_filters_exclusions_and_boosts_priority(monkeypatch, es_calls):
    from sherpa import es_index
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: None)
    # 設定なし: 従来のクエリのまま
    hits, _ = es_index.search(WORLD, "q", scope_paths=["ZZsysA"], vector=False)
    plain = es_calls[-1]
    assert plain["query"]["bool"]["filter"] == [{"terms": {"scopes": ["ZZsysA"]}}]
    assert "should" not in plain["query"]["bool"] and [h["doc_id"] for h in hits] == ["x.md", "ZZspec/p.md"]
    _set_rules(monkeypatch, [{"path": "ZZold", "kind": "exclude"},
                             {"path": "ZZspec", "kind": "priority", "outside_scope": True}])
    hits, _ = es_index.search(WORLD, "q", scope_paths=["ZZsysA"], vector=False)
    assert len(es_calls) == 3                                                       # 優先の下だけを別に取り、通常の取得と統合する
    b = es_calls[-1]["query"]["bool"]
    assert {"bool": {"must_not": [{"terms": {"scopes": ["ZZold"]}}, {"terms": {"doc_id": ["ZZold"]}}]}} in b["filter"]
    assert b["filter"][0]["bool"]["should"][1] == {"terms": {"doc_id": ["ZZspec"]}}   # 範囲の外でも読むを範囲に足す
    assert "should" not in b                                                         # 取った後の並べ替えや押し上げに頼らない
    assert [h["doc_id"] for h in hits] == ["ZZspec/p.md", "x.md"]                   # 優先が上
    # 範囲なし（全体）でも除外は効く
    es_index.search(WORLD, "q", scope_paths=None, vector=False)
    assert es_calls[-1]["query"]["bool"]["filter"] == [
        {"bool": {"must_not": [{"terms": {"scopes": ["ZZold"]}}, {"terms": {"doc_id": ["ZZold"]}}]}}]
    es_index.search_exact(WORLD, ["ID1"], scope_paths=["ZZsysA"])
    assert any("must_not" in json.dumps(f) for f in es_calls[-1]["query"]["bool"]["filter"])


# ---- 調べものの道具の子プロセス・Codex の直読 ----

def test_mcp_env_passes_snapshot_and_child_reads_it(monkeypatch):
    from sherpa import store
    from sherpa.providers.codex.mcp import _mcp_env
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)   # 親として動かす（他の試験が子の印を残していても）
    monkeypatch.delenv(PR.ENV_KEY, raising=False)
    monkeypatch.setattr(PR, "_last_good", None)
    monkeypatch.setattr(PR, "_fail_until", 0.0)
    saved = {PR.SETTINGS_KEY: {WORLD: [{"path": "ZZold", "kind": "exclude", "outside_scope": False},
                                       {"path": "ZZcommon", "kind": "priority", "outside_scope": True}],
                               "other": [{"path": "ZZx", "kind": "exclude", "outside_scope": False}]}}
    monkeypatch.setattr(store, "get_system_settings", lambda **k: saved)
    env = _mcp_env(WORLD, None)
    snap = json.loads(env[PR.ENV_KEY])
    assert list(snap) == [WORLD] and [r["path"] for r in snap[WORLD]] == ["ZZold", "ZZcommon"]  # 他の資料フォルダは渡さない
    assert PR.ENV_KEY not in _mcp_env("nope", None)                    # 設定が無い資料フォルダには渡さない
    # 子（DB を読めない）はこの写しだけで判定する
    monkeypatch.setattr(store, "get_system_settings", lambda **k: pytest.fail("子は DB を読まない"))
    monkeypatch.setenv(PR.ENV_KEY, env[PR.ENV_KEY])
    monkeypatch.setenv("SHERPA_MCP_WORLD", WORLD)
    r = PR.rules_for(WORLD)
    assert r.excluded("ZZold/a.md") and r.outside_paths == ("ZZcommon",)
    monkeypatch.delenv(PR.ENV_KEY)
    assert PR.rules_for(WORLD).empty                                    # 写しが無い子は設定なし（DB は読まない）


def test_sandbox_deny_includes_excluded_paths(tmp_path):
    from sherpa.providers.codex import sandbox
    kb, md, rag = tmp_path / "kb", tmp_path / "md", tmp_path / "rag"
    for d in (kb / "A" / "old", kb / "A" / "keep", md / "A", rag / "A", kb / "B"):
        d.mkdir(parents=True)
    (kb / "A" / "old" / "x.md").write_text("x")
    (kb / "A" / "keep" / "n.xlsx").write_text("x")
    (md / "A" / "keep" / "n.xlsx.md").parent.mkdir(parents=True, exist_ok=True)
    (md / "A" / "keep" / "n.xlsx.md").write_text("x")
    (rag / "A" / "keep" / "n.xlsx.rag.md").parent.mkdir(parents=True, exist_ok=True)
    (rag / "A" / "keep" / "n.xlsx.rag.md").write_text("x")
    (kb / "A" / "link").symlink_to(kb / "B")
    rules = PR.Rules(PR.validate_entries([
        {"path": "A/old", "kind": "exclude", "target_type": "folder"},
        {"path": "A/keep/n.xlsx", "kind": "exclude", "target_type": "file"},
        {"path": "B/gone", "kind": "exclude"}]))
    deny = sandbox._exclude_deny_entries([str(kb), str(md), str(rag)], rules,
                                         derived_roots=[str(md.resolve()), str(rag.resolve())])
    r = lambda p: str(p.resolve())  # noqa: E731
    assert r(kb / "A" / "old") in deny and r(kb / "A" / "keep" / "n.xlsx") in deny
    assert r(md / "A" / "keep" / "n.xlsx.md") in deny and r(rag / "A" / "keep" / "n.xlsx.rag.md") in deny
    assert not any(d.endswith("gone") for d in deny)                               # 無い場所は書かない
    # 範囲の deny の配下は重ねない
    deny2 = sandbox._exclude_deny_entries([str(kb)], rules, [r(kb / "A")])
    assert deny2 == []
    assert sandbox._exclude_deny_entries([str(kb)], PR.EMPTY) == []
    # 範囲の外でも読む優先は、読める側（範囲の deny の計算）に足される
    scope_deny = sandbox._scope_deny_entries(
        [str(kb)], PR.Rules(PR.validate_entries([{"path": "B", "kind": "priority", "outside_scope": True}])).extend_scope(["A/keep"]))
    assert r(kb / "B") not in scope_deny and r(kb / "A" / "old") in scope_deny
    assert os.path.isdir(kb)


# ---- 敵対レビュー 1 巡目の是正（実害の再現） ----

@pytest.fixture
def clean_state(monkeypatch):
    monkeypatch.setattr(PR, "_last_good", None)
    monkeypatch.setattr(PR, "_fail_until", 0.0)
    monkeypatch.delenv(PR.ENV_KEY, raising=False)
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)


def test_unreadable_settings_keep_last_good_and_fail_closed_when_never_read(monkeypatch, clean_state):
    """1: DB の一時障害で除外が全解除にならない。"""
    from sherpa import store
    good = {PR.SETTINGS_KEY: {WORLD: [{"path": "ZZold", "kind": "exclude", "outside_scope": False}]}}
    box = {"v": good}

    def _get(**k):
        if box["v"] is None:
            raise OSError("db down")
        return box["v"]
    monkeypatch.setattr(store, "get_system_settings", _get)
    assert PR.rules_for(WORLD).excluded("ZZold/a.md")
    box["v"] = None
    monkeypatch.setattr(PR, "_fail_until", 0.0)
    assert PR.rules_for(WORLD).excluded("ZZold/a.md")          # 最後に読めた写しで除外を保つ
    # 一度も読めていないときは止める
    monkeypatch.setattr(PR, "_last_good", None)
    monkeypatch.setattr(PR, "_fail_until", 0.0)
    with pytest.raises(PR.PathRulesUnavailable):
        PR.rules_for(WORLD)
    res = _run("ripgrep_search", {"query": NEEDLE})
    assert res["error"] == PR.UNAVAILABLE_MESSAGE == "除外の設定を読めないため検索できません"
    # 設定が 1 件も無いと読めているときの通常の流れは変わらない
    box["v"] = {}
    monkeypatch.setattr(PR, "_fail_until", 0.0)
    assert PR.rules_for(WORLD).empty


def test_snapshot_size_limit():
    """6: 環境変数 1 つに入らない大きさの設定は保存で拒否する。"""
    big = PR.validate_entries([{"path": f"ZZ{i:04d}/" + "あ" * 200, "kind": "exclude"} for i in range(400)])
    with pytest.raises(PR.PathRulesError, match="大きすぎ"):
        PR.check_snapshot_size(big)
    PR.check_snapshot_size(PR.validate_entries([{"path": "ZZa", "kind": "exclude"}]))
    with pytest.raises(PR.PathRulesError):
        PR.validate_entries([{"path": f"p{i}", "kind": "exclude"} for i in range(PR.MAX_RULES_PER_WORLD + 1)])


def test_dot_segments_do_not_bypass_exclusion():
    r = PR.Rules(PR.validate_entries([{"path": "ZZold", "kind": "exclude"}]))
    assert r.excluded("ZZsysA/../ZZold/a.md") and r.excluded("./ZZold/a.md") and r.excluded("ZZold//a.md")
    assert not r.excluded("ZZsysA/./ZZnew.md")


def _impact_result():
    ev = lambda doc: [{"type": "INVOKES", "from": "a", "to": "b", "doc": doc,  # noqa: E731
                       "sources": [{"doc_id": doc, "via": "x"}, {"doc_id": "ZZnew/ok.md", "via": "y"}]}]
    return {
        "start": "T", "starts": [{"name": "T", "path": "ZZnew/t.cbl"}],
        "items": [
            {"name": "hidden-path", "path": "ZZold/h.cbl", "evidence": ev("ZZnew/e.md")},
            {"name": "hidden-trace", "path": "ZZnew/x.cbl", "trace_paths": ["ZZnew/t.cbl", "ZZold/m.cbl"], "evidence": ev("ZZnew/e.md")},
            {"name": "hidden-proof", "path": "ZZnew/y.cbl", "evidence": ev("ZZold/proof.md")},
            {"name": "kept", "path": "ZZnew/k.cbl", "evidence": ev("ZZnew/e.md")},
        ],
        "candidates": [{"name": "flow-hidden", "path": "ZZold/f.cbl", "evidence": []}],
        "unresolved": {"available": True, "omitted": 0, "items": [
            {"path": "ZZold/u.cbl", "name": "SECRET"}, {"path": "ZZnew/u.cbl", "name": "OK"},
            {"path": "ZZnew/v.cbl", "name": "VIA", "from_def": {"file": "ZZold/d.cbl"}}]},
    }


def test_impact_results_hide_excluded_documents(monkeypatch):
    """2: 影響調査の起点・経路・根拠・未解決の参照から除外の資料を落とす（結果の最後の 1 か所）。"""
    from sherpa import impact_service
    from sherpa.ingest import world_neo4j
    _set_rules(monkeypatch, [{"path": "ZZold", "kind": "exclude"}])
    monkeypatch.setattr(world_neo4j, "run_world_impact", lambda *a, **k: _impact_result())
    out = impact_service.run_impact(None, "T", WORLD, include_presumed=False)
    assert [i["name"] for i in out["items"]] == ["kept"]
    assert out["candidates"] == []
    assert [u["name"] for u in out["unresolved"]["items"]] == ["OK"]
    assert "ZZold" not in json.dumps(out, ensure_ascii=False)
    # 起点がすべて除外の資料なら、存在を示さない
    res = _impact_result()
    res["starts"] = [{"name": "T", "path": "ZZold/t.cbl"}]
    monkeypatch.setattr(world_neo4j, "run_world_impact", lambda *a, **k: res)
    out = impact_service.run_impact(None, "T", WORLD, include_presumed=False)
    assert out["items"] == [] and out["candidates"] == [] and out["unresolved"]["items"] == []
    # 設定が無ければそのまま
    monkeypatch.delenv(PR.ENV_KEY)
    base = _impact_result()
    monkeypatch.setattr(world_neo4j, "run_world_impact", lambda *a, **k: base)
    assert len(impact_service.run_impact(None, "T", WORLD, include_presumed=False)["items"]) == 4


def test_neighbor_unresolved_hides_excluded(monkeypatch):
    """5: 近傍の未解決の参照にも同じ規則を当てる。"""
    from sherpa import lens_service
    _set_rules(monkeypatch, [{"path": "ZZold", "kind": "exclude"}])
    monkeypatch.setattr(lens_service, "read_unresolved", lambda *a, **k: {"available": True, "omitted": 0, "items": [
        {"path": "ZZold/u.cbl", "name": "SECRET"}, {"path": "ZZnew/u.cbl", "name": "OK"}]})
    cards = lens_service._Cards([], lens_service.Coverage())
    lens_service._attach_unresolved(None, cards, WORLD, ["t"], None)
    assert [u["name"] for u in cards.unresolved["items"]] == ["OK"]


def test_graph_run_passes_effective_scope_and_sources_note_outside_scope(world, monkeypatch):
    """2・7: グラフの道具へ範囲の外でも読むを足した範囲を渡し、回答の出典に注記を付ける。"""
    from sherpa import chat_service as CS
    from sherpa import graph_tools
    _set_rules(monkeypatch, [{"path": "ZZcommon", "kind": "priority", "outside_scope": True}])
    cap = {}

    def fake_run(name, args, world, scope_paths, *a, **k):
        cap["sp"] = list(scope_paths)
        return {}
    monkeypatch.setattr(graph_tools, "_run", fake_run)
    graph_tools.run("graph_impact", {}, WORLD, ["ZZsysB"])
    assert cap["sp"] == ["ZZsysB", "ZZcommon"]
    assert CS._sources(["ZZcommon/ZZrule.md"], WORLD, ["ZZsysB"])[0]["note"] == PR.NOTE_OUTSIDE_SCOPE


def test_compare_documents_rejects_excluded_counterpart(world, monkeypatch, tmp_path):
    """3: 世代の自動対応付けで、除外の資料を完全一致・候補・読む直前のどれでも通さない。"""
    from types import SimpleNamespace
    from sherpa import compare_docs, doc_ledger
    from sherpa import worlds as W
    rag = tmp_path / "rag"
    for doc, body in (("ZZsysA/ZZx.docx", "# a\nSHA\n"), ("ZZsysB/ZZx.docx", "# b\nSECRETBODY\n")):
        p = rag / (doc + ".rag.md")
        p.parent.mkdir(parents=True)
        p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(W, "rag_md_path", lambda w, d: rag / (d + ".rag.md"))
    monkeypatch.setattr(compare_docs, "_doc_exists", lambda d, w: True)
    monkeypatch.setattr(doc_ledger, "listing_documents", lambda w, deadline=None, prefixes=None: SimpleNamespace(
        rows=[{"name": "ZZsysB/ZZx.docx"}, {"name": "ZZsysB/ZZy.docx"}], ledger_at=None))
    args = {"source_doc_id": "ZZsysA/ZZx.docx", "target_generation": "ZZsysB"}
    assert compare_docs.compare(WORLD, args)["status"] == "comparable"        # 設定なしは従来どおり
    _set_rules(monkeypatch, [{"path": "ZZsysB/ZZx.docx", "kind": "exclude"}])
    out = compare_docs.compare(WORLD, args)
    assert out["status"] == "needs_disambiguation" and "ZZsysB/ZZx.docx" not in out["candidates"]
    assert "SECRETBODY" not in json.dumps(out, ensure_ascii=False)
    pair = compare_docs.compare(WORLD, {"left_doc_id": "ZZsysA/ZZx.docx", "right_doc_id": "ZZsysB/ZZx.docx"})
    assert "error" in pair and "diff" not in pair
    res = _run("compare_documents", args)
    assert "SECRETBODY" not in json.dumps(res, ensure_ascii=False)
