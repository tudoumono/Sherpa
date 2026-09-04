"""影響分析 受け入れ（統合・要 Neo4j）: resolve→world_impact→結果整形が golden を返す（鏡モデル）。

前提: Neo4j 起動。`_world_setup.ensure_v1()` で v1 world をロードする（旧 graph-load 不要）。
オラクル（正解）なのでテーマ名を含む（パイプライン本体は持たない＝AT-G3）。

S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: 意味層フル抽出・REALIZES 橋・名寄せ
（aliasmap）は撤去済み。業務語（「消費税率」等）はもう graph 側の橋渡しで直接コードへ解決しない
——起点語はコード自身の識別子（`TAX-RATE`/`FEE-RATE` 等）を使う。業務語の入口は presumed
（grep 共起の推定）へフォールバックすることを別途固定する（§2＝クエリ時のエージェント入口の代替）。
K12: 「確実/要確認」の2値判定は機構ごと撤去（全件同格）。
"""
from __future__ import annotations

from _world_setup import TEST_WORLD_ID, driver, ensure_v1

V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）


def _run(term, **kw):
    from sherpa.impact_service import run_impact
    ensure_v1()
    drv = driver()
    try:
        with drv.session() as s:
            return run_impact(s, term, V, **kw)
    finally:
        drv.close()


def test_golden_structural_impact():
    res = _run("TAX-RATE")
    by = {i["name"]: i for i in res["items"]}
    names = set(by)
    assert {"TAX-CPY", "TAXCALC", "BILLGEN", "SALESUP", "NIGHTLY"} <= names
    assert "judgement" not in by["TAXCALC"] and "extraction_method" not in by["TAXCALC"]   # K12: 判定表示は撤去済み
    assert by["TAXCALC"]["category"] == "ソース"
    assert by["NIGHTLY"]["category"] == "バッチ"
    assert by["BILLGEN"]["trace"] and by["BILLGEN"]["evidence"]          # 経路(ノード名列)＋根拠
    assert by["TAXCALC"]["evidence"][0]["doc"].startswith("4期/")        # 根拠=rel_path
    assert not (names & {"CUSTMNT", "CUSTOMER-CPY"})                     # precision 番人（税と非連結の孤島）


def test_business_term_has_no_structural_bridge_falls_back_to_presumed():
    """K10: REALIZES 橋の撤去により、業務語「消費税率」は graph 側で直接コードへ解決しない
    （structural items は0件）——presumed（grep 共起の推定）が代わりに関連コードを示す。"""
    res = _run("消費税率")
    assert res["items"] == []
    presumed_names = {p["name"] for p in (res.get("presumed") or [])}
    assert {"BILLGEN", "TAXCALC"} <= presumed_names


def test_code_silent_steers_to_search():
    """構造的な影響が0件（presumed のみ/0件）なら検索へ誘導＋code_silent。構造的な影響ありなら誘導しない。純関数・DB不要。"""
    from sherpa.chat_service import _answer_impact
    silent = {"start": "夜間バッチ", "presumed": [], "starts": [], "items": []}
    env = _answer_impact(silent, "test")
    assert env["summary"]["code_silent"] is True and "検索" in env["headline"]
    assert env["suggest"] and env["suggest"]["lens"] == "qa" and env["suggest"]["query"] == "夜間バッチ"
    none_ = {"start": "未知語", "items": [], "presumed": [], "starts": []}
    assert _answer_impact(none_, "test")["summary"]["code_silent"] is True
    code = {"start": "TAX-RATE", "presumed": [], "starts": [],
            "items": [{"name": "TAXCALC", "category": "ソース", "evidence": []}]}
    env2 = _answer_impact(code, "test")
    assert env2["summary"]["code_silent"] is False and "検索" not in env2["headline"] and env2["suggest"] is None


def test_scope_bounds_impact():
    """範囲（フォルダ prefix）で絞ると影響は増えない（鏡＝subgraph・共通の自動合流はしない）。
    起点（TAX-RATE＝`4期/00_共通/標準コピーブック`）を含まない範囲は起点ごと引けず0件。
    起点を含む世代全体まで広げれば unscoped と一致する（世代内で完結・過不足なし）。
    """
    full = {i["name"] for i in _run("TAX-RATE")["items"]}
    assert full   # 網羅性の前提（他テストで固定済みの中身は _run("TAX-RATE") 側の別テストに委ねる）
    same_gen = {i["name"] for i in _run("TAX-RATE", scope_prefixes=["4期"])["items"]}
    assert same_gen == full
    other = {i["name"] for i in _run("TAX-RATE", scope_prefixes=["4期/02_設計"])["items"]}
    assert other == set()


def test_golden_structural_impact_fee_theme():
    """第2テーマ（フェーズ7 S2・販売手数料率の改定）: 消費税テーマと独立した多段 CALL 連鎖
    （AGENTPAY→COMMISUP→FEECALC）＋共有コピーブック棚（FEE-CPY）が正しく解決される。
    """
    res = _run("FEE-RATE")
    by = {i["name"]: i for i in res["items"]}
    names = set(by)
    assert {"FEE-CPY", "FEECALC", "COMMISUP", "AGENTPAY", "MONTHLY"} <= names
    assert by["FEECALC"]["category"] == "ソース"
    assert by["MONTHLY"]["category"] == "バッチ"
    assert by["AGENTPAY"]["trace"] and by["AGENTPAY"]["evidence"]    # CALL 2段でも経路(ノード名列)＋根拠が付く
    assert by["FEECALC"]["evidence"][0]["doc"].startswith("4期/")    # 根拠=rel_path
    # 精度番人: 税テーマの固有コードは含まれない
    assert not (names & {"TAXCALC", "BILLGEN", "SALESUP"})


def test_theme_isolation_both_directions():
    """税テーマと手数料テーマは同一 world/世代内に同居しても相互に混入しない（precision・両方向）。"""
    tax_names = {i["name"] for i in _run("TAX-RATE")["items"]}
    fee_names = {i["name"] for i in _run("FEE-RATE")["items"]}
    assert not (tax_names & {"FEECALC", "COMMISUP", "AGENTPAY"})
    assert not (fee_names & {"TAXCALC", "BILLGEN", "SALESUP"})
