"""`GET/PUT /worlds/{wid}/resolve-settings`（資料フォルダの解決範囲の設定・ANA-15 P5）の業務ロジック。401/403 は `test_authz_matrix.py` が担保する。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stub(monkeypatch, tmp_path):
    from sherpa import store, worlds
    state = {"row": {"world_id": "w1", "resolve_settings": None}, "audits": []}
    (tmp_path / "G" / "lib").mkdir(parents=True)
    monkeypatch.setattr(store, "get_world", lambda wid: state["row"] if wid == "w1" else None)
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(worlds, "archives_dir", lambda wid: tmp_path / "_arch")
    monkeypatch.setattr(store, "set_resolve_settings",
                        lambda wid, s: state["row"].update(resolve_settings=s) or True)
    monkeypatch.setattr(store, "audit", lambda *a, **k: state["audits"].append((a, k)))
    from sherpa.routers import worlds as worlds_routes
    state["dispatched"] = []
    state["dispatch_exc"] = None

    def _dispatch(wid, op, fp, work_fn, **kw):
        if state["dispatch_exc"]:
            raise state["dispatch_exc"]
        state["dispatched"].append((wid, op))
        return 7, False
    monkeypatch.setattr(worlds_routes, "_dispatch", _dispatch)
    return state


def test_put_saves_normalized_settings_warns_missing_and_audits(client, stub):
    r = client.put("/worlds/w1/resolve-settings",
                   json={"copy_paths": ["/G/lib/", "G/none"], "path_aliases": {"inc": "G\\lib"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["copy_paths"] == ["G/lib", "G/none"] and body["path_aliases"] == {"inc": "G/lib"}
    assert body["changed"] is True and len(body["warnings"]) == 1 and "G/none" in body["warnings"][0]
    assert stub["row"]["resolve_settings"]["copy_paths"] == ["G/lib", "G/none"]
    assert stub["audits"] and stub["audits"][0][0][1] == "world.resolve_settings_updated"
    assert body["refresh_started"] is True and stub["dispatched"] == [("w1", "refresh")]   # 保存で取り込み直しを起こす
    again = client.put("/worlds/w1/resolve-settings", json={"copy_paths": body["copy_paths"], "path_aliases": body["path_aliases"]}).json()
    assert again["changed"] is False
    assert client.get("/worlds/w1/resolve-settings").json()["copy_paths"] == ["G/lib", "G/none"]


def test_audit_failure_restores_settings_and_returns_503(client, stub, monkeypatch):
    from sherpa import store

    def boom(*a, **k):
        raise RuntimeError("audit down")
    monkeypatch.setattr(store, "audit", boom)
    r = client.put("/worlds/w1/resolve-settings", json={"copy_paths": ["G/lib"]})
    assert r.status_code == 503
    assert stub["row"]["resolve_settings"] is None and stub["dispatched"] == []   # 監査が書けなければ取り込みは起動しない


def test_save_while_another_run_is_active_reports_pending_refresh(client, stub):
    from fastapi import HTTPException
    stub["dispatch_exc"] = HTTPException(409, "busy")
    body = client.put("/worlds/w1/resolve-settings", json={"copy_paths": ["G/lib"]}).json()
    assert body["changed"] is True and body["refresh_started"] is False and "更新が必要" in body["note"]


def test_dispatch_failure_other_than_busy_restores_settings(client, stub):
    from fastapi import HTTPException
    stub["dispatch_exc"] = HTTPException(503, "shutting down")
    assert client.put("/worlds/w1/resolve-settings", json={"copy_paths": ["G/lib"]}).status_code == 503
    assert stub["row"]["resolve_settings"] is None
    # 戻したことを監査に別の行で残す
    assert [a[0][1] for a in stub["audits"]] == ["world.resolve_settings_updated", "world.resolve_settings_reverted"]


@pytest.mark.parametrize("payload", [
    {"copy_paths": ["G/lib"], "unknown": 1},
    {"copy_paths": ["G/../H"]}, {"copy_paths": [""]}, {"path_aliases": {"a": "b/x", "b": "a/y"}}])
def test_put_rejects_invalid_settings(client, stub, payload):
    assert client.put("/worlds/w1/resolve-settings", json=payload).status_code == 422
    assert stub["row"]["resolve_settings"] is None


def test_unknown_world_is_404(client, stub):
    assert client.get("/worlds/zz/resolve-settings").status_code == 404
    assert client.put("/worlds/zz/resolve-settings", json={}).status_code == 404


def test_store_roundtrip_changes_world_signature():
    """実 DB: 設定の保存・読み出し・消去と、署名が設定の有無・内容で変わること（既存 DB 向けの ALTER も `_ensure` 経由で通る）。"""
    from sherpa import store
    from sherpa.ingest import resolve_settings
    wid = "p5_roundtrip"
    store.upsert_world(wid, "/tmp/p5-roundtrip-root")
    try:
        assert resolve_settings.signature_of(wid) == ""
        cfg = resolve_settings.normalize({"copy_paths": ["G/lib"], "path_aliases": {"a": "G/x"}})
        assert store.set_resolve_settings(wid, cfg) is True
        assert resolve_settings.load(wid) == cfg and resolve_settings.signature_of(wid) != ""
        # 設定が最後のグラフに反映済みかは、確定時に記録するハッシュとの比較で分かる
        row = store.get_world_status_row(wid)
        assert resolve_settings.signature_material(row["resolve_settings"]) != (row["resolve_applied_sig"] or "")
        run = store.start_ingest_run(wid, scan_root=None, created_by="admin")
        store.finish_ingest_run_and_confirm_world(run["id"], wid, status="auto_published", sig="s",
                                                  resolve_sig=resolve_settings.signature_material(cfg))
        row = store.get_world_status_row(wid)
        assert resolve_settings.signature_material(row["resolve_settings"]) == row["resolve_applied_sig"]
        assert store.set_resolve_settings(wid, None) is True
        assert resolve_settings.load(wid) == resolve_settings.empty()
        assert store.set_resolve_settings("p5_no_such_world", cfg) is False
    finally:
        store.delete_world_row(wid)
