"""Neo4j へ書き込む行（ノード・辺）の組み立てと、読み取った辺の整形。`world_neo4j` が使う（名前はそちらからも引ける）。

設計: docs/05-グラフ語彙.md
"""
from __future__ import annotations

import hashlib
import json


def _sources_json(sources) -> str | None:
    """entity/relation の出所（`chunk_id`/`locator`/`logical_record_id` を持つ dict のリスト）を、
    Neo4j のプロパティに持てる1本の JSON 文字列にする。空/無ければ None（プロパティを立てない）。"""
    if not sources:
        return None
    return json.dumps(sources, ensure_ascii=False)


def _node_row(n: dict, generation: str | None = None, world_id: str | None = None) -> dict:
    """1ノード分の UNWIND 行（`world_id` はクエリの `$world` 側に出す。`sources` は JSON 文字列化済み）。

    `jcl_kind` は JCL の `Batch` 種別（`job`/`proc`/`include`）で、JCL 以外では `None`。
    """
    row = {
        "cid": n["cid"], "name": n["name"],
        "top": n.get("top_scope"), "phase": n.get("phase"), "cat": n.get("category"),
        "membership_paths": n.get("membership_paths"),
        "path": n.get("path"), "sp": n.get("scope_path"), "value": n.get("value"),
        "status": n.get("status", "active"),
        "analyzer": n.get("analyzer"),
        "sources": _sources_json(n.get("sources")),
        "sources_overflow": n.get("sources_overflow_count", 0),
        "jcl_kind": n.get("jcl_kind"),
        "schema": n.get("schema"), "qualified_name": n.get("qualified_name"),
        "unresolved": _sources_json(n.get("unresolved")),
        "unresolved_names": n.get("unresolved_names"),
        "unresolved_overflow": n.get("unresolved_overflow_count", 0),
        "period": n.get("period"), "period_place": n.get("period_place"),
    }
    if generation is not None:
        material = f"{world_id or n.get('world_id') or ''}\0{generation}\0{n['cid']}"
        row.update(generation=generation, storage_id=hashlib.sha256(material.encode("utf-8")).hexdigest())
    return row


def _edge_row(e: dict, generation: str | None = None) -> dict:
    """1エッジ分の UNWIND 行。`via` は `RefCandidate.extra["via"]` 由来。
    根拠は `sources`（JSON 文字列・`{via, doc_id, file, line, rule?, from_def?, locator?, evidence_text?}` の配列）に持ち、上限超過件数は
    `sources_overflow_count`。接続の規則（`rule`）・抜粋（`evidence_text`）も `sources` の中で、辺の単独の `source`・`evidence`・`rule` は書かない。
    """
    row = {
        "binding_paths": e.get("binding_paths"),
        "src": e["src"], "dst": e["dst"], "doc": e.get("doc", ""),
        "line": e.get("line", 0),
        "status": e.get("status", "active"),
        "sources": _sources_json(e.get("sources")),
        "sources_overflow": e.get("sources_overflow_count", 0),
        "via": e.get("via"),
        "confirm": e.get("confirm"), "assumption": e.get("assumption"),
    }
    if generation is not None:
        row["generation"] = generation
    return row


def node_period(r: dict) -> dict:
    """読み取った行の期（`period`）・期の置き場（`period_place`）。期を見分けた資料フォルダの期の中のノードだけ欄を持つ（期の外は空の辞書）。"""
    return {k: r[k] for k in ("period", "period_place") if r.get(k)}


# 読み取りが返す辺の根拠（`sources`）の件数。`world_impact` は上限（`EDGE_SOURCES_RETURN_MAX`）まで返し、呼び出し側（`run_impact`・
# 外部 API の `evidence_limit`・近傍）が `EDGE_SOURCES_RETURN_DEFAULT` を既定に `limit_edge_sources` で切る。
EDGE_SOURCES_RETURN_DEFAULT = 3
EDGE_SOURCES_RETURN_MAX = 10

# Cypher の経路の辺 `e` から根拠の欄を取り出す射影（影響・近傍が共有する。`edge_view` が読む）
EDGE_SOURCE_FIELDS = ("via:e.via, sources:e.sources, sources_overflow:e.sources_overflow_count, "
                      "confirm:e.confirm, assumption:e.assumption")


def edge_view(raw: dict, limit: int = EDGE_SOURCES_RETURN_MAX) -> dict:
    """Cypher で取り出した辺（`type`・`from`・`to`・`doc`・`line` と `EDGE_SOURCE_FIELDS`）→ 返却の辺。

    `via` は辺の関係の種類。`sources`（先頭 `limit` 件）・`sources_overflow_count`（返さなかった根拠の件数＝保存の上限超過を含む）・
    `rule`（接続を決めた解決規則）は根拠のある辺だけに付く。`confirm`（`confirmed`／`conditional`）・`assumption`（条件つきの理由の配列）は期を見分けた資料フォルダの構造エッジだけに付く。`rule` は先頭の根拠から読む（旧欄の辺 `rule` は書かないので読み替えで足りる）。
    """
    out = {k: raw[k] for k in ("type", "from", "to", "doc", "line") if k in raw}
    if raw.get("via"):
        out["via"] = raw["via"]
    if raw.get("confirm"):
        out["confirm"] = raw["confirm"]
        if raw.get("assumption"):
            out["assumption"] = list(raw["assumption"])
    try:
        srcs = json.loads(raw["sources"]) if raw.get("sources") else []
    except (TypeError, ValueError):
        srcs = []
    if srcs:
        if srcs[0].get("rule"):
            out["rule"] = srcs[0]["rule"]
        out["sources"] = srcs[:limit]
        out["sources_overflow_count"] = int(raw.get("sources_overflow") or 0) + max(0, len(srcs) - limit)
    return out


def limit_edge_sources(edges: list, limit: int) -> list:
    """辺（`edge_view` の形）の `sources` を先頭 `limit` 件（0〜`EDGE_SOURCES_RETURN_MAX` に収める）に切り、切った分を
    `sources_overflow_count` に足す（元は変えない）。"""
    limit = max(0, min(int(limit), EDGE_SOURCES_RETURN_MAX))
    out = []
    for e in edges:
        srcs = e.get("sources")
        if srcs is None or len(srcs) <= limit:
            out.append(e)
            continue
        out.append({**e, "sources": srcs[:limit],
                    "sources_overflow_count": int(e.get("sources_overflow_count") or 0) + len(srcs) - limit})
    return out
