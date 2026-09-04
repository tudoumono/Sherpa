"""sherpa/routers/worlds.py の背景実行分岐（`/ingest/rerun`・`_run_worker_or_503`・world 新規登録の
排他制御 等）。旧 `/worlds/{wid}/extract`・`concepts/propose|confirm|disable` の分岐テストは
GRAPH-SRC（2026-09-04・K9-K11）でその供給元（`_run_extract_background` 等）ごと撤去済み。

TestClient を経由せず、ルートハンドラ関数を直接呼ぶ（DB 非依存＝store/worker 等を monkeypatch）。
`_current_user`/`_require_admin`/`valid_world` 等は routers/worlds.py が通常 import
（`from sherpa.deps import ...`）しているため、facade ではなくルータモジュール側の名前
（`worlds_routes._current_user` 等）を monkeypatch する
（tests/unit/test_shares_router_branches.py と同じ事情）。
"""
from __future__ import annotations

import contextlib
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sherpa.routers import worlds as worlds_routes


def _req():
    return SimpleNamespace(cookies={}, headers={}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(worlds_routes, "_current_user", lambda request: {"uid": "admin1"})
    monkeypatch.setattr(worlds_routes, "_require_admin", lambda u: None)
    monkeypatch.setattr(worlds_routes, "valid_world", lambda w: True)
    yield


def test_ingest_rerun_accepts_immediately_with_pre_created_run_id(monkeypatch):
    """`/ingest/rerun` は即受付する（ING-3・背景実行）。`run_id` は受付処理自身
    （`_dispatch`）が O(1) の `store.start_ingest_run` で確保してから背景実行へ渡すため、応答の
    時点で**必ず**非 null（旧: run_id 未確定のまま `None` を返すことがあった）。その run_id が
    `ingest_worker.rerun` へそのまま渡されることを確認する。"""
    monkeypatch.setattr(worlds_routes, "_resolve_world", lambda w: "wtest")
    monkeypatch.setattr(worlds_routes.store, "start_ingest_run",
                        lambda world, **kw: {"id": 321, "version": world, "status": "extracting"})

    calls = []
    released = threading.Event()

    def _fake_rerun(w, run_id=None):
        calls.append((w, run_id))
        released.wait(timeout=2.0)
    monkeypatch.setattr(worlds_routes.ingest_worker, "rerun", _fake_rerun)

    from sherpa.ingest import background
    try:
        res = worlds_routes.ingest_rerun(worlds_routes.RerunReq(world="wtest"), _req())
        assert res["ok"] is True and res["world_id"] == "wtest"
        assert res["run_id"] == 321 and res["joined"] is False

        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == [("wtest", 321)]
    finally:
        released.set()
        # レジストリから完全に消えるまで待つ（次テストが同じ world_id "wtest" を使うため・
        # 未完了のまま抜けると次テストが誤って「合流」してしまう）。
        deadline = time.monotonic() + 2.0
        while background.is_running("wtest") and time.monotonic() < deadline:
            time.sleep(0.01)


def test_ingest_rerun_background_worker_exception_is_caught_by_outer_safety_net(monkeypatch):
    """`ingest_worker.rerun` が想定外の例外を bare raise しても受付応答自体には影響せず、
    最外周のセーフティネット（`store.fail_close_if_extracting`）が run を failed へ
    格下げする（`_dispatch`/`background.start_or_join` 自身はもう best-effort な一発 INSERT
    フォールバックを持たない——run_id は受付時点で既に確定しているため）。"""
    monkeypatch.setattr(worlds_routes, "_resolve_world", lambda w: "wtest")
    monkeypatch.setattr(worlds_routes.store, "start_ingest_run",
                        lambda world, **kw: {"id": 321, "version": world, "status": "extracting"})

    def _boom(w, run_id=None):
        raise RuntimeError("simulated PG/Neo4j failure")
    monkeypatch.setattr(worlds_routes.ingest_worker, "rerun", _boom)

    fail_close_calls = []
    monkeypatch.setattr(worlds_routes.store, "fail_close_if_extracting",
                        lambda run_id, reason: fail_close_calls.append((run_id, reason)) or True)

    res = worlds_routes.ingest_rerun(worlds_routes.RerunReq(world="wtest"), _req())
    assert res["run_id"] == 321 and res["joined"] is False

    from sherpa.ingest import background
    deadline = time.monotonic() + 2.0
    while background.is_running("wtest") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fail_close_calls and fail_close_calls[0][0] == 321


def test_run_worker_or_503_skips_duplicate_recording_when_already_recorded(monkeypatch):
    """worker 側（`_run_locked` の pg_replace 失敗等）が既に詳細な理由付きで `ingest_runs`
    へ記録済みの例外（`_sherpa_ingest_run_recorded` 属性で示される）は、
    `_run_worker_or_503` が汎用な理由で二重に記録しない（1回の取り込みで ingest_runs に
    2件残ることを防ぐ）。"""
    recorded = []
    monkeypatch.setattr(worlds_routes.store, "add_ingest_run", lambda wid, **kw: recorded.append(kw) or {"id": 1})

    def _boom():
        e = RuntimeError("already recorded by worker")
        e._sherpa_ingest_run_recorded = True
        raise e

    with pytest.raises(HTTPException) as ei:
        worlds_routes._run_worker_or_503("wtest", _boom)

    assert ei.value.status_code == 503
    assert ei.value.detail == worlds_routes._INGEST_UNAVAILABLE_MESSAGE
    assert recorded == [], "worker 側で記録済みなら _run_worker_or_503 は再記録しない"


def test_run_worker_or_503_records_when_not_already_recorded(monkeypatch):
    """`_sherpa_ingest_run_recorded` マーカーが無い（worker 側で記録されなかった）想定外の
    例外は、従来どおり `_run_worker_or_503` 自身が汎用な理由で記録する。"""
    recorded = []
    monkeypatch.setattr(worlds_routes.store, "add_ingest_run", lambda wid, **kw: recorded.append(kw) or {"id": 1})

    def _boom():
        raise RuntimeError("not yet recorded")

    with pytest.raises(HTTPException) as ei:
        worlds_routes._run_worker_or_503("wtest", _boom)

    assert ei.value.status_code == 503
    assert recorded and recorded[0]["status"] == "failed"


def test_world_create_new_registration_arbitrated_by_fixed_key_not_provisional_wid(monkeypatch):
    """未登録 root への新規登録は暫定 wid 単位でなく固定キー
    （`worlds_routes._NEW_WORLD_REGISTRY_KEY`）で仲裁する——別フォルダへの競合登録要求は
    run 作成前（`store.start_ingest_run` 呼び出し前）に 409 になる（暫定 wid が別々だと
    in-process レジストリでは衝突が見えず、両方受付済みになってしまう旧い穴の回帰確認）。"""
    monkeypatch.setattr(worlds_routes.world_admin_service, "resolve_root", lambda path: path)
    monkeypatch.setattr(worlds_routes.store, "world_by_root", lambda root: None)   # 常に未登録
    monkeypatch.setattr(worlds_routes.store, "list_worlds_db", lambda: [])         # 単一登録契約: まだ0件
    monkeypatch.setattr(worlds_routes.world_admin_service, "generate_world_id",
                        lambda label, root: "w-" + (label or "x"))

    run_calls: list[str] = []

    def _start_ingest_run(wid, **kw):
        run_calls.append(wid)
        return {"id": len(run_calls)}

    monkeypatch.setattr(worlds_routes.store, "start_ingest_run", _start_ingest_run)

    started = threading.Event()
    released = threading.Event()

    def _slow_register_or_rerun(*aa, **kw):
        started.set()
        released.wait(timeout=2.0)

    monkeypatch.setattr(worlds_routes.world_admin_service, "register_or_rerun", _slow_register_or_rerun)

    req_a = worlds_routes.WorldReq(path="/mnt/folderA", label="folderA", world_id=None)
    t = threading.Thread(target=lambda: worlds_routes.world_create(req_a, _req()))
    t.start()
    try:
        assert started.wait(timeout=2.0), "A の受付処理（register_or_rerun）が開始しなかった"
        req_b = worlds_routes.WorldReq(path="/mnt/folderB", label="folderB", world_id=None)
        with pytest.raises(HTTPException) as exc_info:
            worlds_routes.world_create(req_b, _req())
        assert exc_info.value.status_code == 409
        assert run_calls == ["w-folderA"]   # B は run を作成する前に弾かれた（"w-folderB" が現れない）
    finally:
        released.set()
        t.join(timeout=2.0)
