"""sherpa/routers/worlds.py の受付分岐（`/ingest/rerun`・`_run_worker_or_503`・world 新規登録の
排他制御 等）。旧 `/worlds/{wid}/extract`・`concepts/propose|confirm|disable` の分岐テストは
GRAPH-SRC（2026-09-04・K9-K11）でその供給元（`_run_extract_background` 等）ごと撤去済み。
受付は取り込みの依頼を `ingest_runs` へ queued で積むだけ（実行はワーカー）。積む部分は実 DB で確かめる。

TestClient を経由せず、ルートハンドラ関数を直接呼ぶ（store/worker 等を monkeypatch）。
`_current_user`/`_require_admin`/`valid_world` 等は routers/worlds.py が通常 import
（`from sherpa.deps import ...`）しているため、facade ではなくルータモジュール側の名前
（`worlds_routes._current_user` 等）を monkeypatch する
（tests/unit/test_shares_router_branches.py と同じ事情）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sherpa.routers import worlds as worlds_routes
from sherpa.store import db as store_db


def _req():
    return SimpleNamespace(cookies={}, headers={}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(worlds_routes, "_current_user", lambda request: {"uid": "admin1"})
    monkeypatch.setattr(worlds_routes, "_require_admin", lambda u: None)
    monkeypatch.setattr(worlds_routes, "valid_world", lambda w: True)
    yield


@pytest.fixture
def queue_rows():
    """このテストが積んだ run を片付ける（world_id は `rtest-` 始まりに限る）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    yield
    with store_db._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version LIKE 'rtest-%'")


def _rows(world_like: str = "rtest-%") -> list[dict]:
    with store_db._connect() as c:
        return c.execute("SELECT id, version, status, op, payload, fingerprint, app_version, arb_keys "
                         "FROM ingest_runs WHERE version LIKE %s ORDER BY id", (world_like,)).fetchall()


def test_ingest_rerun_only_queues_a_request_and_does_not_run_it(monkeypatch, queue_rows):
    """Web アプリは取り込みを動かさない。`/ingest/rerun` の受付は queued の依頼を 1 行積んで run_id を返すだけで、
    `ingest_worker.rerun` はこのプロセスで呼ばれない。"""
    monkeypatch.setattr(worlds_routes, "_resolve_world", lambda w: "rtest-rerun")
    monkeypatch.setattr(worlds_routes.ingest_worker, "rerun",
                        lambda *a, **kw: pytest.fail("Web アプリのプロセスで取り込みを実行してはいけない"))

    res = worlds_routes.ingest_rerun(worlds_routes.RerunReq(world="rtest-rerun"), _req())

    assert res["ok"] is True and res["world_id"] == "rtest-rerun" and res["joined"] is False
    rows = _rows("rtest-rerun")
    assert [(r["id"], r["status"], r["op"]) for r in rows] == [(res["run_id"], "queued", "rerun")]
    assert rows[0]["app_version"] == __import__("sherpa.app_version", fromlist=["current"]).current()

    again = worlds_routes.ingest_rerun(worlds_routes.RerunReq(world="rtest-rerun"), _req())
    assert again["run_id"] == res["run_id"] and again["joined"] is True       # 同じ操作の再要求は合流


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


def test_world_create_new_registration_arbitrated_by_fixed_key_not_provisional_wid(monkeypatch, queue_rows):
    """未登録 root への新規登録は暫定 wid 単位でなく固定キー
    （`worlds_routes._NEW_WORLD_REGISTRY_KEY`）で仲裁する——別フォルダへの競合登録要求は
    依頼を積む前に 409 になる（暫定 wid が別々だと衝突が見えず、両方受付済みになってしまう旧い穴の回帰確認）。"""
    monkeypatch.setattr(worlds_routes.world_admin_service, "resolve_root", lambda path: path)
    monkeypatch.setattr(worlds_routes.store, "world_by_root", lambda root: None)   # 常に未登録
    monkeypatch.setattr(worlds_routes.store, "list_worlds_db", lambda: [])         # 単一登録契約: まだ0件
    monkeypatch.setattr(worlds_routes.world_admin_service, "generate_world_id",
                        lambda label, root: "rtest-" + (label or "x"))

    req_a = worlds_routes.WorldReq(path="/mnt/folderA", label="folderA", world_id=None)
    first = worlds_routes.world_create(req_a, _req())
    assert first["world_id"] == "rtest-folderA" and first["joined"] is False

    req_b = worlds_routes.WorldReq(path="/mnt/folderB", label="folderB", world_id=None)
    with pytest.raises(HTTPException) as exc_info:
        worlds_routes.world_create(req_b, _req())
    assert exc_info.value.status_code == 409
    assert [r["version"] for r in _rows()] == ["rtest-folderA"]     # B は依頼を積む前に弾かれた

    same = worlds_routes.world_create(req_a, _req())                 # A の二重クリックは合流
    assert same["run_id"] == first["run_id"] and same["joined"] is True
