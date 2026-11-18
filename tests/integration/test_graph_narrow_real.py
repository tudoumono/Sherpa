"""`graph_neighbors` の絞り込み（起点が多すぎるときは、たどらずに内訳を返す）を実 Neo4j で確かめる（要 Neo4j・`make up`）。

架空の資料フォルダに、同じ名前 `Helper` のノードを上限（既定 50）より多く置く。
受入: 広い名前ではたどる問い合わせを流さず件数の内訳を返す／`path_prefix`・`kind` で絞ればたどる／絞り込みは利用者の範囲を広げない／
狭い名前は今までどおり。
"""
from __future__ import annotations

import pytest

from sherpa import lens_service, tool_dispatch
from sherpa.ingest import world_neo4j
from _world_registry import register_test_world
from _world_setup import TEST_WORLD_ID

WORLD = TEST_WORLD_ID.replace("-", "_") + "_narrow"   # 使い捨て（テスト後に削除）・レーンごとに別 ID
N_A, N_B, N_COPY = 35, 30, 20                          # a/ の Module・b/ の Module・a/ の Copybook（全て名前は Helper）


def _cid(label, path, name):
    return f"{label.lower()}:{WORLD}:{path}#{name}"


@pytest.fixture(scope="module")
def loaded():
    from neo4j import GraphDatabase
    nodes, edges = [], []

    def node(label, path, name):
        n = {"cid": _cid(label, path, name), "label": label, "name": name, "top_scope": path.split("/")[0],
             "path": path, "status": "active"}
        nodes.append(n)
        return n

    def calls(src, dst):
        edges.append({"type": "INVOKES", "src": src["cid"], "dst": dst["cid"], "doc": None, "line": 1, "status": "active"})

    for i in range(N_A):
        calls(node("Module", f"a/m{i}/Caller.java", f"CallerA{i}"), node("Module", f"a/m{i}/Helper.java", "Helper"))
    for i in range(N_B):
        calls(node("Module", f"b/m{i}/Caller.java", f"CallerB{i}"), node("Module", f"b/m{i}/Helper.java", "Helper"))
    for i in range(N_COPY):
        node("Copybook", f"a/copy{i}/Helper.cpy", "Helper")
    calls(node("Module", "a/solo/Caller.java", "SoloCaller"), node("Module", "a/solo/Solo.java", "Solo"))
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)
    drv = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        yield drv
    finally:
        with drv.session() as s:
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD)
        drv.close()


@pytest.fixture
def traversals(monkeypatch):
    """近傍をたどる問い合わせ（`neo4j_related`）を呼んだ起点の数を記録する（本物へそのまま委譲する）。"""
    calls: list[int] = []
    real = lens_service.neo4j_related

    def spy(session, anchors, *a, **k):
        calls.append(len(anchors))
        return real(session, anchors, *a, **k)

    monkeypatch.setattr(lens_service, "neo4j_related", spy)
    return calls


def _neighbors(args, scope=None, graph_only=True):
    return tool_dispatch.run_tool("graph_neighbors", args, WORLD, scope, graph_only=graph_only)[0]


@pytest.mark.parametrize("graph_only", [True, False])
def test_broad_name_is_not_traversed_and_returns_breakdown(loaded, traversals, graph_only):
    name = "Helper" if graph_only else "Helper を呼ぶ処理"
    res = _neighbors({"name": name}, graph_only=graph_only)
    assert res["neighbors"] == [] and traversals == []
    tb = res["too_broad"]
    assert tb["anchors"] == N_A + N_B + N_COPY and tb["limit"] == 50
    assert tb["by_folder"] == {"a": N_A + N_COPY, "b": N_B}
    assert tb["by_kind"] == {"Module": N_A + N_B, "Copybook": N_COPY}
    assert "path_prefix" in tb["message"] and "kind" in tb["message"]
    assert res["coverage"]["complete"] is False
    assert {"kind": "narrow_required", "stage": "anchor"} in res["coverage"]["limits"]


@pytest.mark.parametrize("graph_only", [True, False])
def test_narrowing_by_path_prefix_or_kind_traverses(loaded, traversals, graph_only):
    name = "Helper" if graph_only else "Helper を呼ぶ処理"
    by_folder = _neighbors({"name": name, "path_prefix": "b"}, graph_only=graph_only)
    assert "too_broad" not in by_folder and traversals == [N_B]
    assert {n["name"] for n in by_folder["neighbors"]} == {f"CallerB{i}" for i in range(N_B)}
    by_kind = _neighbors({"name": name, "kind": "Module", "path_prefix": "a"}, graph_only=graph_only)
    assert "too_broad" not in by_kind and traversals == [N_B, N_A]
    assert all(n["name"].startswith("CallerA") for n in by_kind["neighbors"])
    assert by_kind["count"] == N_A and by_kind["truncated"] is True   # 近傍の件数の上限（30）で切れる＝起点の上限とは別


def test_narrowing_still_too_broad_reports_the_narrowed_breakdown(loaded, traversals):
    res = _neighbors({"name": "Helper", "path_prefix": "a"})
    assert traversals == [] and res["too_broad"]["anchors"] == N_A + N_COPY
    assert res["too_broad"]["by_folder"] == {"a": N_A + N_COPY} and "b" not in res["too_broad"]["by_folder"]
    only_kind = _neighbors({"name": "Helper", "kind": "Copybook"})
    assert traversals == [N_COPY] and "too_broad" not in only_kind


def test_narrowing_never_widens_the_users_scope(loaded, traversals):
    inside_scope = _neighbors({"name": "Helper"}, scope=["b"])
    assert "too_broad" not in inside_scope and traversals == [N_B]   # 範囲外の a/ の同名は起点に数えない
    outside = _neighbors({"name": "Helper", "path_prefix": "a"}, scope=["b"])
    assert outside["error"] == "graph_invalid_args" and "範囲の外" in outside["message"] and traversals == [N_B]
    inside = _neighbors({"name": "Helper", "path_prefix": "b/m1"}, scope=["b"])
    assert [n["name"] for n in inside["neighbors"]] == ["CallerB1"]
    broader = _neighbors({"name": "Solo", "path_prefix": "a"}, scope=["a/solo"])   # 範囲より広い指定は範囲のまま
    assert [n["name"] for n in broader["neighbors"]] == ["SoloCaller"]


def test_narrow_name_is_traversed_as_before(loaded, traversals):
    res = _neighbors({"name": "Solo"})
    assert "too_broad" not in res and traversals == [1]
    assert [n["name"] for n in res["neighbors"]] == ["SoloCaller"]


def test_limit_is_configurable(loaded, traversals, monkeypatch):
    monkeypatch.setenv("SHERPA_GRAPH_ANCHOR_MAX", "5")
    assert _neighbors({"name": "Helper", "path_prefix": "b"})["too_broad"]["limit"] == 5 and traversals == []
    monkeypatch.setenv("SHERPA_GRAPH_ANCHOR_MAX", "100")
    assert "too_broad" not in _neighbors({"name": "Helper"}) and traversals == [N_A + N_B + N_COPY]


def test_invalid_kind_is_rejected(loaded):
    assert _neighbors({"name": "Helper", "kind": "Nope"})["error"] == "graph_invalid_args"


def test_graph_resolve_accepts_path_prefix_within_scope(loaded):
    res = tool_dispatch.run_tool("graph_resolve", {"name": "Helper", "path_prefix": "b", "limit": 50}, WORLD, None)[0]
    assert res["count"] == N_B and all(c["path"].startswith("b/") for c in res["candidates"])
    out = tool_dispatch.run_tool("graph_resolve", {"name": "Helper", "path_prefix": "a"}, WORLD, ["b"])[0]
    assert out["error"] == "graph_invalid_args"


def test_graph_impact_path_prefix_does_not_widen(loaded):
    cid = _cid("Module", "a/solo/Solo.java", "Solo")
    ok = tool_dispatch.run_tool("graph_impact", {"canonical_id": cid, "path_prefix": "a/solo"}, WORLD, None)[0]
    assert [i["name"] for i in ok["impact"]] == ["SoloCaller"]
    cut = tool_dispatch.run_tool("graph_impact", {"canonical_id": cid, "path_prefix": "b"}, WORLD, None)[0]
    assert cut["error"] == "graph_start_not_found"


# ---- 起点の取り方（同名が全体で上限を超える／たどらない status／所属パスで範囲を判定する Dataset） ----

WORLD_X = TEST_WORLD_ID.replace("-", "_") + "_narrow_x"   # 使い捨て（テスト後に削除）
N_FILLER = lens_service._NEO4J_MAX_ROWS                    # 先頭の読み取り行数の天井を埋める無関係なノードの数
N_BROAD, N_DEPRECATED = 51, 60                             # 上限（既定 50）+1 の同名／上限より多い deprecated の同名


def _cid_x(label, path, name):
    return f"{label.lower()}:{WORLD_X}:{path}#{name}"


@pytest.fixture(scope="module")
def loaded_x():
    from neo4j import GraphDatabase
    nodes, edges = [], []

    def node(label, path, name, status="active", **extra):
        n = {"cid": _cid_x(label, path, name), "label": label, "name": name, "top_scope": path.split("/")[0],
             "path": path, "status": status, **extra}
        nodes.append(n)
        return n

    def link(kind, src, dst):
        edges.append({"type": kind, "src": src["cid"], "dst": dst["cid"], "doc": None, "line": 1, "status": "active"})

    half = N_BROAD // 2
    for i in range(half):                                   # 同名 Widget の前半（読み取りの先頭に入る）
        node("Module", f"w/a{i}/Widget.java", "Widget")
    for i in range(N_FILLER):                               # 無関係なノード（先頭の天井を埋める）
        node("Module", f"f/m{i}/Filler{i}.java", f"Filler{i}")
    for i in range(N_BROAD - half):                         # 同名 Widget の後半（天井の外）
        node("Module", f"w/b{i}/Widget.java", "Widget")
    for i in range(N_DEPRECATED):                           # たどらない状態の同名 Legacy
        node("Module", f"d/m{i}/Legacy.java", "Legacy", status="deprecated")
    live = node("Module", "d/live/Legacy.java", "Legacy")
    link("INVOKES", node("Module", "d/live/Caller.java", "LiveCaller"), live)
    ds = node("Dataset", "x/first/job.jcl", "PAYROLL.MASTER",
              membership_paths=["x/first/job.jcl", "y/second/job.jcl"])
    link("ACCESSES", node("Module", "y/second/Prog.cob", "PayProg"), ds)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD_X, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD_X)
    drv = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        yield drv
    finally:
        with drv.session() as s:
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD_X)
        drv.close()


def _neighbors_x(args, graph_only=True):
    return tool_dispatch.run_tool("graph_neighbors", args, WORLD_X, None, graph_only=graph_only)[0]


def test_same_name_beyond_the_row_cap_is_still_too_broad(loaded_x, traversals):
    # 同名の起点が全体で上限+1 件。先頭 10,000 行には上限以下しか入らない並びでも、たどらずに広すぎると返す
    res = _neighbors_x({"name": "ここは Widget の話", "kind": "Module"}, graph_only=False)
    assert traversals == [] and res["neighbors"] == []
    assert res["too_broad"]["anchors"] == N_BROAD and res["too_broad"]["by_folder"] == {"w": N_BROAD}
    assert res["too_broad"]["limit"] == 50


def test_deprecated_same_names_do_not_stop_a_single_active_anchor(loaded_x, traversals):
    # 廃止の同名が上限より多くても、たどる status の起点は 1 件＝止めずにたどる
    for graph_only, name in ((True, "Legacy"), (False, "Legacy を呼ぶ処理")):
        traversals.clear()
        res = _neighbors_x({"name": name}, graph_only=graph_only)
        assert "too_broad" not in res and traversals == [1]
        assert [n["name"] for n in res["neighbors"]] == ["LiveCaller"]


def test_dataset_is_found_by_a_membership_path_inside_the_prefix(loaded_x):
    # 代表の path は範囲の外（x/）でも、所属パス（y/）が範囲の中なら見つかり、返す path も範囲の中
    res = tool_dispatch.run_tool("graph_resolve", {"name": "PAYROLL", "path_prefix": "y"}, WORLD_X, None)[0]
    assert [(c["name"], c["path"]) for c in res["candidates"]] == [("PAYROLL.MASTER", "y/second/job.jcl")]
    cid = _cid_x("Dataset", "x/first/job.jcl", "PAYROLL.MASTER")
    imp = tool_dispatch.run_tool("graph_impact", {"canonical_id": cid, "path_prefix": "y"}, WORLD_X, None)[0]
    assert imp["start"]["path"] == "y/second/job.jcl" and [i["name"] for i in imp["impact"]] == ["PayProg"]
    none = tool_dispatch.run_tool("graph_resolve", {"name": "PAYROLL", "path_prefix": "z"}, WORLD_X, None)[0]
    assert none["candidates"] == []
    plain = tool_dispatch.run_tool("graph_resolve", {"name": "PAYROLL"}, WORLD_X, None)[0]
    assert [c["path"] for c in plain["candidates"]] == ["x/first/job.jcl"]


# ---- 所属パスの範囲判定（起点の照合・件数の集計）と、秘匿名の所属パスを避けた代表の選び方 ----

WORLD_M = TEST_WORLD_ID.replace("-", "_") + "_narrow_m"   # 使い捨て（テスト後に削除）


def _cid_m(label, path, name):
    return f"{label.lower()}:{WORLD_M}:{path}#{name}"


@pytest.fixture(scope="module")
def loaded_m():
    from neo4j import GraphDatabase
    nodes, edges = [], []

    def node(label, path, name, **extra):
        n = {"cid": _cid_m(label, path, name), "label": label, "name": name, "top_scope": path.split("/")[0],
             "path": path, "status": "active", **extra}
        nodes.append(n)
        return n

    def link(src, dst):
        edges.append({"type": "ACCESSES", "src": src["cid"], "dst": dst["cid"], "doc": None, "line": 1, "status": "active"})

    pay = node("Module", "y/second/Prog.cob", "PayProg")
    # 同名の Dataset が 2 つ。代表の path は y の外で、所属パスの 1 つが y の中
    link(pay, node("Dataset", "x/first/job.jcl", "PAYROLL.MASTER",
                   membership_paths=["x/first/job.jcl", "y/second/job.jcl"]))
    link(pay, node("Dataset", "z/third/job.jcl", "PAYROLL.MASTER",
                   membership_paths=["y/fourth/job.jcl", "z/third/job.jcl"]))
    # 秘匿名の所属パスが先頭に並ぶ Dataset
    link(node("Module", "a/Prog.cob", "SecretProg"),
         node("Dataset", "a/credentials.jcl", "SECRET.DS",
              membership_paths=["a/credentials.jcl", "a/public.jcl"]))
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD_M, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD_M)
    drv = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        yield drv
    finally:
        with drv.session() as s:
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD_M)
        drv.close()


def _neighbors_m(args, graph_only=True):
    return tool_dispatch.run_tool("graph_neighbors", args, WORLD_M, None, graph_only=graph_only)[0]


@pytest.mark.parametrize("graph_only", [True, False])
def test_neighbors_finds_a_dataset_by_a_membership_path_inside_the_prefix(loaded_m, traversals, graph_only):
    name = "PAYROLL.MASTER" if graph_only else "PAYROLL.MASTER を使う処理"
    res = _neighbors_m({"name": name, "path_prefix": "y"}, graph_only=graph_only)
    assert "too_broad" not in res and traversals == [2]
    assert "PayProg" in {n["name"] for n in res["neighbors"]}
    traversals.clear()
    none = _neighbors_m({"name": name, "path_prefix": "w"}, graph_only=graph_only)
    assert none["neighbors"] == [] and traversals == [0]


@pytest.mark.parametrize("graph_only", [True, False])
def test_breadth_counts_dataset_by_the_membership_path_inside_the_prefix(loaded_m, traversals, monkeypatch, graph_only):
    monkeypatch.setenv(lens_service.GRAPH_ANCHOR_MAX_ENV, "1")
    name = "PAYROLL.MASTER" if graph_only else "PAYROLL.MASTER を使う処理"
    res = _neighbors_m({"name": name, "path_prefix": "y"}, graph_only=graph_only)
    assert traversals == [] and res["too_broad"]["anchors"] == 2
    assert res["too_broad"]["by_folder"] == {"y": 2} and res["too_broad"]["by_kind"] == {"Dataset": 2}


def test_dataset_with_a_sensitive_member_shows_the_public_member_inside_the_prefix(loaded_m):
    res = tool_dispatch.run_tool("graph_resolve", {"name": "SECRET.DS", "path_prefix": "a"}, WORLD_M, None)[0]
    assert [(c["name"], c["path"]) for c in res["candidates"]] == [("SECRET.DS", "a/public.jcl")]
    cid = _cid_m("Dataset", "a/credentials.jcl", "SECRET.DS")
    imp = tool_dispatch.run_tool("graph_impact", {"canonical_id": cid, "path_prefix": "a"}, WORLD_M, None)[0]
    assert imp.get("error") is None and imp["start"]["path"] == "a/public.jcl"
    assert [i["name"] for i in imp["impact"]] == ["SecretProg"]
