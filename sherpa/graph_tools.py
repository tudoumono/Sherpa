"""Codex の道具 `graph_resolve`（起点の候補を並べる）・`graph_impact`（識別子から構造の依存だけを逆向きにたどる）。

2 段で使う: `graph_resolve` で同名を含む候補（`canonical_id`・種別・所属パス・表示名・修飾名）を見て起点を選び、
`graph_impact` へ `canonical_id` を渡す。名前の曖昧さは呼び出し側が解く（`graph_neighbors` の名前の完全一致とは別の経路）。
`graph_impact` は構造の辺（COPIES・CONTAINS・INVOKES・ACCESSES）だけをたどり、言及（DOCUMENTS）は影響に数えず
関連文書として別に返す（層が both のときだけ）。データの流れ（FLOWS_TO・ファイル受け渡しなど）で届く先は影響に混ぜず `flow_candidates` に別に返す。層がソースに限定されている間も使える（コード同士の閉じた辺だけ・資料は一切返さない）。
未探索（時間切れ・件数の天井・深さ・返却件数）は `coverage`（`graph_coverage.py` の欄）で申告する。
契約: docs/design/interfaces.md「graph_resolve・graph_impact」・docs/proposals/2026-10-04-アナライザとグラフの改善.md S6・docs/proposals/2026-10-16-データフロー層.md §4。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import Counter, OrderedDict
from pathlib import Path

from . import graph_coverage, layer as layer_mod, scope
from .graph_coverage import Coverage
from .ingest import text_kind
from .ingest.model import NODE_LABELS
from .ingest.world_neo4j import FLOW_DEPTH_DEFAULT, GraphQueryOverloadError, GraphSchemaEraError

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
OUT_DIR_ENV = "SHERPA_MCP_TMP_DIR"   # 作業フォルダの `.tmp/`（絶対パス）。MCP サーバの起動側が渡す。無ければ全件のファイルは書かない
IMPACT_FILE_DIR = "graph_impact"
IMPACT_SUMMARY_FOLDERS_MAX = 30
FLOW_FILE_PREFIX = "flow"      # 流れの候補の全件ファイル名の接頭辞（影響先は `impact`）
FLOW_DEPTH_MAX = 10
GRAPH_PAGE_BYTES = 12000       # 1 応答の目標バイト数（Codex CLI の出力上限 約 40,000 バイト・サーバのバイト予算より小さく置く）
PAGE_PARTS = ("impact", "flow", "related_documents")   # ページ送りの対象（影響先・流れの候補・関連資料）
PAGE_TOKEN_TTL_S = 1800        # 続きの世代の記憶（本文は持たない）の期限
PAGE_TOKEN_MAX = 64            # 同じく件数の上限（古いものから捨てる）
PAGE_EXPIRED_ERROR = "graph_page_expired"
PAGE_MISMATCH_ERROR = "graph_page_mismatch"
GENERATION_MISMATCH_ERROR = "graph_generation_mismatch"

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


def run(name: str, args: dict, world: str, scope_paths, layer=None, max_bytes: int | None = None) -> dict:
    """`graph_resolve`／`graph_impact` を実行して結果の辞書を返す（層 docs の拒否は呼び出し側）。

    `GraphSchemaEraError`（旧世代のグラフ）は再送出する（呼び出し側が `graph_reingest_required` のツール結果へ変換する）。
    それ以外の Neo4j の障害は捕捉して `error_code`（接続系は `graph_unavailable`・他は `graph_internal_error`）で返す。
    `max_bytes`（省略可）はツール結果 1 件のバイト予算。`graph_impact` の 1 ページはこの 8 割と `GRAPH_PAGE_BYTES` の小さい方に収める。
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
                result = _impact(s, world, sp, code_only, layer in (None, "both"), _out_dir(), **parsed,
                                 page_bytes=page_bytes_for(max_bytes))
            if name == "graph_impact":
                if "exploration" not in result:   # ページを組む前に打ち切った応答だけ、ここで取り込み時の失敗を足す
                    _attach_plugin_failures(s, world, result, _FIELD_STAGE[field])
                _add_exploration(result)
            else:
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


def page_bytes_for(max_bytes: int | None) -> int:
    """`graph_impact` の 1 ページの目標バイト数（`GRAPH_PAGE_BYTES` と、サーバのバイト予算の 8 割の小さい方）。"""
    return GRAPH_PAGE_BYTES if not max_bytes else max(512, min(GRAPH_PAGE_BYTES, int(max_bytes * 0.8)))


def _exploration(cov: dict) -> dict:
    """`exploration`（指定条件内の探索を終えられたか）。転送の都合（`result_cap`＝続きのページが残る）は含めない。
    それは `transfer`（結果を最後まで渡せたか）が答える。"""
    stages = (graph_coverage.STAGE_IMPACT, graph_coverage.STAGE_FLOW, graph_coverage.STAGE_DOCS)
    limits = [lim for lim in cov["limits"]
              if not (lim["kind"] == graph_coverage.KIND_RESULT_CAP and lim.get("stage") in stages)]
    depth = cov.get("depth") if isinstance(cov.get("depth"), dict) else {}
    return {"complete": not limits, "limits": limits, "depth_truncated": depth.get("truncated")}


def _add_exploration(result: dict) -> None:
    """ページを組む前に打ち切った応答（起点・探索の過負荷）へ `exploration` を足す。"""
    if isinstance(result.get("coverage"), dict) and "error" not in result and "exploration" not in result:
        result["exploration"] = _exploration(result["coverage"])


def _out_dir() -> Path | None:
    raw = os.environ.get(OUT_DIR_ENV, "").strip()
    return Path(raw) if raw and os.path.isabs(raw) else None


def _attach_plugin_failures(session, world: str, result: dict, stage: str) -> None:
    """取り込み時に失敗した FW プラグインを `coverage.limits`（`plugin_failed`・プラグイン名つき）へ足す。`coverage` を持たない失敗応答には付けない。"""
    from .ingest import world_neo4j
    cov = result.get("coverage")
    if isinstance(cov, dict):
        graph_coverage.attach_plugin_failures(
            cov, lambda: world_neo4j.read_plugin_failures(session, world), stage,
            read_unparsed=lambda: world_neo4j.read_unparsed_sources(session, world))


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
    from .impact_service import IMPACT_MAX_DEPTH_ABS_MAX
    depth = _int_arg(args, "depth", IMPACT_DEPTH_DEFAULT, 1, IMPACT_MAX_DEPTH_ABS_MAX)
    limit = _int_arg(args, "limit", IMPACT_LIMIT_DEFAULT, 1, IMPACT_LIMIT_MAX)
    ev = _int_arg(args, "evidence_limit", EVIDENCE_LIMIT_DEFAULT, 0, EVIDENCE_LIMIT_MAX)
    flow = _int_arg(args, "flow_depth", FLOW_DEPTH_DEFAULT, 0, FLOW_DEPTH_MAX)
    if depth is None or limit is None or ev is None or flow is None:
        return _invalid("depth・limit・evidence_limit・flow_depth は整数で")
    offset = _offset_arg(args)
    if offset is None:
        return _invalid("offset は 0 以上の整数で（前の応答の transfer.next_offset をそのまま渡す）")
    part = str(args.get("part") or "impact").strip()
    if part not in PAGE_PARTS:
        return _invalid(f"part は {', '.join(PAGE_PARTS)} のいずれか")
    if part == "flow" and flow == 0:
        return _invalid("part=flow は flow_depth を 1 以上にしたときだけ使えます")
    gen = args.get("generation")
    if gen is not None and not isinstance(gen, str):
        return _invalid("generation は前の応答の generation をそのまま渡す文字列で")
    return {"canonical_id": cid, "depth": depth, "limit": limit, "evidence_limit": ev, "flow_depth": flow,
            "offset": offset, "part": part, "generation": (gen or "").strip() or None}


def _offset_arg(args: dict):
    """`offset` を 0 以上の整数として返す。未指定は 0・負数や整数でないものは `None`（丸めない＝続きの位置を取り違えない）。"""
    raw = args.get("offset")
    if raw is None or raw == "":
        return 0
    if isinstance(raw, bool):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(raw, float) and raw != v:
        return None
    return v if v >= 0 else None


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
    """影響先 1 件。`evidence_limit` が 0 のときは一覧用の軽い形（識別子・種別・名前・パス・段数と、経路の辺の種類を順に並べた `via`）にして、
    `trace`・`route`・`evidence` は付けない（根拠が要る対象は `evidence_limit` を上げて取る）。"""
    cids, edges = it["trace_cids"], it["edges"]
    if evidence_limit == 0:
        return {"canonical_id": it["canonical_id"], "kind": it["label"], "name": it["name"], "path": it["path"],
                "distance": len(it["trace"]) - 1, "via": [e["type"] for e in edges]}
    route = [{"from": cids[i], "to": cids[i + 1], "type": e["type"], "doc": _route_doc(e.get("doc"), sp, code_only),
              "line": e.get("line")} for i, e in enumerate(edges)]
    return {"canonical_id": it["canonical_id"], "kind": it["label"], "name": it["name"], "path": it["path"],
            "distance": len(it["trace"]) - 1, "trace": it["trace"], "route": route,
            "evidence": _edge_evidence(edges, cids, sp, evidence_limit, code_only), "evidence_available": True}


def _flow_candidate_item(it: dict, sp, evidence_limit: int, code_only=False) -> dict:
    """流れの候補 1 件（`_impact_item` と同じ形＋代表経路の `FLOWS_TO` の本数 `flow_edges`）。"""
    out = _impact_item(it, sp, evidence_limit, code_only)
    out["flow_edges"] = sum(1 for e in it["edges"] if e.get("type") == "FLOWS_TO")
    return out


def _impact_summary(items: list, flow_total: int | None = None) -> dict:
    """影響先全件の要約（総件数・種別別・フォルダ別（先頭のフォルダ）・段数別）。流れをたどったときは候補の総件数 `flow_total` も足す。"""
    def folder(path):
        return "(不明)" if not path else path.split("/", 1)[0] if "/" in path else "(直下)"

    folders = Counter(folder(it["path"]) for it in items)
    top = folders.most_common(IMPACT_SUMMARY_FOLDERS_MAX)
    by_folder = dict(sorted(top, key=lambda kv: (-kv[1], kv[0])))
    if len(folders) > len(top):
        by_folder["(その他)"] = sum(folders.values()) - sum(n for _, n in top)
    by_distance = Counter(it["distance"] for it in items)
    out = {"total": len(items), "by_kind": dict(sorted(Counter(it["kind"] for it in items).items())),
           "by_folder": by_folder, "by_distance": {str(k): by_distance[k] for k in sorted(by_distance)}}
    if flow_total is not None:
        out["flow_total"] = flow_total
    return out


def _write_impact_file(out_dir: Path, key: str, items: list, prefix: str = "impact") -> str | None:
    """影響先（`prefix` が `flow` なら流れの候補）の全件を 1 行 1 件の JSON Lines で `<out_dir>/graph_impact/` に書き、作業フォルダからの相対パスを返す。
    書けなければ None（結果の本体は先頭の件数だけで返す）。`out_dir` はモデルのシェルから書ける場所なので、
    置き場がシンボリックリンクなら書かず、一時ファイルを新規作成（O_EXCL・O_NOFOLLOW）して置き換える。
    """
    try:
        real_out = os.path.realpath(out_dir)
        target = Path(real_out) / IMPACT_FILE_DIR
        if os.path.islink(target):
            return None
        target.mkdir(exist_ok=True)
        if os.path.realpath(target) != str(target):
            return None
        name = f"{prefix}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}.jsonl"
        tmp = target / f".{name}.{os.getpid()}.part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            os.replace(tmp, target / name)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return f"{Path(real_out).name}/{IMPACT_FILE_DIR}/{name}"
    except OSError as e:
        _log.warning("graph_impact: 全件のファイルを書けませんでした: %s errno=%s", type(e).__name__, e.errno)
        return None


# ---- ページ送り ----
# 設計: docs/05-グラフ語彙.md §6「graph_impact のページ送り」。結果の本文はサーバに保持しない（呼ぶたびに同じ探索をやり直し、安定した並びの
# `offset` 以降を返す）。続きの取り違えは世代つきの印（`generation`）で検出する。記憶するのは世代の文字列だけ（期限・件数の上限つき）。

_page_tokens: "OrderedDict[str, tuple[str, float]]" = OrderedDict()   # 範囲の印 → (世代, 記録時刻)


def _scope_mark(world, sp, code_only, canonical_id, depth, flow_depth) -> str:
    """続きの印に結ぶ範囲（資料フォルダ・検索範囲・層・起点・深さ）。別の範囲・別の条件の続きを受け付けないための印。"""
    raw = json.dumps([world, canonical_id, depth, flow_depth, bool(code_only), sp], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _remember_generation(mark: str, generation: str) -> None:
    _page_tokens[mark] = (generation, time.monotonic())
    _page_tokens.move_to_end(mark)
    while len(_page_tokens) > PAGE_TOKEN_MAX:
        _page_tokens.popitem(last=False)


def _recall_generation(mark: str) -> str | None:
    hit = _page_tokens.get(mark)
    if hit is None:
        return None
    if time.monotonic() - hit[1] > PAGE_TOKEN_TTL_S:
        _page_tokens.pop(mark, None)
        return None
    return hit[0]


def _check_continuation(token: str | None, offset: int, mark: str, current: str) -> dict | None:
    """続きの呼び出しの検査。通れば `None`、通らなければ正常な空結果と区別できる `error` つきの辞書。
    `token`（前の応答の `generation`）があればそれを、無ければ同じ範囲で最後に返した世代の記憶を使う（`offset` が 0 のときは検査しない）。"""
    if token:
        gen, _, tok_mark = token.rpartition(".")
        if tok_mark != mark:
            return {"error": PAGE_MISMATCH_ERROR,
                    "message": "generation は別の起点・深さ・範囲の応答のものです。offset を 0 にして generation を付けずに呼び直してください"}
        expected = gen
    elif offset > 0:
        expected = _recall_generation(mark)
        if expected is None:
            return {"error": PAGE_EXPIRED_ERROR,
                    "message": "続きの元が見つからないか期限切れです。offset を 0 にして呼び直してください"}
    else:
        return None
    if expected != current:
        return {"error": GENERATION_MISMATCH_ERROR,
                "message": "取り込みが更新され、前のページとは別の世代になりました。offset を 0 にして generation を付けずに呼び直し、"
                           "最初から取り直してください（世代の違うページは合わせない）"}
    return None


def _transfer_info(part: str, total: int, offset: int, returned: int) -> dict:
    nxt = offset + returned
    more = nxt < total
    return {"part": part, "total": total, "offset": offset, "returned": returned,
            "next_offset": nxt if more else None, "has_more": more, "complete": not more}


def _clip_str(v, n: int):
    return v if not isinstance(v, str) or len(v) <= n else v[:n]


def _slim_item(it: dict, budget: int) -> dict:
    """1 件だけで `budget` を超える要素を、黙って捨てずに軽い形にして返す（根拠の `sources` →経路・根拠 →文字列の切り詰めの順に落とす）。
    落としたことは `item_slimmed` で示す（詳細は evidence_limit を下げるか、その canonical_id を graph_neighbors・原本で確かめる）。"""
    if "evidence" in it:
        lighter = {**it, "evidence": [{k: v for k, v in e.items() if k not in ("sources", "sources_overflow_count")}
                                      for e in it["evidence"]], "item_slimmed": "sources"}
        if json_bytes(lighter) + 1 <= budget:
            return lighter
    keep = ("canonical_id", "kind", "name", "path", "distance", "via", "line", "target", "flow_edges")
    out = {k: it[k] for k in keep if k in it}
    out["item_slimmed"] = "route"
    if json_bytes(out) + 1 > budget:
        n = max(16, budget // 6)
        out = {k: _clip_str(v, n) if isinstance(v, str) else v for k, v in out.items()}
        out["item_slimmed"] = "clipped"
    return out


def _take_page(items: list, offset: int, limit: int, budget: int) -> list:
    """`items[offset:]` の先頭から、件数 `limit` と 1 応答のバイト `budget` に収まる分だけを取る。1 件目は大きくても軽い形で必ず返す。"""
    page: list = []
    used = 0
    for it in items[offset:offset + limit]:
        size = json_bytes(it) + 1
        if used + size > budget:
            if page:
                break
            it = _slim_item(it, budget)
            size = json_bytes(it) + 1
        page.append(it)
        used += size
    return page


def _doc_sort_key(d: dict):
    return (d["path"] or "", d["target"]["canonical_id"], d["canonical_id"], d.get("via") or "", d.get("line") or 0)


def _impact(session, world, sp, code_only, with_documents, out_dir=None, *, canonical_id, depth, limit,
            evidence_limit, flow_depth=0, offset=0, part="impact", generation=None,
            page_bytes=GRAPH_PAGE_BYTES) -> dict:
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
    mark = _scope_mark(world, sp, code_only, canonical_id, depth, flow_depth)
    try:
        gen_before = world_neo4j.read_generation(session, world) or "none"
    except GraphQueryOverloadError as e:
        cov = Coverage()
        cov.add(graph_coverage.kind_of_overload(e.reason), graph_coverage.STAGE_ANCHOR)
        result = {"start": _node_view(start), "impact": [], "count": None, "coverage": cov.as_dict(omitted=None)}
        _flag_truncated(result, result["coverage"])
        return result
    bad = _check_continuation(generation, offset, mark, gen_before)
    if bad is not None:
        return bad
    cov = Coverage()
    info: dict = {}
    try:
        raw = world_neo4j.world_impact(session, [canonical_id], world, sp, depth, info=info, detail=True,
                                       **({"flow_depth": flow_depth} if flow_depth > 0 else {}))
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
    flow_truncated = False
    if flow_depth > 0:
        flow_truncated = info.get("flow_depth_truncated", False)
        if flow_truncated is None:
            cov.add(info["flow_depth_check_limit"], graph_coverage.STAGE_FLOW)
        elif flow_truncated:
            cov.add(graph_coverage.KIND_DEPTH, graph_coverage.STAGE_FLOW)
    # 起点自身は影響先に数えない（循環で戻る経路）
    # 経路のどのノードも秘匿名のファイルに属さないものだけ返す（途中のノードの名前も出さない）
    raw = [it for it in raw if it["canonical_id"] != canonical_id
           and not any(_sensitive_path(p) for p in it["trace_paths"])]
    structure = [it for it in raw if it.get("origin") != "flow"]
    flow_raw = [it for it in raw if it.get("origin") == "flow"]

    def _sorted(items):
        return sorted(items, key=lambda x: (x["distance"], x["name"] or "", x["path"] or "", x["canonical_id"]))

    items = _sorted(_impact_item(it, sp, evidence_limit, code_only) for it in structure)
    flow_items = _sorted(_flow_candidate_item(it, sp, evidence_limit, code_only) for it in flow_raw)
    docs: list = []
    docs_overload = None
    if with_documents and (part == "related_documents" or (part == "impact" and offset == 0)):
        try:
            docs = world_neo4j.world_related_documents(
                session, [canonical_id] + [it["canonical_id"] for it in items], world, sp)
        except GraphQueryOverloadError as e:
            docs_overload = e
        docs = [d for d in docs if not _sensitive_path(d["path"]) and not _sensitive_path(d["_target_path"])]
        for d in docs:
            d.pop("_target_path")
        docs.sort(key=_doc_sort_key)
    lists = {"impact": items, "flow": flow_items if flow_depth > 0 else [], "related_documents": docs}
    if offset > len(lists[part]):
        return {"error": INVALID_ARGS_ERROR, "total": len(lists[part]),
                "message": f"offset {offset} が {part} の総数 {len(lists[part])} を超えています"}
    gen_after = world_neo4j.read_generation(session, world) or "none"
    if gen_after != gen_before:   # 探索の途中で取り込みが更新された＝この応答の中身は 1 つの世代に決められない
        return {"error": GENERATION_MISMATCH_ERROR,
                "message": "探索の途中で取り込みが更新されました。offset を 0 にして generation を付けずに呼び直してください"}
    token = f"{gen_before}.{mark}"
    _remember_generation(mark, gen_before)

    result_file = flow_result_file = None
    if out_dir is not None and part == "impact" and offset == 0:
        if len(items) > limit:
            key = json.dumps([world, canonical_id, depth, evidence_limit, code_only, sp], ensure_ascii=False)
            result_file = _write_impact_file(out_dir, key, items)
        if len(lists["flow"]) > limit:
            key = json.dumps([world, canonical_id, depth, flow_depth, evidence_limit, code_only, sp], ensure_ascii=False)
            flow_result_file = _write_impact_file(out_dir, key, lists["flow"], FLOW_FILE_PREFIX)

    unresolved = None
    if part == "impact" and offset == 0 and not cov.has(graph_coverage.KIND_TIMEOUT, graph_coverage.KIND_ROW_CAP):
        # 起点の名前に一致する未解決の参照（影響の起点の解決と同じ完全一致）。起点の調べが時間切れ・天井なら付けない
        try:
            unresolved = world_neo4j.read_unresolved(session, world, [start["name"]], sp, fold_case=False)
            unresolved["items"] = [u for u in unresolved["items"] if _route_doc(u.get("path"), None, code_only)]
        except GraphQueryOverloadError as e:
            cov.add(graph_coverage.kind_of_overload(e.reason), graph_coverage.STAGE_IMPACT)
    if docs_overload is not None:
        cov.add(graph_coverage.kind_of_overload(docs_overload.reason), graph_coverage.STAGE_DOCS)
    graph_coverage.attach_plugin_failures(
        cov, lambda: world_neo4j.read_plugin_failures(session, world), graph_coverage.STAGE_IMPACT,
        read_unparsed=lambda: world_neo4j.read_unparsed_sources(session, world))

    total_impact, total_flow = len(items), len(lists["flow"])
    all_known = truncated is False and flow_truncated is False   # 深さの先が残る・不明なら件数は確定しない
    first = part == "impact" and offset == 0
    pages: dict[str, list] = {p: [] for p in PAGE_PARTS}
    included = ["impact"] + (["flow"] if flow_depth > 0 else []) + (["related_documents"] if with_documents else []) \
        if first else [part]

    def assemble() -> dict:
        """いまの `pages` から応答を組む（ページの範囲・coverage・転送の状態は `pages` の長さから毎回作り直す）。"""
        starts_at = {p: (offset if p == part else 0) for p in included}
        info_by = {p: _transfer_info(p, len(lists[p]), starts_at[p], len(pages[p])) for p in included}
        c = cov.copy()
        remaining = 0
        stage_of = {"impact": graph_coverage.STAGE_IMPACT, "flow": graph_coverage.STAGE_FLOW,
                    "related_documents": graph_coverage.STAGE_DOCS}
        for p in included:
            if info_by[p]["has_more"]:
                c.add(graph_coverage.KIND_RESULT_CAP, stage_of[p])
                remaining += info_by[p]["total"] - info_by[p]["offset"] - info_by[p]["returned"]
        res: dict = {"start": _node_view(start), "count": total_impact, "generation": token}
        if part == "impact":
            res["impact"] = pages["impact"]
        if first:
            res["summary"] = _impact_summary(items, total_flow if flow_depth > 0 else None)
        res["coverage"] = c.as_dict(omitted=remaining if all_known else None,
                                    depth={"requested": depth, "truncated": truncated},
                                    flow={"requested": flow_depth, "truncated": flow_truncated} if flow_depth > 0 else None)
        if flow_depth > 0 and "flow" in included:
            res["flow_candidates"] = pages["flow"]
        if part == "flow":
            res["flow_total"] = total_flow
        tr = dict(info_by[part])
        tr["complete"] = all(info_by[p]["complete"] for p in included)
        if first:
            others = {p: {k: info_by[p][k] for k in ("total", "returned", "next_offset")}
                      for p in included if p != part and info_by[p]["has_more"]}
            if others:
                tr["other_parts"] = others
        res["transfer"] = tr
        res["exploration"] = _exploration(res["coverage"])
        if evidence_limit == 0:
            res["list_form"] = "light"
        if "related_documents" in included:
            res["related_documents"] = pages["related_documents"]
            if info_by["related_documents"]["has_more"]:
                res["related_documents_omitted"] = (info_by["related_documents"]["total"]
                                                    - info_by["related_documents"]["returned"])
        if result_file:
            res["result_file"] = result_file
        if flow_result_file:
            res["flow_result_file"] = flow_result_file
        if unresolved is not None:
            res["unresolved"] = unresolved
        _flag_truncated(res, res["coverage"])
        return res

    # ① 固定の欄の大きさを測り、残りのバイトを関連資料・流れの候補・影響先の順に割り当てる ② 実際に組んだ応答が収まるまで末尾を削る
    avail = max(0, page_bytes - json_bytes(assemble()) - 64)
    if first:
        # 関連資料に 1/6、流れの候補に残りの 1/3、影響先にその残りを割り当てる。使い切らなかった分は、まだ続きのある流れの候補・関連資料へ回す
        shares = {"related_documents": avail // 6}
        shares["flow"] = (avail - shares["related_documents"]) // 3 if "flow" in included else 0
        for p in ("related_documents", "flow"):
            if p in included and lists[p]:
                pages[p] = _take_page(lists[p], 0, RELATED_DOCS_LIMIT if p == "related_documents" else limit, shares[p])
        used = {p: json_bytes(pages[p]) for p in ("related_documents", "flow")}
        pages["impact"] = _take_page(items, 0, limit, avail - sum(used.values()))
        spare = avail - sum(used.values()) - json_bytes(pages["impact"])
        for p in ("flow", "related_documents"):
            cap = RELATED_DOCS_LIMIT if p == "related_documents" else limit
            if spare > 0 and p in included and len(pages[p]) < min(len(lists[p]), cap):
                pages[p] = _take_page(lists[p], 0, cap, shares[p] + spare - 64)
                spare = avail - sum(json_bytes(pages[q]) for q in PAGE_PARTS)
    else:
        pages[part] = _take_page(lists[part], offset, limit, avail)
    result = assemble()
    order = ["related_documents", "flow", "impact"]   # 収まらないときは、要求された一覧より先に付随の一覧を削る
    while json_bytes(result) > page_bytes:
        victim = next((p for p in order if p in included and len(pages[p]) > (1 if p == part else 0)), None)
        if victim is None:
            break
        pages[victim].pop()
        result = assemble()
    # 要求された一覧が 1 件だけで超えるときは、その 1 件を上限に収まるまで軽くする（捨てない）
    room = page_bytes - json_bytes(result) + json_bytes(pages[part][0]) if pages[part] else 0
    while json_bytes(result) > page_bytes and pages[part] and room > 64:
        room //= 2
        pages[part][0] = _slim_item(lists[part][offset], room)
        result = assemble()
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


def _resync_transfer(r: dict, part: str, total: int, returned: int) -> None:
    """末尾を削ったあとの `transfer` を、実際に返す件数に合わせる（`next_offset` が先頭からの連続した続きを指すように）。"""
    tr = r.get("transfer")
    if not isinstance(tr, dict):
        return
    tr = dict(tr)
    if tr.get("part") == part:
        tr.update(_transfer_info(part, total, tr.get("offset", 0), returned))
    else:
        others = dict(tr.get("other_parts") or {})
        others[part] = {"total": total, "returned": returned, "next_offset": returned}
        tr["other_parts"] = others
    tr["complete"] = False
    r["transfer"] = tr


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
            if r["related_documents_omitted"] is not None:
                _resync_transfer(r, "related_documents", len(kept_docs) + r["related_documents_omitted"], len(kept_docs))
            r["coverage"] = _cap_coverage(result, graph_coverage.STAGE_DOCS, 0)
            r["truncated"] = True
            if json_bytes(r) <= max_bytes:
                return r, True
        result = r  # 関連文書を全て削っても超える＝一覧も削る
    flow = result.get("flow_candidates")
    if isinstance(flow, list) and flow:
        kept_flow = list(flow)
        while kept_flow:
            kept_flow.pop()
            r = dict(result)
            r["flow_candidates"] = kept_flow
            _resync_transfer(r, "flow", r.get("flow_total") or (r.get("summary") or {}).get("flow_total") or len(flow),
                             len(kept_flow))
            r["coverage"] = _cap_coverage(result, graph_coverage.STAGE_FLOW, len(flow) - len(kept_flow))
            r["truncated"] = True
            if json_bytes(r) <= max_bytes:
                return r, True
        result = r  # 流れの候補を全て削っても超える＝影響先も削る（流れの候補は影響先より先に削る）
    kept = list(result[field])
    total = len(kept)
    while True:
        r = dict(result)
        r[field] = kept
        if field == "impact":
            _resync_transfer(r, "impact", r.get("count") if isinstance(r.get("count"), int) else total, len(kept))
        r["coverage"] = _cap_coverage(result, _FIELD_STAGE[field], total - len(kept))
        r["truncated"] = True
        if json_bytes(r) <= max_bytes:
            return r, True
        if not kept:
            return None
        kept.pop()
