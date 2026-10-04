"""Codex ジョブ（外部 API の非同期 Codex 実行・`C-EXT-CODEXJOB-*`）の契約テスト。

要 Postgres。DB 不可は SKIP。Codex CLI は外部境界のため偽の実行ファイルを PATH に差し込む
（内部関数の monkeypatch では成立させない）。背景ディスパッチャは TestClient 経由では起動しないため、
各テストは `codex_jobs_worker.dispatch_cycle()` を直接呼んでジョブを進め、完了まで短い間隔で
ポーリングする。`codex_mode=plain` に固定し、最小の偽 codex（`agent_message` 1件＋`turn.completed`）で
決定的に完走させる。

同時実行の合算枠・取消と完了の競合（`mark_cancelled`/`mark_completed` の `WHERE status='running'`
原子的 UPDATE）・保存期間の失効は DB/チャット側の不変条件のため、本物の API を軽い入力で呼んで確かめる。
"""
from __future__ import annotations

import os
import stat
import threading
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, ext_api, store
from sherpa.api import app
from sherpa.store import codex_jobs as store_jobs

client = TestClient(app, raise_server_exceptions=True)


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_admin(sfx: str) -> tuple[str, str]:
    uid = f"cjadm{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role="admin", status="active")
    register_test_uid(uid)
    return uid, pw


def _login(uid: str, pw: str) -> None:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"


def _logout() -> None:
    client.post("/auth/logout")


def _issue_key(label: str, allowed_worlds=None) -> dict:
    payload = {"label": label}
    if allowed_worlds is not None:
        payload["allowed_worlds"] = allowed_worlds
    r = client.post("/ext/v1/admin/keys", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _submit(payload: dict, api_key: str | None = None):
    headers = {"X-API-Key": api_key} if api_key else {}
    return client.post("/ext/v1/codex/jobs", json=payload, headers=headers)


def _status(job_id: str, api_key: str):
    return client.get(f"/ext/v1/codex/jobs/{job_id}", headers={"X-API-Key": api_key})


def _result(job_id: str, api_key: str):
    return client.get(f"/ext/v1/codex/jobs/{job_id}/result", headers={"X-API-Key": api_key})


def _cancel(job_id: str, api_key: str):
    return client.post(f"/ext/v1/codex/jobs/{job_id}/cancel", headers={"X-API-Key": api_key})


# ---- 偽 codex（外部境界のみを偽装・内部関数は monkeypatch しない）----

_FAKE_CODEX_PY = r'''#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in")
    sys.exit(0)
if args and args[-1] == "-":   # プロンプトは argv でなく標準入力から渡される
    args = args[:-1] + [sys.stdin.read()]
print(json.dumps({"type": "thread.started", "thread_id": "SID-CODEXJOB"}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "m0", "type": "agent_message",
                           "text": "障害の記録を確認しました。\n\n参照した資料:\n- 4期/04_運用/障害記録.md"}}))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}}))
'''


def _write_fake_codex(bin_dir) -> None:
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_PY)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """偽 codex を PATH に差し込み、`agent=codex`/`codex_mode=plain` に固定する。共有 dev DB の
    `admin` アカウントの設定（`check_available()`/`_execute_job` が読む唯一の Codex 接続設定）は
    他の検証で変わっていることがあるため、テストの間だけ固定し終了時に元へ戻す。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_codex(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    prev_settings = store.get_settings("admin")
    store.update_settings("admin", agent="codex", codex_model_provider="openai")
    store.set_system_settings("admin-uid", {"codex_mode": "plain"})
    try:
        yield
    finally:
        store.set_system_settings("admin-uid", {"codex_mode": None})
        store.update_settings("admin", agent=prev_settings.get("agent") or "",
                              codex_model_provider=prev_settings.get("codex_model_provider") or "")


def _dispatch_and_wait(job_id: str, timeout: float = 20.0) -> dict:
    from sherpa import codex_jobs_worker
    codex_jobs_worker.dispatch_cycle()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store_jobs.get_job(job_id)
        if job is not None and job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not reach a terminal state in time")




def _new_keys(*labels: str, allowed_worlds=None) -> list[dict]:
    """管理者でログインして鍵を発行し、ログアウトする。"""
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    try:
        return [_issue_key(f"{label}-{sfx}", allowed_worlds=allowed_worlds) for label in labels]
    finally:
        _logout()


def _register_destination(key_id: int, secret: str = "whsec-test") -> None:
    """鍵に通知先を直接登録する（登録 API の検証は別契約・ここでは受付と送信だけを見る）。"""
    with store._connect() as c:
        c.execute("UPDATE api_keys SET webhook_url=%s, webhook_secret=%s WHERE id=%s",
                  ("http://hooks.example.test/codex", secret, key_id))


def _keyed(label: str, with_destination: bool) -> dict:
    issued = _new_keys(label)[0]
    if with_destination:
        _register_destination(issued["id"])
    return issued


def _submit_job(key: str, query: str, **extra) -> str:
    r = _submit({"world": "v1", "query": query, **extra}, api_key=key)
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def _expire(job_id: str) -> None:
    from datetime import datetime, timedelta, timezone
    from sherpa.store.db import _connect
    with _connect() as c:
        c.execute("UPDATE codex_jobs SET expires_at=%s WHERE id=%s",
                  (datetime.now(timezone.utc) - timedelta(days=1), job_id))


def _complete_directly(key_id: int, query: str, **mark_completed_kw) -> str:
    """ワーカーが呼ぶのと同じ `insert_job`/`claim_queued_jobs`/`mark_completed` を直接呼んでジョブを完了させる
    （Codex 実行そのものは関与しない DB 層の契約用）。"""
    job_id = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=job_id, key_id=key_id, world="v1", query=query, scope_paths=[],
                          depth="standard")
    claimed = store_jobs.claim_queued_jobs(1)
    assert [j["id"] for j in claimed] == [job_id]   # 共有 DB に他の queued ジョブが無いことの保険
    store_jobs.mark_completed(job_id, answer=f"{query}の回答", sources=[], unconfirmed_items=[],
                              elapsed_ms=5, **mark_completed_kw)
    return job_id


@pytest.fixture
def sent_notices(monkeypatch):
    """HTTP 送信（外部境界）だけを差し替え、キュー投入は同期で `_deliver` へ流す。"""
    from sherpa import webhooks
    sent: list[dict] = []
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda url, **kw: None)
    monkeypatch.setattr(webhooks, "_send_once",
                        lambda url, secret, body, rid, event: sent.append(
                            {"url": url, "secret": secret, "body": body, "rid": rid, "event": event}))
    monkeypatch.setattr(webhooks, "_enqueue",
                        lambda key_id, url, secret, payload: webhooks._deliver(key_id, url, secret, payload))
    return sent


# ===== 受付 =====

@pytest.mark.parametrize("case,payload,status", [
    ("webhook_without_destination", {"world": "v1", "query": "x", "webhook": True}, 422),
    ("world_out_of_key_scope", {"world": "other-world-xyz", "query": "x"}, 403),
    # Codex が今実行できないなら受付時点で 503（簡易チャットへ黙って切り替えない）
    ("codex_unavailable", {"world": "v1", "query": "x"}, 503),
])
def test_codex_job_submission_rejections(monkeypatch, case, payload, status):
    if case == "codex_unavailable":
        def _boom():
            raise ext_api.codex_jobs_worker.CodexUnavailable("codex CLI が見つかりません")
        monkeypatch.setattr(ext_api.codex_jobs_worker, "check_available", _boom)
    issued = _new_keys(f"cj-{case}", allowed_worlds=["v1"] if case == "world_out_of_key_scope" else None)[0]
    r = _submit(payload, api_key=issued["key"])
    assert r.status_code == status, r.text


def test_codex_job_webhook_true_accepted_with_destination_and_notified_on_completion(
        fake_codex, sent_notices):
    import hashlib
    import hmac
    import json
    from sherpa import webhooks
    issued = _keyed("cj-wh-ok", with_destination=True)
    job_id = _submit_job(issued["key"], "通知テスト", webhook=True)
    assert store_jobs.get_job(job_id)["status"] == "queued"
    assert sent_notices == []                      # 終了前には送らない

    job = _dispatch_and_wait(job_id)
    assert job["status"] == "completed"
    deadline = time.monotonic() + 5                # 通知は状態の確定（commit）の後に送る＝少し遅れて届く
    while not sent_notices and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(sent_notices) == 1                  # 遷移1回につき通知1回
    n = sent_notices[0]
    body = json.loads(n["body"])
    assert set(body) == {"event", "job_id", "status", "finished_at"}   # 回答本文は載せない
    assert body["event"] == "codex_job.completed" and body["status"] == "completed"
    assert body["job_id"] == job_id and body["finished_at"]
    assert n["event"] == "codex_job.completed"
    expected = "sha256=" + hmac.new(b"whsec-test", n["body"], hashlib.sha256).hexdigest()
    assert webhooks._sign("whsec-test", n["body"]) == expected


def test_codex_job_webhook_cancel_queued_and_failed_notify_once_each(fake_codex, sent_notices):
    import json
    issued = _keyed("cj-wh-term", with_destination=True)
    j1 = _submit_job(issued["key"], "取消", webhook=True)
    assert _cancel(j1, issued["key"]).json()["status"] == "cancelled"
    _cancel(j1, issued["key"])                     # 冪等な再取消では再送しない
    j2 = _submit_job(issued["key"], "失敗", webhook=True)
    store_jobs.mark_failed(j2, error_code="codex_failed", from_status="queued")
    events = [(json.loads(n["body"])["event"], json.loads(n["body"])["job_id"]) for n in sent_notices]
    assert events == [("codex_job.cancelled", j1), ("codex_job.failed", j2)]


def test_codex_job_webhook_not_sent_when_not_requested_or_destination_removed(fake_codex, sent_notices):
    issued = _keyed("cj-wh-none", with_destination=True)
    j1 = _submit_job(issued["key"], "希望なし")
    _cancel(j1, issued["key"])
    j2 = _submit_job(issued["key"], "宛先消去", webhook=True)
    with store._connect() as c:
        c.execute("UPDATE api_keys SET webhook_url=NULL, webhook_secret=NULL WHERE id=%s", (issued["id"],))
    _cancel(j2, issued["key"])
    assert sent_notices == []


# ===== 受付→状態→結果の一巡 =====

def test_codex_job_round_trip_completed(fake_codex):
    issued = _new_keys("cj-ok")[0]
    r = _submit({"world": "v1", "query": "税率改定の障害は？"}, api_key=issued["key"])
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    job_id = r.json()["job_id"]

    sr = _status(job_id, issued["key"])
    assert sr.status_code == 200, sr.text
    assert sr.json()["status"] == "queued"
    assert sr.headers.get("Retry-After") == "30"
    assert "started_at" not in sr.json()   # まだ起きていない時刻は項目ごと省く

    job = _dispatch_and_wait(job_id)
    assert job["status"] == "completed", job

    sr2 = _status(job_id, issued["key"])
    assert sr2.status_code == 200, sr2.text
    assert sr2.json()["status"] == "completed"
    assert sr2.json()["started_at"] is not None and sr2.json()["finished_at"] is not None
    assert "Retry-After" not in sr2.headers   # 終了状態には付けない

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 200, rr.text
    rbody = rr.json()
    assert rbody["answer"].startswith("障害の記録を確認しました。")
    assert [s["doc_id"] for s in rbody["sources"]] == ["4期/04_運用/障害記録.md"]
    assert isinstance(rbody["unconfirmed_items"], list)
    assert isinstance(rbody["elapsed_ms"], int) and rbody["elapsed_ms"] >= 0


def test_codex_job_result_investigation_field_present_absent_and_cleared_on_expiry():
    """`investigation` は記録がある完了ジョブにだけ付き（`response_model_exclude_none`）、保存期間（7日）
    経過で他の結果列と一緒に消える。"""
    issued, issued_no_ledger = _new_keys("cj-inv", "cj-inv-none")
    investigation = {"complete": True, "truncated": False,
                     "manifest": {"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z",
                                  "items": ["i1"]},
                     "items": {"i1": {"id": "i1", "kind": "qa", "subject": "対象A",
                                      "required_checks": ["source"],
                                      "evidence": [{"kind": "source", "path": "a/b.md", "line": 1}],
                                      "status": "source_confirmed", "reason": "", "owner": "main"}},
                     "coverage": {"i1": ["hit"]}}
    job_id = _complete_directly(issued["id"], "記録あり", investigation=investigation)
    job_id_none = _complete_directly(issued_no_ledger["id"], "記録なし")   # investigation 省略＝None

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 200, rr.text
    inv = rr.json()["investigation"]
    assert inv["complete"] is True and inv["truncated"] is False
    assert inv["items"]["i1"]["status"] == "source_confirmed"
    assert inv["manifest"]["question_kind"] == "qa"

    rr_none = _result(job_id_none, issued_no_ledger["key"])
    assert rr_none.status_code == 200, rr_none.text
    assert "investigation" not in rr_none.json()   # 記録が無ければ項目ごと省く

    _expire(job_id)
    rr_expired = _result(job_id, issued["key"])
    assert rr_expired.status_code == 410, rr_expired.text
    assert store_jobs.get_job(job_id)["investigation"] is None   # 他の結果列と一緒に消える


def test_codex_job_result_before_completion_409_and_other_key_404(fake_codex):
    issued, other = _new_keys("cj-409", "cj-409-other")
    job_id = _submit_job(issued["key"], "まだ終わっていない質問")

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 409, rr.text
    assert rr.json()["status"] == "queued"
    assert "error_code" not in rr.json()

    # 別の鍵のジョブは状態照会・結果取得・取消のいずれも 404（存在を漏らさない）。
    assert _status(job_id, other["key"]).status_code == 404
    assert _result(job_id, other["key"]).status_code == 404
    assert _cancel(job_id, other["key"]).status_code == 404

    _dispatch_and_wait(job_id)   # 後片付け（実行させて終わらせる）


def test_codex_job_cancel_queued_is_idempotent(fake_codex):
    issued = _new_keys("cj-cancel")[0]
    job_id = _submit_job(issued["key"], "取消テスト")

    for _ in range(2):   # 冪等: 既に終端状態でも 200・同じ結果
        c = _cancel(job_id, issued["key"])
        assert c.status_code == 200, c.text
        assert c.json()["status"] == "cancelled"

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 409, rr.text
    assert rr.json()["status"] == "cancelled"
    assert "error_code" not in rr.json()   # error_code は failed のときだけ


def test_codex_job_expired_after_retention_returns_410(fake_codex):
    issued = _new_keys("cj-exp")[0]
    job_id = _submit_job(issued["key"], "保存期間テスト")
    _dispatch_and_wait(job_id)
    _expire(job_id)

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 410, rr.text
    assert rr.json() == {"status": "expired"}

    sr = _status(job_id, issued["key"])
    assert sr.status_code == 200, sr.text
    assert sr.json()["status"] == "expired"

    row = store_jobs.get_job(job_id)
    assert row["answer"] is None and row["query"] is None and row["sources"] is None


def test_codex_job_recover_interrupted_on_startup(fake_codex):
    """再起動時: `running` のまま残ったジョブは `failed(interrupted)`、`queued` は残す。"""
    issued = _new_keys("cj-recover")[0]
    running_id = _submit_job(issued["key"], "running のまま残る")
    queued_id = _submit_job(issued["key"], "queued のまま残る")

    claimed = store_jobs.claim_queued_jobs(1)
    assert [j["id"] for j in claimed] == [running_id]   # 古い順＝先に受け付けた方を claim

    assert running_id in store_jobs.recover_interrupted_on_startup()

    running_row = store_jobs.get_job(running_id)
    assert running_row["status"] == "failed"
    assert running_row["error_code"] == "interrupted"
    assert store_jobs.get_job(queued_id)["status"] == "queued"   # 続きから実行できるよう残す

    store_jobs.cancel_if_queued(queued_id)   # 後片付け


# ===== チャットとジョブの同時実行を合計で数える =====

def test_codex_job_not_dispatched_when_chat_turns_at_global_max(fake_codex):
    """チャットの未完了ターンが同時実行の上限（global）まで埋まっているとき、Codex ジョブは
    `dispatch_cycle()` を呼んでも claim されず `queued` のまま残る（`try_reserve_external` が
    チャットの実行中ターン数＋外部予約数の合計で判定する・ジョブ専用の別枠を持たない）。
    チャットの実行中ターンは本物の `chat_turns.start_turn` に、合図があるまで待つだけの軽い
    `run_fn` を渡して作る。"""
    from sherpa import chat_turns as CT
    from sherpa import codex_jobs_worker

    _per_user, max_global = CT.effective_limits()
    with CT._REGISTRY_LOCK:
        current = sum(1 for r in CT._REGISTRY.values() if not r.buffer.done)
    need = max(0, max_global - current)
    assert need > 0, f"このプロセスの chat_turns レジストリが既に満杯です（max_global={max_global}）"

    release = threading.Event()
    started = [threading.Event() for _ in range(need)]

    def _make_blocking_run(ev):
        def _run(stop_event, emit):
            ev.set()
            release.wait(timeout=30)
        return _run

    recs = []
    sfx = _sfx()
    try:
        for i, ev in enumerate(started):
            rec = CT.start_turn(uid=f"cj-cap-filler-{i}-{sfx}", conversation_factory=lambda i=i: -(9_000_000 + i),
                                run_fn_factory=lambda cid, ev=ev: _make_blocking_run(ev))
            recs.append(rec)
        for ev in started:
            assert ev.wait(timeout=5), "埋め草ターンが実行中状態になりませんでした"

        issued = _new_keys("cj-cap")[0]
        job_id = _submit_job(issued["key"], "容量上限テスト")

        codex_jobs_worker.dispatch_cycle()
        assert store_jobs.get_job(job_id)["status"] == "queued", (
            "チャットが global 上限を埋めているのにジョブが running へ進んだ（合算枠が効いていない）")

        store_jobs.cancel_if_queued(job_id)   # 後片付け
    finally:
        release.set()
        for rec in recs:
            rec.stop_event.set()
        with CT._REGISTRY_LOCK:
            for rec in recs:
                CT._REGISTRY.pop(rec.turn_id, None)


# ===== 取消と完了の競合 =====

def test_codex_job_cancel_wins_race_against_late_completion(fake_codex):
    """`running` 中に取消が先に DB を確定させれば、後から届く完了処理（`mark_completed`）は
    `WHERE status='running'` に一致せず no-op になり、ジョブは `cancelled` のまま・結果（回答・出典・
    未確認項目）は保存されない。取消は実際の HTTP 経路、遅れた完了処理はワーカーが呼ぶのと同じ
    `store_jobs.mark_completed` を直接呼んで模する（DB 層の原子性の検証）。"""
    issued = _new_keys("cj-race")[0]
    job_id = _submit_job(issued["key"], "競合テスト")
    claimed = store_jobs.claim_queued_jobs(1)
    assert [j["id"] for j in claimed] == [job_id]   # running へ（ワーカーのスレッドは起こさない）

    c = _cancel(job_id, issued["key"])
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "cancelled"   # DB 側の atomic UPDATE がここで先に勝つ

    store_jobs.mark_completed(job_id, answer="後から届いた回答", sources=[{"doc_id": "x.md"}],
                              unconfirmed_items=[], elapsed_ms=999)

    row = store_jobs.get_job(job_id)
    assert row["status"] == "cancelled", "取消の後に完了処理が上書きしてしまった"
    assert row["answer"] is None and row["sources"] is None, "cancelled に結果が保存されてしまった"

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 409, rr.text
    assert rr.json()["status"] == "cancelled"
    assert "error_code" not in rr.json()

    c2 = _cancel(job_id, issued["key"])   # 取消要求の再試行（冪等）も状態を変えない
    assert c2.status_code == 200, c2.text
    assert c2.json()["status"] == "cancelled"
