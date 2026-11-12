"""管理者の除外（`path_rules`）を Neo4j の問い合わせへ入れる式（`world_neo4j` が使う。名前はそちらからも引ける）。
除外は起点の選び・経路の全ノード・`LIMIT` の前に掛ける。設定が無ければ式は `"true"`・パラメータは空（問い合わせの結果は従来と同じ）。
設計: docs/design/rag.md
"""
from __future__ import annotations

import contextlib
import contextvars
import functools

_X_PATHS: contextvars.ContextVar = contextvars.ContextVar("world_neo4j_exclude_paths", default=())


def _paths(world: str) -> tuple:
    from .. import path_rules  # 遅延 import（循環回避）
    return path_rules.rules_for(world).exclude_paths


def _params(paths) -> dict:
    return {"x_exact": list(paths), "x_pre": [p + "/" for p in paths]}


def hit(p: str) -> str:
    """パス式 `p` が管理者の除外に当たる、の Cypher 式（`$x_exact`・`$x_pre` を使う）。"""
    return f"coalesce(({p} IN $x_exact OR any(x_pre IN $x_pre WHERE {p} STARTS WITH x_pre)), false)"


def exclude_expr(world: str, path_expr: str) -> tuple[str, dict]:
    """管理者の除外に当たらない、の Cypher 式と、その `$x_exact`・`$x_pre`。`path_expr` は対象のパスを返す式。"""
    paths = _paths(world)
    return (f"NOT {hit(path_expr)}", _params(paths)) if paths else ("true", {})


def exclude_edge_expr(world: str, var: str = "e") -> tuple[str, dict]:
    """辺 `var` の参照元 `doc` が管理者の除外のとき、除外でない根拠（`sources` の `doc_id`）が 1 件でもある辺だけ通す式と、そのパラメータ。
    集約された辺（同じ辺の複数の資料を `sources` に持つ）を、代表の `doc` だけで落とさないために、行数の天井の前に掛ける。"""
    paths = _paths(world)
    if not paths:
        return "true", {}
    alt = "|".join("\\Q" + p + "\\E" for p in paths)
    return (f"(NOT {hit(var + '.doc')} OR ({var}.sources IS NOT NULL AND {var}.sources =~ $x_src_re))",
            {"x_src_re": '(?s).*"doc_id": "(?!(?:' + alt + ')(?:/|"))[^"]*".*'})


def exclude_node_expr(world: str, var: str) -> tuple[str, dict]:
    """ノード `var` が管理者の除外に当たらない、の Cypher 式と `$x_exact`・`$x_pre`。所属パス（`membership_paths`）を持つノード（Dataset）は、
    所属ごとに判定し、除外でない所属が 1 件でもあれば見える（全部が除外のときだけ外す）。"""
    paths = _paths(world)
    if not paths:
        return "true", {}
    mp = f"{var}.membership_paths"
    return (f"(CASE WHEN {mp} IS NOT NULL AND size({mp}) > 0 THEN any(x_mp IN {mp} WHERE NOT {hit('x_mp')}) "
            f"ELSE NOT {hit(var + '.path')} END)", _params(paths))


def x_node(world: str, var: str) -> str:
    """`exclude_node_expr` の式だけ（パラメータは `exclude_node_params`）。"""
    return exclude_node_expr(world, var)[0]


def exclude_node_params(world: str) -> dict:
    """ノードの除外式（`exclude_node_expr`）が使うパラメータ（設定が無ければ空）。"""
    return exclude_node_expr(world, "n")[1]


def exclude_traverse(world: str) -> str:
    """影響たどりで広げた先のノード `m` と辺 `e` を通す条件（根拠が除外の資料だけの辺は辿らない）。"""
    return f"{x_node(world, 'm')} AND {exclude_edge_expr(world, 'e')[0]}"


def resolve_visibility(world: str) -> tuple[str, str, dict]:
    """起点の解決（`world_neo4j.resolve_world_entity`）の範囲の式・代表パスの式・パラメータ。複数の資料に属するノード（Dataset・定義の無い表）は所属（`membership_paths`）のどれかが範囲内なら範囲内で、代表のパスも所属から選ぶ（`get_world_entities` と同じ）。"""
    from . import world_neo4j as wn  # 遅延 import（循環回避）
    params = exclude_node_params(world)
    if not params:
        return wn._member_scope_pred("n"), wn._member_visible_path("n", world), wn._sensitive_name_params()
    return member_scope_visible(world, "n"), wn._member_visible_path("n", world), {**params, **wn._sensitive_name_params()}


def exclude_params(world: str) -> dict:
    """ノードと辺の除外式（`exclude_node_expr`・`exclude_edge_expr`）が使うパラメータをまとめて返す。"""
    return {**exclude_node_expr(world, "n")[1], **exclude_edge_expr(world, "e")[1]}


def exclude_member_clause(world: str) -> str:
    """所属パス（`mpath`）の絞り込みに足す、管理者の除外の条件（`" AND ..."`・`world` が空か設定が無ければ空）。"""
    expr = exclude_expr(world, "mpath")[0] if world else "true"
    return "" if expr == "true" else f" AND {expr}"


def member_scope_visible(world: str, var: str) -> str:
    """ノード `var` が範囲の中で、かつ管理者の除外でない、の Cypher 式（`world_neo4j._member_scope_pred` の除外つき）。
    複数の資料に属するノード（Dataset）は、同じ所属（`mpath`）が「範囲の中」かつ「除外でない」ときだけ通す（別々の所属で満たした候補を上限の前に入れない）。
    除外の設定が無ければ `_member_scope_pred` のまま。`$x_exact`・`$x_pre` を使う。"""
    from . import world_neo4j as wn  # 遅延 import（循環回避）
    if not _paths(world):
        return wn._member_scope_pred(var)
    mp = f"{var}.membership_paths"
    inscope = "(size($prefixes)=0 OR any(pref IN $prefixes WHERE x_mp=pref OR x_mp STARTS WITH pref+'/'))"
    return (f"(CASE WHEN {mp} IS NOT NULL AND size({mp}) > 0 THEN any(x_mp IN {mp} WHERE {inscope} AND NOT {hit('x_mp')}) "
            f"ELSE ({wn._scope_pred(var)} AND NOT {hit(var + '.path')}) END)")


@contextlib.contextmanager
def exclude_scope(world: str):
    """この間に組み立てる流れの述語（`world_neo4j._flow_path_pred`）へ、管理者の除外を入れる（呼び出し側が `$x_exact`・`$x_pre` を渡す）。"""
    token = _X_PATHS.set(tuple(_paths(world)))
    try:
        yield
    finally:
        _X_PATHS.reset(token)


def scoped(fn):
    """`fn(session, start_cids, world_id, ...)` を `exclude_scope(world_id)` の中で実行する（影響たどりの全段へ除外を入れる）。"""
    @functools.wraps(fn)
    def wrapper(session, start_cids, world_id, *args, **kwargs):
        with exclude_scope(world_id):
            return fn(session, start_cids, world_id, *args, **kwargs)
    return wrapper


def flow_exclusion(path: str) -> str:
    """流れの述語（`_flow_path_pred`）に足す除外の条件（`exclude_scope` の外では空）。"""
    return f" AND NOT {hit(path)}" if _X_PATHS.get() else ""
