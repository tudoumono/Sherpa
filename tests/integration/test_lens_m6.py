"""別レンズ probe 受け入れ（grep の節 span・近傍探索の経路・鏡モデル）。

- grep は FS のみ＝Neo4j 不要（doc_id＝rel_path）。
- neo4j_related は要 Neo4j（`ensure_v1`）。
"""
from __future__ import annotations

from _world_setup import NIGHTLY_CID, SPEC, TEST_WORLD_ID, driver, ensure_v1

from sherpa.grep_tool import grep_search
from sherpa.lens_service import neo4j_related

V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）
# S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: L 意味層（Incident 等）は撤去済み。
# 近傍は骨格＋言及エッジ（Pass3）のみ——Document ノードの同一性＝パス（name もそのまま rel_path）。
OPS_DOC = "4期/02_保守/03_運用手順/締めバッチ運用手順.md"
TS_EXPECT = {"TAXCALC", OPS_DOC}   # グラフ近傍のノード名（正解）


# ---- grep（FS のみ） -------------------------------------------------

def test_grep_search_returns_section_span():
    """grep は MD の見出し節を span つき・doc_id＝rel_path で返す。"""
    hits = grep_search("端数処理", V)
    docs = {h["doc_id"] for h in hits}
    assert SPEC in docs
    h = next(h for h in hits if h["doc_id"] == SPEC)
    assert "端数処理" in h["text"]
    assert h["span"][0] <= h["line"] <= h["span"][1]


# ---- neo4j_related（要 Neo4j） ----------------------------

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
