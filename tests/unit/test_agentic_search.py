"""エージェント検索（索引なし・LLM が grep ツールを反復）の単体テスト。LLM は stub（コスト0）。

- run_tool: ripgrep_search / read_around / 範囲外拒否（v1 フィクスチャの filesystem grep・Neo4j 不要）。
- openai_style ループ: _post を差し替え、tool 呼び出し→最終回答→docs 収集／ask_user 質問を検証。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")   # es_search が実埋め込みを叩かない（BM25）
import pytest  # noqa: E402
from sherpa import agentic_search as A   # noqa: E402
from sherpa.parts.read import tools as RT   # noqa: E402
from sherpa import store   # noqa: E402   # BUDGET-1: system_settings > コード既定のテスト用
import _corpus_expect as CE   # noqa: E402   # フィクスチャ実走査ベースの list_docs 期待値（フェーズ7 S1）
import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証

# 本モジュールは fixtures/corpus/v1 の実コーパス（`.md`＝資料・`.cbl`/`.cpy`＝コード）の固定分類を
# 前提にする——フォークが正規の拡張アナライザを登録していても赤にならないよう、登録簿を上流限定に
# 固定する（開発ハーネス S4・敵対 RV 是正・docs/21-拡張の契約.md）。
pytestmark = pytest.mark.usefixtures("upstream_only_registry")


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


# ===== `_hit_summary_node`/`_hit_summary_node_sub`: 「何を探して・いくつ当たったか」の追加ノード =====

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


# ===== ヒット単位のページング（offset）・ヒット単位のバイト上限（網羅性を落とさずに文脈枠を守る）=====

def test_run_tool_offset_omitted_defaults_to_zero(monkeypatch):
    """`args` に `offset` が無ければ0として grep_tool.grep_search へ転送する
    （既存呼び出し元は無変更・offset は ripgrep_search だけが読む）。"""
    captured = {}
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: captured.update(kw) or [])
    A.run_tool("ripgrep_search", {"query": "x"}, "v1", None)
    assert captured.get("offset") == 0


def test_run_tool_forwards_offset_to_grep_search(monkeypatch):
    """`args["offset"]` は `grep_tool.grep_search` へそのまま転送する。"""
    captured = {}
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: captured.update(kw) or [])
    A.run_tool("ripgrep_search", {"query": "x", "offset": 15}, "v1", None)
    assert captured.get("offset") == 15


def test_run_tool_es_search_ignores_offset_argument(monkeypatch):
    """`es_search`（ES hybrid）は offset ページングを持たない——kNN 句の候補集合はページを跨いで
    固定できず、候補集合を揃えようとすると重要度補正後の順位がページごとに入れ替わり重複/欠落が
    起きるため。`args["offset"]` を渡しても `es_index.search` へは転送されない。"""
    from sherpa import documents

    captured = {}

    def fake_search(world, q, scope_paths=None, k=20, layer=None, **kw):
        captured.update(kw)
        return [], None

    monkeypatch.setattr(A.es_index, "search", fake_search)
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: set())
    A.run_tool("es_search", {"query": "x", "offset": 40}, "v1", None)
    assert "offset" not in captured


def test_run_tool_es_search_result_never_has_next_offset(monkeypatch):
    """`es_search` の結果は `truncated:true` になっても `next_offset` を付けない
    （続きが必要なら `max_hits` を増やす・上位K件打切りで続きが取れない実害は語句検索側の問題）。"""
    from sherpa import documents

    hits = [{"doc_id": f"a{i}.md", "line": 1, "text": "x", "ext": ".md", "score": 1.0}
           for i in range(5)]
    monkeypatch.setattr(A.es_index, "search", lambda *a, **kw: (hits, None))
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {h["doc_id"] for h in hits})
    res, _docs, _cites, _cards = A.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=5)
    assert res.get("truncated") is True
    assert "next_offset" not in res


def test_run_tool_offset_negative_clamped_to_zero(monkeypatch):
    """負の `offset` は0扱い（list_docs の offset クランプと同じ流儀）。"""
    captured = {}
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: captured.update(kw) or [])
    A.run_tool("ripgrep_search", {"query": "x", "offset": -7}, "v1", None)
    assert captured.get("offset") == 0


def test_run_tool_offset_past_abs_ceiling_short_circuits_without_calling_search(monkeypatch):
    """`offset` は LLM が渡す未検証値——grep 側はヒープ容量を offset+ヒット数まで広げるため、
    際限なく大きい offset を許すとヒープが無制限に肥大する（DoS）。`offset` 自体は**巻き戻さない**
    （巻き戻すと next_offset を辿るたびに同じ範囲を再送し続け、上限を超えた領域へ永久に到達できない）
    ——`offset >= MAX_HITS_ABS_MAX` なら検索自体を呼ばず空を返す（母集団の先はもう見ない）。"""
    captured = {"called": False}
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: captured.update(called=True) or [])
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "x", "offset": 10_000_000}, "v1", None, max_hits=30)
    assert res == {"hits": []}
    assert captured["called"] is False   # 検索そのものを呼ばない＝ヒープを膨らませない
    assert "next_offset" not in res and "truncated" not in res


def test_run_tool_ripgrep_search_offset_pages_through_real_corpus(monkeypatch, tmp_path):
    """実コーパスを介した end-to-end: `offset` を進めてページングすると全件を重複・欠落なく
    たどれ、続きがある間だけ `truncated`/`next_offset` が付く（最後のページには付かない）。"""
    world = "offset-paging-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        name: "NEEDLE 行\n" for name in ("a.md", "b.md", "c.md", "d.md", "e.md")})

    def _page(offset):
        return A.run_tool("ripgrep_search", {"query": "NEEDLE", "offset": offset}, world, None,
                          max_hits=2)[0]

    p0 = _page(0)
    assert [h["doc_id"] for h in p0["hits"]] == ["a.md", "b.md"]
    assert p0.get("truncated") is True and p0.get("next_offset") == 2

    p1 = _page(p0["next_offset"])
    assert [h["doc_id"] for h in p1["hits"]] == ["c.md", "d.md"]
    assert p1.get("truncated") is True and p1.get("next_offset") == 4

    p2 = _page(p1["next_offset"])
    assert [h["doc_id"] for h in p2["hits"]] == ["e.md"]
    assert "truncated" not in p2 and "next_offset" not in p2   # 最後のページ＝続きは無い


def test_run_tool_ripgrep_search_offset_near_ceiling_shrinks_page_without_rolling_back(monkeypatch, tmp_path):
    """母集団が `MAX_HITS_ABS_MAX` を超えるとき、天井近くの offset は**巻き戻らない**——ページの
    件数（`used_max_hits`）だけが縮み、既出ヒットを再送しない。天井ちょうどに達したページは
    `next_offset` を出さない（最終ページ）。"""
    world = "offset-ceiling-world"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        name: "NEEDLE 行\n" for name in ("a.md", "b.md", "c.md", "d.md", "e.md", "f.md", "g.md", "h.md")})
    monkeypatch.setattr(A, "MAX_HITS_ABS_MAX", 5)   # 母集団(8件) > 天井(5件) を小さく再現
    monkeypatch.setattr(RT, "MAX_HITS_ABS_MAX", 5)

    p0, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "NEEDLE", "offset": 0}, world, None, max_hits=3)
    assert [h["doc_id"] for h in p0["hits"]] == ["a.md", "b.md", "c.md"]
    assert p0.get("truncated") is True and p0.get("next_offset") == 3

    # offset=3, max_hits=3 だが天井(5)まで残り2件しか無い＝ページは2件に縮む（3のままではない）。
    p1, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "NEEDLE", "offset": 3}, world, None, max_hits=3)
    assert [h["doc_id"] for h in p1["hits"]] == ["d.md", "e.md"]   # 既出の a/b/c を再送しない
    assert p1.get("truncated") is True
    assert "next_offset" not in p1   # ちょうど天井(5)に達した＝最終ページ

    # 天井以上の offset は空（もう検索しない）。
    p2, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "NEEDLE", "offset": 5}, world, None, max_hits=3)
    assert p2 == {"hits": []}


def test_run_tool_ripgrep_search_hit_text_clipped_to_per_hit_budget(monkeypatch):
    """`per_hit = max(_HIT_TEXT_MIN_BYTES, tool_result_max_bytes // max_hits)` でヒットの
    `text` を末尾クリップし、切ったヒットにだけ `text_truncated: True` を付ける
    （`tool_result_max_bytes=64*1024, max_hits=30` → 2184 バイト）。"""
    big_text = "x" * 5000   # per_hit より確実に大きい ASCII 本文（マルチバイト境界の影響を排除）
    hits = [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": big_text, "ext": ".md"}]
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "x"}, "v1", None, max_hits=30, tool_result_max_bytes=64 * 1024)
    hit = res["hits"][0]
    assert len(hit["text"].encode("utf-8")) == 2184
    assert hit["text"] == big_text[:2184]   # 末尾だけを切る（先頭は保たれる）
    assert hit["text_truncated"] is True


def test_run_tool_ripgrep_search_per_hit_budget_floors_at_min_bytes(monkeypatch):
    """ヒット数が多いほど1件あたりの均等割りは小さくなるが、下限 `_HIT_TEXT_MIN_BYTES`（512）は
    必ず残る（`tool_result_max_bytes=64*1024, max_hits=200` → 65536//200=327 は512未満なので512）。"""
    big_text = "x" * 5000
    hits = [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": big_text, "ext": ".md"}]
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "x"}, "v1", None, max_hits=200, tool_result_max_bytes=64 * 1024)
    hit = res["hits"][0]
    assert len(hit["text"].encode("utf-8")) == A._HIT_TEXT_MIN_BYTES == 512
    assert hit["text_truncated"] is True


def test_run_tool_ripgrep_search_serialized_view_stays_within_budget_despite_per_hit_overhead(monkeypatch):
    """`per_hit = tr_max_bytes // max_hits` は text だけの割当てで、各ヒットの付帯情報
    （doc_id・line 等）と JSON 構造分を数えない——30件×2184バイトの text だけなら 64KiB に収まる
    計算でも、直列化した最終形は付帯情報の分だけ超過しうる。`view` を組み立てた後の実バイト数で
    収まりを保証し（per_hit を詰めて再構築）、それでも `next_offset` と全ヒットの `doc_id` が
    残ることを確認する。"""
    hits = [{"doc_id": f"doc{i:03d}.md", "line": 1, "span": [1, 1], "text": "x" * 5000, "ext": ".md"}
           for i in range(30)]
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "x"}, "v1", None, max_hits=30, tool_result_max_bytes=64 * 1024)
    final_bytes = len(json.dumps(res, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 64 * 1024, final_bytes
    assert len(res["hits"]) == 30
    assert [h["doc_id"] for h in res["hits"]] == [f"doc{i:03d}.md" for i in range(30)]
    assert res.get("next_offset") == 30
    assert res.get("truncated") is True


def test_run_tool_ripgrep_search_top_level_text_truncated_when_any_hit_clipped(monkeypatch):
    """いずれかのヒットが per_hit で切られたら、結果の最上位にも `text_truncated: True` を立てる
    ——`_record_run_tool_limits`/`mcp_server.py` はどちらも最上位キーしか見ないため、hits[i] だけ
    に付けると `tool_result_clipped` の計測から漏れる。"""
    hits = [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "x" * 5000, "ext": ".md"},
            {"doc_id": "b.md", "line": 1, "span": [1, 1], "text": "short", "ext": ".md"}]
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "x"}, "v1", None, max_hits=30, tool_result_max_bytes=64 * 1024)
    assert res["text_truncated"] is True
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["a.md"]["text_truncated"] is True
    assert "text_truncated" not in by_doc["b.md"]


def test_run_tool_ripgrep_search_no_top_level_text_truncated_when_no_hit_clipped(monkeypatch):
    """1件もクリップされなければ最上位に `text_truncated` キー自体を作らない（既存語彙の流儀）。"""
    hits = [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "short", "ext": ".md"}]
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "x"}, "v1", None, max_hits=30, tool_result_max_bytes=64 * 1024)
    assert "text_truncated" not in res


def test_run_tool_ripgrep_search_hit_under_per_hit_budget_is_byte_identical(monkeypatch):
    """1ヒットが per_hit 未満なら一切変えない（`text_truncated` キー自体を作らない）。"""
    small_text = "NEEDLE を含む短い一行"
    hits = [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": small_text, "ext": ".md"}]
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: hits)
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "x"}, "v1", None, max_hits=30, tool_result_max_bytes=64 * 1024)
    hit = res["hits"][0]
    assert hit["text"] == small_text
    assert "text_truncated" not in hit


def test_run_tool_ripgrep_search_md_heading_survives_per_hit_clip(monkeypatch, tmp_path):
    """MD ヒットの見出し行（節本文の先頭行）は末尾クリップでも失われない——実コーパスを介した
    end-to-end で、大きな見出し節を持つヒットだけが切られ、見出しは残ることを確認する。"""
    world = "hit-cap-md-world"
    heading = "# TITLE"
    body = ("NEEDLE line " + "x" * 60 + "\n") * 50   # 見出し込みで per_hit(2048) を確実に超える
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "big.md": heading + "\n" + body,
        "small.md": "NEEDLE small line\n",
    })
    res, _docs, _cites, _cards = A.run_tool(
        "ripgrep_search", {"query": "NEEDLE"}, world, None, max_hits=2, tool_result_max_bytes=4096)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    big_hit = by_doc["big.md"]
    assert len(big_hit["text"].encode("utf-8")) == 2048   # tool_result_max_bytes=4096, max_hits=2
    assert big_hit["text"].startswith(heading)
    assert big_hit["text_truncated"] is True
    small_hit = by_doc["small.md"]
    assert small_hit["text"] == "NEEDLE small line"
    assert "text_truncated" not in small_hit


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


# ===== docs/archive/2026-09-12-利用統計の拡充2.md §3b: 利用統計チャットの調査ツール =====
# world/scope_paths とは無関係（引数検証・dispatch・cards サイドカーだけを固定する・DB は monkeypatch
# で切り離す＝本ファイルの他の run_tool テストと同じ「実埋め込み/実DBを叩かない」流儀）。

# ===== STAT-4 U4 RV是正 #13: usage 系ツール結果もバイト予算内にクリップされる =====
# `tool_result_max_bytes` は他ツール（read_around 等）と同じ `run_tool` の引数で、usage 分岐にも
# `_fit_usage_result` 経由で効く（`usage_chat._compact_stats_context` と同じ段階縮小の流儀）。

def _big_usage_by_user_rows(n: int) -> dict:
    return {"period": {"start": "2026-01-01", "end": "2026-02-01", "days": 30}, "uid": None, "kind": None,
           "rows": [{"uid": f"user{i:04d}", "kind": "chat", "calls": i, "input": i * 100,
                    "cached_input": 0, "output": i * 50, "reasoning_output": 0,
                    "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0}
                   for i in range(n)]}


def _big_usage_overview() -> dict:
    users = [{"uid": f"u{i:04d}", "turns": i, "conversations": i, "active_days": 1,
             "last_active": "2026-01-01T00:00:00+09:00", "lens": {"impact": 0, "qa": i, "troubleshoot": 0,
             "chat": 0}, "personal_turns": 0, "worlds": ["v1"], "logins": 0, "downloads": 0, "uploads": 0,
             "shares": 0, "knowledge_turns": i, "zero_hit_turns": 0, "zero_hit_rate": None}
            for i in range(200)]
    daily = [{"date": f"2026-01-{d:02d}", "turns": d, "active_users": 1} for d in range(1, 32)]
    return {
        "period": {"start": "2026-01-01", "end": "2026-02-01", "days": 30},
        "totals": {"turns": 1000, "active_users": 200, "conversations": 500}, "zero_hit": {"knowledge_turns": 0,
        "zero_hit_turns": 0, "rate": None}, "worlds": [{"world": "v1", "turns": 100}],
        "providers": [{"provider": "openai", "turns": 100}], "retention": {"weekly": [], "revisit_rate": None},
        "downloads": {"total": 0, "daily": []}, "daily": daily, "stop_kinds": [], "stopped_turns": 0,
        "conversation_turns": {"avg": 1.0, "median": 1.0, "max": 5, "p90": 3.0}, "resume_rate": None,
        "response_time": {"overall": {"avg": 100.0, "median": 90.0, "max": 500, "p90": 300.0, "n": 10,
                          "provider": None}, "by_provider": []},
        "users": users,
        "tokens": {"totals": {"turns": 0, "input": 0, "cached_input": 0, "output": 0, "reasoning_output": 0},
                  "daily": [], "by_kind": [], "by_model": [], "by_user": users, "by_user_kind": []},
        "conversations_top": [{"conversation_id": i, "uid": f"u{i:04d}", "world": "v1", "user_turns": i,
                              "response_time_avg_ms": None, "kinds": []} for i in range(50)],
    }


# ===== STAT-4 C3是正: 返却上限（50件）はバイト予算より先に適用する =====
# `usage_daily` の `series` と `usage_conversation_detail` の `response_time_series` は
# `store.py` 側に上限が無く、件数がバイト予算内に収まっていれば（RV 指摘: 60日分の
# usage_daily 等）バイト超過時だけ効く段階縮小（上のテスト群）を素通りしていた。

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


def test_list_docs_offset_pages_through_all_rows_without_gap_or_overlap():
    """offset で続きを取れる（RV 2026-09-10 #4: 単一フォルダに上限 500 超があると分割では取り切れない）。
    limit ずつ offset を進めた和集合が全件と一致し、重複も欠落もない。範囲外 offset は空一覧・count 不変。"""
    total = CE.count_under("4期")
    seen: list = []
    for off in range(0, total, 3):
        res, _, _, _ = A.run_tool("list_docs", {"path_prefix": "4期", "limit": 3, "offset": off}, "v1", None)
        assert res["count"] == total and res["offset"] == off
        seen += [d["rel_path"] for d in res["docs"]]
    full, _, _, _ = A.run_tool("list_docs", {"path_prefix": "4期", "limit": 500}, "v1", None)
    assert seen == [d["rel_path"] for d in full["docs"]]
    res, _, _, _ = A.run_tool("list_docs", {"path_prefix": "4期", "offset": total + 10}, "v1", None)
    assert res["count"] == total and res["docs"] == []
    res, _, _, _ = A.run_tool("list_docs", {"path_prefix": "4期", "offset": "x"}, "v1", None)
    assert res["offset"] == 0                                        # 不正値は既定 0（落とさない）


def test_list_docs_respects_session_scope_and_unknown_world():
    """scope_paths（セッション範囲）で絞られ、未登録 world は 0 件（例外にならない）。"""
    res, docs, _, _ = A.run_tool("list_docs", {}, "v1", ["4期/03_開発"])
    assert res["count"] == CE.count_under("4期/03_開発")
    assert all(d["rel_path"].startswith("4期/03_開発/") for d in res["docs"])
    assert docs and docs == {d["rel_path"] for d in res["docs"]}

    res2, docs2, _, _ = A.run_tool("list_docs", {}, "no-such-world-xyz", None)
    assert res2 == {"count": 0, "offset": 0, "docs": [], "truncated": False, "next_offset": None} and docs2 == set()


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


def test_list_docs_pagination_covers_all_without_gaps_or_dupes(monkeypatch):
    """S4: 並びは常に rel_path 昇順固定——offset を進めた複数ページの和が全件と一致し、重複も欠落も
    無い（以前は走査順依存＝ページ跨ぎで重複/欠落し得た）。"""
    from sherpa import doc_ledger as DL

    names = [f"p/{n}.md" for n in ["c", "a", "e", "b", "d", "g", "f"]]
    rows = [{"name": n, "branch": "docs", "doctype": "設計書"} for n in names]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)

    collected: list[str] = []
    offset = 0
    for _ in range(10):   # 全件を確実に踏破できる十分な上限（無限ループ防止のガード）
        res, _, _, _ = A.run_tool("list_docs", {"path_prefix": "p", "limit": 3, "offset": offset}, "v1", None)
        collected.extend(d["rel_path"] for d in res["docs"])
        if not res["truncated"]:
            assert res["next_offset"] is None
            break
        offset = res["next_offset"]
    assert collected == sorted(names)                       # 昇順固定・重複/欠落なし
    assert len(collected) == len(set(collected)) == len(names)


def test_list_docs_doctype_and_state_filters_are_case_insensitive_exact_match(monkeypatch):
    """doctype/state は台帳の値との完全一致・大文字小文字は無視（部分一致にしない）。"""
    from sherpa import doc_ledger as DL

    rows = [
        {"name": "p/a.cbl", "branch": "source", "doctype": "cobol", "state": "ready"},
        {"name": "p/b.cbl", "branch": "source", "doctype": "cobol", "state": "unreadable"},
        {"name": "p/c.md", "branch": "docs", "doctype": "設計書", "state": "ready"},
    ]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)

    res, _, _, _ = A.run_tool("list_docs", {"doctype": "COBOL"}, "v1", None)
    assert {d["rel_path"] for d in res["docs"]} == {"p/a.cbl", "p/b.cbl"}

    res2, _, _, _ = A.run_tool("list_docs", {"state": "READY"}, "v1", None)
    assert {d["rel_path"] for d in res2["docs"]} == {"p/a.cbl", "p/c.md"}

    res3, _, _, _ = A.run_tool("list_docs", {"doctype": "cobol", "state": "unreadable"}, "v1", None)
    assert {d["rel_path"] for d in res3["docs"]} == {"p/b.cbl"}

    res4, _, _, _ = A.run_tool("list_docs", {"doctype": "excel"}, "v1", None)   # 該当なし＝0件（例外にしない）
    assert res4["docs"] == [] and res4["count"] == 0


def test_list_docs_truncated_and_next_offset_at_boundaries(monkeypatch):
    """truncated = offset + len(docs) < count・next_offset = offset + len(docs)（truncated のときだけ）。"""
    from sherpa import doc_ledger as DL

    names = [f"p/{i:02d}.md" for i in range(5)]
    rows = [{"name": n, "branch": "docs", "doctype": "設計書"} for n in names]
    monkeypatch.setattr(DL, "documents_for", lambda world, **kw: rows)

    # ちょうど全件に届く（limit==count）: 打ち切りなし
    res, _, _, _ = A.run_tool("list_docs", {"path_prefix": "p", "limit": 5}, "v1", None)
    assert res["count"] == 5 and res["truncated"] is False and res["next_offset"] is None

    # 1件だけ超える（limit<count）: 打ち切りあり・next_offset は返した件数分進む
    res2, _, _, _ = A.run_tool("list_docs", {"path_prefix": "p", "limit": 4}, "v1", None)
    assert len(res2["docs"]) == 4 and res2["truncated"] is True and res2["next_offset"] == 4

    # next_offset をそのまま渡すと残り1件で打ち切りが解消する
    res3, _, _, _ = A.run_tool("list_docs", {"path_prefix": "p", "limit": 5, "offset": res2["next_offset"]},
                               "v1", None)
    assert len(res3["docs"]) == 1 and res3["truncated"] is False and res3["next_offset"] is None


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


def test_es_search_filters_sensitive_named_hits():
    """台帳 #80: 秘匿名（`text_kind.is_sensitive`）導入前に索引化された ES ヒットを、実在チェックを
    通っていても本文付きで返さない・件数（`docs`/`hits`）からも外す（`credentials.xlsx` の名前規約）。"""
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "real.md", "line": 1, "text": "x", "span": [1, 1], "ext": ".md"},
        {"doc_id": "credentials.xlsx", "line": 2, "text": "secret", "span": [2, 2], "ext": ".xlsx"}], None)
    documents.world_rel_set = lambda world, **kw: {"real.md", "credentials.xlsx"}   # 両方とも実在（除外は秘匿名のみが理由）
    try:
        res, docs, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        ids = {h["doc_id"] for h in res["hits"]}
        assert ids == {"real.md"} and "credentials.xlsx" not in docs
        assert len(res["hits"]) == 1
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


def test_es_search_locator_hint_combined_text_is_not_clipped():
    """LLM 向け本文は文字数で切らない: 長い本文＋位置ヒントが丸ごと残る（quote だけ 500 字）。"""
    from sherpa import documents, es_index
    o_search, o_relset = es_index.search, documents.world_rel_set
    es_index.search = lambda world, q, scope_paths=None, k=20, layer=None, **kw: ([
        {"doc_id": "c.xlsx", "line": None, "text": "あ" * 1490, "ext": ".xlsx",
         "locator": {"sheet": "明細", "cell_range": "B1"}}], None)
    documents.world_rel_set = lambda world, **kw: {"c.xlsx"}
    try:
        res, _, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
        text = res["hits"][0]["text"]
        assert text.startswith("あ" * 1490) and "明細" in text and "text_truncated" not in res["hits"][0]
        assert len(cites[0]["quote"]) == 500
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
    monkeypatch.setattr(RT, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)
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


def test_graph_neighbors_tool_view_includes_directed_edges(monkeypatch):
    """S3c（裁定2026-09-11）: `view` の各近傍は `evidence.edges`（`lens_service.neo4j_related` が
    返す `{type, from, to, doc}`）を素通しする。`doc` が無い辺は `doc` キー自体を省く（`_card_edges_view`
    は既知キーだけ・値がある物だけ写す）。edges が無い/空のカードでも `edges: []` で落ちない。"""
    monkeypatch.setattr(A, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)
    monkeypatch.setattr(RT, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)
    from sherpa import lens_service
    fake = [
        {"name": "BILLINGJOB", "label": "Module", "category": "プログラム", "role": "実装",
         "distance": 1, "path": ["請求処理", "BILLINGJOB"],
         "evidence": {"edges": [
             {"type": "COPIES", "from": "請求処理", "to": "BILLINGJOB", "doc": "4期/src/請求処理.cbl"},
             {"type": "INVOKES", "from": "BILLINGJOB", "to": "SUBRTN1"},   # doc 無し（言及以外の辺は doc を持たないこともある）
         ], "grep": []}},
        {"name": "no-edges", "label": "Module", "category": "プログラム", "role": "実装",
         "distance": 1, "path": [], "evidence": {"edges": [], "grep": []}},
    ]
    orig = lens_service.neighbor_cards
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    try:
        res, docs, cites, cards = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
        by_name = {n["name"]: n for n in res["neighbors"]}
        assert by_name["BILLINGJOB"]["edges"] == [
            {"type": "COPIES", "from": "請求処理", "to": "BILLINGJOB", "doc": "4期/src/請求処理.cbl"},
            {"type": "INVOKES", "from": "BILLINGJOB", "to": "SUBRTN1"},
        ]
        assert by_name["no-edges"]["edges"] == []
        assert by_name["BILLINGJOB"]["path"] == ["請求処理", "BILLINGJOB"]   # path は従来どおり残る
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


# ===== 未登録拡張子のソース到達可能性（ソースが神様の穴を塞ぐ・変更B/C）=====

def test_unregistered_ext_reachable_grep_read_and_es_eligible(monkeypatch, tmp_path):
    """`.zzz`（未登録拡張子のプレーンテキスト）が grep（`ripgrep_search`）・read_around（精読）の
    両方から到達でき、`corpus_docs.world_documents`（ES 索引の材料）が doctype 付きで確定させる
    ——拡張子の許可リストではなく `classify_document`/`reachable_as_text` の内容判定で可否が決まる。"""
    world = "unreg-ext-reach"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "notes/app.zzz": "NEEDLE_ZZZ ここに業務ルールを書く\nline two\n",
    })
    res, docs, cites, _ = A.run_tool("ripgrep_search", {"query": "NEEDLE_ZZZ"}, world, None)
    assert res["hits"], res
    hit = res["hits"][0]
    assert hit["doc_id"] == "notes/app.zzz"
    assert "notes/app.zzz" in docs
    assert cites and cites[0]["doc_id"] == "notes/app.zzz"

    r2, d2, _, _ = A.run_tool("read_around", {"doc_id": "notes/app.zzz", "line": hit["line"]}, world, None)
    assert "error" not in r2, r2
    assert "NEEDLE_ZZZ" in r2["text"]
    assert "notes/app.zzz" in d2

    from sherpa import corpus_docs
    entry = next(d for d in corpus_docs.world_documents(world) if d["name"] == "notes/app.zzz")
    assert entry.get("state") != "unreadable"
    assert entry.get("doctype") is not None            # ES 索引の除外条件（doctype 無し）に該当しない


def test_binary_unregistered_ext_unreachable_everywhere(monkeypatch, tmp_path):
    """`.bin`（バイナリ・NUL バイト含む）は grep・read_around・台帳のどこからも到達できない
    （内容が実質バイナリ＝`reachable_as_text` が False）。"""
    world = "unreg-ext-binary"
    binary_content = b"\x00\x01\x02BINARYDATA\xff\xfe\x00" * 20
    _isolate_world_kb(monkeypatch, tmp_path, world, {"blob.bin": binary_content})

    res, docs, _, _ = A.run_tool("ripgrep_search", {"query": "BINARYDATA"}, world, None)
    assert not res.get("hits")
    assert not docs

    r2, d2, _, _ = A.run_tool("read_around", {"doc_id": "blob.bin", "line": 1}, world, None)
    assert "error" in r2
    assert not d2

    from sherpa import corpus_docs
    assert not any(d["name"] == "blob.bin" for d in corpus_docs.world_documents(world))   # 台帳にも載らない


def test_sensitive_dotenv_unreachable_and_not_logged(monkeypatch, tmp_path, caplog):
    """`.env`（秘匿名）は grep・read_around のどちらからも到達できず、ログにも本文（秘密の値）が
    出ない——`text_kind.is_sensitive` の判定で常に拒否される（拡張子の許可リストが安全を担わない
    契約・変更A）。"""
    world = "unreg-ext-dotenv"
    secret_value = "SUPER_SECRET_TOKEN_VALUE_XYZ"
    _isolate_world_kb(monkeypatch, tmp_path, world, {".env": f"API_KEY={secret_value}\n"})

    with caplog.at_level("WARNING"):
        res, docs, _, _ = A.run_tool("ripgrep_search", {"query": "SUPER_SECRET"}, world, None)
        assert not res.get("hits")
        assert not docs
        r2, d2, _, _ = A.run_tool("read_around", {"doc_id": ".env", "line": 1}, world, None)
        assert "error" in r2
        assert not d2
    assert secret_value not in caplog.text


def test_registered_extension_regression_still_reachable(monkeypatch, tmp_path):
    """既存の登録拡張子（`.cbl`）は本変更後も従来どおり grep・read_around から読める（回帰なし）。"""
    world = "unreg-ext-regress-cbl"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "PROG.cbl": "       PROGRAM-ID. PROG.\n       NEEDLE_CBL line.\n",
    })
    res, docs, _, _ = A.run_tool("ripgrep_search", {"query": "NEEDLE_CBL"}, world, None)
    assert res["hits"], res
    r2, d2, _, _ = A.run_tool(
        "read_around", {"doc_id": "PROG.cbl", "line": res["hits"][0]["line"]}, world, None)
    assert "error" not in r2, r2
    assert "PROG.cbl" in d2


# ===== verify_citation: 実在するが本文が読めない doc は exists=True（変更C）=====


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

# ===== BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・管理者設定への昇格） =====
# コード既定は精度優先値（262144）。env フォールバックは撤去済み（ENV-CLEAN・2026-09-03）
# ——このモジュール定数は固定値なので、値そのものを1回ピン留めするだけでよい（settings 段は
# `effective_tool_result_max_bytes`（`store.get_system_settings` 経由）で固定する）。

def test_tool_result_max_bytes_code_default():
    assert A.TOOL_RESULT_MAX_BYTES == 262144


# ---- settings > コード既定（2段）------------------------------------------------------------

def test_effective_tool_result_max_bytes_code_default_when_settings_unset(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    assert A.effective_tool_result_max_bytes() == A.TOOL_RESULT_MAX_BYTES


def test_effective_tool_result_max_bytes_falls_back_to_module_constant_when_settings_unset(monkeypatch):
    """settings 未設定時のフォールバックはモジュール定数 `TOOL_RESULT_MAX_BYTES`——直接
    monkeypatch して確認する（`MAX_TOOLS_PER_TURN` 等、既存の run-level テストと同じ流儀）。"""
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 99000)
    assert A.effective_tool_result_max_bytes() == 99000


def test_effective_tool_result_max_bytes_settings_overrides_env(monkeypatch):
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_per_result": 5000})
    assert A.effective_tool_result_max_bytes() == 5000


def test_effective_tool_result_max_bytes_settings_out_of_range_falls_back(monkeypatch):
    """範囲外（1024〜8MiB 外）の settings 値は「不正な保存値」として env/コード既定へ倒す
    （fail-safe・PUT 側の Field(ge,le) を通常はすり抜けないが、DB 直接編集等の破損値でも
    落ちないことを固定する）。"""
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 99000)
    for bad in (0, -1, 8 * 1024 * 1024 + 1, "not-an-int"):
        monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_per_result": bad})
        assert A.effective_tool_result_max_bytes() == 99000, bad


def test_effective_tool_result_max_bytes_settings_read_failure_falls_back(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 99000)
    monkeypatch.setattr(store, "get_system_settings", _boom)
    assert A.effective_tool_result_max_bytes() == 99000


# ---- run 開始時に1回だけ解決するスナップショット契約 -----------------------------------------

# ===== 窓連動の撤去（旧 BUDGET-2・2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4 で導入・
# `docs/archive/2026-09-22-Codex経路の精度・網羅性と費用の改善.md` で撤去）=====
# `effective_tool_result_max_bytes` は `provider`/`model`/`ollama_base_url` を受け取っても値の解決には使わない
# （利用者裁定「AI が持つ文脈窓を Sherpa が制限しない」）——渡す・渡さないで結果が変わらないことを
# 固定する。呼び出し元（openai_style）が引き続きこれらを渡す配線自体
# （互換のためだけの引数）は下のセクションで固定する。

def test_effective_tool_result_max_bytes_ignores_provider_and_model():
    """`provider`/`model` を渡しても渡さなくても結果は同じ（コード既定/管理画面の基準値のみで
    決まる）——旧実装はシード表に載っている openai/gpt-4o-mini で窓由来の上限まで縮んでいた。"""
    assert A.effective_tool_result_max_bytes({}) == A.TOOL_RESULT_MAX_BYTES
    assert A.effective_tool_result_max_bytes(
        {}, provider="openai", model="gpt-4o-mini") == A.TOOL_RESULT_MAX_BYTES


def test_effective_tool_result_max_bytes_large_admin_setting_not_clipped_by_any_model():
    """管理画面の基準値を大きく設定していれば、モデルが小窓であっても、そのまま使われる
    （旧実装の min() 方式は撤去済み）。"""
    sysset = {"agentic_budget_per_result": 5_000_000}
    assert A.effective_tool_result_max_bytes(sysset, provider="openai", model="gpt-4o-mini") == 5_000_000


def test_run_tool_explicit_tool_result_max_bytes_overrides_module_default(monkeypatch, tmp_path):
    """`run_tool(tool_result_max_bytes=...)` は明示指定の値でクリップする（値の出所（settings/env/
    コード既定のどれで解決されたか）に関わらず `run_tool` 自体は受け取った実効値を使うだけ、という
    契約を固定する）。モジュール既定を大きく設定していても、明示指定の小さい値でクリップされる。"""
    world = "read-doc-explicit-budget-world"
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 10_000_000)   # 明示指定が勝つことを示すため大きく設定
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 10_000_000)
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.md": "x" * 5000})
    res, _, _, _ = A.run_tool("read_doc", {"doc_id": "big.md"}, world, None, tool_result_max_bytes=200)
    assert "error" not in res, res
    assert res.get("text_truncated") is True
    assert len(res["text"].encode("utf-8")) <= 200


# ===== secRV FIX-1（2026-07-19・拒否ツール結果のバイト迂回） =====

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
        assert res["truncated"] is True and res["count"] == 1000   # 部分集合であることと総数を返す
    finally:
        lens_service.neighbor_cards = orig


def test_graph_neighbors_not_truncated_when_within_limit():
    from sherpa import lens_service
    fake = [{"name": f"c{i}", "role": "実装", "category": "プログラム", "distance": 1,
            "path": [], "evidence": {}} for i in range(3)]
    orig = lens_service.neighbor_cards
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    try:
        res, *_ = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
        assert "truncated" not in res and "count" not in res
    finally:
        lens_service.neighbor_cards = orig


def _graph_neighbors_count_script() -> str:
    """`MAX_HITS` が何であっても `graph_neighbors` のカード件数上限
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


def test_graph_neighbors_count_is_fixed_and_grep_max_hits_env_is_ignored():
    out = json.loads(FI.run_script(_graph_neighbors_count_script(), env={"SHERPA_GREP_MAX_HITS": "1"}))
    assert out["max_hits"] == 45               # grep/es 側のコード既定（env は読まない）
    assert out["graph_cards_max"] == 30        # graph_neighbors 側は無関係・従来の 30 のまま
    assert out["n_cards"] == 30
    assert out["n_view"] == 30


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
    monkeypatch.setattr(RT, "_safe_doc_path", lambda w, doc_id, layer=None: (kb_world, "swapped_doc.md", swapped))
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
    monkeypatch.setattr(RT, "_safe_doc_path", lambda w, doc_id, layer=None: (kb_world, "sub/doc.md", swapped_target))
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


# ===== MAX_HITS / READ_WINDOW（コード既定・管理画面の基準値が未設定のときに使う） =====
# 環境変数 `SHERPA_GREP_MAX_HITS`／`SHERPA_READ_WINDOW` は実行時に読まない（画面だけが正）。
# import 時の値は実プロセスを新規に起こして観測する（`_fresh_import` 参照）。

def _max_hits_read_window_env_script() -> str:
    return (
        "import json\n"
        "import sherpa.agentic_search as m\n"
        "print(json.dumps({'max_hits': m.MAX_HITS, 'read_window': m.READ_WINDOW}))\n"
    )


def test_max_hits_read_window_are_code_defaults_and_ignore_env():
    for env in ({"SHERPA_GREP_MAX_HITS": None, "SHERPA_READ_WINDOW": None},
                {"SHERPA_GREP_MAX_HITS": "100", "SHERPA_READ_WINDOW": "80"}):
        out = json.loads(FI.run_script(_max_hits_read_window_env_script(), env=env))
        assert out["max_hits"] == 45
        assert out["read_window"] == 60


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
    """`_PARAMS_READ` の window 説明文（モデルへの通知）は実際の `READ_WINDOW`（60）を埋め込んでおり、
    `window` 省略時の `run_tool("read_around", ...)` の実挙動（返却行数）とも一致する
    （説明文とツールの実配線を `run_tool()` 越しに固定・同じ式を2箇所に書いて re-derive しない）。
    環境変数 `SHERPA_READ_WINDOW` は影響しない。"""
    out = json.loads(FI.run_script(_read_around_run_tool_script(200, 100),
                                   env={"SHERPA_READ_WINDOW": "80"}))
    assert out["read_window"] == 60
    assert "60" in out["desc"]
    assert out["n_lines"] == 2 * 60 + 1   # line=100・window=60 は境界に掛からない


def test_read_around_window_ceiling_is_200_for_explicit_window():
    """read_around の LLM 入力窓ハード上限は 200（明示 `window` 引数が大きくても 200 で頭打ち）。
    `run_tool()` を実際に呼び、返却行数から上限を観測する（式を再計算しない）。"""
    out = json.loads(FI.run_script(_read_around_run_tool_script(700, 350, window_arg=300), env={}))
    assert out["read_window"] == 60
    assert out["n_lines"] == 2 * 200 + 1


# ===== secRV FIX-H（2026-07-19・実行 allowlist の非対称）: メイン経路も offered_names で制限 =====

# ---- RV MEDIUM（2026-07-03再検証）: 途中停止（stop_event）は各ターン発行前に確認 ----

# ===== secRV LOW-E（2026-07-18 再検証）: ノード yield 直後の stop_event 再確認 =====
# generator は yield で呼び出し元へ制御を返す＝その間に停止要求が来ても、是正前は再開後に
# run_tool（実 I/O）を無条件に1件実行してしまっていた。ノード yield 直後・ask_user 分岐/run_tool の
# 直前にも再確認することで、この窓を塞ぐ（3 dialect 共通）。

# ===== secRV MED-2（2026-07-18・ローカルサブの生成物が公式 UI/trace に露出）: サブ経路ノードの固定文言 =====

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
    を SHERPA_MCP_ARMS/SHERPA_MCP_LEGACY_BACKEND として渡すことで、サブプロセス側は env フォールバックだけで
    親と同じ実効値に一致する（list_docs の convertible 判定が grep とずれる、といった不一致を防ぐ）。
    SHERPA_TESSERACT_BIN の透過は tesseract の `ocr` アーム撤去（2026-07-08）に伴い削除した。"""
    from sherpa import agents, store
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"arms_enabled": ["ooxml"], "legacy_backend": "libreoffice"})
    env = agents._mcp_env("v1", None)
    assert env["SHERPA_MCP_ARMS"] == "ooxml"                       # 実効アーム（system_settings 反映済）
    assert env["SHERPA_MCP_LEGACY_BACKEND"] == "libreoffice"       # 実効バックエンド（system_settings 反映済）
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


def test_safe_doc_path_rejects_sensitive_office_original_name(monkeypatch, tmp_path):
    """Office/画像（`is_office` 分岐）は `classify_document` を経由しないため、
    `credentials.xlsx`/`id_rsa.docx` のような秘匿名は派生MDが実在しても read_around で開けない
    （`_safe_doc_path` が doc_id そのもの＝原本名で `text_kind.is_sensitive` を独立に判定する）。
    非秘匿の Office は従来どおり開ける。"""
    der = tmp_path / "md"
    der.mkdir(parents=True, exist_ok=True)
    (der / "credentials.xlsx.md").write_text("SECRET", encoding="utf-8")
    (der / "id_rsa.docx.md").write_text("SECRET", encoding="utf-8")
    (der / "normal.docx.md").write_text("normal", encoding="utf-8")
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(A.worlds, "derived_rag_dir", lambda w: tmp_path / "rag-empty")

    assert A._safe_doc_path("w", "credentials.xlsx") is None
    assert A._safe_doc_path("w", "id_rsa.docx") is None
    resolved = A._safe_doc_path("w", "normal.docx")
    assert resolved is not None
    assert resolved[2].name == "normal.docx.md"


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


# ===== SC-6e: 検索経路トグル（grep/fulltext(ES)/graph）=====


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


# ===== GLOB-1: glob_search（ファイル名/パスのグロブ検索）は grep 軸に同居 =====


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


def test_can_ask_helper_detects_confirm_id_resend():
    """Med-1: `agents._can_ask` は依頼に「確認ID:」があれば False（回答再送＝再質問しない）。"""
    from sherpa import agents as AG
    assert AG._can_ask("税率を変えたら夜間バッチが落ちる？") is True
    assert AG._can_ask("選択: 対象範囲\n確認ID: confirm-abcd\n元の依頼: …") is False
    assert AG._can_ask("確認ID：ask-0011\n選択: 影響") is False   # 全角コロンも検出


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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 65536)
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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 65536)
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
    monkeypatch.setattr(RT, "_READ_AROUND_FILE_CAP_BYTES", 50)
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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 1000)
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
    monkeypatch.setattr(RT, "_READ_AROUND_FILE_CAP_BYTES", 50)
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

def test_tool_hit_count_read_doc_counts_lines_returned_this_call():
    assert A._tool_hit_count("read_doc", {"start_line": 1, "end_line": 40, "total_lines": 120}) == 40
    assert A._tool_hit_count("read_doc", {"start_line": 41, "end_line": 41, "total_lines": 120}) == 1


def test_tool_hit_count_doc_outline_uses_count_field():
    assert A._tool_hit_count("doc_outline", {"count": 7, "headings": [], "total_lines": 50}) == 7


def test_tool_hit_count_new_tools_none_on_error():
    assert A._tool_hit_count("read_doc", {"error": "boom"}) is None
    assert A._tool_hit_count("doc_outline", {"error": "boom"}) is None


def test_tool_hit_count_compare_documents_header_excluded_positionally():
    """diff の本文（3行目以降）に "+++"/"---" で始まる行があっても、diff 自身のヘッダー
    （先頭2行だけ）と誤認して除外しない——ヘッダー除外は内容一致でなく位置で行う契約を固定する
    （`investigation_state.py` の compare_documents 抜粋と同じ契約）。"""
    normal_diff = "--- a\n+++ b\n+新しい行\n-古い行\n 変化なし行"
    assert A._tool_hit_count("compare_documents", {"status": "comparable", "diff": normal_diff}) == 2

    tricky_diff = "--- a\n+++ b\n+++valid content+++\n---also valid---"
    assert A._tool_hit_count("compare_documents", {"status": "comparable", "diff": tricky_diff}) == 2


# ---- EV-0（拡張設計 §4.4）: read_doc も read_around と同じく「精読済み」に載る ----

# ==== EXT-2（拡張設計 §4.3）: 機械検証（verify_citation） ====

_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 実在ファイル・1行目 "# 障害記録"


# ==== EXT-2/EV-0: read_around を通した doc_id だけが "verified_docs" に載る ====

# ==== EXT-3（拡張設計 §3）: 評価フェーズ（Observation → Evaluation → Next Action） ====

def _eval_tool_call(call_id: str, status: str, next_action: str, reason: str = "理由") -> dict:
    args = json.dumps({"status": status, "reason": reason, "next_action": next_action}, ensure_ascii=False)
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": call_id, "function": {"name": "submit_evaluation", "arguments": args}}]}}]}


# ==== stop_reason の細分化（出力上限打ち切り／内容フィルタ打ち切りを "no_tool_calls" と区別する） ====
# 正典（拡張設計 §4.4）の EV-0 自然完了 allowlist は帰属呼び出しの可否だけでなく、UI の
# 「終了理由」（stop_reason）にも同じ判別を反映する——ツール未呼び出しで応答が返っても、
# 実際には出力上限／内容フィルタで打ち切られていたなら「自然終了」（no_tool_calls）と偽らない。

# ==== RV6是正: 最終本文を実際に生成した呼び出しの finish_reason で stop_reason を再分類する ====
# 初回ドラフト時点で決めた stop_reason（no_tool_calls/evaluation_sufficient/evaluation_blocked/
# turns_exhausted）は、直後の再合成（citation 検証で落ちた場合）や最終合成（turns_exhausted/
# 評価早期終了向けの追加呼び出し）で finish_reason が変わりうることを反映していなかった。

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
    """read_around でも単一巨大行の安全弁が効く: `_READ_LINE_MAX_BYTES`（2MiB）を超える
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

# ===== L4c: 親返し（検索は細かく・回答には文脈を・§3.3/§3.4）=====
# es_search 限定・常時 ON（TOGGLE-RM・2026-09-03 でグローバル切替トグル `SHERPA_ES_PARENT_RETURN`
# を撤去）。ヒットを doc_id で束ね、予算内なら rag.md の領域(P2)を返し、超える場合は
# 子チャンク（chunk・最低保証）のまま。**全文(P3)段は無い**（決定 2026-09-21・調査の検索は場所を
# 広く見つけるためのもので、文書まるごとを返すのはその設計思想に反する。全文が要るなら
# read_doc で個別に取得する）。表示側（`rag_parent_return.py`・chat/search 用の出典カード）は
# 別実装で P3/P2/chunk の3段のまま——ここで検証するのは調査側（`agentic_search`）のみ。

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


def test_parent_return_no_full_tier_region_or_chunk_by_size(monkeypatch, tmp_path):
    """全文(P3)段は無い（決定 2026-09-21）: サイズを操作した3文書でも tier は region か chunk にしか
    ならない（§3.4 の配分規則＝ベストスコア順に region→chunk を試す）。`region.docx`（旧名）は
    複数チャンクの rag.md のうち親グループに入る2チャンクだけを含み、対象外の領域（pad1）は
    含まない——`chunk_ids_for_parent` が対象と答えたチャンクだけをアンカー単位で集める P2 の
    仕組みそのもの。`many_chunks.docx` は rag.md に2チャンクあるが親グループには自分自身
    （ヒットしたチャンク）しか含まれない場合の固定——**全文なら含まれたはずの無関係な後続チャンク
    （cf2）が region では含まれない**ことを検証する（P3 撤去の実害＝無関係な内容を混ぜないことの
    直接証拠）。"""
    world = "parent-return-tiers-world"
    single_chunk_md = "<!-- chunk:cf1 -->\n" + "F" * 200 + "\n\n<!-- chunk:cf2 -->\n" + "Z" * 5000 + "\n"
    region_md = ("<!-- chunk:cr1 -->\n" + "R" * 100 + "\n\n"
                "<!-- chunk:cr2 -->\n" + "R" * 100 + "\n\n"
                "<!-- chunk:pad1 -->\n" + "P" * 5000 + "\n")
    chunk_md = ("<!-- chunk:cc1 -->\n" + "C" * 50 + "\n\n"
               "<!-- chunk:cc2 -->\n" + "C" * 5000 + "\n")
    hits = [
        {"doc_id": "many_chunks.docx", "text": "F", "ext": ".docx", "chunk_id": "cf1", "parent_id": "pf", "score": 3.0},
        {"doc_id": "region.docx", "text": "R", "ext": ".docx", "chunk_id": "cr1", "parent_id": "pr", "score": 2.0},
        {"doc_id": "chunk.docx", "text": "C", "ext": ".docx", "chunk_id": "cc1", "parent_id": "pc", "score": 1.0},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {
        "many_chunks.docx": single_chunk_md, "region.docx": region_md, "chunk.docx": chunk_md})

    def fake_chunk_ids_for_parent(w, doc_id, parent_ids, limit=5000):
        if doc_id == "many_chunks.docx":
            return ["cf1"]                  # 親グループは自分自身のチャンクだけ（cf2 は対象外）
        if doc_id == "region.docx":
            return ["cr1", "cr2"]
        if doc_id == "chunk.docx":
            return ["cc1", "cc2"]
        return []

    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", fake_chunk_ids_for_parent)
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 1000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 1000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert {h["tier"] for h in res["hits"]} <= {"region", "chunk"}   # "full" は絶対に出ない
    assert by_doc["many_chunks.docx"]["tier"] == "region"
    assert "F" * 200 in by_doc["many_chunks.docx"]["text"]
    assert "Z" * 5000 not in by_doc["many_chunks.docx"]["text"]     # 親グループ外（cf2）は含まない
    assert by_doc["region.docx"]["tier"] == "region"
    assert "R" * 100 in by_doc["region.docx"]["text"]
    assert "P" * 5000 not in by_doc["region.docx"]["text"]      # 対象外の領域（pad1）は含まない
    assert by_doc["chunk.docx"]["tier"] == "chunk"
    assert by_doc["chunk.docx"]["text"] == "C"                  # 最低保証（子チャンクの結合＝1件分）
    # ベストスコア順（決定的な配分順）で並ぶ。
    assert [h["doc_id"] for h in res["hits"]] == ["many_chunks.docx", "region.docx", "chunk.docx"]


def test_parent_return_minimum_guarantee_lower_score_doc_survives(monkeypatch, tmp_path):
    """§3.4「最低保証」: スコア1位の文書が領域(region)で予算の残りを使い切っても、2位の文書の
    子チャンク（最低保証＝baseline）は消えない（黙って空文字/欠落にならない）。"""
    world = "parent-return-minguard-world"
    doc1_md = "<!-- chunk:c1 -->\n" + "A" * 400 + "\n"          # 1位: 予算内に収まる領域（単一チャンク）
    doc2_md = "<!-- chunk:c2 -->\n" + "B" * 50000 + "\n"        # 2位: 領域も予算を大幅に超える
    hits = [
        {"doc_id": "top.docx", "text": "top-baseline", "ext": ".docx",
         "chunk_id": "c1", "parent_id": "p1", "score": 2.0},
        {"doc_id": "second.docx", "text": "second-baseline", "ext": ".docx",
         "chunk_id": "c2", "parent_id": "p2", "score": 1.0},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits,
                               {"top.docx": doc1_md, "second.docx": doc2_md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: [])
    # baseline 合計（"top-baseline"+"second-baseline"）＋ doc1 の領域アップグレード分だけが入る予算
    # （doc2 に回せる余剰は残らない設計値。per_doc_cap は budget_for_rag//2 で余裕を持たせ、
    # ここでは共有予算の枯渇だけを検証対象にする）。
    baseline_total = len("top-baseline".encode("utf-8")) + len("second-baseline".encode("utf-8"))
    delta_doc1 = len(doc1_md.encode("utf-8")) - len("top-baseline".encode("utf-8"))
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", baseline_total + delta_doc1)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", baseline_total + delta_doc1)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["top.docx"]["tier"] == "region"
    assert by_doc["top.docx"]["text"] == "A" * 400
    assert by_doc["second.docx"]["tier"] == "chunk"
    assert by_doc["second.docx"]["text"] == "second-baseline"   # 消えない・空にならない
    # 共有予算の枯渇が理由（per_doc_cap 側の頭打ちではない）＝新設フラグは立たない。
    assert "text_truncated" not in by_doc["second.docx"]


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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 5000)

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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 5000)

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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 5000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    legacy_entries = [h for h in res["hits"] if h["doc_id"] == "legacy.md"]
    assert legacy_entries == [{"doc_id": "legacy.md", "line": 7, "text": "legacy本文"}]
    assert "tier" not in legacy_entries[0] and "chunks" not in legacy_entries[0]


def test_parent_return_redacts_region_text(monkeypatch, tmp_path):
    """redaction（`_redact`）は領域(P2)の本文にも効く——ES ヒット断片だけでなく、rag.md から直接
    読んだ領域テキストも秘密パターンを伏せて返す（rag.md を未マスクで LLM に渡さない）。"""
    world = "parent-return-redact-world"
    secret = "sk-1234567890ABCDEFGHIJ"                     # `_SECRET_RE` の sk- パターンに一致
    md = f"<!-- chunk:c1 -->\nAPIキー: {secret}\n"
    hits = [{"doc_id": "a.docx", "text": "本文", "ext": ".docx",
            "chunk_id": "c1", "parent_id": "p1", "score": 1.0}]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"a.docx": md})
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 5000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 5000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    assert res["hits"][0]["tier"] == "region"
    assert secret not in res["hits"][0]["text"]
    assert "[REDACTED]" in res["hits"][0]["text"]


def test_parent_return_never_returns_full_tier_even_with_huge_budget(monkeypatch, tmp_path):
    """全文(P3)段は無い（決定 2026-09-21）の直接固定: 共有予算・1文書あたりの上限のどちらも
    ゆとりがあり、旧 P3 なら確実に「全文」を選んでいたはずの条件下でも `tier` は `"region"`
    にしかならない（`"full"` という文字列自体が出力に一切現れない）。"""
    world = "parent-return-no-full-ever-world"
    md = "<!-- chunk:c1 -->\n" + "A" * 100 + "\n"
    hits = [{"doc_id": "a.docx", "text": "本文A", "ext": ".docx",
            "chunk_id": "c1", "parent_id": "p1", "score": 1.0}]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"a.docx": md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: ["c1"])
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 1_000_000)     # 潤沢な予算（旧 P3 なら確実に採用）
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 1_000_000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    assert res["hits"][0]["tier"] == "region"
    assert "full" not in json.dumps(res)


def test_parent_return_per_doc_cap_stops_one_doc_eating_shared_budget(monkeypatch, tmp_path):
    """新設: 領域(P2)にも1文書あたりの上限（ヒット単位のクリップと同じ「総予算を件数で均等割り・
    下限 `_HIT_TEXT_MIN_BYTES`」規則）を掛ける。スコア1位の文書の領域が単独ではこの上限を超える
    （が、共有の残り予算にはまだ余裕がある）とき、旧実装なら1位が予算を独占して2位の領域は
    得られなかった。新実装では1位がこの上限に阻まれて chunk へ留まり（`text_truncated: true` を
    申告）、消費されなかった共有予算のおかげで2位は領域を得られる。"""
    world = "parent-return-per-doc-cap-world"
    top_md = "<!-- chunk:c1 -->\n" + "A" * 2500 + "\n"        # 単独で per_doc_cap(2000) を超える
    second_md = "<!-- chunk:c2 -->\n" + "B" * 1500 + "\n"     # per_doc_cap(2000) 以内に収まる
    hits = [
        {"doc_id": "top.docx", "text": "top-chunk", "ext": ".docx",
         "chunk_id": "c1", "parent_id": "p1", "score": 2.0},
        {"doc_id": "second.docx", "text": "second-chunk", "ext": ".docx",
         "chunk_id": "c2", "parent_id": "p2", "score": 1.0},
    ]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits,
                               {"top.docx": top_md, "second.docx": second_md})
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent",
                        lambda w, doc_id, parent_ids, limit=5000: (["c1"] if doc_id == "top.docx" else ["c2"]))
    # budget_for_rag=4000・doc 数2件 → per_doc_cap=2000（_HIT_TEXT_MIN_BYTES=512 の床より大きい）。
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_BYTES", 4000)
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 4000)

    res, _docs, _cites, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    by_doc = {h["doc_id"]: h for h in res["hits"]}
    assert by_doc["top.docx"]["tier"] == "chunk"              # 1文書あたりの上限で領域を得られない
    assert by_doc["top.docx"]["text"] == "top-chunk"          # 最低保証は消えない
    assert by_doc["top.docx"]["text_truncated"] is True       # 上限で切られたことを申告する
    assert by_doc["second.docx"]["tier"] == "region"          # 1位の独占が無くなり2位は領域を得る
    assert by_doc["second.docx"]["text"] == "B" * 1500
    assert "text_truncated" not in by_doc["second.docx"]
    # `tool_result_clipped` 計測が拾う最上位フラグにも合流する（`_BYTE_CLIP_TOOLS` に es_search 含む）。
    assert res["text_truncated"] is True


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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 2000)

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
    monkeypatch.setattr(RT, "TOOL_RESULT_MAX_BYTES", 200)

    tracemalloc.start()
    try:
        res, _, _, _ = A.run_tool("es_search", {"query": "q"}, world, None)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert res["hits"][0]["tier"] == "chunk"
    assert res["hits"][0]["text"] == "本文"
    assert peak < 10 * 1024 * 1024


# ===== S3b: 原本読取ツール（`docs/archive/2026-09-10-Codex原本直読と調査スキル.md` §2-9）=====
# `_safe_original_path` の封じ込め（`_safe_doc_path` と同じ検査項目を Office/PDF は世界 root の
# 原本へ解決する版で共有）と、`run_tool` への配線（xlsx/docx/pptx/pdf/file_head）を検証する。
# `doc_readers.py` 自体の入出力契約は tests/unit/test_doc_readers.py が担う——ここでは
# 「doc_id→実パス解決」と「run_tool 経由の docs/redaction/layer」だけを見る。

def _write_xlsx(path: pathlib.Path, cell_value: str = "hello") -> None:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = cell_value
    wb.save(path)


def test_safe_original_path_confinement_rejects_traversal_absolute_and_missing(monkeypatch, tmp_path):
    world = "s3b-confine"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"a.xlsx": b""})
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: tmp_path / "kb" / world)
    for bad in ("../../../etc/passwd", "/etc/passwd", "a/../../etc/hosts", ""):
        assert A._safe_original_path(world, bad, None, kinds=A._XLSX_KINDS) is None, bad
    # 実在しないファイル・拡張子不一致（kinds 外）も None。
    assert A._safe_original_path(world, "missing.xlsx", None, kinds=A._XLSX_KINDS) is None
    assert A._safe_original_path(world, "a.xlsx", None, kinds=A._DOCX_KINDS) is None


def test_safe_original_path_rejects_sensitive_name_and_importance_control_file(monkeypatch, tmp_path):
    world = "s3b-sensitive"
    _isolate_world_kb(monkeypatch, tmp_path, world, {
        "credentials.xlsx": b"",
        "_重要度.txt": "*.xlsx: 高\n",
    })
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: tmp_path / "kb" / world)
    assert A._safe_original_path(world, "credentials.xlsx", None, kinds=A._XLSX_KINDS) is None
    assert A._safe_original_path(world, "_重要度.txt", None, kinds=A._FILE_HEAD_KINDS) is None


def test_safe_original_path_rejects_symlink_escape(monkeypatch, tmp_path):
    world = "s3b-symlink"
    _isolate_world_kb(monkeypatch, tmp_path, world, {})
    wd = tmp_path / "kb" / world
    wd.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.xlsx"
    _write_xlsx(outside)
    (wd / "link.xlsx").symlink_to(outside)
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: wd)
    assert A._safe_original_path(world, "link.xlsx", None, kinds=A._XLSX_KINDS) is None


def test_safe_original_path_rejects_out_of_scope(monkeypatch, tmp_path):
    world = "s3b-scope"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"4期/a.xlsx": b""})
    wd = tmp_path / "kb" / world
    _write_xlsx(wd / "4期" / "a.xlsx")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: wd)
    assert A._safe_original_path(world, "4期/a.xlsx", ["5期"], kinds=A._XLSX_KINDS) is None
    resolved = A._safe_original_path(world, "4期/a.xlsx", ["4期"], kinds=A._XLSX_KINDS)
    assert resolved is not None
    root, doc_id, rp, st = resolved   # RV#1: 4つ目に検査直後の stat（TOCTOU 突合用）を返す契約に変更
    assert doc_id == "4期/a.xlsx"
    assert rp == (wd / "4期" / "a.xlsx").resolve()
    assert st.st_ino == rp.stat().st_ino


def test_safe_original_path_rejects_xlsm_extension(monkeypatch, tmp_path):
    """RV#9 是正: `.xlsm` は台帳（`corpus_docs.classify_document`）が文書種別として扱わない拡張子
    ＝事前フィルタで通しても `verify_doc_exists` で必ず落ちるため、入口の `kinds` からも外す。"""
    world = "s3b-xlsm"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"a.xlsm": b""})
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: tmp_path / "kb" / world)
    assert A._safe_original_path(world, "a.xlsm", None, kinds=A._XLSX_KINDS) is None


def test_safe_original_path_resolves_to_world_root_original_not_derived_md(monkeypatch, tmp_path):
    """`_safe_doc_path` は Office を派生 MD へ解決するが、`_safe_original_path` は原本読取ツール専用
    ＝world root の原本そのものへ解決する（派生 MD が無くても・古くても関係ない）。"""
    world = "s3b-original-root"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"a.xlsx": b""})
    wd = tmp_path / "kb" / world
    _write_xlsx(wd / "a.xlsx")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: wd)
    resolved = A._safe_original_path(world, "a.xlsx", None, kinds=A._XLSX_KINDS)
    assert resolved is not None
    root, _doc_id, rp, _st = resolved   # RV#1: 4つ目は stat（TOCTOU 突合用）
    assert root == wd
    assert rp == (wd / "a.xlsx").resolve()


def _setup_office_world(monkeypatch, tmp_path, world: str):
    _isolate_world_kb(monkeypatch, tmp_path, world, {"note.py": "print('hi')\n", "note.txt": "hello\n"})
    wd = tmp_path / "kb" / world
    _write_xlsx(wd / "a.xlsx", cell_value="秘密: sk-ABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: wd)
    return wd


def test_run_tool_xlsx_sheets_and_range_add_docs_and_redact(monkeypatch, tmp_path):
    world = "s3b-run-xlsx"
    _setup_office_world(monkeypatch, tmp_path, world)

    r, docs, _cites, _cards = A.run_tool("xlsx_sheets", {"doc_id": "a.xlsx"}, world, None)
    assert r["sheets"] == [{"name": "Sheet1", "max_row": 1, "max_col": 1}]
    # RV#5: read_evidence 用に doc_id/text/locator を合成して足す（本体の "sheets" は不変）。
    assert r["doc_id"] == "a.xlsx"
    assert r["locator"] == "sheets"
    assert "Sheet1" in r["text"]
    assert docs == {"a.xlsx"}

    r2, docs2, _, _ = A.run_tool("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet1"}, world, None)
    assert docs2 == {"a.xlsx"}
    assert r2["rows"] == [["秘密: [REDACTED]"]]           # _redact_deep が本文の秘密を伏せる
    # RV#5: read_evidence 用の text も伏せ字済みの内容から合成される（漏れない）。
    assert r2["doc_id"] == "a.xlsx"
    assert r2["locator"] == "Sheet1!A1:A1"
    assert "[REDACTED]" in r2["text"]
    assert "秘密: sk-" not in r2["text"]

    # sheet 省略はエラー（doc は出典に載らない＝失敗時は docs に足さない）。
    r3, docs3, _, _ = A.run_tool("xlsx_range", {"doc_id": "a.xlsx"}, world, None)
    assert r3 == {"error": "sheet が必要です"}
    assert docs3 == set()


def test_run_tool_file_head_reads_text_and_redacts(monkeypatch, tmp_path):
    world = "s3b-run-filehead"
    _setup_office_world(monkeypatch, tmp_path, world)
    r, docs, _, _ = A.run_tool("file_head", {"doc_id": "note.txt"}, world, None)
    assert r["size"] == 6
    assert r["text"] == "hello\n"
    assert r["truncated"] is False
    # RV#5: read_evidence 用に doc_id/locator を足す（text は既存のまま・二重化しない）。
    assert r["doc_id"] == "note.txt"
    assert r["locator"] == "head"
    assert docs == {"note.txt"}


def test_run_tool_office_tools_error_when_layer_restricted_to_code(monkeypatch, tmp_path):
    world = "s3b-run-layer"
    _setup_office_world(monkeypatch, tmp_path, world)
    for name, args in (("xlsx_sheets", {"doc_id": "a.xlsx"}),
                       ("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet1"}),
                       ("docx_paragraphs", {"doc_id": "a.xlsx"}),
                       ("pptx_slides", {"doc_id": "a.xlsx"}),
                       ("pdf_pages", {"doc_id": "a.xlsx"})):
        r, docs, _, _ = A.run_tool(name, args, world, None, layer="code")
        assert r == {"error": "探す対象がソースに限定されています"}, name
        assert docs == set()


def test_run_tool_file_head_respects_layer_filter(monkeypatch, tmp_path):
    world = "s3b-run-filehead-layer"
    _setup_office_world(monkeypatch, tmp_path, world)
    # note.py はコード判定＝layer="docs" では読めない・layer="code" では読める。
    r_docs, _, _, _ = A.run_tool("file_head", {"doc_id": "note.py"}, world, None, layer="docs")
    assert r_docs == {"error": "doc_id が無効、または読み取り対象外です"}
    r_code, docs_code, _, _ = A.run_tool("file_head", {"doc_id": "note.py"}, world, None, layer="code")
    assert docs_code == {"note.py"}
    assert "print" in r_code["text"]


def test_run_tool_file_head_respects_tool_result_max_bytes(monkeypatch, tmp_path):
    """RV#7 是正: `tool_result_max_bytes` を無視して大きな text をそのまま返さない——`_finish_reader_result`
    が JSON 全体のバイト数（doc_id/text/locator 込み）を予算内に収める。"""
    world = "s3b-run-filehead-budget"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"big.txt": "x" * 100_000})
    res, docs, _, _ = A.run_tool("file_head", {"doc_id": "big.txt"}, world, None,
                                 tool_result_max_bytes=16384)
    assert "error" not in res, res
    assert res["truncated"] is True
    assert len(json.dumps(res, ensure_ascii=False).encode("utf-8")) <= 16384
    assert docs == {"big.txt"}


def test_run_tool_file_head_redacts_private_key_truncated_by_max_bytes(monkeypatch, tmp_path):
    """RV2巡目#3 是正: `file_head` の `max_bytes` で PEM 鍵ブロックの END 側が切り落とされても、
    鍵本文の断片が残らない——`_redact`（`_SECRET_RE`）は BEGIN/END が対で揃わないとマッチしない
    ため、切断で END が失われると以前は鍵の断片がそのまま外部 LLM へ渡っていた
    （`_finish_reader_result` の未終端鍵ブロック補完伏せ字＝`_redact_unterminated_key_tail` で救う）。
    """
    world = "s3b-filehead-key-redact"
    key_body = "A" * 500
    content = f"prefix\n-----BEGIN RSA PRIVATE KEY-----\n{key_body}\n-----END RSA PRIVATE KEY-----\nsuffix\n"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"secret.txt": content})
    cut = content.index(key_body) + 50   # BEGIN の後・END に届く前で切る
    res, docs, _, _ = A.run_tool("file_head", {"doc_id": "secret.txt", "max_bytes": cut}, world, None)
    assert "error" not in res, res
    assert "-----END" not in res["text"]
    assert "AAAA" not in res["text"]
    assert "[REDACTED]" in res["text"]
    assert "prefix" in res["text"]
    assert docs == {"secret.txt"}


def test_run_tool_toctou_rejects_path_swapped_to_symlink_after_check(monkeypatch, tmp_path):
    """RV#1 是正: `_safe_original_path` の検査後、実際に open するまでの間に検査済みパスが
    KB 外への symlink に差し替えられても読めない（検査済みパス文字列をそのまま再 open していた
    以前の実装の TOCTOU 穴の再現）。`_safe_original_path` の戻りを固定した上でファイルを
    symlink に置換し、`run_tool` がそれでも読めないことを確認する。"""
    world = "s3b-toctou"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"note.txt": "hello\n"})
    wd = tmp_path / "kb" / world
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: wd)

    real_resolved = A._safe_original_path(world, "note.txt", None, kinds=A._FILE_HEAD_KINDS)
    assert real_resolved is not None
    monkeypatch.setattr(A, "_safe_original_path", lambda *a, **kw: real_resolved)
    monkeypatch.setattr(RT, "_safe_original_path", lambda *a, **kw: real_resolved)

    # 検査「後」に実体を KB 外への symlink へ差し替える（TOCTOU の隙間を模す）。
    outside = tmp_path / "outside.txt"
    outside.write_text("secret outside kb\n", encoding="utf-8")
    rp = real_resolved[2]
    rp.unlink()
    rp.symlink_to(outside)

    res, docs, _, _ = A.run_tool("file_head", {"doc_id": "note.txt"}, world, None)
    assert res == {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
    assert docs == set()


def test_run_tool_ancestor_dir_symlink_swap_after_check_is_rejected(monkeypatch, tmp_path):
    """RV2巡目#1 是正: `_safe_original_path` の検査後、実際に open するまでの間に doc_id の
    **祖先ディレクトリ**（最終要素ではなく）が KB 外への symlink に差し替えられても読めない。

    以前の実装（`_open_verified_original` が最終要素だけ `O_NOFOLLOW` で単発 open・fstat 突合）は
    差し替え先の外部ディレクトリに元ファイルと同じ inode をハードリンクしておけば fstat 突合を
    すり抜けた——fstat は一致する（同じ inode）が、実際の open は KB 外のディレクトリを経由して
    いる＝封じ込めが壊れている（`_open_file_nofollow_walk` に切り替える前の実装ではここで読めて
    しまっていた）。新しい実装は途中の `sub` が symlink に差し替わっている時点で `ELOOP` になり、
    fstat 突合を待たずに拒否する。
    """
    world = "s3b-ancestor-toctou"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"sub/note.txt": "hello\n"})
    wd = tmp_path / "kb" / world
    monkeypatch.setattr(A.worlds, "world_dir", lambda w: wd)

    real_resolved = A._safe_original_path(world, "sub/note.txt", None, kinds=A._FILE_HEAD_KINDS)
    assert real_resolved is not None
    monkeypatch.setattr(A, "_safe_original_path", lambda *a, **kw: real_resolved)
    monkeypatch.setattr(RT, "_safe_original_path", lambda *a, **kw: real_resolved)

    # 検査「後」に祖先ディレクトリ（最終要素ではなく sub/ 自体）を KB 外への symlink に差し替える。
    # 差し替え先には元ファイルと同じ inode をハードリンクしておく（fstat 突合だけでは検出できない
    # ことを実証する目的）。
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    sub_dir = wd / "sub"
    os.link(str(sub_dir / "note.txt"), str(outside / "note.txt"))
    shutil.rmtree(sub_dir)
    sub_dir.symlink_to(outside)

    res, docs, _, _ = A.run_tool("file_head", {"doc_id": "sub/note.txt"}, world, None)
    assert res == {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
    assert docs == set()


# ===== `_redact_deep` は文書順の状態付き走査（構造をまたぐ PEM 鍵ブロック）=====================
# 以下は `_redact_deep`（`sherpa/agentic_search.py`）自体の直接テスト。
# 以前は `_redact_unterminated_key_tail` が「合成した1本の text」にしか効かず、`paragraphs[].text`
# ／`rows`／`pages[].text`／`notes` 等の**構造そのもの**に残る、要素をまたいだ鍵ブロックの断片は
# 伏せ字にならなかった（`doc_readers` が返す形を模した dict で直接検証する・上の慣例と同じ）。

def test_redact_deep_docx_paragraphs_redacts_pem_key_spanning_multiple_paragraphs():
    result = {"paragraphs": [
        {"i": 0, "style": "Normal", "text": "prefix -----BEGIN RSA PRIVATE KEY-----"},
        {"i": 1, "style": "Normal", "text": "A" * 200},
        {"i": 2, "style": "Normal", "text": "-----END RSA PRIVATE KEY----- suffix"},
    ], "tables": []}
    out = A._redact_deep(result)
    texts = [p["text"] for p in out["paragraphs"]]
    assert "prefix" in texts[0] and "BEGIN" not in texts[0] and "[REDACTED]" in texts[0]
    assert texts[1] == "[REDACTED]"                       # 中間の段落（鍵本文のみ）は丸ごと伏せる
    assert "suffix" in texts[2] and "END" not in texts[2] and "[REDACTED]" in texts[2]


def test_redact_deep_xlsx_rows_redacts_pem_key_spanning_multiple_cells():
    result = {"sheet": "Sheet1", "range": "A1:A3", "truncated": False,
             "rows": [["prefix -----BEGIN RSA PRIVATE KEY-----"], ["A" * 200],
                      ["-----END RSA PRIVATE KEY----- suffix"]]}
    out = A._redact_deep(result)
    r0, r1, r2 = out["rows"]
    assert "prefix" in r0[0] and "BEGIN" not in r0[0] and "[REDACTED]" in r0[0]
    assert r1[0] == "[REDACTED]"
    assert "suffix" in r2[0] and "END" not in r2[0] and "[REDACTED]" in r2[0]


def test_redact_deep_pdf_pages_redacts_pem_key_spanning_multiple_pages():
    result = {"truncated": False, "pages": [
        {"no": 1, "text": "prefix -----BEGIN RSA PRIVATE KEY-----"},
        {"no": 2, "text": "A" * 200},
        {"no": 3, "text": "-----END RSA PRIVATE KEY----- suffix"},
    ]}
    out = A._redact_deep(result)
    p0, p1, p2 = out["pages"]
    assert "prefix" in p0["text"] and "BEGIN" not in p0["text"] and "[REDACTED]" in p0["text"]
    assert p1["text"] == "[REDACTED]"
    assert "suffix" in p2["text"] and "END" not in p2["text"] and "[REDACTED]" in p2["text"]


# ===== RV2巡目 #5/#6/#7/#8: `_finish_reader_result`/`_doc_reader_text_locator` の仕上げ ==========
# これらは `doc_readers` の出力を受け取る純関数（world/scope/実ファイルを知らない）なので、
# 実際のファイルではなく `doc_readers` が返す形を模した dict で直接検証する
# （`tests/unit/test_doc_readers.py` の実際の出力例と同じ形＝`i`/`row_start`/`total_rows`/`rows` 等）。

def test_finish_reader_result_pdf_single_page_too_big_keeps_truncated_text():
    """RV2巡目#6 是正: 1ページ（1件）だけでもバイト予算を超える場合、以前は二分探索の結果
    `pages` が空（0件）になり本文が丸ごと消えていた。先頭1件を予算内へ切り詰めて
    `text_truncated: true` を立てて残す（番号・locator は保つ）。"""
    text = "あ" * 3000   # 日本語3,000字（UTF-8で1文字3バイト＝素の text だけで9,000バイト超）
    result = {"total": 1, "pages": [{"no": 1, "text": text}], "truncated": False}
    out = A._finish_reader_result("pdf_pages", result, "big.pdf", 16384)
    assert "error" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 16384
    assert out["truncated"] is True
    assert len(out["pages"]) == 1
    assert out["pages"][0]["no"] == 1
    assert out["pages"][0]["text_truncated"] is True
    assert out["pages"][0]["text"]                 # 空にはしない
    assert out["locator"] == "pages[1]"


def test_finish_reader_result_docx_single_paragraph_too_big_keeps_truncated_text():
    """1段落（1件）だけでもバイト予算を超える場合、以前は二分探索の結果
    `paragraphs` が空（0件）になり本文が丸ごと消えていた（`pdf_pages` の RV2巡目#6 と同じ穴が
    `docx_paragraphs` に残っていた）。先頭1段落を予算内へ切り詰めて `text_truncated: true` を
    立てて残す。"""
    text = "あ" * 3000   # 日本語3,000字（UTF-8で1文字3バイト＝素の text だけで9,000バイト超）
    result = {"total": 1, "total_tables": 0, "paragraphs": [{"i": 0, "style": "Normal", "text": text}],
             "tables": [], "truncated": False}
    out = A._finish_reader_result("docx_paragraphs", result, "big.docx", 16384)
    assert "error" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 16384
    assert out["truncated"] is True
    assert len(out["paragraphs"]) == 1
    assert out["paragraphs"][0]["i"] == 0
    assert out["paragraphs"][0]["text_truncated"] is True
    assert out["paragraphs"][0]["text"]                 # 空にはしない


def test_finish_reader_result_docx_tables_are_clipped_to_budget():
    """RV2巡目#5 是正: 表（`tables`）もバイト予算の削減対象にする——以前は段落だけを二分探索し、
    表は丸ごと残っていたため大きな表があると予算を超え得た（50×8 の表）。"""
    rows = [[f"r{r}c{c}" * 20 for c in range(8)] for r in range(50)]
    result = {"paragraphs": [], "tables": [{"i": 0, "row_start": 0, "total_rows": 50, "rows": rows}],
             "truncated": False}
    out = A._finish_reader_result("docx_paragraphs", result, "big.docx", 65536)
    assert "error" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 65536
    assert out["truncated"] is True
    assert len(out["tables"]) == 1
    assert 0 < len(out["tables"][0]["rows"]) < 50
    assert out["tables"][0]["row_truncated"] is True


def test_finish_reader_result_xlsx_range_updates_range_and_locator_after_row_clip():
    """RV2巡目#7 是正: 行がバイト予算で削られたら `range`（延いては `locator`）も実際に返した
    行数へ更新する——以前は行を減らしても `range` が元の（削る前の）範囲のまま食い違って残った。
    """
    rows = [[f"v{r}" * 100] for r in range(50)]
    result = {"sheet": "Sheet1", "range": "A1:A50", "rows": rows, "truncated": False}
    out = A._finish_reader_result("xlsx_range", result, "big.xlsx", 8192)
    assert "error" not in out
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 8192
    assert out["truncated"] is True
    n = len(out["rows"])
    assert 0 < n < 50
    assert out["range"] == f"A1:A{n}"
    assert out["locator"] == f"Sheet1!A1:A{n}"


def test_doc_reader_text_locator_docx_table_only_produces_nonempty_text():
    """RV2巡目#8 是正: 段落が無く表だけの docx でも本文合成の対象にする——以前は段落だけを見て
    いたため、表しか無い docx の read_evidence の text が常に空になっていた。

    locator は表の「表番号の範囲」だけでなく実際に返した行範囲
    （`row_start`〜`row_start+len(rows)-1`）も含む（`paragraphs[s-e];tables[ts-te]rows[rs-re]`
    の形・段落が無いのでここは tables 部分だけになる）——表ページングの区別に使う。"""
    result = {"paragraphs": [], "tables": [{"i": 0, "row_start": 0, "total_rows": 2,
                                           "rows": [["h1", "h2"], ["v1", "v2"]]}]}
    text, locator = A._doc_reader_text_locator("docx_paragraphs", result)
    assert text
    assert "表0行0" in text
    assert "h1" in text and "v2" in text
    assert locator == "tables[0-0]rows[0-1]"


def test_doc_reader_text_locator_pptx_includes_tables_and_notes():
    """RV2巡目#8 是正: pptx の表・ノートも本文合成の対象にする（テキストだけだと表の内容や
    ノートの補足が read_evidence から丸ごと落ちていた）。"""
    result = {"slides": [{"no": 1, "texts": ["title"], "tables": [[["a", "b"]]], "notes": "memo"}]}
    text, locator = A._doc_reader_text_locator("pptx_slides", result)
    assert "title" in text
    assert "a" in text and "b" in text
    assert "memo" in text
    assert locator == "slides[1]"


def test_doc_reader_text_locator_docx_table_row_paging_distinguishes_locator():
    """表だけを `table_row_start` を進めて呼び直した（行のページング）2回の
    結果は、段落の locator（`paragraphs[s-e]`）が同じでも表の行範囲が異なる——以前は段落側の
    範囲だけを locator にしていたため2回とも同じ locator に潰れた。"""
    def _page(row_start: int) -> dict:
        return {"paragraphs": [{"i": 0, "style": "Normal", "text": "見出し"}],
                "tables": [{"i": 0, "row_start": row_start, "total_rows": 60,
                           "rows": [[f"T{row_start + i}"] for i in range(50)]}],
                "truncated": True}

    r0, r50 = _page(0), _page(50)
    _, locator0 = A._doc_reader_text_locator("docx_paragraphs", r0)
    _, locator50 = A._doc_reader_text_locator("docx_paragraphs", r50)
    assert locator0 != locator50
    assert locator0 == "paragraphs[0-0];tables[0-0]rows[0-49]"
    assert locator50 == "paragraphs[0-0];tables[0-0]rows[50-99]"


def test_finish_docx_paragraphs_result_keeps_paragraphs_before_big_tables():
    """大きな表を持つ docx でも段落が先に確保され、余った予算で表の行が入る（段落 0 件にならない）。"""
    paras = [{"i": i, "style": "Normal", "text": f"段落{i} " + "あ" * 50} for i in range(30)]
    tables = [{"i": t, "row_start": 0, "total_rows": 50, "rows": [["セル" * 40] * 8 for _ in range(50)]} for t in range(20)]
    result = {"total": 30, "total_tables": 20, "paragraphs": paras, "tables": tables, "truncated": False}
    r = A._finish_docx_paragraphs_result(result, "big.docx", 262144)
    assert len(r["paragraphs"]) == 30 and r["truncated"] is True
    assert sum(len(t["rows"]) for t in r["tables"]) > 0
    assert A._result_byte_size(r) <= 262144


def test_finish_docx_paragraphs_result_rescues_paragraph_even_with_small_table():
    """RV是正: 小さな表が1行でもあると、以前は「表1行だけなら丸ごと入る」（`best_n_rows > 0`）
    が先に成立して即座に返ってしまい、長い段落の救済（`_shrink_single_item_result`）へ
    絶対に到達しなかった（段落が全部消え、表1行だけが残る）。段落0件のときは表があっても
    必ず救済を経由し、先頭段落を予算内へ切り詰めて（`text_truncated`）確保した上で、表の行も
    一緒に残す。"""
    text = "あ" * 3000   # 日本語3,000字（合成 text と二重化されるため単体でも budget を超える）
    result = {"total": 1, "total_tables": 1,
             "paragraphs": [{"i": 0, "style": "Normal", "text": text}],
             "tables": [{"i": 0, "row_start": 0, "total_rows": 1, "rows": [["r1c1"]]}],
             "truncated": False}
    r = A._finish_docx_paragraphs_result(result, "big.docx", 16384)
    assert "error" not in r
    assert len(json.dumps(r, ensure_ascii=False).encode("utf-8")) <= 16384
    assert r["truncated"] is True
    assert len(r["paragraphs"]) == 1
    assert r["paragraphs"][0]["text_truncated"] is True
    assert r["paragraphs"][0]["text"]                       # 空にはしない
    assert len(r["tables"]) == 1 and r["tables"][0]["rows"] == [["r1c1"]]   # 表の行も残る


def test_finish_docx_paragraphs_result_rescue_secures_paragraph_before_big_table_row():
    """段落が1件も丸ごと入らず、かつ表の1行がそれ自体で予算の大半を占めるほど大きい場合——
    以前は「段落0件を前提にした行数」の表をまず確保してから段落を切り詰めていたため、表が
    予算をほぼ使い切り、救済した段落の text が空文字になり、しかも合計サイズが予算を
    超えてしまっていた（空の段落を足しても表側の見積もりが更新されないため）。表を0行にした
    状態で先に段落を救済してから、残り予算で表の行数を決める順序に直すと、段落本文が非空の
    まま、合計サイズも予算内に収まる。"""
    text = "あ" * 3000                      # 単体でも合成 text と二重化されて budget を超える
    result = {"total": 1, "total_tables": 1,
             "paragraphs": [{"i": 0, "style": "Normal", "text": text}],
             "tables": [{"i": 0, "row_start": 0, "total_rows": 1, "rows": [["x" * 8050]]}],
             "truncated": False}
    r = A._finish_docx_paragraphs_result(result, "big.docx", 16384)
    assert "error" not in r
    assert len(json.dumps(r, ensure_ascii=False).encode("utf-8")) <= 16384
    assert r["truncated"] is True
    assert len(r["paragraphs"]) == 1
    assert r["paragraphs"][0]["text_truncated"] is True
    assert r["paragraphs"][0]["text"]                       # 空にはしない（以前は "" になっていた）


def test_search_results_mark_truncated_when_hit_cap_reached(monkeypatch):
    from sherpa import grep_tool
    fake = [{"doc_id": f"d{i}.md", "line": 1, "text": "x", "span": [1, 1]} for i in range(3)]
    monkeypatch.setattr(grep_tool, "grep_search", lambda *a, **k: list(fake))
    res, *_ = A.run_tool("ripgrep_search", {"query": "x"}, "v1", None, max_hits=3)
    assert res.get("truncated") is True
    monkeypatch.setattr(grep_tool, "grep_search", lambda *a, **k: list(fake[:2]))
    res, *_ = A.run_tool("ripgrep_search", {"query": "x"}, "v1", None, max_hits=3)
    assert "truncated" not in res


def test_es_search_cap_is_judged_on_raw_hits_before_filtering(monkeypatch):
    from sherpa import es_index, documents
    raw = [{"doc_id": f"d{i}.md", "line": 1, "text": "x", "score": 1.0, "span": [1, 1]} for i in range(3)]
    monkeypatch.setattr(es_index, "search", lambda *a, **k: (list(raw), None))
    monkeypatch.setattr(documents, "world_rel_set", lambda *a, **k: {"d0.md", "d1.md"})   # 1 件は実在せず落ちる
    res, *_ = A.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=3)
    assert res.get("truncated") is True and len(res["hits"]) <= 2


# ===== S3: 障害種別の分類（`_is_recoverable_tool_exception`/`_tool_backend_kind`/
# `_record_tool_exception`/`_record_tool_result_error_code`）=====

def test_open_doc_stream_open_failure_carries_read_io_error_code(monkeypatch, tmp_path):
    """`_open_doc_stream` の実際の open 失敗（`OSError`）が固定理由コード `read_io_failed` を
    結果へ付ける——例外にならず結果化される読取I/O失敗を `run_tool` 呼び出し元が拾える形。"""
    from sherpa import scope as scope_mod

    def _boom(root, rel_parts):
        raise OSError("boom")

    monkeypatch.setattr(A, "_open_file_nofollow_walk", _boom)
    monkeypatch.setattr(RT, "_open_file_nofollow_walk", _boom)
    monkeypatch.setattr(A, "_safe_doc_path", lambda world, doc_id, layer=None: (tmp_path, "x.txt", tmp_path / "x.txt"))
    monkeypatch.setattr(RT, "_safe_doc_path", lambda world, doc_id, layer=None: (tmp_path, "x.txt", tmp_path / "x.txt"))
    monkeypatch.setattr(scope_mod, "in_scope", lambda doc_id, sp: True)
    f, err = A._open_doc_stream("v1", "x.txt", None, None)
    assert f is None
    assert err == {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}


def test_es_index_search_classifies_http_400_as_rejected_and_5xx_as_failed(monkeypatch):
    """`es_index.search` の BM25 POST が例外を投げたとき、HTTP ステータスで回復可否を分類する——
    4xx（クエリ自体の拒否＝構文/設定不備）は `es_query_rejected`（回復不可）、5xx/接続断は
    従来どおり `es_query_failed`（回復可能）のまま。外部境界（HTTP 通信）にステータスを注入する。"""
    import urllib.error

    from sherpa import es_index

    monkeypatch.setattr(es_index, "available", lambda: True)

    def boom_400(method, path, body=None, ndjson=False, timeout=es_index._TIMEOUT):
        raise urllib.error.HTTPError("http://es/_search", 400, "Bad Request", {}, None)

    monkeypatch.setattr(es_index, "_req", boom_400)
    hits, reason = es_index.search("v1", "query", vector=False)
    assert hits == [] and reason == "es_query_rejected"

    def boom_503(method, path, body=None, ndjson=False, timeout=es_index._TIMEOUT):
        raise urllib.error.HTTPError("http://es/_search", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(es_index, "_req", boom_503)
    hits, reason = es_index.search("v1", "query", vector=False)
    assert hits == [] and reason == "es_query_failed"


def test_open_verified_original_open_failure_carries_read_io_error_code(monkeypatch, tmp_path):
    """`_open_verified_original`（xlsx_sheets 等・原本読取ツールが使う TOCTOU 再検証 open）の
    失敗が固定理由コード `read_io_failed` を結果へ付ける。"""
    def _boom(root, rel_parts):
        raise OSError("boom")

    monkeypatch.setattr(A, "_open_file_nofollow_walk", _boom)
    monkeypatch.setattr(RT, "_open_file_nofollow_walk", _boom)
    f, err = A._open_verified_original(tmp_path, "x.txt", None)
    assert f is None
    assert err == {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}


def test_es_index_search_keeps_non_raising_contract_for_non_communication_exception(monkeypatch):
    """`es_index.search` の BM25 クエリで `JSONDecodeError`（非 JSON 応答・通信例外ではない）が
    発生しても、`search()` の「例外を投げず `(hits, degrade_reason)` を返す」契約は保たれる
    （`routers/documents.py`・`parts/read/fused_search.py`・`ext_api.py` 等、`run_tool` 境界の型分類に
    委ねられない非 agentic 経路も同じ関数を呼ぶため）——通信障害と区別し、回復不可の固定コード
    `es_query_rejected` を返す（`es_query_failed` として回復可能扱いにはしない）。"""
    import json

    from sherpa import es_index

    monkeypatch.setattr(es_index, "available", lambda: True)

    def boom_bad_json(method, path, body=None, ndjson=False, timeout=es_index._TIMEOUT):
        raise json.JSONDecodeError("bad json", "not json", 0)

    monkeypatch.setattr(es_index, "_req", boom_bad_json)
    hits, reason = es_index.search("v1", "query", vector=False)
    assert hits == [] and reason == "es_query_rejected"


def test_es_index_search_treats_404_as_recoverable_index_not_yet_created(monkeypatch):
    """ES の 404（索引未作成＝未取り込み world の常態）は `es_query_rejected`（回復不可）ではなく
    `es_query_failed`（回復可能）に分類する——4xx を一律回復不可にすると、未取り込み world への
    問い合わせで grep 縮退が恒久的に塞がれてしまう。"""
    import urllib.error

    from sherpa import es_index

    monkeypatch.setattr(es_index, "available", lambda: True)

    def boom_404(method, path, body=None, ndjson=False, timeout=es_index._TIMEOUT):
        raise urllib.error.HTTPError("http://es/_search", 404, "Not Found", {}, None)

    monkeypatch.setattr(es_index, "_req", boom_404)
    hits, reason = es_index.search("v1", "query", vector=False)
    assert hits == [] and reason == "es_query_failed"


def test_tool_hit_count_returns_none_for_graph_neighbors_error_code_result():
    """`graph_neighbors` が `error_code`（`graph_unavailable`/`graph_internal_error`）付きの結果
    （`{"neighbors": []}`・"error" キーは持たない）を返した場合、`_tool_hit_count` は 0 ではなく
    None を返す——es_search の degrade と同じ規律で「実行できなかった」を「0件ヒット」と
    混同しない。"""
    assert A._tool_hit_count("graph_neighbors", {"neighbors": [], "error_code": "graph_unavailable"}) is None
    assert A._tool_hit_count("graph_neighbors", {"neighbors": [], "error_code": "graph_internal_error"}) is None
    # error_code が無い通常の 0 件応答は従来どおり 0（回帰しないことの対照）。
    assert A._tool_hit_count("graph_neighbors", {"neighbors": []}) == 0


# ===== S4（縮退の可視化と計数）: グラフの3状態（空・世代不一致・接続断）を区別して調査を止めない =====
# モックは外部境界（Neo4j ドライバ）だけ——`lens_service`/`world_neo4j` の内部関数は差し替えない。

class _FakeRecord(dict):
    def data(self):
        return dict(self)


class _FakeResult:
    def __init__(self, rows):
        self._rows = [_FakeRecord(r) for r in rows]

    def __iter__(self):
        return iter(self._rows)

    def data(self):
        return [r.data() for r in self._rows]

    def consume(self):
        pass


class _FakeSession:
    """世代プローブ（`SherpaMeta`）にだけ実データ有り＋指定世代を返す fake（他クエリは0件）。"""

    def __init__(self, era, raise_exc=None):
        self.era, self.raise_exc = era, raise_exc

    def run(self, query, **kw):
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResult([{"c": 1, "era": self.era}] if "SherpaMeta" in str(query) else [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session

    def close(self):
        pass


def _patch_neo4j_driver(monkeypatch, session):
    import neo4j
    monkeypatch.setattr(neo4j.GraphDatabase, "driver",
                        staticmethod(lambda *a, **kw: _FakeDriver(session)))


def test_run_tool_graph_neighbors_schema_era_returns_reingest_code(monkeypatch):
    """世代不一致（旧世代の実データ）は例外で調査を終端させず、MCP 側と同じ機械可読コードの
    ツール結果へ変換して返す（`run_tool` は raise しない）。"""
    _patch_neo4j_driver(monkeypatch, _FakeSession("old-era"))
    res, docs, cites, cards = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert res == {"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"}
    assert docs == set() and cites == [] and cards == []


def test_run_tool_graph_neighbors_connection_failure_returns_unavailable_code(monkeypatch):
    """接続断（`ServiceUnavailable`＝`DriverError` 系）は世代不一致とは別コード（回復可能）。"""
    from neo4j.exceptions import ServiceUnavailable
    _patch_neo4j_driver(monkeypatch, _FakeSession(None, raise_exc=ServiceUnavailable("down")))
    res, _docs, _cites, _cards = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert res["neighbors"] == [] and res["error_code"] == "graph_unavailable"
    assert "error" not in res


def test_run_tool_graph_neighbors_empty_graph_is_not_a_failure(monkeypatch):
    """空（未構築＝実データ0件）は現状どおり例外にも障害コードにもならない（近傍0件）。"""
    _patch_neo4j_driver(monkeypatch, _FakeSession(None))   # c=0 相当（SherpaMeta 以外は0件）

    class _EmptySession(_FakeSession):
        def run(self, query, **kw):
            return _FakeResult([{"c": 0, "era": None}] if "SherpaMeta" in str(query) else [])

    _patch_neo4j_driver(monkeypatch, _EmptySession(None))
    res, _docs, _cites, _cards = A.run_tool("graph_neighbors", {"name": "請求"}, "v1", None)
    assert res == {"neighbors": []}


# S4（RV4）: 最初から不達で「ツール集合に入らなかった」バックエンドも縮退として計数する
# （実行中に記録される機会が無いため）。利用者が自分で OFF にした場合は障害ではない＝計数しない。


def test_run_tool_es_search_mode_routing(monkeypatch):
    """mode: 不正値は hybrid・keyword は vector=False・vector は knn のみ（埋め込み不可なら BM25 へ縮退し理由と mode_used を返す）。"""
    from sherpa import documents

    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {"a.md"})
    h = [{"doc_id": "a.md", "line": 1, "text": "x", "ext": ".md"}]
    calls = []

    def fake_search(world, q, scope_paths=None, k=20, layer=None, vector=True, **kw):
        calls.append(("search", vector))
        return h, None

    def fake_knn(world, q, scope_paths=None, k=20, layer=None, **kw):
        calls.append(("knn", None))
        return knn_result

    monkeypatch.setattr(A.es_index, "search", fake_search)
    monkeypatch.setattr(A.es_index, "search_knn_only", fake_knn)
    knn_result = (h, None)

    def run(mode):
        return A.run_tool("es_search", {"query": "x", "mode": mode}, "v1", None)[0]

    assert run("bogus")["mode_used"] == "hybrid" and calls[-1] == ("search", True)
    assert run("keyword")["mode_used"] == "keyword" and calls[-1] == ("search", False)
    assert run("vector")["mode_used"] == "vector" and calls[-1] == ("knn", None)
    knn_result = ([], "embedding_not_configured")
    v = run("vector")
    assert v["mode_used"] == "keyword" and v["degrade_reason"] == "embedding_not_configured"
    assert calls[-2:] == [("knn", None), ("search", False)] and v["hits"]

    # 埋め込み未設定の hybrid（既定）は BM25 だけで検索している＝mode_used は keyword・理由を返す
    monkeypatch.setattr(A.es_index, "search", lambda *a, **kw: (h, "embedding_not_configured"))
    v = A.run_tool("es_search", {"query": "x"}, "v1", None)[0]
    assert v["mode_used"] == "keyword" and v["degrade_reason"] == "embedding_not_configured"
    assert A._tool_hit_count("es_search", v) == 1

    # vector で ES のクエリ自体が失敗したときは BM25 へ倒さず、失敗をそのまま返す
    calls.clear()
    knn_result = ([], "es_query_failed")
    monkeypatch.setattr(A.es_index, "search", fake_search)
    v = run("vector")
    assert calls == [("knn", None)] and v["hits"] == []
    assert v["degrade_reason"] == "es_query_failed" and v["mode_used"] == "vector"
