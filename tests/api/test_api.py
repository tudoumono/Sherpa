"""影響分析 受け入れ（API・要 Neo4j）: POST /impact/run → GET /impact/{id}（鏡モデル）。

S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: 意味層フル抽出・REALIZES 橋・名寄せは撤去済み。
業務語（「消費税率」等）はもう graph 側の橋渡しで直接コードへ解決しない——起点語はコード自身の
識別子（`TAX-RATE`/`FEE-RATE` 等）を使う。K12: 「確実/要確認」の2値判定は機構ごと撤去（全件同格・
`judgement` キーは応答から消えている）。
"""
from __future__ import annotations

import pytest
from _world_setup import TEST_WORLD_ID, ensure_v1
from fastapi.testclient import TestClient

from sherpa.api import app

client = TestClient(app)
V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def test_impact_run_and_get():
    ensure_v1()
    r = client.post("/impact/run", json={"start": "TAX-RATE", "world": V})
    assert r.status_code == 200, r.text
    body = r.json()
    aid = body["analysis_id"]

    g = client.get(f"/impact/{aid}")
    assert g.status_code == 200
    items = g.json()["items"]
    assert body["count"] == len(items)   # POST の集計（run 直後の count）と GET の件数が一致（総数ピンは廃止）
    assert "judgement" not in body       # K12: 判定表示は撤去済み（全件同格）
    names = {i["name"] for i in items}
    assert {"TAXCALC", "BILLGEN", "NIGHTLY"} <= names
    assert "judgement" not in items[0]
    assert not (names & {"CUSTMNT", "CUSTOMER-CPY"})  # precision（税と非連結の孤島）
    # 第2テーマ（手数料率）の要素が混入しない（フェーズ7 S2・相互 precision）
    assert not (names & {"FEECALC", "COMMISUP", "AGENTPAY", "MONTHLY"})


def test_impact_run_and_get_fee_theme():
    """第2テーマ「販売手数料率の改定」（フェーズ7 S2）: CALL 多段連鎖 AGENTPAY→COMMISUP→FEECALC を波及として拾う。"""
    ensure_v1()
    r = client.post("/impact/run", json={"start": "FEE-RATE", "world": V})
    assert r.status_code == 200, r.text
    body = r.json()
    aid = body["analysis_id"]

    g = client.get(f"/impact/{aid}")
    assert g.status_code == 200
    items = g.json()["items"]
    assert body["count"] == len(items)
    names = {i["name"] for i in items}
    assert {"FEECALC", "COMMISUP", "AGENTPAY", "MONTHLY"} <= names
    # 税テーマ側の要素は混入しない（precision・両方向）
    assert not (names & {"TAXCALC", "BILLGEN", "SALESUP", "NIGHTLY"})


def test_scope_filter_and_validation():
    ensure_v1()
    # 既知のフォルダ prefix は通る（影響は world 全体以下）
    r = client.post("/impact/run", json={"start": "TAX-RATE", "world": V,
                                         "scope_paths": ["4期"]})
    assert r.status_code == 200 and r.json()["count"] >= 1
    # 未知の範囲は 422
    bad = client.post("/impact/run", json={"start": "TAX-RATE", "world": V,
                                           "scope_paths": ["存在しない/フォルダ"]})
    assert bad.status_code == 422


def test_not_found():
    assert client.get("/impact/99999").status_code == 404
