"""エンジン分離検索＋RRF融合（共通層・docs/proposals/2026-07-07-外部API化とDify.md E2）。

3プリミティブ（keyword=ES BM25 / vector=ES 純kNN / graph=Neo4j 影響たどり）を分離し、
融合は RRF 固定。**共有 KB のみ**（個人 workspace・personal テーブルへの参照は禁止・CLAUDE.md 契約）。
`sherpa.ext_api` から呼ぶ。チャット側の乗せ替えは本スライスでは行わない（起案どおり）。

**このファイルは `sherpa.api`・`sherpa.agents`・`sherpa.chat_service`・`sherpa.chat_router`・
`sherpa.grep_tool` を import しない**（プロセス分離可能な境界・共有KBのみの契約）。
"""
from __future__ import annotations

from contextlib import contextmanager

from . import documents, es_index, rag_parent_return
from . import scope as scope_mod
from .impact_service import IMPACT_MAX_DEPTH, run_impact

ENGINES = ("keyword", "vector", "graph")
DEFAULT_ENGINES = ("keyword", "vector")     # 決定済み: 既定は ES 1往復系のみ・graph は明示 opt-in
RRF_K = 60                                   # RRF 定数（標準値・固定。チューニングは weights で）
_SNIPPET_LEN = 240                           # es_index の fragment_size と揃える
_JUDGEMENT_FACTOR = {"sure": 1.0, "review": 0.6, "presumed": 0.3}   # graph の RRF 寄与係数（D5）
_MAX_PATHS_PER_HIT = 5                        # 同一 key に複数 graph item が落ちた時の paths 上限
_JUDGE_RANK = {"sure": 0, "review": 1, "presumed": 2}   # judgement の優劣（小さいほど良い）

DEGRADE_REASONS = frozenset({
    "es_unavailable", "es_query_failed", "embedding_not_configured",
    "embedding_cloud_unavailable",  # RV1（FBK-1）: A7 で明示選択したクラウドの埋め込みが解決できない
    "hybrid_query_failed",  # RV3（FBK-1）: hybrid 自体が失敗し BM25 は成功（hits は空でない）
    "vector_feature_mismatch", "query_embed_failed",
    "neo4j_unavailable", "graph_query_failed",
    "graph_reingest_required",  # RV是正（rv-periphery #12）: 旧世代グラフ（GraphSchemaEraError）
})


def search(world: str, query: str, engines=None, k: int = 10,
           scope_paths=None, weights=None, settings: dict | None = None, depth: int = IMPACT_MAX_DEPTH,
           root=None, strict: bool = False, layer=None) -> dict:
    """公開エントリ。返値 `{hits: [...], engines_used: [...], degraded: [{engine, reason}]}`。

    - engines: ENGINES の部分集合（None→DEFAULT_ENGINES）。順序・重複は正規化（ENGINES 順・一意化）。
    - depth: graph エンジンの影響たどりの深さ（`run_impact` へ素通し）。keyword/vector は
      消費しない（無視）。
    - layer（省略可・`"docs"|"code"|"both"`・既定 `None`＝`"both"`）: 探す対象（調べ方ブロック §3.4）。
      keyword/vector（ES）にのみ適用する。graph（Neo4j 影響たどり）は言及エッジ（DOCUMENTS via=mention）が
      Document とコードを木を跨いで繋ぐため非適用（§3.5・裁定1）——`_search_graph` へは渡さない。
    - root（省略可）: 呼び出し側が strict resolver で既に world root を解決済みなら渡す。
      渡された場合は実在フィルタ（`documents.world_rel_set`）がそれを使い、`worlds.world_dir()` を
      再度呼ばない（preflight 後の再解決を避ける）。省略時は従来どおり内部で解決する。
    - strict: `root` 指定時のみ意味を持つ。`scope_infer.safe_files` の OSError を re-raise させる
      （外部 API 経路が「見えなかった」を「無かった」に取り違えないため・呼び出し側が 503 にする）。
    - scope_paths は scope_mod.normalize_scope_paths で正規化（全エンジン共通フィルタ・鏡モデル）。
    - world の存在検証・scope の妥当性検証は**呼び出し側（ext_api）**の責務（本関数は検証済み前提）。
    - 各エンジンは (hits, reason|None) を返す。reason あり→degraded に積み hits は空扱い。
    - engines_used = degrade しなかったエンジン（0件ヒットでも used に入る）。
    - **実在フィルタ**（R3-S2）: `chat_service._es_hits` と同型（`documents.world_rel_set(world)` を
      1回だけ作り、doc_id 付きヒットのうち現 world に実在しないものを落とす）。削除直後の窓で
      ES/Neo4j 索引が古いまま存在しない doc を返さないため。doc_id が無いヒット（純概念グラフノード）は
      対象外＝素通り。

      **既知の残存ギャップ**（Codex R3 finding #8・partial）: このフィルタは**パスの存在**しか見ない。
      rebind で同じ world_id が別 root（A→B）に差し替わり、A/B 双方に**同じ相対パス**（例 `docs/spec.md`）が
      在る場合、B への切替後に ES 再索引（`worker._run_locked` 末尾の best-effort `es_index.index_world`）が
      未完了/失敗のまま残っていると、A の古い ES ヒットが「パスは B にも実在する」という理由でこのフィルタを
      通過し、B の現在の内容ではなく A の古い内容が返り得る。`documents` 台帳（`world_rel_set`）にも ES の
      per-hit 結果（`_parse_hits`＝`{doc_id, line, text, score, ext}`）にも**内容署名（content_sig）は無く**
      世界単位の `es_index._index_meta(world).content_sig`（`needs_reindex` 用）のみ＝この関数からは
      per-hit の鮮度判定ができない。フル修正には世代ID/per-doc content_sig（`docs/proposals/
      2026-07-13-横断レビュー対応.md` §2 が「世代ID＋staging＋active pointer のフル導入」を過剰投資として
      不採用と判断済み）が要り、R3 の scope 外。同一 world への rebind で同名パスの内容が入れ替わる状況が
      許容できないほど頻発するなら、per-doc content_sig の追加を再検討する候補として残す。
    """
    sel = [e for e in ENGINES if e in set(engines or DEFAULT_ENGINES)]   # 正規化（決定的順序）
    sp = scope_mod.normalize_scope_paths(scope_paths)
    w = {e: float((weights or {}).get(e, 1.0)) for e in sel}
    valid = (documents.world_rel_set(world, root=root, strict=strict) if root is not None
             else documents.world_rel_set(world))
    per_engine, degraded = {}, []
    runners = {"keyword": _search_keyword, "vector": _search_vector, "graph": _search_graph}
    for e in sel:
        # graph だけ depth を追加で渡す（layer は渡さない＝非適用・§3.5）。keyword/vector は layer を渡す。
        if e == "graph":
            hits, reason = runners[e](world, query, sp, k, settings, depth)
        else:
            hits, reason = runners[e](world, query, sp, k, settings, layer)
        if reason:
            degraded.append({"engine": e, "reason": reason})
        else:
            hits = _filter_existing(hits, valid)
            per_engine[e] = _dedupe_by_key(hits)[:k]    # エンジン内は doc キーで先勝ち dedupe
    fused = fuse_rrf(per_engine, w, k)
    return {"hits": fused, "engines_used": sorted(per_engine.keys(), key=ENGINES.index),
            "degraded": degraded}


def _filter_existing(hits: list, valid: set) -> list:
    """`doc_id` を持つヒットのうち現 world に実在しないものを落とす（doc_id 無しは素通り）。"""
    return [h for h in hits if not h.get("doc_id") or h["doc_id"] in valid]


def _dedupe_by_key(hits: list) -> list:
    """`hit["key"]` で先勝ち dedupe（エンジン内の重複排除）。"""
    seen: set = set()
    out = []
    for h in hits:
        if h["key"] in seen:
            continue
        seen.add(h["key"])
        out.append(h)
    return out


# ==== 各エンジン（内部関数・全て (hits, reason|None) を返す）====
#
# 共通ヒット中間形（dict・snake_case）:
#   {key, doc_id, path, line, snippet, engine_score, judgement, paths}
# - key: マージキー。doc_id があれば doc_id（=rel_path・鏡モデルの同一性=パス）。
#   無ければ f"graph:{label}:{name}"。
# - engine_score: エンジン固有スコア（参考値・融合には使わない。RRF は rank のみ）。

def _search_keyword(world, query, sp, k, settings, layer=None):
    """ES BM25（kuromoji）。既存 es_index.search(vector=False) をそのまま使う（起案どおり）。

    RV3（FBK-1・2026-09-01）: `vector=False` でも `es_index.search()` は BM25 自体の実クエリ失敗を
    `es_query_failed` として返しうる（埋め込み関連の reason だけが `None` に潰れるわけではない）。
    以前はここで reason を一律 `None` に捨てていたため、keyword-only 障害が「正常 0 件・degraded
    なし」に化けていた——`search()` 呼び出し元の `degraded` 集計（本ファイル `search()` 参照）へ
    そのまま伝播させる。
    """
    if not es_index.available():
        return [], "es_unavailable"
    hits, reason = es_index.search(world, query, scope_paths=sp, k=k, settings=settings, vector=False,
                                   layer=layer)
    hits = rag_parent_return.apply_to_hits(world, hits)
    return [_es_hit(h) for h in hits], reason


def _search_vector(world, query, sp, k, settings, layer=None):
    """ES 純 kNN（BM25 を混ぜない・es_index.search_knn_only を使う）。"""
    hits, reason = es_index.search_knn_only(world, query, scope_paths=sp, k=k, settings=settings,
                                            layer=layer)
    hits = rag_parent_return.apply_to_hits(world, hits)
    return [_es_hit(h) for h in hits], reason


def _es_hit(h) -> dict:
    out = {"key": h["doc_id"], "doc_id": h["doc_id"], "path": h["doc_id"],
           "line": h.get("line"), "snippet": (h.get("text") or "")[:_SNIPPET_LEN],
           "engine_score": h.get("score"), "judgement": None, "paths": None}
    # CITE-1（§3.3/§3.4 の非agentic 展開）: 親返しが full/region へ拡張できた場合だけ加算的に
    # `tier`/`text` を付与する（既存の `snippet`＝240字クリップは不変・Dify 等の既存消費者を壊さない）。
    if h.get("tier"):
        out["tier"] = h["tier"]
        out["text"] = h.get("text")
    return out


@contextmanager
def _neo4j_session():
    """search_service 専用の Neo4j セッション（`sherpa.api.neo4j_session` は import できない＝循環）。

    接続情報は `world_neo4j._env()` と同一（NEO4J_URI/USER/PASSWORD・既定 bolt://localhost:7687）。
    """
    from neo4j import GraphDatabase

    from .ingest.world_neo4j import _env
    e = _env()
    drv = GraphDatabase.driver(e["uri"], auth=(e["user"], e["pw"]),
                               notifications_min_severity="OFF")
    try:
        with drv.session() as s:
            yield s
    finally:
        drv.close()


def _search_graph(world, query, sp, k, settings, depth=IMPACT_MAX_DEPTH):
    """語→ノード照合→近傍展開→文書＋経路。impact の構造たどり＋presumed フォールバックを
    そのまま流用（run_impact＝resolve_world_entity→world_impact→presumed fallback・読むだけ）。

    depth: 影響たどりの深さ（呼び出し元から素通し・既定は `impact_service.IMPACT_MAX_DEPTH`）。
    """
    from neo4j.exceptions import AuthError, ServiceUnavailable

    from .ingest.world_neo4j import GraphSchemaEraError
    try:
        with _neo4j_session() as s:
            result = run_impact(s, query, world, scope_prefixes=(sp or None), depth=depth)
    except (ServiceUnavailable, AuthError, OSError):
        return [], "neo4j_unavailable"
    except GraphSchemaEraError:
        # RV是正（rv-periphery #12・2026-09-05）: 旧世代グラフは他の Neo4j 失敗と同じ generic
        # 理由（`graph_query_failed`）に丸めず、閉じた理由を返す（呼び出し元は再取り込み案内へ
        # 分けて表示できる）。
        return [], "graph_reingest_required"
    except Exception:
        return [], "graph_query_failed"
    return _graph_hits(result, k), None


# ==== graph の run_impact 結果 → ヒット形のマッピング（§4.3）====

def _graph_item_hit(item: dict) -> dict:
    """items[]（構造的な影響・全件同格・K12）を共通ヒット形へ。"""
    name = item.get("name")
    label = item.get("label")
    category = item.get("category") or label
    path = item.get("path")
    evidence = item.get("evidence") or []
    doc_id = path or (evidence[0].get("doc") if evidence else None)
    key = doc_id if doc_id else f"graph:{label}:{name}"
    trace = item.get("trace") or []
    snippet = f"{category}「{name}」"
    if trace:
        snippet += " 経路: " + " → ".join(trace)
    return {"key": key, "doc_id": doc_id, "path": path, "line": None,
            "snippet": snippet[:_SNIPPET_LEN], "engine_score": None, "judgement": None,
            "paths": [{"nodes": trace or [], "edges": evidence or [], "judgement": None}]}


def _presumed_item_hit(item: dict) -> dict:
    """presumed[]（●確実が0件の時の資料からの推定）を共通ヒット形へ。"""
    name = item.get("name")
    label = item.get("label")
    category = item.get("category") or label
    path = item.get("path")
    evidence = item.get("evidence") or []
    ev0 = evidence[0] if evidence else {}
    doc_id = path or ev0.get("doc")
    key = doc_id if doc_id else f"graph:{label}:{name}"
    quote = ev0.get("quote") or ""
    snippet = f"{category}「{name}」（判定: 推定）資料根拠: {quote}"
    return {"key": key, "doc_id": doc_id, "path": path, "line": None,
            "snippet": snippet[:_SNIPPET_LEN], "engine_score": None, "judgement": "presumed",
            "paths": [{"nodes": [name], "edges": [{"type": "PRESUMED", "doc": ev0.get("doc"),
                                                    "line": ev0.get("line")}],
                      "judgement": "presumed"}]}


def _merge_graph_item(merged: dict, order: list, h: dict) -> None:
    """同一 key に複数 item が落ちる場合: 先勝ちで1ヒットに統合し、後続の paths を追記（上限5経路/ヒット）。
    judgement はヒット内で最良のもの（sure > review > presumed）を採用。
    """
    key = h["key"]
    if key not in merged:
        merged[key] = h
        order.append(key)
        return
    m = merged[key]
    m["paths"] = ((m["paths"] or []) + (h.get("paths") or []))[:_MAX_PATHS_PER_HIT]
    if _JUDGE_RANK.get(h.get("judgement"), 9) < _JUDGE_RANK.get(m.get("judgement"), 9):
        m["judgement"] = h["judgement"]


def _graph_hits(result: dict, k: int) -> list:
    """`run_impact` 結果（items[] は sure→review 順にソート済み・presumed[] はその後ろ）を
    共通ヒット形へ変換し、同一 key を統合したうえで `[:k]` に切る（§4.3）。
    """
    merged: dict = {}
    order: list = []
    for item in result.get("items") or []:
        _merge_graph_item(merged, order, _graph_item_hit(item))
    for item in result.get("presumed") or []:
        _merge_graph_item(merged, order, _presumed_item_hit(item))
    return [merged[key] for key in order][:k]


# ==== E2c: RRF 融合 ====

def fuse_rrf(per_engine: dict, weights: dict, k: int) -> list:
    """RRF（Reciprocal Rank Fusion）。決定的・スコア尺度非依存。

    score(hit) = Σ_e  weights[e] * jf(hit) / (RRF_K + rank_e)
      - rank_e: エンジン e 内の 1-based 順位（エンジン内 doc dedupe 後）
      - jf: graph 由来の寄与のみ _JUDGEMENT_FACTOR[judgement]（keyword/vector は 1.0）
      - RRF_K = 60（固定）
    マージ: key（doc_id または graph:label:name）で統合。sources={engine: rank} を全員に記録。
    snippet/line の採用優先: keyword > vector > graph（テキスト断片が実用的な順・固定・ENGINES の走査順で担保）。
    paths は graph 由来のものだけ合流。engine_score は破棄（sources で十分・尺度が違うため出さない）。
    並び: (-score, key) — 完全決定的（同点は key 辞書順）。返値は上位 k 件。
    """
    merged: dict = {}
    for e in ENGINES:                                   # 固定順で走査＝snippet 優先順を担保
        for rank, h in enumerate(per_engine.get(e, []), start=1):
            jf = _JUDGEMENT_FACTOR.get(h.get("judgement"), 1.0) if e == "graph" else 1.0
            contrib = weights.get(e, 1.0) * jf / (RRF_K + rank)
            m = merged.setdefault(h["key"], {"doc_id": h["doc_id"], "path": h["path"],
                                             "line": None, "snippet": "", "score": 0.0,
                                             "sources": {}, "paths": None, "judgement": None})
            m["score"] += contrib
            m["sources"][e] = rank
            if not m["snippet"]:
                m["snippet"], m["line"] = h["snippet"], h.get("line")
            if e == "graph":
                m["paths"] = (m["paths"] or []) + (h.get("paths") or [])
                m["judgement"] = m["judgement"] or h.get("judgement")
    out = sorted(merged.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    return [v for _, v in out[:k]]
