"""`POST /worlds/disk-estimate`（登録・更新を始める前のディスク容量の見積もり）の API 層テスト。

401/403 は `tests/api/test_authz_matrix.py` が全ルート横断で担保するため、ここでは応答の形と、
見積もりを取ったあとに登録・更新を受け付けたときの開始ログに絞る。外部境界（ES・Docker・ディスクの空き）は
`place_filesystems` で固定し、原本の木は実ファイルで作る。
"""
from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient

from sherpa import disk_estimate as de

MB = de.MB
GIB = de.GIB


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    de._JOBS.clear()
    yield TestClient(app, raise_server_exceptions=False)
    de._JOBS.clear()


@pytest.fixture(autouse=True)
def _boundaries(monkeypatch):
    from sherpa import es_index
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    monkeypatch.setattr(de, "place_filesystems",
                        lambda wid: {p: de.Fs(("dev", 1), 500 * GIB, 600 * GIB) for p in de.PLACES})
    monkeypatch.setattr(es_index, "store_bytes", lambda wid: 2 * GIB)


def _poll(client, body, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = client.post("/worlds/disk-estimate", json=body)
        assert r.status_code == 200, r.text
        d = r.json()
        if d["state"] != "counting":
            return d
        time.sleep(0.05)
    raise AssertionError("見積もりが終わらない")


def _tree(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.xlsx").write_bytes(b"x" * 2 * MB)
    (root / "b.pdf").write_bytes(b"y" * 1 * MB)
    (root / "c.txt").write_text("memo")
    return root


def test_register_estimate_returns_per_filesystem_lines(client, monkeypatch, tmp_path):
    from sherpa import store
    monkeypatch.setattr(store, "world_by_root", lambda root: None)
    d = _poll(client, {"path": str(_tree(tmp_path))})
    assert d["state"] == "ready" and d["mode"] == "register"
    assert d["files"] == 3 and d["status"] == "ok"
    assert d["lines"] and d["lines"][0]["need_bytes"] > 0 and "取り込みに必要な空き" in d["lines"][0]["text"]


def test_registered_folder_is_estimated_as_an_update_without_recounting_what_is_in_use(client, monkeypatch, tmp_path):
    import os

    from sherpa import store, worlds
    root = _tree(tmp_path)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    rag, cache = worlds.derived_rag_dir("w1"), worlds.derived_dir("w1") / "_conv_cache"
    rag.mkdir(parents=True)
    cache.mkdir(parents=True)
    for i in range(4):                                                        # 変換のキャッシュと派生物がハードリンクで共有されている状態
        (rag / f"f{i}.rag.md").write_text("x")
        os.link(rag / f"f{i}.rag.md", cache / f"f{i}.rag.md")
    st = (root / "a.xlsx").stat()
    manifest = {"a.xlsx": [st.st_mtime_ns, st.st_ctime_ns, st.st_size]}     # a.xlsx は取り込み済み、残りは追加
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid, "root_path": str(root), "last_manifest": manifest})
    d = _poll(client, {"world_id": "w1"})
    assert d["mode"] == "update" and d["files"] == 3 and d["changed_files"] == 2
    assert d["needs"]["es"] >= 2 * GIB                                        # 作り直し中の新旧 2 世代
    full = de.compute_needs(*de.count_sources(root), mode="register", dim=0)
    assert d["needs"]["derived"] < full["derived"]


def test_unknown_world_and_bad_path_are_rejected(client, monkeypatch, tmp_path):
    from sherpa import store
    monkeypatch.setattr(store, "get_world", lambda wid: None)
    assert client.post("/worlds/disk-estimate", json={"world_id": "nope"}).status_code == 404
    assert client.post("/worlds/disk-estimate", json={"path": str(tmp_path / "missing")}).status_code == 422
    assert client.post("/worlds/disk-estimate", json={}).status_code == 422


def test_estimate_failure_still_answers_200_so_the_screen_can_start_anyway(client, monkeypatch, tmp_path):
    from sherpa import store
    monkeypatch.setattr(store, "world_by_root", lambda root: None)
    monkeypatch.setattr(de, "count_sources", lambda *a, **k: (_ for _ in ()).throw(OSError("share gone")))
    d = _poll(client, {"path": str(_tree(tmp_path))})
    assert d["state"] == "failed" and "取り込みはそのまま始められます" in d["message"]


def test_refresh_logs_the_estimate_at_start_and_warns_when_short(client, monkeypatch, tmp_path, caplog):
    from sherpa import store
    from sherpa.routers import worlds as worlds_routes
    root = _tree(tmp_path)
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid, "root_path": str(root), "last_manifest": {}})
    monkeypatch.setattr(worlds_routes, "_dispatch", lambda *a, **k: (1, False))
    d = _poll(client, {"world_id": "w1"})
    assert d["status"] == "ok"
    with caplog.at_level(logging.INFO, logger="sherpa"):
        assert client.post("/worlds/w1/refresh").status_code == 202
    ok_logs = [r for r in caplog.records if "disk_estimate: 取り込み開始" in r.getMessage()]
    assert len(ok_logs) == 1 and ok_logs[0].levelno == logging.INFO and "op=refresh" in ok_logs[0].getMessage()

    de._JOBS.clear()
    monkeypatch.setattr(de, "place_filesystems",
                        lambda wid: {p: de.Fs(("dev", 1), 1 * MB, 600 * GIB) for p in de.PLACES})
    assert _poll(client, {"world_id": "w1"})["status"] == "short"
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sherpa"):
        assert client.post("/worlds/w1/refresh").status_code == 202
    warn = [r for r in caplog.records if "disk_estimate: 取り込み開始" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING and "判定=short" in warn[0].getMessage()


def test_joined_request_does_not_log_a_second_start(client, monkeypatch, tmp_path, caplog):
    from sherpa import store
    from sherpa.routers import worlds as worlds_routes
    root = _tree(tmp_path)
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid, "root_path": str(root), "last_manifest": {}})
    monkeypatch.setattr(worlds_routes, "_dispatch", lambda *a, **k: (1, True))
    _poll(client, {"world_id": "w1"})
    with caplog.at_level(logging.INFO, logger="sherpa"):
        assert client.post("/worlds/w1/refresh").status_code == 202
    assert not [r for r in caplog.records if "disk_estimate: 取り込み開始" in r.getMessage()]
