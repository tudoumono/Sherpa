"""外部 Codex ジョブの待ち列の上限と待ち期限（全体・鍵ごとの上限は 429、期限切れの queued は理由つきで終わり掃除の対象になる）。

要 Postgres。DB 不可は SKIP。上限は受付のトランザクション内で数えるため、API 経由の受付と `store_jobs.insert_job` の両方で確かめる。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sherpa import store
from sherpa.api import app
from sherpa.store import codex_jobs as store_jobs
from test_ext_codex_jobs import _new_keys, _result, _status, fake_codex  # noqa: F401  (fixture)

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


@pytest.fixture
def made_ids():
    ids: list[str] = []
    yield ids
    with store._connect() as c:
        c.execute("DELETE FROM codex_jobs WHERE id = ANY(%s)", (ids,))


def _insert(key_id: int, ids: list[str], n: int = 1) -> list[str]:
    out = []
    for _ in range(n):
        jid = store_jobs.new_job_id()
        store_jobs.insert_job(job_id=jid, key_id=key_id, world="v1", query="q", scope_paths=[],
                              depth="standard")
        ids.append(jid)
        out.append(jid)
    return out


def _post(key: str):
    return client.post("/ext/v1/codex/jobs", json={"world": "v1", "query": "x"},
                       headers={"X-API-Key": key})


def _queued_total() -> int:
    with store._connect() as c:
        return c.execute("SELECT count(*) AS n FROM codex_jobs WHERE status='queued'").fetchone()["n"]


def test_per_key_limit_returns_429_with_retry_after_and_other_key_is_unaffected(fake_codex, made_ids):
    a, b = _new_keys("cjq-a", "cjq-b")
    _insert(a["id"], made_ids, store_jobs.QUEUE_LIMIT_PER_KEY)
    r = _post(a["key"])
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) > 0
    r2 = _post(b["key"])
    assert r2.status_code == 202, r2.text
    made_ids.append(r2.json()["job_id"])


def test_total_limit_returns_429_for_a_key_with_no_queued_jobs(fake_codex, made_ids):
    per = store_jobs.QUEUE_LIMIT_PER_KEY
    need = store_jobs.QUEUE_LIMIT_TOTAL - _queued_total()
    if need < 0:
        pytest.skip("共有 DB に待ちジョブが上限を超えて残っている")
    keys = _new_keys(*[f"cjq-t{i}" for i in range(need // per + 2)])
    for k in keys[:-1]:
        _insert(k["id"], made_ids, min(per, store_jobs.QUEUE_LIMIT_TOTAL - _queued_total()))
    assert _queued_total() == store_jobs.QUEUE_LIMIT_TOTAL
    r = _post(keys[-1]["key"])
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers


def test_overdue_queued_job_ends_with_reason_is_not_claimed_and_is_swept(fake_codex, made_ids):
    k = _new_keys("cjq-od")[0]
    jid = _insert(k["id"], made_ids)[0]
    with store._connect() as c:
        c.execute("UPDATE codex_jobs SET queue_deadline=%s WHERE id=%s",
                  (datetime.now(timezone.utc) - timedelta(minutes=1), jid))
    assert jid not in [j["id"] for j in store_jobs.claim_queued_jobs(100)]
    # 期限切れは件数に数えない
    assert store_jobs.get_job(jid)["status"] == "queued"

    store_jobs.expire_due_jobs()
    job = store_jobs.get_job(jid)
    assert job["status"] == "failed" and job["error_code"] == "queue_timeout"
    assert job["finished_at"] is not None and job["expires_at"] is not None
    r = _result(jid, k["key"])
    assert r.status_code == 409 and r.json() == {"status": "failed", "error_code": "queue_timeout"}

    with store._connect() as c:
        c.execute("UPDATE codex_jobs SET expires_at=%s WHERE id=%s",
                  (datetime.now(timezone.utc) - timedelta(days=1), jid))
    store_jobs.expire_due_jobs()
    assert store_jobs.get_job(jid)["status"] == "expired"
    assert _status(jid, k["key"]).status_code == 200


def _running_webhook_job(key_id: int, ids: list[str]) -> str:
    jid = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=jid, key_id=key_id, world="v1", query="q", scope_paths=[],
                          depth="standard", webhook=True)
    ids.append(jid)
    with store._connect() as c:
        c.execute("UPDATE codex_jobs SET status='running' WHERE id=%s", (jid,))
    return jid


def _notices(jid: str) -> list[dict]:
    with store._connect() as c:
        return c.execute("SELECT status FROM delivery_outbox WHERE kind='webhook' "
                         "AND payload->>'job_id'=%s", (jid,)).fetchall()


def test_terminal_state_and_its_notice_commit_together(fake_codex, made_ids, monkeypatch):
    k = _new_keys("cjq-atomic")[0]
    jid = _running_webhook_job(k["id"], made_ids)
    from sherpa import webhooks

    def _boom(*a, **kw):
        raise RuntimeError("stopped after the terminal UPDATE")

    monkeypatch.setattr(webhooks, "codex_job_payload", _boom)
    with pytest.raises(RuntimeError):
        store_jobs.mark_failed(jid, error_code="codex_failed")
    assert store_jobs.get_job(jid)["status"] == "running" and _notices(jid) == []
    monkeypatch.undo()
    store_jobs.mark_failed(jid, error_code="codex_failed")
    assert store_jobs.get_job(jid)["status"] == "failed" and len(_notices(jid)) == 1


def test_cancel_just_after_the_wait_deadline_ends_as_queue_timeout(fake_codex, made_ids):
    k = _new_keys("cjq-late")[0]
    jid = _insert(k["id"], made_ids)[0]
    with store._connect() as c:
        c.execute("UPDATE codex_jobs SET queue_deadline=%s WHERE id=%s",
                  (datetime.now(timezone.utc) - timedelta(seconds=5), jid))
    r = client.post(f"/ext/v1/codex/jobs/{jid}/cancel", headers={"X-API-Key": k["key"]})
    assert r.status_code == 200 and r.json() == {"status": "failed"}
    assert store_jobs.get_job(jid)["error_code"] == "queue_timeout"


def test_stale_queued_read_is_refreshed_when_the_sweeper_won_first(fake_codex, made_ids):
    k = _new_keys("cjq-sweep")[0]
    jid = _insert(k["id"], made_ids)[0]
    stale = store_jobs.get_job(jid)                        # 照会が queued を読んだ
    with store._connect() as c:
        c.execute("UPDATE codex_jobs SET queue_deadline=%s WHERE id=%s",
                  (datetime.now(timezone.utc) - timedelta(seconds=5), jid))
    store_jobs.fail_overdue_queued()                       # 掃除が先に期限切れを確定した
    fresh = store_jobs.fail_if_overdue(stale)
    assert fresh["status"] == "failed" and fresh["error_code"] == "queue_timeout"
