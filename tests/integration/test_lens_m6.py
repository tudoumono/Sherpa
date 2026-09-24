"""別レンズ probe 受け入れ（トラブルシュート＝AT-L1／仕様問い合わせ＝AT-L2・鏡モデル）。

- qa/grep は FS のみ＝Neo4j 不要（doc_id＝rel_path）。
- troubleshoot/neo4j_related は要 Neo4j（`ensure_v1`）。
"""
from __future__ import annotations

from _world_setup import NIGHTLY_CID, SPEC, TEST_WORLD_ID, driver, ensure_v1

from sherpa.chat_service import _dispatch, _merge_qa_with_es
from sherpa.grep_tool import grep_search
from sherpa.lens_service import neo4j_related, run_qa, run_troubleshoot

V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）
# S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: L 意味層（Incident 等）は撤去済み。
# 近傍は骨格＋言及エッジ（Pass3）のみ——Document ノードの同一性＝パス（name もそのまま rel_path）。
OPS_DOC = "4期/02_保守/03_運用手順/締めバッチ運用手順.md"
TS_EXPECT = {"TAXCALC", OPS_DOC}   # グラフ近傍のノード名（正解）
SYMPTOM = "夜間バッチ NIGHTLY で税額計算が ABEND。原因候補は？"


# ---- qa / grep（FS のみ） -------------------------------------------------

def test_grep_search_returns_section_span():
    """grep は MD の見出し節を span つき・doc_id＝rel_path で返す。"""
    hits = grep_search("端数処理", V)
    docs = {h["doc_id"] for h in hits}
    assert SPEC in docs
    h = next(h for h in hits if h["doc_id"] == SPEC)
    assert "端数処理" in h["text"]
    assert h["span"][0] <= h["line"] <= h["span"][1]


def test_qa_cites_spec_section():
    """AT-L2: 「端数処理」→ 税計算仕様書 の該当節を doc_id（rel_path）＋span で引用。"""
    res = run_qa("端数処理", V)
    assert res["type"] == "qa" and res["answered"]
    cites = {c["doc_id"]: c for c in res["citations"]}
    assert SPEC in cites
    c = cites[SPEC]
    assert len(c["span"]) == 2 and "端数処理" in c["quote"]
    assert "path" not in c                                     # 物理パスは露出しない


def test_qa_multiterm_fallback():
    res = run_qa("端数処理 切り捨て", V)
    assert res["answered"]
    assert SPEC in {c["doc_id"] for c in res["citations"]}


def test_qa_production_path_keeps_golden_citation():
    """本番 qa 経路（chat の `_dispatch` そのもの＝grep＋ES 統合→出典DL）でも golden 引用が落ちない＝文書側 Done。"""
    env = _dispatch(None, "qa", "端数処理", V, None)               # qa は session 不要（FS+ES のみ）
    cites = {c["doc_id"] for c in env["data"]["citations"]}
    assert SPEC in cites                                            # 統合後も該当節が残る
    srcs = {s["doc_id"]: s for s in env["sources"]}
    assert SPEC in srcs and "/documents/download" in srcs[SPEC]["download_url"]   # 出典＝原本DL


def test_qa_precision_absent_term():
    """precision 番人: 文書に無い語は**偽の引用を出さない**（grep も ES 統合も 0 件）。

    純ASCII の無意味語にする（kuromoji で日本語トークンに分割され ES の OR マッチで偽陽性…を避ける・RV High）。
    """
    absent = "ZZQWXNONEXISTENTTOKEN42"
    res = run_qa(absent, V)
    assert not res.get("citations") and not res.get("answered")
    merged = _merge_qa_with_es(res, V, absent, None)
    assert not merged["citations"]                                  # ES も偽ヒットを足さない（起動/未起動どちらでも）


# ---- troubleshoot / neo4j_related（要 Neo4j） ----------------------------

def test_neo4j_related_includes_cross_lens_neighbors():
    ensure_v1()
    drv = driver()
    try:
        with drv.session() as s:
            rel = neo4j_related(s, [NIGHTLY_CID], V, depth=3)
    finally:
        drv.close()
    names = {r["name"] for r in rel}
    assert TS_EXPECT <= names, f"missing: {TS_EXPECT - names}"


def test_troubleshoot_candidates_with_evidence():
    ensure_v1()
    drv = driver()
    try:
        with drv.session() as s:
            res = run_troubleshoot(s, SYMPTOM, V)
    finally:
        drv.close()
    assert res["type"] == "troubleshoot"
    assert "NIGHTLY" in res["anchors"]
    cards = {c["name"]: c for c in res["candidates"]}
    assert TS_EXPECT <= set(cards), f"missing: {TS_EXPECT - set(cards)}"
    for n in TS_EXPECT:                                       # 各候補に根拠（グラフ経路 or grep）
        ev = cards[n]["evidence"]
        assert ev["edges"] or ev["grep"], f"no evidence for {n}"
        assert all("path" not in g for g in ev["grep"])      # grep 根拠に物理パスを出さない
    assert cards["TAXCALC"]["role"] == "実装"                 # 役割がラベルから付く


def test_troubleshoot_precision_customer_island():
    """precision 番人(troubleshoot): 税と非連結の顧客マスタ島を起点にしても**税クラスタを出さない**（島の分離・RV）。"""
    ensure_v1()
    drv = driver()
    try:
        with drv.session() as s:
            res = run_troubleshoot(s, "顧客マスタ機能 CUSTMNT でエラー。原因は？", V)
    finally:
        drv.close()
    assert "CUSTMNT" in res["anchors"]                            # 起点は解決している（自明合格でない）
    names = {c["name"] for c in res["candidates"]}
    assert not (names & {"TAXCALC", "TAX-RATE", "TAX-CPY"})   # 税クラスタは混ざらない
