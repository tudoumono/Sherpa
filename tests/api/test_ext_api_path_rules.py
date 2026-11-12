"""外部連携 API（/ext/v1）の出口に、管理者の除外（`path_rules`）が効くこと。要 Postgres（DB 不可は skip）。
ヘルパとフィクスチャは `test_ext_api.py` のものを使う。
"""
from __future__ import annotations

from test_ext_api import _TAX_DOC, _audit_writer_running, _db, _doc, _key, _search  # noqa: F401
from sherpa.parts.read import fused_search


def test_search_boundary_drops_excluded_hits(monkeypatch):
    """外部 API の検索の応答にも出口の濾過を通す（除外の資料のヒットは落ち、件数の欄は見える分で数え直す）。"""
    import json as _json
    from sherpa import path_rules
    monkeypatch.setenv(path_rules.ENV_KEY, _json.dumps({"v1": [{"path": "4期/01_標準", "kind": "exclude", "outside_scope": False}]}))
    hit = lambda d: {"doc_id": d, "path": d, "snippet": "s", "score": 1.0, "sources": {"keyword": 1}}  # noqa: E731
    monkeypatch.setattr(fused_search, "search", lambda *a, **k: {
        "hits": [hit(_TAX_DOC), hit("4期/04_運用/障害記録.md")], "engines_used": ["keyword"], "degraded": []})
    r = _search({"world": "v1", "query": "税"}, _key("srchx")["key"])
    assert r.status_code == 200, r.text
    assert [h["doc_id"] for h in r.json()["hits"]] == ["4期/04_運用/障害記録.md"]


def test_doc_refuses_admin_excluded_path(monkeypatch):
    """管理者が検索から外した資料は、既知のパスでも配らない（404）。除外の設定を読めないときは 503。"""
    import json as _json
    from sherpa import path_rules
    key = _key("docexcl")["key"]
    monkeypatch.setenv(path_rules.ENV_KEY, _json.dumps({"v1": [{"path": "4期/01_標準", "kind": "exclude", "outside_scope": False}]}))
    assert _doc("v1", _TAX_DOC, key).status_code == 404
    assert _doc("v1", "4期/04_運用/../01_標準/消費税法.md", key).status_code == 404
    monkeypatch.delenv(path_rules.ENV_KEY)
    assert _doc("v1", _TAX_DOC, key).status_code == 200
