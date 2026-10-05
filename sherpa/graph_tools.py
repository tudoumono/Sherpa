"""Codex の道具 `graph_resolve`（起点の候補を並べる）・`graph_impact`（識別子から構造の依存だけを逆向きにたどる）。

2 段で使う: `graph_resolve` で同名を含む候補（`canonical_id`・種別・所属パス・表示名・修飾名）を見て起点を選び、
`graph_impact` へ `canonical_id` を渡す。名前の曖昧さは呼び出し側が解く（`graph_neighbors` の名前の完全一致とは別の経路）。
`graph_impact` は構造の辺（COPIES・CONTAINS・INVOKES・ACCESSES）だけをたどり、言及（DOCUMENTS）は影響に数えず
関連文書として別に返す（層が both のときだけ）。層がソースに限定されている間も使える（コード同士の閉じた辺だけ・資料は一切返さない）。
未探索（時間切れ・件数の天井・深さ・返却件数）は `coverage`（`graph_coverage.py` の欄）で申告する。
契約: docs/design/interfaces.md「graph_resolve・graph_impact」・docs/proposals/2026-10-04-アナライザとグラフの改善.md S6。
"""
from __future__ import annotations

import json
import logging

from . import graph_coverage, layer as layer_mod, scope
from .graph_coverage import Coverage
from .ingest import text_kind
from .ingest.model import NODE_LABELS
from .ingest.world_neo4j import GraphQueryOverloadError, GraphSchemaEraError

_log = logging.getLogger("sherpa")

TOOL_NAMES = ("graph_resolve", "graph_impact")

# 資料のみの調査（層 docs）では使えない（ソースの名前・経路が層外へ出るため）。層 code・both では使える。
LAYER_REJECT_MESSAGE = "指定した探す対象（層）では関係グラフの照会は使えません"
INVALID_ARGS_ERROR = "graph_invalid_args"
START_NOT_FOUND_ERROR = "graph_start_not_found"

RESOLVE_LIMIT_DEFAULT = 20
RESOLVE_LIMIT_MAX = 50
IMPACT_DEPTH_DEFAULT = 5
IMPACT_LIMIT_DEFAULT = 50
EVIDENCE_LIMIT_DEFAULT = 3   # 辺ごとに返す根拠（sources）の件数（外部 API の `evidence_limit` と同じ型・0〜10）
EVIDENCE_LIMIT_MAX = 10
IMPACT_LIMIT_MAX = 200
RELATED_DOCS_LIMIT = 20

_KINDS = tuple(sorted(NODE_LABELS))


def _invalid(message: str) -> dict:
    return {"error": INVALID_ARGS_ERROR, "message": message}


def _int_arg(args: dict, key: str, default: int, lo: int, hi: int):
    """整数引数を `[lo, hi]` に丸めて返す。未指定は既定値・整数でなければ `None`（呼び出し側が入力の誤りにする）。"""
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def json_bytes(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _sensitive_path(path) -> bool:
    """秘匿名のファイル（`text_kind.is_sensitive_doc_id`）か。パスが無いノードは対象外。"""
    return bool(path) and text_kind.is_sensitive_doc_id(str(path))


def _node_view(n: dict) -> dict:
    out = {"canonical_id": n["canonical_id"], "kind": n["label"], "name": n["name"], "path": n["path"]}
    if n.get("qualified_name"):
        out["qualified_name"] = n["qualified_name"]
    return out


def _flag_truncated(result: dict, coverage: dict) -> None:
    """時間切れ・天井・深さ・返却件数の上限で一部しか返していないとき `truncated:true`（利用統計の打ち切りの内訳と同じ印）。"""
    kinds = {lim["kind"] for lim in coverage["limits"]}
    if kinds & {graph_coverage.KIND_TIMEOUT, graph_coverage.KIND_ROW_CAP, graph_coverage.KIND_DEPTH,
                graph_coverage.KIND_RESULT_CAP}:
        result["truncated"] = True


def _failure(field: str, error_code: str) -> dict:
    """Neo4j の障害（`graph_neighbors` と同じ形）。接続系は `coverage` にも `graph_unavailable` を載せる。"""
    result: dict = {field: []}
    if error_code == graph_coverage.KIND_GRAPH_UNAVAILABLE:
        cov = Coverage()
        cov.add(graph_coverage.KIND_GRAPH_UNAVAILABLE)
        result["coverage"] = cov.as_dict()
    result["error_code"] = error_code
    return result


def _open_driver():
    from neo4j import GraphDatabase

    from .ingest import world_neo4j
    env = world_neo4j._env()
    return GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))


def run(name: str, args: dict, world: str, scope_paths, layer=None) -> dict:
    """`graph_resolve`／`graph_impact` を実行して結果の辞書を返す（層 docs の拒否は呼び出し側）。

    `GraphSchemaEraError`（旧世代のグラフ）は再送出する（呼び出し側が `graph_reingest_required` のツール結果へ変換する）。
    それ以外の Neo4j の障害は捕捉して `error_code`（接続系は `graph_unavailable`・他は `graph_internal_error`）で返す。
    """
    args = args or {}
    code_only = layer == "code"
    sp = scope.normalize_scope_paths(scope_paths) or None
    if name == "graph_resolve":
        parsed = _parse_resolve(args)
        field = "candidates"
    else:
        parsed = _parse_impact(args)
        field = "impact"
    if "error" in parsed:
        return parsed
    driver = None
    try:
        driver = _open_driver()
        with driver.session() as s:
            if name == "graph_resolve":
                result = _resolve(s, world, sp, code_only, **parsed)
            else:
                result = _impact(s, world, sp, code_only, layer in (None, "both"), **parsed)
            _attach_plugin_failures(s, world, result, _FIELD_STAGE[field])
            return result
    except GraphSchemaEraError:
        raise
    except Exception as exc:
        from neo4j.exceptions import ConfigurationError, DriverError, TransientError
        recoverable = not isinstance(exc, ConfigurationError) and isinstance(exc, (DriverError, TransientError))
        _log.warning("graph_tools: %s の取得に失敗（回復%s）: %s errno=%s", name, "可" if recoverable else "不可",
                     type(exc).__name__, getattr(exc, "errno", None))
        return _failure(field, "graph_unavailable" if recoverable else "graph_internal_error")
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def _attach_plugin_failures(session, world: str, result: dict, stage: str) -> None:
    """取り込み時に失敗した FW プラグインを `coverage.limits`（`plugin_failed`・プラグイン名つき）へ足す。`coverage` を持たない失敗応答には付けない。"""
    from .ingest import world_neo4j
    cov = result.get("coverage")
    if isinstance(cov, dict):
        graph_coverage.attach_plugin_failures(cov, lambda: world_neo4j.read_plugin_failures(session, world), stage)


# ---- graph_resolve ----

def _parse_resolve(args: dict) -> dict:
    name = str(args.get("name") or "").strip()
    path = str(args.get("path") or "").strip()
    kind = str(args.get("kind") or "").strip()
    if not name and not path:
        return _invalid("name（名前の一部）か path（所属パスの一部）のどちらかを指定してください")
    if kind and kind not in NODE_LABELS:
        return _invalid(f"kind は {', '.join(_KINDS)} のいずれか")
    limit = _int_arg(args, "limit", RESOLVE_LIMIT_DEFAULT, 1, RESOLVE_LIMIT_MAX)
    if limit is None:
        return _invalid("limit は整数で")
    return {"name": name, "path": path, "kind": kind, "limit": limit}


def _resolve(session, world, sp, code_only, *, name, path, kind, limit) -> dict:
    from .ingest import world_neo4j
    cov = Coverage()
    try:
        # 上限＋1 件だけ取る（超えたら件数は分からない）。資料（層 code では除く）と秘匿名のファイルのノードは Cypher の側で除く
        rows = world_neo4j.list_world_candidates(session, name, world, sp, kind=kind or None, path_part=path or None,
                                                 limit=limit + 1, exclude_documents=code_only)
    except GraphQueryOverloadError as e:
        cov.add(graph_coverage.kind_of_overload(e.reason), graph_coverage.STAGE_ANCHOR)
        result = {"candidates": [], "count": None, "coverage": cov.as_dict(omitted=None)}
        _flag_truncated(result, result["coverage"])
        return result
    rows = [r for r in rows if not _sensitive_path(r["path"])]  # 多層防御（Cypher の述語と同じ規則）
    capped = len(rows) > limit
    shown = rows[:limit]
    if capped:
        cov.add(graph_coverage.KIND_RESULT_CAP, graph_coverage.STAGE_ANCHOR)
    candidates = [{**_node_view(r), "match": r["match"]} for r in shown]
    # 超過の有無だけ分かる（総数は取らない）ので、上限に達したときは count・omitted を null にする
    result = {"candidates": candidates, "count": None if capped else len(shown),
              "coverage": cov.as_dict(omitted=None)}
    _flag_truncated(result, result["coverage"])
    return result


# ---- graph_impact ----

def _parse_impact(args: dict) -> dict:
    cid = str(args.get("canonical_id") or "").strip()
    if not cid:
        return _invalid("canonical_id（graph_resolve の candidates[].canonical_id）を指定してください")
    from .ingest.world_neo4j import IMPACT_MAX_DEPTH
    depth = _int_arg(args, "depth", IMPACT_DEPTH_DEFAULT, 1, IMPACT_MAX_DEPTH)
    limit = _int_arg(args, "limit", IMPACT_LIMIT_DEFAULT, 1, IMPACT_LIMIT_MAX)
    ev = _int_arg(args, "evidence_limit", EVIDENCE_LIMIT_DEFAULT, 0, EVIDENCE_LIMIT_MAX)
    if depth is None or limit is None or ev is None:
        return _invalid("depth・limit・evidence_limit は整数で")
    return {"canonical_id": cid, "depth": depth, "limit": limit, "evidence_limit": ev}


def _route_doc(doc, sp, code_only=False):
    """辺の参照元の資料パス。秘匿名・範囲外は返さない（`None`）。層 code では、ソースでない（アナライザが担当しない拡張子の）パスも返さない。"""
    if not doc or _sensitive_path(doc) or (sp and not scope.in_scope(doc, sp)):
        return None
    if code_only and not layer_mod.in_layer(doc, "code"):
        return None
    return doc


SOURCE_FIELDS = ("via", "doc_id", "file", "line", "rule", "locator", "evidence_text")  # 辺の根拠で返してよい欄（`from_def` は別に組む）


def source_view(src, doc_ok):
    """辺の根拠 1 件を、返してよい欄（`SOURCE_FIELDS`・`from_def{file, key}`）だけで組み直す。`graph_neighbors`・`graph_impact` が共有する。
    根拠の資料（`doc_id`・`file`・`from_def.file`）のどれかが `doc_ok`（実在・範囲・非秘匿・層の検査）を通らなければ根拠ごと `None`
    （`locator`・`evidence_text` は `doc_id` の資料の中の位置・抜粋なので、`doc_id` が通っていれば返す）。"""
    if not isinstance(src, dict) or not (doc_ok(src.get("doc_id")) and doc_ok(src.get("file") or src.get("doc_id"))):
        return None
    out = {k: src[k] for k in SOURCE_FIELDS if k in src}
    fd = src.get("from_def")
    if fd is not None:
        if not isinstance(fd, dict) or (fd.get("file") and not doc_ok(fd["file"])):
            return None
        out["from_def"] = {k: fd[k] for k in ("file", "key") if k in fd}
    return out


def _safe_sources(sources, sp, code_only=False):
    """辺の根拠（`sources`）のうち、参照元の資料（`doc_id`・`file`・`from_def.file`）が秘匿名・範囲外でないものだけ残す。
    外れた根拠は除き、その件数は `sources_overflow_count` に足さない（存在を漏らさない）。"""
    views = (source_view(s, lambda d: bool(_route_doc(d, sp, code_only))) for s in sources or [])
    return [v for v in views if v is not None]


def _edge_evidence(raw_edges: list, cids: list, sp, evidence_limit: int, code_only=False) -> list:
    """代表経路の辺ごとの根拠（`route` と同じ並び）。`via`・`line`・`rule`・`sources`（先頭 `evidence_limit` 件）・`sources_overflow_count`。"""
    from .ingest.world_neo4j import edge_view, limit_edge_sources
    out = []
    for i, raw in enumerate(raw_edges):
        e = edge_view(raw)
        if "sources" in e:
            e["sources"] = _safe_sources(e["sources"], sp, code_only)
        ev = {"from": cids[i], "to": cids[i + 1], **{k: e[k] for k in ("via", "line", "rule") if k in e}}
        if "sources" in e:
            ev.update({k: v for k, v in limit_edge_sources([e], evidence_limit)[0].items()
                       if k in ("sources", "sources_overflow_count")})
        out.append(ev)
    return out


def _impact_item(it: dict, sp, evidence_limit: int, code_only=False) -> dict:
    cids, edges = it["trace_cids"], it["edges"]
    route = [{"from": cids[i], "to": cids[i + 1], "type": e["type"], "doc": _route_doc(e.get("doc"), sp, code_only),
              "line": e.get("line")} for i, e in enumerate(edges)]
    return {"canonical_id": it["canonical_id"], "kind": it["label"], "name": it["name"], "path": it["path"],
            "distance": len(it["trace"]) - 1, "trace": it["trace"], "route": route,
            "evidence": _edge_evidence(edges, cids, sp, evidence_limit, code_only), "evidence_available": True}


def _impact(session, world, sp, code_only, with_documents, *, canonical_id, depth, limit, evidence_limit) -> dict:
    from .ingest import world_neo4j
    try:
        starts = world_neo4j.get_world_entities(session, [canonical_id], world, sp)
    except GraphQueryOverloadError as e:  # 起点の取得の時間切れ・天井も、空・内部障害にせず未完了として申告する（未解決は付けない）
        cov = Coverage()
        cov.add(graph_coverage.kind_of_overload(e.reason), graph_coverage.STAGE_ANCHOR)
        result = {"impact": [], "count": None, "coverage": cov.as_dict(omitted=None)}
        _flag_truncated(result, result["coverage"])
        return result
    if code_only:
        starts = [r for r in starts if r["label"] != "Document"]
    starts = [r for r in starts if not _sensitive_path(r["path"])]  # 秘匿名のファイルは存在しない扱い（存在を漏らさない）
    if not starts:
        return {"error": START_NOT_FOUND_ERROR, "canonical_id": canonical_id,
                "message": "この資料フォルダ・範囲に該当する識別子がありません（graph_resolve で確かめてください）"}
    start = starts[0]
    cov = Coverage()
    info: dict = {}
    try:
        raw = world_neo4j.world_impact(session, [canonical_id], world, sp, depth, info=info, detail=True)
    except GraphQueryOverloadError as e:
        cov.add(graph_coverage.kind_of_overload(e.reason), graph_coverage.STAGE_IMPACT)
        result = {"start": _node_view(start), "impact": [], "count": None,
                  "coverage": cov.as_dict(omitted=None, depth={"requested": depth, "truncated": None})}
        _flag_truncated(result, result["coverage"])
        return result
    truncated = info.get("depth_truncated", False)  # None＝判定できなかった（不明）
    if truncated is None:
        cov.add(info["depth_check_limit"], graph_coverage.STAGE_IMPACT)
    elif truncated:
        cov.add(graph_coverage.KIND_DEPTH, graph_coverage.STAGE_IMPACT)
    # 起点自身は影響先に数えない（循環で戻る経路）
    # 経路のどのノードも秘匿名のファイルに属さないものだけ返す（途中のノードの名前も出さない）
    raw = [it for it in raw if it["canonical_id"] != canonical_id
           and not any(_sensitive_path(p) for p in it["trace_paths"])]
    items = sorted((_impact_item(it, sp, evidence_limit, code_only) for it in raw), key=lambda x: (x["distance"], x["name"] or "", x["path"] or "",
                                                                    x["canonical_id"]))
    total = len(items)
    shown = items[:limit]
    omitted = total - len(shown)
    if omitted:
        cov.add(graph_coverage.KIND_RESULT_CAP, graph_coverage.STAGE_IMPACT)
    result: dict = {"start": _node_view(start), "impact": shown, "count": total,
                    "coverage": cov.as_dict(omitted=omitted if truncated is False else None,  # 深さの先が残る・不明なら件数は確定しない
                                            depth={"requested": depth, "truncated": truncated})}
    if not cov.has(graph_coverage.KIND_TIMEOUT, graph_coverage.KIND_ROW_CAP):
        # 起点の名前に一致する未解決の参照（影響の起点の解決と同じ完全一致）。起点の調べが時間切れ・天井なら付けない
        try:
            un = world_neo4j.read_unresolved(session, world, [start["name"]], sp, fold_case=False)
            un["items"] = [u for u in un["items"] if _route_doc(u.get("path"), None, code_only)]
            result["unresolved"] = un
        except GraphQueryOverloadError as e:
            graph_coverage.add_limit(result["coverage"], graph_coverage.kind_of_overload(e.reason),
                                     graph_coverage.STAGE_IMPACT)
    if with_documents:
        docs = world_neo4j.world_related_documents(
            session, [canonical_id] + [it["canonical_id"] for it in shown], world, sp, limit=RELATED_DOCS_LIMIT + 1)
        docs = [d for d in docs if not _sensitive_path(d["path"])
                and not _sensitive_path(d["_target_path"])]
        for d in docs:
            d.pop("_target_path")
        result["related_documents"] = docs[:RELATED_DOCS_LIMIT]
        if len(docs) > RELATED_DOCS_LIMIT:
            # 上限＋1 件までしか取らないので、省いた件数は分からない（null）。未完了として coverage にも載せる（stage `docs`）
            result["related_documents_omitted"] = None
            graph_coverage.add_limit(result["coverage"], graph_coverage.KIND_RESULT_CAP, graph_coverage.STAGE_DOCS)
            result["coverage"]["omitted"] = None
    _flag_truncated(result, result["coverage"])
    return result


# ---- 返却バイトの収まり ----

_FIELD_STAGE = {"candidates": graph_coverage.STAGE_ANCHOR, "impact": graph_coverage.STAGE_IMPACT}


def _cap_coverage(result: dict, stage: str, dropped: int) -> dict:
    """`result["coverage"]` の写しへ `result_cap`（`stage`）を足し、`omitted` へ削った件数を加える（元が不明なら不明のまま）。"""
    cov = {**result["coverage"], "limits": [dict(x) for x in result["coverage"]["limits"]]} \
        if "coverage" in result else Coverage().as_dict()
    prev = 0 if cov["complete"] else cov["omitted"]
    graph_coverage.add_limit(cov, graph_coverage.KIND_RESULT_CAP, stage)
    cov["omitted"] = None if prev is None else prev + dropped
    return cov


def fit_to_bytes(result: dict, max_bytes: int):
    """結果が `max_bytes` を超えるとき、`coverage`・`start`・`error_code` などの固定欄を残して末尾から削って収める。
    先に関連文書（`related_documents`）を削り（`result_cap` stage `docs`・`related_documents_omitted` を立てる）、それでも超えるときだけ
    一覧（`candidates`／`impact`）の末尾を削る（影響先を優先して残す。`result_cap` は一覧の stage）。
    収まらなければ `None`（呼び出し側が最終防衛線へ）。戻り値は `(result, clipped)`。
    """
    field = "candidates" if "candidates" in result else "impact" if "impact" in result else None
    if field is None or not isinstance(result.get(field), list):
        return None
    if json_bytes(result) <= max_bytes:
        return result, False
    result = dict(result)
    docs = result.get("related_documents")
    if isinstance(docs, list) and docs:
        kept_docs = list(docs)
        while kept_docs:
            kept_docs.pop()
            r = dict(result)
            r["related_documents"] = kept_docs
            prev = r.get("related_documents_omitted", 0)
            r["related_documents_omitted"] = None if ("related_documents_omitted" in r and prev is None) \
                else (prev or 0) + len(docs) - len(kept_docs)
            r["coverage"] = _cap_coverage(result, graph_coverage.STAGE_DOCS, 0)
            r["truncated"] = True
            if json_bytes(r) <= max_bytes:
                return r, True
        result = r  # 関連文書を全て削っても超える＝一覧も削る
    kept = list(result[field])
    total = len(kept)
    while True:
        r = dict(result)
        r[field] = kept
        r["coverage"] = _cap_coverage(result, _FIELD_STAGE[field], total - len(kept))
        r["truncated"] = True
        if json_bytes(r) <= max_bytes:
            return r, True
        if not kept:
            return None
        kept.pop()
