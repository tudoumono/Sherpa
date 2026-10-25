"""`GET /ingest/workers`（取り込み画面の版の照合）: ワーカーが動いていない・版が違うとき、画面に出すお知らせが返る。

Web アプリの版は `app_version.current()`。ワーカーの版は心拍（`ingest_workers`）から読む。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sherpa import app_version, store
from sherpa.ingest import worker_status
from sherpa.store import db as store_db
from sherpa.store import workers as store_workers

_ID = "qtest-api-worker"


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    _clean()
    yield TestClient(app, raise_server_exceptions=False)
    _clean()


def _clean() -> None:
    with store_db._connect() as c:
        c.execute("DELETE FROM ingest_workers")   # テスト専用 DB（使い捨て）。他の心拍が混ざると判定が変わるため全消去する


def _beat(version: str | None, state: str = "idle") -> None:
    store_workers.beat(_ID, role="ingest", app_version=version, state=state, pid=1)


def test_no_worker_shows_not_running_message(client):
    body = client.get("/ingest/workers").json()
    assert body["worker_alive"] is False and body["version_mismatch"] is False
    assert body["message"] == worker_status.MSG_NO_WORKER
    assert "取り込みのワーカーが動いていません" in body["message"]


def test_worker_with_other_version_shows_mismatch_message(client):
    _beat("0.0.1+deadbeef")
    body = client.get("/ingest/workers").json()
    assert body["web_version"] == app_version.current() and body["worker_version"] == "0.0.1+deadbeef"
    assert body["worker_alive"] is True and body["version_mismatch"] is True
    assert body["message"] == ("ワーカーの版が Web アプリと違うため、取り込みを止めています。"
                               "make restart で両方を同じ版にしてください")


def test_worker_with_same_version_has_no_message(client):
    _beat(app_version.current(), state="running")
    body = client.get("/ingest/workers").json()
    assert body["worker_alive"] is True and body["version_mismatch"] is False
    assert body["worker_state"] == "running" and body["message"] is None


def test_stale_heartbeat_counts_as_not_running(client):
    _beat(app_version.current())
    with store_db._connect() as c:
        c.execute("UPDATE ingest_workers SET beat_at = now() - interval '10 minutes'")
    body = client.get("/ingest/workers").json()
    assert body["worker_alive"] is False and body["message"] == worker_status.MSG_NO_WORKER


def test_ocr_worker_stop_is_returned_as_plain_notice(client, monkeypatch):
    from sherpa.ingest import ocr_worker, office_md
    from sherpa.store import ocr_jobs

    monkeypatch.setattr(office_md, "ocr_enabled", lambda: True)
    monkeypatch.setattr(ocr_worker, "profile_hash", lambda: "sha256:test")
    monkeypatch.setattr(ocr_jobs, "worker_availability_summary", lambda _profile: {"available": False})

    body = client.get("/ingest/workers").json()

    assert body["ocr_message"] == (
        "画像の中の文字を読む処理が動いていません。検索には、すでに読み取れた内容だけが使われます")
