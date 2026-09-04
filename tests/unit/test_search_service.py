"""外部連携 API E2a〜c（エンジン分離検索＋RRF融合・`sherpa/search_service.py`）の単体テスト。

ES/Neo4j 不要（全て monkeypatch/スタブ）。実 ES/Neo4j を要する疎通は
`tests/integration/test_ext_search_engines.py` 側（不達なら skip）。
"""
from __future__ import annotations

import ast
import itertools
import json
import pathlib

import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa import documents, ext_api, search_service as ss


def _hit(key, judgement=None, snippet="s", line=None, doc_id=None, paths=None):
    return {"key": key, "doc_id": doc_id if doc_id is not None else key,
            "path": doc_id if doc_id is not None else key, "line": line,
            "snippet": snippet, "engine_score": 1.0, "judgement": judgement, "paths": paths}


class _AllDocs:
    """`in` に常に True を返すダミー実在集合（実在フィルタを無効化するテスト用スタブ）。"""

    def __contains__(self, item):
        return True


_ALL_DOCS = _AllDocs()   # フィルタ挙動を検証しないテストの既定値（doc_id はどれも「実在」扱い）


# ==== engines 正規化 ====

def test_default_engines_keyword_vector(monkeypatch):
    calls = []
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _ALL_DOCS)
    monkeypatch.setattr(ss, "_search_keyword",
                        lambda w, q, sp, k, s, layer=None: (calls.append("keyword") or ([_hit("a")], None)))
    monkeypatch.setattr(ss, "_search_vector",
                        lambda w, q, sp, k, s, layer=None: (calls.append("vector") or ([_hit("b")], None)))

    def _boom(*a, **kw):
        raise AssertionError("graph は既定 engines に含まれず呼ばれてはいけない")

    monkeypatch.setattr(ss, "_search_graph", _boom)
    res = ss.search("w", "q")   # engines=None → DEFAULT_ENGINES
    assert set(calls) == {"keyword", "vector"}
    assert res["engines_used"] == ["keyword", "vector"]
    assert res["degraded"] == []


def test_engine_normalization(monkeypatch):
    """順序・重複を ENGINES 順に正規化する（決定的順序）。"""
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _ALL_DOCS)
    monkeypatch.setattr(ss, "_search_keyword", lambda w, q, sp, k, s, layer=None: ([_hit("a")], None))
    monkeypatch.setattr(ss, "_search_vector", lambda w, q, sp, k, s, layer=None: ([_hit("b")], None))
    monkeypatch.setattr(ss, "_search_graph", lambda w, q, sp, k, s, d=8: ([_hit("c", judgement="sure")], None))
    res = ss.search("w", "q", engines=["graph", "keyword", "keyword", "vector"])
    assert res["engines_used"] == ["keyword", "vector", "graph"]


def test_seven_combinations(monkeypatch):
    """ENGINES の非空部分集合7通り全てで engines_used が正しく決定的に決まる。"""
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _ALL_DOCS)
    monkeypatch.setattr(ss, "_search_keyword", lambda w, q, sp, k, s, layer=None: ([_hit("k")], None))
    monkeypatch.setattr(ss, "_search_vector", lambda w, q, sp, k, s, layer=None: ([_hit("k")], None))
    monkeypatch.setattr(ss, "_search_graph", lambda w, q, sp, k, s, d=8: ([_hit("k", judgement="sure")], None))
    all_subsets = [c for n in range(1, 4) for c in itertools.combinations(ss.ENGINES, n)]
    assert len(all_subsets) == 7
    for combo in all_subsets:
        res = ss.search("w", "q", engines=list(combo))
        assert res["engines_used"] == [e for e in ss.ENGINES if e in combo]
        assert len(res["hits"]) == 1   # 同一 key なので融合後は1件


# ==== 実在フィルタ（R3-S2: chat_service._es_hits と同型・doc 削除直後の窓を閉じる）====

def test_search_filters_nonexistent_docs(monkeypatch):
    """世界に実在しない doc_id（削除直後の窓で古いまま残る ES ヒット等）は search() の戻りから落ちる。"""
    monkeypatch.setattr(ss, "_search_keyword",
                        lambda w, q, sp, k, s, layer=None: ([_hit("real.md"), _hit("stale.md")], None))
    monkeypatch.setattr(ss, "_search_vector", lambda w, q, sp, k, s, layer=None: ([], None))
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: {"real.md"})
    res = ss.search("w", "q", engines=["keyword", "vector"])
    ids = {h["doc_id"] for h in res["hits"]}
    assert ids == {"real.md"}
    assert res["engines_used"] == ["keyword", "vector"]   # フィルタで0件になっても degrade 扱いにはしない


def test_search_calls_world_rel_set_once(monkeypatch):
    """実在集合は per-engine ではなく search() 呼び出し全体で1回だけ作る（rv MED と同型の懸念）。"""
    calls = []
    monkeypatch.setattr(documents, "world_rel_set",
                        lambda world=None, **kw: (calls.append(world) or _ALL_DOCS))
    monkeypatch.setattr(ss, "_search_keyword", lambda w, q, sp, k, s, layer=None: ([_hit("a")], None))
    monkeypatch.setattr(ss, "_search_vector", lambda w, q, sp, k, s, layer=None: ([_hit("b")], None))
    monkeypatch.setattr(ss, "_search_graph", lambda w, q, sp, k, s, d=8: ([_hit("c", judgement="sure")], None))
    ss.search("world-x", "q", engines=["keyword", "vector", "graph"])
    assert calls == ["world-x"]


# ==== 探す対象（層）フィルタ（調べ方ブロック §3.4/§3.5）====

def test_search_forwards_layer_to_keyword_and_vector_but_not_graph(monkeypatch):
    """layer は keyword/vector にのみ渡す。graph（Neo4j 影響たどり）は REALIZES 橋で業務語↔コードを
    繋ぐため非適用（§3.5・裁定1）——graph の fake は layer 引数を受け付けない形にし、渡されたら
    TypeError で検出する（黙って無視ではなく構造的に非適用を固定する）。"""
    captured: dict = {}
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _ALL_DOCS)

    def kw_spy(w, q, sp, k, s, layer=None):
        captured["keyword_layer"] = layer
        return [_hit("a")], None

    def v_spy(w, q, sp, k, s, layer=None):
        captured["vector_layer"] = layer
        return [_hit("b")], None

    def g_spy(w, q, sp, k, s, d=8):   # layer 引数なし＝渡されたら TypeError
        captured["graph_called"] = True
        return [_hit("c", judgement="sure")], None

    monkeypatch.setattr(ss, "_search_keyword", kw_spy)
    monkeypatch.setattr(ss, "_search_vector", v_spy)
    monkeypatch.setattr(ss, "_search_graph", g_spy)
    ss.search("w", "q", engines=["keyword", "vector", "graph"], layer="code")
    assert captured["keyword_layer"] == "code"
    assert captured["vector_layer"] == "code"
    assert captured["graph_called"] is True


def test_search_layer_omitted_defaults_to_none_forwarded_unchanged(monkeypatch):
    """layer 省略時は None がそのまま keyword/vector へ渡る（既存呼び出し元は無変更＝both 相当）。"""
    captured: dict = {}
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _ALL_DOCS)

    def kw_spy(w, q, sp, k, s, layer=None):
        captured["layer"] = layer
        return [_hit("a")], None

    monkeypatch.setattr(ss, "_search_keyword", kw_spy)
    monkeypatch.setattr(ss, "_search_vector", lambda w, q, sp, k, s, layer=None: ([_hit("b")], None))
    ss.search("w", "q", engines=["keyword", "vector"])
    assert captured["layer"] is None


def test_filter_existing_keeps_hits_without_doc_id():
    """doc_id を持たない純概念グラフノードは実在フィルタの対象外（素通り）。"""
    hits = [{"doc_id": None, "key": "graph:BusinessRule:税計算"},
            {"doc_id": "gone.md", "key": "gone.md"},
            {"doc_id": "real.md", "key": "real.md"}]
    out = ss._filter_existing(hits, {"real.md"})
    assert out == [{"doc_id": None, "key": "graph:BusinessRule:税計算"},
                   {"doc_id": "real.md", "key": "real.md"}]


# ==== RRF 融合 ====

def test_rrf_formula():
    """spec §4.4 の数値例と一致（keyword rank2・vector rank1・graph(sure) rank3）。"""
    per_engine = {
        "keyword": [_hit("other-k", doc_id="other-k"), _hit("04_設計/A.md")],   # A は rank2
        "vector": [_hit("04_設計/A.md")],                                       # A は rank1
        "graph": [_hit("g1", judgement="sure"), _hit("g2", judgement="sure"),
                  _hit("04_設計/A.md", judgement="sure")],                      # A は rank3
    }
    weights = {"keyword": 1.0, "vector": 1.0, "graph": 1.0}
    out = ss.fuse_rrf(per_engine, weights, k=10)
    a = next(h for h in out if h["doc_id"] == "04_設計/A.md")
    expected = 1.0 / 62 + 1.0 / 61 + 1.0 / 63
    assert a["score"] == pytest.approx(expected, rel=1e-9)
    assert a["sources"] == {"keyword": 2, "vector": 1, "graph": 3}


def test_rrf_deterministic_order():
    """同点スコアは key 辞書順（完全決定的）。"""
    per_engine = {"keyword": [_hit("zzz"), _hit("aaa")]}   # 両方 rank1/2 相当だが同点になるよう調整
    # 同点を作るため、2件を別々の呼び出しで同じ rank1 契約にする（各1件ずつのエンジン扱い）。
    per_engine = {"keyword": [_hit("zzz")], "vector": [_hit("aaa")]}
    out = ss.fuse_rrf(per_engine, {"keyword": 1.0, "vector": 1.0}, k=10)
    assert out[0]["score"] == pytest.approx(out[1]["score"])
    assert [h["doc_id"] for h in out] == ["aaa", "zzz"]   # 同点は key 昇順


def test_rrf_merge_sources():
    per_engine = {"keyword": [_hit("x")], "vector": [_hit("y"), _hit("x")]}
    out = ss.fuse_rrf(per_engine, {"keyword": 1.0, "vector": 1.0}, k=10)
    x = next(h for h in out if h["doc_id"] == "x")
    assert x["sources"] == {"keyword": 1, "vector": 2}


def test_rrf_weights():
    per_engine = {"keyword": [_hit("x")]}
    base = ss.fuse_rrf(per_engine, {"keyword": 1.0}, k=10)[0]["score"]
    doubled = ss.fuse_rrf(per_engine, {"keyword": 2.0}, k=10)[0]["score"]
    assert doubled == pytest.approx(base * 2)


def test_graph_judgement_factor():
    """graph 由来のみ judgement 係数（sure=1.0/review=0.6/presumed=0.3）を RRF 寄与に乗じる。"""
    per_engine = {"graph": [_hit("s", judgement="sure"), _hit("r", judgement="review"),
                            _hit("p", judgement="presumed")]}
    out = {h["doc_id"]: h["score"] for h in ss.fuse_rrf(per_engine, {"graph": 1.0}, k=10)}
    assert out["s"] == pytest.approx(1.0 * 1.0 / 61)
    assert out["r"] == pytest.approx(1.0 * 0.6 / 62)
    assert out["p"] == pytest.approx(1.0 * 0.3 / 63)


def test_snippet_priority_keyword_over_vector_over_graph():
    per_engine = {
        "keyword": [_hit("x", snippet="from-keyword")],
        "vector": [_hit("x", snippet="from-vector")],
        "graph": [_hit("x", snippet="from-graph", judgement="sure")],
    }
    out = ss.fuse_rrf(per_engine, {"keyword": 1.0, "vector": 1.0, "graph": 1.0}, k=10)
    assert out[0]["snippet"] == "from-keyword"


# ==== graph エンジン: run_impact 結果 → ヒット形マッピング（§4.3）====

def test_graph_item_mapping():
    """K12（2026-09-04-グラフのソース正典化.md §4）: items[] は全件同格（確実/要確認の判定表示は
    機構ごと撤去）——`_graph_item_hit` は judgement を持たず、snippet に判定文言も出さない。"""
    item = {"name": "ORDER-MAIN", "label": "Module", "category": "ソース",
           "path": "4期/src/ORDER-MAIN.cbl", "top_scope": "4期",
           "trace": ["ORDER-LIMIT", "ORDER-MAIN"],
           "evidence": [{"type": "x", "doc": "4期/src/ORDER-MAIN.cbl", "line": 10}]}
    h = ss._graph_item_hit(item)
    assert h["key"] == h["doc_id"] == h["path"] == "4期/src/ORDER-MAIN.cbl"
    assert h["judgement"] is None
    assert "確実" not in h["snippet"] and "要確認" not in h["snippet"]
    assert "ORDER-LIMIT" in h["snippet"] and "ORDER-MAIN" in h["snippet"]
    assert h["paths"] == [{"nodes": ["ORDER-LIMIT", "ORDER-MAIN"],
                           "edges": item["evidence"], "judgement": None}]

    item_presumed = {"name": "SHARED-AMT", "label": "Module", "category": "ソース",
                     "path": None, "top_scope": "4期",
                     "evidence": [{"doc": "4期/design.md", "line": 5, "quote": "根拠テキスト"}]}
    h3 = ss._presumed_item_hit(item_presumed)
    assert h3["doc_id"] == "4期/design.md"     # path 無し→evidence[0].doc
    assert h3["judgement"] == "presumed"       # presumed（grep 共起の推定）は別枠のまま残る
    assert "推定" in h3["snippet"] and "根拠テキスト" in h3["snippet"]


def test_graph_hits_key_without_doc():
    """path も evidence の doc も無い純概念ノードは doc_id=null・key=graph:{label}:{name}。"""
    item = {"name": "税計算", "label": "BusinessRule", "category": "ルール",
            "path": None, "top_scope": "4期", "trace": [], "evidence": []}
    h = ss._graph_item_hit(item)
    assert h["doc_id"] is None
    assert h["key"] == "graph:BusinessRule:税計算"


def test_graph_hits_merge_same_key_caps_paths():
    """同一 key に複数 item が落ちる場合: paths を追記（上限5経路/ヒット）。"""
    items = []
    for i in range(7):
        items.append({"name": f"N{i}", "label": "Module", "category": "ソース",
                      "path": "同じ/doc.md", "top_scope": "4期",
                      "trace": [f"t{i}"], "evidence": [{"doc": "同じ/doc.md", "line": i}]})
    result = {"items": items, "presumed": []}
    out = ss._graph_hits(result, k=10)
    assert len(out) == 1
    h = out[0]
    assert h["judgement"] is None
    assert len(h["paths"]) == 5   # 上限5経路/ヒット


def test_graph_hits_truncates_to_k():
    items = [{"name": f"N{i}", "label": "Module", "category": "ソース", "judgement": "sure",
             "path": f"doc{i}.md", "top_scope": "4期", "trace": [], "evidence": []}
             for i in range(5)]
    out = ss._graph_hits({"items": items, "presumed": []}, k=2)
    assert len(out) == 2


# ==== _search_graph: 例外→degrade 語彙のマッピング ====

def test_search_graph_neo4j_unavailable(monkeypatch):
    def _raise_session():
        raise ServiceUnavailable("down")

    monkeypatch.setattr(ss, "_neo4j_session", lambda: _CtxRaise(_raise_session))
    hits, reason = ss._search_graph("w", "q", [], 10, None)
    assert hits == [] and reason == "neo4j_unavailable"


def test_search_graph_generic_error_is_query_failed(monkeypatch):
    def _raise_session():
        raise RuntimeError("boom")

    monkeypatch.setattr(ss, "_neo4j_session", lambda: _CtxRaise(_raise_session))
    hits, reason = ss._search_graph("w", "q", [], 10, None)
    assert hits == [] and reason == "graph_query_failed"


def test_search_graph_schema_era_error_is_reingest_required(monkeypatch):
    """RV是正（rv-periphery #12・2026-09-05）: 旧世代グラフ（`GraphSchemaEraError`）は他の
    Neo4j 失敗と同じ generic 理由（`graph_query_failed`）に丸めず、閉じた理由
    `graph_reingest_required` を返す。"""
    import contextlib

    from sherpa.ingest.world_neo4j import GraphSchemaEraError

    @contextlib.contextmanager
    def _fake_session():
        yield object()
    monkeypatch.setattr(ss, "_neo4j_session", _fake_session)

    def _boom(*a, **kw):
        raise GraphSchemaEraError("w", "old-era")
    monkeypatch.setattr(ss, "run_impact", _boom)

    hits, reason = ss._search_graph("w", "q", [], 10, None)
    assert hits == [] and reason == "graph_reingest_required"


class _CtxRaise:
    """`with ...` に入る際に例外を送出するダミーコンテキストマネージャ（テスト用）。"""

    def __init__(self, fn):
        self._fn = fn

    def __enter__(self):
        self._fn()

    def __exit__(self, *a):
        return False


# ==== degrade 語彙 ====

def test_degrade_vocabulary():
    assert ss.DEGRADE_REASONS == frozenset({
        "es_unavailable", "es_query_failed", "embedding_not_configured",
        "embedding_cloud_unavailable",
        "hybrid_query_failed",   # RV3（FBK-1）: hybrid 自体が失敗し BM25 は成功（hits は空でない）
        "vector_feature_mismatch", "query_embed_failed",
        "neo4j_unavailable", "graph_query_failed",
        "graph_reingest_required",   # RV是正（rv-periphery #12）: 旧世代グラフ（GraphSchemaEraError）
    })


def test_search_keyword_degrade_when_es_down(monkeypatch):
    from sherpa import es_index
    monkeypatch.setattr(es_index, "available", lambda: False)
    hits, reason = ss._search_keyword("w", "q", [], 10, None)
    assert hits == [] and reason == "es_unavailable"
    assert reason in ss.DEGRADE_REASONS


def test_search_keyword_propagates_es_index_search_reason(monkeypatch):
    """RV3（FBK-1・境界回帰#3・2026-09-01）: `vector=False` でも `es_index.search()` は BM25 自体の
    実クエリ失敗を `es_query_failed` として返しうる（埋め込み関連の reason だけが None に潰れる
    わけではない）——以前は `_search_keyword()` がここで reason を一律 `None` に捨てており、
    keyword-only 障害が「正常 0 件・degraded なし」に化けていた。"""
    from sherpa import es_index
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "search",
                        lambda world, query, scope_paths=None, k=20, settings=None, vector=True,
                              layer=None, **kw: ([], "es_query_failed"))
    hits, reason = ss._search_keyword("w", "q", [], 10, None)
    assert hits == [] and reason == "es_query_failed"
    assert reason in ss.DEGRADE_REASONS


def test_search_keyword_failure_surfaces_in_outer_degraded(monkeypatch):
    """RV3（境界回帰#3）: `_search_keyword()` が拾った reason は `search()` の `degraded` 集計まで
    そのまま届く（`documents.world_rel_set` 等は他エンジンを使わない構成にして keyword 単体で
    確認する）。"""
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _ALL_DOCS)
    monkeypatch.setattr(ss, "_search_keyword", lambda w, q, sp, k, s, layer=None: ([], "es_query_failed"))
    res = ss.search("w", "q", engines=["keyword"])
    assert res["engines_used"] == []                      # degrade したエンジンは used に入らない
    assert res["degraded"] == [{"engine": "keyword", "reason": "es_query_failed"}]


def test_search_vector_propagates_es_reason(monkeypatch):
    from sherpa import es_index
    monkeypatch.setattr(es_index, "search_knn_only",
                        lambda world, query, scope_paths=None, k=20, settings=None, layer=None:
                            ([], "vector_feature_mismatch"))
    hits, reason = ss._search_vector("w", "q", [], 10, None)
    assert hits == [] and reason == "vector_feature_mismatch"
    assert reason in ss.DEGRADE_REASONS


def test_search_keyword_and_vector_tolerate_es_hit_without_line():
    """rag_chunks 由来の ES ヒットは line キーを持たない。keyword/vector 両エンジンとも
    `_es_hit` の `h.get("line")` で防御的に読むため、KeyError にならず line=None で通る。"""
    rag_hit = {"doc_id": "a.docx", "text": "本文", "score": 1.0, "ext": ".docx", "chunk_id": "rc1"}
    assert ss._es_hit(rag_hit) == {"key": "a.docx", "doc_id": "a.docx", "path": "a.docx",
                                   "line": None, "snippet": "本文", "engine_score": 1.0,
                                   "judgement": None, "paths": None}


def test_search_keyword_and_vector_engines_tolerate_missing_line(monkeypatch):
    from sherpa import es_index
    rag_hit = {"doc_id": "a.docx", "text": "本文", "score": 1.0, "ext": ".docx", "chunk_id": "rc1"}
    monkeypatch.setattr(es_index, "available", lambda: True)
    # RV2（FBK-1・2026-09-01）: `es_index.search()` は (hits, degrade_reason) を返す。
    monkeypatch.setattr(es_index, "search", lambda *a, **kw: ([rag_hit], None))
    monkeypatch.setattr(es_index, "search_knn_only", lambda *a, **kw: ([rag_hit], None))
    hits_k, reason_k = ss._search_keyword("w", "q", None, 10, None)
    hits_v, reason_v = ss._search_vector("w", "q", None, 10, None)
    assert reason_k is None and reason_v is None
    assert hits_k[0]["line"] is None and hits_v[0]["line"] is None


# ==== 禁止 import の静的検査（§6-1・CLAUDE.md 契約: 共有KBのみ・個人workspace禁止）====

_FORBIDDEN_MODULE_REFS = {
    "api", "agents", "chat_service", "chat_router", "grep_tool", "providers",
    "sherpa.api", "sherpa.agents", "sherpa.chat_service", "sherpa.chat_router",
    "sherpa.grep_tool", "sherpa.ingest.worker", "ingest.worker", "sherpa.providers",
}
_FORBIDDEN_NAMES = {
    "ensure_workspace", "_workspace_files_dir", "record_workspace_file",
    "list_workspace_files", "personal_workspace_files", "SHERPA_USERS_DIR",
    "agents", "chat_service", "chat_router", "grep_tool", "providers",
}


def _imported_refs(path: pathlib.Path) -> tuple[set, set]:
    """ファイル内の全 import（トップレベル＋関数内の遅延 import 含む）から
    モジュール参照集合とインポートされた名前集合を集める（ast.walk は関数本体も辿る）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set = set()
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            for alias in node.names:
                names.add(alias.name)
    return modules, names


def test_no_forbidden_imports():
    """ext_api.py・search_service.py が禁止モジュール/関数を import・参照していないこと（静的検査）。"""
    for mod in (ext_api, ss):
        path = pathlib.Path(mod.__file__)
        modules, names = _imported_refs(path)
        # RV LOW（2026-07-14 フェーズ5）: 完全一致だけだと `from sherpa.providers.openai import X` の
        # ようなサブモジュール import（記録されるモジュール名は "sherpa.providers.openai"）が素通りする。
        # 禁止名そのもの＋「禁止名.」prefix の両方で照合する。
        bad_modules = {m for m in modules
                       if m in _FORBIDDEN_MODULE_REFS
                       or any(m.startswith(f + ".") for f in _FORBIDDEN_MODULE_REFS)}
        bad_names = names & _FORBIDDEN_NAMES
        assert not bad_modules, f"{path}: 禁止モジュール import {bad_modules}"
        assert not bad_names, f"{path}: 禁止シンボル import {bad_names}"


# ---- SHERPA_IMPACT_MAX_DEPTH の伝播 ----

def test_search_service_depth_defaults_match_impact_max_depth():
    """`search()`/`_search_graph()` の `depth` 既定は `impact_service.IMPACT_MAX_DEPTH` に揃っている
    （`ss.IMPACT_MAX_DEPTH` は `impact_service` からの re-export）。

    既定値と異なる env（20）で fresh import して確認する＝既定値どうしが偶然一致するだけの
    「旧リテラル `depth=8` への退行」を検出できない自己言及を避ける。
    """
    env = {"SHERPA_IMPACT_MAX_DEPTH": "20"}
    assert FI.fresh_import_param_default("sherpa.search_service", "search", "depth", env=env) == 20
    assert FI.fresh_import_param_default(
        "sherpa.search_service", "_search_graph", "depth", env=env) == 20


def test_ext_search_req_depth_default_and_ceiling_track_impact_max_depth():
    """`ExtSearchReq.depth` の既定は `search_service.IMPACT_MAX_DEPTH` に揃える（env を上げれば既定も
    上がる）。上限（le）は元の外部 API 契約 `12` を後退させない＝`max(12, IMPACT_MAX_DEPTH)`
    （env 未設定/12未満でも契約上の上限は12のまま・env で12を超えて広げたときだけ上限も広がる）。

    Pydantic の `Field` は import 時に評価されるため、既定値と異なる env の fresh process で
    `model_fields`/シグネチャを直接観測する（同一プロセス内で既定値のまま比較すると、
    旧リテラル `Field(default=8, ge=1, le=12)` への退行を検出できない）。
    """
    script = (
        "import json\n"
        "import sherpa.ext_api as ext_api\n"
        "field = ext_api.ExtSearchReq.model_fields['depth']\n"
        "ge = next(m.ge for m in field.metadata if hasattr(m, 'ge'))\n"
        "le = next(m.le for m in field.metadata if hasattr(m, 'le'))\n"
        "print(json.dumps({'default': field.default, 'ge': ge, 'le': le}))\n"
    )
    out = json.loads(FI.run_script(script, env={"SHERPA_IMPACT_MAX_DEPTH": "20"}))
    assert out["default"] == 20
    assert out["ge"] == 1
    assert out["le"] == 20   # 12 を超えて広げた env にあわせて上限も 20 まで広がる

    out_low = json.loads(FI.run_script(script, env={"SHERPA_IMPACT_MAX_DEPTH": "5"}))
    assert out_low["default"] == 5
    assert out_low["le"] == 12   # 12 未満の env でも契約上の上限は12のまま後退しない
