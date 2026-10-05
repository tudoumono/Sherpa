"""`graph_resolve`・`graph_impact` の Neo4j 不要の契約（入力の誤り・障害の形・返却バイトの収まり・出し分け）。

内容の確かめ（同名の別ノード・言及・上限・旧世代）は実 Neo4j の `tests/integration/test_graph_tools_real.py`。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import graph_tools, simple_chat, tool_dispatch  # noqa: E402


def _run(name, args, layer=None):
    return tool_dispatch.run_tool(name, args, "v1", None, layer=layer)[0]


@pytest.mark.parametrize("name,args", [
    ("graph_resolve", {}),
    ("graph_resolve", {"name": "A", "kind": "Class"}),
    ("graph_resolve", {"name": "A", "limit": "many"}),
    ("graph_impact", {}),
    ("graph_impact", {"canonical_id": "c", "depth": "deep"}),
])
def test_invalid_arguments_are_a_structured_error_without_touching_neo4j(monkeypatch, name, args):
    monkeypatch.setattr(graph_tools, "_open_driver", lambda: (_ for _ in ()).throw(AssertionError("Neo4j に触れた")))
    res = _run(name, args)
    assert res["error"] == "graph_invalid_args" and res["message"]


@pytest.mark.parametrize("exc_name,code", [("ServiceUnavailable", "graph_unavailable"),
                                           ("ConfigurationError", "graph_internal_error")])
def test_neo4j_failure_has_the_same_shape_as_graph_neighbors(monkeypatch, exc_name, code):
    from neo4j import exceptions

    def boom():
        raise getattr(exceptions, exc_name)("down")

    monkeypatch.setattr(graph_tools, "_open_driver", boom)
    for name, args, field in (("graph_resolve", {"name": "A"}, "candidates"),
                              ("graph_impact", {"canonical_id": "c"}, "impact")):
        res = _run(name, args)
        assert res[field] == [] and res["error_code"] == code
        if code == "graph_unavailable":
            assert res["coverage"] == {"complete": False, "limits": [{"kind": "graph_unavailable"}], "omitted": None}
        else:
            assert "coverage" not in res


def test_docs_layer_is_rejected_before_touching_neo4j(monkeypatch):
    monkeypatch.setattr(graph_tools, "_open_driver", lambda: (_ for _ in ()).throw(AssertionError("Neo4j に触れた")))
    for name in graph_tools.TOOL_NAMES:
        assert _run(name, {"name": "A", "canonical_id": "c"}, layer="docs") == {"error": graph_tools.LAYER_REJECT_MESSAGE}


def test_fit_to_bytes_drops_tail_and_records_result_cap():
    big = {"start": {"canonical_id": "c"}, "count": 30, "impact": [{"canonical_id": f"c{i}", "pad": "x" * 80} for i in range(30)],
           "coverage": {"complete": False, "limits": [{"kind": "depth", "stage": "impact"}], "omitted": None,
                        "depth": {"requested": 5, "truncated": True}}}
    out, clipped = graph_tools.fit_to_bytes(big, 1500)
    assert clipped and graph_tools.json_bytes(out) <= 1500 and out["truncated"] is True
    assert out["coverage"]["omitted"] is None and out["coverage"]["depth"] == big["coverage"]["depth"]
    assert [lim["kind"] for lim in out["coverage"]["limits"]] == ["depth", "result_cap"]
    assert graph_tools.fit_to_bytes(big, 10) is None
    assert graph_tools.fit_to_bytes({"error": "x"}, 10) is None


def test_fit_to_bytes_drops_related_documents_before_impact():
    docs = [{"canonical_id": f"d{i}", "path": f"docs/{i}.md", "pad": "y" * 80} for i in range(20)]
    base = {"start": {"canonical_id": "c"}, "count": 3, "impact": [{"canonical_id": f"c{i}"} for i in range(3)],
            "related_documents": docs, "coverage": {"complete": True, "limits": [], "omitted": 0,
                                                    "depth": {"requested": 5, "truncated": False}}}
    out, clipped = graph_tools.fit_to_bytes(base, 1200)
    assert clipped and graph_tools.json_bytes(out) <= 1200 and len(out["impact"]) == 3  # 影響先は残す
    assert 0 < len(out["related_documents"]) < 20
    assert out["related_documents_omitted"] == 20 - len(out["related_documents"])
    assert out["coverage"]["limits"] == [{"kind": "result_cap", "stage": "docs"}] and out["truncated"] is True
    tiny, _ = graph_tools.fit_to_bytes(base, 450)
    assert tiny["related_documents"] == [] and tiny["related_documents_omitted"] == 20 and len(tiny["impact"]) == 3


def test_simple_chat_does_not_offer_the_impact_tools():
    names = {t["function"]["name"] for t in simple_chat._build_tools({"fulltext": True, "graph": True})}
    assert "graph_neighbors" in names and not (set(graph_tools.TOOL_NAMES) & names)


def test_result_summary_survives_conversation_save_and_restore():
    """思考ノードの補足（結果の要約）は、会話の保存の上限（200 文字）で切られず、保存した trace に残る。"""
    import json
    import time

    from sherpa import chat_service, store
    from sherpa.providers.codex import mcp as codex_mcp
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    rows = [{"canonical_id": f"module:v1:{'d' * 30}/{i}.java#C{i}", "path": f"{'d' * 30}/{i}.java"} for i in range(5)]
    data = {"candidates": rows, "count": 5, "coverage": {"complete": False, "limits": [{"kind": "result_cap"}], "omitted": None}}
    item = {"result": {"content": [{"type": "text", "text": json.dumps(data)}]}}
    summary = codex_mcp._graph_tool_summary("graph_resolve", item)
    assert len(summary) <= codex_mcp.GRAPH_SUMMARY_MAX_CHARS == chat_service._MAX_TRACE_DETAIL_CHARS
    node = {"id": "n1", "kind": "tool", "label": "影響調査の起点を探す", "detail": summary, "status": "done",
            "parent_id": None, "v": 2}
    trace = chat_service._cap_trace_v2({"n1": node})
    conv = store.create_conversation(user_id=f"unit-s6-{int(time.time() * 1000) % 10**8}", world="v1", title="t")
    saved = store.add_message(conv["id"], "assistant", content="x", trace=trace)
    assert saved["trace"][0]["detail"] == summary and "未完了" in summary


def test_graph_neighbors_edge_view_passes_evidence_but_only_for_verified_documents(monkeypatch):
    """graph_neighbors の辺に via・line・rule・sources を通す。根拠の資料が実在しない・秘匿名なら根拠から除き、件数は足さない。"""
    from sherpa.parts.read import tools
    monkeypatch.setattr(tools, "verify_doc_exists", lambda d, w, sp=None: d != "gone.c")
    card = {"evidence": {"edges": [{
        "type": "INVOKES", "from": "A", "to": "B", "doc": "a.c", "line": 4, "via": "call", "rule": "same_dir",
        "sources": [{"via": "call", "doc_id": "a.c", "file": "a.c", "line": 4, "rule": "same_dir"},
                    {"via": "call", "doc_id": "a.c", "file": "a.c", "line": 5,
                     "from_def": {"file": "credentials.c", "key": "k"}},
                    {"via": "call", "doc_id": "gone.c", "file": "gone.c", "line": 1},
                    {"via": "call", "doc_id": "credentials.c", "file": "credentials.c", "line": 2}],
        "sources_overflow_count": 7}]}, "_verified_doc_ids": ["a.c"]}
    (e,) = tools._card_edges_view(card, "v1", None)
    assert (e["via"], e["line"], e["rule"], e["doc"]) == ("call", 4, "same_dir", "a.c")
    assert e["sources"] == [{"via": "call", "doc_id": "a.c", "file": "a.c", "line": 4, "rule": "same_dir"}]
    assert e["sources_overflow_count"] == 7


def test_start_lookup_overload_is_an_unfinished_coverage_not_an_internal_error(monkeypatch):
    from sherpa.ingest import world_neo4j as WN

    class _Drv:
        def session(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

    def boom(*a, **k):
        raise WN.GraphQueryOverloadError("timeout", world="v1")

    monkeypatch.setattr(graph_tools, "_open_driver", lambda: _Drv())
    monkeypatch.setattr(WN, "get_world_entities", boom)
    res = _run("graph_impact", {"canonical_id": "c"})
    assert res["impact"] == [] and res["count"] is None and "error_code" not in res and "unresolved" not in res
    assert res["coverage"] == {"complete": False, "limits": [{"kind": "timeout", "stage": "anchor"}], "omitted": None}
    assert res["truncated"] is True


def test_code_layer_drops_non_source_paths_from_route_evidence_and_unresolved():
    srcs = [{"doc_id": "a/spec.md", "file": "a/spec.md", "line": 1},
            {"doc_id": "a/B.java", "file": "a/B.java", "line": 2, "from_def": {"file": "a/spec.md", "key": "k"}}]
    assert [s["doc_id"] for s in graph_tools._safe_sources(srcs, None, False)] == ["a/spec.md", "a/B.java"]
    assert graph_tools._safe_sources(srcs, None, True) == []  # 資料を指す根拠・資料の定義ファイルを持つ根拠は根拠ごと除く
    assert graph_tools._route_doc("a/spec.md", None, True) is None and graph_tools._route_doc("a/B.java", None, True)


def test_source_view_is_a_whitelist_and_drops_the_whole_source_when_any_document_fails():
    ok = lambda d: bool(d) and "secret" not in d  # noqa: E731
    src = {"via": "call", "doc_id": "a/B.java", "file": "a/B.java", "line": 2, "rule": "r", "locator": "L2",
           "evidence_text": "x", "private": "leak", "from_def": {"file": "a/C.java", "key": "k", "extra": 1}}
    assert graph_tools.source_view(src, ok) == {
        "via": "call", "doc_id": "a/B.java", "file": "a/B.java", "line": 2, "rule": "r", "locator": "L2",
        "evidence_text": "x", "from_def": {"file": "a/C.java", "key": "k"}}
    assert graph_tools.source_view({**src, "from_def": {"file": "a/secret.c", "key": "k"}}, ok) is None
    assert graph_tools.source_view({**src, "doc_id": "a/secret.c"}, ok) is None
