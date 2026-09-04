"""エージェント検索（索引なし・LLM が grep ツールを反復）の単体テスト。LLM は stub（コスト0）。

- run_tool: ripgrep_search / read_around / 範囲外拒否（v1 フィクスチャの filesystem grep・Neo4j 不要）。
- openai_style / gemini ループ: _post を差し替え、tool 呼び出し→最終回答→docs 収集／ask_user 質問を検証。
- プロバイダ統合: OpenAIProvider.run（knowledge ON・qa）が反復検索で _result(env: headline/sources) を返す。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")   # es_search が実埋め込みを叩かない（BM25）
import pytest  # noqa: E402
from sherpa import agentic_search as A   # noqa: E402
from sherpa import store   # noqa: E402   # BUDGET-1: system_settings > コード既定のテスト用
import _corpus_expect as CE   # noqa: E402   # フィクスチャ実走査ベースの list_docs 期待値（フェーズ7 S1）
import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証


def test_run_tool_search_read_and_scope():
    res, docs, cites, cards = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    assert res["hits"] and docs and cites and cards == []          # ヒット＋引用候補（grep はカード無し）
    assert all("span" in c and "quote" in c for c in cites)
    h = res["hits"][0]
    r2, d2, _, _ = A.run_tool("read_around", {"doc_id": h["doc_id"], "line": h["line"], "window": 2}, "v1", None)
    assert "text" in r2 and h["doc_id"] in d2
    # 範囲外の doc は読まない
    r3, _, _, _ = A.run_tool("read_around", {"doc_id": h["doc_id"], "line": 1}, "v1", ["5期"])
    assert "error" in r3
    # 未知ツールは error
    r4, _, _, _ = A.run_tool("rm_rf", {}, "v1", None)
    assert "error" in r4


# ===== 探す対象（層）フィルタ（調べ方ブロック §3.4/§3.5）=====
# "TAX-RATE" は fixtures/corpus/v1 に資料（.md）・コード（.cbl/.cpy）の両方に実在する語。

def _doc_exts(res) -> set:
    return {pathlib.Path(h["doc_id"]).suffix.lower() for h in res["hits"]}


def test_run_tool_ripgrep_search_layer_code_excludes_docs():
    res_both, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    exts_both = _doc_exts(res_both)
    assert ".md" in exts_both and ".cbl" in exts_both   # 前提: 両方の層に実ヒットがある

    res_code, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="code")
    exts_code = _doc_exts(res_code)
    assert exts_code and exts_code <= {".cbl", ".cpy", ".cob", ".cobol", ".copybook", ".jcl"}


def test_run_tool_ripgrep_search_layer_docs_excludes_code():
    res_docs, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="docs")
    exts_docs = _doc_exts(res_docs)
    assert exts_docs and not (exts_docs & {".cbl", ".cpy", ".cob", ".cobol", ".copybook", ".jcl"})


def test_run_tool_forwards_layer_to_grep_search(monkeypatch):
    """`run_tool(layer=...)` は `scope_paths` と同じく `grep_tool.grep_search` へそのまま転送する。"""
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(A.grep_tool, "grep_search", _spy)
    A.run_tool("ripgrep_search", {"query": "x"}, "v1", None, layer="code")
    assert captured.get("layer") == "code"


def test_run_tool_ripgrep_search_hit_view_carries_importance_conditionally(monkeypatch):
    """I2（2026-09-05）: `grep_tool.grep_search` が返すヒットの `importance`/`importance_reason`
    （条件付きキー）を `ripgrep_search` の tool result（LLM 向け hit_view）へそのまま転送する
    ——重要文書を優先的に精読（read_around）できるようにする。無ければキー自体を作らない。"""
    hits = [
        {"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "本文A", "ext": ".md",
         "importance": "高", "importance_reason": "契約書"},
        {"doc_id": "b.md", "line": 1, "span": [1, 1], "text": "本文B", "ext": ".md"},   # importance キー無し
    ]
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = A.run_tool("ripgrep_search", {"query": "x"}, "v1", None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["a.md"]["importance"] == "高" and by_doc["a.md"]["importance_reason"] == "契約書"
    assert "importance" not in by_doc["b.md"] and "importance_reason" not in by_doc["b.md"]


def test_run_tool_es_search_forwards_layer(monkeypatch):
    """`run_tool(layer=...)` の es_search 分岐も `es_index.search` へ layer をそのまま転送する。"""
    from sherpa import documents

    captured = {}

    def fake_search(world, q, scope_paths=None, k=20, layer=None, **_kw):
        captured["layer"] = layer
        return [], None   # RV2（FBK-1・2026-09-01）: es_index.search() は (hits, degrade_reason) を返す

    monkeypatch.setattr(A.es_index, "search", fake_search)
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: set())
    A.run_tool("es_search", {"query": "x"}, "v1", None, layer="docs")
    assert captured.get("layer") == "docs"


def test_run_tool_es_search_surfaces_degrade_reason_in_result(monkeypatch):
    """RV2（FBK-1・境界回帰#2・2026-09-01）: `es_index.search()` が BM25 縮退の理由を返したら、
    `run_tool()` の tool result（`view`）にも `degrade_reason` として載せる——サーバログの
    warning だけでなく、tool result 経由で「思考の流れ」へ搬送できるようにする（`_degrade_
    result_node()` がここから拾う）。理由が無い（None）ときはキー自体を作らない。"""
    from sherpa import documents

    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {"a.md"})
    monkeypatch.setattr(A.es_index, "search",
                        lambda world, q, scope_paths=None, k=20, layer=None, **kw:
                            ([{"doc_id": "a.md", "line": 1, "text": "x", "ext": ".md"}],
                             "embedding_cloud_unavailable"))
    view, _docs, _cites, _cards = A.run_tool("es_search", {"query": "x"}, "v1", None)
    assert view["degrade_reason"] == "embedding_cloud_unavailable"

    monkeypatch.setattr(A.es_index, "search",
                        lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([], None))
    view2, _docs, _cites, _cards = A.run_tool("es_search", {"query": "x"}, "v1", None)
    assert "degrade_reason" not in view2


def test_degrade_result_node_known_reasons_only():
    """RV2/RV3（境界回帰#2）: `_degrade_result_node()` は既知の縮退理由（BM25 継続時のみ・hits は
    空でない・`embedding_cloud_unavailable`/`query_embed_failed`/`hybrid_query_failed`）だけを
    ノード化する。`degrade_reason` が無い／未知／`es_search` 以外のツール結果（`degrade_reason`
    キー自体を持たない）は None。`es_query_failed`（hits が空になる BM25 自体の失敗・RV3 で
    `hybrid_query_failed` と分離）は対象外のまま。"""
    node = A._degrade_result_node({"hits": [], "degrade_reason": "embedding_cloud_unavailable"})
    assert node["type"] == "node" and node["kind"] == "tool"
    node2 = A._degrade_result_node({"hits": [], "degrade_reason": "query_embed_failed"})
    assert node2["label"] == node["label"]                  # 同じ「精度低下」ラベルで文言だけ違う
    assert node2["detail"] != node["detail"]
    node3 = A._degrade_result_node({"hits": [], "degrade_reason": "hybrid_query_failed"})
    assert node3["label"] == node["label"]
    assert node3["detail"] not in (node["detail"], node2["detail"])
    assert A._degrade_result_node({"hits": []}) is None
    assert A._degrade_result_node({"hits": [], "degrade_reason": "es_query_failed"}) is None
    assert A._degrade_result_node({"count": 0, "docs": []}) is None   # list_docs 等の無関係な result


# ===== `_hit_summary_node`/`_hit_summary_node_sub`: 「何を探して・いくつ当たったか」の追加ノード =====

def test_hit_summary_node_ripgrep_includes_query_and_count():
    node = A._hit_summary_node("ripgrep_search", {"query": "TAX-RATE"},
                               {"hits": [{"doc_id": "a.md"}] * 12})
    assert node["label"] != "資料を検索（grep）"          # `_tool_node` の label とは別ノード
    assert "TAX-RATE" in node["detail"] and "12件" in node["detail"]


def test_hit_summary_node_ripgrep_zero_hits_is_explicit():
    node = A._hit_summary_node("ripgrep_search", {"query": "存在しない語"}, {"hits": []})
    assert "0件" in node["detail"]


def test_hit_summary_node_es_search_includes_mode_and_count():
    """degrade_reason の有無で「実際に使われた検索方式」の文言が変わる（縮退表示自体は
    `_degrade_result_node` が別ノードで担うので、ここでは重複しないことだけ見る）。"""
    normal = A._hit_summary_node("es_search", {"query": "税率"}, {"hits": [{"doc_id": "a.md"}] * 3})
    assert "税率" in normal["detail"] and "3件" in normal["detail"]
    degraded = A._hit_summary_node("es_search", {"query": "税率"},
                                   {"hits": [{"doc_id": "a.md"}],
                                    "degrade_reason": "embedding_cloud_unavailable"})
    assert "1件" in degraded["detail"]
    assert degraded["detail"] != normal["detail"]   # 縮退時は方式の文言が変わる


def test_hit_summary_node_es_search_unavailable_or_query_failed_omits_node():
    """M-1是正: `es_unavailable`/`es_query_failed`（BM25 自体も失敗し hits が強制的に空になっている
    ＝`_ES_DEGRADE_WORDING` の3語彙に含まれない）は「0件（キーワード一致のみ）」という、検索は
    実行できたかのような誤表示を避けるため、追加ノード自体を出さない（メイン・サブ両経路とも）。
    既知の3語彙（BM25 は継続して成立）は従来どおり件数を出す。"""
    for reason in ("es_unavailable", "es_query_failed"):
        result = {"hits": [], "degrade_reason": reason}
        assert A._hit_summary_node("es_search", {"query": "税率"}, result) is None
        assert A._hit_summary_node_sub("es_search", result) is None
    for reason in ("embedding_cloud_unavailable", "query_embed_failed", "hybrid_query_failed"):
        result = {"hits": [], "degrade_reason": reason}
        assert A._hit_summary_node("es_search", {"query": "税率"}, result) is not None
        assert A._hit_summary_node_sub("es_search", result) is not None


def test_hit_summary_node_graph_neighbors_includes_term_and_count():
    node = A._hit_summary_node("graph_neighbors", {"name": "請求"},
                               {"neighbors": [{"name": "x"}, {"name": "y"}]})
    assert "請求" in node["detail"] and "2件" in node["detail"]


def test_hit_summary_node_graph_neighbors_zero_hits_is_explicit():
    node = A._hit_summary_node("graph_neighbors", {"name": "存在しない語"}, {"neighbors": []})
    assert "0件" in node["detail"]


def test_hit_summary_node_list_docs_includes_target_and_count():
    node = A._hit_summary_node("list_docs", {"path_prefix": "4期"}, {"count": 7, "docs": []})
    assert "4期" in node["detail"] and "7件" in node["detail"]


def test_hit_summary_node_read_around_includes_doc_and_line_count():
    node = A._hit_summary_node("read_around", {"doc_id": "設計/資料.md"},
                               {"doc_id": "設計/資料.md", "text": "1: a\n2: b\n3: c"})
    assert "設計/資料.md" in node["detail"] and "3行" in node["detail"]


def test_hit_summary_node_none_on_error_result():
    """実行そのものが成立していない（error 応答）場合は「0件ヒット」と紛らわしいノードを出さない。"""
    assert A._hit_summary_node("ripgrep_search", {"query": "x"}, {"error": "指定 doc_id は対象範囲外です"}) is None
    assert A._hit_summary_node("list_docs", {}, {"error": "boom"}) is None


def test_hit_summary_node_clips_long_query():
    long_q = "あ" * 200
    node = A._hit_summary_node("ripgrep_search", {"query": long_q}, {"hits": []})
    assert long_q not in node["detail"]
    assert ("あ" * 60) in node["detail"]


def test_hit_summary_node_sub_omits_model_generated_args():
    """secRV MED-2 と同じ理由（サブ経路はモデル生成の引数を思考ノードに出さない）で、件数のみの
    固定文言にする——query/name/doc_id はどれも渡していないのに件数だけで組み立てられる。"""
    node = A._hit_summary_node_sub("ripgrep_search", {"hits": [{"doc_id": "a.md"}] * 5})
    assert node["label"] == A._hit_summary_node("ripgrep_search", {"query": "x"}, {"hits": []})["label"]
    assert "5件" in node["detail"]
    assert "資料を検索" not in node["detail"]   # `_tool_node_sub` の文言とは別の detail


def test_hit_summary_node_sub_zero_hits_is_explicit():
    node = A._hit_summary_node_sub("es_search", {"hits": []})
    assert "0件" in node["detail"]


def test_hit_summary_node_sub_read_around_uses_line_unit():
    node = A._hit_summary_node_sub("read_around", {"doc_id": "a.md", "text": "1: a\n2: b"})
    assert "2行" in node["detail"]


def test_hit_summary_node_sub_unknown_tool_or_error_is_none():
    assert A._hit_summary_node_sub("ask_user", {"hits": []}) is None
    assert A._hit_summary_node_sub("ripgrep_search", {"error": "boom"}) is None


def test_hit_summary_node_tags_event_type_tool_completed():
    """M-2是正: `web/chat/render.js::_updateLaneStats` は event_type の無い kind:"tool" ノードを
    「道具使用回数」として数える（`et === 'tool_started' || (e.kind === 'tool' && !et)`）ため、
    追加ノードを無印のままにすると開始ノード（`_tool_node`/`_tool_node_sub`）と合わせて実行1回が
    2回とカウントされる。`event_type="tool_completed"`（`exec_event.EVENT_TYPES` の既存語彙）を
    付けてこの二重計上を避ける（メイン・サブ両経路とも）。"""
    node = A._hit_summary_node("ripgrep_search", {"query": "TAX-RATE"}, {"hits": [{"doc_id": "a.md"}]})
    assert node["event_type"] == "tool_completed"
    node_sub = A._hit_summary_node_sub("ripgrep_search", {"hits": [{"doc_id": "a.md"}]})
    assert node_sub["event_type"] == "tool_completed"
    # v1 最小契約（id/kind/label/detail/status）は引き続き満たす（既存フロントの平坦描画が壊れない）。
    assert all(k in node for k in ("id", "kind", "label", "detail", "status"))


def test_run_tool_read_around_rejects_doc_outside_layer():
    """§8 裁定論点2: open ツール（read_around）は層外の doc_id を scope 外と同型で拒否する。"""
    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="code")
    code_doc_id = res["hits"][0]["doc_id"]
    # コード側の doc_id を layer="docs" で read_around すると拒否される。
    r_reject, _, _, _ = A.run_tool(
        "read_around", {"doc_id": code_doc_id, "line": 1}, "v1", None, layer="docs")
    assert "error" in r_reject
    # 同じ doc_id を layer="code"（または既定 both）で読めば拒否されない。
    r_ok, docs_ok, _, _ = A.run_tool(
        "read_around", {"doc_id": code_doc_id, "line": 1}, "v1", None, layer="code")
    assert "error" not in r_ok and code_doc_id in docs_ok


def test_run_tool_graph_neighbors_rejected_when_layer_restricted(monkeypatch):
    """正典 §3.4: 層が限定されている間は graph_neighbors 自体を拒否する（さもないと
    ripgrep_search/es_search/list_docs/read_around を絞っても graph 経由で層外の名前・経路・
    doc_id が漏れる迂回路になる）。lens_service.neighbor_cards は一度も呼ばれない。"""
    from sherpa import lens_service

    def _boom(world, term, sp=None):
        raise AssertionError("層限定なのに neighbor_cards が呼ばれている（迂回路が塞げていない）")

    monkeypatch.setattr(lens_service, "neighbor_cards", _boom)
    for lyr in ("docs", "code"):
        res, docs, cites, cards = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None, layer=lyr)
        assert "error" in res and docs == set() and cites == [] and cards == []


def test_run_tool_graph_neighbors_allowed_when_layer_both_or_omitted():
    """既定（省略・both）は現状の挙動と完全に同一（graph_neighbors は普通に実行される）。"""
    res_omitted, _, _, _ = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    res_both, _, _, _ = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None, layer="both")
    assert "error" not in res_omitted and "error" not in res_both


def test_run_tool_layer_both_or_omitted_unaffected():
    """既定（省略・both）は現状の挙動と完全に同一。"""
    omitted, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    both, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="both")
    assert {h["doc_id"] for h in omitted["hits"]} == {h["doc_id"] for h in both["hits"]}


def test_openai_style_forwards_layer_to_run_tool(monkeypatch):
    """`openai_style(layer=...)` は `scope_paths` と同じく `run_tool` へそのまま転送する。"""
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured["layer"] = kw.get("layer")
        return ({"hits": []}, set(), [], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
           {"choices": [{"message": {"content": "回答"}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None, layer="code"))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert captured.get("layer") == "code"


# ===== 調べる深さ（調べ方ブロック §3.2・SC-6c）: run_tool の hits/window 上限オーバーライド =====

def test_run_tool_forwards_max_hits_to_grep_search(monkeypatch):
    """`run_tool(max_hits=...)` は `scope_paths`/`layer` と同じく `grep_tool.grep_search` へ転送する。"""
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(A.grep_tool, "grep_search", _spy)
    A.run_tool("ripgrep_search", {"query": "x"}, "v1", None, max_hits=45)
    assert captured.get("max_hits") == 45


def test_run_tool_omitted_max_hits_uses_module_default(monkeypatch):
    """省略（None）はモジュール既定 `MAX_HITS`（既存呼び出し元は無変更）。"""
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(A.grep_tool, "grep_search", _spy)
    A.run_tool("ripgrep_search", {"query": "x"}, "v1", None)
    assert captured.get("max_hits") == A.MAX_HITS


def test_run_tool_forwards_max_hits_to_es_search(monkeypatch):
    """`run_tool(max_hits=...)` の es_search 分岐も `es_index.search` の `k` へそのまま転送する。"""
    from sherpa import documents

    captured = {}

    def fake_search(world, q, scope_paths=None, k=20, layer=None, **_kw):
        captured["k"] = k
        return [], None

    monkeypatch.setattr(A.es_index, "search", fake_search)
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: set())
    A.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=60)
    assert captured.get("k") == 60


def test_run_tool_window_cap_raises_read_around_ceiling(monkeypatch, tmp_path):
    """`run_tool(window_cap=...)` は read_around の安全弁クランプ `max(200, window_cap or
    READ_WINDOW)` の一部を成す——`window_cap` を大きくすると、LLM が大きな window を要求した
    ときにより広い範囲を返せる（既定 `READ_WINDOW` は 200 未満のため実際には常に 200 で
    頭打ちになっていた・SC-6c で初めて 200 を超えて引き上げられる経路ができる）。"""
    world = "depth-window-world"
    lines = [f"line {i}" if i != 250 else "line 250: TAX-RATE" for i in range(1, 501)]
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "\n".join(lines)})

    # window_cap 省略（既定 READ_WINDOW=40）: ceiling=max(200,40)=200 → line=250 中心に 50..450 行
    # （s=max(0,249-200)=49・e=min(500,249+200+1)=450）。
    res_default, _, _, _ = A.run_tool(
        "read_around", {"doc_id": "big.md", "line": 250, "window": 1000}, world, None)
    assert res_default["text"].splitlines()[0] == "50: line 50"    # 先頭行がレンジ外に伸びない

    # window_cap=1000: ceiling=max(200,1000)=1000 → ファイル全体（1..500行）が範囲に入る
    # （s=max(0,249-1000)=0・e=min(500,249+1000+1)=500）。
    res_wide, _, _, _ = A.run_tool(
        "read_around", {"doc_id": "big.md", "line": 250, "window": 1000}, world, None,
        window_cap=1000)
    assert res_wide["text"].splitlines()[0] == "1: line 1"         # 先頭行までレンジが伸びる


def test_run_tool_read_around_default_window_scales_with_window_cap(monkeypatch, tmp_path):
    """LLM が `window` 引数を省略したときの既定値にも `window_cap`（調べる深さが計算した実効値）を
    使う。標準/深く/最大（40/60/80）と PROF-1 相当の `READ_WINDOW=60`（60/90/120）の両方で、
    返却された行範囲の下端行番号を検証する（200 安全クランプの範囲外＝`window_cap` がそのまま
    実効窓になる境界だけを見る）。"""
    world = "depth-window-default-world"
    lines = [f"line {i}" for i in range(1, 301)]
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "\n".join(lines)})

    # (window_cap, 期待する先頭行番号): s = max(0, 150-1-window_cap)。
    for window_cap, expected_first_line in ((40, 110), (60, 90), (80, 70), (90, 60), (120, 30)):
        res, _, _, _ = A.run_tool(
            "read_around", {"doc_id": "big.md", "line": 150}, world, None, window_cap=window_cap)
        assert "error" not in res, res
        first = res["text"].splitlines()[0]
        assert first == f"{expected_first_line}: line {expected_first_line}", (window_cap, first)


def test_openai_style_forwards_max_hits_and_window_cap_to_run_tool(monkeypatch):
    """`openai_style(max_hits=, window_cap=)` は `layer` と同じく `run_tool` へそのまま転送する
    （SC-6c §3.2・調べる深さが計算した実効値）。"""
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured["max_hits"] = kw.get("max_hits")
        captured["window_cap"] = kw.get("window_cap")
        return ({"hits": []}, set(), [], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
           {"choices": [{"message": {"content": "回答"}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                            max_hits=99, window_cap=123))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert captured == {"max_hits": 99, "window_cap": 123}


def test_anthropic_style_forwards_layer_to_run_tool():
    """`anthropic_style(layer=...)` も `run_tool` へそのまま転送する。"""
    captured = {}
    orig_run_tool = A.run_tool

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured["layer"] = kw.get("layer")
        return ({"hits": []}, set(), [], [])

    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "回答")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    A.run_tool = fake_run_tool
    try:
        list(A.anthropic_style(client, "anthropic.claude-opus-4-8", A.SYSTEM, "質問", "v1", None,
                               layer="docs"))
    finally:
        A.run_tool = orig_run_tool
    assert captured.get("layer") == "docs"


def test_gemini_forwards_layer_to_run_tool():
    """`gemini(layer=...)` も `run_tool` へそのまま転送する。"""
    captured = {}
    orig_post, orig_run_tool = A._post, A.run_tool

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured["layer"] = kw.get("layer")
        return ({"hits": []}, set(), [], [])

    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "回答"}]}}]},
    ]
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None, layer="both"))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert captured.get("layer") == "both"


def test_run_tool_forwards_deadline_to_grep_search_only_for_ripgrep(monkeypatch):
    """`run_tool(deadline=...)` は `ripgrep_search`（`grep_tool.grep_search`）へそのまま転送する
    ——同期的なツリー全文検索は `stop_event`（ターン境界でのみ確認）では中断できないため、この
    経路だけが実行中のツール呼び出し自体を打ち切れる。"""
    captured = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(A.grep_tool, "grep_search", _spy)
    A.run_tool("ripgrep_search", {"query": "x"}, "v1", None, deadline=123.5)
    assert captured.get("deadline") == 123.5


def test_run_tool_deadline_defaults_to_none_unbounded():
    """`deadline` 省略時（既定 None）は従来どおり無期限——既存呼び出し元は無変更。"""
    res, docs, cites, cards = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    assert res["hits"]


def test_run_tool_forwards_deadline_to_documents_for_for_list_docs(monkeypatch):
    """RV12 是正の固定: `run_tool(deadline=...)` は `list_docs`（`doc_ledger.documents_for`→
    `corpus_docs.world_documents`→`scope_infer.safe_files`）へもそのまま転送する——list_docs も
    world のフォルダ木を同期的に走査するため、ripgrep_search と同じ理由でツール呼び出し自体を
    打ち切る経路が必要。"""
    from sherpa import doc_ledger as DL

    captured = {}

    def _spy(world, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(DL, "documents_for", _spy)
    A.run_tool("list_docs", {}, "v1", None, deadline=123.5)
    assert captured.get("deadline") == 123.5


def test_run_tool_forwards_deadline_to_world_rel_set_for_es_search(monkeypatch):
    """RV12 是正の固定: `run_tool(deadline=...)` は `es_search`（`documents.world_rel_set`→
    `scope_infer.safe_files`）へもそのまま転送する。"""
    from sherpa import documents as DOCS

    captured = {}

    def _spy(world, **kw):
        captured.update(kw)
        return set()

    monkeypatch.setattr(DOCS, "world_rel_set", _spy)
    # RV2（FBK-1・2026-09-01）: es_index.search() は (hits, degrade_reason) を返す。
    monkeypatch.setattr(A.es_index, "search", lambda *a, **kw: ([], None))
    A.run_tool("es_search", {"query": "x"}, "v1", None, deadline=123.5)
    assert captured.get("deadline") == 123.5


def test_run_tool_list_docs_raises_when_deadline_already_past():
    """RV12 是正の固定: `list_docs` も deadline 超過時は `scope_infer.ScopeWalkDeadlineExceeded`
    を送出する（既存のデッドライン優先の再分類で `ResearchTimeout`/504 になる）。"""
    import time as time_mod

    from sherpa import scope_infer

    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        A.run_tool("list_docs", {}, "v1", None, deadline=time_mod.monotonic() - 1)


def test_run_tool_es_search_raises_when_deadline_already_past():
    """RV12 是正の固定: `es_search` の実在集合走査（`documents.world_rel_set`）も deadline 超過時は
    `scope_infer.ScopeWalkDeadlineExceeded` を送出する（`es_index.search` へ進む前に打ち切る）。"""
    import time as time_mod

    from sherpa import scope_infer

    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        A.run_tool("es_search", {"query": "x"}, "v1", None, deadline=time_mod.monotonic() - 1)


# ==== _openai_style_text（OpenAI refusal 応答の本文抽出） ====

def test_openai_style_text_prefers_content_over_refusal():
    assert A._openai_style_text({"content": "本文", "refusal": "拒否理由"}) == "本文"


def test_openai_style_text_falls_back_to_refusal_when_content_is_none():
    """RV11 是正の固定: OpenAI の refusal（拒否）応答は `content=null`・`refusal="<理由>"`という
    形を取る——`content` だけを見ると空文字列に潰れてしまう（拒否理由という正当な本文が消える）。"""
    assert A._openai_style_text({"content": None, "refusal": "この内容にはお答えできません。"}) == \
        "この内容にはお答えできません。"


def test_openai_style_text_falls_back_to_refusal_when_content_missing():
    assert A._openai_style_text({"refusal": "お答えできません。"}) == "お答えできません。"


def test_openai_style_text_empty_when_both_absent():
    assert A._openai_style_text({}) == ""
    assert A._openai_style_text({"content": None, "refusal": None}) == ""


def test_openai_style_text_strips_whitespace():
    assert A._openai_style_text({"content": "  本文  "}) == "本文"


def test_openai_style_refusal_response_uses_refusal_text_as_final_answer():
    """RV11 是正の固定: OpenAI の refusal（拒否）応答（`content=None`・`refusal="..."`・
    `finish_reason="stop"`）を「空の自然完了」（実質的な合成失敗）に誤分類せず、拒否理由の文章を
    そのまま最終回答として扱う——`synthesis_failed` は立たず、`final["final"]` が空文字列に
    ならないことを固定する（chat/research 共通の `openai_style` 本体で修正しているため、両経路に
    同時に効く）。"""
    seq = [
        {"choices": [{"message": {"content": None, "refusal": "この内容にはお答えできません。"},
                     "finish_reason": "stop"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
        final = next(ev for ev in events if "final" in ev)
        assert final["final"] == "この内容にはお答えできません。"
        assert final.get("synthesis_failed", False) is False
        assert final["attribution_eligible"] is True
    finally:
        A._post = orig


def test_list_docs_path_prefix_and_doctype():
    """S1: 台帳ツール list_docs。path_prefix でフォルダ配下に絞り、rel_path/doctype を返す。"""
    res, docs, cites, cards = A.run_tool("list_docs", {"path_prefix": "4期/02_設計"}, "v1", None)
    expected = CE.count_under("4期/02_設計")                        # 01_基本設計 配下の .md 件数（fixtures 実走査由来）
    assert res["count"] == expected and len(res["docs"]) == expected
    assert cites == [] and cards == []                              # list_docs は引用/カードを作らない
    assert all(d["rel_path"].startswith("4期/02_設計/") for d in res["docs"])
    assert all(d["doctype"] == "設計書" for d in res["docs"])
    assert docs == {d["rel_path"] for d in res["docs"]}             # 返した分だけ出典(docs)に載る


def test_list_docs_name_pattern_matches_path_not_just_content():
    """フォルダ名/ファイル名の部分一致（本文は見ない）＝grep で拾えない台帳質問に応える。"""
    res, _, _, _ = A.run_tool("list_docs", {"name_pattern": "請求"}, "v1", None)
    expected = CE.rel_paths_matching("請求")                        # fixtures 実走査由来（第2テーマ追加で壊れない）
    assert res["count"] == len(expected)
    assert {d["rel_path"] for d in res["docs"]} == expected


def test_list_docs_count_independent_of_limit():
    """count は絞り込み後の全件数（limit で切られる docs 一覧とは独立）。"""
    res, _, _, _ = A.run_tool("list_docs", {"path_prefix": "4期", "limit": 5}, "v1", None)
    assert res["count"] == CE.count_under("4期") and len(res["docs"]) == 5   # 4期配下の全件数・一覧は5件だけ


def test_list_docs_respects_session_scope_and_unknown_world():
    """scope_paths（セッション範囲）で絞られ、未登録 world は 0 件（例外にならない）。"""
    res, docs, _, _ = A.run_tool("list_docs", {}, "v1", ["4期/03_開発"])
    assert res["count"] == CE.count_under("4期/03_開発")
    assert all(d["rel_path"].startswith("4期/03_開発/") for d in res["docs"])
    assert docs and docs == {d["rel_path"] for d in res["docs"]}

    res2, docs2, _, _ = A.run_tool("list_docs", {}, "no-such-world-xyz", None)
    assert res2 == {"count": 0, "docs": []} and docs2 == set()


def test_list_docs_layer_code_and_docs_partition_the_prefix():
    """list_docs にも scope と同型の硬い層フィルタを適用する（列挙対象自体を絞る・
    層外の件数/パスを根拠や sources に載せない）。"""
    import pathlib
    from sherpa.doc_kinds import CODE_EXT
    prefix = "4期"
    all_rels = CE.rel_paths_under(prefix)
    code_rels = {r for r in all_rels if pathlib.Path(r).suffix.lower() in CODE_EXT}
    docs_rels = all_rels - code_rels
    assert code_rels and docs_rels   # 前提: fixtures はこのフォルダに両方の層を持つ

    res_code, docs_out, _, _ = A.run_tool("list_docs", {"path_prefix": prefix}, "v1", None, layer="code")
    assert {d["rel_path"] for d in res_code["docs"]} == code_rels == docs_out
    assert res_code["count"] == len(code_rels)

    res_docs, _, _, _ = A.run_tool("list_docs", {"path_prefix": prefix}, "v1", None, layer="docs")
    assert {d["rel_path"] for d in res_docs["docs"]} == docs_rels
    assert res_docs["count"] == len(docs_rels)

    res_both, _, _, _ = A.run_tool("list_docs", {"path_prefix": prefix}, "v1", None)
    assert {d["rel_path"] for d in res_both["docs"]} == all_rels   # 既定 both は現状の挙動と完全同一


def test_es_search_filters_stale_hits():
    """rv-full2 #4: agentic es_search は現 world に**実在する doc** だけ採用（古い ES ヒットを除外）。"""
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    # RV2（FBK-1・2026-09-01）: es_index.search() は (hits, degrade_reason) を返す。
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "real.md", "line": 1, "text": "x", "span": [1, 1], "ext": ".md"},
        {"doc_id": "stale.md", "line": 2, "text": "y", "span": [2, 2], "ext": ".md"}], None)
    documents.world_rel_set = lambda world, **kw: {"real.md"}            # 実在集合は real.md のみ（1回走査の batch 版）
    try:
        res, docs, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        ids = {h["doc_id"] for h in res["hits"]}
        assert ids == {"real.md"} and "stale.md" not in docs        # 実在のみ＝古いヒットは出さない
        assert all(c["doc_id"] == "real.md" for c in cites)
    finally:
        es_index.search, documents.world_rel_set = o_search, o_relset


def test_es_search_tolerates_hit_without_line():
    """rag_chunks 由来の ES ヒット（line キー無し）でも es_search ツールはクラッシュしない
    （`h.get("line")` の防御的取得・citation の span も [None, None] のまま許容）。

    `chunk_id` を持つため親返し（L4c・既定 ON）の対象になり doc 単位へ束ねられる——rag.md が
    実在しないため tier は "chunk"（最低保証）のまま。"""
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "a.docx", "text": "rag_chunks 由来", "ext": ".docx", "chunk_id": "rc1"}], None)
    documents.world_rel_set = lambda world, **kw: {"a.docx"}
    try:
        res, docs, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        assert res["hits"] == [{"doc_id": "a.docx", "tier": "chunk", "text": "rag_chunks 由来",
                                "chunks": [{"chunk_id": "rc1"}]}]
        assert docs == {"a.docx"}
        assert cites[0]["span"] == [None, None]
    finally:
        es_index.search, documents.world_rel_set = o_search, o_relset


def test_es_search_appends_locator_hint_to_llm_text_but_not_citation():
    """SEARCH-CUT-3: locator あり ES ヒットは `hits[].text`（LLM が読むツール結果）に位置ヒントを添えるが、
    citation の quote は hint 抜きのまま（redaction/500字上限は従来どおり適用済み・出典フッターに
    位置ヒントを出さない・docs/04 契約は不変）。"""
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "b.xlsx", "line": None, "text": "単価100円", "ext": ".xlsx",
         "locator": {"sheet": "明細", "cell_range": "A2"}}], None)
    documents.world_rel_set = lambda world, **kw: {"b.xlsx"}
    try:
        res, docs, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        assert res["hits"] == [{"doc_id": "b.xlsx", "line": None, "text": "単価100円（位置: シート「明細」A2）"}]
        assert cites[0]["quote"] == "単価100円"                # citation 側は位置ヒントを足さない
        assert docs == {"b.xlsx"}
    finally:
        es_index.search, documents.world_rel_set = o_search, o_relset


def test_es_search_without_locator_text_is_unchanged():
    """locator 無し（従来/OFF 相当）は `hits[].text` がバイト一致で従来どおり。"""
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "a.md", "line": 3, "text": "本文", "ext": ".md"}], None)
    documents.world_rel_set = lambda world, **kw: {"a.md"}
    try:
        res, _, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        assert res["hits"] == [{"doc_id": "a.md", "line": 3, "text": "本文"}]
        assert "locator" not in cites[0]
    finally:
        es_index.search, documents.world_rel_set = o_search, o_relset


def test_es_search_locator_hint_combined_text_respects_500_cap():
    """RV MED-3: hint は本文と結合してから500字上限を通す（結合前に本文だけ切ると合計で上限を超える）。"""
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "c.xlsx", "line": None, "text": "あ" * 490, "ext": ".xlsx",
         "locator": {"sheet": "明細", "cell_range": "B1"}}], None)
    documents.world_rel_set = lambda world, **kw: {"c.xlsx"}
    try:
        res, _, _, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        # 本文490字＋hint を足すと500字を超えるが、結合後にまとめて[:500]するため上限を超えない。
        assert len(res["hits"][0]["text"]) <= 500
    finally:
        es_index.search, documents.world_rel_set = o_search, o_relset


def test_es_search_locator_hint_secret_in_sheet_name_is_redacted():
    """RV item3 是正: 秘密様パターンを **hint 側**（sheet 名）に置く回帰テスト。本文側に秘密を置く
    テストだと「hint を redaction 後に無検査で追記する」旧実装でも本文の redaction だけで素通りして
    しまい、この不具合を検出できない（本文の redaction は新旧どちらの実装でも起きるため）。hint 側に
    置くことで「結合してから redaction する」契約（結合前に足すと迂回する・MED-3）を確実に固定する。
    """
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "b.xlsx", "line": None, "text": "単価100円", "ext": ".xlsx",
         "locator": {"sheet": "password: hunter2", "cell_range": "A2"}}], None)
    documents.world_rel_set = lambda world, **kw: {"b.xlsx"}
    try:
        res, _, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        text = res["hits"][0]["text"]
        assert "hunter2" not in text and "[REDACTED]" in text
        assert cites[0]["quote"] == "単価100円"          # citation の quote は hint を含まない（本文のみ）
    finally:
        es_index.search, documents.world_rel_set = o_search, o_relset


def test_graph_neighbors_tool_returns_cards(monkeypatch):
    """graph_neighbors ツール: lens_service.neighbor_cards をスタブし、カードと近傍ビューを返す（Neo4j 不要）。

    本テストの関心はカード/近傍ビューの配線であり、裏付け doc の機械検証（EXT-2）ではないため、
    架空 doc_id をそのまま通せるよう `verify_doc_exists` を直接差し替える（検証自体は
    test_ext2_evidence.py の専用テスト。機械検証は常時実施＝TOGGLE-RM で明示 OFF の退避口を撤去済み）。
    """
    monkeypatch.setattr(A, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)
    from sherpa import lens_service
    fake = [{"name": "BILLINGJOB", "label": "Module", "category": "プログラム", "role": "実装",
             "distance": 2, "path": ["請求画面", "請求処理", "BILLINGJOB"],
             "evidence": {"edges": [], "grep": [{"doc_id": "4期/設計/請求.md", "line": 3}]}}]
    orig = lens_service.neighbor_cards
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    try:
        res, docs, cites, cards = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
        assert len(cards) == 1
        # UI 用カードは元の内容を保つ（EV-0: 検証済み裏付け doc を `_verified_doc_ids` として同梱する
        # ため、fake との完全一致ではなく部分一致で確認する）。
        assert {k: v for k, v in cards[0].items() if k != "_verified_doc_ids"} == fake[0]
        assert cards[0]["_verified_doc_ids"] == ["4期/設計/請求.md"]
        assert res["neighbors"][0]["name"] == "BILLINGJOB" and res["neighbors"][0]["role"] == "実装"
        assert "4期/設計/請求.md" in docs                           # 根拠 doc は出典付与のため docs に
    finally:
        lens_service.neighbor_cards = orig


def test_run_tool_graph_neighbors_filters_invalid_cards_but_keeps_valid_ones(monkeypatch):
    """カード単位で裏付け doc の実在を検証する——有効カードが1枚あっても、裏付け doc を主張した
    のに1件も実在しない無効カードは `cards`/ツール結果（LLM への view）に残さない。裏付け doc を
    1件も主張しない card（純粋なグラフ位相情報等）は検証対象外＝そのまま通す。"""
    real_doc = "4期/04_運用/障害記録.md"
    from sherpa import lens_service
    fake = [
        {"name": "valid", "label": "有効", "category": "プログラム", "role": "実装", "distance": 1,
         "path": [], "evidence": {"edges": [], "grep": [{"doc_id": real_doc}]}},
        {"name": "invalid", "label": "無効", "category": "プログラム", "role": "実装", "distance": 1,
         "path": [], "evidence": {"edges": [], "grep": [{"doc_id": "ghost-does-not-exist.md"}]}},
        {"name": "no-claim", "label": "主張無し", "category": "プログラム", "role": "実装", "distance": 1,
         "path": [], "evidence": {"edges": [], "grep": []}},
    ]
    orig = lens_service.neighbor_cards
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    try:
        res, docs, cites, cards = A.run_tool("graph_neighbors", {"name": "x"}, "v1", None)
        names = {c["name"] for c in cards}
        assert names == {"valid", "no-claim"}          # invalid だけが除外される
        assert docs == {real_doc}                       # 検証済み doc のみ集約
        assert {n["name"] for n in res["neighbors"]} == {"valid", "no-claim"}   # ツール結果からも除外
    finally:
        lens_service.neighbor_cards = orig


@pytest.mark.parametrize("cid,expected", [
    (None, None),                       # cid キーが明示的に None（値としての None）
    ("", None),                         # 空文字列は非一意な label:name フォールバックの引き金にしない
    (0, None),                          # 数値（int）は cid の型契約外
    (123, None),                        # 数値（int・truthy でも）は cid の型契約外
    (12.5, None),                       # 数値（float）も同様
    (True, None),                       # bool（int のサブクラス）も同様
    ("module:v1:a/b#TAXCALC", "module:v1:a/b#TAXCALC"),   # 非空文字列はそのまま返す
])
def test_card_graph_node_id_only_accepts_non_empty_string_cid(cid, expected):
    """`_card_graph_node_id` は `cid` が非空文字列のときだけそれを返し、それ以外（None／空文字列／
    数値／bool）はすべて None にする——後続の `_card_graph_node_evidence` が「cid 無し」と同じ扱いで
    昇格させない判断に使う契約。"""
    card = {"name": "TAXCALC", "label": "Module", "cid": cid}
    assert A._card_graph_node_id(card) == expected


def test_card_graph_node_id_missing_key_is_none():
    """`cid` キー自体が無い card（フィクスチャ未対応等）も None——`.get()` の既定 None と同じ扱い。"""
    assert A._card_graph_node_id({"name": "TAXCALC", "label": "Module"}) is None


def test_card_structural_evidence_does_not_promote_claimless_card_without_cid():
    """機械検証は常時実施（TOGGLE-RM・2026-09-03 で明示 OFF の退避口・`label:name` への
    フォールバックを撤去済み）: cid 欠落の claimless card は昇格しない。"""
    card = {"name": "TAXCALC", "label": "Module", "role": "", "category": "", "path": [],
           "evidence": {"edges": [], "grep": []}}   # cid 無し・裏付け doc も無し（claimless）
    assert A._card_structural_evidence([card]) == []   # cid 無しは昇格しない


def test_read_around_confinement_and_redaction():
    # トラバーサル/絶対/対象外種別は読めない（BLOCKER 修正）
    for bad in ["../../../etc/passwd", "/etc/passwd", "4期/../../../etc/hosts",
                "4期/03_開発/01_ソース/secret.env", "4期/00_共通/メモ.key"]:
        r, _, _, _ = A.run_tool("read_around", {"doc_id": bad, "line": 1}, "v1", None)
        assert "error" in r, bad
    # 秘密は伏せる（HIGH 修正）
    assert "[REDACTED]" in A._redact("config: api_key=sk-ABCDEFGHIJKLMNOP1234 done")
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in A._redact("token sk-ABCDEFGHIJKLMNOPQRSTUVWX")


# ===== secRV MED-B（2026-07-18・DoS/メモリ増幅対策）: read_around のバイト上限 =====

def _isolate_world_kb(monkeypatch, tmp_path, world: str, files: dict) -> None:
    """`sherpa.worlds.world_dir` を tmp_path 配下の KB へ隔離する（DB 不要・実登録 world と非干渉）。

    `tests/unit/test_graph_extract_ab.py::_write_world` と同じ手法（`SHERPA_KB_DIR` を tmp へ向け、
    `store.get_world` を None 固定して registry 解決をバイパスする）。
    """
    from sherpa import store
    kb = tmp_path / "kb"
    wd = kb / world
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    monkeypatch.setenv("SHERPA_KB_DIR", str(kb))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    for env in ("SHERPA_MCP_WORLD", "SHERPA_MCP_WORLD_ROOT"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)


def test_read_around_clips_output_for_huge_single_line_doc(monkeypatch, tmp_path):
    """secRV MED-B (a)(b): 単一行が巨大（200万文字）な文書でも、read_around の返却テキストは
    `TOOL_RESULT_MAX_BYTES`（BUDGET-1・§3.4 でコード既定 262144 へ引き上げ済み）に収まる。
    ファイル全体も一括ロードしない（`_READ_AROUND_FILE_CAP_BYTES` で読み込み自体を bound する）。"""
    world = "hugeline-world"
    huge_line = "A" * 2_000_000   # 200万文字（1行のみ・改行なし）
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": huge_line})

    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "big.md", "line": 1, "window": 5}, world, None)
    assert "error" not in res, res
    assert len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES
    assert "big.md" in docs


def test_read_around_normal_document_unaffected_by_cap(monkeypatch, tmp_path):
    """正常系（既定OFF・メイン経路 byte-identical の要件）: 通常サイズの複数行文書では、
    バイト上限に一切影響されず従来どおりの window 抽出結果が返る。"""
    world = "normal-world"
    content = "\n".join(f"line {i}: TAX-RATE" if i == 10 else f"line {i}" for i in range(1, 21))
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})

    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "doc.md", "line": 10, "window": 2}, world, None)
    assert "error" not in res, res
    assert "TAX-RATE" in res["text"]
    assert res["text"] == "\n".join(f"{i}: line {i}: TAX-RATE" if i == 10 else f"{i}: line {i}"
                                    for i in range(8, 13))
    assert "doc.md" in docs


def test_clip_utf8_bytes_does_not_break_multibyte_boundary():
    s = "あ" * 100   # 各文字3バイト（UTF-8）
    clipped = A._clip_utf8_bytes(s, 10)
    assert len(clipped.encode("utf-8")) <= 10
    clipped.encode("utf-8")   # 例外を出さず正しくデコードできる（壊れた文字が残らない）


# ===== secRV MED-B (c)（2026-07-18）: 1 run 累計 tool-result バイト上限 =====

def test_openai_style_cumulative_tool_result_bytes_cap_terminates_run(monkeypatch):
    """1 run 累計の tool-result バイト量が上限を超えたら、固定エラーで run を打ち切る
    （以降のターンへは進まない）。"""
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", 10)   # 小さい上限で即座に発火させる
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "final answer (should not be reached)"}}]},
    ]
    post_calls = []

    def fake_post(url, headers, body, timeout=90):
        post_calls.append(1)
        return seq.pop(0)

    orig = A._post
    A._post = fake_post
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
        assert len(post_calls) == 1   # 1回目のツール結果だけで上限超過＝2ターン目へは進まない
        assert any(ev.get("node", {}).get("label") == "ツール結果の合計サイズ上限" for ev in events)
        final = next(ev for ev in events if "final" in ev)
        assert final["final"] == ""
    finally:
        A._post = orig


def test_gemini_cumulative_tool_result_bytes_cap_terminates_run(monkeypatch):
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", 10)
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "TAX-RATE"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "not reached"}]}}]},
    ]
    post_calls = []

    def fake_post(url, headers, body, timeout=90):
        post_calls.append(1)
        return seq.pop(0)

    orig = A._post
    A._post = fake_post
    try:
        events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "消費税率は?", "v1", None))
        assert len(post_calls) == 1
        final = next(ev for ev in events if "final" in ev)
        assert final["final"] == ""
    finally:
        A._post = orig


# ===== BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・管理者設定への昇格） =====
# コード既定は精度優先値（262144/4194304）。env フォールバックは撤去済み（ENV-CLEAN・2026-09-03）
# ——このモジュール定数は固定値なので、値そのものを1回ピン留めするだけでよい（settings 段は
# `effective_tool_result_max_bytes`/`effective_tool_result_max_total_bytes`/
# `resolve_tool_result_budgets`（`store.get_system_settings` 経由）で固定する）。

def test_tool_result_max_bytes_code_default():
    assert A.TOOL_RESULT_MAX_BYTES == 262144


def test_tool_result_max_total_bytes_code_default():
    assert A.TOOL_RESULT_MAX_TOTAL_BYTES == 4194304


# ---- settings > コード既定（2段）------------------------------------------------------------

def test_effective_tool_result_max_bytes_code_default_when_settings_unset(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    assert A.effective_tool_result_max_bytes() == A.TOOL_RESULT_MAX_BYTES


def test_effective_tool_result_max_bytes_falls_back_to_module_constant_when_settings_unset(monkeypatch):
    """settings 未設定時のフォールバックはモジュール定数 `TOOL_RESULT_MAX_BYTES`——直接
    monkeypatch して確認する（`MAX_TOOLS_PER_TURN` 等、既存の run-level テストと同じ流儀）。"""
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    assert A.effective_tool_result_max_bytes() == 99000


def test_effective_tool_result_max_bytes_settings_overrides_env(monkeypatch):
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_per_result": 5000})
    assert A.effective_tool_result_max_bytes() == 5000


def test_effective_tool_result_max_bytes_settings_out_of_range_falls_back(monkeypatch):
    """範囲外（1024〜8MiB 外）の settings 値は「不正な保存値」として env/コード既定へ倒す
    （fail-safe・PUT 側の Field(ge,le) を通常はすり抜けないが、DB 直接編集等の破損値でも
    落ちないことを固定する）。"""
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    for bad in (0, -1, 8 * 1024 * 1024 + 1, "not-an-int"):
        monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_per_result": bad})
        assert A.effective_tool_result_max_bytes() == 99000, bad


def test_effective_tool_result_max_bytes_settings_read_failure_falls_back(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(store, "get_system_settings", _boom)
    assert A.effective_tool_result_max_bytes() == 99000


def test_effective_tool_result_max_total_bytes_code_default_when_settings_unset(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    assert A.effective_tool_result_max_total_bytes() == A.TOOL_RESULT_MAX_TOTAL_BYTES


def test_effective_tool_result_max_total_bytes_settings_overrides_env(monkeypatch):
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", 1_000_000)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_total": 20000})
    assert A.effective_tool_result_max_total_bytes() == 20000


def test_effective_tool_result_max_total_bytes_settings_out_of_range_falls_back(monkeypatch):
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", 1_000_000)
    for bad in (0, -1, 64 * 1024 * 1024 + 1, "not-an-int"):
        monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_total": bad})
        assert A.effective_tool_result_max_total_bytes() == 1_000_000, bad


def test_resolve_tool_result_budgets_returns_both_tiers_from_one_settings_read(monkeypatch):
    """`resolve_tool_result_budgets()` は1回の `system_settings` 取得結果を両方の解決に使い回す
    （`store.get_system_settings` の呼び出し回数を数えて固定する）。"""
    calls = []

    def _fake(**kw):
        calls.append(1)
        return {"agentic_budget_per_result": 5000, "agentic_budget_total": 20000}
    monkeypatch.setattr(store, "get_system_settings", _fake)
    per_result, total = A.resolve_tool_result_budgets()
    assert (per_result, total) == (5000, 20000)
    assert len(calls) == 1


# ---- run 開始時に1回だけ解決するスナップショット契約 -----------------------------------------

def test_openai_style_forwards_resolved_tool_result_budget_to_run_tool(monkeypatch):
    """`openai_style` は run 開始時に解決した1件あたり予算を `run_tool` へそのまま転送する
    （`max_hits`/`window_cap` と同じ転送契約）。"""
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_per_result": 12345})
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured["tool_result_max_bytes"] = kw.get("tool_result_max_bytes")
        return ({"hits": []}, set(), [], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
           {"choices": [{"message": {"content": "回答"}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert captured.get("tool_result_max_bytes") == 12345


def test_openai_style_budget_snapshotted_once_settings_change_mid_run_has_no_effect(monkeypatch):
    """run 途中で admin が設定を変えても、run 開始時に1回だけ解決した予算がその run の間ずっと
    使われる（`resolve_tool_result_budgets` を run の先頭で1回だけ呼ぶ契約・累計判定の整合性）。

    1ターン目は小さい予算（下限 4096 byte・`agentic_budget_total` の有効最小値）を返す settings、
    2ターン目以降は巨大な予算（10MB）を返す settings に「admin が変更した」体で差し替える——
    snapshot が効いていれば1ターン目のツール結果（`run_tool` を固定サイズ約5000 byte の
    ダミー結果へ差し替え、実フィクスチャの内容量に依存させない）だけで累計上限（4096 byte）を
    超えて打ち切られ、2ターン目のリクエストへは進まない。
    """
    responses = [{"agentic_budget_total": 4096}, {"agentic_budget_total": 10_000_000}]

    def _fake(**kw):
        # 1回目の呼び出し（run 開始時の snapshot）は小さい予算・以降（もし再度呼ばれたら）は
        # 巨大な予算——snapshot 契約が壊れていれば2ターン目の判定がこの巨大な予算を見てしまう。
        return responses[0] if len(responses) == 1 else responses.pop(0)
    monkeypatch.setattr(store, "get_system_settings", _fake)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": [{"doc_id": "x.md", "line": 1, "text": "x" * 5000}]}, set(), [], [])

    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "final answer (should not be reached)"}}]},
    ]
    post_calls = []

    def fake_post(url, headers, body, timeout=90):
        post_calls.append(1)
        return seq.pop(0)

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
        assert len(post_calls) == 1   # snapshot が効いていれば1ターン目で打ち切り＝2ターン目へ進まない
        assert any(ev.get("node", {}).get("label") == "ツール結果の合計サイズ上限" for ev in events)
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


# ===== BUDGET-2（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・2026-09-03 裁定・
# モデル窓連動・min() 方式）=====
# `resolve_tool_result_budgets`/`effective_tool_result_max_bytes`/`effective_tool_result_max_total_bytes`
# の `provider`/`model`（省略可）引数が「BUDGET-1 の解決値」と「窓由来の上限」の min() を取る契約を
# 固定する。窓解決そのもの（4段の優先順・ライブ照会・シード表）は `tests/unit/test_model_windows.py`
# が固定し、ここでは agentic_search 側の配線（min() の適用・byte-identical フォールバック・
# openai_style/anthropic_style/gemini の呼び出し配線）だけを見る。

def test_window_derived_min_no_provider_is_byte_identical():
    """`provider`/`model` を渡さない（既定 None）呼び出しは BUDGET-1 のみの結果——BUDGET-2 導入前と
    完全に同じ値（退行チェック）。"""
    assert A.effective_tool_result_max_bytes({}) == A.TOOL_RESULT_MAX_BYTES
    assert A.effective_tool_result_max_total_bytes({}) == A.TOOL_RESULT_MAX_TOTAL_BYTES
    assert A.resolve_tool_result_budgets({}) == (A.TOOL_RESULT_MAX_BYTES, A.TOOL_RESULT_MAX_TOTAL_BYTES)


def test_window_derived_min_unknown_model_keeps_base():
    """`provider`/`model` を渡しても、窓がどの段（登録値/API/シード）にも無ければ「不明」——
    BUDGET-1 の値のまま（フォールバック・退行にならない）。"""
    per_result, total = A.resolve_tool_result_budgets(
        {}, provider="openai", model="never-seen-model-xyz")
    assert (per_result, total) == (A.TOOL_RESULT_MAX_BYTES, A.TOOL_RESULT_MAX_TOTAL_BYTES)


def test_window_derived_min_shrinks_for_small_registered_window():
    """小窓（登録値）のモデルへ切り替えると、両方の予算が窓由来の上限まで自動的に縮む
    （§3.4「小窓ローカルLLMでの API ハードエラーを自動で防ぐ」）。"""
    from sherpa import model_windows
    sysset = {model_windows.MODEL_WINDOWS_KEY: {"openai:tiny-model": 40000}}   # 40k tokens
    expected_cap = model_windows.derive_window_bytes(40000)
    assert expected_cap < A.TOOL_RESULT_MAX_BYTES   # 前提: 実際に縮む窓であること
    per_result, total = A.resolve_tool_result_budgets(sysset, provider="openai", model="tiny-model")
    assert per_result == expected_cap
    assert total == expected_cap


def test_window_derived_min_does_not_increase_for_large_registered_window():
    """大窓（登録値）でも、BUDGET-1 の解決値を超えて自動的には増えない（min() の対称性・
    「支出の自動拡大はしない」裁定）。"""
    from sherpa import model_windows
    sysset = {model_windows.MODEL_WINDOWS_KEY: {"openai:huge-model": 50_000_000},   # 5000万 token
             "agentic_budget_per_result": 5000, "agentic_budget_total": 20000}
    per_result, total = A.resolve_tool_result_budgets(sysset, provider="openai", model="huge-model")
    assert (per_result, total) == (5000, 20000)   # BUDGET-1 の解決値のまま（増えない）


def test_window_derived_min_registered_overrides_seed():
    """段1（登録値）は段3（シード表）より優先する（4段解決の優先順）——
    `gpt-4o-mini` はシード表に実在するが、登録値がある場合はそちらを使う。"""
    from sherpa import model_windows
    sysset = {model_windows.MODEL_WINDOWS_KEY: {"openai:gpt-4o-mini": 1000}}   # 極端に小さい登録値
    expected_cap = model_windows.derive_window_bytes(1000)
    per_result, _ = A.resolve_tool_result_budgets(sysset, provider="openai", model="gpt-4o-mini")
    assert per_result == expected_cap
    assert expected_cap != model_windows.derive_window_bytes(128_000)   # シード値とは異なる


def test_window_derived_min_seed_table_applies_for_known_openai_model():
    """段3（シード表）: 登録値が無くても、シード表に載っている実在モデル（gpt-4o-mini）なら
    窓が判明し、予算が窓由来の上限まで縮む。"""
    from sherpa import model_windows
    expected_cap = model_windows.derive_window_bytes(128_000)
    assert expected_cap < A.TOOL_RESULT_MAX_BYTES   # 前提
    per_result, _ = A.resolve_tool_result_budgets({}, provider="openai", model="gpt-4o-mini")
    assert per_result == expected_cap


def test_openai_style_derives_ollama_provider_and_base_url_for_budget_resolution(monkeypatch):
    """`openai_style(ollama=True, ...)` は `resolve_tool_result_budgets` へ
    `provider="ollama"`・`model`・chat URL から逆算した `ollama_base_url` を渡す。"""
    captured = {}
    orig = A.resolve_tool_result_budgets

    def spy(system_settings=None, **kw):
        captured.update(kw)
        return orig(system_settings, **kw)
    monkeypatch.setattr(A, "resolve_tool_result_budgets", spy)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})

    seq = [{"message": {"content": "回答"}}]
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        list(A.openai_style("http://localhost:11434/api/chat", {}, "qwen2.5", A.SYSTEM, "質問", "v1",
                            None, ollama=True, max_turns=1))
    finally:
        A._post = orig_post
    assert captured.get("provider") == "ollama"
    assert captured.get("model") == "qwen2.5"
    assert captured.get("ollama_base_url") == "http://localhost:11434"


def test_openai_style_derives_openai_provider_for_budget_resolution(monkeypatch):
    """`openai_style(ollama=False, ...)`（既定）は `provider="openai"` を渡す・`ollama_base_url`
    は None（Ollama 以外は窓のライブ照会対象外）。"""
    captured = {}
    orig = A.resolve_tool_result_budgets

    def spy(system_settings=None, **kw):
        captured.update(kw)
        return orig(system_settings, **kw)
    monkeypatch.setattr(A, "resolve_tool_result_budgets", spy)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})

    seq = [{"choices": [{"message": {"content": "回答"}}]}]
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None, max_turns=1))
    finally:
        A._post = orig_post
    assert captured.get("provider") == "openai"
    assert captured.get("model") == "gpt-5.5"
    assert captured.get("ollama_base_url") is None


def test_anthropic_style_derives_bedrock_provider_and_client_for_budget_resolution(monkeypatch):
    """`anthropic_style(...)`（本アプリ唯一の呼び出し元は Bedrock）は `provider="bedrock"`・
    `model`・`anthropic_client=client` を渡す。`_AClient`（テスト用スタブ）は `.models` を
    持たない＝実 `AnthropicBedrock` と同じ形のため、窓のライブ照会は安全に no-op になる。"""
    captured = {}
    orig = A.resolve_tool_result_budgets

    def spy(system_settings=None, **kw):
        captured.update(kw)
        return orig(system_settings, **kw)
    monkeypatch.setattr(A, "resolve_tool_result_budgets", spy)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})

    seq = [_AResp([_ABlock("text", "回答")], stop_reason="end_turn")]
    client = _AClient(seq)
    list(A.anthropic_style(client, "anthropic.claude-opus-4-8", A.SYSTEM, "質問", "v1", None))
    assert captured.get("provider") == "bedrock"
    assert captured.get("model") == "anthropic.claude-opus-4-8"
    assert captured.get("anthropic_client") is client


def test_gemini_derives_gemini_provider_for_budget_resolution(monkeypatch):
    """`gemini(...)` は `provider="gemini"` を渡す。"""
    captured = {}
    orig = A.resolve_tool_result_budgets

    def spy(system_settings=None, **kw):
        captured.update(kw)
        return orig(system_settings, **kw)
    monkeypatch.setattr(A, "resolve_tool_result_budgets", spy)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})

    seq = [{"candidates": [{"content": {"parts": [{"text": "回答"}]}}]}]
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        list(A.gemini("key", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post = orig_post
    assert captured.get("provider") == "gemini"
    assert captured.get("model") == "gemini-2.5-flash"


def test_run_tool_explicit_tool_result_max_bytes_overrides_module_default(monkeypatch, tmp_path):
    """`run_tool(tool_result_max_bytes=...)` は明示指定の値でクリップする（値の出所（settings/env/
    コード既定のどれで解決されたか）に関わらず `run_tool` 自体は受け取った実効値を使うだけ、という
    契約を固定する）。モジュール既定を大きく設定していても、明示指定の小さい値でクリップされる。"""
    world = "read-doc-explicit-budget-world"
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 10_000_000)   # 明示指定が勝つことを示すため大きく設定
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "x" * 5000})
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "big.md"}, world, None, tool_result_max_bytes=200)
    assert "error" not in res, res
    assert res.get("text_truncated") is True
    assert len(res["text"].encode("utf-8")) <= 200


# ===== secRV FIX-1（2026-07-19・拒否ツール結果のバイト迂回） =====

def test_openai_style_rejected_tool_name_clipped_in_tool_message():
    """(a): 許可外ツール拒否時の tool-result に埋める name（モデル生成・長さ無制限）は
    固定長（`_REJECTED_TOOL_NAME_MAX_BYTES`）へクリップされる。"""
    huge_name = "X" * 5000
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": huge_name, "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    captured = []
    orig = A._post

    def spy_post(url, headers, body, timeout=90):
        captured.append(body)
        return seq.pop(0)

    A._post = spy_post
    try:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                            allowed_tools=frozenset({"ripgrep_search"})))
        # 2ターン目のリクエストに含まれる直前の tool message（拒否結果）を検査する。
        msgs2 = captured[1]["messages"]
        tool_msg = next(m for m in msgs2 if m.get("role") == "tool" and m.get("tool_call_id") == "c1")
        assert len(tool_msg["name"].encode("utf-8")) <= A._REJECTED_TOOL_NAME_MAX_BYTES
        assert huge_name not in tool_msg["name"]
        assert huge_name not in tool_msg["content"]
    finally:
        A._post = orig


def test_openai_style_rejected_tool_result_counted_in_cumulative_bytes(monkeypatch):
    """(b): 拒否 tool-result も `total_tool_bytes` の累計へ計上され、1 run 累計バイト上限判定を
    すり抜けない（以前はこの経路だけ計上されず、累計上限を無制限に迂回できた）。"""
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", 10)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "some_disallowed_tool", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "final (should not be reached)"}}]},
    ]
    post_calls = []

    def fake_post(url, headers, body, timeout=90):
        post_calls.append(1)
        return seq.pop(0)

    orig = A._post
    A._post = fake_post
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                     allowed_tools=frozenset({"ripgrep_search"})))
        assert len(post_calls) == 1   # 拒否ツール1件だけで累計上限超過＝2ターン目へは進まない
        assert any(ev.get("node", {}).get("label") == "ツール結果の合計サイズ上限" for ev in events)
        final = next(ev for ev in events if "final" in ev)
        assert final["final"] == ""
    finally:
        A._post = orig


# ===== secRV FIX-2（2026-07-19・cards サイドカーのバイト迂回） =====

def test_clip_cards_limits_count():
    cards = [{"name": f"c{i}", "evidence": {}} for i in range(1000)]
    clipped = A._clip_cards(cards, max_count=30, max_bytes=10_000_000)
    assert len(clipped) == 30


def test_clip_cards_respects_byte_cap_even_under_count_cap():
    big_card = {"name": "x" * 1000, "evidence": {}}
    cards = [dict(big_card) for _ in range(100)]
    clipped = A._clip_cards(cards, max_count=100, max_bytes=2000)
    assert 1 <= len(clipped) < 100   # バイト予算で件数上限より先に打ち切られる


def test_clip_cards_rejects_oversized_single_card_even_when_out_is_empty():
    """secRV FIX-M1（2026-07-19・単一巨大カードが個別上限を迂回）: 以前は `out` が空（先頭カード）
    だと無条件で1件通してしまっていた（実測: 単一 10,030 byte カードが 100 byte 上限でも通過）。
    是正後は先頭カードも例外なくバイト上限で判定され、単体で上限を超えるカードは1件も採用されない
    （空リストを返す＝fail-closed。「最低1件は返す」設計は撤去）。"""
    oversized = {"name": "x" * 10000, "evidence": {}}
    clipped = A._clip_cards([oversized, dict(oversized)], max_count=30, max_bytes=100)
    assert clipped == []


def test_graph_neighbors_cards_sidecar_clipped_to_graph_cards_max():
    """FIX-2: `run_tool` の `graph_neighbors` は cards（4つ目の戻り値・troubleshoot サイドカー）も
    `_GRAPH_CARDS_MAX` 件に切り詰める（以前は LLM 向け `view` のみ制限し、cards は無制限に返していた）。
    grep/es のヒット数上限 `MAX_HITS`（env で変わりうる）とは独立の固定値であることも固定する。"""
    from sherpa import lens_service
    fake = [{"name": f"c{i}", "role": "実装", "category": "プログラム", "distance": 1,
            "path": [], "evidence": {}} for i in range(1000)]
    orig = lens_service.neighbor_cards
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    try:
        res, docs, cites, cards = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
        assert len(cards) == A._GRAPH_CARDS_MAX == 30
        assert len(res["neighbors"]) == A._GRAPH_CARDS_MAX == 30
    finally:
        lens_service.neighbor_cards = orig


def _graph_neighbors_count_script() -> str:
    """`SHERPA_GREP_MAX_HITS`（`MAX_HITS`）を変えても `graph_neighbors` のカード件数上限
    （`_GRAPH_CARDS_MAX`）は 30 のまま動かないことを、実プロセスの `run_tool()` 越しに観測する。"""
    return (
        "import json, os\n"
        "os.environ.setdefault('SHERPA_USE_FIXTURES', '1')\n"
        "import sherpa.lens_service as lens_service\n"
        "fake = [{'name': f'c{i}', 'role': 'x', 'category': 'x', 'distance': 1,\n"
        "         'path': [], 'evidence': {}} for i in range(1000)]\n"
        "lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)\n"
        "import sherpa.agentic_search as A\n"
        "res, docs, cites, cards = A.run_tool('graph_neighbors', {'name': 'x'}, 'v1', None)\n"
        "print(json.dumps({'max_hits': A.MAX_HITS, 'graph_cards_max': A._GRAPH_CARDS_MAX,\n"
        "                   'n_cards': len(cards), 'n_view': len(res['neighbors'])}))\n"
    )


def test_graph_neighbors_count_unaffected_by_grep_max_hits_env_set_to_1():
    out = json.loads(FI.run_script(_graph_neighbors_count_script(), env={"SHERPA_GREP_MAX_HITS": "1"}))
    assert out["max_hits"] == 1                # grep/es 側は env どおり 1 に下がる
    assert out["graph_cards_max"] == 30        # graph_neighbors 側は無関係・従来の 30 のまま
    assert out["n_cards"] == 30
    assert out["n_view"] == 30


def test_graph_neighbors_count_unaffected_by_grep_max_hits_env_set_to_100():
    out = json.loads(FI.run_script(_graph_neighbors_count_script(), env={"SHERPA_GREP_MAX_HITS": "100"}))
    assert out["max_hits"] == 100               # grep/es 側は env どおり 100 に上がる
    assert out["graph_cards_max"] == 30         # graph_neighbors 側は無関係・従来の 30 のまま
    assert out["n_cards"] == 30
    assert out["n_view"] == 30


def test_openai_style_cards_sidecar_bytes_counted_in_cumulative_cap(monkeypatch):
    """FIX-2: cards サイドカーの直列化バイトも `total_tool_bytes` の累計へ計上される
    （以前は計上されず、この経路だけ累計上限をすり抜けられた）。"""
    from sherpa import lens_service
    fake = [{"name": f"cand{i}", "role": "実装", "category": "プログラム", "distance": 1,
            "path": [], "evidence": {}} for i in range(30)]
    orig_cards = lens_service.neighbor_cards
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", 50)   # cards 30件分より確実に小さい
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"請求"}'}}]}}]},
        {"choices": [{"message": {"content": "final (should not be reached)"}}]},
    ]
    post_calls = []

    def fake_post(url, headers, body, timeout=90):
        post_calls.append(1)
        return seq.pop(0)

    orig_post = A._post
    A._post = fake_post
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "原因は?", "v1", None))
        assert len(post_calls) == 1
        assert any(ev.get("node", {}).get("label") == "ツール結果の合計サイズ上限" for ev in events)
    finally:
        A._post = orig_post
        lens_service.neighbor_cards = orig_cards


# ===== secRV FIX-3（2026-07-19・read_around の open symlink TOCTOU・軽量是正） =====

def test_read_around_rejects_symlink_at_open_time_toctou(monkeypatch, tmp_path):
    """secRV FIX-3（2026-07-19・open の symlink TOCTOU・軽量是正）: `_safe_doc_path()` の検査
    （realpath 確認・symlink 拒否）から実際の `open()` までの間に窓があり、検査済みファイルが
    外部秘密への symlink に競合差し替えられると、素朴な `open(p, "rb")` は追跡してしまう。

    `_safe_doc_path` 自身は resolve() 済みの realpath で symlink を検出済みのため、実際の
    競合レース（別プロセスによる差し替え）はタイミング依存で単体テストとして再現できない。
    「open() 直前の瞬間だけ symlink だった」という到達状態を、`_safe_doc_path` の返り値
    （`(root, lexical_rel, path)`）の `path` を世界 root 配下の symlink パスへ差し替えることで
    固定する（FIX-L 是正後は root からの dir_fd walk により独立に再検証されるため、mock 先も
    実際の world root 配下に置く必要がある）。`open()` 側の防御（`O_NOFOLLOW`）が単体で正しく
    機能する（symlink を辿らず fail する）ことを検証する。
    """
    world = "toctou-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"placeholder.md": "x"})
    kb_world = tmp_path / "kb" / world

    secret = tmp_path / "outside_secret.txt"
    secret.write_text("SECRET OUTSIDE WORLD ROOT", encoding="utf-8")
    swapped = kb_world / "swapped_doc.md"
    swapped.symlink_to(secret)

    monkeypatch.setattr(A, "_safe_doc_path", lambda w, doc_id, layer=None: (kb_world, "swapped_doc.md", swapped))
    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "swapped_doc.md", "line": 1, "window": 5}, world, None)
    assert "error" in res
    assert "SECRET" not in str(res)


def test_read_around_rejects_ancestor_symlink_toctou(monkeypatch, tmp_path):
    """secRV FIX-L（2026-07-19・read_around の祖先 symlink TOCTOU）: FIX-3 の `O_NOFOLLOW` は
    最終パス要素にしか効かない。対象ファイルの**中間ディレクトリ**（doc の祖先）が保護対象
    （world root 外）への symlink に差し替えられていても、単発 open はそれを追跡してしまう。

    `_open_file_nofollow_walk` は world root を信頼アンカーに、相対パスの各要素を dir_fd 相対で
    `O_NOFOLLOW` により1段ずつ辿るため、中間ディレクトリが symlink であればその段で拒否される
    （最終ファイルへ到達する前に fail-closed）。
    """
    world = "toctou-ancestor-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"placeholder.md": "x"})
    kb_world = tmp_path / "kb" / world

    secret_dir = tmp_path / "outside_secret_dir"
    secret_dir.mkdir()
    (secret_dir / "doc.md").write_text("SECRET OUTSIDE WORLD ROOT", encoding="utf-8")

    # world root 配下の中間ディレクトリ "sub" が、検査後に外部ディレクトリへの symlink に
    # 差し替えられた、を模す（最初から symlink として用意する＝到達状態を固定）。
    sub_symlink = kb_world / "sub"
    sub_symlink.symlink_to(secret_dir)
    swapped_target = kb_world / "sub" / "doc.md"   # 文字列としては world root 配下の通常パス

    monkeypatch.setattr(A, "_safe_doc_path", lambda w, doc_id, layer=None: (kb_world, "sub/doc.md", swapped_target))
    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "sub/doc.md", "line": 1, "window": 5}, world, None)
    assert "error" in res
    assert "SECRET" not in str(res)


def test_read_around_normal_file_still_readable_after_fix3(monkeypatch, tmp_path):
    """正常系回帰: symlink でない通常ファイル（world root 配下）は dir_fd walk 経由でも従来どおり
    読める（既定 OFF・メイン経路 byte-identical の要件）。"""
    world = "normal-read-world"
    content = "1行目\n2行目\nTAX-RATE 3行目\n4行目\n5行目\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"plain.md": content})

    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "plain.md", "line": 3, "window": 1}, world, None)
    assert "error" not in res
    assert "TAX-RATE" in res["text"]


def test_read_around_normal_nested_file_still_readable(monkeypatch, tmp_path):
    """正常系回帰: 中間ディレクトリを含む通常のネストしたファイルも dir_fd walk 経由で読める
    （祖先が全て通常ディレクトリの場合は従来どおり成功する）。"""
    world = "nested-read-world"
    content = "1行目\nTAX-RATE 2行目\n3行目\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"a/b/nested.md": content})

    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "a/b/nested.md", "line": 2, "window": 1}, world, None)
    assert "error" not in res
    assert "TAX-RATE" in res["text"]


# ===== secRV FIX-N（2026-07-19・既存 symlink による scope/拡張子迂回） =====

def test_read_around_rejects_existing_symlink_scope_bypass(monkeypatch, tmp_path):
    """secRV FIX-N: scope 内に見える doc_id（`public/link.md`）が実は scope 外
    （`private/secret.md`）への**既存 symlink** の場合、`_safe_doc_path` 自体の resolve() ベースの
    検査は「world root 配下」という条件だけで通過してしまう（`scope_mod.in_scope()` は doc_id の
    文字列にしか効かず、resolve 後の実体までは見ない＝scope 迂回）。是正後は lexical walk が
    symlink 自体を `O_NOFOLLOW` で拒否し、本文は一切返らない（scope 内で完結）。"""
    world = "fixn-scope-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"private/secret.md": "TOP SECRET CONTENT"})
    kb_world = tmp_path / "kb" / world
    (kb_world / "public").mkdir(parents=True, exist_ok=True)
    (kb_world / "public" / "link.md").symlink_to(kb_world / "private" / "secret.md")

    res, docs, _, _ = A.run_tool(
        "read_around", {"doc_id": "public/link.md", "line": 1, "window": 5}, world, ["public"])
    assert "error" in res
    assert "TOP SECRET" not in str(res)


def test_read_around_rejects_existing_symlink_extension_bypass(monkeypatch, tmp_path):
    """secRV FIX-N: 許可拡張子（`.md`）を装った doc_id（`x.md`）が、実は禁止種別（`.json`）への
    **既存 symlink** の場合、`_safe_doc_path` は doc_id の見かけの拡張子でしか判定しないため
    （resolve 後の実体の拡張子は見ない）通過してしまう。是正後は lexical walk が symlink 自体を
    拒否し、禁止種別の内容は一切返らない。"""
    world = "fixn-ext-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"secret.json": '{"leaked": true}'})
    kb_world = tmp_path / "kb" / world
    (kb_world / "x.md").symlink_to(kb_world / "secret.json")

    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "x.md", "line": 1, "window": 5}, world, None)
    assert "error" in res
    assert "leaked" not in str(res)


def test_read_around_normal_document_regression_after_fix_n(monkeypatch, tmp_path):
    """正常系回帰（非 Office）: symlink を介さない通常のネストしたドキュメントは、lexical walk
    経由でも従来どおり本文が返る（正常系は不変）。"""
    world = "fixn-normal-world"
    content = "1行目\nTAX-RATE 2行目\n3行目\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"public/normal.md": content})

    res, docs, _, _ = A.run_tool(
        "read_around", {"doc_id": "public/normal.md", "line": 2, "window": 1}, world, ["public"])
    assert "error" not in res
    assert "TAX-RATE" in res["text"]


def test_read_around_office_document_regression_after_fix_n(monkeypatch, tmp_path):
    """正常系回帰（Office 派生 MD）: `ext in _OFFICE_MD` の文書は `doc_id + ".md"` の派生 MD
    相対パスを lexical に辿るが、symlink を介さない通常配置なら従来どおり本文が返る。"""
    world = "fixn-office-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {})   # world root 自体は空でよい（office は derived 側）
    derived = tmp_path / "derived"
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(derived))
    md_dir = derived / world / "md"
    md_dir.mkdir(parents=True)
    content = "1行目\nTAX-RATE 2行目\n3行目\n"
    (md_dir / "report.docx.md").write_text(content, encoding="utf-8")

    res, docs, _, _ = A.run_tool("read_around", {"doc_id": "report.docx", "line": 2, "window": 1}, world, None)
    assert "error" not in res
    assert "TAX-RATE" in res["text"]


# ===== secRV FIX-Q（2026-07-19・anchor（world root）の祖先 symlink 競合） =====

def test_open_file_nofollow_walk_rejects_when_anchor_itself_is_symlink(tmp_path):
    """secRV FIX-Q: `_open_file_nofollow_walk` は anchor（world root 等）も `/` から dir_fd 相対で
    walk する。anchor 自身が保護対象外への symlink に差し替えられていれば、その段で `OSError`
    となり fail-closed で拒否される（単発 `os.open(str(anchor), O_NOFOLLOW)` は anchor 自身を
    保護できていたが、以前は anchor の**祖先**までは保護できていなかった＝本テストは anchor 自身の
    保護が walk 化後も維持されていることの回帰確認）。"""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "file.txt").write_text("REAL CONTENT", encoding="utf-8")

    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(OSError):
        A._open_file_nofollow_walk(link, ("sub", "file.txt"))


def test_open_file_nofollow_walk_reads_through_real_anchor(tmp_path):
    """正常系回帰: anchor が symlink でない通常ディレクトリなら、anchor 祖先までの walk 追加後も
    従来どおり fd が返り、内容が読める。"""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "file.txt").write_text("REAL CONTENT", encoding="utf-8")

    fd = A._open_file_nofollow_walk(real, ("sub", "file.txt"))
    try:
        with os.fdopen(fd, "rb") as f:
            assert f.read() == b"REAL CONTENT"
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def test_open_file_nofollow_walk_rejects_ancestor_of_anchor_symlink(tmp_path):
    """secRV FIX-Q の核心: anchor 自身は symlink でなくても、anchor の**祖先**（`base`）が保護対象外
    への symlink に差し替えられていれば拒否される。単発 `os.open(str(anchor), O_NOFOLLOW)` は
    anchor 自身の最終パス要素にしか symlink 拒否が効かず（POSIX 仕様）、祖先レベルの差し替えは
    素通りしてしまっていた。"""
    base = tmp_path / "base"
    (base / "world_root" / "sub").mkdir(parents=True)
    (base / "world_root" / "sub" / "file.txt").write_text("REAL CONTENT", encoding="utf-8")
    anchor = base / "world_root"

    # symlink 先にも anchor と同じ相対構造（world_root/sub/file.txt）を用意する。旧実装
    # （単発 `os.open(str(anchor), O_NOFOLLOW)`）ならこの構造で SECRET 側の open が**成功**して
    # しまう＝walk 化で初めて拒否される、を確認する（構造が無いと旧実装でも ENOENT で通ってしまい
    # 回帰テストにならない・secRV 7巡目指摘）。
    outside_secret = tmp_path / "outside_secret"
    (outside_secret / "world_root" / "sub").mkdir(parents=True)
    (outside_secret / "world_root" / "sub" / "file.txt").write_text("SECRET OUTSIDE", encoding="utf-8")

    # 検証後、anchor の祖先（base）が保護対象外ディレクトリへの symlink に差し替えられた、を模す。
    base.rename(tmp_path / "base_moved")
    base.symlink_to(outside_secret)

    # 旧実装なら成功してしまう構造であることを自己検証（テスト自身の健全性チェック）。
    legacy_fd = os.open(str(anchor / "sub" / "file.txt"), os.O_RDONLY)
    os.close(legacy_fd)

    with pytest.raises(OSError):
        A._open_file_nofollow_walk(anchor, ("sub", "file.txt"))


def test_open_file_nofollow_walk_rejects_dotdot_in_anchor(tmp_path):
    """secRV FIX-V（7巡目 LOW#1）: anchor に `..` 要素が含まれると、`os.path.abspath()` の lexical
    正規化が `symlink/..` を字面で潰し、`_safe_doc_path()`（symlink を辿って検証）と walk（潰した
    パスを open）で対象が食い違いうる。`..` を含む生 anchor は fail-closed で拒否する。"""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "file.txt").write_text("REAL CONTENT", encoding="utf-8")

    # 実体としては同じ場所を指す `..` 入り anchor でも、正規化の食い違いを避けるため一律拒否。
    dotted = tmp_path / "real" / "sub" / ".."
    with pytest.raises(OSError):
        A._open_file_nofollow_walk(dotted, ("sub", "file.txt"))


# ===== secRV FIX-W（7巡目 LOW#2・security-limit env の負値/巨大値） =====

def test_env_int_falls_back_on_invalid_values(monkeypatch):
    """secRV FIX-W: security-limit 系 env（`SHERPA_AGENTIC_MAX_TOOLS_PER_TURN` 等）に負値を渡すと
    `calls[:-1]`／`b[:-1]` のようにスライスが反転して上限が実質無効化されていた。`_env_int` は
    範囲外（負値・0・hard cap 超え）・非整数を全て既定値へ戻す。"""
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "-1")
    assert A._env_int("SHERPA_TEST_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "0")
    assert A._env_int("SHERPA_TEST_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "999999")
    assert A._env_int("SHERPA_TEST_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "abc")
    assert A._env_int("SHERPA_TEST_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "32")
    assert A._env_int("SHERPA_TEST_LIMIT", 16, 1, 256) == 32
    monkeypatch.delenv("SHERPA_TEST_LIMIT")
    assert A._env_int("SHERPA_TEST_LIMIT", 16, 1, 256) == 16


def test_env_int_clamps_dynamic_default(monkeypatch):
    """secRV FIX-X（8巡目 LOW）: `_env_int` は**既定値側**も [lo, hi] へクランプする。total の既定は
    per-call 値×16 の動的値のため、per-call を許容上限（8MiB）に設定すると既定 128MiB が
    hard cap 64MiB を素通りしていた（env 未設定・不正値の fallback 経路が未検証だった）。"""
    hi = 64 * 1024 * 1024
    big_default = 8 * 1024 * 1024 * 16   # per=8MiB 時の動的既定（128MiB）> hard cap
    monkeypatch.delenv("SHERPA_TEST_LIMIT", raising=False)
    assert A._env_int("SHERPA_TEST_LIMIT", big_default, 4096, hi) == hi
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "-1")   # 不正値 → 既定へ fallback してもクランプ済み
    assert A._env_int("SHERPA_TEST_LIMIT", big_default, 4096, hi) == hi
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "abc")
    assert A._env_int("SHERPA_TEST_LIMIT", big_default, 4096, hi) == hi
    # lo 側のクランプも対称に確認。
    assert A._env_int("SHERPA_TEST_LIMIT", 1, 4096, hi) == 4096


# ===== MAX_HITS / READ_WINDOW（コード内定数の env 化） =====
# import 時に一度だけ確定する定数は、同一プロセス内の monkeypatch では「起動時の env」を
# 再現できないため、実プロセスを新規に起こして検証する（`_fresh_import` 参照）。

def test_max_hits_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.agentic_search", "MAX_HITS",
                                env={"SHERPA_GREP_MAX_HITS": None}) == 30


def test_max_hits_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.agentic_search", "MAX_HITS",
                                env={"SHERPA_GREP_MAX_HITS": "100"}) == 100


def test_max_hits_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("0", "1001", "abc"):
        assert FI.fresh_import_attr("sherpa.agentic_search", "MAX_HITS",
                                    env={"SHERPA_GREP_MAX_HITS": bad}) == 30, bad


def test_max_hits_env_change_after_import_has_no_effect(monkeypatch):
    """import 済みの `MAX_HITS` は起動時に確定済み＝同一プロセス内で env を後から変えても効かない。"""
    before = A.MAX_HITS
    monkeypatch.setenv("SHERPA_GREP_MAX_HITS", "999")
    assert A.MAX_HITS == before == 30


def test_read_window_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.agentic_search", "READ_WINDOW",
                                env={"SHERPA_READ_WINDOW": None}) == 40


def test_read_window_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.agentic_search", "READ_WINDOW",
                                env={"SHERPA_READ_WINDOW": "80"}) == 80


def test_read_window_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("5", "401", "abc"):
        assert FI.fresh_import_attr("sherpa.agentic_search", "READ_WINDOW",
                                    env={"SHERPA_READ_WINDOW": bad}) == 40, bad


def test_read_window_env_change_after_import_has_no_effect(monkeypatch):
    before = A.READ_WINDOW
    monkeypatch.setenv("SHERPA_READ_WINDOW", "300")
    assert A.READ_WINDOW == before == 40


def _read_around_run_tool_script(doc_lines: int, center_line: int, window_arg: int | None = None) -> str:
    """`read_around` のツール説明（window の既定値通知）と `run_tool()` の実際の挙動（返却行数）を、
    同一プロセス内で **同じ式を再計算せず** `run_tool()` 越しに観測するスクリプト。世界は tmp 上に
    自前で組み、`SHERPA_KB_DIR`／`store.get_world` の隔離は
    `tests/unit/test_agentic_search.py::_isolate_world_kb` と同じ手法をスクリプト内で直接行う
    （別プロセスのため monkeypatch は使えない）。`window_arg` を渡すと `run_tool` の引数に明示
    `window` を含める（省略時は既定値 `READ_WINDOW` の経路を試す）。"""
    args_literal = f"'doc_id': 'big.md', 'line': {center_line}"
    if window_arg is not None:
        args_literal += f", 'window': {window_arg}"
    return (
        "import json, os, tempfile\n"
        "tmp = tempfile.mkdtemp()\n"
        "wd = os.path.join(tmp, 'kb', 'freshworld')\n"
        "os.makedirs(wd, exist_ok=True)\n"
        f"content = chr(10).join(f'line {{i}}' for i in range(1, {doc_lines + 1}))\n"
        "with open(os.path.join(wd, 'big.md'), 'w', encoding='utf-8') as f:\n"
        "    f.write(content)\n"
        "os.environ['SHERPA_KB_DIR'] = os.path.join(tmp, 'kb')\n"
        "os.environ.pop('SHERPA_USE_FIXTURES', None)\n"
        "for _e in ('SHERPA_MCP_WORLD', 'SHERPA_MCP_WORLD_ROOT'):\n"
        "    os.environ.pop(_e, None)\n"
        "import sherpa.store as store\n"
        "store.get_world = lambda world_id: None\n"
        "import sherpa.agentic_search as A\n"
        "desc = A._PARAMS_READ['properties']['window']['description']\n"
        f"res, docs, _, _ = A.run_tool('read_around', {{{args_literal}}}, 'freshworld', None)\n"
        "n_lines = len(res['text'].strip().split(chr(10)))\n"
        "print(json.dumps({'read_window': A.READ_WINDOW, 'desc': desc, 'n_lines': n_lines}))\n"
    )


def test_read_around_tool_description_matches_actual_default_window_behavior():
    """`_PARAMS_READ` の window 説明文（モデルへの通知）は実際の `READ_WINDOW` を埋め込んでおり、
    `window` 省略時の `run_tool("read_around", ...)` の実挙動（返却行数）とも一致する
    （説明文とツールの実配線を `run_tool()` 越しに固定・同じ式を2箇所に書いて re-derive しない）。"""
    out = json.loads(FI.run_script(_read_around_run_tool_script(200, 100),
                                   env={"SHERPA_READ_WINDOW": "80"}))
    assert out["read_window"] == 80
    assert "80" in out["desc"]
    assert out["n_lines"] == 2 * 80 + 1   # line=100・window=80 は境界に掛からない


def test_read_around_tool_description_matches_default_when_env_unset():
    out = json.loads(FI.run_script(_read_around_run_tool_script(200, 100),
                                   env={"SHERPA_READ_WINDOW": None}))
    assert out["read_window"] == 40
    assert "40" in out["desc"]
    assert out["n_lines"] == 2 * 40 + 1


def test_read_around_window_ceiling_tracks_read_window_when_raised_above_200():
    """read_around の LLM 入力窓ハード上限（200）は、明示 `window` 引数に対しても
    `SHERPA_READ_WINDOW` を200超に上げたときだけ追随する（既定時は200のまま・後退しない）。
    `run_tool()` を実際に呼び、返却行数から上限を観測する（式を再計算しない）。"""
    script = _read_around_run_tool_script(700, 350, window_arg=300)
    out = json.loads(FI.run_script(script, env={"SHERPA_READ_WINDOW": "300"}))
    assert out["read_window"] == 300
    assert out["n_lines"] == 2 * 300 + 1   # 200 を後退させず、明示 window=300 がそのまま通る

    out_default = json.loads(FI.run_script(script, env={"SHERPA_READ_WINDOW": None}))
    assert out_default["read_window"] == 40
    assert out_default["n_lines"] == 2 * 200 + 1   # 既定時は明示 window=300 でも 200 で頭打ち


# ===== secRV FIX-H（2026-07-19・実行 allowlist の非対称）: メイン経路も offered_names で制限 =====

def test_openai_style_main_path_rejects_tool_not_offered():
    """メイン経路（`allowed_tools=None`・既定）でも、実際に提示していないツール名をモデルが
    呼べば拒否され `run_tool` は実行されない（以前は提示外でも実行されうる非対称があった）。
    提示済みツール（ripgrep_search）は従来どおり実行される＝正常系は不変。"""
    restricted_toolset = [{"type": "function", "function": {
        "name": "ripgrep_search", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    called = []
    orig_run_tool = A.run_tool

    def spy_run_tool(name, *a, **kw):
        called.append(name)
        return orig_run_tool(name, *a, **kw)

    A.run_tool = spy_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                     toolset=restricted_toolset))
        assert "graph_neighbors" not in called   # 提示外＝実行されない
        assert "ripgrep_search" in called        # 提示内＝正常実行（正常系不変）
        assert any(ev.get("node", {}).get("label") == "許可外のツール呼び出し" for ev in events)
    finally:
        A._post = orig_post
        A.run_tool = orig_run_tool


def test_gemini_main_path_rejects_tool_not_offered():
    """gemini の実行 allowlist も `offered_names` で制限される（提示していない
    graph_neighbors は拒否・提示済み ripgrep_search は実行）。"""
    restricted_toolset = [{"functionDeclarations": [{"name": "ripgrep_search", "description": "d",
                                                     "parameters": {"type": "object", "properties": {}}}]}]
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "graph_neighbors", "args": {"name": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "TAX-RATE"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
    ]
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    called = []
    orig_run_tool = A.run_tool

    def spy_run_tool(name, *a, **kw):
        called.append(name)
        return orig_run_tool(name, *a, **kw)

    A.run_tool = spy_run_tool
    try:
        events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "調べて", "v1", None,
                               toolset=restricted_toolset))
        assert "graph_neighbors" not in called
        assert "ripgrep_search" in called
        assert any(ev.get("node", {}).get("label") == "許可外のツール呼び出し" for ev in events)
    finally:
        A._post = orig_post
        A.run_tool = orig_run_tool


def test_anthropic_style_main_path_rejects_tool_not_offered():
    """anthropic_style の実行 allowlist も `offered_names` で制限される（提示していない
    graph_neighbors は拒否・提示済み ripgrep_search は実行）。"""
    restricted_toolset = [{"type": "function", "function": {
        "name": "ripgrep_search", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    seq = [
        _AResp([_ABlock("tool_use", name="graph_neighbors", input={"name": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "TAX-RATE"}, id="tu2")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "done")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    called = []
    orig_run_tool = A.run_tool

    def spy_run_tool(name, *a, **kw):
        called.append(name)
        return orig_run_tool(name, *a, **kw)

    A.run_tool = spy_run_tool
    try:
        events = list(A.anthropic_style(client, "m", A.SYSTEM, "調べて", "v1", None,
                                        toolset=restricted_toolset))
        assert "graph_neighbors" not in called
        assert "ripgrep_search" in called
        assert any(ev.get("node", {}).get("label") == "許可外のツール呼び出し" for ev in events)
    finally:
        A.run_tool = orig_run_tool


def test_openai_style_loop_stub():
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        nodes, final = [], None
        for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None):
            (nodes.append(ev["node"]) if "node" in ev else None)
            if "final" in ev:
                final = ev
        assert final and "TAX-RATE" in final["final"] and final["docs"]
        assert any("資料を検索" in n["label"] for n in nodes)
    finally:
        A._post = orig


# ---- RV MEDIUM（2026-07-03再検証）: 途中停止（stop_event）は各ターン発行前に確認 ----

def test_openai_style_stops_between_turns_when_stop_event_set_mid_flight():
    """1ターン目の応答が返ってきた直後に停止要求が来たケース＝2ターン目のリクエストは発行しない
    （「HTTP呼び出し自体の中断は不要＝次の境界で止まれば可」という設計どおり）。"""
    import threading

    stop_event = threading.Event()
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        stop_event.set()   # 1ターン目のレスポンスが返った直後に停止ボタンが押された、を模す
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}

    orig = A._post
    A._post = fake_post
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     stop_event=stop_event))
        assert len(calls) == 1, "停止後も2ターン目のリクエストが発行されている"
        assert not any("final" in ev for ev in events), "停止時に final を yield すべきでない"
    finally:
        A._post = orig


def test_openai_style_returns_immediately_if_already_stopped():
    """開始前から stop_event が立っていれば、1回も _post を呼ばない。"""
    import threading

    stop_event = threading.Event()
    stop_event.set()
    calls = []
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: calls.append(1)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     stop_event=stop_event))
        assert events == []
        assert calls == []
    finally:
        A._post = orig


def test_openai_style_stop_event_set_during_final_synthesis_skips_attribution(monkeypatch):
    """最終合成（turns_exhausted）の応答が返ってきた直後に停止要求が来た場合、帰属呼び出し
    （`submit_attribution`）は発行しない——帰属**直前**の再確認で捕捉する（tail 冒頭のチェック
    だけでは、その後の最終合成 `_post` の間に来た停止要求を捕まえられない）。"""
    import threading
    stop_event = threading.Event()
    calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, {_REAL_DOC},
               [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "x", "ext": ".md"}], [])

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        if len(calls) == 1:   # 1ターン目: tool_calls を返し turns_exhausted（最終合成）へ向かわせる
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        stop_event.set()   # 2回目＝最終合成の応答が返った直後に停止要求が来た、を模す
        return {"choices": [{"message": {"content": "最終回答"}}]}

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, fake_run_tool
    try:
        final = None
        for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                                 stop_event=stop_event, max_turns=1):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # tool turn + 最終合成の2回だけ（帰属の3回目は発行されない）
        assert final["final"] == "最終回答"
        assert final["stop_reason"] == "turns_exhausted"   # STOP-1: 到達可能経路の閉じた語彙を固定
        assert final["attributed_ev_ids"] == set()
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


def test_openai_style_finish_reason_length_skips_attribution(monkeypatch):
    """`finish_reason=="length"`（打ち切り＝未完了）で終わった応答は、たとえ本文があっても
    帰属呼び出しを発行しない（部分本文を確定回答として帰属しない）。"""
    calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, {_REAL_DOC},
               [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "x", "ext": ".md"}], [])

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        return {"choices": [{"message": {"content": "途中で切れた回答"}, "finish_reason": "length"}]}

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, fake_run_tool
    try:
        final = None
        for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                                 max_turns=1):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 帰属は発行されない（3回目は無い）
        assert final["final"] == "途中で切れた回答"
        assert final["attributed_ev_ids"] == set()
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


def test_final_synthesis_exception_logs_masked_warning(monkeypatch, caplog):
    """RV7 是正の固定: turns 上限到達時の最終合成（tools 無し）が例外で失敗した場合
    （`_synthesis_failed=True` に畳んで空回答へ縮退させる契約は維持）、元例外の型とマスク済み
    メッセージを WARNING ログへ残す——これまでは `except Exception:`（変数すら束縛しない）で
    例外そのものを完全に握り潰しており、診断の手掛かりが一切残らなかった。"""
    import logging

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, set(), [], [])

    secret = "sk-shouldnotleak1234567890"

    def fake_post(url, headers, body, timeout=90):
        if "tools" in body:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        raise RuntimeError(f"simulated final synthesis failure: Bearer {secret}")

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        final = None
        for ev in A.openai_style("http://x", {"Authorization": f"Bearer {secret}"}, "gpt-5.5",
                                 A.SYSTEM, "質問", "v1", None, max_turns=1):
            if "final" in ev:
                final = ev
    assert final["synthesis_failed"] is True
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged
    assert secret not in logged


def test_final_synthesis_connection_failure_tags_failure_kind_connection(monkeypatch):
    """最終合成の HTTP 呼び出しが接続失敗（`_send` が付与する `_sherpa_llm_send_error` マーカー
    付きで `_is_connection_failure` も真）で失敗した場合、`synthesis_failed=True` に加えて
    `failure_kind="connection"` を payload に残す（生の例外は載せない安全な分類値・
    `sherpa/research_service.py` が provider 付き専用文言へ倒す判別材料に使う）。"""
    import urllib.error

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, set(), [], [])

    def fake_post(url, headers, body, timeout=90):
        if "tools" in body:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    final = None
    for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None, max_turns=1):
        if "final" in ev:
            final = ev
    assert final["synthesis_failed"] is True
    assert final["failure_kind"] == "connection"


def test_final_synthesis_generic_exception_leaves_failure_kind_unset(monkeypatch):
    """対照実験: 接続失敗ではない汎用例外（RuntimeError）は `failure_kind` を立てない
    （既存の汎用「合成中に失敗」経路を維持する）。"""
    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, set(), [], [])

    def fake_post(url, headers, body, timeout=90):
        if "tools" in body:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        raise RuntimeError("boom")

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    final = None
    for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None, max_turns=1):
        if "final" in ev:
            final = ev
    assert final["synthesis_failed"] is True
    assert final.get("failure_kind") is None


def test_final_synthesis_connection_error_without_send_marker_leaves_failure_kind_unset(monkeypatch):
    """最終合成の try は `_send`（物理送信）だけでなく usage 加算・応答パースも同じ try で囲む。
    `_send` を経由しない（`_sherpa_llm_send_error` マーカーの無い）`ConnectionError` がそこで
    起きても `failure_kind="connection"` にはしない——型が `_is_connection_failure` と一致する
    だけでは倒れない（マーカーと型判定の AND 条件を固定する）。"""
    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, set(), [], [])

    def fake_post(url, headers, body, timeout=90):
        if "tools" in body:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        return {"choices": [{"message": {"content": "最終回答"}, "finish_reason": "stop"}]}

    real_acc_usage = A._acc_openai_usage
    calls = []

    def boom_acc_usage(acc, resp, ollama):
        calls.append(1)
        if len(calls) > 1:   # 1回目（tool-turn の usage 加算）は素通し・2回目（最終合成）だけ失敗させる
            raise ConnectionError("not raised by _send")
        return real_acc_usage(acc, resp, ollama)

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    monkeypatch.setattr(A, "_acc_openai_usage", boom_acc_usage)
    final = None
    for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None, max_turns=1):
        if "final" in ev:
            final = ev
    assert final["synthesis_failed"] is True
    assert final.get("failure_kind") is None


def test_final_synthesis_skips_send_when_stop_event_fires_after_tool_execution(monkeypatch):
    """RV8 是正の固定: turns 上限到達時、ツール実行完了直後（tail 冒頭の停止確認の直前）に
    watchdog（stop_event）が発火した場合、最終合成を新規送信しない——「停止時は final を出さない」
    契約どおり、final を一切 yield せずに終了する（旧実装は tail 冒頭で1回確認するだけで、
    送信直前の再確認が無かったため、この窓で新規送信できてしまっていた）。

    FB統合後: 呼び出し予算の消費・usage 加算は `_send`（`llm.begin_openai_send`/`_consume_call`）が
    自分で行うため、`_consume_call` を直接スパイして発火タイミングを作る旧来の手法は使えない
    （OpenAI 経路では `_consume_call` 自体が呼ばれなくなった）。`run_tool`（turn 0 のツール実行）
    完了直後に stop_event を立てることで、同じ「ツール実行後・tail 開始前」の窓を模す。"""
    import threading

    stop_event = threading.Event()

    def fake_run_tool(name, args, world, scope_paths, **kw):
        stop_event.set()   # ツール実行完了直後（tail 開始前）に watchdog が発火した窓を模す
        return ({"hits": []}, set(), [], [])

    def fake_post(url, headers, body, timeout=90):
        if "tools" in body:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        raise AssertionError("stop_event 発火後に最終合成を送信してはいけない")

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                                 max_turns=1, stop_event=stop_event))
    assert not any("final" in ev for ev in events)


def test_final_synthesis_skips_send_when_stop_event_fires_during_node_yield(monkeypatch):
    """RV9 是正の固定: 「ここまでに集めた資料で回答をまとめます」の node yield は呼び出し元へ
    制御を戻す——再開までにかかる時間は呼び出し元次第（chat の UI 停止操作・PART-4 の watchdog
    とも、この yield の間隔で stop_event が立ちうる）。yield 復帰直後（送信直前）で
    stop_event を再確認せず、その前のチェックだけに頼っていると、通常チャットでも余分な
    送信が1回発生し・停止時に final を出さない契約が破れる。"""
    import threading

    stop_event = threading.Event()

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, set(), [], [])

    def fake_post(url, headers, body, timeout=90):
        if "tools" in body:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        raise AssertionError("stop_event 発火後に最終合成を送信してはいけない")

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)

    gen = A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                         max_turns=1, stop_event=stop_event)
    events = []
    for ev in gen:
        events.append(ev)
        if "node" in ev and ev["node"].get("detail") == "ここまでに集めた資料で回答をまとめます":
            # ちょうどこの yield から戻ってくる直前（＝次の `next()` を呼ぶ前）に、呼び出し元側で
            # 停止要求が来た窓を模す。
            stop_event.set()
    assert not any("final" in ev for ev in events)


def test_openai_style_finish_reason_content_filter_skips_attribution(monkeypatch):
    """`finish_reason=="content_filter"`（自然完了 allowlist に無い）で終わった応答は、たとえ
    本文があっても帰属呼び出しを発行しない（main 3方言も plan/hybrid と同じ自然完了 allowlist へ
    揃える）。"""
    calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, {_REAL_DOC},
               [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "x", "ext": ".md"}], [])

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        return {"choices": [{"message": {"content": "止められた回答"}, "finish_reason": "content_filter"}]}

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, fake_run_tool
    try:
        final = None
        for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                                 max_turns=1):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 帰属は発行されない（3回目は無い）
        assert final["final"] == "止められた回答"
        assert final["attributed_ev_ids"] == set()
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


def test_openai_style_finish_reason_missing_skips_attribution(monkeypatch):
    """`finish_reason` が欠落（キー自体が無い）した応答は、以前は「明示的に `length` のときだけ
    未完了」という denylist 判定のもとで帰属が発行されていたが、自然完了 allowlist では
    理由欠落もすべて未完了扱いになる——旧 denylist 期待（理由欠落でも帰属成功）を反転する固定。"""
    calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, {_REAL_DOC},
               [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "x", "ext": ".md"}], [])

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        return {"choices": [{"message": {"content": "理由欠落の回答"}}]}   # finish_reason キー自体が無い

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, fake_run_tool
    try:
        final = None
        for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                                 max_turns=1):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 帰属は発行されない（3回目は無い・旧 denylist なら発行されていた）
        assert final["final"] == "理由欠落の回答"
        assert final["attributed_ev_ids"] == set()
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


def test_openai_style_non_string_finish_reason_skips_attribution_without_raising(monkeypatch):
    """`finish_reason` が文字列でない（壊れた upstream 応答が数値/dict 等を返した）場合、本文の
    配信・`_result` の生成は落ちずに完走し（`TypeError` にならない）、帰属呼び出しも発行しない
    （`_openai_style_finish_reason` の非文字列→None 変換＋`_is_natural_completion` の isinstance
    ガード、両方の防御を経路として通す）。"""
    calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, {_REAL_DOC},
               [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "x", "ext": ".md"}], [])

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
        # finish_reason が壊れて非文字列（dict）で返ってくる想定（JSON としては妥当な形）。
        return {"choices": [{"message": {"content": "壊れた完了理由の回答"},
                             "finish_reason": {"unexpected": "shape"}}]}

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, fake_run_tool
    try:
        final = None
        for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                                 max_turns=1):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 例外にならず完走・帰属は発行されない
        assert final["final"] == "壊れた完了理由の回答"
        assert final["attributed_ev_ids"] == set()
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


def test_gemini_finish_reason_safety_skips_attribution(monkeypatch):
    """`finishReason=="SAFETY"`（自然完了 allowlist に無い）で終わった応答は、たとえ本文があっても
    帰属呼び出しを発行しない。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "止められた回答"}]},
                        "finishReason": "SAFETY"}]},
    ]
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        return seq.pop(0)

    orig = A._post
    A._post = fake_post
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 帰属（3回目）は発行されない
        assert final["final"] == "止められた回答"
        assert final["attributed_ev_ids"] == set()
        assert final["stop_reason"] == "content_filtered"   # "no_tool_calls"（自然終了）と偽らない
    finally:
        A._post = orig


def test_gemini_non_string_finish_reason_skips_attribution_without_raising(monkeypatch):
    """`finishReason` が文字列でない（壊れた upstream 応答が dict 等を返した）場合、`cand0.get(
    "finishReason")` はそのまま非文字列値を返す（openai_style と異なりラッパー関数を経由しない）が、
    `_is_natural_completion` の isinstance ガードで例外にならず、本文配信・`_result` 生成は完走し
    帰属呼び出しも発行しない。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "壊れた完了理由の回答"}]},
                        "finishReason": {"unexpected": "shape"}}]},
    ]
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        return seq.pop(0)

    orig = A._post
    A._post = fake_post
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 例外にならず完走・帰属（3回目）は発行されない
        assert final["final"] == "壊れた完了理由の回答"
        assert final["attributed_ev_ids"] == set()
        assert final["stop_reason"] == "unknown"   # 非文字列は自然終了(no_tool_calls)へ丸めない
    finally:
        A._post = orig


def test_gemini_stops_between_turns_when_stop_event_set_mid_flight():
    import threading

    stop_event = threading.Event()
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        stop_event.set()
        return {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "TAX-RATE"}}}]}}]}

    orig = A._post
    A._post = fake_post
    try:
        events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "消費税率は?", "v1", None,
                               stop_event=stop_event))
        assert len(calls) == 1
        assert not any("final" in ev for ev in events)
    finally:
        A._post = orig


def test_openai_style_ask_user_stub():
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ask_user", "arguments": '{"prompt":"どの範囲で調べますか？","mode":"single","options":[{"id":"all","label":"全体"},{"id":"design","label":"設計書だけ"}]}'}}]}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None))
        q = next(ev["question"] for ev in events if "question" in ev)
        assert q["type"] == "question" and q["mode"] == "single"
        assert [o["label"] for o in q["options"]] == ["全体", "設計書だけ"]
        assert any(ev.get("node", {}).get("label") == "ユーザに確認" for ev in events)
        assert not any("final" in ev for ev in events)
    finally:
        A._post = orig


# ===== secRV MED-3（2026-07-18・DoS/コスト増幅）: 1応答あたりのツール実行数上限 =====

def test_openai_style_caps_tool_calls_per_turn():
    """1応答に25個の tool_calls が積まれていても、`MAX_TOOLS_PER_TURN`（既定16）を超えた分は
    `run_tool` を呼ばずに打ち切る（次のターンへは進まない・fail-closed）。

    レビュー是正（LOW-D・secRV・2026-07-18 再検証）: 超過件数（25-16=9件）と同数の「上限」ノードを
    生成すると、超過が極端な場合（例: 1応答10万件）に SSE/trace が肥大化する。是正後は超過があっても
    固定ノードを**1件だけ**生成する。
    """
    calls = [{"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}
             for i in range(25)]
    seq = [{"choices": [{"message": {"content": "", "tool_calls": calls}}]}]
    post_calls = []

    def fake_post(url, headers, body, timeout=90):
        post_calls.append(1)
        return seq.pop(0)

    orig = A._post
    A._post = fake_post
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
        assert len(post_calls) == 1   # 上限超過＝この応答で打ち切り・次のターンへは進まない
        cap_nodes = [ev["node"] for ev in events
                    if "node" in ev and ev["node"]["label"] == "ツール呼び出し上限"]
        assert len(cap_nodes) == 1   # 超過件数に関わらず固定ノードは1件だけ（LOW-D）
        executed_nodes = [ev["node"] for ev in events
                          if "node" in ev and ev["node"]["label"] == "資料を検索（grep）"]
        assert len(executed_nodes) == A.MAX_TOOLS_PER_TURN   # 実行されたのは上限まで
        final = next(ev for ev in events if "final" in ev)
        assert final["final"] == ""   # 打ち切り＝最終回答は空
        assert final["stop_reason"] == "tools_per_turn_exceeded"   # STOP-1: 閉じた語彙を固定
    finally:
        A._post = orig


def test_openai_style_extreme_excess_still_emits_single_cap_node():
    """LOW-D の実害シナリオ: 1応答に10万件の tool_calls があっても、上限ノードは1件だけ
    （超過件数分（99,984件）を生成しない）。"""
    calls = [{"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}
             for i in range(100_000)]
    seq = [{"choices": [{"message": {"content": "", "tool_calls": calls}}]}]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
        cap_nodes = [ev for ev in events if "node" in ev and ev["node"]["label"] == "ツール呼び出し上限"]
        assert len(cap_nodes) == 1
    finally:
        A._post = orig


def test_openai_style_stop_event_checked_before_each_tool_within_turn():
    """secRV MED-3 (b): stop_event は各ツール実行の直前にも確認する（1応答内に複数 tool_calls が
    あっても、途中で停止要求が来たら即座に打ち切り、以降のツールは実行しない）。"""
    import threading

    stop_event = threading.Event()
    calls = [{"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}
             for i in range(5)]
    seq = [{"choices": [{"message": {"content": "", "tool_calls": calls}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    run_tool_calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        run_tool_calls.append(1)
        if len(run_tool_calls) == 2:
            stop_event.set()   # 2回目のツール実行直後に停止要求が来た、を模す
        return orig_run_tool(name, args, world, scope_paths)

    A.run_tool = fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     stop_event=stop_event))
        assert len(run_tool_calls) == 2   # 3回目の直前チェックで停止＝以降は実行しない
        assert not any("final" in ev for ev in events)   # 停止時は final を出さない
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


# ===== secRV LOW-E（2026-07-18 再検証）: ノード yield 直後の stop_event 再確認 =====
# generator は yield で呼び出し元へ制御を返す＝その間に停止要求が来ても、是正前は再開後に
# run_tool（実 I/O）を無条件に1件実行してしまっていた。ノード yield 直後・ask_user 分岐/run_tool の
# 直前にも再確認することで、この窓を塞ぐ（3 dialect 共通）。

def test_openai_style_stop_event_set_during_node_yield_prevents_run_tool():
    import threading

    stop_event = threading.Event()
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    run_tool_calls = []

    def fake_run_tool(*a, **kw):
        run_tool_calls.append(1)
        raise AssertionError("run_tool が呼ばれた＝ノード yield 後の stop_event 再確認が効いていない")

    A.run_tool = fake_run_tool
    try:
        gen = A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                             stop_event=stop_event)
        first = next(gen)                 # tool_node が yield される（run_tool はまだ呼ばれていない）
        assert "node" in first
        stop_event.set()                  # generator 一時停止中に停止要求が来た、を模す
        events = list(gen)                # 再開: run_tool を呼ばず即終了するはず
        assert events == []
        assert run_tool_calls == []
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


def test_gemini_stop_event_set_during_node_yield_prevents_run_tool():
    import threading

    stop_event = threading.Event()
    seq = [{"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "ripgrep_search", "args": {"query": "TAX-RATE"}}}]}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    run_tool_calls = []

    def fake_run_tool(*a, **kw):
        run_tool_calls.append(1)
        raise AssertionError("run_tool が呼ばれた")

    A.run_tool = fake_run_tool
    try:
        gen = A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "消費税率は?", "v1", None, stop_event=stop_event)
        first = next(gen)
        assert "node" in first
        stop_event.set()
        events = list(gen)
        assert events == []
        assert run_tool_calls == []
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


def test_anthropic_style_stop_event_set_during_node_yield_prevents_run_tool():
    import threading

    stop_event = threading.Event()
    seq = [_AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "TAX-RATE"}, id="tu1")],
                  stop_reason="tool_use")]
    client = _AClient(seq)
    orig_run_tool = A.run_tool
    run_tool_calls = []

    def fake_run_tool(*a, **kw):
        run_tool_calls.append(1)
        raise AssertionError("run_tool が呼ばれた")

    A.run_tool = fake_run_tool
    try:
        gen = A.anthropic_style(client, "m", A.SYSTEM, "消費税率は?", "v1", None, stop_event=stop_event)
        first = next(gen)
        assert "node" in first
        stop_event.set()
        events = list(gen)
        assert events == []
        assert run_tool_calls == []
    finally:
        A.run_tool = orig_run_tool


# ===== secRV MED-2（2026-07-18・ローカルサブの生成物が公式 UI/trace に露出）: サブ経路ノードの固定文言 =====

def test_openai_style_main_path_tool_node_includes_args_regression():
    """メイン経路（allowed_tools=None・既定）は引数を含む豊かなノード表示のまま（byte-identical）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"SENTINEL_MAIN_QUERY"}'}}]}}]},
        {"choices": [{"message": {"content": "final"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None))
        nodes = [ev["node"] for ev in events if "node" in ev]
        assert any("SENTINEL_MAIN_QUERY" in n["detail"] for n in nodes)
    finally:
        A._post = orig


def test_openai_style_sub_path_tool_node_omits_model_generated_args():
    """secRV MED-2: サブ経路（`allowed_tools` が非 None＝`_sub_agentic_loop` の合図）の許可済みツール
    ノードは、モデル生成の引数（query 等）を一切含まない固定文言になる（`_tool_node_sub` 参照）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"SENTINEL_SUB_QUERY"}'}}]}}]},
        {"choices": [{"message": {"content": "final"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                     allowed_tools=frozenset({"ripgrep_search"})))
        nodes = [ev["node"] for ev in events if "node" in ev]
        assert not any("SENTINEL_SUB_QUERY" in str(n) for n in nodes)
        assert any(n["label"] == "資料を検索（grep）" for n in nodes)   # ラベル自体は維持（固定文言の範囲内）
    finally:
        A._post = orig


def test_openai_style_wires_hit_summary_node_after_ripgrep_search():
    """L-3: run_tool 実行後に `_hit_summary_node` の結果ノードが実際に流れることを配線ごと固定する
    （`_hit_summary_node` 単体テストだけでは、呼び出し箇所の配線を消しても緑のまま通ってしまう）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "final"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None))
        nodes = [ev["node"] for ev in events if "node" in ev]
        hit = next((n for n in nodes if n["label"] == A._HIT_SUMMARY_LABELS["ripgrep_search"]), None)
        assert hit is not None, nodes
        assert "TAX-RATE" in hit["detail"] and "件" in hit["detail"]
    finally:
        A._post = orig


def test_gemini_loop_stub():
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "TAX-RATE"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "TAX-RATE です。"}]}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "消費税率は?", "v1", None):
            if "final" in ev:
                final = ev
        assert final and "TAX-RATE" in final["final"] and final["docs"]
    finally:
        A._post = orig


def test_gemini_wires_hit_summary_node_after_ripgrep_search():
    """L-3: openai_style と同じく、gemini でも run_tool 実行後に `_hit_summary_node` の結果ノードが
    実際に流れることを配線ごと固定する。"""
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "TAX-RATE"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "TAX-RATE です。"}]}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "消費税率は?", "v1", None))
        nodes = [ev["node"] for ev in events if "node" in ev]
        hit = next((n for n in nodes if n["label"] == A._HIT_SUMMARY_LABELS["ripgrep_search"]), None)
        assert hit is not None, nodes
        assert "TAX-RATE" in hit["detail"] and "件" in hit["detail"]
    finally:
        A._post = orig


def test_gemini_drops_nonexistent_doc_citation_via_commit_gate(monkeypatch):
    """Gemini 経路も `openai_style` と同じ Committed Evidence 化ゲートを通る（機械検証で実在しない
    doc の citation を落とす・全滅時は stop_reason が evidence_verification_failed になる）。"""
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {"ghost.md"}, [{"doc_id": "ghost.md", "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "回答です。"}]}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None):
            if "final" in ev:
                final = ev
        assert final["cites"] == []
        assert final["stop_reason"] == "evidence_verification_failed"
    finally:
        A._post = orig


def test_gemini_attribution_call_marks_used_evidence_docs(monkeypatch):
    """EV-0（拡張設計 §4.4・設計簡素化）: Gemini 経路は本文に根拠申告用の制御構文を一切書かせず、
    確定した回答本文の完了後に別の非ストリーム呼び出し（`submit_attribution` の function-calling
    強制・`tool_config.mode=ANY`）で帰属を判定する。本文は byte-identical のまま。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "回答です。"}]}, "finishReason": "STOP"}]},
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "submit_attribution", "args": {"used": ["ev-1"]}}}]}}]},
    ]
    calls = []
    orig = A._post

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        return seq.pop(0)

    A._post = fake_post
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None):
            if "final" in ev:
                final = ev
        assert final["final"] == "回答です。"   # 本文は一切変更しない（byte-identical）
        assert final["used_evidence_docs"] == {real_doc}
        assert final["attributed_ev_ids"] == {"ev-1"}
        attribution_body = calls[-1]
        assert attribution_body["tool_config"]["function_calling_config"]["mode"] == "ANY"
        text = attribution_body["contents"][0]["parts"][0]["text"]
        assert "回答です。" in text and "ev-1" in text
    finally:
        A._post = orig


def test_gemini_stop_event_set_after_final_response_skips_attribution(monkeypatch):
    """最終応答（functionCall 無し）が返ってきた直後に停止要求が来た場合、帰属呼び出し
    （`submit_attribution`）は発行しない——帰属**直前**の再確認で捕捉する。"""
    import threading
    stop_event = threading.Event()
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "回答です。"}]}}]},
    ]
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        resp = seq.pop(0)
        if len(calls) == 2:
            stop_event.set()   # 最終応答が返った直後に停止要求が来た、を模す
        return resp

    orig = A._post
    A._post = fake_post
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None,
                           stop_event=stop_event):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 帰属（3回目）は発行されない
        assert final["final"] == "回答です。"
        assert final["attributed_ev_ids"] == set()
    finally:
        A._post = orig


def test_gemini_finish_reason_max_tokens_skips_attribution(monkeypatch):
    """`finishReason=="MAX_TOKENS"`（打ち切り＝未完了）で終わった応答は、たとえ本文が
    あっても帰属呼び出しを発行しない。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "途中で切れた回答"}]},
                        "finishReason": "MAX_TOKENS"}]},
    ]
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        return seq.pop(0)

    orig = A._post
    A._post = fake_post
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None):
            if "final" in ev:
                final = ev
        assert len(calls) == 2   # 帰属（3回目）は発行されない
        assert final["final"] == "途中で切れた回答"
        assert final["attributed_ev_ids"] == set()
    finally:
        A._post = orig


def test_gemini_mixed_valid_and_invalid_citations_resynthesizes_clean_body(monkeypatch):
    """検証で一部 citation が落ちた（実在 doc 1件＋存在しない doc 1件の混在）場合、Gemini 経路も
    `openai_style` と同じ「Committed Evidence だけからのクリーン再合成」を行う。落ちた doc に触れた
    最初の draft 本文は使わず、再合成呼び出しは tools 無し・ツール結果履歴も含まない最小コンテキスト
    （`history` 省略時は user パート1件だけ）で行う。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc, "ghost.md"},
        [{"doc_id": real_doc, "span": [1, 1], "quote": "実在", "ext": ".md"},
         {"doc_id": "ghost.md", "span": [1, 1], "quote": "存在しない", "ext": ".md"}], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "ghost.md にも記載があります（古い草稿）。"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "確認できた根拠に基づく回答です。"}]},
                        "finishReason": "STOP"}]},
    ]
    calls = []
    orig = A._post

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        return seq.pop(0)

    A._post = fake_post
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None):
            if "final" in ev:
                final = ev
        # tool turn + no-tool draft + クリーン再合成 + 帰属呼び出し1回（citation が1件残るため
        # digest が非空になり発火する・fake の seq 切れは attribute_gemini 側で安全に空集合へ縮退）。
        assert len(calls) == 4
        assert [c["doc_id"] for c in final["cites"]] == [real_doc]
        assert final["final"] == "確認できた根拠に基づく回答です。"
        assert "ghost.md" not in final["final"] and "古い草稿" not in final["final"]
        resynth_body = calls[-2]
        assert "tools" not in resynth_body       # これ以上ツールを呼ばせない
        assert len(resynth_body["contents"]) == 1   # history 省略＝再合成用 user パート1件だけ
        text = resynth_body["contents"][0]["parts"][0]["text"]
        assert "ghost.md" not in text and real_doc in text
    finally:
        A._post = orig


def test_gemini_empty_list_docs_is_still_one_aggregate_evidence(monkeypatch):
    """`list_docs` が0件でも、呼び出し単位の集計 Evidence を1件持つ（`has_structural_evidence` を
    立てる・拡張設計 §4.4・Gemini 経路）。"""
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"count": 0, "docs": []}, set(), [], []))
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "list_docs", "args": {}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "0件でした。"}]}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None)
                    if "final" in ev)
        assert final["has_structural_evidence"] is True
        assert len(final["structural_evidence_meta"]) == 1
        m = final["structural_evidence_meta"][0]
        assert m["doc_id"] is None and m["matched_doc_ids"] == [] and m["list_meta"]["count"] == 0
    finally:
        A._post = orig


def test_anthropic_style_empty_list_docs_is_still_one_aggregate_evidence():
    """`list_docs` が0件でも、呼び出し単位の集計 Evidence を1件持つ（`has_structural_evidence` を
    立てる・拡張設計 §4.4）——「該当0件」という具体的な事実として根拠ゲート・帰属の対象になる
    契約（Anthropic 経路）。"""
    orig_run_tool = A.run_tool
    A.run_tool = lambda name, args, world, scope_paths, **kw: ({"count": 0, "docs": []}, set(), [], [])
    seq = [
        _AResp([_ABlock("tool_use", name="list_docs", input={}, id="tu1")], stop_reason="tool_use"),
        _AResp([_ABlock("text", "0件でした。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    try:
        final = next(ev for ev in A.anthropic_style(client, "m", A.SYSTEM, "質問", "v1", None)
                    if "final" in ev)
        assert final["has_structural_evidence"] is True
        assert len(final["structural_evidence_meta"]) == 1
        m = final["structural_evidence_meta"][0]
        assert m["doc_id"] is None and m["matched_doc_ids"] == [] and m["list_meta"]["count"] == 0
    finally:
        A.run_tool = orig_run_tool


def test_openai_style_empty_list_docs_is_still_one_aggregate_evidence(monkeypatch):
    """`list_docs` が0件でも、呼び出し単位の集計 Evidence を1件持つ（`has_structural_evidence` を
    立てる・拡張設計 §4.4・OpenAI/Ollama 経路）。"""
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"count": 0, "docs": []}, set(), [], []))
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "0件でした。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
        final = next(ev for ev in events if "final" in ev)
        assert final["has_structural_evidence"] is True
        assert len(final["structural_evidence_meta"]) == 1
        m = final["structural_evidence_meta"][0]
        assert m["doc_id"] is None and m["matched_doc_ids"] == [] and m["list_meta"]["count"] == 0
    finally:
        A._post = orig


def test_gemini_graph_neighbors_requires_verified_backing_doc_for_structural_evidence(monkeypatch):
    """Gemini 経路でも card の存在だけでは `has_structural_evidence` を立てない——裏付け doc
    （`evidence.grep[].doc_id`）が world 内に実在するときだけ構造的根拠として数える。`run_tool`
    がカード単位で検証済みの doc_id 集合を返す契約（`agentic_search.run_tool` の docs 戻り値）
    なので、fake もその契約に合わせて検証済みの doc_id を返す。"""
    real_doc = "4期/04_運用/障害記録.md"

    def fake_run_tool_verified(name, args, world, scope_paths, **kw):
        # 実 run_tool は裏付け doc を検証済みで card 自身に `_verified_doc_ids` として同梱してから
        # 返す契約（`_card_structural_evidence` はこれを見る・自前で再検証しない）。
        return ({"nodes": []}, {real_doc}, [],
               [{"name": "n1", "label": "L1", "evidence": {"grep": [{"doc_id": real_doc}], "edges": []},
                 "_verified_doc_ids": [real_doc]}])

    def fake_run_tool_unverified(name, args, world, scope_paths, **kw):
        # 実 run_tool は裏付け doc が1件も実在しない card を cards・docs の両方から除外して返す
        # （呼び出し元は再検証しない契約）。
        return ({"nodes": []}, set(), [], [])

    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "graph_neighbors", "args": {"name": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "グラフから確認しました。"}]}}]},
    ]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    A.run_tool = fake_run_tool_verified
    try:
        final = next(ev for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None)
                    if "final" in ev)
        assert final["has_structural_evidence"] is True
        assert [m["matched_doc_ids"] for m in final["structural_evidence_meta"]] == [[real_doc]]
        assert final["structural_evidence_meta"][0]["doc_id"] is None
        assert final["structural_evidence_meta"][0]["verification_method"] == "graph_verified"
    finally:
        A._post = orig_post
        A.run_tool = orig_run_tool

    seq2 = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "graph_neighbors", "args": {"name": "x"}}}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "グラフから確認しました。"}]}}]},
    ]
    A._post = lambda url, headers, body, timeout=90: seq2.pop(0)
    A.run_tool = fake_run_tool_unverified
    try:
        final = next(ev for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None)
                    if "final" in ev)
        assert final["has_structural_evidence"] is False   # 裏付け doc が実在しない card は数えない
        assert final["structural_evidence_meta"] == []
    finally:
        A._post = orig_post
        A.run_tool = orig_run_tool


def test_gemini_ask_user_stub():
    seq = [
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "ask_user", "args": {"prompt": "対象は？", "mode": "multiple",
                                                             "options": [{"label": "設計"}, {"label": "ソース"}]}}}]}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "調べて", "v1", None))
        q = next(ev["question"] for ev in events if "question" in ev)
        assert q["mode"] == "multiple" and [o["label"] for o in q["options"]] == ["設計", "ソース"]
    finally:
        A._post = orig


# ===== 調べる深さ（調べ方ブロック §3.2・SC-6c）: OpenAIProvider._agentic_loop の倍率配線 =====

@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_openai_provider_agentic_loop_scales_with_depth_profile(monkeypatch, profile):
    """`_agentic_loop` は `ctx.scope_meta["depth_profile"]` の倍率を `openai_style` の
    `max_turns`/`max_hits`/`window_cap` へ渡す（既定 `standard` は倍率×1＝env 既定値のまま）。"""
    from sherpa.agents import Ctx, OpenAIProvider
    from sherpa import depth_profile as D
    captured = {}

    def fake_openai_style(*a, **kw):
        captured.update(kw)
        return iter([])

    monkeypatch.setattr(A, "openai_style", fake_openai_style)
    p = OpenAIProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="質問", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all", "depth_profile": profile},
              make_sources=lambda docs: [])
    list(p._agentic_loop(ctx))
    assert captured.get("max_turns") == D.scaled_turns(A.MAX_TURNS, profile)
    assert captured.get("max_hits") == D.scaled_ratio(A.MAX_HITS, profile)
    assert captured.get("window_cap") == D.scaled_ratio(A.READ_WINDOW, profile)


def test_openai_provider_agentic_loop_honors_system_settings_base_override(monkeypatch):
    """管理画面の基準値編集（`self._system_settings`）が env 既定より優先される（実効基準値）。"""
    from sherpa.agents import Ctx, OpenAIProvider
    captured = {}

    def fake_openai_style(*a, **kw):
        captured.update(kw)
        return iter([])

    monkeypatch.setattr(A, "openai_style", fake_openai_style)
    p = OpenAIProvider("sk-dummy", "gpt-5.5", system_settings={"depth_base_max_turns": 5})
    ctx = Ctx(message="質問", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all", "depth_profile": "deep"},
              make_sources=lambda docs: [])
    list(p._agentic_loop(ctx))
    assert captured.get("max_turns") == 10   # 5（基準値上書き）×2（深く）


def test_openai_provider_agentic_loop_abs_max_clamps_admin_base_times_multiplier(monkeypatch):
    """管理画面の基準値編集が Field 上限いっぱい（例: grep ヒット上限1000・読み取り窓400）でも、
    調べる深さ「最大」との組み合わせで既存の絶対上限を超えない。"""
    from sherpa.agents import Ctx, OpenAIProvider
    captured = {}

    def fake_openai_style(*a, **kw):
        captured.update(kw)
        return iter([])

    monkeypatch.setattr(A, "openai_style", fake_openai_style)
    p = OpenAIProvider("sk-dummy", "gpt-5.5", system_settings={
        "depth_base_grep_max_hits": 1000, "depth_base_read_window": 400})
    ctx = Ctx(message="質問", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all", "depth_profile": "max"},
              make_sources=lambda docs: [])
    list(p._agentic_loop(ctx))
    assert captured.get("max_hits") == A.MAX_HITS_ABS_MAX      # 2000 ではなく 1000
    assert captured.get("window_cap") == A.READ_WINDOW_ABS_MAX  # 800 ではなく 400


def test_ollama_provider_agentic_loop_scales_with_depth_profile(monkeypatch):
    """`OllamaProvider._agentic_loop` も OpenAIProvider と同じ倍率計算を openai_style へ渡す。"""
    from sherpa.agents import Ctx, OllamaProvider
    from sherpa import depth_profile as D
    captured = {}

    def fake_openai_style(*a, **kw):
        captured.update(kw)
        return iter([])

    monkeypatch.setattr(A, "openai_style", fake_openai_style)
    p = OllamaProvider("http://localhost:11434", "qwen2.5")
    ctx = Ctx(message="質問", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all", "depth_profile": "max"},
              make_sources=lambda docs: [])
    list(p._agentic_loop(ctx))
    assert captured.get("max_turns") == D.scaled_turns(A.MAX_TURNS, "max")
    assert captured.get("max_hits") == D.scaled_ratio(A.MAX_HITS, "max")
    assert captured.get("window_cap") == D.scaled_ratio(A.READ_WINDOW, "max")


def test_provider_agentic_run_builds_env():
    from sherpa.agents import Ctx, OpenAIProvider
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        p = OpenAIProvider("sk-dummy", "gpt-5.5")
        ctx = Ctx(message="消費税率は?", world="v1", knowledge=True,
                  route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
                  dispatch=lambda lens, inp: {},
                  scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
                  make_sources=lambda docs: [{"doc_id": d} for d in docs])
        result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
        env = result["env"]
        assert env["lens"] == "qa" and "TAX-RATE" in env["headline"]
        assert env["sources"] and result["decision"]["lens"] == "qa"
        assert env["data"]["citations"] and "span" in env["data"]["citations"][0]   # qa UI 用に span/quote 付き
    finally:
        A._post = orig


def test_provider_agentic_run_keeps_distinct_null_span_citations():
    """SEARCH-CUT-3 RV MED-2 追加是正: `providers/base.py::_agentic_run` の citation 集約
    （`citations.citation_dedupe_key` を共通で使う3箇所の1つ）が、同一 doc・span=[None, None]
    （rag_chunks 由来で行番号を持たない）・異なる quote の2ヒットを集約後も両方残すことを、
    citations.py 単体だけでなく base.py の実配線で固定する（重複実装せず共通鍵を使っている確認）。

    doc_id は fixtures/corpus/v1 実在ファイル（EXT-2 機械検証が既定 ON のため、実在しない doc を
    指す citation は Committed Evidence から落ちる＝この dedup 検証とは無関係な理由で落ちてしまう。
    テストの関心は「span=null の2ヒットが別 citation として残るか」であり doc の実体とは無関係
    なので、実在ファイルへ差し替えるだけで足りる）。
    """
    from sherpa import documents, es_index
    from sherpa.agents import Ctx, OpenAIProvider
    real_doc = "4期/04_運用/障害記録.md"
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": real_doc, "line": None, "text": "単価100円", "ext": ".md"},
        {"doc_id": real_doc, "line": None, "text": "数量5個", "ext": ".md"},
    ], None)
    documents.world_rel_set = lambda world, **kw: {real_doc}
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "es_search", "arguments": '{"query":"単価"}'}}]}}]},
        {"choices": [{"message": {"content": "単価と数量です。"}}]},
    ]
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        p = OpenAIProvider("sk-dummy", "gpt-5.5")
        ctx = Ctx(message="単価と数量は?", world="v1", knowledge=True,
                  route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
                  dispatch=lambda lens, inp: {},
                  scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
                  make_sources=lambda docs: [{"doc_id": d} for d in docs])
        result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
        cites = result["env"]["data"]["citations"]
        quotes = {c["quote"] for c in cites if c["doc_id"] == real_doc}
        assert quotes == {"単価100円", "数量5個"}   # 2件とも残る（span 同一でも quote が違えば別 citation）
    finally:
        A._post = orig_post
        es_index.search, documents.world_rel_set = o_search, o_relset


def test_provider_run_stops_promptly_and_skips_fallback_when_stop_event_set_mid_agentic():
    """RV MEDIUM（2026-07-03再検証）: OpenAIProvider.run(ctx)（agentic 経路・qa/troubleshoot）は
    stop_event が立つと後続ターンを発行せず、単発 grep フォールバックも試みずに終了する
    （＝停止 POST から stopped イベントまでの最大待ち時間が「1ターン分」で頭打ちになる裏付け。
    fake の遅い provider として、応答が返るたびに stop_event を立てる _post を使う）。"""
    import threading

    from sherpa.agents import Ctx, OpenAIProvider

    stop_event = threading.Event()
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        stop_event.set()   # 1ターン目の応答が返った直後に停止ボタンが押された、を模す
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}

    orig = A._post
    A._post = fake_post
    try:
        p = OpenAIProvider("sk-dummy", "gpt-5.5")
        ctx = Ctx(message="消費税率は?", world="v1", knowledge=True,
                  route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
                  dispatch=lambda lens, inp: {},
                  scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
                  make_sources=lambda docs: [{"doc_id": d} for d in docs],
                  stop_event=stop_event)
        events = list(p.run(ctx))
        assert len(calls) == 1, "停止後も2ターン目（以降）のリクエストが発行されている"
        assert not any(e.get("type") == "_result" for e in events), \
            "停止後にフォールバック経由で _result を作ってしまっている（無駄な追加処理）"
    finally:
        A._post = orig


def test_provider_run_single_shot_stream_stops_between_chunks_when_stop_event_set():
    """単発ストリーミング（lens=impact 等・非 agentic）は chunk を受信するたびにそのまま即時配信し
    （保留しない）、配信直後に stop_event を確認する——停止検知後は `_stream` から**それ以上**次の
    chunk を引き出さない。停止検知の直前に既に生成・配信済みの chunk は取り消さない
    （headline と配信本文を一致させる）。"""
    import threading

    from sherpa.agents import Ctx, OpenAIProvider

    stop_event = threading.Event()
    produced = []

    class _FakeStreamProvider(OpenAIProvider):
        def _stream(self, prompt):
            for i in range(5):
                produced.append(i)
                if i == 1:
                    stop_event.set()   # 2個目のチャンクが生成された直後に停止要求
                yield f"chunk{i}"

    p = _FakeStreamProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="影響は?", world="v1", knowledge=True,
              route=lambda m: {"lens": "impact", "input": m, "reason": "test"},
              dispatch=lambda lens, inp: {"summary": {"total": 0}, "data": {}},
              make_sources=lambda docs: [],
              stop_event=stop_event)
    events = list(p.run(ctx))
    deltas = [e for e in events if e.get("type") == "answer_delta"]
    assert produced == [0, 1], f"停止後も _stream から次のチャンクを引き出し続けている: {produced}"
    assert deltas == [{"type": "answer_delta", "text": "chunk0"},
                      {"type": "answer_delta", "text": "chunk1"}], \
        f"停止検知までに生成済みのチャンクは両方 yield されるはず: {deltas}"
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["headline"] == "".join(d["text"] for d in deltas)   # headline と配信本文が一致


def test_provider_run_single_shot_headline_byte_identical_to_stream():
    """設計簡素化（拡張設計 §4.4）: 単発ストリーミング（lens=impact 等・非 agentic）は本文中に
    制御タグを一切書かせない・保留もしない——`env["headline"]` は配信した本文と byte-identical。"""
    from sherpa.agents import Ctx, OpenAIProvider

    class _FakeStreamProvider(OpenAIProvider):
        def _stream(self, prompt):
            yield "影響は3件です。"

    p = _FakeStreamProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="影響は?", world="v1", knowledge=True,
              route=lambda m: {"lens": "impact", "input": m, "reason": "test"},
              dispatch=lambda lens, inp: {"summary": {"total": 0}, "data": {}},
              make_sources=lambda docs: [])
    result = next(e for e in p.run(ctx) if e.get("type") == "_result")
    assert result["env"]["headline"] == "影響は3件です。"


def test_provider_run_single_shot_stop_event_headline_is_partial_stream_so_far():
    """停止契約（拡張設計 §4.4・設計簡素化）: provider 単体は「停止＝その時点までに配信した本文」
    がそのまま headline になる（保留・確定処理は無い単純な契約）。"""
    import threading

    from sherpa.agents import Ctx, OpenAIProvider

    stop_event = threading.Event()

    class _StopMidChunk(OpenAIProvider):
        def _stream(self, prompt):
            stop_event.set()
            yield "回答本文"

    p = _StopMidChunk("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="影響は?", world="v1", knowledge=True,
              route=lambda m: {"lens": "impact", "input": m, "reason": "test"},
              dispatch=lambda lens, inp: {"summary": {"total": 0}, "data": {}},
              make_sources=lambda docs: [], stop_event=stop_event)
    events = list(p.run(ctx))
    deltas = [e for e in events if e.get("type") == "answer_delta"]
    assert deltas == [{"type": "answer_delta", "text": "回答本文"}]
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["headline"] == "回答本文"
    assert result["env"]["headline"] == "".join(d["text"] for d in deltas)


def test_provider_run_single_shot_exception_mid_stream_discards_full_response():
    """単発ストリーミングは例外発生時、既に flush 済みの本文も含めて全て破棄する
    （`acc=""` のまま・plan/hybrid の「部分本文は採用」とは異なる本経路従来からの契約・
    本経路従来からの契約）。held-back な断片も当然 finish() されず破棄される。"""
    from sherpa.agents import Ctx, OpenAIProvider

    class _MidFail(OpenAIProvider):
        def _stream(self, prompt):
            yield "回答"
            raise RuntimeError("boom mid-stream")

    p = _MidFail("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="影響は?", world="v1", knowledge=True,
              route=lambda m: {"lens": "impact", "input": m, "reason": "test"},
              dispatch=lambda lens, inp: {"summary": {"total": 0}, "data": {}},
              make_sources=lambda docs: [])
    events = list(p.run(ctx))
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"].get("headline") != "回答"   # 部分応答は headline に残らない（決定的回答へ切替）


def test_plain_run_stream_stops_between_chunks_when_stop_event_set():
    """RV MEDIUM（2026-07-03再検証）: ナレッジ参照オフ（素の会話・_plain_run）でも chunk 受信間で
    stop_event を確認し、以降のチャンクを消費・yield しない。"""
    import threading

    from sherpa.agents import Ctx, OpenAIProvider

    stop_event = threading.Event()
    produced = []

    class _FakeStreamProvider(OpenAIProvider):
        def _plain_stream(self, message):
            for i in range(5):
                produced.append(i)
                if i == 1:
                    stop_event.set()
                yield f"chunk{i}"

    p = _FakeStreamProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="こんにちは", world="v1", knowledge=False,
              route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
              dispatch=lambda lens, inp: {}, stop_event=stop_event)
    events = list(p.run(ctx))
    deltas = [e for e in events if e.get("type") == "answer_delta"]
    assert produced == [0, 1], f"停止後も _plain_stream から次のチャンクを引き出し続けている: {produced}"
    assert deltas == [{"type": "answer_delta", "text": "chunk0"}]


def test_provider_run_skips_stream_entirely_if_already_stopped_before_start():
    """発行前チェック: knowledge=False の会話で開始前から stop_event が立っていれば
    _plain_stream/_stream を1回も呼ばない（無駄な LLM 呼び出しをそもそも発行しない）。"""
    import threading

    from sherpa.agents import Ctx, OpenAIProvider

    stop_event = threading.Event()
    stop_event.set()
    calls = []

    class _FakeStreamProvider(OpenAIProvider):
        def _plain_stream(self, message):
            calls.append(1)
            yield "should not be called"

    p = _FakeStreamProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="こんにちは", world="v1", knowledge=False,
              route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
              dispatch=lambda lens, inp: {}, stop_event=stop_event)
    list(p.run(ctx))
    assert calls == [], "既に停止済みなのに _plain_stream を呼び出してしまっている"


def test_provider_agentic_run_folds_personal_facts_into_prompt():
    """S1確認: personal=True + knowledge=True（agentic/qa）でも個人ファイル内ヒットが LLM プロンプトに
    折り込まれ、env["_personal_facts"] に残る（chat_service がここから personal_sources を統合する）。
    HIGH-1 fix（_agentic_run の ctx.personal_facts 注入）の agentic 経路での回帰確認。
    """
    from sherpa.agents import Ctx, OpenAIProvider
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    # `body["messages"]` は openai_style 側でループ中に同一リストが破壊的更新されるため、参照を貯めずに
    # 初回呼び出し時点の user メッセージだけをその場で複製して残す（rv: 参照保存は後続ターンで上書きされる）。
    captured = {}
    orig = A._post

    def _capture(url, headers, body, timeout=90):
        if "first_user_msg" not in captured:
            captured["first_user_msg"] = str(body["messages"][1]["content"])
        return seq.pop(0)
    A._post = _capture
    try:
        p = OpenAIProvider("sk-dummy", "gpt-5.5")
        ctx = Ctx(message="消費税率は?", world="v1", knowledge=True,
                  route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
                  dispatch=lambda lens, inp: {},
                  scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
                  make_sources=lambda docs: [{"doc_id": d} for d in docs],
                  personal_facts="[個人ファイル: my_notes.txt 行3] 独自のメモ内容XYZ")
        result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
        env = result["env"]
        # 初回リクエストの user メッセージに個人ヒットが折り込まれている（LLM が根拠に使える）。
        first_user_msg = captured["first_user_msg"]
        assert "独自のメモ内容XYZ" in first_user_msg and "個人ファイル内ヒット" in first_user_msg
        # chat_service.handle_message はここから personal_sources を統合する（agentic 経路でも欠落しない）。
        assert env.get("_personal_facts") == "[個人ファイル: my_notes.txt 行3] 独自のメモ内容XYZ"
    finally:
        A._post = orig


def test_provider_agentic_troubleshoot_cards():
    """rv-full2 #3 解消: agentic 経路の troubleshoot が graph_neighbors 由来の candidates を env.data に載せる。"""
    from sherpa import lens_service
    from sherpa.agents import Ctx, OpenAIProvider
    # cid（lens_service.neighbor_cards が付与する内部専用の Neo4j canonical_id）が無ければ
    # 既定 ON の機械検証で構造 Evidence に昇格せず根拠ゲートを通らない（agentic_search.py 参照）。
    fake = [{"name": "BILLINGJOB", "label": "Module", "category": "プログラム", "role": "実装",
             "distance": 2, "path": ["請求画面", "BILLINGJOB"], "evidence": {"edges": [], "grep": []},
             "cid": "module:v1:請求画面/billingjob.cob#BILLINGJOB"}]
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"請求"}'}}]}}]},
        {"choices": [{"message": {"content": "請求処理が関係している可能性があります。"}}]},
    ]
    o_post, o_nc = A._post, lens_service.neighbor_cards
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    try:
        p = OpenAIProvider("sk-dummy", "gpt-5.5")
        ctx = Ctx(message="請求でエラー。原因は?", world="v1", knowledge=True,
                  route=lambda m: {"lens": "troubleshoot", "input": m, "reason": "test"},
                  dispatch=lambda lens, inp: {},
                  scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
                  make_sources=lambda docs: [])
        result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
        env = result["env"]
        assert env["lens"] == "troubleshoot"
        cands = env["data"].get("candidates") or []
        assert cands and cands[0]["name"] == "BILLINGJOB" and cands[0]["role"] == "実装"
    finally:
        A._post, lens_service.neighbor_cards = o_post, o_nc


def test_codex_mcp_config_builder():
    """Phase2b: SHERPA_CODEX_MCP フラグ・codex への MCP 設定 -c 引数・MCP プロンプト（事実前渡し無し）を検証。"""
    from sherpa import agents
    os.environ.pop("SHERPA_CODEX_MCP", None)
    assert agents._codex_mcp_enabled() is True                # 既定 ON（2026-07-01・agentic 主軸）
    os.environ["SHERPA_CODEX_MCP"] = "0"
    try:
        assert agents._codex_mcp_enabled() is False           # =0 で従来の事実前渡しに戻せる
    finally:
        os.environ.pop("SHERPA_CODEX_MCP", None)
    args = agents._mcp_config_args("v1", ["4期", "00_共通"])
    joined = " ".join(args)
    assert args.count("-c") == 5
    assert "mcp_servers.sherpa.command=" in joined and 'sherpa.args = ["-m", "sherpa.mcp_server"]' in joined
    assert 'SHERPA_MCP_WORLD = "v1"' in joined and 'SHERPA_MCP_SCOPE = "4期\\n00_共通"' in joined  # scope は \n 区切り
    # MCP ツールを sandbox 維持のまま自動承認（codex 0.139・bypass 不要）
    assert 'default_tools_approval_mode = "approve"' in joined and 'approval_policy = "never"' in joined
    env = agents._mcp_env("v1", None)
    assert env["SHERPA_MCP_WORLD"] == "v1" and "SHERPA_MCP_SCOPE" not in env   # scope 無しは未設定
    assert "SHERPA_MCP_ASK_DISABLED" not in env                                # 既定は付けない
    p = agents.CodexProvider()._prompt_mcp("請求でエラー。原因は?", "troubleshoot", "v1")
    assert "graph_neighbors" in p and "参考（構造化済みの事実）" not in p       # MCP は自律＝事実を前渡ししない


def test_mcp_env_includes_effective_arms_and_legacy_backend_snapshot(monkeypatch):
    """W0 Med RV（2026-07-08）: MCP サブプロセスは PG creds を持たない（`_MCP_PASSTHROUGH` に非含）ため
    system_settings を読めず env フォールバックに落ちる。親（API リクエスト時点）の**実効値スナップショット**
    を SHERPA_ARMS/SHERPA_LEGACY_BACKEND として渡すことで、サブプロセス側は env フォールバックだけで
    親と同じ実効値に一致する（list_docs の convertible 判定が grep とずれる、といった不一致を防ぐ）。
    SHERPA_TESSERACT_BIN の透過は tesseract の `ocr` アーム撤去（2026-07-08）に伴い削除した。"""
    from sherpa import agents, store
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"arms_enabled": ["ooxml"], "legacy_backend": "libreoffice"})
    env = agents._mcp_env("v1", None)
    assert env["SHERPA_ARMS"] == "ooxml"                       # 実効アーム（system_settings 反映済）
    assert env["SHERPA_LEGACY_BACKEND"] == "libreoffice"       # 実効バックエンド（system_settings 反映済）
    assert "SHERPA_TESSERACT_BIN" not in env                   # もう透過しない（撤去済み env）


def test_mcp_env_includes_vlm_usable_snapshot(monkeypatch):
    """RV Med（Codex gpt-5.5/xhigh・2026-07-08 R1）: MCP サブプロセスは PG creds を持たず
    system_settings.vlm を読めないため、親（API リクエスト時点）の `markitdown_ocr_arm.resolve_vlm()`
    実効可用性（1bit・secrets は含まない）を SHERPA_VLM_USABLE として渡す。既定（system_settings 未設定＝
    ローカル ollama）は "1"。親が openai・cloud_allowed=false（unusable）なら "0" を渡す。"""
    from sherpa import agents, store
    env = agents._mcp_env("v1", None)
    assert env["SHERPA_VLM_USABLE"] == "1"                     # 既定＝ローカル ollama＝usable

    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"vlm": {"provider": "openai", "model": "gpt-4o", "cloud_allowed": False}})
    env2 = agents._mcp_env("v1", None)
    assert env2["SHERPA_VLM_USABLE"] == "0"                    # openai・許可無し＝unusable


def test_mcp_env_snapshots_legacy_exts_and_omits_office_com_secrets(monkeypatch):
    """W1 RV Med（2026-07-08・token 漏洩対策）: office_com の URL/TOKEN は Codex sandbox 無効時の
    fallback 実行環境（MCP サブプロセス）へ渡さない。代わりに親の実効 legacy_exts() スナップショットを
    SHERPA_LEGACY_EXTS として渡し、サブプロセス側は healthz へ probe せずこれを信じる。
    SHERPA_SOFFICE_BIN も legacy_exts のスナップショットで不要になったため渡さない。"""
    from sherpa import agents
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:8091")
    monkeypatch.setenv("SHERPA_OFFICE_COM_TOKEN", "super-secret-token")
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", "/usr/bin/soffice")
    monkeypatch.setattr(legacy_convert, "legacy_exts", lambda: {".doc", ".xls"})

    env = agents._mcp_env("v1", None)

    assert "SHERPA_OFFICE_COM_URL" not in env                  # secrets/接続先を渡さない
    assert "SHERPA_OFFICE_COM_TOKEN" not in env                # ＝共有シークレットが sandbox 無効時にも出ない
    assert "SHERPA_SOFFICE_BIN" not in env                     # legacy_exts スナップショットで不要
    assert env["SHERPA_LEGACY_EXTS"] == ".doc,.xls"            # 親の実効値（ソート済み）を渡す


def test_mcp_ask_disabled_flag_reaches_subprocess_env():
    """S2 RV HIGH（2026-07-07）: 確認ID 付き再送実行では ask_disabled=True を `_mcp_env`/
    `_mcp_config_args` に渡すと SHERPA_MCP_ASK_DISABLED=1 が MCP サブプロセスの env（フォールバック
    経路は -c mcp_servers.sherpa.env、sandbox 経路は _mcp_env 直渡し）に乗ること。実行ベースで固定
    （mcp_server 側が実際にこのフラグを見て tool を隠すことは test_mcp_server.py 側で検証）。"""
    from sherpa import agents
    env = agents._mcp_env("v1", None, ask_disabled=True)
    assert env["SHERPA_MCP_ASK_DISABLED"] == "1"
    args = agents._mcp_config_args("v1", None, ask_disabled=True)
    assert 'SHERPA_MCP_ASK_DISABLED = "1"' in " ".join(args)


def test_mcp_env_layer_env_var_only_when_restrictive(monkeypatch):
    """`layer` が docs/code のときだけ SHERPA_MCP_LAYER を渡す（both/未指定は付けない＝
    既存呼び出し元は無変更）。sandbox 経路（`_mcp_env` 直渡し）・fallback 経路（`_mcp_config_args`
    の -c 引数）の両方を固定する。"""
    from sherpa import agents
    env_both = agents._mcp_env("v1", None, layer="both")
    assert "SHERPA_MCP_LAYER" not in env_both
    env_none = agents._mcp_env("v1", None)
    assert "SHERPA_MCP_LAYER" not in env_none
    env_code = agents._mcp_env("v1", None, layer="code")
    assert env_code["SHERPA_MCP_LAYER"] == "code"
    env_docs = agents._mcp_env("v1", None, layer="docs")
    assert env_docs["SHERPA_MCP_LAYER"] == "docs"
    args = agents._mcp_config_args("v1", None, layer="docs")
    assert 'SHERPA_MCP_LAYER = "docs"' in " ".join(args)


def test_mcp_env_rejects_invalid_layer_before_codex_starts():
    """不正な内部 layer 値は Codex 起動前（config/env 組み立て時点）に
    ValueError で明示拒否する（HTTP 入口は pydantic Literal が別途 422 で防ぐため、ここに届く
    のは呼び出し側のバグ）。sandbox 経路（`_mcp_env`）・fallback 経路（`_mcp_config_args`）の両方。"""
    import pytest
    from sherpa import agents
    with pytest.raises(ValueError):
        agents._mcp_env("v1", None, layer="bogus")
    with pytest.raises(ValueError):
        agents._mcp_config_args("v1", None, layer="bogus")


def test_mcp_neighbors_from_stream_item():
    """A2: 完了 graph_neighbors の mcp_tool_call item から neighbors を抽出（壊れは []）。"""
    from sherpa import agents
    import json as _json
    good = {"result": {"content": [{"type": "text",
            "text": _json.dumps({"neighbors": [{"name": "BILLINGJOB", "role": "実装", "path": ["a", "b"]}]})}]}}
    ns = agents._mcp_neighbors_from(good)
    assert ns and ns[0]["name"] == "BILLINGJOB" and ns[0]["role"] == "実装"
    assert agents._mcp_neighbors_from({"result": {"content": [{"text": "{ broken"}]}}) == []   # 壊れ JSON
    assert agents._mcp_neighbors_from({"result": None}) == []                                   # 形が違う
    assert agents._mcp_neighbors_from({}) == []


def test_provider_agentic_run_yields_question():
    from sherpa.agents import Ctx, OpenAIProvider
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ask_user", "arguments": '{"prompt":"範囲を選んでください","mode":"single","options":[{"label":"全体"},{"label":"設計"}]}'}}]}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        p = OpenAIProvider("sk-dummy", "gpt-5.5")
        ctx = Ctx(message="調べて", world="v1", knowledge=True,
                  route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
                  dispatch=lambda lens, inp: {},
                  scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
                  make_sources=lambda docs: [])
        events = list(p.run(ctx))
        q = next(e for e in events if e.get("type") == "question")
        assert q["prompt"] == "範囲を選んでください"
        assert not any(e.get("type") == "_result" for e in events)
    finally:
        A._post = orig


# ---- anthropic_style（Bedrock/Claude の手動ツールループ・fake クライアント）----

class _ABlock:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class _AResp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason


class _AMessages:
    def __init__(self, seq):
        self._seq, self.calls = list(seq), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._seq.pop(0)


class _AClient:
    def __init__(self, seq):
        self.messages = _AMessages(seq)


_BANNED = ("temperature", "top_p", "top_k", "thinking")


def test_anthropic_tools_from_openai_conversion():
    conv = A.anthropic_tools_from_openai(A.graph_openai_tools())
    assert conv and conv[0]["name"] == "graph_neighbors"
    assert conv[0]["input_schema"]["type"] == "object"              # parameters → input_schema（同形）
    assert "parameters" not in conv[0] and "function" not in conv[0]


def test_anthropic_style_tool_loop_two_turns():
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "TAX-RATE"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "TAX-RATE で管理しています。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    nodes, final = [], None
    for ev in A.anthropic_style(client, "anthropic.claude-opus-4-8", A.SYSTEM, "消費税率は?", "v1", None):
        (nodes.append(ev["node"]) if "node" in ev else None)
        if "final" in ev:
            final = ev
    assert final and "TAX-RATE" in final["final"] and final["docs"] and final["searched"]
    assert any("資料を検索" in n["label"] for n in nodes)
    # tool_use → tool_result → end_turn の2周＋帰属呼び出し1回（citation が実在すれば digest が
    # 非空になり発火する・fake の seq 切れは attribute_anthropic 側で安全に空集合へ縮退する）。
    assert len(client.messages.calls) == 3
    for kw in client.messages.calls[:2]:                            # temperature/top_p/top_k/thinking は送らない
        assert all(b not in kw for b in _BANNED)
        assert kw["max_tokens"]                                     # max_tokens 必須
    msgs2 = client.messages.calls[1]["messages"]                    # 2回目: 末尾は tool_result を束ねた user
    assert msgs2[-1]["role"] == "user"
    assert all(b["type"] == "tool_result" for b in msgs2[-1]["content"])


def test_anthropic_style_drops_nonexistent_doc_citation_via_commit_gate(monkeypatch):
    """Anthropic 経路も `openai_style` と同じ Committed Evidence 化ゲートを通る（機械検証で実在しない
    doc の citation を落とす・全滅時は stop_reason が evidence_verification_failed になる）。"""
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {"ghost.md"}, [{"doc_id": "ghost.md", "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "回答です。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    final = None
    for ev in A.anthropic_style(client, "anthropic.claude-opus-4-8", A.SYSTEM, "質問", "v1", None):
        if "final" in ev:
            final = ev
    assert final["cites"] == []
    assert final["stop_reason"] == "evidence_verification_failed"


def test_anthropic_style_mixed_valid_and_invalid_citations_resynthesizes_clean_body(monkeypatch):
    """検証で一部 citation が落ちた（実在 doc 1件＋存在しない doc 1件の混在）場合、Anthropic 経路も
    `openai_style` と同じ「Committed Evidence だけからのクリーン再合成」を行う。落ちた doc に触れた
    最初の draft 本文は使わず、再合成呼び出しは tools 無し・ツール結果履歴も含まない最小コンテキスト
    （`history` 省略時は user メッセージ1件だけ）で行う。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc, "ghost.md"},
        [{"doc_id": real_doc, "span": [1, 1], "quote": "実在", "ext": ".md"},
         {"doc_id": "ghost.md", "span": [1, 1], "quote": "存在しない", "ext": ".md"}], []))
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "ghost.md にも記載があります（古い草稿）。")], stop_reason="end_turn"),
        _AResp([_ABlock("text", "確認できた根拠に基づく回答です。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    final = None
    for ev in A.anthropic_style(client, "anthropic.claude-opus-4-8", A.SYSTEM, "質問", "v1", None):
        if "final" in ev:
            final = ev
    # tool turn + no-tool draft + クリーン再合成 + 帰属呼び出し1回（citation が1件残るため digest
    # が非空になり発火する・fake の seq 切れは attribute_anthropic 側で安全に空集合へ縮退する）。
    assert len(client.messages.calls) == 4
    assert [c["doc_id"] for c in final["cites"]] == [real_doc]
    assert final["final"] == "確認できた根拠に基づく回答です。"
    assert "ghost.md" not in final["final"] and "古い草稿" not in final["final"]
    resynth_call = client.messages.calls[-2]
    assert "tools" not in resynth_call          # これ以上ツールを呼ばせない
    assert len(resynth_call["messages"]) == 1   # history 省略＝再合成用 user メッセージ1件だけ
    assert resynth_call["messages"][0]["role"] == "user"
    assert "ghost.md" not in resynth_call["messages"][0]["content"]
    assert real_doc in resynth_call["messages"][0]["content"]


def test_anthropic_style_graph_neighbors_requires_verified_backing_doc_for_structural_evidence(monkeypatch):
    """Anthropic 経路でも card の存在だけでは `has_structural_evidence` を立てない——裏付け doc
    （`evidence.grep[].doc_id`）が world 内に実在するときだけ構造的根拠として数える。`run_tool`
    がカード単位で検証済みの doc_id 集合を返す契約なので、fake もその契約に合わせる。"""
    real_doc = "4期/04_運用/障害記録.md"

    def fake_run_tool_verified(name, args, world, scope_paths, **kw):
        # 実 run_tool は裏付け doc を検証済みで card 自身に `_verified_doc_ids` として同梱してから
        # 返す契約（`_card_structural_evidence` はこれを見る・自前で再検証しない）。
        return ({"nodes": []}, {real_doc}, [],
               [{"name": "n1", "label": "L1", "evidence": {"grep": [{"doc_id": real_doc}], "edges": []},
                 "_verified_doc_ids": [real_doc]}])

    def fake_run_tool_unverified(name, args, world, scope_paths, **kw):
        # 実 run_tool は裏付け doc が1件も実在しない card を cards・docs の両方から除外して返す。
        return ({"nodes": []}, set(), [], [])

    seq = [
        _AResp([_ABlock("tool_use", name="graph_neighbors", input={"name": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "グラフから確認しました。")], stop_reason="end_turn"),
    ]
    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool_verified
    try:
        client = _AClient(seq)
        final = next(ev for ev in A.anthropic_style(client, "m", A.SYSTEM, "質問", "v1", None)
                    if "final" in ev)
        assert final["has_structural_evidence"] is True
        assert [m["matched_doc_ids"] for m in final["structural_evidence_meta"]] == [[real_doc]]
        assert final["structural_evidence_meta"][0]["doc_id"] is None
        assert final["structural_evidence_meta"][0]["verification_method"] == "graph_verified"
    finally:
        A.run_tool = orig_run_tool

    seq2 = [
        _AResp([_ABlock("tool_use", name="graph_neighbors", input={"name": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "グラフから確認しました。")], stop_reason="end_turn"),
    ]
    A.run_tool = fake_run_tool_unverified
    try:
        client = _AClient(seq2)
        final = next(ev for ev in A.anthropic_style(client, "m", A.SYSTEM, "質問", "v1", None)
                    if "final" in ev)
        assert final["has_structural_evidence"] is False   # 裏付け doc が実在しない card は数えない
        assert final["structural_evidence_meta"] == []
    finally:
        A.run_tool = orig_run_tool


def test_anthropic_style_attribution_call_marks_used_evidence_docs(monkeypatch):
    """EV-0（拡張設計 §4.4・設計簡素化）: Anthropic 経路は本文に根拠申告用の制御構文を一切書かせず、
    確定した回答本文の完了後に別の非ストリーム呼び出し（`submit_attribution` の tool 強制呼び出し）
    で帰属を判定する。ストリーム/本文は byte-identical のまま・`used_evidence_docs` は帰属呼び出し
    の結果を ev-N→doc_id 逆引きしたもの。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "回答です。")], stop_reason="end_turn"),
        _AResp([_ABlock("tool_use", name="submit_attribution", input={"used": ["ev-1"]}, id="tu2")],
               stop_reason="tool_use"),
    ]
    client = _AClient(seq)
    final = None
    for ev in A.anthropic_style(client, "anthropic.claude-opus-4-8", A.SYSTEM, "質問", "v1", None):
        if "final" in ev:
            final = ev
    assert final["final"] == "回答です。"   # 本文は一切変更しない（byte-identical）
    assert final["used_evidence_docs"] == {real_doc}
    assert final["attributed_ev_ids"] == {"ev-1"}
    attribution_call = client.messages.calls[-1]
    assert attribution_call["tools"][0]["name"] == "submit_attribution"
    assert attribution_call["tool_choice"] == {"type": "tool", "name": "submit_attribution"}
    assert "回答です。" in attribution_call["messages"][0]["content"]   # 確定した回答本文を渡す
    assert "ev-1" in attribution_call["messages"][0]["content"]        # Evidence digest も渡す


def test_anthropic_style_stop_event_set_after_final_response_skips_attribution(monkeypatch):
    """最終応答（end_turn）が返ってきた直後に停止要求が来た場合、帰属呼び出し
    （`submit_attribution`）は発行しない——帰属**直前**の再確認で捕捉する。"""
    import threading
    stop_event = threading.Event()
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "回答です。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    orig_create = client.messages.create

    def spying_create(**kwargs):
        resp = orig_create(**kwargs)
        if len(client.messages.calls) == 2:
            stop_event.set()   # 最終応答が返った直後に停止要求が来た、を模す
        return resp

    client.messages.create = spying_create
    final = None
    for ev in A.anthropic_style(client, "m", A.SYSTEM, "質問", "v1", None, stop_event=stop_event):
        if "final" in ev:
            final = ev
    assert len(client.messages.calls) == 2   # 帰属（3回目）は発行されない
    assert final["final"] == "回答です。"
    assert final["attributed_ev_ids"] == set()


def test_anthropic_style_stop_reason_max_tokens_skips_attribution(monkeypatch):
    """`stop_reason=="max_tokens"`（打ち切り＝未完了）で終わった応答は、たとえ本文が
    あっても帰属呼び出しを発行しない。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "途中で切れた回答")], stop_reason="max_tokens"),
    ]
    client = _AClient(seq)
    final = None
    for ev in A.anthropic_style(client, "m", A.SYSTEM, "質問", "v1", None):
        if "final" in ev:
            final = ev
    assert len(client.messages.calls) == 2   # 帰属（3回目）は発行されない
    assert final["final"] == "途中で切れた回答"
    assert final["attributed_ev_ids"] == set()


def test_anthropic_style_stop_reason_missing_skips_attribution(monkeypatch):
    """`stop_reason` が欠落（None）した応答は、自然完了 allowlist（"end_turn"/"stop_sequence"）に
    無いためすべて未完了扱い——旧 denylist 期待（理由欠落でも帰属成功）を反転する固定。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "理由欠落の回答")], stop_reason=None),
    ]
    client = _AClient(seq)
    final = None
    for ev in A.anthropic_style(client, "m", A.SYSTEM, "質問", "v1", None):
        if "final" in ev:
            final = ev
    assert len(client.messages.calls) == 2   # 帰属（3回目）は発行されない・旧 denylist なら発行されていた
    assert final["final"] == "理由欠落の回答"
    assert final["attributed_ev_ids"] == set()


def test_anthropic_style_non_string_stop_reason_skips_attribution_without_raising(monkeypatch):
    """`stop_reason` が文字列でない（壊れた SDK/upstream 応答が dict 等を返した）場合、
    `getattr(resp, "stop_reason", None)` はそのまま非文字列値を返す（ラッパー関数を経由しない）が、
    `_is_natural_completion` の isinstance ガードで例外にならず、本文配信・`_result` 生成は完走し
    帰属呼び出しも発行しない。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc}, [{"doc_id": real_doc, "span": [1, 1], "quote": "x", "ext": ".md"}], []))
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "壊れた完了理由の回答")], stop_reason={"unexpected": "shape"}),
    ]
    client = _AClient(seq)
    final = None
    for ev in A.anthropic_style(client, "m", A.SYSTEM, "質問", "v1", None):
        if "final" in ev:
            final = ev
    assert len(client.messages.calls) == 2   # 例外にならず完走・帰属（3回目）は発行されない
    assert final["final"] == "壊れた完了理由の回答"
    assert final["attributed_ev_ids"] == set()


def test_anthropic_style_stops_between_turns_when_stop_event_set_mid_flight():
    """RV MEDIUM（2026-07-03再検証）: 1ターン目の応答が返ってきた直後に停止要求が来たケース＝
    2ターン目のリクエストは発行しない（openai_style/gemini と同じ意味論）。"""
    import threading

    stop_event = threading.Event()
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "TAX-RATE"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "TAX-RATE で管理しています。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    orig_create = client.messages.create

    def _create(**kwargs):
        resp = orig_create(**kwargs)
        stop_event.set()   # 1ターン目のレスポンスが返った直後に停止ボタンが押された、を模す
        return resp

    client.messages.create = _create
    events = list(A.anthropic_style(client, "m", A.SYSTEM, "消費税率は?", "v1", None, stop_event=stop_event))
    assert len(client.messages.calls) == 1, "停止後も2ターン目のリクエストが発行されている"
    assert not any("final" in ev for ev in events), "停止時に final を yield すべきでない"


def test_anthropic_style_returns_immediately_if_already_stopped():
    """開始前から stop_event が立っていれば、1回も client.messages.create を呼ばない。"""
    import threading

    stop_event = threading.Event()
    stop_event.set()
    client = _AClient([])   # 空シーケンス＝呼ばれたら IndexError になるはず
    events = list(A.anthropic_style(client, "m", A.SYSTEM, "調べて", "v1", None, stop_event=stop_event))
    assert events == []
    assert client.messages.calls == []


def test_anthropic_style_cumulative_tool_result_bytes_cap_terminates_run(monkeypatch):
    """secRV MED-B (c): 1 run 累計の tool-result バイト量が上限を超えたら、固定エラーで
    run を打ち切る（3 dialect 共通の是正）。"""
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", 10)
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "TAX-RATE"}, id="tu1")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "not reached")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    events = list(A.anthropic_style(client, "m", A.SYSTEM, "消費税率は?", "v1", None))
    assert len(client.messages.calls) == 1   # 1回目のツール結果だけで上限超過＝2ターン目へは進まない
    assert any(ev.get("node", {}).get("label") == "ツール結果の合計サイズ上限" for ev in events)
    final = next(ev for ev in events if "final" in ev)
    assert final["final"] == ""


def test_anthropic_style_parallel_tool_results_single_user_message():
    seq = [
        _AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "TAX-RATE"}, id="tu1"),
                _ABlock("tool_use", name="ripgrep_search", input={"query": "税率"}, id="tu2")],
               stop_reason="tool_use"),
        _AResp([_ABlock("text", "まとめました。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)
    list(A.anthropic_style(client, "m", A.SYSTEM, "調べて", "v1", None))
    msgs2 = client.messages.calls[1]["messages"]
    tr = [m for m in msgs2 if m["role"] == "user" and isinstance(m["content"], list)
          and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])]
    assert len(tr) == 1 and len(tr[-1]["content"]) == 2             # 並列 tool_use は **1つの** user に束ねる
    assert {b["tool_use_id"] for b in tr[-1]["content"]} == {"tu1", "tu2"}


def test_anthropic_style_refusal_branch():
    client = _AClient([_AResp([], stop_reason="refusal")])
    evs = list(A.anthropic_style(client, "m", A.SYSTEM, "q", "v1", None))
    final = next(e for e in evs if "final" in e)
    assert final["searched"] is False and "控え" in final["final"]  # refusal は安全に終了（回答を控える）
    assert final["stop_reason"] == "refusal"   # STOP-1: 到達可能経路の閉じた語彙を固定


def test_anthropic_style_ask_user_stub():
    seq = [_AResp([_ABlock("tool_use", name="ask_user", id="a1", input={
        "prompt": "範囲は？", "mode": "single", "options": [{"label": "全体"}, {"label": "設計"}]})],
        stop_reason="tool_use")]
    client = _AClient(seq)
    evs = list(A.anthropic_style(client, "m", A.SYSTEM, "調べて", "v1", None))
    q = next(e["question"] for e in evs if "question" in e)
    assert q["mode"] == "single" and [o["label"] for o in q["options"]] == ["全体", "設計"]
    assert not any("final" in e for e in evs)


def test_anthropic_style_mixed_tool_use_and_ask_user_discards_prior_results():
    """Codex RV major: 同一応答内で tool_use（先）＋ ask_user（後）が並列で返るケース。

    意味論（コード内コメント参照・openai_style/gemini と同一・意図的）: ask_user は question 優先。
    先に実行済みの ripgrep_search は run_tool までは呼ばれる（副作用として実行される）が、
    その結果（docs/cites/cards）は呼び出し元に一切渡らない（`final` イベントを yield しない）
    ＝次ターンは新規メッセージとしてフロントから再送され、検索し直す設計なので実害はない。
    """
    seq = [_AResp([
        _ABlock("tool_use", name="ripgrep_search", input={"query": "TAX-RATE"}, id="tu1"),
        _ABlock("tool_use", name="ask_user", id="tu2", input={
            "prompt": "範囲は？", "mode": "single", "options": [{"label": "全体"}, {"label": "設計"}]}),
    ], stop_reason="tool_use")]
    client = _AClient(seq)
    calls = []
    orig_run_tool = A.run_tool
    A.run_tool = lambda name, args, world, scope_paths, **kw: (
        calls.append(name),
        orig_run_tool(name, args, world, scope_paths))[1]
    try:
        evs = list(A.anthropic_style(client, "m", A.SYSTEM, "調べて", "v1", None))
    finally:
        A.run_tool = orig_run_tool
    assert calls == ["ripgrep_search"]                              # 先行ツールは実行される（副作用）
    q = next(e["question"] for e in evs if "question" in e)
    assert q["mode"] == "single" and [o["label"] for o in q["options"]] == ["全体", "設計"]
    assert not any("final" in e for e in evs)                       # 実行済み結果は final に出ず破棄される
    node_labels = [e["node"]["label"] for e in evs if "node" in e]
    # run_tool 実行後に「検索結果（grep）」（ヒット件数の追加ノード）がもう1件挟まる
    # （`_hit_summary_node` 参照）。
    assert node_labels == ["資料を検索（grep）", "検索結果（grep）", "ユーザに確認"]   # 3ノードとも流れる（UI 表示用）


def test_openai_style_mixed_tool_calls_and_ask_user_discards_prior_results():
    """anthropic_style と同じ「question 優先・先行結果は破棄」意味論が openai_style にも一貫している検証。"""
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}},
        {"id": "c2", "function": {"name": "ask_user", "arguments":
         '{"prompt":"範囲は？","mode":"single","options":[{"label":"全体"},{"label":"設計"}]}'}},
    ]}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    calls = []
    A.run_tool = lambda name, args, world, scope_paths, **kw: (
        calls.append(name),
        orig_run_tool(name, args, world, scope_paths))[1]
    try:
        evs = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert calls == ["ripgrep_search"]
    q = next(e["question"] for e in evs if "question" in e)
    assert q["mode"] == "single"
    assert not any("final" in e for e in evs)                       # anthropic_style と同一の破棄意味論


def test_anthropic_style_accepts_client_factory():
    client = _AClient([_AResp([_ABlock("text", "直接回答。")], stop_reason="end_turn")])
    final = next(e for e in A.anthropic_style(lambda: client, "m", A.SYSTEM, "q", "v1", None) if "final" in e)
    assert final["final"] == "直接回答。" and final["searched"] is False   # factory（callable）でも動く
    assert final["stop_reason"] == "no_tool_calls"   # STOP-1: 到達可能経路の閉じた語彙を固定


def test_pdf_doc_id_resolves_to_derived_md():
    """RV Med: PDF ヒットを read_around で精読できる＝.pdf doc_id を派生 .pdf.md に解決する。
    `_safe_doc_path` は `(root, lexical_rel, path)` を返す。"""
    import tempfile
    o_dd = A.worlds.derived_md_dir
    der = pathlib.Path(tempfile.mkdtemp())
    (der / "設計").mkdir(parents=True, exist_ok=True)
    (der / "設計" / "資料.pdf.md").write_text("## ページ 1\n\n税率10%", encoding="utf-8")
    A.worlds.derived_md_dir = lambda w: der
    try:
        assert ".pdf" in A._OFFICE_MD and ".pdf" in A._READABLE_EXT
        resolved = A._safe_doc_path("w", "設計/資料.pdf")   # PDF doc_id → 派生 .pdf.md（read_around 可能）
        assert resolved is not None
        root, lexical_rel, p = resolved
        assert root == der and lexical_rel == "設計/資料.pdf.md" and p.name == "資料.pdf.md"
        assert A._safe_doc_path("w", "設計/欠落.pdf") is None  # 派生MD 無しは読めない
    finally:
        A.worlds.derived_md_dir = o_dd


def test_legacy_doc_id_resolves_to_derived_md():
    """W0 RV High: 旧形式（.doc/.xls/.ppt）は grep_search（derived md/ 直接見る）ではヒットするのに
    read_around（旧実装は _READABLE_EXT に .doc 等が無く拒否）で精読できない非対称があった。
    legacy_backend（W0）が前段変換した OOXML を①アームが MD化する際、出力名は原本 rel（`旧資料.doc.md`）
    に揃えているため、新形式と同じ解決規約（derived_md_dir 配下 `rel + ".md"`）で読める。"""
    import tempfile
    o_dd = A.worlds.derived_md_dir
    der = pathlib.Path(tempfile.mkdtemp())
    (der / "旧資料.doc.md").write_text("旧資料の中身テキストXYZ", encoding="utf-8")
    A.worlds.derived_md_dir = lambda w: der
    try:
        for ext in (".doc", ".xls", ".ppt"):
            assert ext in A._OFFICE_MD and ext in A._READABLE_EXT
        resolved = A._safe_doc_path("w", "旧資料.doc")
        assert resolved is not None
        root, lexical_rel, p = resolved
        assert root == der and lexical_rel == "旧資料.doc.md" and p.name == "旧資料.doc.md"
        assert p.read_text(encoding="utf-8") == "旧資料の中身テキストXYZ"
        assert A._safe_doc_path("w", "欠落.xls") is None        # 派生MD 無しは読めない（変換不可/未取込）
    finally:
        A.worlds.derived_md_dir = o_dd


# ===== rag 優先・legacy フォールバック（grep_search との整合） =====

def test_safe_doc_path_rejects_importance_control_file(monkeypatch, tmp_path):
    """`_重要度.txt`（文書の重要度設定ファイル自体）は read_around/verify_citation
    で精読できない（§5・除外契約）。拡張子（`.txt`）は `_READABLE_EXT` に含まれるため、除外の
    単一判定関数を明示的に通す必要がある。"""
    (tmp_path / "_重要度.txt").write_text("*.md: 高\n", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: tmp_path)
    assert A._safe_doc_path("w", "_重要度.txt") is None


def test_safe_doc_path_prefers_rag_when_enabled(monkeypatch, tmp_path):
    """`_safe_doc_path` は grep_search と同じ `grep_tool.preferred_derived_name` を使う:
    ON かつ rag.md が実在すればそちらを開く（legacy ではない）。§8.1 三階層＝rag/md は別ディレクトリ。"""
    der_md = tmp_path / "md"
    der_rag = tmp_path / "rag"
    der_md.mkdir(parents=True, exist_ok=True)
    (der_md / "report.docx.md").write_text("legacy", encoding="utf-8")
    der_rag.mkdir(parents=True, exist_ok=True)
    (der_rag / "report.docx.rag.md").write_text("rag", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: der_md)
    monkeypatch.setattr(A.worlds, "derived_rag_dir", lambda w: der_rag)

    resolved = A._safe_doc_path("w", "report.docx")
    assert resolved is not None
    root, lexical_rel, p = resolved
    assert root == der_rag and lexical_rel == "report.docx.rag.md" and p.name == "report.docx.rag.md"
    assert p.read_text(encoding="utf-8") == "rag"


def test_safe_doc_path_falls_back_to_legacy_when_rag_missing(monkeypatch, tmp_path):
    """ON でも rag.md が無い文書は従来どおり legacy 版を開く（縮退吸収）。"""
    (tmp_path / "onlylegacy.xlsx.md").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: tmp_path)

    resolved = A._safe_doc_path("w", "onlylegacy.xlsx")
    assert resolved is not None
    root, lexical_rel, p = resolved
    assert lexical_rel == "onlylegacy.xlsx.md" and p.name == "onlylegacy.xlsx.md"


def test_safe_doc_path_ignores_rag_when_disabled(monkeypatch, tmp_path):
    """TOGGLE-RM（2026-09-03）: グローバルな系統切替トグルは撤去済み・env では OFF にできない。
    `grep_tool.rag_grep_enabled` は今も内部シームとして残るため、直接差し替えて False 分岐
    （rag.md の実在に関わらず legacy 版を開く）を引き続き検証する。"""
    (tmp_path / "report.docx.md").write_text("legacy", encoding="utf-8")
    (tmp_path / "report.docx.rag.md").write_text("rag", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: tmp_path)
    monkeypatch.setattr(A.grep_tool, "rag_grep_enabled", lambda: False)

    resolved = A._safe_doc_path("w", "report.docx")
    assert resolved is not None
    root, lexical_rel, p = resolved
    assert lexical_rel == "report.docx.md" and p.name == "report.docx.md"
    assert p.read_text(encoding="utf-8") == "legacy"


def test_safe_doc_path_none_when_neither_rag_nor_legacy_exist(monkeypatch, tmp_path):
    """rag も legacy も存在しない doc_id は ON でも None（受入条件の直接固定）。"""
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: tmp_path)   # 空の派生 root

    assert A._safe_doc_path("w", "missing.docx") is None


# ===== classify_document への一本化（accepts 全滅＝未対応は read_around でも拒否・§7 裁定10） =====

def test_safe_doc_path_rejects_declined_registered_code_extension(monkeypatch, tmp_path):
    """登録拡張子（`_READABLE_EXT` は `registered_extensions()` を含む）でも `accepts()` が全滅
    （＝未対応）した文書は、拡張子の所属だけで「読める」と見なさない——grep/ES/list_docs と
    同じ `classify_document` の最終判定に集約する（既知 doc_id を直指定した read_around だけが
    抜け道になっていた穴を塞ぐ）。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    class _AlwaysDeclineCobol(Analyzer):
        name = "decline_cobol"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            return False

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_AlwaysDeclineCobol(),))
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: tmp_path)
    (tmp_path / "PROG.cbl").write_text("line 1\nTAX-RATE line\n", encoding="utf-8")

    assert A._safe_doc_path("w", "PROG.cbl") is None


def test_safe_doc_path_still_reads_accepted_registered_code_extension(monkeypatch, tmp_path):
    """既定 accepts（全アナライザ共通）の登録拡張子は従来どおり読める（回帰なし）。"""
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: tmp_path)
    (tmp_path / "PROG.cbl").write_text("       PROGRAM-ID. PROG.\n", encoding="utf-8")

    resolved = A._safe_doc_path("w", "PROG.cbl")
    assert resolved is not None
    root, lexical_rel, p = resolved
    assert root == tmp_path and lexical_rel == "PROG.cbl" and p.name == "PROG.cbl"


def test_safe_doc_path_rejects_out_of_root_symlink_before_calling_accepts(monkeypatch, tmp_path):
    """封じ込め（root 配下確認）・symlink 拒否は `classify_document`（accepts() 内容判定の
    read_head）より**先に**行う——範囲外シンボリックリンクの内容を検証前に読んでしまわない
    （多層防御・順序の固定）。`accepts()` を上書きする登録アナライザがあっても、範囲外へ
    resolve() する doc_id では一度も呼ばれないことを直接確認する。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    accepts_calls: list = []

    class _RecordingAnalyzer(Analyzer):
        name = "recording"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            accepts_calls.append(rel_path)
            return True

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_RecordingAnalyzer(),))

    world_root = tmp_path / "world"
    world_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.cbl").write_text("SECRET CONTENT", encoding="utf-8")
    (world_root / "link.cbl").symlink_to(outside / "secret.cbl")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: world_root)

    assert A._safe_doc_path("w", "link.cbl") is None
    assert accepts_calls == []          # 内容判定（read_head→accepts）は一度も呼ばれていない


def test_safe_doc_path_rejects_in_root_symlink_before_calling_accepts(monkeypatch, tmp_path):
    """symlink 拒否は resolve() **後**の実体だけを見ない——root **内**を指す symlink は、
    最終実体（symlink の先）自身が symlink でないため `is_symlink()` では検知できず、旧実装
    では通過して accepts() にリンク先の内容が渡ってしまっていた。字面パスと resolve() 結果の
    突き合わせで、root 内を指す symlink でも一律拒否し、accepts() が一度も呼ばれないことを
    直接確認する。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    accepts_calls: list = []

    class _RecordingAnalyzer(Analyzer):
        name = "recording"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            accepts_calls.append(rel_path)
            return True

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_RecordingAnalyzer(),))

    world_root = tmp_path / "world"
    world_root.mkdir()
    (world_root / "real.cbl").write_text("       PROGRAM-ID. REAL.\n", encoding="utf-8")
    (world_root / "link.cbl").symlink_to(world_root / "real.cbl")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: world_root)

    assert A._safe_doc_path("w", "link.cbl") is None
    assert accepts_calls == []          # 内容判定（read_head→accepts）は一度も呼ばれていない
    # 対照: symlink を介さない実体は従来どおり読める（回帰なし）。
    assert A._safe_doc_path("w", "real.cbl") is not None


def test_safe_doc_path_rejects_symlinked_ancestor_directory_before_calling_accepts(monkeypatch, tmp_path):
    """`cand` 自身は symlink でなくても、その**祖先ディレクトリ**が root 内外を問わず symlink
    だと同様に拒否する（字面パスとの不一致で検知・`_open_file_nofollow_walk` の祖先 symlink
    是正と同じ問題領域）。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    accepts_calls: list = []

    class _RecordingAnalyzer(Analyzer):
        name = "recording"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            accepts_calls.append(rel_path)
            return True

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_RecordingAnalyzer(),))

    world_root = tmp_path / "world"
    real_sub = tmp_path / "real_sub"
    real_sub.mkdir(parents=True)
    (real_sub / "file.cbl").write_text("       PROGRAM-ID. FILE.\n", encoding="utf-8")
    world_root.mkdir()
    (world_root / "sub").symlink_to(real_sub)          # 祖先ディレクトリが symlink
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: world_root)

    assert A._safe_doc_path("w", "sub/file.cbl") is None
    assert accepts_calls == []


def test_safe_doc_path_rejects_fifo_before_calling_accepts(monkeypatch, tmp_path):
    """regular file 確認（`rp.is_file()`）も `classify_document` より先——FIFO 等の非 regular は
    内容判定を試みる前に拒否する。"""
    import os as _os

    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    accepts_calls: list = []

    class _RecordingAnalyzer(Analyzer):
        name = "recording"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            accepts_calls.append(rel_path)
            return True

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_RecordingAnalyzer(),))
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: tmp_path)
    fifo_path = tmp_path / "pipe.cbl"
    _os.mkfifo(fifo_path)

    assert A._safe_doc_path("w", "pipe.cbl") is None
    assert accepts_calls == []


def test_read_around_resolves_target_exactly_once(monkeypatch, tmp_path):
    """read_around 全体で対象名解決（`grep_tool.preferred_derived_name`）が厳密に1回だけ呼ばれる
    ことを固定する。安定 fixture 上の結果一致だけでは、`_safe_doc_path` の内部と後段の lexical
    open が独立にもう一度解決する二重解決の再発を検出できない（呼び出し回数そのものを spy で見る）。"""
    world = "resolve-once-world"
    world_root = tmp_path / "kb" / world
    world_root.mkdir(parents=True)
    der = tmp_path / "derived" / world / "md"
    der.mkdir(parents=True)
    der_rag = tmp_path / "derived" / world / "rag"           # §8.1 三階層＝rag/md は別ディレクトリ
    der_rag.mkdir(parents=True)
    (der / "report.docx.md").write_text("legacy body TAX-RATE\n", encoding="utf-8")
    (der_rag / "report.docx.rag.md").write_text("## 見出し\nrag body TAX-RATE\n", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: world_root)
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(A.worlds, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(A.worlds, "observation_current_dir", lambda w: None)

    calls = []
    orig = A.grep_tool.preferred_derived_name

    def spy(root, rel):
        calls.append((root, rel))
        return orig(root, rel)

    monkeypatch.setattr(A.grep_tool, "preferred_derived_name", spy)

    res, _, _, _ = A.run_tool("read_around", {"doc_id": "report.docx", "line": 2, "window": 2}, world, None)
    assert "error" not in res and "rag body" in res["text"]
    assert len(calls) == 1, f"preferred_derived_name が{len(calls)}回呼ばれた（1回のみが契約）: {calls}"


def test_grep_and_read_around_agree_on_rag_priority_when_enabled(monkeypatch, tmp_path):
    """grep がヒットを作ったファイルと read_around が開くファイルは常に一致する。ON で rag.md を
    優先しているときに read_around が legacy を開いてしまうと、ヒット行番号と精読内容が食い違う
    （grep_search・_safe_doc_path・run_tool の lexical open が解決規約を共有していないと起きる非対称）。"""
    world = "align-rag-world"
    world_root = tmp_path / "kb" / world
    world_root.mkdir(parents=True)
    der = tmp_path / "derived" / world / "md"
    der.mkdir(parents=True)
    der_rag = tmp_path / "derived" / world / "rag"           # §8.1 三階層＝rag/md は別ディレクトリ
    der_rag.mkdir(parents=True)
    (der / "report.docx.md").write_text("legacy 本文 TAX-RATE 旧版\n", encoding="utf-8")
    (der_rag / "report.docx.rag.md").write_text("## 概要\nrag 本文 TAX-RATE 新版\n", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: world_root)
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(A.worlds, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(A.worlds, "observation_current_dir", lambda w: None)

    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, world, None)
    assert len(res["hits"]) == 1
    hit = res["hits"][0]
    assert hit["doc_id"] == "report.docx"

    r2, docs2, _, _ = A.run_tool(
        "read_around", {"doc_id": hit["doc_id"], "line": hit["line"], "window": 2}, world, None)
    assert "error" not in r2
    assert "rag 本文" in r2["text"]
    assert "legacy 本文" not in r2["text"]   # legacy を開いていたら混入するはずの文字列が無い
    assert "report.docx" in docs2


def test_grep_and_read_around_agree_on_legacy_when_disabled(monkeypatch, tmp_path):
    """TOGGLE-RM（2026-09-03）: グローバルな系統切替トグルは撤去済み・env では OFF にできない。
    `grep_tool.rag_grep_enabled` は今も内部シームとして残るため、直接差し替えて False 分岐
    （grep も read_around も従来どおり legacy だけを見る・回帰なし）を引き続き検証する。"""
    world = "align-legacy-world"
    world_root = tmp_path / "kb" / world
    world_root.mkdir(parents=True)
    der = tmp_path / "derived" / world / "md"
    der.mkdir(parents=True)
    (der / "report.docx.md").write_text("legacy 本文 TAX-RATE 旧版\n", encoding="utf-8")
    (der / "report.docx.rag.md").write_text("## 概要\nrag 本文 TAX-RATE 新版\n", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: world_root)
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(A.worlds, "observation_current_dir", lambda w: None)
    monkeypatch.setattr(A.grep_tool, "rag_grep_enabled", lambda: False)

    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, world, None)
    assert len(res["hits"]) == 1
    hit = res["hits"][0]
    assert hit["doc_id"] == "report.docx"

    r2, _, _, _ = A.run_tool(
        "read_around", {"doc_id": hit["doc_id"], "line": hit["line"], "window": 2}, world, None)
    assert "error" not in r2
    assert "legacy 本文" in r2["text"]
    assert "rag 本文" not in r2["text"]


def test_list_docs_registered_in_all_three_drivers_with_same_schema():
    """RV LOW（S1）: list_docs が3ドライバ全てのツール定義に同一の説明/スキーマで含まれることを固定する。
    特に Anthropic は openai_tools からの変換経路なので、provider toolset から落ちる回帰を検出する。"""
    ot = A.openai_tools(with_es=True, with_graph=True)
    o = next((t["function"] for t in ot if t["function"]["name"] == "list_docs"), None)
    assert o is not None, "openai_tools に list_docs が無い"

    gt = A.gemini_tools(with_es=True, with_graph=True)
    g = next((f for f in gt[0]["functionDeclarations"] if f["name"] == "list_docs"), None)
    assert g is not None, "gemini_tools に list_docs が無い"

    at = A.anthropic_tools_from_openai(ot)
    a = next((t for t in at if t["name"] == "list_docs"), None)
    assert a is not None, "anthropic 変換後に list_docs が無い"

    # 説明・スキーマが3経路で同一（単一の真実源 _DESC/_PARAMS からのブレを検出）。
    assert o["description"] == g["description"] == a["description"]
    assert o["parameters"] == g["parameters"] == a["input_schema"]


def test_tools_omit_ask_user_when_cannot_ask():
    """Med-1（RV・2026-07-07）: can_ask=False（依頼に「確認ID:」を含む回答再送）では ask_user ツール自体を
    渡さない＝再質問ループを構造的に塞ぐ（3ドライバ）。既定（can_ask=True）は従来どおり ask_user を含む。"""
    # OpenAI: can_ask=False で ask_user なし・True（既定）で含む。他ツールは残る（read_around 等）。
    ot_no = A.openai_tools(with_es=True, with_graph=True, can_ask=False)
    assert not any(t["function"]["name"] == "ask_user" for t in ot_no)
    assert any(t["function"]["name"] == "read_around" for t in ot_no)
    assert any(t["function"]["name"] == "ask_user" for t in A.openai_tools())

    # Gemini: 同上。
    gt_no = A.gemini_tools(with_es=True, with_graph=True, can_ask=False)
    assert not any(f["name"] == "ask_user" for f in gt_no[0]["functionDeclarations"])
    assert any(f["name"] == "ask_user" for f in A.gemini_tools()[0]["functionDeclarations"])

    # Anthropic は openai_tools 変換経路＝can_ask=False の toolset を渡せば ask_user は落ちる。
    at_no = A.anthropic_tools_from_openai(ot_no)
    assert not any(t["name"] == "ask_user" for t in at_no)


# ===== SC-6e: 検索経路トグル（grep/fulltext(ES)/graph）=====

def test_openai_tools_with_grep_false_omits_ripgrep_but_keeps_base_tools():
    """`with_grep=False`（既定 True）で ripgrep_search だけが落ち、
    list_docs/doc_outline/read_doc/read_around/ask_user は残る。
    SC-6e: 順序も含めて固定する（`insert_at` の計算違いを set 比較では検出できない）。"""
    t = A.openai_tools(with_es=True, with_graph=True, with_grep=False)
    names = [x["function"]["name"] for x in t]
    assert names == ["list_docs", "folder_tree", "graph_neighbors", "es_search", "doc_outline", "read_doc",
                     "read_around", "compare_documents", "ask_user"]
    # 既定（省略）は従来どおり grep（＋同居する glob_search）を含み、正準順（list_docs→
    # folder_tree→ripgrep_search→glob_search→graph_neighbors→es_search→doc_outline→read_doc→
    # read_around→compare_documents→ask_user）のまま。
    assert [x["function"]["name"] for x in A.openai_tools(with_es=True, with_graph=True)] == [
        "list_docs", "folder_tree", "ripgrep_search", "glob_search", "graph_neighbors", "es_search",
        "doc_outline", "read_doc", "read_around", "compare_documents", "ask_user"]


def test_gemini_tools_with_grep_false_omits_ripgrep_but_keeps_base_tools():
    fns = A.gemini_tools(with_es=True, with_graph=True, with_grep=False)[0]["functionDeclarations"]
    names = [f["name"] for f in fns]
    assert names == ["list_docs", "folder_tree", "graph_neighbors", "es_search", "doc_outline", "read_doc",
                     "read_around", "compare_documents", "ask_user"]


def test_openai_style_tools_pref_default_toolset_excludes_off_tools(monkeypatch):
    """`toolset` 省略時のデフォルト構築（`openai_tools(...)`）が `tools_pref` を反映する
    （ES/Neo4j 到達可否ゲートとの AND・§3.6）。実際に `_post` へ送る body["tools"] を
    順序付き list で検証する（SC-6e・set 化すると順序回帰を検出できない）。"""
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured["names"] = [t["function"]["name"] for t in body["tools"]]
        return {"choices": [{"message": {"content": "回答", "finish_reason": "stop"}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None,
                        tools_pref={"grep": False, "fulltext": True, "graph": True}))
    assert captured["names"] == ["list_docs", "folder_tree", "graph_neighbors", "es_search", "doc_outline",
                                 "read_doc", "read_around", "compare_documents", "ask_user"]


def test_openai_style_tools_pref_none_keeps_existing_default_behavior(monkeypatch):
    """`tools_pref` 省略（None）は全ON＝既存呼び出し元と byte-identical（ES/Neo4j 到達可否のみで決まる）。
    順序も基点（list_docs→folder_tree→ripgrep_search→glob_search→graph_neighbors→es_search→
    doc_outline→read_doc→read_around→compare_documents→ask_user）と一致する。"""
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured["names"] = [t["function"]["name"] for t in body["tools"]]
        return {"choices": [{"message": {"content": "回答", "finish_reason": "stop"}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    assert captured["names"] == ["list_docs", "folder_tree", "ripgrep_search", "glob_search", "graph_neighbors",
                                 "es_search", "doc_outline", "read_doc", "read_around",
                                 "compare_documents", "ask_user"]


def test_gemini_tools_pref_default_toolset_excludes_off_tools(monkeypatch):
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        fns = body["tools"][0]["functionDeclarations"]
        captured["names"] = [f["name"] for f in fns]
        return {"candidates": [{"content": {"parts": [{"text": "回答"}]}, "finishReason": "STOP"}]}

    monkeypatch.setattr(A, "_post", fake_post)
    list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None,
                  tools_pref={"grep": True, "fulltext": False, "graph": True}))
    assert captured["names"] == ["list_docs", "folder_tree", "ripgrep_search", "glob_search", "graph_neighbors",
                                 "doc_outline", "read_doc", "read_around", "compare_documents", "ask_user"]


def test_anthropic_style_tools_pref_default_toolset_excludes_off_tools(monkeypatch):
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    captured = {}

    class FakeResp:
        stop_reason = "end_turn"
        content = [type("Blk", (), {"type": "text", "text": "回答"})()]
        usage = None

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                captured["names"] = [t["name"] for t in kwargs["tools"]]
                return FakeResp()

    list(A.anthropic_style(FakeClient(), "m", A.SYSTEM, "質問", "v1", None,
                           tools_pref={"grep": True, "fulltext": True, "graph": False}))
    assert captured["names"] == ["list_docs", "folder_tree", "ripgrep_search", "glob_search", "es_search",
                                 "doc_outline", "read_doc", "read_around", "compare_documents", "ask_user"]


def test_openai_style_explicit_toolset_skips_availability_check(monkeypatch):
    """SC-6e: `toolset` を明示指定した呼び出し（検索アシスタント等）は `tool_availability()`
    （ひいては ES/Neo4j への実接続チェック）を一切呼ばない——`toolset` は既に確定済みのツール
    定義配列のため、`tools_pref`/`tools_availability` のどちらを省略しても再確認は不要。"""
    calls = _counting_probe(monkeypatch)
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: {
        "choices": [{"message": {"content": "回答", "finish_reason": "stop"}}]})
    fixed_toolset = [{"type": "function", "function": {
        "name": "ripgrep_search", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None, toolset=fixed_toolset))
    assert calls == {"es": 0, "graph": 0}


def test_gemini_explicit_toolset_skips_availability_check(monkeypatch):
    calls = _counting_probe(monkeypatch)
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: {
        "candidates": [{"content": {"parts": [{"text": "回答"}]}, "finishReason": "STOP"}]})
    fixed_toolset = [{"functionDeclarations": [
        {"name": "ripgrep_search", "description": "d", "parameters": {"type": "object", "properties": {}}}]}]
    list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None, toolset=fixed_toolset))
    assert calls == {"es": 0, "graph": 0}


def test_anthropic_style_explicit_toolset_skips_availability_check(monkeypatch):
    calls = _counting_probe(monkeypatch)

    class FakeResp:
        stop_reason = "end_turn"
        content = [type("Blk", (), {"type": "text", "text": "回答"})()]
        usage = None

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return FakeResp()

    fixed_toolset = [{"type": "function", "function": {
        "name": "ripgrep_search", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    list(A.anthropic_style(FakeClient(), "m", A.SYSTEM, "質問", "v1", None, toolset=fixed_toolset))
    assert calls == {"es": 0, "graph": 0}


# ===== SC-6e: 可用性の実接続判定（UI/実行側の共有） =====

def test_graph_available_real_connectivity_check_unreachable_uri(monkeypatch):
    """`_graph_available` は URI の有無だけでなく実接続を確認する——未起動（到達不可）な URI では
    False になる（旧実装は `world_neo4j.default_neo4j_uri()` のフォールバックにより常に True だった）。"""
    from sherpa.ingest import world_neo4j
    monkeypatch.setattr(world_neo4j, "_env", lambda: {
        "uri": "bolt://127.0.0.1:1", "user": "neo4j", "pw": "x"})
    assert A._graph_available() is False


def test_tool_availability_grep_always_true(monkeypatch):
    """grep は外部依存が無いため常に True。fulltext/graph は各可用性判定に委譲する。"""
    monkeypatch.setattr(A.es_index, "available", lambda: False)
    monkeypatch.setattr(A, "_graph_available", lambda: False)
    assert A.tool_availability() == {"grep": True, "fulltext": False, "graph": False}


def _counting_probe(monkeypatch, es_result=True, graph_result=True):
    """`es_index.available`/`_graph_available` の呼び出し回数を数える偽物に差し替える。"""
    calls = {"es": 0, "graph": 0}

    def _es():
        calls["es"] += 1
        return es_result

    def _graph():
        calls["graph"] += 1
        return graph_result

    monkeypatch.setattr(A.es_index, "available", _es)
    monkeypatch.setattr(A, "_graph_available", _graph)
    return calls


def test_tool_availability_dedupes_repeated_calls_within_ttl(monkeypatch):
    """SC-6e: TTL 内の複数回呼び出しは1回分の実接続チェックだけを行い、以降はキャッシュを返す
    （ターン内で複数箇所——ルータの422判定・agentic既定toolset構築・検索アシスタント複数本——が
    独立に呼んでも、実接続チェックの直列加算にならない）。"""
    calls = _counting_probe(monkeypatch)
    first = A.tool_availability()
    second = A.tool_availability()
    third = A.tool_availability()
    assert first == second == third == {"grep": True, "fulltext": True, "graph": True}
    assert calls == {"es": 1, "graph": 1}


def test_tool_availability_force_bypasses_cache(monkeypatch):
    """`force=True` は TTL 内でも必ず再計算する（`sherpa.health.snapshot(force=True)` と同じ流儀）。"""
    calls = _counting_probe(monkeypatch)
    A.tool_availability()
    A.tool_availability(force=True)
    A.tool_availability(force=True)
    assert calls == {"es": 3, "graph": 3}


def test_tool_availability_ttl_expiry_triggers_recheck(monkeypatch):
    """TTL 経過後は force を指定しなくても再計算する（キャッシュの `at` を TTL 分だけ過去へ
    ずらして経過をシミュレートする・実時間の sleep はしない）。"""
    calls = _counting_probe(monkeypatch, es_result=False, graph_result=False)
    first = A.tool_availability()
    assert first == {"grep": True, "fulltext": False, "graph": False}
    assert calls == {"es": 1, "graph": 1}
    # TTL 内はキャッシュのまま（可用性が変わっていても反映されない）。
    calls["es"], calls["graph"] = 0, 0
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    still_cached = A.tool_availability()
    assert still_cached == {"grep": True, "fulltext": False, "graph": False}
    # TTL 経過をシミュレート（`at` を TTL+1 秒だけ過去にずらす）すると次の呼び出しで再計算される。
    A._tools_availability_cache["at"] -= (A._TOOLS_AVAILABILITY_TTL + 1)
    refreshed = A.tool_availability()
    assert refreshed == {"grep": True, "fulltext": True, "graph": True}


def test_tools_availability_ttl_env_rejects_invalid_values_at_import():
    """TTL は正の有限値に限定し、不正値（0/負値/NaN/inf/非数値）は起動時（import時）に明示
    エラーで落ちる——他の env 駆動チューニング値（`es_index._env_float` 等）のような黙った
    クランプはしない。TTL がプローブ所要時間以下だと待機側が毎回「期限切れ」と誤判定し、
    single-flight（同時 miss の集約）が静かに壊れるため、不正値は fail-closed にする。"""
    for bad in ("0", "-1", "nan", "inf", "abc"):
        stderr = FI.fresh_import_fails("sherpa.agentic_search",
                                       env={"SHERPA_TOOLS_AVAILABILITY_TTL": bad})
        assert "SHERPA_TOOLS_AVAILABILITY_TTL" in stderr, f"bad={bad!r} stderr={stderr}"


def test_tool_availability_records_at_after_probe_completes(monkeypatch):
    """キャッシュの `at`（鮮度の起点）はプローブ完了後に記録する——プローブ開始前の時刻を
    使うと、TTL がプローブ所要時間以下の構成で待機側が「期限切れ」と誤判定し single-flight が
    成立しなくなる。遅いプローブを模し、記録された `at` がプローブ開始時刻より後（プローブに
    要した時間分だけ進んでいる）ことを確認する。"""
    import time as _time

    def _slow_es():
        _time.sleep(0.05)
        return True

    monkeypatch.setattr(A.es_index, "available", _slow_es)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    before = _time.monotonic()
    A.tool_availability()
    recorded_at = A._tools_availability_cache["at"]
    assert recorded_at - before >= 0.04, "at がプローブ開始前の時刻のまま記録されている"


def test_tool_availability_single_flight_under_real_concurrency(monkeypatch):
    """複数スレッドが実際に同時に呼び出しても、実接続チェックは1回だけに集約される
    （`test_tool_availability_dedupes_repeated_calls_within_ttl` は同一スレッドからの逐次呼出し
    だけを検査しており、ロックの取得順・複数スレッドが本当に競合するタイミングを再現できない）。
    `threading.Barrier` で全スレッドの呼び出し開始タイミングを揃え、真の同時 miss を作る。"""
    import threading
    import time as _time

    calls = {"es": 0, "graph": 0}
    call_lock = threading.Lock()

    def _slow_es():
        with call_lock:
            calls["es"] += 1
        _time.sleep(0.05)   # 実接続チェック相当の遅延——他スレッドがロック待ちになる窓を作る
        return True

    def _graph():
        with call_lock:
            calls["graph"] += 1
        return True

    monkeypatch.setattr(A.es_index, "available", _slow_es)
    monkeypatch.setattr(A, "_graph_available", _graph)

    n = 8
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        barrier.wait()   # 全スレッドがここで足並みを揃えてから呼ぶ（真の同時 miss）
        results[i] = A.tool_availability()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(r == {"grep": True, "fulltext": True, "graph": True} for r in results)
    assert calls == {"es": 1, "graph": 1}, f"single-flight が成立していない: {calls}"


def test_tool_availability_short_ttl_single_flight_holds_under_concurrency(monkeypatch):
    """正の短小 TTL（既定20秒に対し極端に短い値）でも、ロック待機中に完成したキャッシュ世代を
    共有し、待機側がロック受け渡しの遅延だけで「期限切れ」と誤判定して再probeしない。
    実測（このRVの指摘元）: 生成時刻だけを見るTTL判定では、20並行・probe20ms・TTL 0.0001秒で
    最大13回まで再probeが発生していた。呼び出し開始時刻（`call_start`・ロック取得前に記録）
    以降に完成した世代は、TTLに関わらず共有する是正で、この極端な設定でも1回に集約される
    はず。"""
    import threading
    import time as _time

    monkeypatch.setattr(A, "_TOOLS_AVAILABILITY_TTL", 0.0001)   # probe所要時間よりはるかに短い
    calls = {"es": 0, "graph": 0}
    call_lock = threading.Lock()

    def _slow_es():
        with call_lock:
            calls["es"] += 1
        _time.sleep(0.02)   # 実測条件（probe20ms）を再現
        return True

    def _graph():
        with call_lock:
            calls["graph"] += 1
        return True

    monkeypatch.setattr(A.es_index, "available", _slow_es)
    monkeypatch.setattr(A, "_graph_available", _graph)

    n = 20
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        barrier.wait()   # 全スレッドがここで足並みを揃えてから呼ぶ（真の同時 miss）
        results[i] = A.tool_availability()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(r == {"grep": True, "fulltext": True, "graph": True} for r in results)
    assert calls == {"es": 1, "graph": 1}, f"短小TTLで single-flight が崩れている: {calls}"


def test_unavailable_explicit_tools_flags_only_explicit_true_and_unavailable():
    assert A.unavailable_explicit_tools(None) == []
    assert A.unavailable_explicit_tools({"graph": False}) == []          # 明示 OFF は対象外
    assert A.unavailable_explicit_tools({}) == []                        # 何も明示していない


def test_unavailable_explicit_tools_reports_canonical_order(monkeypatch):
    monkeypatch.setattr(A, "tool_availability", lambda: {"grep": True, "fulltext": False, "graph": False})
    assert A.unavailable_explicit_tools({"fulltext": True, "graph": True}) == ["fulltext", "graph"]
    assert A.unavailable_explicit_tools({"grep": True}) == []   # grep は常に available


def test_unavailable_explicit_tools_uses_passed_snapshot_without_recheck(monkeypatch):
    """`availability` を渡すと `tool_availability()` を一切呼ばない——受付時の422判定と実行本体
    （`handle_message`/`stream_message`）が別々に可用性を再取得すると、TTLキャッシュの境界を
    挟んで判定が食い違い得るため、呼び出し元が計算済みの同一 snapshot を明示的に渡せる。"""
    calls = []
    monkeypatch.setattr(A, "tool_availability", lambda: (calls.append(1), {"grep": True})[1])
    snapshot = {"grep": True, "fulltext": True, "graph": False}
    assert A.unavailable_explicit_tools({"graph": True}, availability=snapshot) == ["graph"]
    assert calls == []   # 渡した snapshot をそのまま使い、都度チェックはしない


# ===== SC-6e: 非agentic経路（_dispatch/_gather）の実効ツール判定 =====

def test_dispatch_tools_for_lens_impact_requires_graph():
    eff, blocked = A.dispatch_tools_for_lens("impact", None, availability={
        "grep": True, "fulltext": True, "graph": False})
    assert blocked is True
    assert eff == {"grep": True, "fulltext": True, "graph": False}


def test_dispatch_tools_for_lens_troubleshoot_requires_graph():
    _, blocked = A.dispatch_tools_for_lens("troubleshoot", None, availability={
        "grep": True, "fulltext": True, "graph": False})
    assert blocked is True


def test_dispatch_tools_for_lens_qa_needs_grep_or_fulltext():
    _, blocked_both_off = A.dispatch_tools_for_lens(
        "qa", {"grep": False, "fulltext": False, "graph": True}, availability=None)
    assert blocked_both_off is True
    _, blocked_grep_only = A.dispatch_tools_for_lens(
        "qa", {"fulltext": False}, availability=None)
    assert blocked_grep_only is False   # grep が残っている


def test_dispatch_tools_for_lens_availability_omitted_means_fully_available():
    """`availability` 省略（既定 None）は全て利用可能扱い＝`tools_pref` の希望どおりに決まる。"""
    eff, blocked = A.dispatch_tools_for_lens("impact", None)
    assert eff == {"grep": True, "fulltext": True, "graph": True}
    assert blocked is False


# ===== SC-6e: 検索経路トグルに応じた SYSTEM/description の組み立て =====

# SC-6e: `system_prompt()`/`_desc_es`/`_desc_graph` は全ON実装が「対応する定数をそのまま
# 返す」だけ（`if grep and fulltext and graph: return SYSTEM` 等）のため、`is A.SYSTEM`/
# `== A._DESC_ES` の自己参照比較は SYSTEM/_DESC_ES/_DESC_GRAPH の中身が何であっても必ず通る恒真式
# になり、意図しない内容変化を検出できない。固定 byte 長＋SHA-256（独立の golden）で比較する。
# `_SYSTEM_GOLDEN_*` は現在の SYSTEM の中身（glob_search の使いどころ・「コマンド検索」表記を
# 含む）に対する固定値——中身を変えたらここも更新する（golden の意図は「意図しない変化を検知
# する」ことであって、特定の過去の値に固定し続けることではない）。
_SYSTEM_GOLDEN_BYTES = 3900
_SYSTEM_GOLDEN_SHA256 = "ad36c79d371aa0de27cac844c983814ae28420eb6235f9b83835dd0719a7745c"
_DESC_ES_GOLDEN_BYTES = 315
_DESC_ES_GOLDEN_SHA256 = "3cf6ee101c15db27cd4ac5a9315f6c55f0de61e6e67eefd8971a5217326135ee"
_DESC_GRAPH_GOLDEN_BYTES = 553
_DESC_GRAPH_GOLDEN_SHA256 = "8a039ffdb660f76dd3d976aef666ee68eec716b666549dc5861fb9d090f52820"


def _sha256_utf8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_system_prompt_full_on_matches_fixed_golden_hash():
    """全ON（省略含む）は固定 golden（byte 長＋SHA-256）と一致する（自己参照の恒真式を廃止・SC-6e）。"""
    for v in (A.system_prompt(), A.system_prompt(None),
             A.system_prompt({"grep": True, "fulltext": True, "graph": True})):
        assert len(v.encode("utf-8")) == _SYSTEM_GOLDEN_BYTES
        assert _sha256_utf8(v) == _SYSTEM_GOLDEN_SHA256


def test_system_prompt_grep_off_omits_ripgrep_mention():
    text = A.system_prompt({"grep": False, "fulltext": True, "graph": True})
    assert "ripgrep_search" not in text
    assert "es_search" in text and "graph_neighbors" in text


def test_system_prompt_fulltext_off_omits_es_search_mention():
    text = A.system_prompt({"grep": True, "fulltext": False, "graph": True})
    assert "es_search" not in text
    assert "ripgrep_search" in text and "graph_neighbors" in text


def test_system_prompt_graph_off_omits_graph_neighbors_mention():
    text = A.system_prompt({"grep": True, "fulltext": True, "graph": False})
    assert "graph_neighbors" not in text
    assert "ripgrep_search" in text and "es_search" in text


def test_system_prompt_grep_and_fulltext_off_omits_content_search_step_entirely():
    """grep/fulltext とも OFF（graph のみ）: 本文検索の手順そのものを案内しない。"""
    text = A.system_prompt({"grep": False, "fulltext": False, "graph": True})
    assert "ripgrep_search" not in text
    assert "es_search" not in text
    assert "graph_neighbors" in text


def test_system_prompt_grep_only_has_no_comparison_or_es_mention():
    text = A.system_prompt({"grep": True, "fulltext": False, "graph": False})
    assert "es_search" not in text
    assert "graph_neighbors" not in text
    assert "ripgrep_search" in text


def test_system_prompt_grep_off_also_omits_glob_mention():
    """glob_search は grep 軸に同居する——grep OFF/不達では ripgrep_search と同様に案内しない。"""
    text = A.system_prompt({"grep": False, "fulltext": True, "graph": True})
    assert "glob_search" not in text


def test_system_prompt_grep_on_mentions_glob():
    text = A.system_prompt({"grep": True, "fulltext": True, "graph": True})
    assert "glob_search" in text


def test_desc_es_and_desc_graph_omit_grep_mention_when_grep_off():
    assert "ripgrep_search" not in A._desc_es(with_grep=False)
    assert "grep" not in A._desc_graph(with_grep=False)


def test_desc_es_and_desc_graph_full_on_match_fixed_golden_hash():
    """`_desc_es`/`_desc_graph` の全ON（`with_grep=True`）値も自己参照でなく固定 golden と比較する
    （`test_system_prompt_full_on_matches_fixed_golden_hash` と同じ理由・SC-6e）。"""
    es = A._desc_es(with_grep=True)
    graph = A._desc_graph(with_grep=True)
    assert len(es.encode("utf-8")) == _DESC_ES_GOLDEN_BYTES
    assert _sha256_utf8(es) == _DESC_ES_GOLDEN_SHA256
    assert len(graph.encode("utf-8")) == _DESC_GRAPH_GOLDEN_BYTES
    assert _sha256_utf8(graph) == _DESC_GRAPH_GOLDEN_SHA256


def test_openai_tools_description_reflects_grep_off():
    tools = A.openai_tools(with_es=True, with_graph=True, with_grep=False)
    es_desc = next(t["function"]["description"] for t in tools if t["function"]["name"] == "es_search")
    graph_desc = next(t["function"]["description"] for t in tools if t["function"]["name"] == "graph_neighbors")
    assert "ripgrep_search" not in es_desc
    assert "grep" not in graph_desc


# ===== GLOB-1: glob_search（ファイル名/パスのグロブ検索）は grep 軸に同居 =====

def test_openai_tools_glob_search_gated_by_with_grep():
    """glob_search は with_grep=True のときだけ提示され、False では ripgrep_search と同様に消える。"""
    on = A.openai_tools(with_grep=True)
    off = A.openai_tools(with_grep=False)
    assert any(t["function"]["name"] == "glob_search" for t in on)
    assert not any(t["function"]["name"] == "glob_search" for t in off)
    assert not any(t["function"]["name"] == "ripgrep_search" for t in off)


def test_gemini_tools_glob_search_gated_by_with_grep():
    on = A.gemini_tools(with_grep=True)[0]["functionDeclarations"]
    off = A.gemini_tools(with_grep=False)[0]["functionDeclarations"]
    assert any(f["name"] == "glob_search" for f in on)
    assert not any(f["name"] == "glob_search" for f in off)


def test_glob_search_registered_in_all_three_drivers_with_same_schema():
    """list_docs と同じ固定（RV LOW・S1）を glob_search にも適用する（`test_list_docs_registered_
    in_all_three_drivers_with_same_schema` と同じ理由）。"""
    ot = A.openai_tools(with_grep=True)
    o = next((t["function"] for t in ot if t["function"]["name"] == "glob_search"), None)
    assert o is not None, "openai_tools に glob_search が無い"

    gt = A.gemini_tools(with_grep=True)
    g = next((f for f in gt[0]["functionDeclarations"] if f["name"] == "glob_search"), None)
    assert g is not None, "gemini_tools に glob_search が無い"

    at = A.anthropic_tools_from_openai(ot)
    a = next((t for t in at if t["name"] == "glob_search"), None)
    assert a is not None, "anthropic 変換後に glob_search が無い"

    assert o["description"] == g["description"] == a["description"]
    assert o["parameters"] == g["parameters"] == a["input_schema"]


def test_glob_search_matches_basename_pattern_at_any_depth():
    """スラッシュを含まないパターン（例 `*.jcl`）は深さを問わずファイル名に一致する
    （ripgrep の `--glob` と同じ慣習）。"""
    res, docs, cites, cards = A.run_tool("glob_search", {"pattern": "*.jcl"}, "v1", None)
    expected = CE.rel_paths_glob("*.jcl")
    assert expected                                          # 前提: fixtures に .jcl がある
    assert res["count"] == len(expected)
    assert set(res["paths"]) == expected
    assert res["truncated"] is False
    assert docs == expected                                  # 返した分だけ出典(docs)に載る
    assert cites == [] and cards == []                        # glob_search は引用/カードを作らない


def test_glob_search_case_insensitive():
    upper, _, _, _ = A.run_tool("glob_search", {"pattern": "*.JCL"}, "v1", None)
    lower, _, _, _ = A.run_tool("glob_search", {"pattern": "*.jcl"}, "v1", None)
    assert upper["count"] > 0
    assert set(upper["paths"]) == set(lower["paths"])


def test_glob_search_slash_pattern_matches_hierarchical_segments():
    """スラッシュを含むパターンは world ルートからの絞り込み＝`**` だけが複数階層を跨ぐ。"""
    res, _, _, _ = A.run_tool("glob_search", {"pattern": "4期/02_設計/**/*.md"}, "v1", None)
    expected = CE.rel_paths_glob("4期/02_設計/**/*.md")
    assert expected
    assert set(res["paths"]) == expected


def test_glob_search_zero_hits():
    res, docs, cites, cards = A.run_tool("glob_search", {"pattern": "*.no-such-ext"}, "v1", None)
    assert res == {"count": 0, "paths": [], "truncated": False}
    assert docs == set() and cites == [] and cards == []


def test_glob_search_truncates_at_200_and_marks_truncated(monkeypatch):
    """要件: 上限200件で打ち切り・打ち切りは明示（`truncated`）。fixtures には200件を超える対象が
    無いため `doc_ledger.documents_for` を差し替えて件数超過を作る
    （`test_run_tool_forwards_deadline_to_documents_for_for_list_docs` と同じ差し替え流儀）。"""
    from sherpa import doc_ledger as DL

    rows = [{"name": f"synth/{i:04d}.md", "branch": "docs"} for i in range(250)]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)
    res, docs, _, _ = A.run_tool("glob_search", {"pattern": "synth/*.md"}, "v1", None)
    assert res["count"] == 250
    assert len(res["paths"]) == 200
    assert res["truncated"] is True
    assert len(docs) == 200


def test_glob_search_invalid_pattern_returns_error():
    for bad in ["", "   ", "/abs/path", "a\\b", "a/../b", "a//b", "x" * (A._GLOB_PATTERN_MAX_LEN + 1), 123, None]:
        res, docs, cites, cards = A.run_tool("glob_search", {"pattern": bad}, "v1", None)
        assert "error" in res, f"invalid pattern accepted: {bad!r}"
        assert docs == set() and cites == [] and cards == []


def test_glob_search_respects_session_scope():
    """scope_paths（セッション範囲）は list_docs と同じ規約で glob_search にも効く。"""
    scoped, docs, _, _ = A.run_tool("glob_search", {"pattern": "*.md"}, "v1", ["4期/03_開発"])
    assert scoped == {"count": 0, "paths": [], "truncated": False}   # 03_開発 配下に .md は無い
    assert docs == set()

    unscoped, _, _, _ = A.run_tool("glob_search", {"pattern": "*.md"}, "v1", None)
    assert unscoped["count"] > 0                                     # 範囲を外せば .md がヒットする


def test_glob_search_layer_code_and_docs_partition():
    """list_docs と同じ硬い層フィルタ（`layer_mod.in_layer_code`）を glob_search にも適用する。"""
    prefix = "4期/03_開発"
    all_files = CE.rel_paths_under(prefix)
    assert all_files   # 前提: fixtures はこのフォルダにソース（cbl/cpy/jcl）を持つ

    res_code, docs_code, _, _ = A.run_tool("glob_search", {"pattern": "*"}, "v1", [prefix], layer="code")
    assert set(res_code["paths"]) == all_files == docs_code   # 03_開発 配下は全てソース

    res_docs, _, _, _ = A.run_tool("glob_search", {"pattern": "*"}, "v1", [prefix], layer="docs")
    assert res_docs["paths"] == []


def test_agentic_loop_uses_tools_aware_system_prompt(monkeypatch):
    """provider._agentic_loop が `ctx.scope_meta["tools"]` を `agentic_search.system_prompt` へ渡す
    （openai/ollama/gemini/bedrock/検索アシスタント共通の配線・SC-6e）。"""
    from sherpa.agents import Ctx, OpenAIProvider

    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured["system"] = body["messages"][0]["content"]
        return {"choices": [{"message": {"content": "回答", "finish_reason": "stop"}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    p = OpenAIProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="質問", world="v1", route=lambda m: {}, dispatch=lambda l, i: {},
             scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                         "tools": {"grep": False, "fulltext": True, "graph": True}})
    list(p._agentic_loop(ctx))
    assert "ripgrep_search" not in captured["system"]
    assert captured["system"] != A.SYSTEM


def test_agentic_run_resolves_none_tools_availability_consistently_for_system_and_schema(monkeypatch):
    """`ctx.tools_availability` が `None`（provider を直接呼ぶ経路・通常の chat 経路
    （`chat_service.handle_message`/`stream_message`）は必ずターン先頭の snapshot を渡すため
    到達しない）でも、SYSTEM とツール schema が同じ実効集合を見る。`_agentic_run` が入口で
    1回だけ解決せず各所が個別に「省略時は都度チェック」へ倒れると、gate 判定・SYSTEM は
    「省略=全て利用可能」の楽観的前提のまま通過するのに、schema だけが実接続の結果
    （ここではグラフ不達）を反映してしまい、SYSTEM が実際には提示されないツールを推奨する
    食い違いが起きる。impact レンズ（グラフ必須）で確認する——qa/author は grep が常に
    available 固定で gate 判定に影響しないため解決の対象外（`_agentic_run` 参照・
    provider の URL 構築＝SSRF チョークポイントより前に実接続チェックが走ってしまう回帰を
    避けるため、影響のあるグラフ必須レンズだけに絞ってある）。"""
    from sherpa.agents import Ctx, OpenAIProvider

    # グラフは available のまま（impact の gate 判定はグラフのみを見るため blocked にしない）・
    # 全文（fulltext/ES）だけ不達にして、gate 判定に影響しない軸で SYSTEM/schema の一致を見る。
    monkeypatch.setattr(A.es_index, "available", lambda: False)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured["system"] = body["messages"][0]["content"]
        captured["tool_names"] = [t["function"]["name"] for t in body["tools"]]
        return {"choices": [{"message": {"content": "回答", "finish_reason": "stop"}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    p = OpenAIProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="質問", world="v1", knowledge=True,
             route=lambda m: {"lens": "impact", "reason": "テスト", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}},
             make_sources=lambda docs: [],
             scope_meta={"world": "v1", "scope_paths": [], "source": "all"})
    # tools_availability は既定 None のまま渡す（provider 直呼び出し・通常経路は必ず渡す）。
    list(p.run(ctx))
    assert "es_search" not in captured["system"]
    assert "es_search" not in captured["tool_names"]


def test_can_ask_helper_detects_confirm_id_resend():
    """Med-1: `agents._can_ask` は依頼に「確認ID:」があれば False（回答再送＝再質問しない）。"""
    from sherpa import agents as AG
    assert AG._can_ask("税率を変えたら夜間バッチが落ちる？") is True
    assert AG._can_ask("選択: 対象範囲\n確認ID: confirm-abcd\n元の依頼: …") is False
    assert AG._can_ask("確認ID：lens-0011\n選択: 影響") is False   # 全角コロンも検出


def test_impact_lens_uses_agentic_tool_loop():
    """影響分析（impact）も反復ツール検索を通る（2026-08-15 決定）。

    従来は `run()` の分岐が impact を除外しており、Neo4j を1回引くだけで終わっていた。
    グラフが 0 件だと「根拠なし」で終わってしまい、自前 grep を続ける Codex と差が出ていた。
    ここでは「impact でも `_agentic_loop` が呼ばれ、引用付きの envelope が返る」ことを固定する
    （author だけは agentic_search 未対応ツールのため従来どおり単発取得）。

    world/doc_id は fixtures/corpus/v1 実在ファイル（EXT-2 機械検証が既定 ON のため、実在しない
    doc を指す citation は Committed Evidence から落ちる。テストの関心はルーティング＝
    「impact が agentic ループを通るか」であり doc の実体とは無関係なので、実在ファイルへ
    差し替えるだけで足りる）。
    """
    from sherpa.providers.base import Ctx, _GenProvider

    real_doc = "4期/04_運用/障害記録.md"
    seen = []

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"

        def _agentic_loop(self, ctx):
            seen.append("agentic")
            yield {"node": {"type": "node", "id": "n", "kind": "tool", "label": "資料を検索（grep）"}}
            yield {"final": "税率変更は夜間バッチに影響します", "docs": {real_doc}, "searched": True,
                   "cites": [{"doc_id": real_doc, "span": [1, 1], "quote": "# 障害記録"}], "cards": []}

    def _ctx(lens):
        return Ctx(message="税率を変えたら夜間バッチが落ちる？", world="v1", knowledge=True,
                   route=lambda m: {"lens": lens, "reason": "テスト", "input": m},
                   dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}},
                   make_sources=lambda docs: [])

    events = list(_P().run(_ctx("impact")))
    assert seen == ["agentic"], "impact が反復ツール検索を通っていない"
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["lens"] == "impact"
    assert len(env["data"]["citations"]) == 1     # グラフ 0 件でも根拠が残る

    seen.clear()
    list(_P().run(_ctx("author")))
    assert seen == [], "author は従来どおり単発取得のまま（agentic_search 未対応ツール）"


# ===== TOOLREAD: read_doc/doc_outline（土台系・新設） =====
# 「全文が見えない」「長文の構造が掴めない」の解消——read_doc は doc_id＋開始行からページングして
# 通読、doc_outline は見出し一覧（行番号つき）を返して当たりを付ける。scope/層フィルタ・
# symlink TOCTOU 対策は read_around と同じ機構（`_open_doc_stream`）を共有する。

def test_run_tool_read_doc_normal_paginates_forward(monkeypatch, tmp_path):
    """M-3: 1ページの幅は最低200行（read_around の200行フロアと同じ流儀）——window_cap が
    それより小さくても200行フロアが優先される。最終ページは総行数で打ち切る。"""
    world = "read-doc-world"
    _isolate_world_kb(monkeypatch, tmp_path, world,
                      {"doc.md": "\n".join(f"line {i}" for i in range(1, 451))})   # 450行

    res1, docs1, cites1, cards1 = A.run_tool("read_doc", {"doc_id": "doc.md"}, world, None, window_cap=5)
    assert "error" not in res1, res1
    assert (res1["start_line"], res1["end_line"], res1["total_lines"]) == (1, 200, 450)
    assert res1["text"] == "\n".join(f"{i}: line {i}" for i in range(1, 201))
    assert "text_truncated" not in res1
    assert docs1 == {"doc.md"} and cites1 == [] and cards1 == []

    res2, _, _, _ = A.run_tool(
        "read_doc", {"doc_id": "doc.md", "start_line": res1["end_line"] + 1}, world, None, window_cap=5)
    assert (res2["start_line"], res2["end_line"]) == (201, 400)

    res3, _, _, _ = A.run_tool(
        "read_doc", {"doc_id": "doc.md", "start_line": res2["end_line"] + 1}, world, None, window_cap=5)
    assert (res3["start_line"], res3["end_line"]) == (401, 450)   # 最終ページは総行数で打ち切る（50行のみ）


def test_run_tool_read_doc_page_size_floor_is_200_lines_regardless_of_small_window_cap(monkeypatch, tmp_path):
    """M-3: window_cap（省略時は READ_WINDOW）が200未満でも、1ページの幅は最低200行
    （read_around の `max(200, window_cap or READ_WINDOW)` と同じ流儀）。"""
    world = "read-doc-floor-world"
    _isolate_world_kb(monkeypatch, tmp_path, world,
                      {"big.md": "\n".join(f"line {i}" for i in range(1, 301))})   # 300行
    for window_cap in (5, 40, 100, 199):
        res, _, _, _ = A.run_tool("read_doc", {"doc_id": "big.md"}, world, None, window_cap=window_cap)
        assert "error" not in res, res
        assert (res["start_line"], res["end_line"], res["total_lines"]) == (1, 200, 300), window_cap


def test_run_tool_read_doc_page_size_scales_above_floor_with_window_cap(monkeypatch, tmp_path):
    """window_cap（調べる深さの倍率適用後の実効値）が200を超えたら、その値までページ幅が伸びる
    （`test_run_tool_read_around_default_window_scales_with_window_cap` と同じ理由）。"""
    world = "read-doc-scale-world"
    _isolate_world_kb(monkeypatch, tmp_path, world,
                      {"big.md": "\n".join(f"line {i}" for i in range(1, 501))})   # 500行
    for window_cap, expected_end in ((250, 250), (300, 300), (400, 400)):
        res, _, _, _ = A.run_tool("read_doc", {"doc_id": "big.md"}, world, None, window_cap=window_cap)
        assert "error" not in res, res
        assert (res["start_line"], res["end_line"], res["total_lines"]) == (1, expected_end, 500)


# ---- H-1: バイト予算の累積とページング契約（無言欠落の是正） ----

def test_run_tool_read_doc_byte_budget_stops_before_page_end_and_reports_actual_end_line(monkeypatch, tmp_path):
    """H-1: ページ幅どおりに組んでから一括クリップすると、`end_line`（「ここまで読んだ」の申告）と
    実際の `text` が食い違う（無言の欠落）。長い行（4KB級のパイプ表行を想定）×ページ境界で、
    バイト予算（TOOL_RESULT_MAX_BYTES 既定64KiB）を超える直前の行で止め、その行を実際の
    end_line にすることを固定する。"""
    world = "read-doc-longline-world"
    # BUDGET-1（§3.4）でコード既定が 262144（256KiB）へ引き上げられたため、旧既定 64KiB を
    # 明示的に固定してテストの意図（境界での打切り）を保つ。
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 65536)
    long_line = "x" * 4000   # 4KB級（パイプ表の1行を想定）
    total_lines = 30         # 30 * (4000+数バイト) は64KiBを優に超える
    _isolate_world_kb(monkeypatch, tmp_path, world,
                      {"big.md": "\n".join(long_line for _ in range(total_lines))})
    res, docs, _, _ = A.run_tool("read_doc", {"doc_id": "big.md"}, world, None, window_cap=200)
    assert "error" not in res, res
    assert res["total_lines"] == total_lines
    assert res["text_truncated"] is True
    assert res["end_line"] < total_lines   # ページ幅（200・total でクランプ）まで伸びていない
    # text に実際に入っている内容が end_line の申告と厳密に一致する（欠落した行の断片が残らない）。
    expected = "\n".join(f"{i}: {long_line}" for i in range(1, res["end_line"] + 1))
    assert res["text"] == expected
    assert len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES
    assert "big.md" in docs


def test_run_tool_read_doc_single_huge_line_is_clipped_with_text_truncated(monkeypatch, tmp_path):
    """H-1: 1行目単独でバイト予算を超える場合だけ、その1行をクリップして返す
    （end_line=1・text_truncated=True）。"""
    world = "read-doc-hugeline-world"
    # BUDGET-1（§3.4）でコード既定が 262144（256KiB）へ引き上げられたため、旧既定 64KiB を
    # 明示的に固定してテストの意図（単一行でも予算超過を検知する）を保つ。
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 65536)
    huge_line = "A" * 200_000
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": huge_line})
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "big.md"}, world, None)
    assert "error" not in res, res
    assert (res["start_line"], res["end_line"]) == (1, 1)
    assert res["text_truncated"] is True
    assert len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES


def test_run_tool_read_doc_small_file_has_no_truncation_flags(monkeypatch, tmp_path):
    """通常サイズの文書では text_truncated/file_truncated のいずれも立たない（キー自体が無い・
    `degrade_reason` と同じ「理由が無ければキーを作らない」流儀）。"""
    world = "read-doc-normal-flags-world"
    _isolate_world_kb(monkeypatch, tmp_path, world,
                      {"doc.md": "\n".join(f"line {i}" for i in range(1, 11))})
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "doc.md"}, world, None)
    assert "text_truncated" not in res
    assert "file_truncated" not in res


# ---- L-1: 8MiB cap 到達時の file_truncated ----

def test_run_tool_read_doc_file_cap_hit_sets_file_truncated(monkeypatch, tmp_path):
    """L-1: ファイル読み込みが `_READ_AROUND_FILE_CAP_BYTES` に達したら file_truncated:true を
    付与し、`total_lines`（「全N行」の申告）が実ファイルの続きを見落としている可能性を明示する
    （テストでは cap を小さい値に差し替えて到達を再現する）。"""
    world = "read-doc-filecap-world"
    monkeypatch.setattr(A, "_READ_AROUND_FILE_CAP_BYTES", 50)
    content = "\n".join(f"line {i}" for i in range(1, 21))   # 50バイトを優に超える
    assert len(content.encode("utf-8")) > 50
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "doc.md"}, world, None)
    assert "error" not in res, res
    assert res["file_truncated"] is True
    assert res["total_lines"] < 20   # cap で打ち切られた分、実ファイルの全行数より少ない


# ---- ripgrep_search の file_truncated 伝播（探す経路が黙って打ち切りを取りこぼさない） ----

def test_run_tool_ripgrep_search_reports_file_truncated_on_capped_hit(monkeypatch, tmp_path):
    """`_GREP_FILE_CAP_BYTES` を小さくして打切りを再現すると、ripgrep_search のツール結果
    （LLM への `hits`）に `file_truncated: true` が載る（読む経路の `file_truncated` と同じ語彙）。"""
    world = "ripgrep-filecap-world"
    line1 = "NEEDLE line one\n"
    filler = "x" * 200 + "\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.txt": line1 + filler})
    monkeypatch.setattr(A.grep_tool, "_GREP_FILE_CAP_BYTES", len(line1.encode("utf-8")) + 10)

    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "NEEDLE"}, world, None)
    assert "error" not in res, res
    assert len(res["hits"]) == 1
    assert res["hits"][0]["file_truncated"] is True


def test_run_tool_ripgrep_search_normal_hit_has_no_file_truncated_key(monkeypatch, tmp_path):
    """打切りが起きていない通常のヒットには `file_truncated` キー自体が無い（加算的変更＝
    既存の消費者が壊れない）。"""
    world = "ripgrep-normal-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": "# 見出し\n本文中に NEEDLE を含む一行\n"})

    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "NEEDLE"}, world, None)
    assert "error" not in res, res
    assert len(res["hits"]) == 1
    assert "file_truncated" not in res["hits"][0]
    assert set(res["hits"][0].keys()) == {"doc_id", "line", "text"}


def test_run_tool_read_doc_start_line_beyond_total_is_range_error(monkeypatch, tmp_path):
    world = "read-doc-range-world"
    _isolate_world_kb(monkeypatch, tmp_path, world,
                      {"doc.md": "\n".join(f"line {i}" for i in range(1, 26))})   # 25行
    res, docs, cites, cards = A.run_tool(
        "read_doc", {"doc_id": "doc.md", "start_line": 26}, world, None, window_cap=5)
    assert "error" in res
    assert docs == set() and cites == [] and cards == []   # 失敗した読み取りは出典に載せない


def test_run_tool_read_doc_empty_file_returns_zero_total_without_error(monkeypatch, tmp_path):
    """空文書は range 外エラーにしない（総0行・0〜0行が正しい結果）。"""
    world = "read-doc-empty-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"empty.md": ""})
    res, docs, _, _ = A.run_tool("read_doc", {"doc_id": "empty.md"}, world, None)
    assert "error" not in res, res
    assert (res["start_line"], res["end_line"], res["total_lines"], res["text"]) == (1, 0, 0, "")
    assert "empty.md" in docs


def test_run_tool_read_doc_negative_start_line_clamped_to_one(monkeypatch, tmp_path):
    """負の start_line をそのまま `lines[start-1:...]` に使うと Python の負インデックスで末尾から
    読んでしまう——1未満は1へ丸める。"""
    world = "read-doc-negative-world"
    _isolate_world_kb(monkeypatch, tmp_path, world,
                      {"doc.md": "\n".join(f"line {i}" for i in range(1, 11))})
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "doc.md", "start_line": -5}, world, None, window_cap=3)
    assert res["start_line"] == 1
    assert res["text"].splitlines()[0] == "1: line 1"


def test_run_tool_read_doc_invalid_start_line_type_is_error():
    res, docs, cites, cards = A.run_tool("read_doc", {"doc_id": "x.md", "start_line": "abc"}, "v1", None)
    assert "error" in res
    assert docs == set() and cites == [] and cards == []


def test_run_tool_read_doc_redacts_secrets(monkeypatch, tmp_path):
    world = "read-doc-secret-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": "api_key=sk-ABCDEFGHIJKLMNOP1234"})
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "doc.md"}, world, None)
    assert "[REDACTED]" in res["text"]
    assert "ABCDEFGHIJKLMNOP1234" not in res["text"]


def test_run_tool_read_doc_rejects_out_of_scope():
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "4期/04_運用/障害記録.md"}, "v1", ["5期"])
    assert "error" in res


def test_run_tool_read_doc_rejects_doc_outside_layer():
    """§8 裁定論点2: open ツール（read_doc）は層外の doc_id を scope 外と同型で拒否する
    （`test_run_tool_read_around_rejects_doc_outside_layer` と同じ規則）。"""
    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="code")
    code_doc_id = res["hits"][0]["doc_id"]
    r_reject, _, _, _ = A.run_tool("read_doc", {"doc_id": code_doc_id}, "v1", None, layer="docs")
    assert "error" in r_reject
    r_ok, docs_ok, _, _ = A.run_tool("read_doc", {"doc_id": code_doc_id}, "v1", None, layer="code")
    assert "error" not in r_ok and code_doc_id in docs_ok


def test_run_tool_doc_outline_normal_returns_headings_with_line_numbers(monkeypatch, tmp_path):
    world = "outline-world"
    content = "intro\n# 見出し1\n本文\n## 見出し2\n本文2\n### 見出し3\n#### 深すぎる見出し\n末尾"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, docs, cites, cards = A.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert "error" not in res, res
    assert res["total_lines"] == 8
    assert res["count"] == 3            # レベル4（#### 深すぎる見出し）は outline の対象外
    assert res["truncated"] is False
    assert [(h["line"], h["level"], h["title"]) for h in res["headings"]] == [
        (2, 1, "見出し1"), (4, 2, "見出し2"), (6, 3, "見出し3")]
    assert docs == {"doc.md"} and cites == [] and cards == []


def test_run_tool_doc_outline_no_headings_returns_empty_with_total_lines(monkeypatch, tmp_path):
    """見出しが無い文書は「見出しなし・総行数」（headings 空リスト＋total_lines）を返す。"""
    world = "outline-noheading-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": "line1\nline2\nline3"})
    res, _, _, _ = A.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert res == {"doc_id": "doc.md", "total_lines": 3, "count": 0, "headings": [], "truncated": False}


def test_run_tool_doc_outline_empty_file(monkeypatch, tmp_path):
    world = "outline-empty-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"empty.md": ""})
    res, _, _, _ = A.run_tool("doc_outline", {"doc_id": "empty.md"}, world, None)
    assert res == {"doc_id": "empty.md", "total_lines": 0, "count": 0, "headings": [], "truncated": False}


def test_run_tool_doc_outline_truncates_at_cap(monkeypatch, tmp_path):
    world = "outline-truncate-world"
    content = "\n".join(f"# h{i}" for i in range(250))   # _OUTLINE_MAX_HEADINGS(200) を超える
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, _, _, _ = A.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert res["count"] == 250                    # 打ち切り前の総件数（list_docs/glob_search と同じ流儀）
    assert len(res["headings"]) == A._OUTLINE_MAX_HEADINGS
    assert res["truncated"] is True


def test_run_tool_doc_outline_truncates_by_byte_budget_before_count_cap(monkeypatch, tmp_path):
    """M-2: 見出し件数が上限（_OUTLINE_MAX_HEADINGS=200）未満でも、タイトルの累積 UTF-8
    バイト数が TOOL_RESULT_MAX_BYTES を超えたら打ち切る（長い CJK タイトル×多数の見出しで
    1結果が既定64KiBを超えるのを防ぐ・件数だけでは足りない）。"""
    world = "outline-bytebudget-world"
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 1000)
    content = "\n".join(f"# 長い見出しタイトルの例その{i}あいうえおかきくけこさしすせそ" for i in range(1, 21))
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, _, _, _ = A.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert "error" not in res, res
    assert res["count"] == 20             # 打ち切り前の総見出し数はそのまま（件数上限は無関係）
    assert 0 < len(res["headings"]) < 20  # 返す件数はバイト予算で減る
    assert res["truncated"] is True
    total_title_bytes = sum(len(h["title"].encode("utf-8")) for h in res["headings"])
    assert total_title_bytes <= 1000


def test_run_tool_doc_outline_redacts_secrets_in_title(monkeypatch, tmp_path):
    world = "outline-secret-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": "## config api_key=sk-ABCDEFGHIJKLMNOP1234"})
    res, _, _, _ = A.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert "[REDACTED]" in res["headings"][0]["title"]
    assert "ABCDEFGHIJKLMNOP1234" not in res["headings"][0]["title"]


def test_run_tool_doc_outline_file_cap_hit_sets_file_truncated(monkeypatch, tmp_path):
    """L-1: doc_outline も read_doc と同じ `_open_doc_stream` 経由なので、8MiB cap 到達時は
    同じく file_truncated:true を付与する（見出し一覧が文書全体の見出しでない可能性の明示）。"""
    world = "outline-filecap-world"
    monkeypatch.setattr(A, "_READ_AROUND_FILE_CAP_BYTES", 50)
    content = "\n".join(f"# h{i}" for i in range(1, 21))
    assert len(content.encode("utf-8")) > 50
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.md": content})
    res, _, _, _ = A.run_tool("doc_outline", {"doc_id": "doc.md"}, world, None)
    assert "error" not in res, res
    assert res["file_truncated"] is True


def test_run_tool_doc_outline_rejects_out_of_scope():
    res, _, _, _ = A.run_tool("doc_outline", {"doc_id": "4期/04_運用/障害記録.md"}, "v1", ["5期"])
    assert "error" in res


def test_run_tool_doc_outline_rejects_doc_outside_layer():
    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None, layer="code")
    code_doc_id = res["hits"][0]["doc_id"]
    r_reject, _, _, _ = A.run_tool("doc_outline", {"doc_id": code_doc_id}, "v1", None, layer="docs")
    assert "error" in r_reject
    r_ok, docs_ok, _, _ = A.run_tool("doc_outline", {"doc_id": code_doc_id}, "v1", None, layer="code")
    assert "error" not in r_ok and code_doc_id in docs_ok


def test_run_tool_unknown_tool_still_rejected_with_read_doc_and_doc_outline_registered():
    """新規ツール追加後も、未知ツール名は引き続き error（fallback 分岐の回帰確認）。"""
    res, docs, cites, cards = A.run_tool("bogus_tool", {}, "v1", None)
    assert "error" in res and docs == set() and cites == [] and cards == []


# ---- 思考の流れ（`_tool_node`/`_tool_node_sub`/`_hit_summary_node`/`_hit_summary_node_sub`）----

def test_tool_node_read_doc_includes_doc_id():
    node = A._tool_node("read_doc", {"doc_id": "設計/資料.md", "start_line": 10})
    assert node["label"] == "文書を通読"
    assert "設計/資料.md" in node["detail"]


def test_tool_node_doc_outline_includes_doc_id():
    node = A._tool_node("doc_outline", {"doc_id": "設計/資料.md"})
    assert node["label"] == "見出し構造を確認"
    assert "設計/資料.md" in node["detail"]


def test_tool_node_sub_read_doc_and_doc_outline_are_fixed_wording():
    """secRV MED-2: サブ経路のツールノードは doc_id を一切含まない固定文言。"""
    node = A._tool_node_sub("read_doc")
    assert node["label"] == "文書を通読" and "SENTINEL" not in node["detail"]
    node2 = A._tool_node_sub("doc_outline")
    assert node2["label"] == "見出し構造を確認" and "SENTINEL" not in node2["detail"]


def test_tool_hit_count_read_doc_counts_lines_returned_this_call():
    assert A._tool_hit_count("read_doc", {"start_line": 1, "end_line": 40, "total_lines": 120}) == 40
    assert A._tool_hit_count("read_doc", {"start_line": 41, "end_line": 41, "total_lines": 120}) == 1


def test_tool_hit_count_doc_outline_uses_count_field():
    assert A._tool_hit_count("doc_outline", {"count": 7, "headings": [], "total_lines": 50}) == 7


def test_tool_hit_count_new_tools_none_on_error():
    assert A._tool_hit_count("read_doc", {"error": "boom"}) is None
    assert A._tool_hit_count("doc_outline", {"error": "boom"}) is None


def test_hit_summary_node_read_doc_includes_doc_and_range():
    node = A._hit_summary_node("read_doc", {"doc_id": "設計/資料.md"},
                               {"doc_id": "設計/資料.md", "start_line": 1, "end_line": 40, "total_lines": 120})
    assert "設計/資料.md" in node["detail"]
    assert "1〜40行を読了" in node["detail"] and "全120行" in node["detail"]


def test_hit_summary_node_doc_outline_includes_doc_and_count():
    node = A._hit_summary_node("doc_outline", {"doc_id": "設計/資料.md"},
                               {"doc_id": "設計/資料.md", "total_lines": 10, "count": 3,
                                "headings": [{}] * 3, "truncated": False})
    assert "設計/資料.md" in node["detail"] and "見出し3件" in node["detail"]


def test_hit_summary_node_none_on_error_for_new_tools():
    assert A._hit_summary_node("read_doc", {"doc_id": "a.md"}, {"error": "boom"}) is None
    assert A._hit_summary_node("doc_outline", {"doc_id": "a.md"}, {"error": "boom"}) is None
    assert A._hit_summary_node_sub("read_doc", {"error": "boom"}) is None
    assert A._hit_summary_node_sub("doc_outline", {"error": "boom"}) is None


def test_hit_summary_node_sub_read_doc_omits_doc_id_fixed_template():
    """secRV MED-2: サブ経路は固定文言＋数値のみ（doc_id は含めない）。"""
    node = A._hit_summary_node_sub(
        "read_doc", {"doc_id": "SENTINEL_DOC.md", "start_line": 1, "end_line": 40, "total_lines": 120})
    assert "SENTINEL_DOC.md" not in node["detail"]
    assert node["detail"] == "1〜40行を読了（全120行）"


def test_hit_summary_node_sub_doc_outline_omits_doc_id_fixed_template():
    node = A._hit_summary_node_sub(
        "doc_outline", {"doc_id": "SENTINEL_DOC.md", "count": 5, "headings": [{}] * 5, "total_lines": 50})
    assert "SENTINEL_DOC.md" not in node["detail"]
    assert node["detail"] == "見出し5件"


# ---- EV-0（拡張設計 §4.4）: read_doc も read_around と同じく「精読済み」に載る ----

def test_openai_style_final_event_tags_read_doc_docs_as_verified():
    real_doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read_doc", "arguments": f'{{"doc_id":"{real_doc}"}}'}}]}}]},
        {"choices": [{"message": {"content": "確認しました。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?",
                                                 "v1", None) if "final" in ev)
        assert final["verified_docs"] == {real_doc}, final["verified_docs"]
        assert real_doc in final["docs"]
    finally:
        A._post = orig


def test_openai_style_doc_outline_hit_is_not_verified():
    """doc_outline は構造の当たり付けのみ（本文精読ではない）——read_around/read_doc と違い
    `verified_docs` には入らない（出典候補 `docs` には従来どおり残る）。"""
    real_doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "doc_outline", "arguments": f'{{"doc_id":"{real_doc}"}}'}}]}}]},
        {"choices": [{"message": {"content": "確認しました。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?",
                                                 "v1", None) if "final" in ev)
        assert final["verified_docs"] == set()
        assert real_doc in final["docs"]
    finally:
        A._post = orig


# ===== SC-6e: agentic 経路（_agentic_run）のレンズ必須ツール判定 =====
# 非agentic（chat_service._dispatch）と同じ dispatch_tools_for_lens をここでも通す——
# 以前は agentic 経路がこの判定を一切見ずに直接 _agentic_loop/_sub_agentic_loop へ進んでいた
# （impact/troubleshoot でもグラフ不達/OFF のまま反復ツール検索を試みてしまう非対称）。

def _blocking_gate_ctx(lens, tools_availability, tools_pref=None):
    """`_agentic_loop` が絶対に呼ばれてはいけないことを検証するための Ctx（_GenProvider._agentic_run
    共通）。`tools_availability`/`scope_meta["tools"]` 以外は最小構成。"""
    from sherpa.providers.base import Ctx
    return Ctx(message="質問", world="v1", knowledge=True,
              route=lambda m: {"lens": lens, "reason": "テスト", "input": m},
              dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}},
              make_sources=lambda docs: [],
              scope_meta={"world": "v1", "scope_paths": [], "source": "all", "tools": tools_pref},
              tools_availability=tools_availability)


class _NeverCallAgenticLoop:
    """`_agentic_loop`/`_sub_agentic_loop` が呼ばれたら即座に検出できる mixin
    （呼ばれずに honest-failure envelope だけが返るはず）。"""
    label, model, provider_id = "T", "m", "openai"

    def _agentic_loop(self, ctx):
        raise AssertionError("blocked のはずの lens で _agentic_loop が呼ばれた")


def test_agentic_run_impact_blocked_when_graph_unavailable():
    """impact はグラフ必須（`_DISPATCH_REQUIRES_GRAPH`）——不達なら `_agentic_loop` を一切呼ばず
    honest-failure envelope（`tools_blocked_env`）を返す。"""
    from sherpa.providers.base import _GenProvider

    class _P(_NeverCallAgenticLoop, _GenProvider):
        pass

    ctx = _blocking_gate_ctx("impact", {"grep": True, "fulltext": True, "graph": False})
    events = list(_P().run(ctx))
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["lens"] == "impact"
    assert env["data"] == {}
    assert env["summary"]["total"] == 0
    assert "グラフ" in env["headline"]


def test_agentic_run_troubleshoot_blocked_when_graph_off_via_pref():
    """troubleshoot もグラフ必須——実接続は可用でも、会話の検索経路トグルで明示 OFF にしていれば
    同じく blocked（可用性とユーザー希望の AND・`effective_tools_pref` 参照）。"""
    from sherpa.providers.base import _GenProvider

    class _P(_NeverCallAgenticLoop, _GenProvider):
        pass

    ctx = _blocking_gate_ctx("troubleshoot", {"grep": True, "fulltext": True, "graph": True},
                            tools_pref={"graph": False})
    events = list(_P().run(ctx))
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["lens"] == "troubleshoot"
    assert env["data"] == {}


def test_agentic_run_qa_blocked_when_grep_and_fulltext_both_unavailable():
    """qa/author はグラフ必須ではなく grep か全文のどちらか一方で足りる——両方 OFF/不達のときだけ
    blocked（グラフだけが available でも qa は救われない・§3.6 の非agentic 判定と同じ規則）。"""
    from sherpa.providers.base import _GenProvider

    class _P(_NeverCallAgenticLoop, _GenProvider):
        pass

    ctx = _blocking_gate_ctx("qa", {"grep": False, "fulltext": False, "graph": True})
    events = list(_P().run(ctx))
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["lens"] == "qa"
    assert env["data"] == {}


def test_agentic_run_qa_blocked_when_unresolved_availability_and_explicit_grep_off(monkeypatch):
    """`ctx.tools_availability=None`（provider 直呼び出し・通常経路は必ず snapshot を渡す）でも、
    明示的に grep を OFF にし、かつ実際に fulltext が不達なら qa は blocked になる——解決せず
    `None=全て利用可能` の楽観的 gate をそのまま通すと、この組み合わせで本来 blocked のはずが
    素通りしてしまう（`_agentic_run` が全 agentic レンズで snapshot を解決するようになった
    ことの直接確認・qa/author を対象外にしていた旧実装ではこの回帰を検出できない）。"""
    from sherpa.providers.base import _GenProvider

    monkeypatch.setattr(A.es_index, "available", lambda: False)   # fulltext は実際に不達
    monkeypatch.setattr(A, "_graph_available", lambda: True)      # qa の gate には無関係

    class _P(_NeverCallAgenticLoop, _GenProvider):
        pass

    ctx = _blocking_gate_ctx("qa", None, tools_pref={"grep": False})
    events = list(_P().run(ctx))
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["lens"] == "qa"
    assert env["data"] == {}


def test_agentic_run_qa_not_blocked_when_grep_alone_available():
    """qa は grep だけが available なら blocked にならず、通常どおり `_agentic_loop` を呼ぶ
    （over-block しないことの陰性対照）。"""
    from sherpa.providers.base import _GenProvider

    seen = []

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"

        def _agentic_loop(self, ctx):
            seen.append("agentic")
            # has_structural_evidence=True で根拠ゲート（EXT-2）を素直に通す——空のままだと
            # `_agentic_run` が「evidence below threshold」で例外を投げ、対象外の provider
            # フォールバック（`ctx.dispatch` 経由の単発 grep）へ縮退してしまい、この陰性対照が
            # 検証したい「blocked にならず agentic ループが正常完走する」ことを確認できない。
            yield {"final": "回答", "docs": set(), "searched": True, "cites": [], "cards": [],
                  "has_structural_evidence": True}

    ctx = _blocking_gate_ctx("qa", {"grep": True, "fulltext": False, "graph": False})
    events = list(_P().run(ctx))
    assert seen == ["agentic"]
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["headline"] == "回答"   # 根拠ゲートで例外→フォールバックへ縮退していないことの確認


def test_agentic_run_impact_not_blocked_when_graph_available():
    """impact はグラフが available なら blocked にならない（over-block しないことの陰性対照）。"""
    from sherpa.providers.base import _GenProvider

    seen = []

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"

        def _agentic_loop(self, ctx):
            seen.append("agentic")
            # has_structural_evidence=True で根拠ゲート（EXT-2）を素直に通す（上の
            # test_agentic_run_qa_not_blocked_when_grep_alone_available と同じ理由）。
            yield {"final": "回答", "docs": set(), "searched": True, "cites": [], "cards": [],
                  "has_structural_evidence": True}

    ctx = _blocking_gate_ctx("impact", {"grep": True, "fulltext": True, "graph": True})
    events = list(_P().run(ctx))
    assert seen == ["agentic"]
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["headline"] == "回答"   # 根拠ゲートで例外→フォールバックへ縮退していないことの確認


def test_openai_requests_omit_temperature():
    """gpt-5.5 系は temperature の既定値(1)以外を拒否する（400 unsupported_value・2026-08-15 実測）。

    送るとツールループが丸ごと失敗し、影響調査も仕様問い合わせも「根拠なし」で終わっていた。
    OpenAI 宛ての本文には temperature を載せない（Ollama 側の options.temperature は据え置き）。
    """
    import json

    from sherpa import agentic_search as A
    from sherpa import llm

    sent = {}

    def _fake_post(url, headers, body, timeout=None):
        sent["body"] = body
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    orig = A._post
    A._post = _fake_post
    try:
        list(A.openai_style(llm.OPENAI_CHAT_URL, {}, "gpt-5.5", "sys", "質問", "w", None, max_turns=1))
    finally:
        A._post = orig
    assert "temperature" not in sent["body"], f"OpenAI へ temperature を送っている: {sent['body'].keys()}"

    # 単発ストリーミング（_stream）も同様。
    from sherpa.providers.openai import OpenAIProvider
    captured = {}

    class _P(OpenAIProvider):
        pass

    p = _P("sk-dummy", "gpt-5.5")
    import urllib.request
    orig_req = urllib.request.Request

    def _fake_request(url, data=None, headers=None):
        captured["body"] = json.loads(data.decode())
        raise RuntimeError("stop-before-network")

    urllib.request.Request = _fake_request
    try:
        list(p._stream("prompt"))
    except RuntimeError:
        pass
    finally:
        urllib.request.Request = orig_req
    assert "temperature" not in captured["body"], f"_stream が temperature を送っている: {captured['body'].keys()}"


# ==== EXT-2（拡張設計 §4.3）: 機械検証（verify_citation） ====

_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 実在ファイル・1行目 "# 障害記録"


def test_verify_citation_doc_missing_for_nonexistent_doc_id():
    v = A.verify_citation({"doc_id": "no-such-file.md", "span": [1, 1], "quote": "x"}, "v1")
    assert v == {"exists": False, "method": "doc_missing"}


def test_verify_citation_exists_no_span_when_span_absent():
    v = A.verify_citation({"doc_id": _REAL_DOC, "quote": "x"}, "v1")
    assert v == {"exists": True, "method": "exists_no_span"}


def test_verify_citation_span_verified_when_quote_matches_real_content():
    v = A.verify_citation({"doc_id": _REAL_DOC, "span": [1, 1], "quote": "# 障害記録"}, "v1")
    assert v == {"exists": True, "method": "span_verified"}


def test_verify_citation_span_unmatched_when_quote_diverges_but_doc_exists():
    v = A.verify_citation({"doc_id": _REAL_DOC, "span": [1, 1], "quote": "存在しない引用文言"}, "v1")
    assert v == {"exists": True, "method": "span_unmatched"}


def test_verify_citation_rejects_traversal_and_out_of_range_span():
    assert A.verify_citation({"doc_id": "../etc/passwd", "span": [1, 1], "quote": "x"}, "v1") \
        == {"exists": False, "method": "doc_missing"}
    # span が全ファイル行数を超える＝不一致（存在チェック自体は通る）。
    v = A.verify_citation({"doc_id": _REAL_DOC, "span": [999999, 999999], "quote": "x"}, "v1")
    assert v == {"exists": True, "method": "span_unmatched"}


# ==== EXT-2/EV-0: read_around を通した doc_id だけが "verified_docs" に載る ====

def test_openai_style_final_event_tags_read_around_docs_as_verified():
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "read_around",
             "arguments": f'{{"doc_id":"{_REAL_DOC}","line":1}}'}}]}}]},
        {"choices": [{"message": {"content": "確認しました。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?",
                                                 "v1", None) if "final" in ev)
        assert final["verified_docs"] == {_REAL_DOC}, final["verified_docs"]
        # 最終応答に finish_reason が無い（自然完了 allowlist に無い＝非自然完了）ため
        # "unknown"（原因不明・自然終了とは偽らない）——本テストの主眼は verified_docs のため
        # stop_reason はここでは詳細検証しない（細分化は test_incomplete_stop_reason_* が担当）。
        assert final["stop_reason"] == "unknown"
        assert _REAL_DOC in final["docs"]   # grep ヒットも従来どおり docs（＝出典候補）には残る
    finally:
        A._post = orig


def test_openai_style_grep_only_hit_is_not_verified():
    """grep ヒットのみ（read_around を呼んでいない）は verified_docs に入らない（EV-0 の「参考」相当）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?",
                                                 "v1", None) if "final" in ev)
        assert final["verified_docs"] == set()
        assert final["docs"]   # 出典候補としては残る（EV-0 は除外しない＝recall 不変）
    finally:
        A._post = orig


# ==== EXT-3（拡張設計 §3）: 評価フェーズ（Observation → Evaluation → Next Action） ====

def _eval_tool_call(call_id: str, status: str, next_action: str, reason: str = "理由") -> dict:
    args = json.dumps({"status": status, "reason": reason, "next_action": next_action}, ensure_ascii=False)
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": call_id, "function": {"name": "submit_evaluation", "arguments": args}}]}}]}


def test_openai_style_depth_light_never_triggers_evaluation_even_with_cycle_boundary(monkeypatch):
    """既定 depth="light" は Research Cycle 境界（毎ターン）でも評価フェーズ（submit_evaluation 呼び
    出し）を一切発動しない（既存呼び出し元は誰も depth を渡さない＝byte-identical の根拠）。

    Committed Evidence 化ゲート（機械検証）自体は depth に関わらず常時動くが、`evidence_committed`
    ノードの発行は根拠ゲート通過後に `providers/base.py` が行う（本関数はここでは検証しない）——
    ここで固定するのは「評価フェーズ由来のノード（evaluation_*/replan_requested/
    finalization_started）が出ないこと」に限る。
    """
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
        final = next(ev for ev in events if "final" in ev)
        # 最終応答に finish_reason が無いため "unknown"（本テストの主眼は評価フェーズ非発動の
        # 確認であり stop_reason の細分化ではない）。
        assert final["stop_reason"] == "unknown"
        assert not seq   # ちょうど2コールだけ消費（評価フェーズ用の3コール目が無い）
        node_types = {ev["node"].get("event_type") for ev in events if "node" in ev}
        assert not (node_types & {"evaluation_completed", "replan_requested", "finalization_started"})
    finally:
        A._post = orig


def test_openai_style_evaluation_sufficient_commits_evidence_and_ends_early(monkeypatch):
    """`evidence_committed` ノード自体は `providers/base.py` が根拠ゲート通過後に発行する
    （test_provider_agentic_run_emits_evidence_committed_node_after_gate 参照）。ここでは
    openai_style 単体の契約（評価ノード・stop_reason・citation）だけを検証する。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _eval_tool_call("e1", "sufficient", "commit_evidence", "十分な根拠"),
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
        assert final["evaluation_next_action"] == "commit_evidence"
        node_types = [ev["node"].get("event_type") for ev in events if "node" in ev]
        assert "evaluation_completed" in node_types
        assert not seq   # 評価→最終合成まで3コールすべて消費
    finally:
        A._post = orig


def test_openai_style_evaluation_blocked_ends_with_finalization_event(monkeypatch):
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _eval_tool_call("e1", "blocked", "stop", "行き詰まり"),
        {"choices": [{"message": {"content": "確認できた範囲で回答します。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="deep"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_blocked"
        node_types = [ev["node"].get("event_type") for ev in events if "node" in ev]
        assert "finalization_started" in node_types
    finally:
        A._post = orig


def test_openai_style_evaluation_conflicting_emits_replan_and_continues(monkeypatch):
    """conflicting は同一 Cycle 内で継続する縮退。no-tool 終了も評価境界として強制されるため、
    次に模型が tool_calls 無しで止まってももう一度評価を挟む（ここでは sufficient で確定させる）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _eval_tool_call("e1", "conflicting", "delegate_more", "矛盾を検知"),
        {"choices": [{"message": {"content": "調べ直した結果はこうです。"}}]},
        _eval_tool_call("e2", "sufficient", "commit_evidence", "十分"),
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
        node_types = [ev["node"].get("event_type") for ev in events if "node" in ev]
        assert "replan_requested" in node_types
        assert not seq
    finally:
        A._post = orig


def test_openai_style_evaluation_insufficient_is_silent_and_continues(monkeypatch):
    """§3.2 の表どおり insufficient はイベントを出さず同一 Research Cycle 内で継続する。no-tool
    終了も評価境界として強制されるため、模型が次に tool_calls 無しで止まってももう一度評価を挟む
    （ここでは sufficient で確定させる）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _eval_tool_call("e1", "insufficient", "continue_search", "まだ不足"),
        {"choices": [{"message": {"content": "追加で確認しました。"}}]},
        _eval_tool_call("e2", "sufficient", "commit_evidence", "十分"),
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
        node_types = [ev["node"].get("event_type") for ev in events if "node" in ev]
        assert not any(t in node_types for t in
                       ("replan_requested", "finalization_started"))
        assert not seq
    finally:
        A._post = orig


def test_openai_style_no_tool_call_exit_forces_evaluation_even_before_cycle_boundary(monkeypatch):
    """`tool_calls==0` による即終了は、既定の Research Cycle 境界（3ターン）より前でも Medium/Deep
    なら評価を回避できない（1ターン目でモデルが止まっても評価が必ず挟まる）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 3)   # 境界はまだ先（3ターン目）だが no-tool で強制
    seq = [
        {"choices": [{"message": {"content": "早期の回答です。"}}]},
        _eval_tool_call("e1", "sufficient", "commit_evidence", "十分"),
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
        assert not seq   # 評価コールが実際に発行された（境界前スキップを回避できていない証拠）
    finally:
        A._post = orig


def test_openai_style_evaluation_rejects_wrong_function_name_and_retries_once(monkeypatch):
    """評価応答が `submit_evaluation` 以外の関数を呼んだ場合は拒否し、1回だけ厳格な再ナッジで再試行
    する。再試行が正しければ採用する。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    wrong_call = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "w1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        wrong_call,                                            # 1回目の評価: 関数名不一致で拒否
        _eval_tool_call("e1", "sufficient", "commit_evidence", "十分"),   # 2回目: 正しい応答
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
        assert not seq
    finally:
        A._post = orig


def test_parse_eval_response_rejects_mixed_tool_calls():
    """`submit_evaluation` に加えて他ツールも同時に呼んだ応答（tool_calls が2件以上）は、その唯一の
    関数名が `submit_evaluation` であっても拒否する（存在チェックだけでなく件数チェックを先に行う）。"""
    resp = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "e1", "function": {"name": "submit_evaluation",
         "arguments": '{"status":"sufficient","reason":"x","next_action":"commit_evidence"}'}},
        {"id": "e2", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}},
    ]}}]}
    assert A._parse_eval_response(resp) is None


def test_openai_style_evaluation_rejects_mixed_tool_calls_and_retries_once(monkeypatch):
    """評価応答が `submit_evaluation` と他ツールを同時に呼んだ（tool_calls 2件以上）場合も、単体
    呼び出しの関数名不一致と同様に拒否し、1回だけ厳格な再ナッジで再試行する。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    mixed_call = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "e1", "function": {"name": "submit_evaluation",
         "arguments": '{"status":"sufficient","reason":"x","next_action":"commit_evidence"}'}},
        {"id": "e2", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}},
    ]}}]}
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        mixed_call,                                            # 1回目の評価: 混在で拒否
        _eval_tool_call("e1", "sufficient", "commit_evidence", "十分"),   # 2回目: 正しい応答
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
        assert not seq
    finally:
        A._post = orig


def test_openai_style_evaluation_malformed_json_becomes_blocked_after_one_retry(monkeypatch):
    """不正 JSON・status/next_action 不整合の応答は拒否し、1回再試行しても直らなければ `blocked`
    として stop reason に評価失敗が残る（fail-open で insufficient に倒さない）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    inconsistent_call = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "e1", "function": {"name": "submit_evaluation",
         "arguments": '{"status":"sufficient","reason":"x","next_action":"stop"}'}}]}}]}   # 整合しない組
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        inconsistent_call,
        inconsistent_call,   # 再試行も不整合のまま
        {"choices": [{"message": {"content": "最終回答です。"}}]},   # blocked 後の最終合成
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["stop_reason"] == "evaluation_blocked"
        assert final["evaluation_status"] == "blocked"
        assert not seq
    finally:
        A._post = orig


def test_openai_style_evaluation_call_failure_retries_once_then_blocked(monkeypatch):
    """評価呼び出し自体が2回とも通信例外で失敗したら `blocked`（stop reason に評価失敗が残る）へ倒す
    （fail-open で insufficient に倒すと評価を強制する意味が失われるため採らない）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(body)
        if len(calls) in (2, 3):
            raise RuntimeError("network down")
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}
        return {"choices": [{"message": {"content": "最終回答です。"}, "finish_reason": "stop"}]}

    orig = A._post
    A._post = fake_post
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                     depth="medium"))
        final = next(ev for ev in events if "final" in ev)
        assert final["final"] == "最終回答です。"
        assert final["stop_reason"] == "evaluation_blocked"
        # tool turn + 評価2回（両方失敗） + blocked 後の最終合成 + 帰属呼び出し1回（citation が
        # 実在すれば digest が非空になり発火する・fake の calls 切れは attribute_openai_style 側で
        # 安全に空集合へ縮退する）。
        assert len(calls) == 5
    finally:
        A._post = orig


# ==== stop_reason の細分化（出力上限打ち切り／内容フィルタ打ち切りを "no_tool_calls" と区別する） ====
# 正典（拡張設計 §4.4）の EV-0 自然完了 allowlist は帰属呼び出しの可否だけでなく、UI の
# 「終了理由」（stop_reason）にも同じ判別を反映する——ツール未呼び出しで応答が返っても、
# 実際には出力上限／内容フィルタで打ち切られていたなら「自然終了」（no_tool_calls）と偽らない。

def test_incomplete_stop_reason_maps_known_truncation_and_filter_tokens():
    assert A._incomplete_stop_reason("length", truncated=A._OPENAI_STYLE_TRUNCATED,
                                     content_filtered=A._OPENAI_STYLE_CONTENT_FILTERED) == "truncated"
    assert A._incomplete_stop_reason("content_filter", truncated=A._OPENAI_STYLE_TRUNCATED,
                                     content_filtered=A._OPENAI_STYLE_CONTENT_FILTERED) == "content_filtered"


def test_incomplete_stop_reason_unknown_or_non_string_normalizes_to_unknown():
    """理由欠落・非文字列（壊れた upstream 応答）・真の未知値（既知のどの集合にも無い将来の新しい
    値を想定・例 'weird_reason'）は "no_tool_calls"（自然終了）へ丸めない——原因不明を自然完了と
    偽ると、実際には出力上限/内容フィルタ等で打ち切られていたケースまで UI に「自然終了」と
    表示してしまう（silent fallback）。新しい断定はせず、既存の「終了理由を確認できませんでした」
    表示に載る専用の値 "unknown" へ正規化する。"""
    for reason in (None, {"unexpected": "shape"}, "weird_reason"):
        result = A._incomplete_stop_reason(reason, truncated=A._OPENAI_STYLE_TRUNCATED,
                                           content_filtered=A._OPENAI_STYLE_CONTENT_FILTERED)
        assert result == "unknown", f"{reason!r} が unknown へ正規化されていない: {result}"
        assert result != "no_tool_calls", (
            f"{reason!r} が自然終了(no_tool_calls)へ丸められている（silent fallback の再発）: {result}")


def test_openai_style_length_finish_reason_sets_truncated_stop_reason():
    """OpenAI/Ollama 方言: ツール未呼び出しで `finish_reason=="length"`（出力上限で打ち切り）は
    従来「自然終了」と同じ `no_tool_calls` へ丸められていたが、`truncated` に分ける。"""
    seq = [
        {"choices": [{"message": {"content": "途中まで書きました"}, "finish_reason": "length"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?",
                                                 "v1", None) if "final" in ev)
        assert final["stop_reason"] == "truncated"
    finally:
        A._post = orig


def test_openai_style_content_filter_finish_reason_sets_content_filtered_stop_reason():
    """OpenAI/Ollama 方言: `finish_reason=="content_filter"` は `content_filtered` に分ける。"""
    seq = [
        {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?",
                                                 "v1", None) if "final" in ev)
        assert final["stop_reason"] == "content_filtered"
    finally:
        A._post = orig


def test_anthropic_style_max_tokens_sets_truncated_stop_reason():
    """Anthropic 方言: ツール未呼び出しで `stop_reason=="max_tokens"` は `truncated` に分ける。"""
    seq = [_AResp([_ABlock("text", "途中まで書きました")], stop_reason="max_tokens")]
    client = _AClient(seq)
    events = list(A.anthropic_style(client, "m", A.SYSTEM, "消費税率は?", "v1", None))
    final = next(ev for ev in events if "final" in ev)
    assert final["stop_reason"] == "truncated"


def test_gemini_max_tokens_finish_reason_sets_truncated_stop_reason(monkeypatch):
    """Gemini 方言: `finishReason=="MAX_TOKENS"` は `truncated` に分ける。"""
    seq = [
        {"candidates": [{"content": {"parts": [{"text": "途中まで書きました"}]},
                        "finishReason": "MAX_TOKENS"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = None
        for ev in A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None):
            if "final" in ev:
                final = ev
        assert final["stop_reason"] == "truncated"
    finally:
        A._post = orig


# ==== RV6是正: 最終本文を実際に生成した呼び出しの finish_reason で stop_reason を再分類する ====
# 初回ドラフト時点で決めた stop_reason（no_tool_calls/evaluation_sufficient/evaluation_blocked/
# turns_exhausted）は、直後の再合成（citation 検証で落ちた場合）や最終合成（turns_exhausted/
# 評価早期終了向けの追加呼び出し）で finish_reason が変わりうることを反映していなかった。

def test_openai_style_resynthesis_after_dropped_citations_reclassifies_truncated_stop_reason(monkeypatch):
    """初回ドラフトは自然完了（finish_reason 無し＝"stop"相当）でも、citation 検証で一部が落ちて
    クリーン再合成が走り、その再合成呼び出しの finish_reason が "length"（出力上限）なら、
    最終的な stop_reason は "no_tool_calls"（自然終了）のままにせず "truncated" へ再分類する
    （**表示する本文を実際に生成した呼び出し**の finish_reason を優先する）。"""
    real_doc = "4期/04_運用/障害記録.md"
    monkeypatch.setattr(A, "run_tool", lambda name, args, world, scope_paths, **kw: (
        {"hits": []}, {real_doc, "ghost.md"},
        [{"doc_id": real_doc, "span": [1, 1], "quote": "実在", "ext": ".md"},
         {"doc_id": "ghost.md", "span": [1, 1], "quote": "存在しない", "ext": ".md"}], []))
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "ghost.md にも記載があります（古い草稿）。"}}]},
        {"choices": [{"message": {"content": "確認できた根拠に基づく回答です（途中"},
                     "finish_reason": "length"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None)
                     if "final" in ev)
        assert [c["doc_id"] for c in final["cites"]] == [real_doc]
        assert final["stop_reason"] == "truncated"
    finally:
        A._post = orig


def test_openai_style_final_synthesis_after_evaluation_sufficient_reclassifies_truncated_stop_reason(monkeypatch):
    """評価フェーズが sufficient と判定して早期終了しても、その後の最終合成呼び出し
    （tools 無し・Committed Evidence を使ってまとめる）の finish_reason が "length" なら、
    stop_reason は "evaluation_sufficient" ではなく "truncated" になる（PART-4 research 経路等・
    Deep/Medium いずれの depth でも同じ最終合成コードパスを通るため同様に露出していた）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _eval_tool_call("e1", "sufficient", "commit_evidence", "十分な根拠"),
        {"choices": [{"message": {"content": "TAX-RATE で管理しています（途中"},
                     "finish_reason": "length"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                                 depth="medium") if "final" in ev)
        assert final["stop_reason"] == "truncated"
        assert final["evaluation_next_action"] == "commit_evidence"   # 評価結果自体は失わない
    finally:
        A._post = orig


def test_openai_style_final_synthesis_natural_completion_keeps_evaluation_stop_reason(monkeypatch):
    """再分類は「打ち切りだと判別できたとき」だけ発生する——最終合成呼び出しが明示的に自然完了
    （`finish_reason="stop"`）でも stop_reason は元の "evaluation_sufficient" のまま
    （回帰防止・既存契約の固定）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _eval_tool_call("e1", "sufficient", "commit_evidence", "十分な根拠"),
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}, "finish_reason": "stop"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                                 depth="medium") if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
    finally:
        A._post = orig


def test_openai_style_final_synthesis_unknown_finish_reason_keeps_evaluation_stop_reason(monkeypatch):
    """再分類は「truncated/content_filtered と判別できたとき」だけ発生する——最終合成呼び出しが
    真に未知の finish_reason（`'weird_reason'`・将来の新しい値を想定）を返しても、内部的には
    "unknown" に正規化されるだけで evaluation_* 等の情報を上書きしない（stop_reason は元の
    "evaluation_sufficient" のまま保持する）。"""
    monkeypatch.setattr(A, "RESEARCH_CYCLE_TURNS", 1)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _eval_tool_call("e1", "sufficient", "commit_evidence", "十分な根拠"),
        {"choices": [{"message": {"content": "TAX-RATE で管理しています。"}, "finish_reason": "weird_reason"}]},
    ]
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        final = next(ev for ev in A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                                 depth="medium") if "final" in ev)
        assert final["stop_reason"] == "evaluation_sufficient"
    finally:
        A._post = orig


def test_stop_reason_vocabulary_matches_render_js_display_table():
    """`stop_reason` の閉じた語彙（`agentic_search.STOP_REASONS`・本モジュールの生成箇所から
    実際に導出される定数集合）が、`web/chat/render.js::STOP_REASON_TOKEN_LABEL`（UI 表示側の
    唯一の対応表）と過不足なく一致することを固定する。新しい stop_reason をサーバ側だけ追加して
    表示側の対応表を更新し忘れる（＝「終了理由を確認できませんでした」に落ちる）、または表示側
    だけ増やしてサーバが実際には出さない値が残る、の両方を防ぐ。`plan_completed`（複数下調べ役の
    計画経路・退役済み `_run_sub_plan` のみが生成）は `STOP_REASONS` にも表示側にも含まれない
    （本モジュールからは到達不能）。
    """
    import re

    render_js = pathlib.Path(__file__).resolve().parents[2] / "web" / "chat" / "render.js"
    src = render_js.read_text(encoding="utf-8")
    m = re.search(r"const STOP_REASON_TOKEN_LABEL = Object\.assign\(Object\.create\(null\), \{(.*?)\}\);",
                 src, re.S)
    assert m, "STOP_REASON_TOKEN_LABEL が render.js に見つからない"
    keys = set(re.findall(r"(\w+):\s*'", m.group(1)))
    assert keys == A.STOP_REASONS, (
        f"render.js の対応表と agentic_search.STOP_REASONS が食い違っている: {keys ^ A.STOP_REASONS}")


def test_hit_summary_node_glob_search_reports_total_count():
    """GLOB-1×TRACE-HITS 調停: glob にも件数ノード（打ち切り前の総件数・パターン付き）。"""
    node = A._hit_summary_node("glob_search", {"pattern": "*.jcl"},
                               {"count": 250, "paths": ["a.jcl"], "truncated": True})
    assert node is not None
    assert node["label"] == "検索結果（ファイル名）"
    assert "「*.jcl」→ 250件" in node["detail"]
    assert node.get("event_type") == "tool_completed"
    sub = A._hit_summary_node_sub("glob_search", {"count": 0, "paths": [], "truncated": False})
    assert sub is not None and "0件ヒットしました" in sub["detail"]


def test_ripgrep_search_tool_result_reports_truncated_docs_with_zero_hits(monkeypatch):
    """ツール結果の `truncated_docs` は **ヒット0件の打切り文書**も LLM へ伝える（検収是正）。

    `file_truncated`（ヒットに付く）だけでは、cap より後ろにしか一致が無い文書が無音になる。
    `degrade_reason` と同じく「理由が無ければキーを作らない」流儀＝打切りが無ければキーは出ない。
    """
    def fake_grep(q, world, **kw):
        td = kw.get("truncated_docs")
        if td is not None:
            td.append("big.xlsx")
        return []

    monkeypatch.setattr(A.grep_tool, "grep_search", fake_grep)
    view, _docs, _cites, _cards = A.run_tool("ripgrep_search", {"query": "X"}, "w", None)
    assert view["hits"] == []
    assert view["truncated_docs"] == ["big.xlsx"]


def test_ripgrep_search_tool_result_omits_truncated_docs_when_none(monkeypatch):
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda q, world, **kw: [])
    view, _docs, _cites, _cards = A.run_tool("ripgrep_search", {"query": "X"}, "w", None)
    assert "truncated_docs" not in view


def test_truncated_docs_is_capped(monkeypatch):
    """件数上限（`_TRUNCATED_DOCS_MAX`）でツール結果のバイト予算を圧迫しない。"""
    def fake_grep(q, world, **kw):
        kw["truncated_docs"].extend(f"doc{i}.xlsx" for i in range(A._TRUNCATED_DOCS_MAX + 5))
        return []

    monkeypatch.setattr(A.grep_tool, "grep_search", fake_grep)
    view, _docs, _cites, _cards = A.run_tool("ripgrep_search", {"query": "X"}, "w", None)
    assert len(view["truncated_docs"]) == A._TRUNCATED_DOCS_MAX


# ===== S2: read 側のストリーミング化（read_around/read_doc/doc_outline） =====
# grep が2026-09にストリーミング走査（`grep_tool._CappedStreamReader`/`_logical_lines`）へ移行し、
# ファイル上限の既定を 64MiB へ引き上げた際、read 側（`_open_doc_stream`/`_stream_doc_lines`
# 経由の read_around/read_doc/doc_outline）は据え置かれ、1回の呼び出しが最大 64MB を一括で
# メモリに載せる懸念が再燃していた（secRV MED-B 型）。以下は read 側も同じリーダーを再利用して
# ストリーミング化したことの直接固定（メモリの非比例・grep とのカウント整合・単一巨大行の
# 安全弁）。

def test_run_tool_read_doc_bounded_memory_for_large_normal_file(monkeypatch, tmp_path):
    """`test_large_file_normal_lines_bounded_memory`（`tests/unit/test_grep_tool.py`）の read_doc
    版——通常の改行を含む大きめファイル（20MB超）でも、read_doc（1ページ目取得）実行中の Python
    側ピーク割当はファイルサイズに比例しない（`total_lines` の申告に全行のカウントは要るが、
    ページ窓の外の行内容は保持しない）。"""
    import tracemalloc

    world = "read-doc-bigfile-world"
    line = "x" * 200 + "\n"
    n = (20 * 1024 * 1024) // len(line) + 10   # 端数切り捨てを見込んで少し多めに
    content = line * n
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": content})
    assert len(content.encode("utf-8")) > 20 * 1024 * 1024

    tracemalloc.start()
    try:
        res, _, _, _ = A.run_tool("read_doc", {"doc_id": "big.md"}, world, None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert "error" not in res, res
    assert res["total_lines"] == n
    assert peak < 5 * 1024 * 1024   # 20MB超のファイルに対しピーク割当は5MB未満（比例しない）


def test_run_tool_read_around_bounded_memory_and_early_exit_for_large_file(monkeypatch, tmp_path):
    """read_around は目的の行が既知のため、窓（`e_target`）に達したらファイル全体を読み切らずに
    打ち切る——20MB超のファイルで先頭付近を read_around しても、ピーク割当はファイルサイズに
    比例しない（旧実装は cap まで一括ロードしていた）。"""
    import tracemalloc

    world = "read-around-bigfile-world"
    line = "x" * 200 + "\n"
    n = (20 * 1024 * 1024) // len(line) + 10
    content = line * n
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": content})
    assert len(content.encode("utf-8")) > 20 * 1024 * 1024

    tracemalloc.start()
    try:
        res, _, _, _ = A.run_tool(
            "read_around", {"doc_id": "big.md", "line": 5, "window": 2}, world, None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert "error" not in res, res
    assert res["text"].splitlines()[0] == f"3: {line.rstrip(chr(10))}"
    assert peak < 2 * 1024 * 1024   # 早期打ち切りにより 20MB 超のファイルでもピーク割当は極小


def test_run_tool_read_doc_single_huge_line_bounded_memory_and_sets_file_truncated(monkeypatch, tmp_path):
    """単一巨大行への安全弁（`_READ_LINE_MAX_BYTES`・grep の `_GREP_LINE_MAX_BYTES` 相当）が read
    側にも効く: 改行が来ないまま30MB続く単一行（cap=64MiB 内）でも、read_doc 実行中のピーク割当は
    非比例で頭打ちになり、`file_truncated: true`（探せていない範囲がある）を申告する
    （`test_single_huge_line_bounded_memory`/`test_line_overflow_reports_truncation_even_without_
    file_cap`＝`tests/unit/test_grep_tool.py` の read 版）。"""
    import tracemalloc

    world = "read-doc-hugesingleline-world"
    size = 30 * 1024 * 1024
    _isolate_world_kb(monkeypatch, tmp_path, world, {"huge.md": "x" * size})   # 改行なし・NEEDLE も含まない

    tracemalloc.start()
    try:
        res, _, _, _ = A.run_tool("read_doc", {"doc_id": "huge.md"}, world, None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert "error" not in res, res
    assert res["total_lines"] == 1
    assert res["file_truncated"] is True
    assert peak < 15 * 1024 * 1024   # 30MBの単一行に対しピーク割当はずっと小さい（行サイズに非比例）


def test_run_tool_read_around_single_huge_line_beyond_line_max_bounded_memory(monkeypatch, tmp_path):
    """read_around でも単一巨大行の安全弁が効く: `SHERPA_READ_LINE_MAX_BYTES`（既定2MiB）を超える
    単一行（10MB）でも、ピーク割当は非比例のまま、最終的な返却テキストは従来どおり
    `TOOL_RESULT_MAX_BYTES` に収まる（`test_read_around_clips_output_for_huge_single_line_doc` は
    既定の行安全弁の閾値未満（200万文字）だったため、本テストは閾値を超える行で確認する）。"""
    import tracemalloc

    world = "read-around-hugesingleline-world"
    size = 10 * 1024 * 1024   # 既定 _READ_LINE_MAX_BYTES(2MiB) を優に超える
    _isolate_world_kb(monkeypatch, tmp_path, world, {"huge.md": "A" * size})

    tracemalloc.start()
    try:
        res, _, _, _ = A.run_tool(
            "read_around", {"doc_id": "huge.md", "line": 1, "window": 5}, world, None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert "error" not in res, res
    assert len(res["text"].encode("utf-8")) <= A.TOOL_RESULT_MAX_BYTES
    assert peak < 15 * 1024 * 1024


def test_run_tool_grep_hit_line_matches_read_around_for_special_separators(monkeypatch, tmp_path):
    """`\\f`（改ページ＝COBOL/JCL リストに実在）・`\\x85`（NEL＝EBCDIC 変換由来）は `str.splitlines()`
    の区切りだが実バイトの `\\n` ではない——grep 側（`ripgrep_search`）と read 側（`read_around`）は
    どちらも `grep_tool._logical_lines` を共有するため、grep が返した行番号をそのまま read_around
    に渡すと同じ行が返る（引用と精読の整合＝コミット 1cb58549 の検収是正と同型の回帰固定）。"""
    world = "sep-consistency-world"
    # 論理行: 1=alpha / 2=beta / 3=GAMMA_NEEDLE（\f区切り） / 4=delta / 5=epsilon（\x85区切り）
    content = "alpha\nbeta\x0cGAMMA_NEEDLE\ndelta\x85epsilon\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"doc.txt": content})

    hit_res, _, _, _ = A.run_tool("ripgrep_search", {"query": "GAMMA_NEEDLE"}, world, None)
    assert "error" not in hit_res, hit_res
    assert len(hit_res["hits"]) == 1
    hit_line = hit_res["hits"][0]["line"]
    assert hit_line == 3

    # window=0 は falsy（`args.get("window") or ...`）で既定 window へフォールバックするため、
    # ここでは最小の非0窓（1）を明示して行3の周辺だけに絞る。
    around_res, _, _, _ = A.run_tool(
        "read_around", {"doc_id": "doc.txt", "line": hit_line, "window": 1}, world, None)
    assert "error" not in around_res, around_res
    assert around_res["text"] == "2: beta\n3: GAMMA_NEEDLE\n4: delta"


# ===== S2: `_truncated_docs_node`（UI「思考の流れ」への打切り表示） =====

def test_truncated_docs_node_present_only_when_truncated_docs_nonempty():
    """`_truncated_docs_node()` は `truncated_docs`（非空）があるときだけノード化する
    （`_degrade_result_node` と同じ「run_tool 直後に result を見てもう1件 yield する」枠組み）。
    文言は平文のみ（内部語彙＝doc_id は一切出さない・docs/04-画面の原則.md）。"""
    node = A._truncated_docs_node({"hits": [], "truncated_docs": ["big.xlsx"]})
    assert node["type"] == "node" and node["kind"] == "tool"
    assert "big.xlsx" not in node["label"] and "big.xlsx" not in node["detail"]
    assert A._truncated_docs_node({"hits": []}) is None
    assert A._truncated_docs_node({"hits": [], "truncated_docs": []}) is None   # 空リストは None
    assert A._truncated_docs_node({"count": 0, "docs": []}) is None   # list_docs 等の無関係な result


def test_openai_style_yields_truncated_docs_node_when_ripgrep_search_reports_it(monkeypatch):
    """S2: `ripgrep_search` が `truncated_docs` を申告したら、「思考の流れ」に打切りノードが乗る
    （`degrade_reason` の既存の仕組みと完全に同型の枠組みに1種類足しただけ＝フロントは既存ノードの
    kind/label/detail 契約のまま無改修で表示できる）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "確認しました。"}}]},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    def fake_grep(q, world, **kw):
        td = kw.get("truncated_docs")
        if td is not None:
            td.append("big.xlsx")
        return []

    monkeypatch.setattr(A.grep_tool, "grep_search", fake_grep)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
    nodes = [e["node"] for e in events if "node" in e]
    assert any(n["label"] == "検索が一部打ち切られています" for n in nodes)


# ===== L4c: 親返し（検索は細かく・回答には文脈を・§3.3/§3.4）=====
# es_search 限定・常時 ON（TOGGLE-RM・2026-09-03 でグローバル切替トグル `SHERPA_ES_PARENT_RETURN`
# を撤去）。ヒットを doc_id で束ね、予算内なら rag.md 全文(P3)／領域(P2)を返し、両方超える場合は
# 子チャンク（chunk・最低保証）のまま。

def _setup_parent_return_world(monkeypatch, tmp_path, world: str, hits: list, rag_files: dict) -> None:
    """親返しテスト共通セットアップ: `es_index.search`/`documents.world_rel_set` をスタブし、
    `rag_files`（`{doc_id: rag.md 本文}`）を `worlds.derived_rag_dir(world)`（§8.1 三階層）配下へ書く。
    """
    from sherpa import documents
    der_rag = tmp_path / "rag"
    der_rag.mkdir(parents=True, exist_ok=True)
    for doc_id, content in rag_files.items():
        (der_rag / (doc_id + ".rag.md")).write_text(content, encoding="utf-8")
    monkeypatch.setattr(A.worlds, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: tmp_path / "md")   # legacy 無し
    monkeypatch.setattr(A.es_index, "search",
                        lambda w, q, scope_paths=None, k=20, layer=None, **kw: (list(hits), None))
    monkeypatch.setattr(documents, "world_rel_set", lambda w, **kw: {h["doc_id"] for h in hits})


def test_parent_return_three_tiers_fixed_by_size(monkeypatch, tmp_path):
    """P3(全文が予算内)・P2(領域縮退)・chunk(両方超過)の3段を、サイズを操作した3文書で同時に固定する
    （§3.4 の配分規則＝ベストスコア順に P3→P2→chunk を試す）。"""
    world = "parent-return-tiers-world"
    full_md = "<!-- chunk:cf1 -->\n" + "F" * 200 + "\n"
    region_md = ("<!-- chunk:cr1 -->\n" + "R" * 100 + "\n\n"
                "<!-- chunk:cr2 -->\n" + "R" * 100 + "\n\n"
                "<!-- chunk:pad1 -->\n" + "P" * 5000 + "\n")
    chunk_md = ("<!-- chunk:cc1 -->\n" + "C" * 50 + "\n\n"
               "<!-- chunk:cc2 -->\n" + "C" * 5000 + "\n")
    hits = [
        {"doc_id": "full.docx", "text": "F", "ext": ".docx", "chunk_id": "cf1", "parent_id": "pf", "score": 3.0},
        {"doc_id": "region.docx", "text": "R", "ext": ".docx", "chunk_id": "cr1", "parent_id": "pr", "score": 2.0},
        {"doc_id": "chunk.docx", "text": "C", "ext": ".docx", "chunk_id": "cc1", "parent_id": "pc", "score": 1.0},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {
        "full.docx": full_md, "region.docx": region_md, "chunk.docx": chunk_md})

    def fake_chunk_ids_for_parent(w, doc_id, parent_ids, limit=5000):
        if doc_id == "region.docx":
            return ["cr1", "cr2"]
        if doc_id == "chunk.docx":
            return ["cc1", "cc2"]
        return []

    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", fake_chunk_ids_for_parent)
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 1000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["full.docx"]["tier"] == "full"
    assert by_doc["full.docx"]["text"] == "\n".join(full_md.splitlines())
    assert by_doc["region.docx"]["tier"] == "region"
    assert "R" * 100 in by_doc["region.docx"]["text"]
    assert "P" * 5000 not in by_doc["region.docx"]["text"]      # 対象外の領域（pad1）は含まない
    assert by_doc["chunk.docx"]["tier"] == "chunk"
    assert by_doc["chunk.docx"]["text"] == "C"                  # 最低保証（子チャンクの結合＝1件分）
    # ベストスコア順（決定的な配分順）で並ぶ。
    assert [h["doc_id"] for h in res["hits"]] == ["full.docx", "region.docx", "chunk.docx"]


def test_parent_return_minimum_guarantee_lower_score_doc_survives(monkeypatch, tmp_path):
    """§3.4「最低保証」: スコア1位の文書が P3 で予算の残りを使い切っても、2位の文書の子チャンク
    （最低保証＝baseline）は消えない（黙って空文字/欠落にならない）。"""
    world = "parent-return-minguard-world"
    doc1_md = "<!-- chunk:c1 -->\n" + "A" * 400 + "\n"          # 1位: 予算内に収まる全文
    doc2_md = "<!-- chunk:c2 -->\n" + "B" * 50000 + "\n"        # 2位: 全文も領域も予算を大幅に超える
    hits = [
        {"doc_id": "top.docx", "text": "top-baseline", "ext": ".docx",
         "chunk_id": "c1", "parent_id": "p1", "score": 2.0},
        {"doc_id": "second.docx", "text": "second-baseline", "ext": ".docx",
         "chunk_id": "c2", "parent_id": "p2", "score": 1.0},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits,
                               {"top.docx": doc1_md, "second.docx": doc2_md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: [])
    # baseline 合計（"top-baseline"+"second-baseline"）＋ doc1 の全文アップグレード分だけが入る予算
    # （doc2 に回せる余剰は残らない設計値）。
    baseline_total = len("top-baseline".encode("utf-8")) + len("second-baseline".encode("utf-8"))
    delta_doc1 = len(doc1_md.encode("utf-8")) - len("top-baseline".encode("utf-8"))
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", baseline_total + delta_doc1)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["top.docx"]["tier"] == "full"
    assert by_doc["second.docx"]["tier"] == "chunk"
    assert by_doc["second.docx"]["text"] == "second-baseline"   # 消えない・空にならない


def test_parent_return_declares_tier_for_every_doc(monkeypatch, tmp_path):
    """§3.4「限界に当たったら黙らない」: 親返し対象（chunk_id あり）の全エントリが `tier` を持つ
    （アップグレードできなかった doc も含めて必ず申告する）。"""
    world = "parent-return-declare-world"
    hits = [{"doc_id": "a.docx", "text": "本文A", "ext": ".docx",
            "chunk_id": "c1", "parent_id": "p1", "score": 1.0}]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {})   # rag.md 不在（解決不能）
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: [])

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    assert res["hits"] == [{"doc_id": "a.docx", "tier": "chunk", "text": "本文A",
                            "chunks": [{"chunk_id": "c1"}]}]


def test_parent_return_deterministic(monkeypatch, tmp_path):
    """同じ入力なら同じ結果（並び・段）になる（§3.4「決定的な貪欲法」）。"""
    world = "parent-return-determinism-world"
    md = ("<!-- chunk:c1 -->\n" + "X" * 100 + "\n\n"
         "<!-- chunk:c2 -->\n" + "Y" * 100 + "\n")
    hits = [
        {"doc_id": "a.docx", "text": "aの本文", "ext": ".docx", "chunk_id": "c1", "parent_id": "p", "score": 1.5},
        {"doc_id": "b.docx", "text": "bの本文", "ext": ".docx", "chunk_id": "c2", "parent_id": "p", "score": 1.5},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"a.docx": md, "b.docx": md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: [])
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 5000)

    res1, _, _, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    res2, _, _, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    assert res1 == res2
    # 同点スコアは doc_id 昇順（決定的なタイブレーク）。
    assert [h["doc_id"] for h in res1["hits"]] == ["a.docx", "b.docx"]


def test_parent_return_citations_stay_at_chunk_grain(monkeypatch, tmp_path):
    """§3.3「引用の粒度は落とさない」: `out`（LLM 表示）は doc 単位に束ねても、`cites` は
    従来どおり子チャンク単位（locator 付き）のまま——doc あたり複数ヒットでも `cites` は
    ヒット数ぶんそのまま残る。"""
    world = "parent-return-citations-world"
    md = ("<!-- chunk:c1 -->\nセルA本文\n\n<!-- chunk:c2 -->\nセルB本文\n")
    hits = [
        {"doc_id": "a.xlsx", "text": "セルA本文", "ext": ".xlsx", "chunk_id": "c1", "parent_id": "p",
         "score": 1.0, "locator": {"sheet": "一覧", "cell_range": "A1"}},
        {"doc_id": "a.xlsx", "text": "セルB本文", "ext": ".xlsx", "chunk_id": "c2", "parent_id": "p",
         "score": 1.0, "locator": {"sheet": "一覧", "cell_range": "B1"}},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"a.xlsx": md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: [])
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 5000)

    res, _docs, cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    assert len(res["hits"]) == 1 and res["hits"][0]["doc_id"] == "a.xlsx"   # doc 単位に束ねられている
    assert len(cites) == 2                                                 # 引用は子チャンク単位のまま
    assert {c["quote"] for c in cites} == {"セルA本文", "セルB本文"}
    assert res["hits"][0]["chunks"] == [
        {"chunk_id": "c1", "locator": {"sheet": "一覧", "cell_range": "A1"}},
        {"chunk_id": "c2", "locator": {"sheet": "一覧", "cell_range": "B1"}},
    ]


def test_parent_return_legacy_hits_pass_through_untouched(monkeypatch, tmp_path):
    """legacy 40行チャンク由来のヒット（`chunk_id` 無し）は親返しの対象外＝従来どおり素通しする
    （rag チャンクのヒットと混在しても、legacy 側の形は変わらない）。"""
    world = "parent-return-legacy-world"
    rag_md = "<!-- chunk:c1 -->\nrag本文\n"
    hits = [
        {"doc_id": "rag.docx", "text": "rag本文", "ext": ".docx", "chunk_id": "c1", "parent_id": "p", "score": 2.0},
        {"doc_id": "legacy.md", "line": 7, "text": "legacy本文", "ext": ".md", "score": 1.0},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"rag.docx": rag_md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: [])
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 5000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    legacy_entries = [h for h in res["hits"] if h["doc_id"] == "legacy.md"]
    assert legacy_entries == [{"doc_id": "legacy.md", "line": 7, "text": "legacy本文"}]
    assert "tier" not in legacy_entries[0] and "chunks" not in legacy_entries[0]


def test_parent_return_redacts_full_and_region_text(monkeypatch, tmp_path):
    """redaction（`_redact`）は P3/P2 の本文にも効く——ES ヒット断片だけでなく、rag.md から直接
    読んだ全文/領域テキストも秘密パターンを伏せて返す（rag.md 全文を未マスクで LLM に渡さない）。"""
    world = "parent-return-redact-world"
    secret = "sk-1234567890ABCDEFGHIJ"                     # `_SECRET_RE` の sk- パターンに一致
    md = f"<!-- chunk:c1 -->\nAPIキー: {secret}\n"
    hits = [{"doc_id": "a.docx", "text": "本文", "ext": ".docx",
            "chunk_id": "c1", "parent_id": "p1", "score": 1.0}]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"a.docx": md})
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 5000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    assert res["hits"][0]["tier"] == "full"
    assert secret not in res["hits"][0]["text"]
    assert "[REDACTED]" in res["hits"][0]["text"]


def test_parent_return_disabled_env_is_byte_identical_to_hit_per_chunk(monkeypatch, tmp_path):
    """TOGGLE-RM（2026-09-03）: グローバルな系統切替トグルは撤去済み・env では OFF にできない。
    `_parent_return_enabled` は今も内部シームとして残るため、直接差し替えて False 分岐
    （従来のヒット単位・束ねない・`tier`/`chunks` 無し・rag.md が実在しても一切読みに行かない）を
    byte-identical のまま引き続き検証する。"""
    world = "parent-return-off-world"
    md = "<!-- chunk:c1 -->\n" + "Z" * 100 + "\n"
    hits = [{"doc_id": "a.docx", "text": "本文A", "ext": ".docx",
            "chunk_id": "c1", "parent_id": "p1", "score": 1.0}]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"a.docx": md})
    monkeypatch.setattr(A, "_parent_return_enabled", lambda: False)

    res, docs, cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    assert res["hits"] == [{"doc_id": "a.docx", "line": None, "text": "本文A"}]
    assert docs == {"a.docx"} and len(cites) == 1


def test_parent_return_p2_region_bounded_memory_for_large_rag_md(monkeypatch, tmp_path):
    """§3.3「全文を読み込んでから切り詰める実装は禁止」の実測固定: 20MB超の rag.md でも、
    対象外チャンクの本文は蓄積せずスキップするため、P2（領域）解決中の Python 側ピーク割当は
    ファイルサイズに比例しない（`tests/unit/test_grep_tool.py`/read側ストリーミングテストと同じ
    tracemalloc の流儀）。対象チャンクをファイル**末尾**に置き、全文スキャンを要求する
    最悪ケースで固定する。"""
    import tracemalloc

    world = "parent-return-bigfile-world"
    pad_body = "x" * 5000
    n = (20 * 1024 * 1024) // (len(pad_body) + 40) + 1000   # 端数切り捨て＋桁数増加分を見込んで多めに
    padding = "".join(f"<!-- chunk:pad{i} -->\n{pad_body}\n\n" for i in range(n))
    target = "<!-- chunk:t1 -->\n領域本文1。\n\n<!-- chunk:t2 -->\n領域本文2。\n"
    rag_md = padding + target
    assert len(rag_md.encode("utf-8")) > 20 * 1024 * 1024

    hit = {"doc_id": "big.docx", "text": "本文", "ext": ".docx",
          "chunk_id": "t1", "parent_id": "pt", "score": 1.0}
    _setup_parent_return_world(monkeypatch, tmp_path, world, [hit], {"big.docx": rag_md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent",
                        lambda w, doc_id, parent_ids, limit=5000: ["t1", "t2"])
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 2000)   # 全文(20MB超)は不可・領域(小)は入る

    tracemalloc.start()
    try:
        res, _, _, _ = A.run_tool("es_search", {"query": "q"}, world, None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert res["hits"][0]["tier"] == "region"
    assert "領域本文1。" in res["hits"][0]["text"] and "領域本文2。" in res["hits"][0]["text"]
    assert peak < 10 * 1024 * 1024   # 20MB超のファイルに対しピーク割当はそれよりずっと小さい


def test_parent_return_chunk_degrade_bounded_memory_for_large_rag_md(monkeypatch, tmp_path):
    """同上の大ファイルで、領域も予算を超える（chunk へ縮退する）場合も全文を読み切らずピーク割当は
    非比例のまま——`_rag_md_region_text` は蓄積バイト数が `byte_cap` を超えた時点で打ち切る。"""
    import tracemalloc

    world = "parent-return-bigfile-degrade-world"
    pad_body = "x" * 5000
    n = (20 * 1024 * 1024) // (len(pad_body) + 40) + 1000
    padding = "".join(f"<!-- chunk:pad{i} -->\n{pad_body}\n\n" for i in range(n))
    target = "<!-- chunk:t1 -->\n" + "領" * 3000 + "\n\n<!-- chunk:t2 -->\n" + "域" * 3000 + "\n"
    rag_md = padding + target
    assert len(rag_md.encode("utf-8")) > 20 * 1024 * 1024

    hit = {"doc_id": "big2.docx", "text": "本文", "ext": ".docx",
          "chunk_id": "t1", "parent_id": "pt", "score": 1.0}
    _setup_parent_return_world(monkeypatch, tmp_path, world, [hit], {"big2.docx": rag_md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent",
                        lambda w, doc_id, parent_ids, limit=5000: ["t1", "t2"])
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 200)   # 領域(t1+t2)すら入らない予算

    tracemalloc.start()
    try:
        res, _, _, _ = A.run_tool("es_search", {"query": "q"}, world, None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert res["hits"][0]["tier"] == "chunk"
    assert res["hits"][0]["text"] == "本文"
    assert peak < 10 * 1024 * 1024
