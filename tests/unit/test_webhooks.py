"""Webhook 通知（PART-6・sherpa/webhooks.py・docs/proposals/2026-09-05-Webhook通知.md）の unit テスト。

対象: 宛先検証（allowlist必須〔loopback含む〕/userinfo拒否/fail-closed・W3・RV是正#1/#8）・
署名の既知ベクトル（W4）・リトライ回数とバックオフ・試行毎の宛先再評価（W1・RV是正#6・
`time.sleep` を monkeypatch）・配送キュー（単一 worker＋有界キュー・RV是正#4）・監査記録の
detail に secret/フル URL が含まれないこと。ネットワーク I/O は一切発生させない
（`_send_once`/`llm.urlopen_no_redirect` を monkeypatch で差し替える）。
"""
from __future__ import annotations

import hashlib
import hmac
import queue
import threading

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


def test_webhook_host_port_rejects_empty_userinfo():
    """RV是正#8: 空文字の userinfo（`http://@host/`）は `p.username == ""`（falsy）で `or` 判定を
    すり抜けていた旧実装のバグ再発防止。片方だけ空文字の場合も拒否する。"""
    assert webhooks._webhook_host_port("http://@evil.com/hook") is None
    assert webhooks._webhook_host_port("http://user:@evil.com/hook") is None
    assert webhooks._webhook_host_port("http://:pass@evil.com/hook") is None


def test_webhook_host_port_rejects_non_http_scheme():
    assert webhooks._webhook_host_port("ftp://example.com/hook") is None
    assert webhooks._webhook_host_port("javascript:alert(1)") is None


def test_webhook_host_port_strips_trailing_dot():
    assert webhooks._webhook_host_port("http://example.com./hook") == ("example.com", 80)


def test_assert_webhook_url_allowed_loopback_rejected_without_allowlist():
    """RV是正#1: loopback も既定（allowlist 未設定）では拒否される——自己発行キー利用者が
    認証なしの内蔵サービス（例: ES:9200）を宛先登録できてしまう穴を塞ぐ（Ollama の loopback
    常時許可とは意図的に違える）。"""
    with pytest.raises(webhooks.WebhookUrlInvalid):
        webhooks.assert_webhook_url_allowed("http://127.0.0.1:8080/hook", system_settings={})
    with pytest.raises(webhooks.WebhookUrlInvalid):
        webhooks.assert_webhook_url_allowed("http://localhost/hook", system_settings={})


def test_assert_webhook_url_allowed_loopback_allowed_when_allowlisted():
    """loopback でも `webhook_allowlist` に host:port を明示登録すれば許可される。"""
    settings = {"webhook_allowlist": ["127.0.0.1:8080"]}
    webhooks.assert_webhook_url_allowed("http://127.0.0.1:8080/hook", system_settings=settings)


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


def test_deliver_reevaluates_allowlist_before_each_attempt(monkeypatch):
    """RV是正#6: 宛先ポリシーは試行ごとに再評価する——3回目の直前で admin が allowlist を
    変更した想定にすると、2回（送信失敗）した時点で打ち切り、3回目は `_send_once` へ到達しない。
    """
    from sherpa import store

    check_calls = []

    def _check(url, **kw):
        check_calls.append(1)
        if len(check_calls) >= 3:
            raise webhooks.WebhookUrlInvalid("拒否（途中で allowlist が変わった想定）")

    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", _check)

    send_calls = []

    def _always_fail(*a, **kw):
        send_calls.append(1)
        raise ConnectionError("boom")

    monkeypatch.setattr(webhooks, "_send_once", _always_fail)
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._deliver(5, "https://example.com/hook", "sec", {
        "event": "ingest.failed", "world": "test", "run_id": 6})

    assert len(check_calls) == 3    # 1・2回目は許可・3回目の再評価で不許可
    assert len(send_calls) == 2     # 3回目は _send_once へ到達しない
    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[1] == "webhook.failed"
    assert kwargs["detail"]["attempts"] == 2
    assert kwargs["reason"] == "WebhookUrlInvalid"


def test_send_once_does_not_read_response_body(monkeypatch):
    """RV是正#3: 応答本体は読まない（`with` を抜けるだけで完了・2xx 以外は `urlopen` 自身が
    `HTTPError` を送出する契約に乗る）。"""
    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise AssertionError("_send_once は応答本体を読んではいけない")

    monkeypatch.setattr(webhooks.llm, "urlopen_no_redirect",
                        lambda req, timeout=None: _FakeResp())
    webhooks._send_once("https://example.com/hook", "sec", b"{}", "req-1", "ingest.completed")


# ---- notify_run_terminal のイベント/対象キー解決 ----
# RV是正#4: 実送信は単一 daemon worker＋有界キューへ変わった（`threading.Thread` をキーごとに
# 起こす旧実装ではない）ため、ここでは `_enqueue`（キュー投入の直前まで）を monkeypatch して
# 検証する。`_enqueue` 自体・worker/キューの挙動は下の「配送キュー」節で別途検証する。

def test_notify_run_terminal_maps_status_to_event_and_enqueues_per_key(monkeypatch):
    from sherpa import store

    monkeypatch.setattr(store, "list_webhook_keys_for_world", lambda world: [
        {"id": 1, "webhook_url": "https://a.example.com/hook", "webhook_secret": "sa"},
        {"id": 2, "webhook_url": "https://b.example.com/hook", "webhook_secret": "sb"},
    ])
    enqueued = []
    monkeypatch.setattr(webhooks, "_enqueue", lambda *a: enqueued.append(a))

    webhooks.notify_run_terminal("test", 42, "sync", "auto_published_with_flags", doc_count=5)

    assert len(enqueued) == 2
    key_id0, url0, secret0, payload0 = enqueued[0]
    assert key_id0 == 1
    assert url0 == "https://a.example.com/hook"
    assert secret0 == "sa"
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
    enqueued = []
    monkeypatch.setattr(webhooks, "_enqueue", lambda *a: enqueued.append(a))

    webhooks.notify_run_terminal("test", 1, "delete", "failed")

    assert enqueued[0][3]["event"] == "ingest.failed"


def test_notify_run_terminal_no_keys_does_not_enqueue(monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "list_webhook_keys_for_world", lambda world: [])
    monkeypatch.setattr(webhooks, "_enqueue",
                        lambda *a, **kw: pytest.fail("should not enqueue"))
    webhooks.notify_run_terminal("test", 1, "sync", "auto_published")


def test_notify_run_terminal_swallows_store_errors(monkeypatch):
    """対象キー列挙が失敗しても例外を外へ伝播しない（best-effort・呼び出し元の取り込みを壊さない）。"""
    from sherpa import store

    def _boom(world):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "list_webhook_keys_for_world", _boom)
    webhooks.notify_run_terminal("test", 1, "sync", "auto_published")   # 例外を投げなければ成功


# ---- 配送キュー: 単一 worker＋有界キュー（W1・RV是正#4） ----

@pytest.fixture
def _isolated_queue(monkeypatch):
    """モジュール共有のグローバル `_queue`/`_worker_thread` をテスト間で汚染しないよう、
    各テストで専用のインスタンスに差し替える（本番は単一プロセス内で1つを使い回す設計のため、
    モジュール変数を直接 monkeypatch する。実スレッドは起こさない——`_ensure_worker_started`
    は各テストで個別に monkeypatch/検証する）。"""
    fake_q: queue.Queue = queue.Queue(maxsize=2)
    monkeypatch.setattr(webhooks, "_queue", fake_q)
    monkeypatch.setattr(webhooks, "_worker_thread", None)
    return fake_q


def test_enqueue_starts_worker_and_puts_item(monkeypatch, _isolated_queue):
    monkeypatch.setattr(webhooks, "_ensure_worker_started", lambda: None)
    webhooks._enqueue(1, "https://example.com/hook", "sec", {"world": "test", "run_id": 1})
    assert _isolated_queue.qsize() == 1
    assert _isolated_queue.get_nowait() == (
        1, "https://example.com/hook", "sec", {"world": "test", "run_id": 1})


def test_enqueue_drops_and_audits_when_queue_full(monkeypatch, _isolated_queue):
    """RV是正#4: キュー飽和時はその1件だけ捨てて `webhook.dropped` を監査記録する
    （他キー・取り込み自体には影響させない）。"""
    from sherpa import store
    monkeypatch.setattr(webhooks, "_ensure_worker_started", lambda: None)
    _isolated_queue.put_nowait((0, "u", "s", {}))   # maxsize=2 を先に埋める
    _isolated_queue.put_nowait((0, "u", "s", {}))
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._enqueue(9, "https://example.com/hook", "sec",
                      {"world": "test", "run_id": 3, "event": "ingest.completed"})

    assert _isolated_queue.qsize() == 2   # 溢れた分は積まれない
    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[1] == "webhook.dropped"
    assert args[3] == "9"
    assert kwargs["reason"] == "queue_full"
    assert kwargs["detail"]["world"] == "test"
    assert kwargs["detail"]["run_id"] == 3


def test_ensure_worker_started_starts_only_one_thread(monkeypatch, _isolated_queue):
    """worker は lazy start・複数回呼んでも生存中なら1本しか起動しない。"""
    starts = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._alive = True

        def start(self):
            starts.append(1)

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(webhooks.threading, "Thread", _FakeThread)
    webhooks._ensure_worker_started()
    webhooks._ensure_worker_started()
    assert len(starts) == 1


def test_process_queue_item_calls_deliver(monkeypatch):
    """`_worker_loop` の本体（1件分の処理）を実スレッド/実キューなしで直接検証する。"""
    calls = []
    monkeypatch.setattr(webhooks, "_deliver", lambda *a: calls.append(a))
    webhooks._process_queue_item((1, "https://example.com/hook", "sec", {"a": 1}))
    assert calls == [(1, "https://example.com/hook", "sec", {"a": 1})]


def test_process_queue_item_swallows_deliver_exceptions(monkeypatch):
    """`_deliver` が想定外の例外を投げても worker（呼び出し元のループ）を落とさない。"""
    def _boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(webhooks, "_deliver", _boom)
    webhooks._process_queue_item((1, "https://example.com/hook", "sec", {}))   # 例外を投げなければ成功
