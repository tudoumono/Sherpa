"""エージェント検索（索引なし・LLM が grep ツールを反復）の run_tool 単体テスト。LLM は stub（コスト0）。

tool_dispatch.run_tool と parts/read/tools.py のツール（ripgrep_search / es_search / read_around / read_doc /
doc_outline / list_docs / glob_search / graph_neighbors / 原本読取）を v1 フィクスチャまたは tmp の KB で確かめる。
Neo4j・実埋め込み不要。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import time
import urllib.error

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")   # es_search が実埋め込みを叩かない（BM25）
import pytest  # noqa: E402
from sherpa import agentic_search as A   # noqa: E402
from sherpa.parts.read import tools as RT   # noqa: E402
from sherpa import grep_tool as grep_tool_mod   # noqa: E402
from sherpa import tool_dispatch as TD   # noqa: E402
from sherpa import worlds as worlds_mod   # noqa: E402
from sherpa import store   # noqa: E402
import _corpus_expect as CE   # noqa: E402   # フィクスチャ実走査ベースの list_docs 期待値
import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証

# fixtures/corpus/v1 の実コーパス（`.md`＝資料・`.cbl`/`.cpy`＝コード）の固定分類を前提にするため、登録簿を上流限定に固定する。
pytestmark = pytest.mark.usefixtures("upstream_only_registry")


# ===== run_tool: 基本・転送・層 =====

def test_run_tool_search_read_and_scope():
    res, docs, cites, cards = TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    assert res["hits"] and docs and cites and cards == []
    assert all("span" in c and "quote" in c for c in cites)
    h = res["hits"][0]
    r2, d2, _, _ = TD.run_tool("read_around", {"doc_id": h["doc_id"], "line": h["line"], "window": 2}, "v1", None)
    assert "text" in r2 and h["doc_id"] in d2
    r3, _, _, _ = TD.run_tool("read_around", {"doc_id": h["doc_id"], "line": 1}, "v1", ["5期"])   # 範囲外
    assert "error" in r3
    r4, d4, c4, k4 = TD.run_tool("rm_rf", {}, "v1", None)                                         # 未知ツール
    assert "error" in r4 and d4 == set() and c4 == [] and k4 == []


# "TAX-RATE" は fixtures/corpus/v1 に資料（.md）・コード（.cbl/.cpy）の両方に実在する語。
_CODE_EXTS = {".cbl", ".cpy", ".cob", ".cobol", ".copybook", ".jcl"}


def _doc_exts(res) -> set:
    return {pathlib.Path(h["doc_id"]).suffix.lower() for h in res["hits"]}


def test_run_tool_ripgrep_search_layer_filters():
    omitted, _, _, _ = TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    both, _, _, _ = TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="both")
    assert ".md" in _doc_exts(omitted) and ".cbl" in _doc_exts(omitted)
    assert {h["doc_id"] for h in omitted["hits"]} == {h["doc_id"] for h in both["hits"]}
    exts_code = _doc_exts(TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="code")[0])
    assert exts_code and exts_code <= _CODE_EXTS
    exts_docs = _doc_exts(TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="docs")[0])
    assert exts_docs and not (exts_docs & _CODE_EXTS)


@pytest.mark.parametrize("call_kw, args_extra, expected", [
    ({"layer": "code"}, {}, {"layer": "code"}),
    ({"max_hits": 45}, {}, {"max_hits": 45}),
    ({}, {}, {"max_hits": A.MAX_HITS, "offset": 0}),     # 省略はモジュール既定・offset 既定 0
    ({}, {"offset": 15}, {"offset": 15}),
    ({}, {"offset": -7}, {"offset": 0}),                 # 負の offset は 0 扱い
    ({"deadline": 123.5}, {}, {"deadline": 123.5}),      # 同期ツリー検索を打ち切れる経路は deadline のみ
])
def test_run_tool_forwards_options_to_grep_search(monkeypatch, call_kw, args_extra, expected):
    captured = {}
    monkeypatch.setattr(grep_tool_mod, "grep_search", lambda *a, **kw: captured.update(kw) or [])
    TD.run_tool("ripgrep_search", {"query": "x", **args_extra}, "v1", None, **call_kw)
    assert {k: captured.get(k) for k in expected} == expected


def test_run_tool_ripgrep_search_hit_view_carries_importance_conditionally(monkeypatch):
    hits = [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "本文A", "ext": ".md",
             "importance": "高", "importance_reason": "契約書"},
            {"doc_id": "b.md", "line": 1, "span": [1, 1], "text": "本文B", "ext": ".md"}]
    monkeypatch.setattr(grep_tool_mod, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = TD.run_tool("ripgrep_search", {"query": "x"}, "v1", None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["a.md"]["importance"] == "高" and by_doc["a.md"]["importance_reason"] == "契約書"
    assert "importance" not in by_doc["b.md"] and "importance_reason" not in by_doc["b.md"]


def test_run_tool_es_search_forwards_options_and_ignores_offset(monkeypatch):
    """es_search は layer・max_hits(k)・deadline を転送し、offset ページングは持たない。"""
    from sherpa import documents

    captured, rel_set_kw = {}, {}

    def fake_search(world, q, scope_paths=None, k=20, layer=None, **kw):
        captured.update(kw, k=k, layer=layer)
        return [], None

    def fake_rel_set(world, **kw):
        rel_set_kw.update(kw)
        return set()

    monkeypatch.setattr(A.es_index, "search", fake_search)
    monkeypatch.setattr(documents, "world_rel_set", fake_rel_set)
    TD.run_tool("es_search", {"query": "x", "offset": 40}, "v1", None, layer="docs", max_hits=60, deadline=123.5)
    assert captured["layer"] == "docs" and captured["k"] == 60 and "offset" not in captured
    assert rel_set_kw.get("deadline") == 123.5


def test_run_tool_es_search_surfaces_degrade_reason_in_result(monkeypatch):
    """BM25 縮退の理由は tool result に degrade_reason として載る（None のときはキー自体を作らない）。"""
    from sherpa import documents

    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {"a.md"})
    monkeypatch.setattr(A.es_index, "search", lambda world, q, scope_paths=None, k=20, layer=None, **kw: (
        [{"doc_id": "a.md", "line": 1, "text": "x", "ext": ".md"}], "embedding_cloud_unavailable"))
    view, _docs, _cites, _cards = TD.run_tool("es_search", {"query": "x"}, "v1", None)
    assert view["degrade_reason"] == "embedding_cloud_unavailable"
    monkeypatch.setattr(A.es_index, "search", lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([], None))
    view2, _docs, _cites, _cards = TD.run_tool("es_search", {"query": "x"}, "v1", None)
    assert "degrade_reason" not in view2


def test_run_tool_es_search_result_never_has_next_offset(monkeypatch):
    from sherpa import documents

    hits = [{"doc_id": f"a{i}.md", "line": 1, "text": "x", "ext": ".md", "score": 1.0} for i in range(5)]
    monkeypatch.setattr(A.es_index, "search", lambda *a, **kw: (hits, None))
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {h["doc_id"] for h in hits})
    res, _docs, _cites, _cards = TD.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=5)
    assert res.get("truncated") is True and "next_offset" not in res


def test_run_tool_read_around_rejects_doc_outside_layer():
    res, _, _, _ = TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="code")
    code_doc_id = res["hits"][0]["doc_id"]
    r_reject, _, _, _ = TD.run_tool("read_around", {"doc_id": code_doc_id, "line": 1}, "v1", None, layer="docs")
    assert "error" in r_reject
    r_ok, docs_ok, _, _ = TD.run_tool("read_around", {"doc_id": code_doc_id, "line": 1}, "v1", None, layer="code")
    assert "error" not in r_ok and code_doc_id in docs_ok


def test_run_tool_graph_neighbors_layer_restriction(monkeypatch):
    """層限定中は graph_neighbors 自体を拒否する（graph 経由で層外が漏れる迂回路を塞ぐ）。省略/both は通常実行。"""
    res_omitted, _, _, _ = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    res_both, _, _, _ = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None, layer="both")
    assert "error" not in res_omitted and "error" not in res_both

    from sherpa import lens_service

    def _boom(world, term, sp=None):
        raise AssertionError("層限定なのに neighbor_cards が呼ばれている")

    monkeypatch.setattr(lens_service, "neighbor_cards", _boom)
    for lyr in ("docs", "code"):
        res, docs, cites, cards = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None, layer=lyr)
        assert "error" in res and docs == set() and cites == [] and cards == []


# ===== ripgrep_search: offset ページング・ヒット単位のバイト上限 =====

def test_run_tool_offset_past_abs_ceiling_short_circuits_without_calling_search(monkeypatch):
    """offset は LLM が渡す未検証値。巻き戻さず、offset >= MAX_HITS_ABS_MAX なら検索を呼ばず空を返す
    （grep 側のヒープ肥大＝DoS を防ぎ、天井超えの領域へ永久に届かなくなる巻き戻しも避ける）。"""
    captured = {"called": False}
    monkeypatch.setattr(grep_tool_mod, "grep_search", lambda *a, **kw: captured.update(called=True) or [])
    res, _docs, _cites, _cards = TD.run_tool(
        "ripgrep_search", {"query": "x", "offset": 10_000_000}, "v1", None, max_hits=30)
    assert res == {"hits": []} and captured["called"] is False


def test_run_tool_ripgrep_search_offset_pages_through_real_corpus(monkeypatch, tmp_path):
    """offset を進めると全件を重複・欠落なくたどれ、続きがある間だけ truncated/next_offset が付く。"""
    world = "offset-paging-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        name: "NEEDLE 行\n" for name in ("a.md", "b.md", "c.md", "d.md", "e.md")})

    def _page(offset):
        return TD.run_tool("ripgrep_search", {"query": "NEEDLE", "offset": offset}, world, None, max_hits=2)[0]

    p0 = _page(0)
    assert [h["doc_id"] for h in p0["hits"]] == ["a.md", "b.md"]
    assert p0.get("truncated") is True and p0.get("next_offset") == 2
    p1 = _page(p0["next_offset"])
    assert [h["doc_id"] for h in p1["hits"]] == ["c.md", "d.md"]
    assert p1.get("truncated") is True and p1.get("next_offset") == 4
    p2 = _page(p1["next_offset"])
    assert [h["doc_id"] for h in p2["hits"]] == ["e.md"]
    assert "truncated" not in p2 and "next_offset" not in p2


def test_run_tool_ripgrep_search_offset_near_ceiling_shrinks_page_without_rolling_back(monkeypatch, tmp_path):
    """母集団が MAX_HITS_ABS_MAX を超えるとき、天井近くの offset は巻き戻らずページ件数だけが縮み、
    天井ちょうどに達したページは next_offset を出さない。"""
    world = "offset-ceiling-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        name: "NEEDLE 行\n" for name in ("a.md", "b.md", "c.md", "d.md", "e.md", "f.md", "g.md", "h.md")})
    monkeypatch.setattr(A, "MAX_HITS_ABS_MAX", 5)
    monkeypatch.setattr(RT, "MAX_HITS_ABS_MAX", 5)

    def _page(offset):
        return TD.run_tool("ripgrep_search", {"query": "NEEDLE", "offset": offset}, world, None, max_hits=3)[0]

    p0 = _page(0)
    assert [h["doc_id"] for h in p0["hits"]] == ["a.md", "b.md", "c.md"]
    assert p0.get("truncated") is True and p0.get("next_offset") == 3
    p1 = _page(3)
    assert [h["doc_id"] for h in p1["hits"]] == ["d.md", "e.md"]
    assert p1.get("truncated") is True and "next_offset" not in p1
    assert _page(5) == {"hits": []}


def _run_rg_with_hits(monkeypatch, hits, **kw):
    monkeypatch.setattr(grep_tool_mod, "grep_search", lambda *a, **_kw: hits)
    kw = {"max_hits": 30, "tool_result_max_bytes": 64 * 1024, **kw}
    return TD.run_tool("ripgrep_search", {"query": "x"}, "v1", None, **kw)[0]


def _md_hit(doc_id="a.md", text="short"):
    return {"doc_id": doc_id, "line": 1, "span": [1, 1], "text": text, "ext": ".md"}


@pytest.mark.parametrize("max_hits, expected_bytes", [(30, 2184), (200, 512)])
def test_run_tool_ripgrep_search_hit_text_clipped_to_per_hit_budget(monkeypatch, max_hits, expected_bytes):
    """per_hit = max(_HIT_TEXT_MIN_BYTES, tool_result_max_bytes // max_hits) で text を末尾クリップし、
    切ったヒットだけに text_truncated を付ける（下限 512）。"""
    big_text = "x" * 5000
    res = _run_rg_with_hits(monkeypatch, [_md_hit(text=big_text)], max_hits=max_hits)
    hit = res["hits"][0]
    assert len(hit["text"].encode("utf-8")) == expected_bytes and hit["text"] == big_text[:expected_bytes]
    assert hit["text_truncated"] is True
    assert RT._HIT_TEXT_MIN_BYTES == 512


def test_run_tool_ripgrep_search_serialized_view_stays_within_budget_despite_per_hit_overhead(monkeypatch):
    """付帯情報・JSON 構造分を含めた直列化後の実バイト数で収まりを保証し、next_offset と全 doc_id は残る。"""
    hits = [_md_hit(f"doc{i:03d}.md", "x" * 5000) for i in range(30)]
    res = _run_rg_with_hits(monkeypatch, hits)
    assert len(json.dumps(res, ensure_ascii=False).encode("utf-8")) <= 64 * 1024
    assert [h["doc_id"] for h in res["hits"]] == [f"doc{i:03d}.md" for i in range(30)]
    assert res.get("next_offset") == 30 and res.get("truncated") is True


def test_run_tool_ripgrep_search_text_truncated_flags(monkeypatch):
    """いずれかのヒットが切られたら最上位にも text_truncated（統計・MCP は最上位だけを見る）。
    切られないヒット・1 件も切られない結果にはキー自体を作らない。"""
    res = _run_rg_with_hits(monkeypatch, [_md_hit("a.md", "x" * 5000), _md_hit("b.md", "short")])
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert res["text_truncated"] is True and by_doc["a.md"]["text_truncated"] is True
    assert "text_truncated" not in by_doc["b.md"]
    small = "NEEDLE を含む短い一行"
    res2 = _run_rg_with_hits(monkeypatch, [_md_hit(text=small)])
    assert res2["hits"][0]["text"] == small
    assert "text_truncated" not in res2 and "text_truncated" not in res2["hits"][0]


def test_run_tool_ripgrep_search_md_heading_survives_per_hit_clip(monkeypatch, tmp_path):
    world = "hit-cap-md-world"
    heading = "# TITLE"
    body = ("NEEDLE line " + "x" * 60 + "\n") * 50
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": heading + "\n" + body, "small.md": "NEEDLE small line\n"})
    res, _docs, _cites, _cards = TD.run_tool(
        "ripgrep_search", {"query": "NEEDLE"}, world, None, max_hits=2, tool_result_max_bytes=4096)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    big_hit = by_doc["big.md"]
    assert len(big_hit["text"].encode("utf-8")) == 2048 and big_hit["text"].startswith(heading)
    assert big_hit["text_truncated"] is True
    assert by_doc["small.md"]["text"] == "NEEDLE small line" and "text_truncated" not in by_doc["small.md"]


def test_run_tool_window_cap_raises_read_around_ceiling(monkeypatch, tmp_path):
    """read_around の安全弁クランプ max(200, window_cap or READ_WINDOW)。window_cap で 200 を超えて広げられる。"""
    world = "depth-window-world"
    lines = [f"line {i}" if i != 250 else "line 250: TAX-RATE" for i in range(1, 501)]
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "\n".join(lines)})
    args = {"doc_id": "big.md", "line": 250, "window": 1000}
    assert TD.run_tool("read_around", args, world, None)[0]["text"].splitlines()[0] == "50: line 50"
    assert TD.run_tool("read_around", args, world, None, window_cap=1000)[0]["text"].splitlines()[0] == "1: line 1"


def test_run_tool_read_around_default_window_scales_with_window_cap(monkeypatch, tmp_path):
    """window 省略時の既定値にも window_cap（調べる深さの実効値）を使う。"""
    world = "depth-window-default-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "\n".join(f"line {i}" for i in range(1, 301))})
    for window_cap, expected_first_line in ((40, 110), (60, 90), (80, 70), (90, 60), (120, 30)):
        res, _, _, _ = TD.run_tool("read_around", {"doc_id": "big.md", "line": 150}, world, None, window_cap=window_cap)
        assert "error" not in res, res
        assert res["text"].splitlines()[0] == f"{expected_first_line}: line {expected_first_line}", window_cap


# ===== deadline（同期ツリー走査を打ち切る）=====

def test_run_tool_forwards_deadline_to_documents_for_for_list_docs(monkeypatch):
    from sherpa import doc_ledger as DL

    captured = {}

    def _spy(world, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(DL, "documents_for", _spy)
    TD.run_tool("list_docs", {}, "v1", None, deadline=123.5)
    assert captured.get("deadline") == 123.5


@pytest.mark.parametrize("tool", ["list_docs", "es_search"])
def test_run_tool_tree_walk_raises_when_deadline_already_past(tool):
    """list_docs・es_search の実在集合走査も deadline 超過なら ScopeWalkDeadlineExceeded（es_index.search へ進まない）。"""
    import time as time_mod

    from sherpa import scope_infer

    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        TD.run_tool(tool, {"query": "x"} if tool == "es_search" else {}, "v1", None, deadline=time_mod.monotonic() - 1)


# ===== _openai_style_text（OpenAI refusal 応答の本文抽出） =====

@pytest.mark.parametrize("message, expected", [
    ({"content": "本文", "refusal": "拒否理由"}, "本文"),
    ({"content": None, "refusal": "この内容にはお答えできません。"}, "この内容にはお答えできません。"),   # refusal は content=null で来る
    ({"refusal": "お答えできません。"}, "お答えできません。"),
    ({}, ""),
    ({"content": None, "refusal": None}, ""),
    ({"content": "  本文  "}, "本文"),
])
def test_openai_style_text(message, expected):
    assert A._openai_style_text(message) == expected


# ===== list_docs =====

def test_list_docs_path_prefix_and_doctype():
    res, docs, cites, cards = TD.run_tool("list_docs", {"path_prefix": "4期/02_設計"}, "v1", None)
    expected = CE.count_under("4期/02_設計")
    assert res["count"] == expected and len(res["docs"]) == expected
    assert cites == [] and cards == []
    assert all(d["rel_path"].startswith("4期/02_設計/") for d in res["docs"])
    assert all(d["doctype"] == "設計書" for d in res["docs"])
    assert docs == {d["rel_path"] for d in res["docs"]}


def test_list_docs_name_pattern_matches_path_not_just_content():
    res, _, _, _ = TD.run_tool("list_docs", {"name_pattern": "請求"}, "v1", None)
    expected = CE.rel_paths_matching("請求")
    assert res["count"] == len(expected) and {d["rel_path"] for d in res["docs"]} == expected


def test_list_docs_count_independent_of_limit():
    res, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "4期", "limit": 5}, "v1", None)
    assert res["count"] == CE.count_under("4期") and len(res["docs"]) == 5


def test_list_docs_offset_pages_through_all_rows_without_gap_or_overlap():
    """limit ずつ offset を進めた和集合が全件と一致（単一フォルダに 500 超でも取り切れる）。範囲外は空・count 不変。"""
    total = CE.count_under("4期")
    seen: list = []
    for off in range(0, total, 3):
        res, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "4期", "limit": 3, "offset": off}, "v1", None)
        assert res["count"] == total and res["offset"] == off
        seen += [d["rel_path"] for d in res["docs"]]
    full, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "4期", "limit": 500}, "v1", None)
    assert seen == [d["rel_path"] for d in full["docs"]]
    res, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "4期", "offset": total + 10}, "v1", None)
    assert res["count"] == total and res["docs"] == []
    res, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "4期", "offset": "x"}, "v1", None)
    assert res["offset"] == 0


def test_list_docs_respects_session_scope_and_unknown_world():
    res, docs, _, _ = TD.run_tool("list_docs", {}, "v1", ["4期/03_開発"])
    assert res["count"] == CE.count_under("4期/03_開発")
    assert all(d["rel_path"].startswith("4期/03_開発/") for d in res["docs"])
    assert docs and docs == {d["rel_path"] for d in res["docs"]}
    res2, docs2, _, _ = TD.run_tool("list_docs", {}, "no-such-world-xyz", None)
    assert res2 == {"count": 0, "offset": 0, "docs": [], "truncated": False, "next_offset": None} and docs2 == set()


def test_list_docs_layer_code_and_docs_partition_the_prefix():
    """list_docs にも層フィルタを適用する（列挙対象自体を絞り、層外の件数/パスを根拠に載せない）。"""
    from sherpa.doc_kinds import CODE_EXT
    prefix = "4期"
    all_rels = CE.rel_paths_under(prefix)
    code_rels = {r for r in all_rels if pathlib.Path(r).suffix.lower() in CODE_EXT}
    docs_rels = all_rels - code_rels
    assert code_rels and docs_rels

    res_code, docs_out, _, _ = TD.run_tool("list_docs", {"path_prefix": prefix}, "v1", None, layer="code")
    assert {d["rel_path"] for d in res_code["docs"]} == code_rels == docs_out
    assert res_code["count"] == len(code_rels)
    res_docs, _, _, _ = TD.run_tool("list_docs", {"path_prefix": prefix}, "v1", None, layer="docs")
    assert {d["rel_path"] for d in res_docs["docs"]} == docs_rels and res_docs["count"] == len(docs_rels)
    res_both, _, _, _ = TD.run_tool("list_docs", {"path_prefix": prefix}, "v1", None)
    assert {d["rel_path"] for d in res_both["docs"]} == all_rels


def test_list_docs_pagination_covers_all_without_gaps_or_dupes(monkeypatch):
    """並びは常に rel_path 昇順固定＝offset を進めた複数ページの和が全件と一致し重複も欠落も無い。"""
    from sherpa import doc_ledger as DL

    names = [f"p/{n}.md" for n in ["c", "a", "e", "b", "d", "g", "f"]]
    rows = [{"name": n, "branch": "docs", "doctype": "設計書"} for n in names]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)
    collected: list[str] = []
    offset = 0
    for _ in range(10):
        res, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "p", "limit": 3, "offset": offset}, "v1", None)
        collected.extend(d["rel_path"] for d in res["docs"])
        if not res["truncated"]:
            assert res["next_offset"] is None
            break
        offset = res["next_offset"]
    assert collected == sorted(names) and len(collected) == len(set(collected)) == len(names)


def test_list_docs_doctype_and_state_filters_are_case_insensitive_exact_match(monkeypatch):
    from sherpa import doc_ledger as DL

    rows = [{"name": "p/a.cbl", "branch": "source", "doctype": "cobol", "state": "ready"},
            {"name": "p/b.cbl", "branch": "source", "doctype": "cobol", "state": "unreadable"},
            {"name": "p/c.md", "branch": "docs", "doctype": "設計書", "state": "ready"}]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)

    def _rels(args):
        return {d["rel_path"] for d in TD.run_tool("list_docs", args, "v1", None)[0]["docs"]}

    assert _rels({"doctype": "COBOL"}) == {"p/a.cbl", "p/b.cbl"}
    assert _rels({"state": "READY"}) == {"p/a.cbl", "p/c.md"}
    assert _rels({"doctype": "cobol", "state": "unreadable"}) == {"p/b.cbl"}
    res4, _, _, _ = TD.run_tool("list_docs", {"doctype": "excel"}, "v1", None)   # 部分一致にしない・該当なしは 0 件
    assert res4["docs"] == [] and res4["count"] == 0


def test_list_docs_truncated_and_next_offset_at_boundaries(monkeypatch):
    """truncated = offset + len(docs) < count・next_offset = offset + len(docs)（truncated のときだけ）。"""
    from sherpa import doc_ledger as DL

    rows = [{"name": f"p/{i:02d}.md", "branch": "docs", "doctype": "設計書"} for i in range(5)]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)
    res, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "p", "limit": 5}, "v1", None)
    assert res["count"] == 5 and res["truncated"] is False and res["next_offset"] is None
    res2, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "p", "limit": 4}, "v1", None)
    assert len(res2["docs"]) == 4 and res2["truncated"] is True and res2["next_offset"] == 4
    res3, _, _, _ = TD.run_tool("list_docs", {"path_prefix": "p", "limit": 5, "offset": res2["next_offset"]}, "v1", None)
    assert len(res3["docs"]) == 1 and res3["truncated"] is False and res3["next_offset"] is None


# ===== es_search の採用条件・位置ヒント =====

def _stub_es(monkeypatch, hits, rel_set):
    from sherpa import documents
    monkeypatch.setattr(A.es_index, "search", lambda world, q, scope_paths=None, k=20, layer=None, **kw: (hits, None))
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: rel_set)


def test_es_search_filters_stale_and_sensitive_named_hits(monkeypatch):
    """現 world に実在しない古いヒットと、秘匿名（credentials.xlsx）の旧索引ヒットは本文付きで返さず docs からも外す。"""
    _stub_es(monkeypatch, [
        {"doc_id": "real.md", "line": 1, "text": "x", "span": [1, 1], "ext": ".md"},
        {"doc_id": "stale.md", "line": 2, "text": "y", "span": [2, 2], "ext": ".md"},
        {"doc_id": "credentials.xlsx", "line": 2, "text": "secret", "span": [2, 2], "ext": ".xlsx"}],
        {"real.md", "credentials.xlsx"})
    res, docs, cites, _ = TD.run_tool("es_search", {"query": "q"}, "v1", None)
    assert {h["doc_id"] for h in res["hits"]} == {"real.md"} and len(res["hits"]) == 1
    assert docs == {"real.md"} and all(c["doc_id"] == "real.md" for c in cites)


def test_es_search_tolerates_hit_without_line(monkeypatch):
    """rag_chunks 由来（line キー無し）でもクラッシュしない。chunk_id を持つため親返しで doc 単位へ束ねられる
    （rag.md が無いので tier は chunk）。"""
    _stub_es(monkeypatch, [{"doc_id": "a.docx", "text": "rag_chunks 由来", "ext": ".docx", "chunk_id": "rc1"}], {"a.docx"})
    res, docs, cites, _ = TD.run_tool("es_search", {"query": "q"}, "v1", None)
    assert res["hits"] == [{"doc_id": "a.docx", "tier": "chunk", "text": "rag_chunks 由来", "chunks": [{"chunk_id": "rc1"}]}]
    assert docs == {"a.docx"} and cites[0]["span"] == [None, None]


def test_es_search_locator_hint_goes_to_llm_text_only_and_absent_locator_is_unchanged(monkeypatch):
    """位置ヒントは hits[].text にだけ添え、citation の quote には足さない。locator 無しは従来どおり。"""
    _stub_es(monkeypatch, [
        {"doc_id": "b.xlsx", "line": None, "text": "単価100円", "ext": ".xlsx",
         "locator": {"sheet": "明細", "cell_range": "A2"}},
        {"doc_id": "a.md", "line": 3, "text": "本文", "ext": ".md"}], {"b.xlsx", "a.md"})
    res, docs, cites, _ = TD.run_tool("es_search", {"query": "q"}, "v1", None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["b.xlsx"] == {"doc_id": "b.xlsx", "line": None, "text": "単価100円（位置: シート「明細」A2）"}
    assert by_doc["a.md"] == {"doc_id": "a.md", "line": 3, "text": "本文"}
    quotes = {c["doc_id"]: c for c in cites}
    assert quotes["b.xlsx"]["quote"] == "単価100円" and "locator" not in quotes["a.md"]


def test_es_search_locator_hint_combined_text_is_not_clipped(monkeypatch):
    """LLM 向け本文は文字数で切らない（長い本文＋位置ヒントが丸ごと残り、quote だけ 500 字）。"""
    _stub_es(monkeypatch, [{"doc_id": "c.xlsx", "line": None, "text": "あ" * 1490, "ext": ".xlsx",
                            "locator": {"sheet": "明細", "cell_range": "B1"}}], {"c.xlsx"})
    res, _, cites, _ = TD.run_tool("es_search", {"query": "q"}, "v1", None)
    text = res["hits"][0]["text"]
    assert text.startswith("あ" * 1490) and "明細" in text and "text_truncated" not in res["hits"][0]
    assert len(cites[0]["quote"]) == 500


def test_es_search_locator_hint_secret_in_sheet_name_is_redacted(monkeypatch):
    """秘密様パターンを hint 側（sheet 名）に置く＝「結合してから redaction する」契約（結合前に足すと迂回する）の固定。"""
    _stub_es(monkeypatch, [{"doc_id": "b.xlsx", "line": None, "text": "単価100円", "ext": ".xlsx",
                            "locator": {"sheet": "password: hunter2", "cell_range": "A2"}}], {"b.xlsx"})
    res, _, cites, _ = TD.run_tool("es_search", {"query": "q"}, "v1", None)
    text = res["hits"][0]["text"]
    assert "hunter2" not in text and "[REDACTED]" in text and cites[0]["quote"] == "単価100円"


# ===== graph_neighbors =====

def _stub_neighbors(monkeypatch, fake):
    from sherpa import lens_service
    monkeypatch.setattr(lens_service, "neighbor_cards", lambda world, term, sp=None: list(fake))


def _skip_doc_verification(monkeypatch):
    monkeypatch.setattr(A, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)
    monkeypatch.setattr(RT, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)


def test_graph_neighbors_tool_returns_cards(monkeypatch):
    """lens_service.neighbor_cards をスタブし、カードと近傍ビューを返す（Neo4j 不要・架空 doc_id は検証を差し替え）。"""
    _skip_doc_verification(monkeypatch)
    fake = [{"name": "BILLINGJOB", "label": "Module", "category": "プログラム", "role": "実装",
             "distance": 2, "path": ["請求画面", "請求処理", "BILLINGJOB"],
             "evidence": {"edges": [], "grep": [{"doc_id": "4期/設計/請求.md", "line": 3}]}}]
    _stub_neighbors(monkeypatch, fake)
    res, docs, cites, cards = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert len(cards) == 1
    assert {k: v for k, v in cards[0].items() if k != "_verified_doc_ids"} == fake[0]
    assert cards[0]["_verified_doc_ids"] == ["4期/設計/請求.md"]
    assert res["neighbors"][0]["name"] == "BILLINGJOB" and res["neighbors"][0]["role"] == "実装"
    assert "4期/設計/請求.md" in docs


def test_graph_neighbors_tool_view_includes_directed_edges(monkeypatch):
    """各近傍は evidence.edges を素通しする（doc が無い辺は doc キーを省く・edges 空でも落ちない）。"""
    _skip_doc_verification(monkeypatch)
    _stub_neighbors(monkeypatch, [
        {"name": "BILLINGJOB", "label": "Module", "category": "プログラム", "role": "実装",
         "distance": 1, "path": ["請求処理", "BILLINGJOB"],
         "evidence": {"edges": [
             {"type": "COPIES", "from": "請求処理", "to": "BILLINGJOB", "doc": "4期/src/請求処理.cbl"},
             {"type": "INVOKES", "from": "BILLINGJOB", "to": "SUBRTN1"}], "grep": []}},
        {"name": "no-edges", "label": "Module", "category": "プログラム", "role": "実装",
         "distance": 1, "path": [], "evidence": {"edges": [], "grep": []}}])
    res, _docs, _cites, _cards = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    by_name = {n["name"]: n for n in res["neighbors"]}
    assert by_name["BILLINGJOB"]["edges"] == [
        {"type": "COPIES", "from": "請求処理", "to": "BILLINGJOB", "doc": "4期/src/請求処理.cbl"},
        {"type": "INVOKES", "from": "BILLINGJOB", "to": "SUBRTN1"}]
    assert by_name["no-edges"]["edges"] == [] and by_name["BILLINGJOB"]["path"] == ["請求処理", "BILLINGJOB"]


def test_run_tool_graph_neighbors_filters_invalid_cards_but_keeps_valid_ones(monkeypatch):
    """カード単位で裏付け doc の実在を検証し、主張したのに 1 件も実在しないカードは cards/view から外す。
    裏付け doc を主張しないカードは検証対象外＝通す。"""
    real_doc = "4期/04_運用/障害記録.md"
    _stub_neighbors(monkeypatch, [
        {"name": "valid", "label": "有効", "category": "プログラム", "role": "実装", "distance": 1,
         "path": [], "evidence": {"edges": [], "grep": [{"doc_id": real_doc}]}},
        {"name": "invalid", "label": "無効", "category": "プログラム", "role": "実装", "distance": 1,
         "path": [], "evidence": {"edges": [], "grep": [{"doc_id": "ghost-does-not-exist.md"}]}},
        {"name": "no-claim", "label": "主張無し", "category": "プログラム", "role": "実装", "distance": 1,
         "path": [], "evidence": {"edges": [], "grep": []}}])
    res, docs, _cites, cards = TD.run_tool("graph_neighbors", {"name": "x"}, "v1", None)
    assert {c["name"] for c in cards} == {"valid", "no-claim"} and docs == {real_doc}
    assert {n["name"] for n in res["neighbors"]} == {"valid", "no-claim"}


def test_graph_neighbors_cards_sidecar_clipped_to_graph_cards_max(monkeypatch):
    """cards（troubleshoot サイドカー）も LLM 向け view と同じ _GRAPH_CARDS_MAX 件に切り詰める。
    上限内なら truncated/count を付けない。"""
    def _fake(n):
        return [{"name": f"c{i}", "role": "実装", "category": "プログラム", "distance": 1, "path": [], "evidence": {}}
                for i in range(n)]

    _stub_neighbors(monkeypatch, _fake(1000))
    res, _docs, _cites, cards = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert len(cards) == RT._GRAPH_CARDS_MAX == 30 and len(res["neighbors"]) == 30
    assert res["truncated"] is True and res["count"] == 1000
    _stub_neighbors(monkeypatch, _fake(3))
    res2, *_ = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert "truncated" not in res2 and "count" not in res2


def test_graph_neighbors_count_is_fixed_and_grep_max_hits_env_is_ignored():
    """grep/es の MAX_HITS が何であっても graph_neighbors のカード件数上限は 30 のまま（実プロセスで観測）。"""
    script = (
        "import json, os\n"
        "os.environ.setdefault('SHERPA_USE_FIXTURES', '1')\n"
        "import sherpa.lens_service as lens_service\n"
        "fake = [{'name': f'c{i}', 'role': 'x', 'category': 'x', 'distance': 1,\n"
        "         'path': [], 'evidence': {}} for i in range(1000)]\n"
        "lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)\n"
        "import sherpa.agentic_search as A\n"
        "import sherpa.parts.read.tools as RT\n"
        "import sherpa.tool_dispatch as TD\n"
        "res, docs, cites, cards = TD.run_tool('graph_neighbors', {'name': 'x'}, 'v1', None)\n"
        "print(json.dumps({'max_hits': A.MAX_HITS, 'graph_cards_max': RT._GRAPH_CARDS_MAX,\n"
        "                   'n_cards': len(cards), 'n_view': len(res['neighbors'])}))\n")
    out = json.loads(FI.run_script(script, env={"SHERPA_GREP_MAX_HITS": "1"}))
    assert out == {"max_hits": 45, "graph_cards_max": 30, "n_cards": 30, "n_view": 30}


# ===== read_around の封じ込め・伏せ字・未登録拡張子の到達性 =====

def test_read_around_confinement_and_redaction():
    for bad in ["../../../etc/passwd", "/etc/passwd", "4期/../../../etc/hosts",
                "4期/03_開発/01_ソース/secret.env", "4期/00_共通/メモ.key"]:
        r, _, _, _ = TD.run_tool("read_around", {"doc_id": bad, "line": 1}, "v1", None)
        assert "error" in r, bad
    assert "[REDACTED]" in A._redact("config: api_key=sk-ABCDEFGHIJKLMNOP1234 done")
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in A._redact("token sk-ABCDEFGHIJKLMNOPQRSTUVWX")


def _isolate_world_kb(monkeypatch, tmp_path, world: str, files: dict) -> None:
    """`sherpa.worlds.world_dir` を tmp_path 配下の KB へ隔離する（DB 不要・実登録 world と非干渉）。"""
    kb = tmp_path / "kb"
    wd = kb / world
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    monkeypatch.setenv("SHERPA_KB_DIR", str(kb))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    for env in ("SHERPA_MCP_WORLD", "SHERPA_MCP_WORLD_ROOT"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)


def test_unregistered_ext_reachable_grep_read_and_es_eligible(monkeypatch, tmp_path):
    """未登録拡張子のプレーンテキスト（.zzz）は grep・read_around から到達でき、台帳（ES 索引の材料）にも
    doctype 付きで載る——可否は拡張子の許可リストでなく内容判定で決まる。"""
    world = "unreg-ext-reach"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"notes/app.zzz": "NEEDLE_ZZZ ここに業務ルールを書く\nline two\n"})
    res, docs, cites, _ = TD.run_tool("ripgrep_search", {"query": "NEEDLE_ZZZ"}, world, None)
    assert res["hits"], res
    hit = res["hits"][0]
    assert hit["doc_id"] == "notes/app.zzz" and "notes/app.zzz" in docs
    assert cites and cites[0]["doc_id"] == "notes/app.zzz"
    r2, d2, _, _ = TD.run_tool("read_around", {"doc_id": "notes/app.zzz", "line": hit["line"]}, world, None)
    assert "error" not in r2 and "NEEDLE_ZZZ" in r2["text"] and "notes/app.zzz" in d2
    from sherpa import corpus_docs
    entry = next(d for d in corpus_docs.world_documents(world) if d["name"] == "notes/app.zzz")
    assert entry.get("state") != "unreadable" and entry.get("doctype") is not None


def test_binary_unregistered_ext_unreachable_everywhere(monkeypatch, tmp_path):
    world = "unreg-ext-binary"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"blob.bin": b"\x00\x01\x02BINARYDATA\xff\xfe\x00" * 20})
    res, docs, _, _ = TD.run_tool("ripgrep_search", {"query": "BINARYDATA"}, world, None)
    assert not res.get("hits") and not docs
    r2, d2, _, _ = TD.run_tool("read_around", {"doc_id": "blob.bin", "line": 1}, world, None)
    assert "error" in r2 and not d2
    from sherpa import corpus_docs
    assert not any(d["name"] == "blob.bin" for d in corpus_docs.world_documents(world))


def test_sensitive_dotenv_unreachable_and_not_logged(monkeypatch, tmp_path, caplog):
    """`.env`（秘匿名）は grep・read_around のどちらからも到達できず、ログにも値が出ない（安全は秘匿判定が担う）。"""
    world = "unreg-ext-dotenv"
    secret_value = "SUPER_SECRET_TOKEN_VALUE_XYZ"
    _isolate_world_kb(monkeypatch, tmp_path, world, {".env": f"API_KEY={secret_value}\n"})
    with caplog.at_level("WARNING"):
        res, docs, _, _ = TD.run_tool("ripgrep_search", {"query": "SUPER_SECRET"}, world, None)
        assert not res.get("hits") and not docs
        r2, d2, _, _ = TD.run_tool("read_around", {"doc_id": ".env", "line": 1}, world, None)
        assert "error" in r2 and not d2
    assert secret_value not in caplog.text


def test_registered_extension_regression_still_reachable(monkeypatch, tmp_path):
    world = "unreg-ext-regress-cbl"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"PROG.cbl": "       PROGRAM-ID. PROG.\n       NEEDLE_CBL line.\n"})
    res, docs, _, _ = TD.run_tool("ripgrep_search", {"query": "NEEDLE_CBL"}, world, None)
    assert res["hits"], res
    r2, d2, _, _ = TD.run_tool("read_around", {"doc_id": "PROG.cbl", "line": res["hits"][0]["line"]}, world, None)
    assert "error" not in r2 and "PROG.cbl" in d2


def test_read_around_clips_output_for_huge_single_line_doc(monkeypatch, tmp_path):
    """単一行が巨大（200 万文字）でも返却テキストは TOOL_RESULT_MAX_BYTES に収まり、全体を一括ロードしない。"""
    world = "hugeline-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "A" * 2_000_000})
    res, docs, _, _ = TD.run_tool("read_around", {"doc_id": "big.md", "line": 1, "window": 5}, world, None)
    assert "error" not in res and len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES
    assert "big.md" in docs


def test_read_around_normal_document_unaffected_by_cap(monkeypatch, tmp_path):
    world = "normal-world"
    content = "\n".join(f"line {i}: TAX-RATE" if i == 10 else f"line {i}" for i in range(1, 21))
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, docs, _, _ = TD.run_tool("read_around", {"doc_id": "doc.md", "line": 10, "window": 2}, world, None)
    assert "error" not in res and "doc.md" in docs
    assert res["text"] == "\n".join(f"{i}: line {i}: TAX-RATE" if i == 10 else f"{i}: line {i}" for i in range(8, 13))


def test_clip_utf8_bytes_does_not_break_multibyte_boundary():
    clipped = A._clip_utf8_bytes("あ" * 100, 10)
    assert len(clipped.encode("utf-8")) <= 10


# ===== ツール結果のバイト予算（管理者設定 > コード既定・窓連動は撤去済み） =====

def test_tool_result_max_bytes_code_default(monkeypatch):
    assert A.TOOL_RESULT_MAX_BYTES == 262144
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    assert A.effective_tool_result_max_bytes() == A.TOOL_RESULT_MAX_BYTES


@pytest.mark.parametrize("stored, expected", [
    ({}, 99000),                                         # 未設定はモジュール定数
    ({"agentic_budget_per_result": 5000}, 5000),         # 管理者設定が勝つ
    ({"agentic_budget_per_result": 0}, 99000),           # 範囲外（1024〜8MiB 外）・不正な保存値は fail-safe でコード既定へ
    ({"agentic_budget_per_result": -1}, 99000),
    ({"agentic_budget_per_result": 8 * 1024 * 1024 + 1}, 99000),
    ({"agentic_budget_per_result": "not-an-int"}, 99000),
    (RuntimeError("db down"), 99000),                    # 設定読取失敗でも落ちない
])
def test_effective_tool_result_max_bytes_resolution(monkeypatch, stored, expected):
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 99000)

    def _settings(**kw):
        if isinstance(stored, Exception):
            raise stored
        return stored

    monkeypatch.setattr(store, "get_system_settings", _settings)
    assert A.effective_tool_result_max_bytes() == expected


def test_effective_tool_result_max_bytes_does_not_shrink_large_admin_setting():
    """AI が持つ文脈窓を Sherpa が制限しない。大きい管理者設定も縮めない。"""
    assert A.effective_tool_result_max_bytes({}) == A.TOOL_RESULT_MAX_BYTES
    assert A.effective_tool_result_max_bytes({"agentic_budget_per_result": 5_000_000}) == 5_000_000


def test_run_tool_explicit_tool_result_max_bytes_overrides_module_default(monkeypatch, tmp_path):
    world = "read-doc-explicit-budget-world"
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 10_000_000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 10_000_000)
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "x" * 5000})
    res, _, _, _ = TD.run_tool("read_doc", {"doc_id": "big.md"}, world, None, tool_result_max_bytes=200)
    assert "error" not in res and res.get("text_truncated") is True and len(res["text"].encode("utf-8")) <= 200


def test_clip_cards_limits_count_and_bytes_and_rejects_oversized_single_card():
    assert len(RT._clip_cards([{"name": f"c{i}", "evidence": {}} for i in range(1000)],
                             max_count=30, max_bytes=10_000_000)) == 30
    cards = [{"name": "x" * 1000, "evidence": {}} for _ in range(100)]
    assert 1 <= len(RT._clip_cards(cards, max_count=100, max_bytes=2000)) < 100    # バイト予算が件数上限より先に効く
    oversized = {"name": "x" * 10000, "evidence": {}}
    # 先頭カードも例外なくバイト上限で判定する（単体で超えるカードは 1 件も採用しない＝fail-closed）。
    assert RT._clip_cards([oversized, dict(oversized)], max_count=30, max_bytes=100) == []


# ===== read_around の symlink 封じ込め（open 時の TOCTOU・既存 symlink による scope/拡張子迂回） =====

@pytest.mark.parametrize("case", ["leaf_symlink", "ancestor_symlink"])
def test_read_around_rejects_symlink_swapped_after_check(monkeypatch, tmp_path, case):
    """検査済み path が open 直前に symlink だった状態を固定する（競合そのものは単体で再現できない）。
    最終要素（O_NOFOLLOW）も中間ディレクトリ（dir_fd walk）も world root 外への symlink は追跡せず fail-closed。"""
    world = f"toctou-{case}".replace("_", "-")
    _isolate_world_kb(monkeypatch, tmp_path, world, {"placeholder.md": "x"})
    kb_world = tmp_path / "kb" / world
    if case == "leaf_symlink":
        secret = tmp_path / "outside_secret.txt"
        secret.write_text("SECRET OUTSIDE WORLD ROOT", encoding="utf-8")
        rel = "swapped_doc.md"
        (kb_world / rel).symlink_to(secret)
    else:
        secret_dir = tmp_path / "outside_secret_dir"
        secret_dir.mkdir()
        (secret_dir / "doc.md").write_text("SECRET OUTSIDE WORLD ROOT", encoding="utf-8")
        (kb_world / "sub").symlink_to(secret_dir)
        rel = "sub/doc.md"
    swapped = kb_world / rel
    monkeypatch.setattr(RT, "_safe_doc_path", lambda w, doc_id, layer=None: (kb_world, rel, swapped))
    res, _docs, _, _ = TD.run_tool("read_around", {"doc_id": rel, "line": 1, "window": 5}, world, None)
    assert "error" in res and "SECRET" not in str(res)


def test_read_around_rejects_existing_symlink_scope_and_extension_bypass(monkeypatch, tmp_path):
    """scope 内に見える doc_id が scope 外への既存 symlink、許可拡張子（.md）を装った実体が禁止種別（.json）
    への既存 symlink でも、symlink 自体を O_NOFOLLOW で拒否し本文を返さない。"""
    world = "fixn-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"private/secret.md": "TOP SECRET CONTENT",
                                                      "secret.json": '{"leaked": true}'})
    kb_world = tmp_path / "kb" / world
    (kb_world / "public").mkdir(parents=True, exist_ok=True)
    (kb_world / "public" / "link.md").symlink_to(kb_world / "private" / "secret.md")
    (kb_world / "x.md").symlink_to(kb_world / "secret.json")
    res, _docs, _, _ = TD.run_tool("read_around", {"doc_id": "public/link.md", "line": 1, "window": 5}, world, ["public"])
    assert "error" in res and "TOP SECRET" not in str(res)
    res, _docs, _, _ = TD.run_tool("read_around", {"doc_id": "x.md", "line": 1, "window": 5}, world, None)
    assert "error" in res and "leaked" not in str(res)


def test_read_around_normal_files_readable_through_nofollow_walk(monkeypatch, tmp_path):
    """symlink を介さない通常ファイル（直下・ネスト・scope 内）は dir_fd walk 経由でも従来どおり読める。"""
    world = "normal-read-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "plain.md": "1行目\n2行目\nTAX-RATE 3行目\n4行目\n5行目\n",
        "a/b/nested.md": "1行目\nTAX-RATE 2行目\n3行目\n",
        "public/normal.md": "1行目\nTAX-RATE 2行目\n3行目\n"})
    for doc_id, line, scope in (("plain.md", 3, None), ("a/b/nested.md", 2, None), ("public/normal.md", 2, ["public"])):
        res, _docs, _, _ = TD.run_tool("read_around", {"doc_id": doc_id, "line": line, "window": 1}, world, scope)
        assert "error" not in res and "TAX-RATE" in res["text"], doc_id


def test_read_around_office_document_regression_after_fix_n(monkeypatch, tmp_path):
    """Office 派生 MD（doc_id + ".md"）も symlink を介さない通常配置なら lexical walk で読める。"""
    world = "fixn-office-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {})
    derived = tmp_path / "derived"
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(derived))
    md_dir = derived / world / "md"
    md_dir.mkdir(parents=True)
    (md_dir / "report.docx.md").write_text("1行目\nTAX-RATE 2行目\n3行目\n", encoding="utf-8")
    res, _docs, _, _ = TD.run_tool("read_around", {"doc_id": "report.docx", "line": 2, "window": 1}, world, None)
    assert "error" not in res and "TAX-RATE" in res["text"]


def _make_real_tree(tmp_path):
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "file.txt").write_text("REAL CONTENT", encoding="utf-8")
    return real


def test_open_file_nofollow_walk_reads_through_real_anchor(tmp_path):
    fd = RT._open_file_nofollow_walk(_make_real_tree(tmp_path), ("sub", "file.txt"))
    with os.fdopen(fd, "rb") as f:
        assert f.read() == b"REAL CONTENT"


def test_open_file_nofollow_walk_rejects_anchor_symlink_and_dotdot_anchor(tmp_path):
    """anchor 自身が symlink なら拒否。`..` を含む生 anchor は lexical 正規化の食い違いを避けるため一律拒否。"""
    real = _make_real_tree(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        RT._open_file_nofollow_walk(link, ("sub", "file.txt"))
    with pytest.raises(OSError):
        RT._open_file_nofollow_walk(real / "sub" / "..", ("sub", "file.txt"))


def test_open_file_nofollow_walk_rejects_ancestor_of_anchor_symlink(tmp_path):
    """anchor 自身は symlink でなくても、祖先（base）が保護対象外への symlink に差し替えられていれば拒否する
    （単発 O_NOFOLLOW は最終要素にしか効かない）。symlink 先にも同じ相対構造を用意して旧実装なら成功する構造にする。"""
    base = tmp_path / "base"
    (base / "world_root" / "sub").mkdir(parents=True)
    (base / "world_root" / "sub" / "file.txt").write_text("REAL CONTENT", encoding="utf-8")
    anchor = base / "world_root"
    outside_secret = tmp_path / "outside_secret"
    (outside_secret / "world_root" / "sub").mkdir(parents=True)
    (outside_secret / "world_root" / "sub" / "file.txt").write_text("SECRET OUTSIDE", encoding="utf-8")
    base.rename(tmp_path / "base_moved")
    base.symlink_to(outside_secret)
    os.close(os.open(str(anchor / "sub" / "file.txt"), os.O_RDONLY))   # 単発 open なら成功する構造の自己検証
    with pytest.raises(OSError):
        RT._open_file_nofollow_walk(anchor, ("sub", "file.txt"))


# ===== env 整数・コード既定 =====

# `SHERPA_GREP_MAX_HITS`／`SHERPA_READ_WINDOW` は実行時に読まない（画面だけが正）。import 時の値は実プロセスで観測する。

def test_max_hits_read_window_are_code_defaults_and_ignore_env():
    script = ("import json\nimport sherpa.agentic_search as m\n"
              "print(json.dumps({'max_hits': m.MAX_HITS, 'read_window': m.READ_WINDOW}))\n")
    for env in ({"SHERPA_GREP_MAX_HITS": None, "SHERPA_READ_WINDOW": None},
                {"SHERPA_GREP_MAX_HITS": "100", "SHERPA_READ_WINDOW": "80"}):
        assert json.loads(FI.run_script(script, env=env)) == {"max_hits": 45, "read_window": 60}


def _read_around_run_tool_script(doc_lines: int, center_line: int, window_arg: int | None = None) -> str:
    """read_around のツール説明と run_tool の実挙動（返却行数）を、同じ式を再計算せず run_tool 越しに観測する
    スクリプト（別プロセスのため world 隔離はスクリプト内で行う）。"""
    args_literal = f"'doc_id': 'big.md', 'line': {center_line}"
    if window_arg is not None:
        args_literal += f", 'window': {window_arg}"
    return (
        "import json, os, tempfile\n"
        "tmp = tempfile.mkdtemp()\n"
        "wd = os.path.join(tmp, 'kb', 'freshworld')\n"
        "os.makedirs(wd, exist_ok=True)\n"
        f"content = chr(10).join(f'line {{i}}' for i in range(1, {doc_lines + 1}))\n"
        "with open(os.path.join(wd, 'big.md'), 'w', encoding='utf-8') as f:\n"
        "    f.write(content)\n"
        "os.environ['SHERPA_KB_DIR'] = os.path.join(tmp, 'kb')\n"
        "os.environ.pop('SHERPA_USE_FIXTURES', None)\n"
        "for _e in ('SHERPA_MCP_WORLD', 'SHERPA_MCP_WORLD_ROOT'):\n"
        "    os.environ.pop(_e, None)\n"
        "import sherpa.store as store\n"
        "store.get_world = lambda world_id: None\n"
        "import sherpa.agentic_search as A\n"
        "import sherpa.tool_dispatch as TD\n"
        "desc = A._PARAMS_READ['properties']['window']['description']\n"
        f"res, docs, _, _ = TD.run_tool('read_around', {{{args_literal}}}, 'freshworld', None)\n"
        "n_lines = len(res['text'].strip().split(chr(10)))\n"
        "print(json.dumps({'read_window': A.READ_WINDOW, 'desc': desc, 'n_lines': n_lines}))\n"
    )


def test_read_around_default_window_description_and_ceiling():
    """window 説明文は実 READ_WINDOW(60) を埋め込み、省略時の返却行数と一致する。明示 window は 200 で頭打ち。"""
    out = json.loads(FI.run_script(_read_around_run_tool_script(200, 100), env={"SHERPA_READ_WINDOW": "80"}))
    assert out["read_window"] == 60 and "60" in out["desc"] and out["n_lines"] == 2 * 60 + 1
    out = json.loads(FI.run_script(_read_around_run_tool_script(700, 350, window_arg=300), env={}))
    assert out["read_window"] == 60 and out["n_lines"] == 2 * 200 + 1


# ===== Codex MCP 設定（agents） =====

def test_codex_mcp_config_builder():
    from sherpa import agents
    from sherpa.providers.codex import mcp
    args = mcp._mcp_config_args("v1", ["4期", "00_共通"])
    joined = " ".join(args)
    assert args.count("-c") == 5
    assert "mcp_servers.sherpa.command=" in joined and 'sherpa.args = ["-m", "sherpa.mcp_server"]' in joined
    assert 'SHERPA_MCP_WORLD = "v1"' in joined and 'SHERPA_MCP_SCOPE = "4期\\n00_共通"' in joined
    assert 'default_tools_approval_mode = "approve"' in joined and 'approval_policy = "never"' in joined
    env = mcp._mcp_env("v1", None)
    assert env["SHERPA_MCP_WORLD"] == "v1" and "SHERPA_MCP_SCOPE" not in env
    assert "SHERPA_MCP_ASK_DISABLED" not in env
    p = agents.CodexProvider()._prompt_mcp("請求でエラー。原因は?", "troubleshoot", "v1")
    assert "graph_neighbors" in p and "参考（構造化済みの事実）" not in p    # MCP は自律＝事実を前渡ししない


def test_mcp_env_includes_effective_arms_and_legacy_backend_snapshot(monkeypatch):
    """MCP サブプロセスは PG creds を持たないため、親の実効値スナップショットを env で渡す。"""
    from sherpa.providers.codex import mcp
    monkeypatch.setattr(store, "get_system_settings", lambda: {"arms_enabled": ["ooxml"], "legacy_backend": "libreoffice"})
    env = mcp._mcp_env("v1", None)
    assert env["SHERPA_MCP_ARMS"] == "ooxml" and env["SHERPA_MCP_LEGACY_BACKEND"] == "libreoffice"
    assert "SHERPA_TESSERACT_BIN" not in env


def test_mcp_env_includes_vlm_usable_snapshot(monkeypatch):
    """親の resolve_vlm() 実効可用性（1bit・secrets を含まない）を渡す。既定（ローカル ollama）は "1"、
    openai・cloud_allowed=false は "0"。"""
    from sherpa.providers.codex import mcp
    assert mcp._mcp_env("v1", None)["SHERPA_VLM_USABLE"] == "1"
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"vlm": {"provider": "openai", "model": "gpt-4o", "cloud_allowed": False}})
    assert mcp._mcp_env("v1", None)["SHERPA_VLM_USABLE"] == "0"


def test_mcp_env_snapshots_legacy_exts_and_omits_office_com_secrets(monkeypatch):
    """office_com の URL/TOKEN は MCP サブプロセスへ渡さない。親の実効 legacy_exts() スナップショットを渡す。"""
    from sherpa.providers.codex import mcp
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:8091")
    monkeypatch.setenv("SHERPA_OFFICE_COM_TOKEN", "super-secret-token")
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", "/usr/bin/soffice")
    monkeypatch.setattr(legacy_convert, "legacy_exts", lambda: {".doc", ".xls"})
    env = mcp._mcp_env("v1", None)
    for k in ("SHERPA_OFFICE_COM_URL", "SHERPA_OFFICE_COM_TOKEN", "SHERPA_SOFFICE_BIN"):
        assert k not in env
    assert env["SHERPA_LEGACY_EXTS"] == ".doc,.xls"


def test_mcp_ask_disabled_and_layer_reach_subprocess_env_and_config_args():
    """ask_disabled・layer(docs/code) は sandbox 経路（_mcp_env）と fallback 経路（_mcp_config_args の -c）の両方に乗る。
    both/未指定は付けない。不正な layer は Codex 起動前に ValueError。"""
    from sherpa.providers.codex import mcp
    assert mcp._mcp_env("v1", None, ask_disabled=True)["SHERPA_MCP_ASK_DISABLED"] == "1"
    assert 'SHERPA_MCP_ASK_DISABLED = "1"' in " ".join(mcp._mcp_config_args("v1", None, ask_disabled=True))
    assert "SHERPA_MCP_LAYER" not in mcp._mcp_env("v1", None, layer="both")
    assert "SHERPA_MCP_LAYER" not in mcp._mcp_env("v1", None)
    assert mcp._mcp_env("v1", None, layer="code")["SHERPA_MCP_LAYER"] == "code"
    assert mcp._mcp_env("v1", None, layer="docs")["SHERPA_MCP_LAYER"] == "docs"
    assert 'SHERPA_MCP_LAYER = "docs"' in " ".join(mcp._mcp_config_args("v1", None, layer="docs"))
    with pytest.raises(ValueError):
        mcp._mcp_env("v1", None, layer="bogus")
    with pytest.raises(ValueError):
        mcp._mcp_config_args("v1", None, layer="bogus")


def test_mcp_neighbors_from_stream_item():
    from sherpa.providers.codex import mcp
    good = {"result": {"content": [{"type": "text",
            "text": json.dumps({"neighbors": [{"name": "BILLINGJOB", "role": "実装", "path": ["a", "b"]}]})}]}}
    ns = mcp._mcp_neighbors_from(good)
    assert ns and ns[0]["name"] == "BILLINGJOB" and ns[0]["role"] == "実装"
    assert mcp._mcp_neighbors_from({"result": {"content": [{"text": "{ broken"}]}}) == []
    assert mcp._mcp_neighbors_from({"result": None}) == []
    assert mcp._mcp_neighbors_from({}) == []


# ===== _safe_doc_path: 派生 MD の解決 =====

def test_pdf_and_legacy_doc_id_resolve_to_derived_md(monkeypatch, tmp_path):
    """PDF・旧形式（.doc/.xls/.ppt）の doc_id は原本 rel + ".md" の派生 MD に解決する（grep とのヒット/精読の非対称を作らない）。"""
    der = tmp_path / "md"
    (der / "設計").mkdir(parents=True)
    (der / "設計" / "資料.pdf.md").write_text("## ページ 1\n\n税率10%", encoding="utf-8")
    (der / "旧資料.doc.md").write_text("旧資料の中身テキストXYZ", encoding="utf-8")
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: der)
    for ext in (".pdf", ".doc", ".xls", ".ppt"):
        assert ext in RT._OFFICE_MD and ext in RT._READABLE_EXT
    root, lexical_rel, p = RT._safe_doc_path("w", "設計/資料.pdf")
    assert root == der and lexical_rel == "設計/資料.pdf.md" and p.name == "資料.pdf.md"
    assert RT._safe_doc_path("w", "設計/欠落.pdf") is None
    root, lexical_rel, p = RT._safe_doc_path("w", "旧資料.doc")
    assert root == der and lexical_rel == "旧資料.doc.md" and p.read_text(encoding="utf-8") == "旧資料の中身テキストXYZ"
    assert RT._safe_doc_path("w", "欠落.xls") is None


# ===== _safe_doc_path: 派生 MD の優先・秘匿名・classify_document への一本化 =====

def test_safe_doc_path_rejects_importance_control_file(monkeypatch, tmp_path):
    """`_重要度.txt` は拡張子（.txt）が読取可でも、除外の単一判定関数を通して精読できない。"""
    (tmp_path / "_重要度.txt").write_text("*.md: 高\n", encoding="utf-8")
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: tmp_path)
    assert RT._safe_doc_path("w", "_重要度.txt") is None


def test_safe_doc_path_derived_md_resolution(monkeypatch, tmp_path):
    """grep_search と同じ preferred_derived_name を使う: rag.md が実在すればそちら（rag/md は別ディレクトリ）・
    無ければ legacy・どちらも無ければ None。Office/画像は classify_document を経由しないため、秘匿名の原本は派生 MD が
    実在しても開けない（非秘匿は開ける）。"""
    der_md, der_rag = tmp_path / "md", tmp_path / "rag"
    der_md.mkdir()
    der_rag.mkdir()
    (der_md / "report.docx.md").write_text("legacy", encoding="utf-8")
    (der_rag / "report.docx.rag.md").write_text("rag", encoding="utf-8")
    for name in ("onlylegacy.xlsx.md", "credentials.xlsx.md", "id_rsa.docx.md", "normal.docx.md"):
        (der_md / name).write_text("SECRET" if name.startswith(("cred", "id_")) else "legacy", encoding="utf-8")
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: der_md)
    monkeypatch.setattr(worlds_mod, "derived_rag_dir", lambda w: der_rag)

    root, lexical_rel, p = RT._safe_doc_path("w", "report.docx")
    assert root == der_rag and lexical_rel == "report.docx.rag.md" and p.read_text(encoding="utf-8") == "rag"
    _, lexical_rel, p = RT._safe_doc_path("w", "onlylegacy.xlsx")
    assert lexical_rel == "onlylegacy.xlsx.md" and p.name == "onlylegacy.xlsx.md"
    assert RT._safe_doc_path("w", "normal.docx")[2].name == "normal.docx.md"
    for rejected in ("credentials.xlsx", "id_rsa.docx", "missing.docx"):
        assert RT._safe_doc_path("w", rejected) is None


def _recording_analyzer(monkeypatch, accepts_result=True, calls=None):
    """.cbl を担当する登録アナライザに差し替える（accepts の呼び出しを calls に記録）。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    class _Stub(Analyzer):
        name = "recording"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            if calls is not None:
                calls.append(rel_path)
            return accepts_result

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_Stub(),))


def test_safe_doc_path_rejects_declined_registered_code_extension_and_keeps_accepted(monkeypatch, tmp_path):
    """登録拡張子でも accepts() が全滅（未対応）なら read_around も拒否する（classify_document に一本化）。
    accepts する登録拡張子は従来どおり読める。"""
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: tmp_path)
    (tmp_path / "PROG.cbl").write_text("       PROGRAM-ID. PROG.\n", encoding="utf-8")
    _recording_analyzer(monkeypatch, accepts_result=False)
    assert RT._safe_doc_path("w", "PROG.cbl") is None
    _recording_analyzer(monkeypatch, accepts_result=True)
    root, lexical_rel, p = RT._safe_doc_path("w", "PROG.cbl")
    assert root == tmp_path and lexical_rel == "PROG.cbl" and p.name == "PROG.cbl"


@pytest.mark.parametrize("case", ["out_of_root_symlink", "in_root_symlink", "ancestor_symlink", "fifo"])
def test_safe_doc_path_rejects_unsafe_entry_before_calling_accepts(monkeypatch, tmp_path, case):
    """封じ込め（root 配下・symlink・regular file）は classify_document（accepts の内容読取）より先に行う
    ——範囲外 symlink・root 内を指す symlink・祖先ディレクトリ symlink・FIFO の内容を検証前に読まない。"""
    accepts_calls: list = []
    _recording_analyzer(monkeypatch, calls=accepts_calls)
    world_root = tmp_path / "world"
    world_root.mkdir()
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: world_root)
    if case == "out_of_root_symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.cbl").write_text("SECRET CONTENT", encoding="utf-8")
        (world_root / "link.cbl").symlink_to(outside / "secret.cbl")
        rel = "link.cbl"
    elif case == "in_root_symlink":
        (world_root / "real.cbl").write_text("       PROGRAM-ID. REAL.\n", encoding="utf-8")
        (world_root / "link.cbl").symlink_to(world_root / "real.cbl")
        rel = "link.cbl"
    elif case == "ancestor_symlink":
        real_sub = tmp_path / "real_sub"
        real_sub.mkdir()
        (real_sub / "file.cbl").write_text("       PROGRAM-ID. FILE.\n", encoding="utf-8")
        (world_root / "sub").symlink_to(real_sub)
        rel = "sub/file.cbl"
    else:
        os.mkfifo(world_root / "pipe.cbl")
        rel = "pipe.cbl"
    assert RT._safe_doc_path("w", rel) is None
    assert accepts_calls == []
    if case == "in_root_symlink":
        assert RT._safe_doc_path("w", "real.cbl") is not None      # 対照: symlink を介さない実体は読める


def test_grep_and_read_around_resolve_target_once_and_agree_on_rag_priority(monkeypatch, tmp_path):
    """grep がヒットを作ったファイルと read_around が開くファイルは一致し（rag 優先）、対象名解決
    （preferred_derived_name）は read_around 全体で厳密に 1 回（二重解決の再発を呼び出し回数で検出）。"""
    world = "align-rag-world"
    world_root = tmp_path / "kb" / world
    world_root.mkdir(parents=True)
    der = tmp_path / "derived" / world / "md"
    der_rag = tmp_path / "derived" / world / "rag"
    der.mkdir(parents=True)
    der_rag.mkdir(parents=True)
    (der / "report.docx.md").write_text("legacy 本文 TAX-RATE 旧版\n", encoding="utf-8")
    (der_rag / "report.docx.rag.md").write_text("## 概要\nrag 本文 TAX-RATE 新版\n", encoding="utf-8")
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: world_root)
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds_mod, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(worlds_mod, "observation_current_dir", lambda w: None)

    res, _, _, _ = TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, world, None)
    assert len(res["hits"]) == 1 and res["hits"][0]["doc_id"] == "report.docx"
    hit = res["hits"][0]

    calls = []
    orig = grep_tool_mod.preferred_derived_name

    def spy(root, rel):
        calls.append((root, rel))
        return orig(root, rel)

    monkeypatch.setattr(grep_tool_mod, "preferred_derived_name", spy)
    r2, docs2, _, _ = TD.run_tool("read_around", {"doc_id": hit["doc_id"], "line": hit["line"], "window": 2}, world, None)
    assert "error" not in r2 and "rag 本文" in r2["text"] and "legacy 本文" not in r2["text"]
    assert "report.docx" in docs2
    assert len(calls) == 1, calls


# ===== ツール可用性（実接続判定・TTL キャッシュ・single-flight） =====

def test_graph_available_real_connectivity_check_unreachable_uri(monkeypatch):
    """_graph_available は URI の有無でなく実接続を確認する（到達不可な URI では False）。"""
    from sherpa.ingest import world_neo4j
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://127.0.0.1:1", "user": "neo4j", "pw": "x"})
    assert A._graph_available() is False


def test_tool_availability_grep_always_true(monkeypatch):
    monkeypatch.setattr(A.es_index, "available", lambda: False)
    monkeypatch.setattr(A, "_graph_available", lambda: False)
    assert A.tool_availability() == {"grep": True, "fulltext": False, "graph": False}


def _counting_probe(monkeypatch, es_result=True, graph_result=True):
    """es_index.available / _graph_available の呼び出し回数を数える偽物に差し替える。"""
    calls = {"es": 0, "graph": 0}

    def _es():
        calls["es"] += 1
        return es_result

    def _graph():
        calls["graph"] += 1
        return graph_result

    monkeypatch.setattr(A.es_index, "available", _es)
    monkeypatch.setattr(A, "_graph_available", _graph)
    return calls


def test_tool_availability_dedupes_within_ttl_and_force_bypasses_cache(monkeypatch):
    calls = _counting_probe(monkeypatch)
    assert A.tool_availability() == A.tool_availability() == A.tool_availability() == {
        "grep": True, "fulltext": True, "graph": True}
    assert calls == {"es": 1, "graph": 1}
    A.tool_availability(force=True)
    A.tool_availability(force=True)
    assert calls == {"es": 3, "graph": 3}


def test_tool_availability_ttl_expiry_triggers_recheck(monkeypatch):
    calls = _counting_probe(monkeypatch, es_result=False, graph_result=False)
    assert A.tool_availability() == {"grep": True, "fulltext": False, "graph": False}
    assert calls == {"es": 1, "graph": 1}
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    assert A.tool_availability() == {"grep": True, "fulltext": False, "graph": False}      # TTL 内はキャッシュのまま
    A._tools_availability_cache["at"] -= (A._TOOLS_AVAILABILITY_TTL + 1)                   # 経過をシミュレート
    assert A.tool_availability() == {"grep": True, "fulltext": True, "graph": True}


def test_tool_availability_records_at_after_probe_completes(monkeypatch):
    """鮮度の起点 at はプローブ完了後に記録する（開始前だと TTL ≦ プローブ所要の構成で single-flight が崩れる）。"""
    def _slow_es():
        time.sleep(0.05)
        return True

    monkeypatch.setattr(A.es_index, "available", _slow_es)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    before = time.monotonic()
    A.tool_availability()
    assert A._tools_availability_cache["at"] - before >= 0.04


@pytest.mark.parametrize("n_threads, ttl, probe_sleep", [
    (8, None, 0.05),
    (20, 0.0001, 0.02),      # probe 所要よりはるかに短い TTL でも、完成した世代を共有して 1 回に集約される
])
def test_tool_availability_single_flight_under_real_concurrency(monkeypatch, n_threads, ttl, probe_sleep):
    """Barrier で呼び出し開始を揃えた真の同時 miss でも、実接続チェックは 1 回に集約される。"""
    import threading

    if ttl is not None:
        monkeypatch.setattr(A, "_TOOLS_AVAILABILITY_TTL", ttl)
    calls = {"es": 0, "graph": 0}
    call_lock = threading.Lock()

    def _slow_es():
        with call_lock:
            calls["es"] += 1
        time.sleep(probe_sleep)
        return True

    def _graph():
        with call_lock:
            calls["graph"] += 1
        return True

    monkeypatch.setattr(A.es_index, "available", _slow_es)
    monkeypatch.setattr(A, "_graph_available", _graph)
    barrier = threading.Barrier(n_threads)
    results: list = [None] * n_threads

    def worker(i):
        barrier.wait()
        results[i] = A.tool_availability()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert all(r == {"grep": True, "fulltext": True, "graph": True} for r in results)
    assert calls == {"es": 1, "graph": 1}, calls


def test_unavailable_explicit_tools(monkeypatch):
    """明示 ON かつ不可用のツールだけを正順で返す（明示 OFF・未指定・grep は対象外）。
    availability を渡すと tool_availability() を再取得しない（受付時の判定と実行本体で食い違わない）。"""
    assert A.unavailable_explicit_tools(None) == []
    assert A.unavailable_explicit_tools({"graph": False}) == []
    assert A.unavailable_explicit_tools({}) == []
    monkeypatch.setattr(A, "tool_availability", lambda: {"grep": True, "fulltext": False, "graph": False})
    assert A.unavailable_explicit_tools({"fulltext": True, "graph": True}) == ["fulltext", "graph"]
    assert A.unavailable_explicit_tools({"grep": True}) == []
    calls = []
    monkeypatch.setattr(A, "tool_availability", lambda: (calls.append(1), {"grep": True})[1])
    snapshot = {"grep": True, "fulltext": True, "graph": False}
    assert A.unavailable_explicit_tools({"graph": True}, availability=snapshot) == ["graph"]
    assert calls == []


def test_dispatch_tools_for_lens():
    off_graph = {"grep": True, "fulltext": True, "graph": False}
    eff, blocked = A.dispatch_tools_for_lens("impact", None, availability=off_graph)
    assert blocked is True and eff == off_graph
    assert A.dispatch_tools_for_lens("troubleshoot", None, availability=off_graph)[1] is True
    assert A.dispatch_tools_for_lens("qa", {"grep": False, "fulltext": False, "graph": True}, availability=None)[1] is True
    assert A.dispatch_tools_for_lens("qa", {"fulltext": False}, availability=None)[1] is False    # grep が残る
    eff, blocked = A.dispatch_tools_for_lens("impact", None)           # availability 省略は全て利用可能扱い
    assert eff == {"grep": True, "fulltext": True, "graph": True} and blocked is False


# ===== glob_search（ファイル名/パスのグロブ検索・grep 軸に同居） =====

def test_glob_search_basename_pattern_matches_at_any_depth_case_insensitively():
    """スラッシュを含まないパターンは深さを問わずファイル名に一致する（ripgrep の --glob と同じ慣習・大文字小文字を区別しない）。"""
    res, docs, cites, cards = TD.run_tool("glob_search", {"pattern": "*.jcl"}, "v1", None)
    expected = CE.rel_paths_glob("*.jcl")
    assert expected
    assert res["count"] == len(expected) and set(res["paths"]) == expected and res["truncated"] is False
    assert docs == expected and cites == [] and cards == []
    upper, _, _, _ = TD.run_tool("glob_search", {"pattern": "*.JCL"}, "v1", None)
    assert set(upper["paths"]) == expected


def test_glob_search_slash_pattern_matches_hierarchical_segments():
    """スラッシュを含むパターンは world ルートからの絞り込み＝`**` だけが複数階層を跨ぐ。"""
    res, _, _, _ = TD.run_tool("glob_search", {"pattern": "4期/02_設計/**/*.md"}, "v1", None)
    expected = CE.rel_paths_glob("4期/02_設計/**/*.md")
    assert expected and set(res["paths"]) == expected


def test_glob_search_zero_hits_and_session_scope():
    res, docs, cites, cards = TD.run_tool("glob_search", {"pattern": "*.no-such-ext"}, "v1", None)
    assert res == {"count": 0, "paths": [], "truncated": False} and docs == set() and cites == [] and cards == []
    scoped, docs, _, _ = TD.run_tool("glob_search", {"pattern": "*.md"}, "v1", ["4期/03_開発"])    # 03_開発 配下に .md は無い
    assert scoped == {"count": 0, "paths": [], "truncated": False} and docs == set()
    assert TD.run_tool("glob_search", {"pattern": "*.md"}, "v1", None)[0]["count"] > 0


def test_glob_search_truncates_at_200_and_marks_truncated(monkeypatch):
    from sherpa import doc_ledger as DL

    rows = [{"name": f"synth/{i:04d}.md", "branch": "docs"} for i in range(250)]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)
    res, docs, _, _ = TD.run_tool("glob_search", {"pattern": "synth/*.md"}, "v1", None)
    assert res["count"] == 250 and len(res["paths"]) == 200 and res["truncated"] is True and len(docs) == 200


@pytest.mark.parametrize("bad", ["", "   ", "/abs/path", "a\\b", "a/../b", "a//b",
                                 "x" * (RT._GLOB_PATTERN_MAX_LEN + 1), 123, None])
def test_glob_search_invalid_pattern_returns_error(bad):
    res, docs, cites, cards = TD.run_tool("glob_search", {"pattern": bad}, "v1", None)
    assert "error" in res and docs == set() and cites == [] and cards == []


def test_glob_search_layer_code_and_docs_partition():
    prefix = "4期/03_開発"
    all_files = CE.rel_paths_under(prefix)
    assert all_files
    res_code, docs_code, _, _ = TD.run_tool("glob_search", {"pattern": "*"}, "v1", [prefix], layer="code")
    assert set(res_code["paths"]) == all_files == docs_code
    res_docs, _, _, _ = TD.run_tool("glob_search", {"pattern": "*"}, "v1", [prefix], layer="docs")
    assert res_docs["paths"] == []


@pytest.fixture()
def small_tool_budget(monkeypatch):
    """コード既定（256KiB）でなく旧既定 64KiB に固定して、境界での打切りを小さい入力で確かめる。"""
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 65536)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 65536)


def test_run_tool_read_doc_normal_paginates_forward(monkeypatch, tmp_path):
    """1 ページの幅は最低 200 行（window_cap が小さくても）。最終ページは総行数で打ち切る。通常サイズでは打切りフラグが立たない。"""
    world = "read-doc-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": "\n".join(f"line {i}" for i in range(1, 451))})
    res1, docs1, cites1, cards1 = TD.run_tool("read_doc", {"doc_id": "doc.md"}, world, None, window_cap=5)
    assert "error" not in res1, res1
    assert (res1["start_line"], res1["end_line"], res1["total_lines"]) == (1, 200, 450)
    assert res1["text"] == "\n".join(f"{i}: line {i}" for i in range(1, 201))
    assert "text_truncated" not in res1 and "file_truncated" not in res1
    assert docs1 == {"doc.md"} and cites1 == [] and cards1 == []
    res2, _, _, _ = TD.run_tool("read_doc", {"doc_id": "doc.md", "start_line": 201}, world, None, window_cap=5)
    assert (res2["start_line"], res2["end_line"]) == (201, 400)
    res3, _, _, _ = TD.run_tool("read_doc", {"doc_id": "doc.md", "start_line": 401}, world, None, window_cap=5)
    assert (res3["start_line"], res3["end_line"]) == (401, 450)


@pytest.mark.parametrize("n_lines, window_cap, expected_end", [
    (300, 5, 200), (300, 40, 200), (300, 100, 200), (300, 199, 200),     # 200 行フロア
    (500, 250, 250), (500, 300, 300), (500, 400, 400),                   # window_cap が 200 を超えたらその値まで伸びる
])
def test_run_tool_read_doc_page_size_floor_and_scaling(monkeypatch, tmp_path, n_lines, window_cap, expected_end):
    world = "read-doc-size-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "\n".join(f"line {i}" for i in range(1, n_lines + 1))})
    res, _, _, _ = TD.run_tool("read_doc", {"doc_id": "big.md"}, world, None, window_cap=window_cap)
    assert "error" not in res, res
    assert (res["start_line"], res["end_line"], res["total_lines"]) == (1, expected_end, n_lines)


def test_run_tool_read_doc_byte_budget_stops_before_page_end_and_reports_actual_end_line(
        monkeypatch, tmp_path, small_tool_budget):
    """ページ幅どおりに組んでから一括クリップすると end_line の申告と実際の text が食い違う（無言の欠落）。
    予算を超える直前の行で止め、その行を実際の end_line にする。"""
    world = "read-doc-longline-world"
    long_line = "x" * 4000
    total_lines = 30
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "\n".join(long_line for _ in range(total_lines))})
    res, docs, _, _ = TD.run_tool("read_doc", {"doc_id": "big.md"}, world, None, window_cap=200)
    assert "error" not in res and res["total_lines"] == total_lines and res["text_truncated"] is True
    assert res["end_line"] < total_lines
    assert res["text"] == "\n".join(f"{i}: {long_line}" for i in range(1, res["end_line"] + 1))
    assert len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES and "big.md" in docs


def test_run_tool_read_doc_single_huge_line_is_clipped_with_text_truncated(monkeypatch, tmp_path, small_tool_budget):
    world = "read-doc-hugeline-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "A" * 200_000})
    res, _, _, _ = TD.run_tool("read_doc", {"doc_id": "big.md"}, world, None)
    assert "error" not in res and (res["start_line"], res["end_line"]) == (1, 1) and res["text_truncated"] is True
    assert len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES


@pytest.mark.parametrize("tool, content", [
    ("read_doc", "\n".join(f"line {i}" for i in range(1, 21))),
    ("doc_outline", "\n".join(f"# h{i}" for i in range(1, 21))),
])
def test_run_tool_read_doc_and_outline_file_cap_hit_sets_file_truncated(monkeypatch, tmp_path, tool, content):
    """ファイル読込が _READ_AROUND_FILE_CAP_BYTES に達したら file_truncated:true（total_lines/見出しが文書全体でない可能性の明示）。"""
    world = "filecap-world"
    monkeypatch.setattr(RT, "_READ_AROUND_FILE_CAP_BYTES", 50)
    assert len(content.encode("utf-8")) > 50
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, _, _, _ = TD.run_tool(tool, {"doc_id": "doc.md"}, world, None)
    assert "error" not in res and res["file_truncated"] is True
    if tool == "read_doc":
        assert res["total_lines"] < 20


def test_run_tool_ripgrep_search_reports_file_truncated_only_on_capped_hit(monkeypatch, tmp_path):
    """cap 打切りを再現すると hits に file_truncated:true が載る（読む経路と同じ語彙）。打切りの無いヒットにはキー自体が無い。"""
    world = "ripgrep-filecap-world"
    line1 = "NEEDLE line one\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "doc.txt": line1 + "x" * 200 + "\n", "doc.md": "# 見出し\n本文中に NEEDLE を含む一行\n"})
    res, _, _, _ = TD.run_tool("ripgrep_search", {"query": "NEEDLE"}, world, None)
    normal = next(h for h in res["hits"] if h["doc_id"] == "doc.md")
    assert "file_truncated" not in normal and set(normal) == {"doc_id", "line", "text"}
    monkeypatch.setattr(grep_tool_mod, "_GREP_FILE_CAP_BYTES", len(line1.encode("utf-8")) + 10)
    res, _, _, _ = TD.run_tool("ripgrep_search", {"query": "NEEDLE"}, world, None)
    assert "error" not in res
    assert next(h for h in res["hits"] if h["doc_id"] == "doc.txt")["file_truncated"] is True


def test_run_tool_read_doc_edge_cases(monkeypatch, tmp_path):
    """範囲外 start_line はエラー（出典に載せない）・空文書は 0 行でエラーにしない・負の start_line は 1 へ丸める
    （負インデックスで末尾から読まない）・不正型はエラー・秘密は伏せる。"""
    world = "read-doc-edge-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "doc.md": "\n".join(f"line {i}" for i in range(1, 26)), "empty.md": "",
        "secret.md": "api_key=sk-ABCDEFGHIJKLMNOP1234"})
    res, docs, cites, cards = TD.run_tool("read_doc", {"doc_id": "doc.md", "start_line": 26}, world, None, window_cap=5)
    assert "error" in res and docs == set() and cites == [] and cards == []
    res, docs, _, _ = TD.run_tool("read_doc", {"doc_id": "empty.md"}, world, None)
    assert "error" not in res and (res["start_line"], res["end_line"], res["total_lines"], res["text"]) == (1, 0, 0, "")
    assert "empty.md" in docs
    res, _, _, _ = TD.run_tool("read_doc", {"doc_id": "doc.md", "start_line": -5}, world, None, window_cap=3)
    assert res["start_line"] == 1 and res["text"].splitlines()[0] == "1: line 1"
    res, _, _, _ = TD.run_tool("read_doc", {"doc_id": "secret.md"}, world, None)
    assert "[REDACTED]" in res["text"] and "ABCDEFGHIJKLMNOP1234" not in res["text"]
    res, docs, cites, cards = TD.run_tool("read_doc", {"doc_id": "x.md", "start_line": "abc"}, "v1", None)
    assert "error" in res and docs == set() and cites == [] and cards == []


@pytest.mark.parametrize("tool", ["read_doc", "doc_outline"])
def test_run_tool_open_tools_reject_out_of_scope_and_doc_outside_layer(tool):
    """open ツールは範囲外・層外の doc_id を拒否する（層内なら読める）。"""
    assert "error" in TD.run_tool(tool, {"doc_id": "4期/04_運用/障害記録.md"}, "v1", ["5期"])[0]
    code_doc_id = TD.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="code")[0]["hits"][0]["doc_id"]
    assert "error" in TD.run_tool(tool, {"doc_id": code_doc_id}, "v1", None, layer="docs")[0]
    r_ok, docs_ok, _, _ = TD.run_tool(tool, {"doc_id": code_doc_id}, "v1", None, layer="code")
    assert "error" not in r_ok and code_doc_id in docs_ok


def test_run_tool_doc_outline_normal_returns_headings_with_line_numbers(monkeypatch, tmp_path):
    world = "outline-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "doc.md": "intro\n# 見出し1\n本文\n## 見出し2\n本文2\n### 見出し3\n#### 深すぎる見出し\n末尾"})
    res, docs, cites, cards = TD.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert "error" not in res and res["total_lines"] == 8 and res["truncated"] is False
    assert res["count"] == 3            # レベル 4 は対象外
    assert [(h["line"], h["level"], h["title"]) for h in res["headings"]] == [
        (2, 1, "見出し1"), (4, 2, "見出し2"), (6, 3, "見出し3")]
    assert docs == {"doc.md"} and cites == [] and cards == []


@pytest.mark.parametrize("content, total", [("line1\nline2\nline3", 3), ("", 0)])
def test_run_tool_doc_outline_without_headings(monkeypatch, tmp_path, content, total):
    world = "outline-noheading-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, _, _, _ = TD.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert res == {"doc_id": "doc.md", "total_lines": total, "count": 0, "headings": [], "truncated": False}


def test_run_tool_doc_outline_truncates_at_cap(monkeypatch, tmp_path):
    world = "outline-truncate-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": "\n".join(f"# h{i}" for i in range(250))})
    res, _, _, _ = TD.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert res["count"] == 250 and len(res["headings"]) == RT._OUTLINE_MAX_HEADINGS and res["truncated"] is True


def test_run_tool_doc_outline_truncates_by_byte_budget_before_count_cap(monkeypatch, tmp_path):
    """見出し件数が上限未満でも、タイトルの累積 UTF-8 バイト数が TOOL_RESULT_MAX_BYTES を超えたら打ち切る。"""
    world = "outline-bytebudget-world"
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 1000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 1000)
    content = "\n".join(f"# 長い見出しタイトルの例その{i}あいうえおかきくけこさしすせそ" for i in range(1, 21))
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, _, _, _ = TD.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert "error" not in res and res["count"] == 20 and 0 < len(res["headings"]) < 20 and res["truncated"] is True
    assert sum(len(h["title"].encode("utf-8")) for h in res["headings"]) <= 1000


def test_run_tool_doc_outline_redacts_secrets_in_title(monkeypatch, tmp_path):
    world = "outline-secret-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": "## config api_key=sk-ABCDEFGHIJKLMNOP1234"})
    res, _, _, _ = TD.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert "[REDACTED]" in res["headings"][0]["title"] and "ABCDEFGHIJKLMNOP1234" not in res["headings"][0]["title"]


@pytest.mark.parametrize("truncated_by_grep, expected", [
    (["big.xlsx"], ["big.xlsx"]),                                              # ヒット 0 件の打切り文書も LLM へ伝える
    ([], None),                                                                # 理由が無ければキーを作らない
    ([f"doc{i}.xlsx" for i in range(RT._TRUNCATED_DOCS_MAX + 5)], RT._TRUNCATED_DOCS_MAX),   # 件数上限でバイト予算を圧迫しない
])
def test_ripgrep_search_tool_result_truncated_docs(monkeypatch, truncated_by_grep, expected):
    def fake_grep(q, world, **kw):
        kw["truncated_docs"].extend(truncated_by_grep)
        return []

    monkeypatch.setattr(grep_tool_mod, "grep_search", fake_grep)
    view, _docs, _cites, _cards = TD.run_tool("ripgrep_search", {"query": "X"}, "w", None)
    assert view["hits"] == []
    if expected is None:
        assert "truncated_docs" not in view
    elif isinstance(expected, int):
        assert len(view["truncated_docs"]) == expected
    else:
        assert view["truncated_docs"] == expected


# ===== read 側のストリーミング走査（メモリ非比例・grep と行番号整合・単一巨大行の安全弁） =====

def _peak_bytes(fn):
    import tracemalloc

    tracemalloc.start()
    try:
        result = fn()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def test_run_tool_read_doc_and_read_around_bounded_memory_for_large_file(monkeypatch, tmp_path):
    """20MB 超のファイルでも read_doc（総行数のカウントは要るが窓外の内容は保持しない）・read_around（目的の窓に
    達したら読み切らず打ち切る）のピーク割当はファイルサイズに比例しない。"""
    world = "read-bigfile-world"
    line = "x" * 200 + "\n"
    n = (20 * 1024 * 1024) // len(line) + 10
    content = line * n
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": content})
    assert len(content.encode("utf-8")) > 20 * 1024 * 1024
    res, peak = _peak_bytes(lambda: TD.run_tool("read_doc", {"doc_id": "big.md"}, world, None)[0])
    assert "error" not in res and res["total_lines"] == n and peak < 5 * 1024 * 1024
    res, peak = _peak_bytes(lambda: TD.run_tool("read_around", {"doc_id": "big.md", "line": 5, "window": 2}, world, None)[0])
    assert "error" not in res and res["text"].splitlines()[0] == f"3: {line.rstrip(chr(10))}"
    assert peak < 2 * 1024 * 1024


def test_run_tool_read_doc_single_huge_line_bounded_memory_and_sets_file_truncated(monkeypatch, tmp_path):
    """改行なしで 30MB 続く単一行でも read_doc のピーク割当は非比例で、file_truncated を申告する。"""
    world = "read-doc-hugesingleline-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"huge.md": "x" * (30 * 1024 * 1024)})
    res, peak = _peak_bytes(lambda: TD.run_tool("read_doc", {"doc_id": "huge.md"}, world, None)[0])
    assert "error" not in res and res["total_lines"] == 1 and res["file_truncated"] is True
    assert peak < 15 * 1024 * 1024


def test_run_tool_read_around_single_huge_line_beyond_line_max_bounded_memory(monkeypatch, tmp_path):
    """_READ_LINE_MAX_BYTES(2MiB) を超える単一行（10MB）でも、ピーク割当は非比例・返却テキストは TOOL_RESULT_MAX_BYTES 内。"""
    world = "read-around-hugesingleline-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"huge.md": "A" * (10 * 1024 * 1024)})
    res, peak = _peak_bytes(lambda: TD.run_tool("read_around", {"doc_id": "huge.md", "line": 1, "window": 5}, world, None)[0])
    assert "error" not in res and len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES
    assert peak < 15 * 1024 * 1024


def test_run_tool_grep_hit_line_matches_read_around_for_special_separators(monkeypatch, tmp_path):
    """`\\f`・`\\x85` は str.splitlines() の区切りだが実バイトの `\\n` ではない——grep と read 側は
    grep_tool._logical_lines を共有するため、grep の行番号をそのまま read_around に渡すと同じ行が返る。"""
    world = "sep-consistency-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.txt": "alpha\nbeta\x0cGAMMA_NEEDLE\ndelta\x85epsilon\n"})
    hit_res, _, _, _ = TD.run_tool("ripgrep_search", {"query": "GAMMA_NEEDLE"}, world, None)
    assert "error" not in hit_res and len(hit_res["hits"]) == 1 and hit_res["hits"][0]["line"] == 3
    around_res, _, _, _ = TD.run_tool("read_around", {"doc_id": "doc.txt", "line": 3, "window": 1}, world, None)
    assert "error" not in around_res and around_res["text"] == "2: beta\n3: GAMMA_NEEDLE\n4: delta"


# ===== 親返し（es_search 限定・常時 ON）: 検索は細かく・回答には文脈を =====
# ヒットを doc_id で束ね、予算内なら rag.md の領域(P2)を返し、超える場合は子チャンク（chunk・最低保証）のまま。
# 全文(P3)段は無い（全文が要るなら read_doc）。ここで検証するのは調査側（agentic_search）のみ。

def _setup_parent_return_world(monkeypatch, tmp_path, world: str, hits: list, rag_files: dict) -> None:
    """es_index.search / documents.world_rel_set をスタブし、rag_files（{doc_id: rag.md 本文}）を derived_rag_dir 配下へ書く。"""
    from sherpa import documents
    der_rag = tmp_path / "rag"
    der_rag.mkdir(parents=True, exist_ok=True)
    for doc_id, content in rag_files.items():
        (der_rag / (doc_id + ".rag.md")).write_text(content, encoding="utf-8")
    monkeypatch.setattr(worlds_mod, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: tmp_path / "md")
    monkeypatch.setattr(A.es_index, "search", lambda w, q, scope_paths=None, k=20, layer=None, **kw: (list(hits), None))
    monkeypatch.setattr(documents, "world_rel_set", lambda w, **kw: {h["doc_id"] for h in hits})


def _prepare_parent_return(monkeypatch, tmp_path, hits, rag_files, budget, chunk_ids=None):
    """親返しの es_search を実行する前の準備。chunk_ids は chunk_ids_for_parent の差し替え（関数 or 固定リスト・None なら既定）。"""
    _setup_parent_return_world(monkeypatch, tmp_path, "parent-return-world", hits, rag_files)
    if chunk_ids is not None:
        monkeypatch.setattr(A.es_index, "chunk_ids_for_parent",
                            chunk_ids if callable(chunk_ids) else (lambda w, doc_id, parent_ids, limit=5000: chunk_ids))
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", budget)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", budget)


def _es_search_parent_world():
    res, _docs, cites, _ = TD.run_tool("es_search", {"query": "q"}, "parent-return-world", None)
    return res, cites


def _run_parent_return(monkeypatch, tmp_path, hits, rag_files, budget, chunk_ids=None):
    _prepare_parent_return(monkeypatch, tmp_path, hits, rag_files, budget, chunk_ids)
    return _es_search_parent_world()


def _chunk_hit(doc_id, chunk_id, parent_id, score, text="本文", ext=".docx", **extra):
    return {"doc_id": doc_id, "text": text, "ext": ext, "chunk_id": chunk_id, "parent_id": parent_id,
            "score": score, **extra}


def test_parent_return_no_full_tier_region_or_chunk_by_size(monkeypatch, tmp_path):
    """tier は region か chunk にしかならない（ベストスコア順に region→chunk を試す）。region は親グループに入る
    チャンクだけを集め（対象外 pad1 を含まない）、親グループが自分自身だけなら全文なら含まれた無関係な後続
    チャンク（cf2）も混ぜない（P3 撤去の実害＝無関係な内容を混ぜないことの直接証拠）。"""
    single_chunk_md = "<!-- chunk:cf1 -->\n" + "F" * 200 + "\n\n<!-- chunk:cf2 -->\n" + "Z" * 5000 + "\n"
    region_md = ("<!-- chunk:cr1 -->\n" + "R" * 100 + "\n\n<!-- chunk:cr2 -->\n" + "R" * 100 + "\n\n"
                 "<!-- chunk:pad1 -->\n" + "P" * 5000 + "\n")
    chunk_md = "<!-- chunk:cc1 -->\n" + "C" * 50 + "\n\n<!-- chunk:cc2 -->\n" + "C" * 5000 + "\n"
    hits = [_chunk_hit("many_chunks.docx", "cf1", "pf", 3.0, "F"), _chunk_hit("region.docx", "cr1", "pr", 2.0, "R"),
            _chunk_hit("chunk.docx", "cc1", "pc", 1.0, "C")]
    own_chunks = {"many_chunks.docx": ["cf1"], "region.docx": ["cr1", "cr2"], "chunk.docx": ["cc1", "cc2"]}
    res, _ = _run_parent_return(
        monkeypatch, tmp_path, hits,
        {"many_chunks.docx": single_chunk_md, "region.docx": region_md, "chunk.docx": chunk_md}, 1000,
        chunk_ids=lambda w, doc_id, parent_ids, limit=5000: own_chunks.get(doc_id, []))
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert {h["tier"] for h in res["hits"]} <= {"region", "chunk"}
    assert by_doc["many_chunks.docx"]["tier"] == "region"
    assert "F" * 200 in by_doc["many_chunks.docx"]["text"] and "Z" * 5000 not in by_doc["many_chunks.docx"]["text"]
    assert by_doc["region.docx"]["tier"] == "region"
    assert "R" * 100 in by_doc["region.docx"]["text"] and "P" * 5000 not in by_doc["region.docx"]["text"]
    assert by_doc["chunk.docx"]["tier"] == "chunk" and by_doc["chunk.docx"]["text"] == "C"
    assert [h["doc_id"] for h in res["hits"]] == ["many_chunks.docx", "region.docx", "chunk.docx"]


def test_parent_return_minimum_guarantee_lower_score_doc_survives(monkeypatch, tmp_path):
    """スコア 1 位が領域で予算の残りを使い切っても、2 位の子チャンク（最低保証）は消えない・空にならない。"""
    doc1_md = "<!-- chunk:c1 -->\n" + "A" * 400 + "\n"
    doc2_md = "<!-- chunk:c2 -->\n" + "B" * 50000 + "\n"
    hits = [_chunk_hit("top.docx", "c1", "p1", 2.0, "top-baseline"), _chunk_hit("second.docx", "c2", "p2", 1.0, "second-baseline")]
    baseline_total = len("top-baseline".encode("utf-8")) + len("second-baseline".encode("utf-8"))
    delta_doc1 = len(doc1_md.encode("utf-8")) - len("top-baseline".encode("utf-8"))
    res, _ = _run_parent_return(monkeypatch, tmp_path, hits, {"top.docx": doc1_md, "second.docx": doc2_md},
                                baseline_total + delta_doc1, chunk_ids=[])
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["top.docx"]["tier"] == "region" and by_doc["top.docx"]["text"] == "A" * 400
    assert by_doc["second.docx"]["tier"] == "chunk" and by_doc["second.docx"]["text"] == "second-baseline"
    assert "text_truncated" not in by_doc["second.docx"]     # 共有予算の枯渇が理由（per_doc_cap の頭打ちではない）


def test_parent_return_declares_tier_for_every_doc(monkeypatch, tmp_path):
    """アップグレードできなかった doc（rag.md 不在）も tier を申告する（限界に当たったら黙らない）。"""
    res, _ = _run_parent_return(monkeypatch, tmp_path, [_chunk_hit("a.docx", "c1", "p1", 1.0, "本文A")], {}, 262144, chunk_ids=[])
    assert res["hits"] == [{"doc_id": "a.docx", "tier": "chunk", "text": "本文A", "chunks": [{"chunk_id": "c1"}]}]


def test_parent_return_deterministic(monkeypatch, tmp_path):
    md = "<!-- chunk:c1 -->\n" + "X" * 100 + "\n\n<!-- chunk:c2 -->\n" + "Y" * 100 + "\n"
    hits = [_chunk_hit("a.docx", "c1", "p", 1.5, "aの本文"), _chunk_hit("b.docx", "c2", "p", 1.5, "bの本文")]
    res1, _ = _run_parent_return(monkeypatch, tmp_path, hits, {"a.docx": md, "b.docx": md}, 5000, chunk_ids=[])
    res2, _ = _es_search_parent_world()
    assert res1 == res2
    assert [h["doc_id"] for h in res1["hits"]] == ["a.docx", "b.docx"]      # 同点スコアは doc_id 昇順


def test_parent_return_citations_stay_at_chunk_grain(monkeypatch, tmp_path):
    """LLM 表示は doc 単位に束ねても、引用は従来どおり子チャンク単位（locator 付き）のまま。"""
    md = "<!-- chunk:c1 -->\nセルA本文\n\n<!-- chunk:c2 -->\nセルB本文\n"
    hits = [_chunk_hit("a.xlsx", "c1", "p", 1.0, "セルA本文", ".xlsx", locator={"sheet": "一覧", "cell_range": "A1"}),
            _chunk_hit("a.xlsx", "c2", "p", 1.0, "セルB本文", ".xlsx", locator={"sheet": "一覧", "cell_range": "B1"})]
    res, cites = _run_parent_return(monkeypatch, tmp_path, hits, {"a.xlsx": md}, 5000, chunk_ids=[])
    assert len(res["hits"]) == 1 and res["hits"][0]["doc_id"] == "a.xlsx"
    assert len(cites) == 2 and {c["quote"] for c in cites} == {"セルA本文", "セルB本文"}
    assert res["hits"][0]["chunks"] == [{"chunk_id": "c1", "locator": {"sheet": "一覧", "cell_range": "A1"}},
                                         {"chunk_id": "c2", "locator": {"sheet": "一覧", "cell_range": "B1"}}]


def test_parent_return_legacy_hits_pass_through_untouched(monkeypatch, tmp_path):
    """legacy 40 行チャンク由来（chunk_id 無し）は親返しの対象外＝rag チャンクと混在しても形が変わらない。"""
    hits = [_chunk_hit("rag.docx", "c1", "p", 2.0, "rag本文"),
            {"doc_id": "legacy.md", "line": 7, "text": "legacy本文", "ext": ".md", "score": 1.0}]
    res, _ = _run_parent_return(monkeypatch, tmp_path, hits, {"rag.docx": "<!-- chunk:c1 -->\nrag本文\n"}, 5000, chunk_ids=[])
    legacy_entries = [h for h in res["hits"] if h["doc_id"] == "legacy.md"]
    assert legacy_entries == [{"doc_id": "legacy.md", "line": 7, "text": "legacy本文"}]


def test_parent_return_redacts_region_text(monkeypatch, tmp_path):
    """領域(P2)の本文（rag.md から直接読んだテキスト）にも伏せ字が効く。"""
    secret = "sk-1234567890ABCDEFGHIJ"
    res, _ = _run_parent_return(monkeypatch, tmp_path, [_chunk_hit("a.docx", "c1", "p1", 1.0)],
                                {"a.docx": f"<!-- chunk:c1 -->\nAPIキー: {secret}\n"}, 5000)
    assert res["hits"][0]["tier"] == "region"
    assert secret not in res["hits"][0]["text"] and "[REDACTED]" in res["hits"][0]["text"]


def test_parent_return_never_returns_full_tier_even_with_huge_budget(monkeypatch, tmp_path):
    """旧 P3 なら確実に全文を選んだ潤沢な予算でも tier は region のみ（"full" が出力に現れない）。"""
    res, _ = _run_parent_return(monkeypatch, tmp_path, [_chunk_hit("a.docx", "c1", "p1", 1.0, "本文A")],
                                {"a.docx": "<!-- chunk:c1 -->\n" + "A" * 100 + "\n"}, 1_000_000, chunk_ids=["c1"])
    assert res["hits"][0]["tier"] == "region" and "full" not in json.dumps(res)


def test_parent_return_per_doc_cap_stops_one_doc_eating_shared_budget(monkeypatch, tmp_path):
    """領域にも 1 文書あたりの上限（総予算を件数で均等割り・下限 _HIT_TEXT_MIN_BYTES）を掛ける。1 位が上限に
    阻まれて chunk に留まり（text_truncated を申告）、余った共有予算で 2 位が領域を得る。"""
    hits = [_chunk_hit("top.docx", "c1", "p1", 2.0, "top-chunk"), _chunk_hit("second.docx", "c2", "p2", 1.0, "second-chunk")]
    res, _ = _run_parent_return(
        monkeypatch, tmp_path, hits,
        {"top.docx": "<!-- chunk:c1 -->\n" + "A" * 2500 + "\n", "second.docx": "<!-- chunk:c2 -->\n" + "B" * 1500 + "\n"},
        4000, chunk_ids=lambda w, doc_id, parent_ids, limit=5000: (["c1"] if doc_id == "top.docx" else ["c2"]))
    by_doc = {h["doc_id"]: h for h in res["hits"]}     # budget 4000・2 件 → per_doc_cap=2000
    assert by_doc["top.docx"]["tier"] == "chunk" and by_doc["top.docx"]["text"] == "top-chunk"
    assert by_doc["top.docx"]["text_truncated"] is True
    assert by_doc["second.docx"]["tier"] == "region" and by_doc["second.docx"]["text"] == "B" * 1500
    assert "text_truncated" not in by_doc["second.docx"]
    assert res["text_truncated"] is True      # tool_result_clipped 計測が拾う最上位フラグにも合流する


@pytest.mark.parametrize("target, budget, tier, expected_text", [
    ("<!-- chunk:t1 -->\n領域本文1。\n\n<!-- chunk:t2 -->\n領域本文2。\n", 2000, "region", None),
    ("<!-- chunk:t1 -->\n" + "領" * 3000 + "\n\n<!-- chunk:t2 -->\n" + "域" * 3000 + "\n", 200, "chunk", "本文"),
])
def test_parent_return_bounded_memory_for_large_rag_md(monkeypatch, tmp_path, target, budget, tier, expected_text):
    """20MB 超の rag.md でも、対象外チャンクを蓄積せずスキップし（領域が予算を超えるなら全文を読み切らず打ち切り）、
    ピーク割当はファイルサイズに比例しない。対象チャンクは末尾に置く最悪ケース。"""
    pad_body = "x" * 5000
    n = (20 * 1024 * 1024) // (len(pad_body) + 40) + 1000
    rag_md = "".join(f"<!-- chunk:pad{i} -->\n{pad_body}\n\n" for i in range(n)) + target
    assert len(rag_md.encode("utf-8")) > 20 * 1024 * 1024
    _prepare_parent_return(monkeypatch, tmp_path, [_chunk_hit("big.docx", "t1", "pt", 1.0)], {"big.docx": rag_md},
                           budget, chunk_ids=["t1", "t2"])
    (res, _cites), peak = _peak_bytes(_es_search_parent_world)
    assert res["hits"][0]["tier"] == tier and peak < 10 * 1024 * 1024
    if expected_text is None:
        assert "領域本文1。" in res["hits"][0]["text"] and "領域本文2。" in res["hits"][0]["text"]
    else:
        assert res["hits"][0]["text"] == expected_text


# ===== 原本読取ツール（xlsx/docx/pptx/pdf/file_head）: _safe_original_path の封じ込めと run_tool への配線 =====
# doc_readers.py 自体の入出力契約は test_doc_readers.py が担う。ここでは doc_id→実パス解決と run_tool 経由の docs/伏せ字/layer を見る。

def _write_xlsx(path: pathlib.Path, cell_value: str = "hello") -> None:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = cell_value
    wb.save(path)


def test_safe_original_path_confinement_and_resolution(monkeypatch, tmp_path):
    """traversal・絶対パス・実在しない・kinds 外・秘匿名・重要度設定ファイル・symlink・範囲外・.xlsm（台帳が文書種別として
    扱わない）は None。解決できるものは派生 MD でなく world root の原本そのもの（検査直後の stat も返す）。"""
    world = "s3b-original"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "credentials.xlsx": b"", "_重要度.txt": "*.xlsx: 高\n", "a.xlsm": b""})
    wd = tmp_path / "kb" / world
    _write_xlsx(wd / "a.xlsx")
    (wd / "4期").mkdir()
    _write_xlsx(wd / "4期" / "a.xlsx")
    outside = tmp_path / "outside.xlsx"
    _write_xlsx(outside)
    (wd / "link.xlsx").symlink_to(outside)
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)

    for bad in ("../../../etc/passwd", "/etc/passwd", "a/../../etc/hosts", "", "missing.xlsx", "credentials.xlsx",
                "link.xlsx", "a.xlsm"):
        assert RT._safe_original_path(world, bad, None, kinds=RT._XLSX_KINDS) is None, bad
    assert RT._safe_original_path(world, "a.xlsx", None, kinds=RT._DOCX_KINDS) is None
    assert RT._safe_original_path(world, "_重要度.txt", None, kinds=RT._FILE_HEAD_KINDS) is None
    assert RT._safe_original_path(world, "4期/a.xlsx", ["5期"], kinds=RT._XLSX_KINDS) is None
    root, doc_id, rp, st = RT._safe_original_path(world, "4期/a.xlsx", ["4期"], kinds=RT._XLSX_KINDS)
    assert root == wd and doc_id == "4期/a.xlsx" and rp == (wd / "4期" / "a.xlsx").resolve()
    assert st.st_ino == rp.stat().st_ino


def _setup_office_world(monkeypatch, tmp_path, world: str):
    _isolate_world_kb(monkeypatch, tmp_path, world, {"note.py": "print('hi')\n", "note.txt": "hello\n"})
    wd = tmp_path / "kb" / world
    _write_xlsx(wd / "a.xlsx", cell_value="秘密: sk-ABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    return wd


def test_run_tool_xlsx_sheets_and_range_add_docs_and_redact(monkeypatch, tmp_path):
    world = "s3b-run-xlsx"
    _setup_office_world(monkeypatch, tmp_path, world)
    r, docs, _cites, _cards = TD.run_tool("xlsx_sheets", {"doc_id": "a.xlsx"}, world, None)
    assert r["sheets"] == [{"name": "Sheet1", "max_row": 1, "max_col": 1}]
    assert r["doc_id"] == "a.xlsx" and r["locator"] == "sheets" and "Sheet1" in r["text"] and docs == {"a.xlsx"}
    r2, docs2, _, _ = TD.run_tool("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet1"}, world, None)
    assert docs2 == {"a.xlsx"} and r2["rows"] == [["秘密: [REDACTED]"]]
    assert r2["doc_id"] == "a.xlsx" and r2["locator"] == "Sheet1!A1:A1"
    assert "[REDACTED]" in r2["text"] and "秘密: sk-" not in r2["text"]      # read_evidence 用の text も伏せ字済み
    r3, docs3, _, _ = TD.run_tool("xlsx_range", {"doc_id": "a.xlsx"}, world, None)
    assert r3 == {"error": "sheet が必要です"} and docs3 == set()          # 失敗時は出典に載せない


def test_run_tool_file_head_reads_text_and_respects_layer(monkeypatch, tmp_path):
    world = "s3b-run-filehead"
    _setup_office_world(monkeypatch, tmp_path, world)
    r, docs, _, _ = TD.run_tool("file_head", {"doc_id": "note.txt"}, world, None)
    assert r["size"] == 6 and r["text"] == "hello\n" and r["truncated"] is False
    assert r["doc_id"] == "note.txt" and r["locator"] == "head" and docs == {"note.txt"}
    # note.py はコード判定＝layer="docs" では読めず layer="code" では読める
    r_docs, _, _, _ = TD.run_tool("file_head", {"doc_id": "note.py"}, world, None, layer="docs")
    assert r_docs == {"error": "doc_id が無効、または読み取り対象外です"}
    r_code, docs_code, _, _ = TD.run_tool("file_head", {"doc_id": "note.py"}, world, None, layer="code")
    assert docs_code == {"note.py"} and "print" in r_code["text"]


def test_run_tool_office_tools_error_when_layer_restricted_to_code(monkeypatch, tmp_path):
    world = "s3b-run-layer"
    _setup_office_world(monkeypatch, tmp_path, world)
    for name, args in (("xlsx_sheets", {"doc_id": "a.xlsx"}), ("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet1"}),
                       ("docx_paragraphs", {"doc_id": "a.xlsx"}), ("pptx_slides", {"doc_id": "a.xlsx"}),
                       ("pdf_pages", {"doc_id": "a.xlsx"})):
        r, docs, _, _ = TD.run_tool(name, args, world, None, layer="code")
        assert r == {"error": "探す対象がソースに限定されています"} and docs == set(), name


def test_run_tool_file_head_respects_tool_result_max_bytes(monkeypatch, tmp_path):
    """tool_result_max_bytes を無視して大きな text を返さない（JSON 全体のバイト数を予算内に収める）。"""
    world = "s3b-run-filehead-budget"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.txt": "x" * 100_000})
    res, docs, _, _ = TD.run_tool("file_head", {"doc_id": "big.txt"}, world, None, tool_result_max_bytes=16384)
    assert "error" not in res and res["truncated"] is True and docs == {"big.txt"}
    assert len(json.dumps(res, ensure_ascii=False).encode("utf-8")) <= 16384


def test_run_tool_file_head_redacts_private_key_truncated_by_max_bytes(monkeypatch, tmp_path):
    """max_bytes で PEM 鍵ブロックの END 側が切り落とされても鍵本文の断片が残らない（_redact は BEGIN/END が対で揃わないと
    マッチしないため、未終端の鍵ブロックは補完して伏せる）。"""
    world = "s3b-filehead-key-redact"
    key_body = "A" * 500
    content = f"prefix\n-----BEGIN RSA PRIVATE KEY-----\n{key_body}\n-----END RSA PRIVATE KEY-----\nsuffix\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"secret.txt": content})
    cut = content.index(key_body) + 50
    res, docs, _, _ = TD.run_tool("file_head", {"doc_id": "secret.txt", "max_bytes": cut}, world, None)
    assert "error" not in res and "-----END" not in res["text"] and "AAAA" not in res["text"]
    assert "[REDACTED]" in res["text"] and "prefix" in res["text"] and docs == {"secret.txt"}


@pytest.mark.parametrize("case", ["leaf_swapped_to_symlink", "ancestor_dir_swapped_to_symlink"])
def test_run_tool_toctou_rejects_path_swapped_to_symlink_after_check(monkeypatch, tmp_path, case):
    """_safe_original_path の検査後、open までの間に検査済みパスが KB 外への symlink に差し替えられても読めない。
    祖先ディレクトリの差し替えは、外部ディレクトリに同じ inode をハードリンクして fstat 突合だけでは検出できない状況にしても、
    途中の symlink で ELOOP になり拒否する。"""
    world = "s3b-toctou"
    rel = "note.txt" if case == "leaf_swapped_to_symlink" else "sub/note.txt"
    _isolate_world_kb(monkeypatch, tmp_path, world, {rel: "hello\n"})
    wd = tmp_path / "kb" / world
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    real_resolved = RT._safe_original_path(world, rel, None, kinds=RT._FILE_HEAD_KINDS)
    assert real_resolved is not None
    monkeypatch.setattr(RT, "_safe_original_path", lambda *a, **kw: real_resolved)
    if case == "leaf_swapped_to_symlink":
        outside = tmp_path / "outside.txt"
        outside.write_text("secret outside kb\n", encoding="utf-8")
        rp = real_resolved[2]
        rp.unlink()
        rp.symlink_to(outside)
    else:
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        sub_dir = wd / "sub"
        os.link(str(sub_dir / "note.txt"), str(outside / "note.txt"))
        shutil.rmtree(sub_dir)
        sub_dir.symlink_to(outside)
    res, docs, _, _ = TD.run_tool("file_head", {"doc_id": rel}, world, None)
    assert res == {"error": "読み取りに失敗しました", "error_code": "read_io_failed"} and docs == set()


# ===== _redact_deep: 文書順の状態付き走査（構造をまたぐ PEM 鍵ブロック） =====

@pytest.mark.parametrize("result, texts_of", [
    ({"paragraphs": [{"i": i, "style": "Normal", "text": t} for i, t in enumerate(
        ["prefix -----BEGIN RSA PRIVATE KEY-----", "A" * 200, "-----END RSA PRIVATE KEY----- suffix"])], "tables": []},
     lambda out: [p["text"] for p in out["paragraphs"]]),
    ({"sheet": "Sheet1", "range": "A1:A3", "truncated": False,
      "rows": [["prefix -----BEGIN RSA PRIVATE KEY-----"], ["A" * 200], ["-----END RSA PRIVATE KEY----- suffix"]]},
     lambda out: [r[0] for r in out["rows"]]),
    ({"truncated": False, "pages": [{"no": i, "text": t} for i, t in enumerate(
        ["prefix -----BEGIN RSA PRIVATE KEY-----", "A" * 200, "-----END RSA PRIVATE KEY----- suffix"], 1)]},
     lambda out: [p["text"] for p in out["pages"]]),
])
def test_redact_deep_redacts_pem_key_spanning_multiple_elements(result, texts_of):
    texts = texts_of(RT._redact_deep(result))
    assert "prefix" in texts[0] and "BEGIN" not in texts[0] and "[REDACTED]" in texts[0]
    assert texts[1] == "[REDACTED]"                      # 中間（鍵本文のみ）は丸ごと伏せる
    assert "suffix" in texts[2] and "END" not in texts[2] and "[REDACTED]" in texts[2]


# ===== _finish_reader_result / _doc_reader_text_locator（doc_readers の出力形を模した dict で直接検証） =====

@pytest.mark.parametrize("tool, result, items_key, index_key", [
    ("pdf_pages", {"total": 1, "pages": [{"no": 1, "text": "あ" * 3000}], "truncated": False}, "pages", "no"),
    ("docx_paragraphs", {"total": 1, "total_tables": 0, "paragraphs": [{"i": 0, "style": "Normal", "text": "あ" * 3000}],
                         "tables": [], "truncated": False}, "paragraphs", "i"),
])
def test_finish_reader_result_single_item_too_big_keeps_truncated_text(tool, result, items_key, index_key):
    """1 件だけでもバイト予算を超えるとき（日本語 3000 字＝9000 バイト超）、二分探索で 0 件に潰して本文を消さず、
    先頭 1 件を予算内へ切り詰めて text_truncated:true で残す（番号・locator は保つ）。"""
    out = RT._finish_reader_result(tool, result, "big.bin", 16384)
    assert "error" not in out and len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 16384
    assert out["truncated"] is True and len(out[items_key]) == 1
    assert out[items_key][0][index_key] == (1 if tool == "pdf_pages" else 0)
    assert out[items_key][0]["text_truncated"] is True and out[items_key][0]["text"]
    if tool == "pdf_pages":
        assert out["locator"] == "pages[1]"


def test_finish_reader_result_docx_tables_are_clipped_to_budget():
    """表（tables）もバイト予算の削減対象（以前は段落だけを削り、50×8 の大きな表で予算を超えた）。"""
    rows = [[f"r{r}c{c}" * 20 for c in range(8)] for r in range(50)]
    result = {"paragraphs": [], "tables": [{"i": 0, "row_start": 0, "total_rows": 50, "rows": rows}], "truncated": False}
    out = RT._finish_reader_result("docx_paragraphs", result, "big.docx", 65536)
    assert "error" not in out and len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 65536
    assert out["truncated"] is True and len(out["tables"]) == 1
    assert 0 < len(out["tables"][0]["rows"]) < 50 and out["tables"][0]["row_truncated"] is True


def test_finish_reader_result_xlsx_range_updates_range_and_locator_after_row_clip():
    """行がバイト予算で削られたら range・locator も実際に返した行数へ更新する。"""
    result = {"sheet": "Sheet1", "range": "A1:A50", "rows": [[f"v{r}" * 100] for r in range(50)], "truncated": False}
    out = RT._finish_reader_result("xlsx_range", result, "big.xlsx", 8192)
    assert "error" not in out and len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 8192
    n = len(out["rows"])
    assert out["truncated"] is True and 0 < n < 50
    assert out["range"] == f"A1:A{n}" and out["locator"] == f"Sheet1!A1:A{n}"


def test_doc_reader_text_locator_docx_table_only_and_row_paging():
    """段落が無く表だけの docx でも本文合成の対象（read_evidence の text が空にならない）。locator は表番号の範囲に加え
    実際に返した行範囲を含み、表の行ページングの 2 回を区別する。"""
    text, locator = RT._doc_reader_text_locator("docx_paragraphs", {
        "paragraphs": [], "tables": [{"i": 0, "row_start": 0, "total_rows": 2, "rows": [["h1", "h2"], ["v1", "v2"]]}]})
    assert "表0行0" in text and "h1" in text and "v2" in text and locator == "tables[0-0]rows[0-1]"

    def _page(row_start: int) -> dict:
        return {"paragraphs": [{"i": 0, "style": "Normal", "text": "見出し"}],
                "tables": [{"i": 0, "row_start": row_start, "total_rows": 60,
                            "rows": [[f"T{row_start + i}"] for i in range(50)]}], "truncated": True}

    assert RT._doc_reader_text_locator("docx_paragraphs", _page(0))[1] == "paragraphs[0-0];tables[0-0]rows[0-49]"
    assert RT._doc_reader_text_locator("docx_paragraphs", _page(50))[1] == "paragraphs[0-0];tables[0-0]rows[50-99]"


def test_doc_reader_text_locator_pptx_includes_tables_and_notes():
    text, locator = RT._doc_reader_text_locator("pptx_slides", {
        "slides": [{"no": 1, "texts": ["title"], "tables": [[["a", "b"]]], "notes": "memo"}]})
    assert "title" in text and "a" in text and "b" in text and "memo" in text and locator == "slides[1]"


def test_finish_docx_paragraphs_result_keeps_paragraphs_before_big_tables():
    """大きな表を持つ docx でも段落が先に確保され、余った予算で表の行が入る（段落 0 件にならない）。"""
    paras = [{"i": i, "style": "Normal", "text": f"段落{i} " + "あ" * 50} for i in range(30)]
    tables = [{"i": t, "row_start": 0, "total_rows": 50, "rows": [["セル" * 40] * 8 for _ in range(50)]} for t in range(20)]
    r = RT._finish_docx_paragraphs_result(
        {"total": 30, "total_tables": 20, "paragraphs": paras, "tables": tables, "truncated": False}, "big.docx", 262144)
    assert len(r["paragraphs"]) == 30 and r["truncated"] is True
    assert sum(len(t["rows"]) for t in r["tables"]) > 0 and RT._result_byte_size(r) <= 262144


@pytest.mark.parametrize("table_row, table_survives", [
    (["r1c1"], True),        # 小さな表 1 行があっても、段落 0 件のときは必ず救済（先頭段落を切り詰めて確保）を経由する
    (["x" * 8050], False),   # 表の 1 行が予算の大半を占めるときは、表を 0 行にして先に段落を救済し、残り予算で表の行数を決める
])
def test_finish_docx_paragraphs_result_rescues_paragraph_before_table(table_row, table_survives):
    result = {"total": 1, "total_tables": 1, "paragraphs": [{"i": 0, "style": "Normal", "text": "あ" * 3000}],
              "tables": [{"i": 0, "row_start": 0, "total_rows": 1, "rows": [table_row]}], "truncated": False}
    r = RT._finish_docx_paragraphs_result(result, "big.docx", 16384)
    assert "error" not in r and len(json.dumps(r, ensure_ascii=False).encode("utf-8")) <= 16384
    assert r["truncated"] is True and len(r["paragraphs"]) == 1
    assert r["paragraphs"][0]["text_truncated"] is True and r["paragraphs"][0]["text"]     # 空にはしない
    if table_survives:
        assert len(r["tables"]) == 1 and r["tables"][0]["rows"] == [table_row]


# ===== 検索結果の truncated 判定 =====

def test_search_results_mark_truncated_when_hit_cap_reached(monkeypatch):
    fake = [{"doc_id": f"d{i}.md", "line": 1, "text": "x", "span": [1, 1]} for i in range(3)]
    monkeypatch.setattr(grep_tool_mod, "grep_search", lambda *a, **k: list(fake))
    assert TD.run_tool("ripgrep_search", {"query": "x"}, "v1", None, max_hits=3)[0].get("truncated") is True
    monkeypatch.setattr(grep_tool_mod, "grep_search", lambda *a, **k: list(fake[:2]))
    assert "truncated" not in TD.run_tool("ripgrep_search", {"query": "x"}, "v1", None, max_hits=3)[0]


def test_es_search_cap_is_judged_on_raw_hits_before_filtering(monkeypatch):
    _stub_es(monkeypatch, [{"doc_id": f"d{i}.md", "line": 1, "text": "x", "score": 1.0, "span": [1, 1]} for i in range(3)],
             {"d0.md", "d1.md"})      # 1 件は実在せず落ちる
    res, *_ = TD.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=3)
    assert res.get("truncated") is True and len(res["hits"]) <= 2


# ===== 障害種別の分類（回復可否・固定理由コード） =====

def test_open_failures_carry_read_io_error_code(monkeypatch, tmp_path):
    """読取 I/O の open 失敗（OSError）は例外にせず固定理由コード read_io_failed を結果へ付ける
    （_open_doc_stream も、原本読取ツールの TOCTOU 再検証 open＝_open_verified_original も）。"""
    from sherpa import scope as scope_mod

    def _boom(root, rel_parts):
        raise OSError("boom")

    expected = {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
    monkeypatch.setattr(RT, "_open_file_nofollow_walk", _boom)
    monkeypatch.setattr(RT, "_safe_doc_path", lambda world, doc_id, layer=None: (tmp_path, "x.txt", tmp_path / "x.txt"))
    monkeypatch.setattr(scope_mod, "in_scope", lambda doc_id, sp: True)
    assert RT._open_doc_stream("v1", "x.txt", None, None) == (None, expected)
    assert RT._open_verified_original(tmp_path, "x.txt", None) == (None, expected)


@pytest.mark.parametrize("make_exc, reason", [
    (lambda: urllib.error.HTTPError("http://es/_search", 400, "Bad Request", {}, None), "es_query_rejected"),   # クエリ自体の拒否＝回復不可
    (lambda: urllib.error.HTTPError("http://es/_search", 503, "Service Unavailable", {}, None), "es_query_failed"),
    (lambda: urllib.error.HTTPError("http://es/_search", 404, "Not Found", {}, None), "es_query_failed"),   # 索引未作成＝未取り込み world の常態は回復可能
    (lambda: json.JSONDecodeError("bad json", "not json", 0), "es_query_rejected"),   # 非通信例外でも search() は例外を投げない
])
def test_es_index_search_classifies_failures(monkeypatch, make_exc, reason):
    from sherpa import es_index

    def boom(method, path, body=None, ndjson=False, timeout=es_index._TIMEOUT):
        raise make_exc()

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_req", boom)
    assert es_index.search("v1", "query", vector=False) == ([], reason)


# ===== グラフの 3 状態（空・世代不一致・接続断）を区別して調査を止めない（モックは Neo4j ドライバ境界だけ） =====

class _FakeRecord(dict):
    def data(self):
        return dict(self)


class _FakeResult:
    def __init__(self, rows):
        self._rows = [_FakeRecord(r) for r in rows]

    def __iter__(self):
        return iter(self._rows)

    def data(self):
        return [r.data() for r in self._rows]

    def consume(self):
        pass


class _FakeSession:
    """世代プローブ（SherpaMeta）にだけ件数 count＋指定世代を返す fake（他クエリは 0 件）。"""

    def __init__(self, era, raise_exc=None, count=1):
        self.era, self.raise_exc, self.count = era, raise_exc, count

    def run(self, query, **kw):
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResult([{"c": self.count, "era": self.era}] if "SherpaMeta" in str(query) else [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session

    def close(self):
        pass


def _patch_neo4j_driver(monkeypatch, session):
    import neo4j
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", staticmethod(lambda *a, **kw: _FakeDriver(session)))


def test_run_tool_graph_neighbors_three_graph_states(monkeypatch):
    """世代不一致は例外で終端させず MCP 側と同じ機械可読コードで返す・接続断は別コード（回復可能）・空（未構築）は
    例外にも障害コードにもならない（近傍 0 件）。"""
    from neo4j.exceptions import ServiceUnavailable
    _patch_neo4j_driver(monkeypatch, _FakeSession("old-era"))
    res, docs, cites, cards = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert res == {"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"}
    assert docs == set() and cites == [] and cards == []
    _patch_neo4j_driver(monkeypatch, _FakeSession(None, raise_exc=ServiceUnavailable("down")))
    res, *_ = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert res["neighbors"] == [] and res["error_code"] == "graph_unavailable" and "error" not in res
    _patch_neo4j_driver(monkeypatch, _FakeSession(None, count=0))
    res, *_ = TD.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert res == {"neighbors": [], "coverage": {"complete": True, "limits": [], "omitted": 0},
                   "unresolved": {"available": False, "items": [], "omitted": 0}}


# ===== es_search の mode 経路 =====

def test_run_tool_es_search_mode_routing(monkeypatch):
    """mode: 不正値は hybrid・keyword は vector=False・vector は knn のみ（埋め込み不可なら BM25 へ縮退し理由と mode_used を返す）。"""
    from sherpa import documents

    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {"a.md"})
    h = [{"doc_id": "a.md", "line": 1, "text": "x", "ext": ".md"}]
    calls = []
    knn_result = (h, None)

    def fake_search(world, q, scope_paths=None, k=20, layer=None, vector=True, **kw):
        calls.append(("search", vector))
        return h, None

    def fake_knn(world, q, scope_paths=None, k=20, layer=None, **kw):
        calls.append(("knn", None))
        return knn_result

    monkeypatch.setattr(A.es_index, "search", fake_search)
    monkeypatch.setattr(A.es_index, "search_knn_only", fake_knn)

    def run(mode):
        return TD.run_tool("es_search", {"query": "x", "mode": mode}, "v1", None)[0]

    assert run("bogus")["mode_used"] == "hybrid" and calls[-1] == ("search", True)
    assert run("keyword")["mode_used"] == "keyword" and calls[-1] == ("search", False)
    assert run("vector")["mode_used"] == "vector" and calls[-1] == ("knn", None)
    knn_result = ([], "embedding_not_configured")
    v = run("vector")
    assert v["mode_used"] == "keyword" and v["degrade_reason"] == "embedding_not_configured"
    assert calls[-2:] == [("knn", None), ("search", False)] and v["hits"]

    # 埋め込み未設定の hybrid（既定）は BM25 だけで検索している＝mode_used は keyword・理由を返す
    monkeypatch.setattr(A.es_index, "search", lambda *a, **kw: (h, "embedding_not_configured"))
    v = TD.run_tool("es_search", {"query": "x"}, "v1", None)[0]
    assert v["mode_used"] == "keyword" and v["degrade_reason"] == "embedding_not_configured"

    # vector で ES のクエリ自体が失敗したときは BM25 へ倒さず、失敗をそのまま返す
    calls.clear()
    knn_result = ([], "es_query_failed")
    monkeypatch.setattr(A.es_index, "search", fake_search)
    v = run("vector")
    assert calls == [("knn", None)] and v["hits"] == []
    assert v["degrade_reason"] == "es_query_failed" and v["mode_used"] == "vector"
