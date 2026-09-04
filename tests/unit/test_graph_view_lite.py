"""②graph 軽量化（段階読み込み＋ETag）の純関数ユニット（外部サービス不要）。

`preview_service._select_top_nodes`（次数上位の主要ノード選択・決定性）と
`preview_service._graph_signature`（ETag の素＝内容署名の安定性）を検証する。
"""
from __future__ import annotations

from sherpa import preview_service as ps


def _node(nid, name, **kw):
    base = {"id": nid, "name": name, "type": "Module", "em": "static", "status": "active",
            "value": None, "top_scope": None, "path": None}
    base.update(kw)
    return base


# a=次数3 / b,c=次数2 / d=次数1（tie: b,c は名前昇順で b が上位）
NODES = [_node("a", "Alpha"), _node("b", "Bravo"), _node("c", "Charlie"), _node("d", "Delta")]
EDGES = [
    {"source": "a", "target": "b", "type": "CALLS", "em": "static", "status": "active"},
    {"source": "a", "target": "c", "type": "CALLS", "em": "static", "status": "active"},
    {"source": "a", "target": "d", "type": "CALLS", "em": "static", "status": "active"},
    {"source": "b", "target": "c", "type": "CALLS", "em": "static", "status": "active"},
]


def test_no_limit_passthrough_not_truncated():
    for lim in (None, 0, -5):
        nodes, edges, trunc = ps._select_top_nodes(NODES, EDGES, lim)
        assert trunc is False
        assert nodes == NODES and edges == EDGES


def test_limit_ge_total_not_truncated():
    nodes, edges, trunc = ps._select_top_nodes(NODES, EDGES, 4)
    assert trunc is False and len(nodes) == 4 and edges == EDGES


def test_top_by_degree_and_edge_filter():
    nodes, edges, trunc = ps._select_top_nodes(NODES, EDGES, 2)
    assert trunc is True
    assert {n["id"] for n in nodes} == {"a", "b"}            # 次数上位2（a=3, b=2）
    # 残す辺は両端が採用ノードのものだけ（a-b のみ・a-c/a-d/b-c は落ちる）
    assert edges == [EDGES[0]]
    # ノード列は元の順序を保持（決定的・レイアウト差を生まない）
    assert [n["id"] for n in nodes] == ["a", "b"]


def test_degree_tie_breaks_by_name_then_id():
    # b,c は同次数(2)。limit=2 の第2枠は名前昇順で b（Bravo < Charlie）。
    _, _, _ = ps._select_top_nodes(NODES, EDGES, 2)
    # 同名同次数は id 昇順（名前を同一にして id で決める）
    nodes = [_node("y", "Same"), _node("x", "Same"), _node("z", "Zeta")]
    edges = [{"source": "z", "target": "z", "type": "SELF", "em": "static", "status": "active"}]
    # z は自己ループで次数2（source/target 双方カウント）＝最上位、残り1枠は Same 同士→id 昇順で x
    sel, _e, trunc = ps._select_top_nodes(nodes, edges, 2)
    assert trunc is True
    assert {n["id"] for n in sel} == {"z", "x"}


def test_selection_is_deterministic():
    a = ps._select_top_nodes(NODES, EDGES, 3)
    b = ps._select_top_nodes(list(reversed(NODES)), list(reversed(EDGES)), 3)
    assert {n["id"] for n in a[0]} == {n["id"] for n in b[0]}   # 入力順に依らず同じ集合
    assert a[2] is True and b[2] is True


def _payload(**overrides):
    base = {"world": "w1", "counts": {"documents": 3}, "nodes": NODES, "edges": EDGES,
            "total_nodes": len(NODES), "total_edges": len(EDGES), "truncated": False}
    base.update(overrides)
    return base


def test_signature_stable_and_order_independent():
    s1 = ps._graph_signature(_payload())
    s2 = ps._graph_signature(_payload(nodes=list(reversed(NODES)), edges=list(reversed(EDGES))))
    assert s1 == s2 and isinstance(s1, str) and len(s1) == 40   # sha1 hexdigest


def test_signature_changes_with_content():
    base = ps._graph_signature(_payload())
    # 値ピンが変われば署名も変わる（描画に効く項目を含む）
    changed_nodes = [dict(n, value="99") if n["id"] == "a" else n for n in NODES]
    assert ps._graph_signature(_payload(nodes=changed_nodes)) != base
    # ノード集合が変われば別署名
    assert ps._graph_signature(_payload(nodes=NODES[:2])) != base
    # 辺が変われば別署名
    assert ps._graph_signature(_payload(edges=EDGES[:1])) != base


def test_signature_changes_when_only_counts_change():
    """RV是正（2026-07-08 Med#1）: グラフ内容（nodes/edges）が不変でも counts（文書数等）だけが
    変化すれば署名は変わる（＝304 が古い counts を返し続けない）。"""
    base = ps._graph_signature(_payload())
    assert ps._graph_signature(_payload(counts={"documents": 4})) != base


def test_signature_changes_when_world_or_derived_fields_change():
    """world・total_nodes/total_edges・truncated も署名対象（応答本体を丸ごと署名・Med#1 是正）。"""
    base = ps._graph_signature(_payload())
    assert ps._graph_signature(_payload(world="w2")) != base
    assert ps._graph_signature(_payload(total_nodes=99)) != base
    assert ps._graph_signature(_payload(truncated=True)) != base
