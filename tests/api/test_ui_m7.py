"""画面 受け入れ（鏡モデル）: 抽出プレビュー API ＋ 静的UI 配信。preview は world グラフ由来＝Neo4j 不要。"""
from __future__ import annotations

import pytest
from _world_setup import SPEC, TAXCALC

from sherpa.preview_service import build_preview

V = "v1"


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def test_preview_extraction_structure():
    """S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: 意味層フル抽出・REALIZES 橋・名寄せ
    （merges）は撤去済み——entities/relations は骨格（Pass1/Pass2）＋言及エッジ（Pass3）のみ。
    K12: entities_llm/entities_both 等は撤去済み（全件 static のため無意味）。"""
    pv = build_preview(V)
    assert "merges" not in pv
    ents = {(e["name"], e["label"]) for e in pv["entities"]}
    assert ("TAX-RATE", "DataItem") in ents
    assert ("TAXCALC", "Module") in ents
    rel_types = {r["type"] for r in pv["relations"]}
    assert rel_types <= {"COPIES", "CONTAINS", "INVOKES", "ACCESSES", "DOCUMENTS"}
    assert {"COPIES", "CONTAINS", "INVOKES"} <= rel_types
    # 骨格＋言及のみ＝供給源を失った status（deprecated/hidden_candidate）はもう作られない。
    assert pv["counts"]["deprecated"] == 0 and pv["counts"]["hidden"] == 0
    assert pv["counts"]["entities_static"] == pv["counts"]["entities"] > 0
    assert pv["counts"]["relations_static"] == pv["counts"]["relations"] > 0


def test_preview_documents_path_based():
    """文書一覧は doc_id＝rel_path・ソース枝/Office枝が並ぶ（鏡＝フォルダ木）。"""
    docs = {d["name"]: d for d in build_preview(V)["documents"]}
    assert TAXCALC in docs and docs[TAXCALC]["branch"] == "source"
    assert SPEC in docs and docs[SPEC]["branch"] == "office"
    assert docs[TAXCALC]["top_scope"] == "4期" and docs[TAXCALC]["state"] == "ready"


def test_preview_endpoint_and_ui_served():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    pv = c.get("/ingest/preview", params={"world": V})
    assert pv.status_code == 200 and {"counts", "entities", "relations", "documents"} <= pv.json().keys()
    assert "merges" not in pv.json()   # S3・K9-K11: 名寄せ（REALIZES 由来）は撤去済み
    for page in ("chat.html", "chat.js", "graph.html", "graph.js", "ingest.html", "ingest.js",
                 "app.css", "vendor/cytoscape.min.js"):
        r = c.get(f"/ui/{page}")
        assert r.status_code == 200, f"/ui/{page} -> {r.status_code}"
    assert "Sherpa" in c.get("/ui/chat.html").text


def test_ui_static_cache_control_no_cache():
    """S3: /ui 配信の Cache-Control（実ユーザー再報告「保存バーが見えない」＝旧アセット固着対策）。

    html/css/js は毎回サーバへ確認させる（no-cache＝ETag 等での再検証は残るので 304 で軽い）。
    フォントは自己ホストせず固定スタック（fonts.css）で指定するため woff2 は配信しない。
    """
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    for page in ("settings.html", "settings.js", "chat.js", "app.css", "fonts.css"):
        r = c.get(f"/ui/{page}")
        assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache", page


def test_graph_and_worlds():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    g = c.get("/graph", params={"world": V}).json()
    assert g["nodes"] and g["edges"]
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])   # 孤立エッジなし
    # S3・K9-K11: 業務 Parameter（REALIZES 由来）は撤去済み——骨格ノード（コード）で確認する。
    assert any(n["name"] == "TAXCALC" and n["type_ja"] == "プログラム" for n in g["nodes"])
    assert "v1" in c.get("/world-options").json()["worlds"]
    root = c.get("/", follow_redirects=False)
    assert root.status_code in (307, 308) and root.headers["location"] == "/ui/home.html"
