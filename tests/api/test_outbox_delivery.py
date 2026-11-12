"""配送待ち（Webhook 通知・外部 API 監査）: 先に保存してから送り、送れたら完了にする。

要 Postgres。DB 不可は SKIP。外部境界は `webhooks._send_once`（HTTP 送信）と `store._audit_insert`（監査の INSERT）だけ差し替える。
送り手（`outbox_sender.run_due`）は本物。待ち時間は行の `next_attempt_at` を直接進めて飛ばす。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import pytest

from sherpa import outbox_sender, store, webhooks
from sherpa.ext import audit as ext_audit
from sherpa.store import outbox
from test_ext_codex_jobs import _new_keys, _register_destination


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
        outbox.pending_count()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda url, **kw: None)


@pytest.fixture
def audits(monkeypatch):
    got: list[tuple] = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: got.append((a, kw)))
    return got


@pytest.fixture
def key_id():
    k = _new_keys("outbox")[0]
    _register_destination(k["id"])
    return k["id"]


def _tag() -> str:
    return uuid.uuid4().hex


def _rows(tag: str) -> list[dict]:
    with store._connect() as c:
        return c.execute("SELECT id, kind, status, attempts, last_error FROM delivery_outbox "
                         "WHERE payload->>'tag' = %s ORDER BY id", (tag,)).fetchall()


def _make_due(tag: str) -> None:
    with store._connect() as c:
        c.execute("UPDATE delivery_outbox SET next_attempt_at=now() WHERE payload->>'tag'=%s "
                  "AND status='pending'", (tag,))


def _notice(tag: str, n: int = 0) -> dict:
    return {"event": "ingest.completed", "world": "outbox", "run_id": n, "tag": tag}


def test_pending_notices_outlive_the_worker_and_the_next_sender_delivers_them(
        monkeypatch, allowed, audits, key_id):
    """宛先が落ちている間に worker が終わっても、通知は配送待ちに残り、次の送り手が送る。"""
    tag = _tag()
    down = {"on": True}
    sent: list[bytes] = []

    def _send(url, secret, body, rid, event):
        if down["on"]:
            raise ConnectionError("down")
        sent.append(body)

    monkeypatch.setattr(webhooks, "_send_once", _send)
    for i in range(2):
        webhooks._enqueue(key_id, _notice(tag, i))

    left = webhooks.drain(timeout=0.3)             # worker の終了前の最後の一押し（宛先は落ちたまま）
    assert left >= 2 and sent == []
    assert [r["status"] for r in _rows(tag)] == ["pending", "pending"]

    down["on"] = False                             # API のプロセスの送り手が引き継ぐ
    _make_due(tag)
    outbox_sender.run_due({"webhook": webhooks.KIND})
    assert len(sent) == 2
    assert [r["status"] for r in _rows(tag)] == ["done", "done"]
    assert [a[0][1] for a in audits].count("webhook.delivered") == 2


def test_gives_up_after_the_retry_limit_and_keeps_the_reason(monkeypatch, allowed, audits, key_id):
    tag = _tag()
    calls: list[int] = []

    def _send(*a):
        calls.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr(webhooks, "_send_once", _send)
    webhooks._enqueue(key_id, _notice(tag))
    for _ in range(len(webhooks._RETRY_DELAYS_SEC) + 3):
        _make_due(tag)
        outbox_sender.run_due({"webhook": webhooks.KIND})

    assert len(calls) == len(webhooks._RETRY_DELAYS_SEC) + 1     # 最初の1回 + 再送の回数
    row = _rows(tag)[0]
    assert row["status"] == "gave_up" and row["last_error"] == "ConnectionError"
    failed = [a for a in audits if a[0][1] == "webhook.failed"]
    assert len(failed) == 1 and failed[0][1]["reason"] == "ConnectionError"


def test_two_senders_never_send_the_same_row(monkeypatch, allowed, audits, key_id):
    tag = _tag()
    sent: list[int] = []
    lock = threading.Lock()

    def _send(url, secret, body, rid, event):
        time.sleep(0.05)
        with lock:
            sent.append(json.loads(body)["run_id"])

    monkeypatch.setattr(webhooks, "_send_once", _send)
    for i in range(8):
        webhooks._enqueue(key_id, _notice(tag, i))

    barrier = threading.Barrier(2)

    def _sender():
        barrier.wait()
        outbox_sender.run_due({"webhook": webhooks.KIND})

    ts = [threading.Thread(target=_sender) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(i for i in sent if i in range(8)) == list(range(8))   # 8 件が1回ずつ（重複なし）


def test_a_leased_row_is_not_handed_out_again(key_id):
    tag = _tag()
    outbox.add("webhook", _notice(tag), key_id=key_id)
    mine = [r for r in outbox.claim_due(("webhook",), 1000) if r["payload"].get("tag") == tag]
    assert len(mine) == 1
    assert [r for r in outbox.claim_due(("webhook",), 1000) if r["payload"].get("tag") == tag] == []


def test_a_failed_audit_insert_is_parked_and_written_later(monkeypatch):
    request_id = _tag()
    real_insert = store._audit_insert
    failures = {"left": 1}

    def _flaky_insert(*a, **kw):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("insert failed")
        return real_insert(*a, **kw)

    monkeypatch.setattr(store, "_audit_insert", _flaky_insert)
    writer = ext_audit._AuditWriter(maxsize=10)
    monkeypatch.setattr(ext_audit, "_audit_writer", writer)
    pending = ext_audit._init_audit_pending("ext_api.search", "ext_search", "ext:outbox-test")
    try:
        asyncio.run(ext_audit._write_pending_audit_async(pending, 200, 1.0, "GET", "/x", request_id))
    finally:
        writer.stop(drain_timeout=2)

    def _written() -> int:
        with store._connect() as c:
            return c.execute("SELECT count(*) AS n FROM audit_log WHERE request_id=%s",
                             (request_id,)).fetchone()["n"]

    assert _written() == 0
    with store._connect() as c:
        c.execute("UPDATE delivery_outbox SET next_attempt_at=now() WHERE kind='ext_audit' "
                  "AND payload->>'request_id'=%s", (request_id,))
    outbox_sender.run_due({"ext_audit": ext_audit.AUDIT_OUTBOX_KIND})
    assert _written() == 1


def test_finished_rows_are_purged_after_the_retention_days_and_pending_rows_are_kept(key_id):
    tag = _tag()
    ids = [outbox.add("webhook", _notice(tag, i), key_id=key_id) for i in range(3)]
    claimed = {r["id"]: r for r in outbox.claim_due(("webhook",), 1000)}
    outbox.mark_done(claimed[ids[0]])
    outbox.mark_gave_up(claimed[ids[1]], reason="x")
    with store._connect() as c:
        c.execute("UPDATE delivery_outbox SET finished_at=now() - interval '8 days' WHERE id = ANY(%s)",
                  (ids[:2],))
    outbox.purge_finished()
    assert [r["id"] for r in _rows(tag)] == [ids[2]]


def test_outbox_refuses_beyond_the_pending_cap(monkeypatch):
    monkeypatch.setattr(outbox, "MAX_PENDING_PER_KIND", outbox.pending_count(("webhook",)) + 2)
    tag = _tag()
    outbox.add("webhook", _notice(tag, 0))
    outbox.add("webhook", _notice(tag, 1))
    with pytest.raises(outbox.OutboxFull):
        outbox.add("webhook", _notice(tag, 2))


def test_notice_survives_when_the_destination_is_removed_after_acceptance(
        monkeypatch, allowed, audits):
    from sherpa.store import codex_jobs as store_jobs
    k = _new_keys("outbox-nodest")[0]                   # 通知先は登録しない
    jid = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=jid, key_id=k["id"], world="v1", query="q", scope_paths=[],
                          depth="standard", webhook=True)
    try:
        with store._connect() as c:
            c.execute("UPDATE codex_jobs SET status='running' WHERE id=%s", (jid,))
        sent = []
        monkeypatch.setattr(webhooks, "_send_once", lambda *a: sent.append(a))
        store_jobs.mark_failed(jid, error_code="codex_failed")
        outbox_sender.run_due({"webhook": webhooks.KIND})
        with store._connect() as c:
            rows = c.execute("SELECT status, last_error FROM delivery_outbox "
                             "WHERE payload->>'job_id'=%s", (jid,)).fetchall()
        assert [(r["status"], r["last_error"]) for r in rows] == [("gave_up", "destination_removed")]
        assert [a for a in sent if jid.encode() in a[2]] == []
        failed = [a for a in audits if a[0][1] == "webhook.failed" and a[1]["detail"].get("job_id") == jid]
        assert len(failed) == 1 and failed[0][1]["reason"] == "destination_removed"
    finally:
        with store._connect() as c:
            c.execute("DELETE FROM codex_jobs WHERE id=%s", (jid,))


def _audit_count(request_id: str) -> int:
    with store._connect() as c:
        return c.execute("SELECT count(*) AS n FROM audit_log WHERE request_id=%s",
                         (request_id,)).fetchone()["n"]


def _park_audit(request_id: str, path: str) -> None:
    pending = ext_audit._init_audit_pending("ext_api.search", "ext_search", "ext:outbox-dup")
    outbox.add("ext_audit", {"pending": pending, "status_code": 200, "duration_ms": 1.0,
                             "method": "GET", "path": path, "request_id": request_id})


def test_audit_rows_of_different_requests_sharing_a_request_id_are_both_written():
    """X-Request-Id は呼び出し側が決められる＝一意ではない。同じ値の別の要求の監査を取り違えて消さない。"""
    request_id = _tag()
    _park_audit(request_id, "/a")
    _park_audit(request_id, "/b")
    outbox_sender.run_due({"ext_audit": ext_audit.AUDIT_OUTBOX_KIND})
    assert _audit_count(request_id) == 2


def test_audit_insert_and_done_mark_commit_together(monkeypatch):
    """INSERT の確定後、完了の記録の前に止まっても、二度目の再配送は起きない（行は同じトランザクションで done になっている）。"""
    request_id = _tag()
    _park_audit(request_id, "/a")

    def _stop(row_id):
        raise RuntimeError("stopped before mark_done")

    monkeypatch.setattr(outbox, "mark_done", _stop)
    with pytest.raises(RuntimeError):
        outbox_sender.run_due({"ext_audit": ext_audit.AUDIT_OUTBOX_KIND})
    with store._connect() as c:
        st = c.execute("SELECT status FROM delivery_outbox WHERE payload->>'request_id'=%s",
                       (request_id,)).fetchone()["status"]
    assert st == "done" and _audit_count(request_id) == 1


def test_run_due_returns_unsent_rows_when_the_deadline_passes():
    """期限が来たら、借りただけで送っていない行は貸出を解いて返る（次の送り手がすぐ取れ、試行にも数えない）。"""
    tag = _tag()
    for i in range(6):
        outbox.add("webhook", _notice(tag, i))

    def _slow(row):
        time.sleep(0.3)

    kind = outbox_sender.Kind(deliver=_slow, delays=(1,))
    handled = outbox_sender.run_due({"webhook": kind}, deadline=time.monotonic() + 0.5)
    rows = _rows(tag)
    assert handled < 6
    left = [r for r in rows if r["status"] == "pending"]
    assert len(left) == 6 - handled and all(r["attempts"] == 0 for r in left)
    claimed = [r for r in outbox.claim_due(("webhook",), 1000) if r["payload"].get("tag") == tag]
    assert len(claimed) == len(left)


def test_terminal_notice_does_not_need_a_second_pool_connection(monkeypatch):
    """プールが1本でも、終端化のトランザクションの中で別の接続を取りにいかない。"""
    from sherpa.store import codex_jobs as store_jobs
    from sherpa.store import db
    k = _new_keys("outbox-pool1")[0]
    jid = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=jid, key_id=k["id"], world="v1", query="q", scope_paths=[],
                          depth="standard", webhook=True)
    with store._connect() as c:
        c.execute("UPDATE codex_jobs SET status='running' WHERE id=%s", (jid,))
    monkeypatch.setenv("SHERPA_PG_POOL_MAX", "1")
    monkeypatch.setenv("SHERPA_PG_POOL_MIN", "0")
    monkeypatch.setenv("SHERPA_PG_POOL_TIMEOUT", "1")
    db.close_pg_pool()
    monkeypatch.setattr(outbox, "_ready_dsn", None)       # 表の作成が未了の状態から始める
    try:
        store_jobs.mark_failed(jid, error_code="codex_failed")
        assert store_jobs.get_job(jid)["status"] == "failed"
    finally:
        monkeypatch.undo()   # プールの設定を戻してから作り直す（1 本のプールを後の試験へ残さない）
        db.close_pg_pool()
        with store._connect() as c:
            c.execute("DELETE FROM codex_jobs WHERE id=%s", (jid,))


def test_outbox_full_terminal_notice_is_audited_after_commit(monkeypatch, audits):
    from sherpa.store import codex_jobs as store_jobs
    k = _new_keys("outbox-full")[0]
    _register_destination(k["id"])
    jid = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=jid, key_id=k["id"], world="v1", query="q", scope_paths=[],
                          depth="standard", webhook=True)
    with store._connect() as c:
        c.execute("UPDATE codex_jobs SET status='running' WHERE id=%s", (jid,))
    monkeypatch.setattr(outbox, "MAX_PENDING_PER_KIND", 0)
    try:
        store_jobs.mark_failed(jid, error_code="codex_failed")
        assert store_jobs.get_job(jid)["status"] == "failed"     # 終端化は確定する
        dropped = [a for a in audits if a[0][1] == "webhook.dropped" and a[1]["detail"].get("job_id") == jid]
        assert len(dropped) == 1 and dropped[0][1]["reason"] == "outbox_full"
    finally:
        with store._connect() as c:
            c.execute("DELETE FROM codex_jobs WHERE id=%s", (jid,))


def _claim_one(tag: str) -> dict:
    return next(r for r in outbox.claim_due(("webhook",), 1000) if r["payload"].get("tag") == tag)


def _expire_lease(tag: str) -> None:
    with store._connect() as c:
        c.execute("UPDATE delivery_outbox SET locked_until=now() WHERE payload->>'tag'=%s", (tag,))


def test_stale_release_does_not_free_a_newer_lease(key_id):
    tag = _tag()
    outbox.add("webhook", _notice(tag), key_id=key_id)
    old = _claim_one(tag)
    _expire_lease(tag)                                   # 貸出が切れ、別の送り手が借り直した
    new = _claim_one(tag)
    outbox.release([old])
    row = _rows(tag)[0]
    assert row["attempts"] == new["attempts"] == 2
    assert [r for r in outbox.claim_due(("webhook",), 1000) if r["payload"].get("tag") == tag] == []


def test_stale_sender_cannot_finish_retry_or_give_up_a_newer_lease(key_id):
    tag = _tag()
    outbox.add("webhook", _notice(tag), key_id=key_id)
    old = _claim_one(tag)
    _expire_lease(tag)
    _claim_one(tag)
    outbox.mark_done(old)
    outbox.mark_retry(old, delay_seconds=0, error="x")
    assert outbox.mark_gave_up(old, reason="x") is False
    row = _rows(tag)[0]
    assert row["status"] == "pending" and row["last_error"] is None
    assert [r for r in outbox.claim_due(("webhook",), 1000) if r["payload"].get("tag") == tag] == []


def test_drain_returns_at_once_when_nothing_is_due_now(monkeypatch, key_id):
    """再送待ち・他の送り手が貸出中の行があっても、今送れる行が無ければ期限まで待たない。"""
    tag = _tag()
    outbox.add("webhook", _notice(tag), key_id=key_id)
    _claim_one(tag)                                      # API 側が貸出中
    other = _tag()
    outbox.add("webhook", _notice(other), key_id=key_id)
    with store._connect() as c:
        c.execute("UPDATE delivery_outbox SET next_attempt_at=now() + interval '1 hour' "
                  "WHERE payload->>'tag'=%s", (other,))
    t0 = time.monotonic()
    webhooks.drain(timeout=5)
    assert time.monotonic() - t0 < 2
