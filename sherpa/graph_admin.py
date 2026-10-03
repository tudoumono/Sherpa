"""管理画面向けグラフ操作（read-only）。

Neo4j の資料フォルダのグラフを直接読む検索（AI は使わない）。書き込みは行わない。
"""
from __future__ import annotations

import logging

from .impact_service import CATEGORY
from .ingest.model import NODE_LABELS
from .ingest.world_neo4j import (
    WORLD_EDGE_TYPES,
    _scope_pred,
    check_schema_era,
)
from .preview_service import _TYPE_JA

_log = logging.getLogger("sherpa")

_COND_FIELDS = {
    "category": lambda v: f"{v}.category",
    "phase": lambda v: f"{v}.phase",
    "top_scope": lambda v: f"{v}.top_scope",
    "path": lambda v: f"{v}.path",
    "status": lambda v: f"coalesce({v}.status,'active')",
    "extraction_method": lambda v: f"coalesce({v}.extraction_method,'static')",
    "em": lambda v: f"coalesce({v}.extraction_method,'static')",
    "type": lambda v: f"[l IN labels({v}) WHERE l<>'Entity'][0]",
    "label": lambda v: f"[l IN labels({v}) WHERE l<>'Entity'][0]",
    # グラフ内に role プロパティはないため、管理検索ではノード種別を role として扱う
    "role": lambda v: f"[l IN labels({v}) WHERE l<>'Entity'][0]",
}
_OPS = {"eq", "contains", "prefix"}
def facets() -> dict:
    """UI が使う閉じた語彙。Neo4j に聞かずコード正典から返す。"""
    labels = sorted(NODE_LABELS)
    rels = sorted(WORLD_EDGE_TYPES)
    return {"node_labels": labels, "node_labels_ja": {l: _TYPE_JA.get(l, l) for l in labels},
            "relationship_types": rels,
            "condition_fields": ["category", "phase", "role", "top_scope", "status"]}


def _condition(var: str, field: str | None, value: str | None, op: str) -> str | None:
    if not field and not value:
        return None
    field = (field or "").strip()
    value = (value or "").strip()
    op = (op or "eq").strip()
    if field not in _COND_FIELDS:
        raise ValueError(f"unknown condition field: {field}")
    if op not in _OPS:
        raise ValueError(f"unknown condition op: {op}")
    if not value:
        raise ValueError("condition value is empty")
    expr = _COND_FIELDS[field](var)
    text = f"toString(coalesce({expr},''))"
    if op == "eq":
        return f"{text} = $cond_value"
    if op == "prefix":
        return f"toLower({text}) STARTS WITH toLower($cond_value)"
    return f"toLower({text}) CONTAINS toLower($cond_value)"


def _node(row: dict, prefix: str) -> dict | None:
    cid = row.get(prefix + "id")
    if not cid:
        return None
    label = row.get(prefix + "label")
    return {"id": cid, "name": row.get(prefix + "name"), "type": label,
            "type_ja": _TYPE_JA.get(label, label),
            "em": row.get(prefix + "em") or "static",
            "status": row.get(prefix + "status") or "active",
            "value": row.get(prefix + "value"),
            "top_scope": row.get(prefix + "top_scope"),
            "phase": row.get(prefix + "phase"),
            "category": row.get(prefix + "category"),
            "path": row.get(prefix + "path")}


def _rows_to_graph(rows: list[dict], world: str, counts: dict | None = None) -> dict:
    nodes, edges, seen_e = {}, [], set()
    for r in rows:
        for p in ("source_", "target_"):
            n = _node(r, p)
            if n:
                nodes[n["id"]] = n
        if r.get("edge_type") and r.get("source_id") and r.get("target_id"):
            key = (r["source_id"], r["target_id"], r["edge_type"])
            if key not in seen_e:
                seen_e.add(key)
                edges.append({"source": r["source_id"], "target": r["target_id"],
                              "type": r["edge_type"], "em": r.get("edge_em") or "static",
                              "status": r.get("edge_status") or "active"})
    out_nodes = sorted(nodes.values(), key=lambda n: ((n.get("type_ja") or ""), (n.get("name") or "")))
    return {"world": world, "nodes": out_nodes, "edges": edges,
            "counts": counts or {"nodes": len(out_nodes), "edges": len(edges)}}


def _ret(prefix: str, var: str) -> str:
    return (
        f"{var}.canonical_id AS {prefix}id, {var}.name AS {prefix}name, "
        f"[l IN labels({var}) WHERE l<>'Entity'][0] AS {prefix}label, "
        f"coalesce({var}.extraction_method,'static') AS {prefix}em, "
        f"coalesce({var}.status,'active') AS {prefix}status, {var}.value AS {prefix}value, "
        f"{var}.top_scope AS {prefix}top_scope, {var}.phase AS {prefix}phase, "
        f"{var}.category AS {prefix}category, {var}.path AS {prefix}path"
    )


def graph_search(session, world: str, relationship_types=None, field: str | None = None,
                 value: str | None = None, op: str = "eq", scope_paths=None,
                 include_deprecated: bool = False, limit: int = 200) -> dict:
    """関係種別/属性条件で world グラフを検索し、可視化と同じ nodes/edges 形で返す。

    主クエリの後に `check_schema_era` を呼ぶ（旧世代の実データがあれば `GraphSchemaEraError`＝呼び出し元が 503 へ変換）。
    """
    rels = [str(r).strip().upper() for r in (relationship_types or []) if str(r).strip()]
    bad = [r for r in rels if r not in WORLD_EDGE_TYPES]
    if bad:
        raise ValueError(f"unknown relationship type: {', '.join(sorted(bad))}")
    prefixes = list(scope_paths or [])
    params = {"world": world, "prefixes": prefixes, "incl": include_deprecated,
              "cond_value": value or "", "limit": max(1, min(int(limit or 200), 1000))}

    if rels:
        rel_clause = "|".join(sorted(set(rels)))
        cond_a = _condition("a", field, value, op)
        cond_b = _condition("b", field, value, op)
        where = [
            "a.world_id=$world AND b.world_id=$world",
            _scope_pred("a"), _scope_pred("b"),
            "($incl OR (coalesce(a.status,'active')='active' AND coalesce(b.status,'active')='active' "
            "AND coalesce(r.status,'active')='active'))",
        ]
        if cond_a:
            where.append(f"({cond_a} OR {cond_b})")
        cy = (
            f"MATCH (a:Entity)-[r:{rel_clause}]->(b:Entity) "
            "WHERE " + " AND ".join(where) + " "
            "RETURN " + _ret("source_", "a") + ", " + _ret("target_", "b") + ", "
            "type(r) AS edge_type, coalesce(r.extraction_method,'static') AS edge_em, "
            "coalesce(r.status,'active') AS edge_status "
            "ORDER BY edge_type, source_name, target_name LIMIT $limit"
        )
        rows = session.run(cy, **params).data()
        check_schema_era(session, world)
        return _rows_to_graph(rows, world)

    cond_n = _condition("n", field, value, op)
    if not cond_n:
        raise ValueError("relationship_types or condition is required")
    cond_m = _condition("m", field, value, op)
    cy = (
        "MATCH (n:Entity {world_id:$world}) "
        f"WHERE {_scope_pred('n')} AND {cond_n} "
        "  AND ($incl OR coalesce(n.status,'active')='active') "
        "WITH n LIMIT $limit "
        "OPTIONAL MATCH (n)-[r]-(m:Entity {world_id:$world}) "
        f"WHERE {_scope_pred('m')} AND {cond_m} "
        "  AND ($incl OR (coalesce(m.status,'active')='active' AND coalesce(r.status,'active')='active')) "
        "RETURN " + _ret("source_", "n") + ", " + _ret("target_", "m") + ", "
        "type(r) AS edge_type, coalesce(r.extraction_method,'static') AS edge_em, "
        "coalesce(r.status,'active') AS edge_status "
        "ORDER BY source_name, edge_type, target_name"
    )
    rows = session.run(cy, **params).data()
    check_schema_era(session, world)
    return _rows_to_graph(rows, world)


