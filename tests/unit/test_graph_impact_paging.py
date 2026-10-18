"""`graph_impact` のページ送り（欠けなく・重複なく最後まで取れること）。Neo4j 不要・探索（`world_neo4j.world_impact`）だけを固定の結果に差し替える。

期待する対象 ID の集合は、差し替えた探索の結果から手で決めた（起点自身・秘匿名のファイルに属する先を除く）。
CLI の切り詰めは実際の CLI を通す保証ではなく、保存ログで測った上限（約 40,000 バイト）より 1 ページが小さいことの確認。
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import graph_tools, tool_call_log, tool_dispatch  # noqa: E402
from sherpa.ingest import world_neo4j as WN  # noqa: E402

_READ_GENERATION = WN.read_generation   # フィクスチャが差し替える前の実物
CLI_OUTPUT_LIMIT_BYTES = 40000   # 保存ログ（rollout の custom_tool_call_output）で切られなかった最大 39,577 と切られた最小 40,147 の間
SENSITIVE_ID = "c13"             # 秘匿名のファイルに属するので返さない
N_ITEMS = 250
EXPECTED_IDS = {f"c{i}" for i in range(1, N_ITEMS + 1)} - {SENSITIVE_ID}   # c0（起点）は循環で戻ってくるが数えない


class _Drv:
    def session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


class Env:
    """差し替えた探索の設定（テストごとに書き換える）。"""

    def __init__(self):
        self.n = N_ITEMS
        self.flow_n = 0
        self.docs_n = 0
        self.generation = "gen-1"
        self.info = {"depth_truncated": False}
        self.pad = 0           # 根拠 1 件あたりの余分な文字数
        self.sources = 0       # 辺ごとの根拠（sources）の件数
        self.long_path = {}    # canonical_id → 長いパス
        self.calls = 0
        self.cid_pad = ""      # 識別子の末尾に足す文字列（実グラフの長い識別子の再現）
        self.max_dist = 3


def _edge(i, env):
    e = {"type": "INVOKES", "doc": f"top/d{i % 7}.md", "line": i + 1}
    if env.sources:
        e["via"] = "call"
        e["sources"] = json.dumps([{"via": "call", "doc_id": f"top/s{i}_{k}.cbl", "file": f"top/s{i}_{k}.cbl", "line": k + 1,
                                    "rule": "same_package", "evidence_text": "あ" * env.pad} for k in range(env.sources)],
                                  ensure_ascii=False)
        e["sources_overflow"] = 0
    return e


@pytest.fixture
def env(monkeypatch):
    env = Env()

    def impact(session, cids, world, sp, depth, info=None, detail=False, flow_depth=0, **k):
        env.calls += 1
        info.update(env.info)
        if flow_depth > 0:
            info.setdefault("flow_depth_truncated", False)
        out = []
        for i in range(1, env.n + 1):
            cid = f"c{i}"
            dist = i % env.max_dist + 1
            path = env.long_path.get(cid) or f"top/a{i % 5}/N{i}.cbl"
            tpaths = ["top/p"] * (dist + 1)
            if cid == SENSITIVE_ID:
                tpaths[-1] = "top/credentials.py"
            out.append({"name": f"N{i}", "label": "Module", "category": "x", "status": "active", "analyzer": None,
                        "top_scope": "t", "path": path, "trace": [f"T{j}" for j in range(dist + 1)], "evidence": [],
                        "canonical_id": cid + env.cid_pad,
                        "trace_cids": [cid + env.cid_pad] + [f"m{j}{env.cid_pad}" for j in range(dist - 1)] + ["c0"],
                        "trace_paths": tpaths, "edges": [_edge(i, env) for _ in range(dist)]})
        out.append({"name": "S", "label": "Module", "category": "x", "status": "active", "analyzer": None,
                    "top_scope": "t", "path": "top/s.cbl", "trace": ["S", "X", "S"], "evidence": [], "canonical_id": "c0",
                    "trace_cids": ["c0", "m0", "c0"], "trace_paths": ["top/s.cbl"] * 3,
                    "edges": [_edge(0, env), _edge(0, env)]})            # 循環で起点へ戻る経路
        for i in range(1, env.flow_n + 1 if flow_depth > 0 else 1):
            if i > env.flow_n:
                break
            out.append({"name": f"F{i}", "label": "Module", "category": "x", "status": "active", "analyzer": None,
                        "top_scope": "t", "path": f"top/f/F{i}.cbl", "origin": "flow", "trace": ["F", "D", "S"],
                        "evidence": [], "canonical_id": f"f{i}", "trace_cids": [f"f{i}", "d1", "c0"],
                        "trace_paths": ["top/p"] * 3,
                        "edges": [{"type": "FLOWS_TO", "doc": "top/f.cbl", "line": 3, "via": "read_file"},
                                  {"type": "INVOKES", "doc": "top/d.md", "line": 1}]})
        return out

    def related(session, cids, world, sp, **k):
        return [{"canonical_id": f"doc{i:03d}", "name": f"D{i}", "path": f"docs/d{i:03d}.md", "via": "mention", "line": i,
                 "target": {"canonical_id": "c0", "name": "S"}, "_target_path": "top/s.cbl"}
                for i in range(env.docs_n)]

    monkeypatch.setattr(graph_tools, "_open_driver", lambda: _Drv())
    monkeypatch.setattr(WN, "get_world_entities", lambda *a, **k: [{"canonical_id": "c0", "label": "Module", "name": "S",
                                                                    "path": "top/s.cbl"}])
    monkeypatch.setattr(WN, "world_impact", impact)
    monkeypatch.setattr(WN, "world_related_documents", related)
    monkeypatch.setattr(WN, "read_unresolved", lambda *a, **k: {"available": True, "items": [], "omitted": 0})
    monkeypatch.setattr(WN, "read_plugin_failures", lambda *a, **k: [])
    monkeypatch.setattr(WN, "read_unparsed_sources", lambda *a, **k: None)
    monkeypatch.setattr(WN, "read_generation", lambda *a, **k: env.generation)
    graph_tools._page_tokens.clear()
    yield env
    graph_tools._page_tokens.clear()


def _run(args, scope=None, layer=None, world="w", max_bytes=None):
    return graph_tools.run("graph_impact", {"canonical_id": "c0", **args}, world, scope, layer=layer, max_bytes=max_bytes)


def _walk(args, part="impact", scope=None, layer=None, max_bytes=None, key="impact"):
    """next_offset が null になるまで、前の応答の generation と next_offset を付けて呼び続ける。各ページの応答を返す。"""
    pages, offset, token = [], 0, None
    while True:
        a = {**args, "part": part, "offset": offset}
        if token:
            a["generation"] = token
        res = _run(a, scope=scope, layer=layer, max_bytes=max_bytes)
        assert "error" not in res, res
        pages.append(res)
        token = res["generation"]
        offset = res["transfer"]["next_offset"]
        if offset is None:
            return pages
        assert len(pages) < 500


def _ids(pages, key="impact", field="canonical_id"):
    return [x[field] for p in pages for x in p.get(key, [])]


# ---- 欠けなく・重複なく ----

@pytest.mark.parametrize("args", [{"evidence_limit": 0, "limit": 200}, {"limit": 200}, {"evidence_limit": 0, "limit": 7}, {}])
def test_every_page_together_is_exactly_the_expected_set(env, args):
    pages = _walk(args)
    ids = _ids(pages)
    assert len(ids) == len(set(ids)) and set(ids) == EXPECTED_IDS             # 重複なし・欠落なし（件数でなく集合で）
    assert "c0" not in ids and SENSITIVE_ID not in ids
    for p in pages:
        assert p["count"] == len(EXPECTED_IDS) == p["transfer"]["total"]
        assert graph_tools.json_bytes(p) <= graph_tools.GRAPH_PAGE_BYTES
        assert graph_tools.json_bytes(p) < CLI_OUTPUT_LIMIT_BYTES
    # 範囲は途切れなく続く
    pos = 0
    for p in pages:
        assert p["transfer"]["offset"] == pos and p["transfer"]["returned"] == len(p["impact"])
        pos += p["transfer"]["returned"]
    assert pos == len(EXPECTED_IDS) and pages[-1]["transfer"]["next_offset"] is None


def test_the_order_is_stable_across_calls_and_page_boundaries(env):
    a = _ids(_walk({"evidence_limit": 0}))
    b = _ids(_walk({"evidence_limit": 0, "limit": 11}))
    assert a == b and a == sorted(a, key=lambda c: (int(c[1:]) % 3 + 1, f"N{c[1:]}", f"top/a{int(c[1:]) % 5}/N{c[1:]}.cbl", c))


def test_more_than_200_items_need_several_pages_and_the_first_says_so(env):
    first = _run({"evidence_limit": 0, "limit": 200})
    assert first["transfer"]["total"] == 249 and first["transfer"]["has_more"] is True
    assert 0 < first["transfer"]["returned"] <= 200 and first["transfer"]["next_offset"] == first["transfer"]["returned"]
    assert first["transfer"]["complete"] is False
    assert {"kind": "result_cap", "stage": "impact"} in first["coverage"]["limits"] and first["truncated"] is True


def test_long_evidence_and_japanese_text_make_smaller_pages_but_lose_nothing(env):
    env.sources, env.pad = 4, 400
    pages = _walk({"evidence_limit": 4})
    assert len(pages) > 5 and set(_ids(pages)) == EXPECTED_IDS and len(_ids(pages)) == len(EXPECTED_IDS)
    assert all(graph_tools.json_bytes(p) <= graph_tools.GRAPH_PAGE_BYTES for p in pages)
    light = _walk({"evidence_limit": 0})
    assert len(light) < len(pages)                                              # 根拠 0 件の一覧は 1 ページに多く入る


def test_one_huge_item_is_returned_alone_in_a_lighter_form_not_dropped(env):
    env.long_path = {"c5": "top/" + "長い/" * 4000 + "x.cbl"}                   # 日本語の長いパス（約 6 万バイト）
    pages = _walk({"evidence_limit": 0})
    assert set(_ids(pages)) == EXPECTED_IDS
    huge = [x for p in pages for x in p["impact"] if x["canonical_id"] == "c5"]
    assert len(huge) == 1 and huge[0].get("item_slimmed") in ("route", "clipped")
    assert all(graph_tools.json_bytes(p) <= graph_tools.GRAPH_PAGE_BYTES for p in pages)


def test_tiny_budget_still_returns_one_item_per_page(env):
    pages = _walk({"evidence_limit": 0, "limit": 5}, max_bytes=1000)           # 予算の 8 割・最小 512 バイト
    assert set(_ids(pages)) == EXPECTED_IDS and all(len(p["impact"]) >= 1 for p in pages)


# ---- バイト予算の直前・一致・直後（純関数） ----

def test_take_page_at_the_byte_budget_boundary():
    items = [{"canonical_id": f"c{i}", "pad": "x" * 20} for i in range(10)]
    size = graph_tools.json_bytes(items[0]) + 1
    assert len(graph_tools._take_page(items, 0, 10, size * 4 - 1)) == 3         # 直前
    assert len(graph_tools._take_page(items, 0, 10, size * 4)) == 4             # 一致
    assert len(graph_tools._take_page(items, 0, 10, size * 4 + 1)) == 4         # 直後
    assert [x["canonical_id"] for x in graph_tools._take_page(items, 6, 10, size * 9)] == ["c6", "c7", "c8", "c9"]
    assert len(graph_tools._take_page(items, 0, 2, size * 9)) == 2             # 件数の上限が先に効く
    assert len(graph_tools._take_page(items, 0, 10, 1)) == 1                   # 1 件だけでも返す


def test_page_bytes_follow_the_server_budget():
    assert graph_tools.page_bytes_for(None) == graph_tools.GRAPH_PAGE_BYTES
    assert graph_tools.page_bytes_for(65536) == graph_tools.GRAPH_PAGE_BYTES < 65536
    assert graph_tools.page_bytes_for(10000) == 8000
    assert graph_tools.GRAPH_PAGE_BYTES < CLI_OUTPUT_LIMIT_BYTES


# ---- 関連資料・流れの候補 ----

def test_related_documents_are_paged_to_the_end_and_say_which_part_is_paged(env):
    env.docs_n = 130
    first = _run({"evidence_limit": 0, "limit": 200})
    assert first["transfer"]["part"] == "impact" and 0 < len(first["related_documents"]) <= graph_tools.RELATED_DOCS_LIMIT
    assert first["transfer"]["other_parts"]["related_documents"]["total"] == 130
    assert first["related_documents_omitted"] == 130 - len(first["related_documents"])
    assert {"kind": "result_cap", "stage": "docs"} in first["coverage"]["limits"]
    pages = _walk({"evidence_limit": 0}, part="related_documents")
    assert all(p["transfer"]["part"] == "related_documents" and "impact" not in p for p in pages)
    got = [d["canonical_id"] for p in pages for d in p["related_documents"]]
    assert got == [f"doc{i:03d}" for i in range(130)]                           # 手で決めた順（パス → 対象）で欠けも重複もない
    assert pages[-1]["transfer"]["complete"] is True
    assert all(lim.get("stage") != "docs" for lim in pages[-1]["coverage"]["limits"])


def test_flow_candidates_are_paged_to_the_end(env):
    env.flow_n = 90
    first = _run({"evidence_limit": 0})
    assert first["transfer"]["other_parts"]["flow"]["total"] == 90 or len(first["flow_candidates"]) == 90
    pages = _walk({"evidence_limit": 0}, part="flow")
    got = [c["canonical_id"] for p in pages for c in p["flow_candidates"]]
    assert sorted(got, key=lambda c: int(c[1:])) == [f"f{i}" for i in range(1, 91)] and len(got) == len(set(got))
    assert pages[-1]["transfer"]["complete"] is True and pages[0]["flow_total"] == 90


def test_flow_part_needs_flow_depth_and_code_layer_has_no_documents(env):
    assert _run({"part": "flow", "flow_depth": 0})["error"] == "graph_invalid_args"
    env.docs_n = 5
    res = _run({"evidence_limit": 0}, layer="code")
    assert "related_documents" not in res


# ---- 探索の完了と転送の完了を分ける ----

def test_exploration_and_transfer_are_reported_apart(env):
    env.info = {"depth_truncated": True}                                      # 深さの上限で止まった（先が残る）
    pages = _walk({"evidence_limit": 0})
    for p in pages:
        assert p["exploration"]["complete"] is False and {"kind": "depth", "stage": "impact"} in p["exploration"]["limits"]
        assert all(lim["kind"] != "result_cap" for lim in p["exploration"]["limits"])
    last = pages[-1]
    assert last["transfer"]["complete"] is True and last["transfer"]["has_more"] is False
    assert last["coverage"]["complete"] is False and last["coverage"]["depth"]["truncated"] is True   # 深さの印は最後まで残る
    assert pages[0]["transfer"]["complete"] is False


def test_the_transfer_mark_disappears_on_the_last_page_when_exploration_was_complete(env):
    pages = _walk({"evidence_limit": 0})
    assert len(pages) > 1
    for p in pages[:-1]:
        assert p["exploration"]["complete"] is True and {"kind": "result_cap", "stage": "impact"} in p["coverage"]["limits"]
    last = pages[-1]
    assert last["exploration"]["complete"] is True and last["transfer"]["complete"] is True
    assert last["coverage"] ["complete"] is True and last["coverage"]["limits"] == [] and "truncated" not in last


@pytest.mark.parametrize("kind", ["timeout", "row_cap"])
def test_a_time_or_row_cap_stop_is_an_exploration_gap_even_on_the_last_page(env, kind):
    env.info = {"depth_truncated": None, "depth_check_limit": kind}
    last = _walk({"evidence_limit": 0})[-1]
    assert last["transfer"]["complete"] is True and last["exploration"]["complete"] is False
    assert {"kind": kind, "stage": "impact"} in last["exploration"]["limits"] and last["coverage"]["omitted"] is None


# ---- 同じページ・無効な offset ----

def test_fetching_the_same_page_again_returns_the_same_content(env):
    a = _run({"evidence_limit": 0, "offset": 0})
    graph_tools._page_tokens.clear()
    b = _run({"evidence_limit": 0, "offset": 0})
    assert a == b
    c = _run({"evidence_limit": 0, "offset": a["transfer"]["next_offset"], "generation": a["generation"]})
    d = _run({"evidence_limit": 0, "offset": a["transfer"]["next_offset"], "generation": a["generation"]})
    assert c == d and c["impact"][0]["canonical_id"] not in {x["canonical_id"] for x in a["impact"]}


@pytest.mark.parametrize("offset", [-1, "abc", 1.5, True, [1]])
def test_invalid_offsets_are_input_errors(env, offset):
    res = _run({"offset": offset})
    assert res["error"] == "graph_invalid_args" and "offset" in res["message"]


def test_offset_past_the_total_is_an_error_and_at_the_total_is_an_empty_last_page(env):
    total = len(EXPECTED_IDS)
    graph_tools._remember_generation(graph_tools._scope_mark("w", None, False, "c0", 5, 3), "gen-1")
    bad = _run({"offset": total + 1, "evidence_limit": 0})
    assert bad["error"] == "graph_invalid_args" and bad["total"] == total
    end = _run({"offset": total, "evidence_limit": 0})
    assert end["impact"] == [] and end["transfer"]["next_offset"] is None and end["transfer"]["complete"] is True


# ---- 世代・範囲・期限 ----

def test_a_changed_generation_is_reported_not_merged(env):
    first = _run({"evidence_limit": 0})
    env.generation = "gen-2"
    for extra in ({"generation": first["generation"]}, {}):                  # 印を付けても、記憶に頼っても
        res = _run({"evidence_limit": 0, "offset": first["transfer"]["next_offset"], **extra})
        assert res["error"] == "graph_generation_mismatch" and "impact" not in res
    again = _run({"evidence_limit": 0})                                      # 最初から取り直せば新しい世代で取れる
    assert again["generation"].startswith("gen-2.") and again["transfer"]["offset"] == 0


def test_generation_that_changes_during_the_read_is_not_returned(env, monkeypatch):
    seq = iter(["gen-1", "gen-2", "gen-2"])
    monkeypatch.setattr(WN, "read_generation", lambda *a, **k: next(seq))
    assert _run({"evidence_limit": 0})["error"] == "graph_generation_mismatch"


def test_a_continuation_with_no_record_or_after_expiry_is_distinct_from_an_empty_result(env, monkeypatch):
    res = _run({"evidence_limit": 0, "offset": 50})
    assert res["error"] == "graph_page_expired" and "impact" not in res
    first = _run({"evidence_limit": 0})
    now = [1000.0]
    monkeypatch.setattr(graph_tools.time, "monotonic", lambda: now[0])
    graph_tools._remember_generation(graph_tools._scope_mark("w", None, False, "c0", 5, 3), "gen-1")
    assert "error" not in _run({"evidence_limit": 0, "offset": first["transfer"]["next_offset"]})
    now[0] += graph_tools.PAGE_TOKEN_TTL_S + 1
    assert _run({"evidence_limit": 0, "offset": first["transfer"]["next_offset"]})["error"] == "graph_page_expired"


def test_the_token_is_bound_to_scope_world_start_and_depth(env):
    token = _run({"evidence_limit": 0}, scope=["top/a1"])["generation"]
    off = {"evidence_limit": 0, "offset": 1}
    assert _run(off | {"generation": token}, scope=["top/a1"])["transfer"]["offset"] == 1
    for other in (dict(scope=["top/a2"]), dict(scope=None), dict(world="other"), dict(layer="code")):
        assert _run(off | {"generation": token}, **other)["error"] == "graph_page_mismatch"
    assert _run(off | {"generation": token, "depth": 4}, scope=["top/a1"])["error"] == "graph_page_mismatch"
    assert _run(off | {"generation": "../../etc/passwd"}, scope=["top/a1"])["error"] == "graph_page_mismatch"
    # 記憶に頼る続きも、別の範囲からは受け付けない
    assert _run(off, scope=["top/zzz"])["error"] == "graph_page_expired"


def test_the_generation_memory_is_bounded(env):
    for i in range(graph_tools.PAGE_TOKEN_MAX + 20):
        graph_tools._remember_generation(f"m{i}", "g")
    assert len(graph_tools._page_tokens) == graph_tools.PAGE_TOKEN_MAX and "m0" not in graph_tools._page_tokens


# ---- MCP の応答を通して ----

def _mcp(name, args):
    from sherpa import mcp_server as M
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    text = resp["result"]["content"][0]["text"]
    return json.loads(text), resp["result"]["isError"], len(text.encode("utf-8"))


def test_pages_survive_the_mcp_response_and_stay_under_the_cli_limit(env, monkeypatch):
    from sherpa import mcp_server as M
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "65536")   # Codex 経路のバイト予算（usage._CODEX_MCP_TOOL_BUDGET_CEILING_BYTES）
    monkeypatch.setattr(M, "_world", lambda: "w")
    monkeypatch.setattr(M, "_scope", lambda: None)
    M._seen_tool_calls.clear()
    env.sources, env.pad = 2, 120
    env.docs_n = 45
    offset, token, got, docs, sizes = 0, None, [], [], []
    while offset is not None:
        args = {"canonical_id": "c0", "offset": offset, "evidence_limit": 2}
        if token:
            args["generation"] = token
        body, is_error, size = _mcp("graph_impact", args)
        assert not is_error, body
        sizes.append(size)
        token, offset = body["generation"], body["transfer"]["next_offset"]
        got += [x["canonical_id"] for x in body["impact"]]
        if not docs:
            docs = body["related_documents"]
    assert set(got) == EXPECTED_IDS and len(got) == len(set(got))
    assert max(sizes) < CLI_OUTPUT_LIMIT_BYTES and max(sizes) <= graph_tools.GRAPH_PAGE_BYTES
    rest, offset, token = [], 0, None
    while offset is not None:
        args = {"canonical_id": "c0", "part": "related_documents", "offset": offset}
        if token:
            args["generation"] = token
        body, is_error, _ = _mcp("graph_impact", args)
        assert not is_error
        rest += [d["canonical_id"] for d in body["related_documents"]]
        token, offset = body["generation"], body["transfer"]["next_offset"]
    assert rest == [f"doc{i:03d}" for i in range(45)]
    assert [d["canonical_id"] for d in docs] == rest[:len(docs)]


def test_a_repeated_call_is_not_rejected_and_a_different_offset_is_a_different_call(env, monkeypatch):
    from sherpa import mcp_server as M
    monkeypatch.setattr(M, "_world", lambda: "w")
    monkeypatch.setattr(M, "_scope", lambda: None)
    M._seen_tool_calls.clear()
    a, err_a, _ = _mcp("graph_impact", {"canonical_id": "c0", "limit": 200, "evidence_limit": 0})
    b, err_b, _ = _mcp("graph_impact", {"canonical_id": "c0", "limit": 200, "evidence_limit": 0})
    assert not err_a and not err_b and a == b and a["impact"]
    c, err_c, _ = _mcp("graph_impact", {"canonical_id": "c0", "limit": 200, "evidence_limit": 0,
                                       "offset": a["transfer"]["next_offset"], "generation": a["generation"]})
    assert not err_c and c["impact"][0]["canonical_id"] != a["impact"][0]["canonical_id"]
    M._seen_tool_calls.clear()


def test_errors_are_tool_errors_in_the_mcp_response(env, monkeypatch):
    from sherpa import mcp_server as M
    monkeypatch.setattr(M, "_world", lambda: "w")
    monkeypatch.setattr(M, "_scope", lambda: None)
    M._seen_tool_calls.clear()
    body, is_error, _ = _mcp("graph_impact", {"canonical_id": "c0", "offset": 40})
    assert is_error and body["error"] == "graph_page_expired"
    M._seen_tool_calls.clear()


# ---- 最後の防衛線（fit_to_bytes）でも続きの位置がずれない ----

def test_fit_to_bytes_keeps_next_offset_consistent_with_what_it_kept(env):
    first = _run({"evidence_limit": 0, "limit": 40})
    fitted, clipped = graph_tools.fit_to_bytes(first, graph_tools.json_bytes(first) // 2)
    assert clipped
    kept = len(fitted["impact"])
    assert 0 < kept < len(first["impact"])
    assert fitted["transfer"]["returned"] == kept and fitted["transfer"]["next_offset"] == kept
    nxt = _run({"evidence_limit": 0, "limit": 200, "offset": kept, "generation": first["generation"]})
    assert [x["canonical_id"] for x in fitted["impact"]] + [x["canonical_id"] for x in nxt["impact"]][:5] \
        == _ids([first])[:kept] + _ids([nxt])[:5]
    assert nxt["impact"][0]["canonical_id"] == _ids([first])[kept]


# ---- 呼び出しの記録と B の照合 ----

def _views(pages, tool="graph_impact"):
    out = []
    for i, p in enumerate(pages):
        v = tool_call_log.graph_view(tool, p)
        out.append({"tool": tool, "call_id": f"1:{i}", **v})
    return out


def test_graph_view_records_the_received_range_and_the_server_side_total_apart(env):
    first = _run({"evidence_limit": 0})
    v = tool_call_log.graph_view("graph_impact", first)
    assert v["page"] == {"part": "impact", "offset": 0, "returned": len(first["impact"]), "total": len(EXPECTED_IDS),
                         "next_offset": len(first["impact"]), "has_more": True}
    assert len(v["rows"]) == len(first["impact"]) < v["count"] == len(EXPECTED_IDS)
    assert v["generation"] == first["generation"] and v["exploration_complete"] is True and v["complete"] is False


def test_merge_impact_pages_makes_one_result_with_the_union_of_received_rows(env):
    pages = _walk({"evidence_limit": 0})
    merged = tool_call_log.merge_impact_pages(_views(pages))
    assert len(merged) == 1
    m = merged[0]
    assert {r["cid"] for r in m["rows"]} == EXPECTED_IDS and len(m["rows"]) == len(EXPECTED_IDS)
    assert m["count"] == m["total"] == m["received"] == len(EXPECTED_IDS)
    assert m["transfer_complete"] is True and m["complete"] is True and m["limits"] == []
    assert m["pages_received"] == len(pages)


def test_merge_does_not_call_unreceived_rows_read(env):
    pages = _walk({"evidence_limit": 0})
    assert len(pages) >= 3
    skipped = _views(pages[:1] + pages[2:])                                     # 2 ページ目を取っていない
    m = tool_call_log.merge_impact_pages(skipped)[0]
    got = {r["cid"] for r in m["rows"]}
    assert got == set(_ids(pages[:1] + pages[2:])) and got < EXPECTED_IDS
    assert m["count"] == len(EXPECTED_IDS) and m["received"] < m["count"]
    assert m["transfer_complete"] is False and m["complete"] is False
    assert {"kind": "result_cap", "stage": "impact"} in m["limits"]
    head_only = tool_call_log.merge_impact_pages(_views(pages[:1]))[0]
    assert head_only["complete"] is False and head_only["received"] == len(pages[0]["impact"])


def test_merge_keeps_an_exploration_gap_after_the_last_page_and_drops_a_superseded_generation(env):
    env.info = {"depth_truncated": True}
    pages = _walk({"evidence_limit": 0})
    m = tool_call_log.merge_impact_pages(_views(pages))[0]
    assert m["transfer_complete"] is True and m["complete"] is False and m["depth_truncated"] is True
    old = _views(pages[:2])
    env.generation = "gen-2"
    env.info = {"depth_truncated": False}
    new_pages = _walk({"evidence_limit": 0})
    merged = tool_call_log.merge_impact_pages(old + [{**v, "call_id": f"2:{i}"} for i, v in enumerate(_views(new_pages))])
    assert len(merged) == 1 and merged[0]["generation"] == new_pages[0]["generation"]
    assert {r["cid"] for r in merged[0]["rows"]} == EXPECTED_IDS and merged[0]["complete"] is True
    assert merged[0]["generation_changes"] == 1


def test_merge_passes_legacy_results_errors_and_other_parts_through(env):
    env.docs_n = 30
    docs_page = _views([_run({"evidence_limit": 0, "part": "related_documents"})])[0]
    legacy = {"tool": "graph_impact", "call_id": "9:1", "complete": True, "rows": [{"name": "A", "path": "p"}], "count": 1,
              "start": {"cid": "x"}}
    err = {"tool": "graph_impact", "call_id": "9:2", "error": "graph_generation_mismatch", "complete": False}
    out = tool_call_log.merge_impact_pages([legacy, err, docs_page])
    assert out == [legacy, err]                                                 # 関連資料のページは影響先の集合に入らない


def test_the_ledger_target_set_comes_from_all_pages_and_the_rules_do_not_change(env):
    from sherpa.providers.codex import plan_ledger as PL
    pages = _walk({"evidence_limit": 0})

    class Merged:
        graph = _views(pages)

    class St:
        call_log = Merged()
        _call_log_dir = None
        turn_uid = "t"

    results = PL.graph_impact_results(St())
    assert len(results) == 1
    targets, calls = PL.required_targets(results)
    assert {t["id"] for t in targets.values()} == {f"top/a{int(c[1:]) % 5}/N{c[1:]}.cbl" for c in EXPECTED_IDS}
    assert len(calls) == 1 and PL.graph_incomplete(St()) is False
    answer = "影響先:\n" + "\n".join(f"- {t['path']}" for t in list(targets.values())[:-1])
    check = PL.check_targets(results, "この項目を変えたときの影響先を全て挙げて", answer)
    assert check["status"] == "missing" and check["missing_count"] == 1 and check["coverage_complete"] is True
    Merged.graph = _views(pages[:1])                                            # 先頭だけ受け取った状態
    assert PL.graph_incomplete(St()) is True
    partial = PL.check_targets(PL.graph_impact_results(St()), "影響先を全て挙げて", answer)
    assert partial["coverage_complete"] is False
    assert partial["required_count"] == len(pages[0]["impact"])                # 受け取っていない対象を必須にも読んだことにもしない
    assert PL._graph_stats(St())["impact_calls"] == 1


@pytest.mark.parametrize("rows,expected", [
    ([{"g": "abc123", "nc": 5, "ec": 7, "era": "e1"}], "abc123"),
    ([{"g": None, "nc": 5, "ec": 7, "era": "e1"}], "legacy-e1-5-7"),      # 印のない旧グラフは件数から作った印
    ([], None),
])
def test_read_generation_uses_the_load_stamp(monkeypatch, rows, expected):
    monkeypatch.setattr(WN, "_run_read_capped", lambda *a, **k: rows)
    assert _READ_GENERATION(object(), "w") == expected


# ---- 一覧と根拠の分離・上限の厳密さ ----

def test_evidence_limit_zero_gives_a_light_list_and_a_positive_one_keeps_route_and_evidence(env):
    light = _run({"evidence_limit": 0})
    item = light["impact"][0]
    assert set(item) == {"canonical_id", "kind", "name", "path", "distance", "via"}
    assert item["via"] == ["INVOKES"] * item["distance"] and light["list_form"] == "light"
    full = _run({"evidence_limit": 1})
    assert {"trace", "route", "evidence"} <= set(full["impact"][0]) and "list_form" not in full
    assert len(light["impact"]) > len(full["impact"])
    row = tool_call_log.graph_view("graph_impact", light)["rows"][0]
    assert {"cid", "kind", "distance", "name", "path"} <= set(row)             # B の照合・影響一覧が使う欄は残る


@pytest.mark.parametrize("max_bytes", [3000, 4000, 7777, 12345, 15000, 40000, 65536])
@pytest.mark.parametrize("evidence_limit", [0, 3])
def test_every_page_is_within_the_page_bytes_exactly(env, max_bytes, evidence_limit):
    env.docs_n, env.flow_n = 30, 20
    budget = graph_tools.page_bytes_for(max_bytes)
    for part in ("impact", "flow", "related_documents"):
        for p in _walk({"evidence_limit": evidence_limit}, part=part, max_bytes=max_bytes):
            assert graph_tools.json_bytes(p) <= budget, (part, graph_tools.json_bytes(p), budget)


def test_a_real_sized_13_step_result_fits_in_a_few_light_pages(env):
    """実グラフに近い形（影響先 112・関連資料 42・最大 12 段・長い識別子と日本語のパス）。"""
    env.n, env.docs_n, env.max_dist = 112, 42, 12
    env.cid_pad = "#" + "x" * 90
    env.long_path = {f"c{i}": "業務システム/" + "サブ/" * 6 + f"プログラム{i}.cbl" for i in range(1, 113)}
    pages = _walk({"evidence_limit": 0, "depth": 10})
    docs = _walk({"evidence_limit": 0, "depth": 10}, part="related_documents")
    assert len(pages) < 10 and len(pages) + len(docs) - 1 < 10
    got = {x["canonical_id"] for p in pages for x in p["impact"]}
    assert len(got) == 111 and all(graph_tools.json_bytes(p) <= graph_tools.GRAPH_PAGE_BYTES for p in pages + docs)
