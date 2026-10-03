"""Codex ジョブ（外部 API の非同期 Codex 実行・`C-EXT-CODEXJOB-*`）の契約テスト。

要 Postgres。DB 不可は SKIP（tests/api の既存流儀）。Codex CLI は外部境界のため、偽の実行ファイル
（`tests/unit/test_codex_plain_mode.py`/`test_codex_no_presearch.py` と同じ「PATH に偽 codex を
差し込む」流儀）を使う——内部関数（`CodexProvider.run`・`get_provider` 等）の monkeypatch では
成立させない。背景ディスパッチャ（`sherpa/codex_jobs_worker.py`）は TestClient 経由では起動しない
（`TestClient(app)` を `with` 無しで使う既存流儀＝lifespan を実行しない）ため、各テストは
`codex_jobs_worker.dispatch_cycle()` を直接呼んでジョブを進め、完了まで短い間隔でポーリングする。

`codex_mode=plain`（system_settings）に固定して台帳・出力スキーマ・下調べ役の複雑さを避け、
最小の偽 codex（`item.completed` の `agent_message` 1件＋`turn.completed`）で決定的に完走させる
（`test_codex_plain_mode.py` と同じ単純化の判断）。

**同時実行の合算枠**（検収指摘・2026-10-01）: `test_codex_job_not_dispatched_when_chat_turns_at_global_max`
はチャットの実行中ターンを `sherpa.chat_turns.start_turn`（本物の予約方式・`run_fn` だけ
block する軽量版に差し替える——`tests/unit/test_chat_turns_limits_env.py` の
`_noop_run`/`_blocking_get_system_settings` と同じ「内部関数は monkeypatch せず、本物の API を
軽い run_fn で呼ぶ」流儀）で global 上限まで満たしてから Codex ジョブを受付・`dispatch_cycle()` を
呼び、claim されず `queued` のまま残ることを確かめる。
`test_codex_job_cancel_wins_race_against_late_completion` は取消と完了処理の競合
（`sherpa/store/codex_jobs.py::mark_cancelled`/`mark_completed` の `WHERE status='running'`
原子的 UPDATE）を、実際の HTTP 取消→実際の（遅れて届いたことを模した）`mark_completed` 呼び出しの
順で直接検証する——Codex 実行そのものは関与しない DB 層の不変条件のため、偽 codex は使わない。
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


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
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
    """偽 codex を PATH に差し込み、`codex_mode=plain` に固定する（テスト後に復元）。

    `codex_jobs_worker.check_available()`/`_execute_job` は管理者の唯一の Codex 接続設定
    （`store.get_settings("admin")`・ブートストラップ admin アカウント＝
    `sherpa/deps.py::_ensure_initial_admin` が作る唯一の初期管理者。チャット画面の頭脳選択と
    同じ設定を共有する契約）を読む。共有 dev DB の `admin` アカウントは他の手動検証/テストで
    `agent` が Codex 以外（例: `openai`）へ変わっていることがあるため、本フィクスチャがテストの
    間だけ `agent=codex`/`codex_model_provider=openai` へ固定し、終了時に元の値へ戻す
    （他テスト・開発者の環境を汚さない）。
    """
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


# ===== 受付 =====

def test_codex_job_webhook_true_rejected_422_without_destination():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-webhook-{sfx}")
    _logout()

    r = _submit({"world": "v1", "query": "x", "webhook": True}, api_key=issued["key"])
    assert r.status_code == 422, r.text


def _register_destination(key_id: int, secret: str = "whsec-test") -> None:
    """鍵に通知先を直接登録する（登録 API の検証は別契約・ここでは受付と送信だけを見る）。"""
    with store._connect() as c:
        c.execute("UPDATE api_keys SET webhook_url=%s, webhook_secret=%s WHERE id=%s",
                  ("http://hooks.example.test/codex", secret, key_id))


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


def _keyed(label: str, with_destination: bool) -> dict:
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"{label}-{sfx}")
    _logout()
    if with_destination:
        _register_destination(issued["id"])
    return issued


def test_codex_job_webhook_true_accepted_with_destination_and_notified_on_completion(
        fake_codex, sent_notices):
    import hashlib
    import hmac
    import json
    if not _try_init():
        pytest.skip("DB down")
    issued = _keyed("cj-wh-ok", with_destination=True)
    r = _submit({"world": "v1", "query": "通知テスト", "webhook": True}, api_key=issued["key"])
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert store_jobs.get_job(job_id)["status"] == "queued"
    assert sent_notices == []                      # 終了前には送らない

    job = _dispatch_and_wait(job_id)
    assert job["status"] == "completed"
    assert len(sent_notices) == 1                  # 遷移1回につき通知1回
    n = sent_notices[0]
    body = json.loads(n["body"])
    assert set(body) == {"event", "job_id", "status", "finished_at"}   # 回答本文は載せない
    assert body["event"] == "codex_job.completed" and body["status"] == "completed"
    assert body["job_id"] == job_id and body["finished_at"]
    assert n["event"] == "codex_job.completed"
    expected = "sha256=" + hmac.new(b"whsec-test", n["body"], hashlib.sha256).hexdigest()
    from sherpa import webhooks
    assert webhooks._sign("whsec-test", n["body"]) == expected


def test_codex_job_webhook_cancel_queued_and_failed_notify_once_each(fake_codex, sent_notices):
    import json
    if not _try_init():
        pytest.skip("DB down")
    issued = _keyed("cj-wh-term", with_destination=True)
    j1 = _submit({"world": "v1", "query": "取消", "webhook": True}, api_key=issued["key"]).json()["job_id"]
    assert _cancel(j1, issued["key"]).json()["status"] == "cancelled"
    _cancel(j1, issued["key"])                     # 冪等な再取消では再送しない
    j2 = _submit({"world": "v1", "query": "失敗", "webhook": True}, api_key=issued["key"]).json()["job_id"]
    store_jobs.mark_failed(j2, error_code="codex_failed", from_status="queued")
    events = [(json.loads(n["body"])["event"], json.loads(n["body"])["job_id"]) for n in sent_notices]
    assert events == [("codex_job.cancelled", j1), ("codex_job.failed", j2)]


def test_codex_job_webhook_not_sent_when_not_requested_or_destination_removed(
        fake_codex, sent_notices):
    if not _try_init():
        pytest.skip("DB down")
    issued = _keyed("cj-wh-none", with_destination=True)
    j1 = _submit({"world": "v1", "query": "希望なし"}, api_key=issued["key"]).json()["job_id"]
    _cancel(j1, issued["key"])
    j2 = _submit({"world": "v1", "query": "宛先消去", "webhook": True}, api_key=issued["key"]).json()["job_id"]
    with store._connect() as c:
        c.execute("UPDATE api_keys SET webhook_url=NULL, webhook_secret=NULL WHERE id=%s", (issued["id"],))
    _cancel(j2, issued["key"])
    assert sent_notices == []


def test_codex_job_world_scope_403():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-scope-{sfx}", allowed_worlds=["v1"])
    _logout()

    r = _submit({"world": "other-world-xyz", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 403, r.text


def test_codex_job_unavailable_returns_503(monkeypatch):
    """Codex が今実行できない（`codex_jobs_worker.check_available` が例外）なら受付時点で503——
    簡易チャットへ黙って切り替えない（契約）。"""
    if not _try_init():
        pytest.skip("DB down")

    def _boom():
        raise ext_api.codex_jobs_worker.CodexUnavailable("codex CLI が見つかりません")

    monkeypatch.setattr(ext_api.codex_jobs_worker, "check_available", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-unavail-{sfx}")
    _logout()

    r = _submit({"world": "v1", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 503, r.text


# ===== 受付→状態→結果の一巡 =====

def test_codex_job_round_trip_completed(fake_codex):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-ok-{sfx}")
    _logout()

    r = _submit({"world": "v1", "query": "税率改定の障害は？"}, api_key=issued["key"])
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    sr = _status(job_id, issued["key"])
    assert sr.status_code == 200, sr.text
    sbody = sr.json()
    assert sbody["status"] == "queued"
    assert sr.headers.get("Retry-After") == "30"
    assert "started_at" not in sbody   # まだ起きていない時刻は項目ごと省く

    job = _dispatch_and_wait(job_id)
    assert job["status"] == "completed", job

    sr2 = _status(job_id, issued["key"])
    assert sr2.status_code == 200, sr2.text
    sbody2 = sr2.json()
    assert sbody2["status"] == "completed"
    assert sbody2["started_at"] is not None and sbody2["finished_at"] is not None
    assert "Retry-After" not in sr2.headers   # 終了状態には付けない

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 200, rr.text
    rbody = rr.json()
    assert rbody["answer"].startswith("障害の記録を確認しました。")
    assert [s["doc_id"] for s in rbody["sources"]] == ["4期/04_運用/障害記録.md"]
    assert isinstance(rbody["unconfirmed_items"], list)
    assert isinstance(rbody["elapsed_ms"], int) and rbody["elapsed_ms"] >= 0


def test_codex_job_result_investigation_field_present_absent_and_cleared_on_expiry():
    """COD-18 §4: `investigation` は記録がある完了ジョブにだけ付き（`response_model_exclude_none`）、
    保存期間（7日）経過で他の結果列と一緒に消える。Codex 実行そのものは関与しない DB 層の契約
    のため、偽 codex は使わない（`test_codex_job_cancel_wins_race_against_late_completion` と同じ
    判断・モジュール docstring 参照）——`store_jobs.insert_job`/`claim_queued_jobs`/`mark_completed`
    （ワーカーが呼ぶのと同じ関数）を直接呼んでジョブを完了させる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-inv-{sfx}")
    issued_no_ledger = _issue_key(f"cj-inv-none-{sfx}")
    _logout()

    job_id = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=job_id, key_id=issued["id"], world="v1",
                          query="記録ありテスト", scope_paths=[], depth="standard")
    claimed = store_jobs.claim_queued_jobs(1)
    assert [j["id"] for j in claimed] == [job_id]   # 共有 DB に他の queued ジョブが無いことの保険
    investigation = {"complete": True, "truncated": False,
                     "manifest": {"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z",
                                  "items": ["i1"]},
                     "items": {"i1": {"id": "i1", "kind": "qa", "subject": "対象A",
                                      "required_checks": ["source"],
                                      "evidence": [{"kind": "source", "path": "a/b.md", "line": 1}],
                                      "status": "source_confirmed", "reason": "", "owner": "main"}},
                     "coverage": {"i1": ["hit"]}}
    store_jobs.mark_completed(job_id, answer="記録ありの回答", sources=[], unconfirmed_items=[],
                              elapsed_ms=5, investigation=investigation)

    job_id_none = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=job_id_none, key_id=issued_no_ledger["id"], world="v1",
                          query="記録なしテスト", scope_paths=[], depth="standard")
    claimed_none = store_jobs.claim_queued_jobs(1)
    assert [j["id"] for j in claimed_none] == [job_id_none]
    store_jobs.mark_completed(job_id_none, answer="記録なしの回答", sources=[],
                              unconfirmed_items=[], elapsed_ms=5)   # investigation 省略＝None

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 200, rr.text
    inv = rr.json()["investigation"]
    assert inv["complete"] is True and inv["truncated"] is False
    assert inv["items"]["i1"]["status"] == "source_confirmed"
    assert inv["manifest"]["question_kind"] == "qa"

    rr_none = _result(job_id_none, issued_no_ledger["key"])
    assert rr_none.status_code == 200, rr_none.text
    assert "investigation" not in rr_none.json()   # 記録が無ければ項目ごと省く

    from datetime import datetime, timedelta, timezone
    from sherpa.store.db import _connect
    with _connect() as c:
        c.execute("UPDATE codex_jobs SET expires_at=%s WHERE id=%s",
                 (datetime.now(timezone.utc) - timedelta(days=1), job_id))
    rr_expired = _result(job_id, issued["key"])
    assert rr_expired.status_code == 410, rr_expired.text
    row = store_jobs.get_job(job_id)
    assert row["investigation"] is None   # 他の結果列と一緒に消える


def test_codex_job_result_before_completion_409_and_other_key_404(fake_codex):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-409-{sfx}")
    other = _issue_key(f"cj-409-other-{sfx}")
    _logout()

    r = _submit({"world": "v1", "query": "まだ終わっていない質問"}, api_key=issued["key"])
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 409, rr.text
    assert rr.json()["status"] == "queued"
    assert "error_code" not in rr.json()

    # 別の鍵のジョブは状態照会・結果取得のどちらも 404（存在を漏らさない）。
    assert _status(job_id, other["key"]).status_code == 404
    assert _result(job_id, other["key"]).status_code == 404
    assert _cancel(job_id, other["key"]).status_code == 404

    _dispatch_and_wait(job_id)   # 後片付け（実行させて終わらせる）


def test_codex_job_cancel_queued_is_idempotent(fake_codex):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-cancel-{sfx}")
    _logout()

    r = _submit({"world": "v1", "query": "取消テスト"}, api_key=issued["key"])
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    c1 = _cancel(job_id, issued["key"])
    assert c1.status_code == 200, c1.text
    assert c1.json()["status"] == "cancelled"

    c2 = _cancel(job_id, issued["key"])   # 冪等: 既に終端状態でも 200・同じ結果
    assert c2.status_code == 200, c2.text
    assert c2.json()["status"] == "cancelled"

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 409, rr.text
    assert rr.json()["status"] == "cancelled"
    assert "error_code" not in rr.json()   # error_code は failed のときだけ


def test_codex_job_expired_after_retention_returns_410(fake_codex):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-exp-{sfx}")
    _logout()

    r = _submit({"world": "v1", "query": "保存期間テスト"}, api_key=issued["key"])
    job_id = r.json()["job_id"]
    _dispatch_and_wait(job_id)

    from datetime import datetime, timedelta, timezone
    from sherpa.store.db import _connect
    with _connect() as c:
        c.execute("UPDATE codex_jobs SET expires_at=%s WHERE id=%s",
                 (datetime.now(timezone.utc) - timedelta(days=1), job_id))

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
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-recover-{sfx}")
    _logout()

    r1 = _submit({"world": "v1", "query": "running のまま残る"}, api_key=issued["key"])
    running_id = r1.json()["job_id"]
    r2 = _submit({"world": "v1", "query": "queued のまま残る"}, api_key=issued["key"])
    queued_id = r2.json()["job_id"]

    claimed = store_jobs.claim_queued_jobs(1)
    assert [j["id"] for j in claimed] == [running_id]   # 古い順＝先に受け付けた方を claim

    recovered = store_jobs.recover_interrupted_on_startup()
    assert running_id in recovered

    running_row = store_jobs.get_job(running_id)
    assert running_row["status"] == "failed"
    assert running_row["error_code"] == "interrupted"

    queued_row = store_jobs.get_job(queued_id)
    assert queued_row["status"] == "queued"   # 続きから実行できるよう残す

    store_jobs.cancel_if_queued(queued_id)   # 後片付け


# ===== 検収是正①: チャットとジョブの同時実行を合計で数える =====

def test_codex_job_not_dispatched_when_chat_turns_at_global_max(fake_codex):
    """チャットの未完了ターンが同時実行の上限（global）まで埋まっているとき、Codex ジョブは
    `dispatch_cycle()` を呼んでも claim されず `queued` のまま残る——`chat_turns.try_reserve_external`
    がチャットの実行中ターン数＋外部予約数の合計で判定するため（ジョブ専用の別枠を持たない）。

    チャットの「実行中ターン」は `chat_turns.start_turn`（本物の予約方式）を使って作る。
    `run_fn_factory` には実チャット処理の代わりに、合図があるまで待つだけの軽量関数を渡す
    （`tests/unit/test_chat_turns_limits_env.py` と同じ「本物の API を軽い run_fn で呼ぶ」流儀・
    `chat_turns`/`codex_jobs_worker` いずれの内部関数も monkeypatch しない）。
    """
    if not _try_init():
        pytest.skip("DB down")
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

        adm_uid, adm_pw = _mk_admin(sfx)
        _login(adm_uid, adm_pw)
        issued = _issue_key(f"cj-cap-{sfx}")
        _logout()

        r = _submit({"world": "v1", "query": "容量上限テスト"}, api_key=issued["key"])
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        codex_jobs_worker.dispatch_cycle()
        job = store_jobs.get_job(job_id)
        assert job["status"] == "queued", (
            "チャットが global 上限を埋めているのにジョブが running へ進んだ（合算枠が効いていない）")

        store_jobs.cancel_if_queued(job_id)   # 後片付け
    finally:
        release.set()
        for rec in recs:
            rec.stop_event.set()
        with CT._REGISTRY_LOCK:
            for rec in recs:
                CT._REGISTRY.pop(rec.turn_id, None)


# ===== 検収是正②: 取消と完了の競合 =====

def test_codex_job_cancel_wins_race_against_late_completion(fake_codex):
    """`running` 中に取消が先に DB を確定させれば、後から届く完了処理（`mark_completed`）は
    `WHERE status='running'` に一致せず no-op になり、ジョブは `cancelled` のまま・結果
    （回答・出典・未確認項目）は保存されない。`POST .../cancel` は実際の HTTP 経路を使い、
    「後から届く完了処理」は実際の `store_jobs.mark_completed`（ワーカーが呼ぶのと同じ関数）を
    直接呼んで模する——Codex の実行そのものは本テストの対象外（DB 層の原子性の検証）。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cj-race-{sfx}")
    _logout()

    r = _submit({"world": "v1", "query": "競合テスト"}, api_key=issued["key"])
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    claimed = store_jobs.claim_queued_jobs(1)
    assert [j["id"] for j in claimed] == [job_id]   # running へ（ワーカーのスレッドは起こさない）

    c = _cancel(job_id, issued["key"])
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "cancelled"   # DB 側の atomic UPDATE がここで先に勝つ

    # 「後から届く完了処理」——ワーカーが Codex の結果を得て呼ぶのと同じ関数を直接呼ぶ。
    # from_status 既定の "running" に一致しないため no-op になるはず。
    store_jobs.mark_completed(job_id, answer="後から届いた回答", sources=[{"doc_id": "x.md"}],
                              unconfirmed_items=[], elapsed_ms=999)

    row = store_jobs.get_job(job_id)
    assert row["status"] == "cancelled", "取消の後に完了処理が上書きしてしまった"
    assert row["answer"] is None and row["sources"] is None, "cancelled に結果が保存されてしまった"

    rr = _result(job_id, issued["key"])
    assert rr.status_code == 409, rr.text
    assert rr.json()["status"] == "cancelled"
    assert "error_code" not in rr.json()

    # 取消要求の再試行（冪等）も状態を変えない。
    c2 = _cancel(job_id, issued["key"])
    assert c2.status_code == 200, c2.text
    assert c2.json()["status"] == "cancelled"
