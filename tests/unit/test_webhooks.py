"""Webhook 通知（PART-6・sherpa/webhooks.py・docs/proposals/2026-09-05-Webhook通知.md）の unit テスト。

対象: 宛先検証（loopback/allowlist/userinfo拒否/fail-closed・W3）・署名の既知ベクトル（W4）・
リトライ回数とバックオフ（W1・`time.sleep` を monkeypatch）・監査記録の detail に secret/フル URL
が含まれないこと。ネットワーク I/O は一切発生させない（`_send_once`/`llm.urlopen_no_redirect` を
monkeypatch で差し替える）。
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from sherpa import webhooks


# ---- 宛先検証（W3） ----

def test_webhook_host_port_allows_path_and_query():
    """`_webhook_host_port` は path/query を持つ URL でも host:port を抽出できる
    （`llm._canonical_host_port` は path 付きを拒否する契約＝ここが差分）。"""
    assert webhooks._webhook_host_port("https://example.com:8443/hooks/sherpa?x=1") == ("example.com", 8443)


def test_webhook_host_port_default_ports():
    assert webhooks._webhook_host_port("http://example.com/hook") == ("example.com", 80)
    assert webhooks._webhook_host_port("https://example.com/hook") == ("example.com", 443)


def test_webhook_host_port_rejects_userinfo():
    assert webhooks._webhook_host_port("http://user:pass@evil.com/hook") is None


def test_webhook_host_port_rejects_non_http_scheme():
    assert webhooks._webhook_host_port("ftp://example.com/hook") is None
    assert webhooks._webhook_host_port("javascript:alert(1)") is None


def test_webhook_host_port_strips_trailing_dot():
    assert webhooks._webhook_host_port("http://example.com./hook") == ("example.com", 80)


def test_assert_webhook_url_allowed_loopback_always_allowed():
    """loopback は allowlist が空でも常に許可される。"""
    webhooks.assert_webhook_url_allowed("http://127.0.0.1:8080/hook", system_settings={})
    webhooks.assert_webhook_url_allowed("http://localhost/hook", system_settings={})


def test_assert_webhook_url_allowed_fail_closed_without_allowlist():
    """非 loopback は allowlist 未設定（空 dict＝webhook_allowlist キー無し）なら拒否（fail-closed）。"""
    with pytest.raises(webhooks.WebhookUrlInvalid):
        webhooks.assert_webhook_url_allowed("https://example.com/hook", system_settings={})


def test_assert_webhook_url_allowed_matches_allowlist_entry():
    settings = {"webhook_allowlist": ["example.com:443"]}
    webhooks.assert_webhook_url_allowed("https://example.com/hooks/sherpa?x=1", system_settings=settings)


def test_assert_webhook_url_allowed_rejects_unlisted_host():
    settings = {"webhook_allowlist": ["other.example.com:443"]}
    with pytest.raises(webhooks.WebhookUrlInvalid):
        webhooks.assert_webhook_url_allowed("https://example.com/hook", system_settings=settings)


def test_assert_webhook_url_allowed_rejects_malformed_url():
    with pytest.raises(webhooks.WebhookUrlInvalid):
        webhooks.assert_webhook_url_allowed("not a url", system_settings={})
    with pytest.raises(webhooks.WebhookUrlInvalid):
        webhooks.assert_webhook_url_allowed("http://user:pass@example.com/hook",
                                            system_settings={"webhook_allowlist": ["example.com:80"]})


# ---- 署名（W4） ----

def test_sign_known_vector():
    body = b'{"event":"ingest.completed"}'
    secret = "s3cr3t"
    got = webhooks._sign(secret, body)
    expect = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert got == expect
    assert got.startswith("sha256=")


def test_sign_differs_by_secret():
    body = b"{}"
    assert webhooks._sign("a", body) != webhooks._sign("b", body)


# ---- 配送・リトライ（W1） ----

class _FakeStore:
    """`_deliver`/`notify_run_terminal` が `from . import store` で解決する `sherpa.store` の
    代わりに使う——`monkeypatch.setattr(store, ...)` で個別関数を差し替える方が実 DB 非依存で
    完結する（`sherpa.store` は import 済みシングルトンなのでモジュール属性の monkeypatch が
    `webhooks.py` 内の遅延 import にもそのまま効く）。"""


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """リトライのバックオフを待たない（テストを高速化する・回数自体は別途検証する）。"""
    monkeypatch.setattr(webhooks.time, "sleep", lambda *_a, **_kw: None)


def test_deliver_success_on_first_attempt_records_delivered_audit(monkeypatch):
    from sherpa import store

    sent = []
    monkeypatch.setattr(webhooks, "_send_once", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda *a, **kw: None)
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._deliver(7, "https://example.com/hook", "sec", {
        "event": "ingest.completed", "world": "test", "run_id": 1})

    assert len(sent) == 1   # 即時送信で成功＝リトライしない
    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[1] == "webhook.delivered"
    assert args[2] == "webhook"
    assert args[3] == "7"
    assert kwargs["outcome"] == "success"
    assert kwargs["detail"]["attempts"] == 1
    assert kwargs["detail"]["host_port"] == "example.com:443"
    # secret・フル URL（path 含む）が detail に一切含まれないこと。
    detail_str = repr(kwargs["detail"])
    assert "sec" not in detail_str
    assert "/hook" not in detail_str


def test_deliver_retries_three_times_then_gives_up(monkeypatch):
    """即時送信＋失敗時リトライ3回＝合計4回試行してから諦める（W1）。"""
    from sherpa import store

    attempts = []

    def _always_fail(*a, **kw):
        attempts.append(1)
        raise ConnectionError("boom")

    monkeypatch.setattr(webhooks, "_send_once", _always_fail)
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda *a, **kw: None)
    sleeps = []
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: sleeps.append(s))
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._deliver(9, "https://example.com/hook", "sec", {
        "event": "ingest.failed", "world": "test", "run_id": 2})

    assert len(attempts) == 4                 # 即時1回＋リトライ3回
    assert sleeps == [2, 8, 30]                # W1 の裁定どおりのバックオフ間隔
    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[1] == "webhook.failed"
    assert kwargs["outcome"] == "failure"
    assert kwargs["detail"]["attempts"] == 4
    assert kwargs["reason"] == "ConnectionError"


def test_deliver_succeeds_on_retry_stops_immediately(monkeypatch):
    """2回目で成功したら3回目以降は送らない。"""
    from sherpa import store

    attempts = []

    def _fail_once_then_succeed(*a, **kw):
        attempts.append(1)
        if len(attempts) < 2:
            raise TimeoutError("slow")

    monkeypatch.setattr(webhooks, "_send_once", _fail_once_then_succeed)
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda *a, **kw: None)
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._deliver(1, "https://example.com/hook", "sec", {
        "event": "ingest.completed", "world": "test", "run_id": 3})

    assert len(attempts) == 2
    assert audits[0][1]["outcome"] == "success"
    assert audits[0][1]["detail"]["attempts"] == 2


def test_deliver_invalid_destination_skips_network_and_retries(monkeypatch):
    """送信直前の宛先再検証で不許可なら、ネットワークへは一切出さず1回で打ち切る（リトライしない）。"""
    from sherpa import store

    sent = []
    monkeypatch.setattr(webhooks, "_send_once", lambda *a, **kw: sent.append(a))

    def _reject(*a, **kw):
        raise webhooks.WebhookUrlInvalid("拒否")

    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", _reject)
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._deliver(3, "https://evil.example.com/hook", "sec", {
        "event": "ingest.completed", "world": "test", "run_id": 4})

    assert sent == []
    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[1] == "webhook.failed"
    assert kwargs["detail"]["attempts"] == 0


# ---- notify_run_terminal のイベント/対象キー解決 ----

def test_notify_run_terminal_maps_status_to_event_and_schedules_per_key(monkeypatch):
    from sherpa import store

    monkeypatch.setattr(store, "list_webhook_keys_for_world", lambda world: [
        {"id": 1, "webhook_url": "https://a.example.com/hook", "webhook_secret": "sa"},
        {"id": 2, "webhook_url": "https://b.example.com/hook", "webhook_secret": "sb"},
    ])
    started = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self._target = target
            self._args = args

        def start(self):
            started.append(self._args)
            self._target(*self._args)   # 同期実行（テストではスレッド化不要）

    monkeypatch.setattr(webhooks.threading, "Thread", _FakeThread)
    delivered = []
    monkeypatch.setattr(webhooks, "_deliver", lambda *a: delivered.append(a))

    webhooks.notify_run_terminal("test", 42, "sync", "auto_published_with_flags", doc_count=5)

    assert len(started) == 2
    assert len(delivered) == 2
    payload0 = delivered[0][3]
    assert payload0["event"] == "ingest.completed"
    assert payload0["world"] == "test"
    assert payload0["run_id"] == 42
    assert payload0["op"] == "sync"
    assert payload0["status"] == "auto_published_with_flags"
    assert payload0["doc_count"] == 5


def test_notify_run_terminal_failed_status_maps_to_ingest_failed(monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "list_webhook_keys_for_world", lambda world: [
        {"id": 1, "webhook_url": "https://a.example.com/hook", "webhook_secret": "sa"}])
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            captured["args"] = args

        def start(self):
            pass

    monkeypatch.setattr(webhooks.threading, "Thread", _FakeThread)

    webhooks.notify_run_terminal("test", 1, "delete", "failed")

    assert captured["args"][3]["event"] == "ingest.failed"


def test_notify_run_terminal_no_keys_does_not_spawn_thread(monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "list_webhook_keys_for_world", lambda world: [])
    spawned = []
    monkeypatch.setattr(webhooks.threading, "Thread",
                        lambda *a, **kw: spawned.append(1) or pytest.fail("should not spawn"))
    webhooks.notify_run_terminal("test", 1, "sync", "auto_published")
    assert spawned == []


def test_notify_run_terminal_swallows_store_errors(monkeypatch):
    """対象キー列挙が失敗しても例外を外へ伝播しない（best-effort・呼び出し元の取り込みを壊さない）。"""
    from sherpa import store

    def _boom(world):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "list_webhook_keys_for_world", _boom)
    webhooks.notify_run_terminal("test", 1, "sync", "auto_published")   # 例外を投げなければ成功
