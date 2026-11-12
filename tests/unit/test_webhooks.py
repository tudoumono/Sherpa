"""Webhook 通知（PART-6・sherpa/webhooks.py・docs/archive/2026-09-05-Webhook通知.md）の unit テスト。

対象: 宛先検証（allowlist必須〔loopback含む〕/userinfo拒否/fail-closed・W3・RV是正#1/#8）・
署名の既知ベクトル（W4）・1 回分の配送（送信のたびの宛先再評価・不許可と宛先なしは再送しない）・
配送待ちへの保存・監査記録の detail に secret/フル URL が含まれないこと。ネットワーク I/O は一切発生させない
（`_send_once`/`llm.urlopen_no_redirect` を monkeypatch で差し替える）。再送の間隔と上限・永続は `tests/api/test_outbox_delivery.py`。
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from sherpa import outbox_sender, webhooks


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


def _row(key_id=7, payload=None, attempts=1, row_id=11):
    return {"id": row_id, "kind": "webhook", "key_id": key_id, "attempts": attempts,
            "payload": payload or {"event": "ingest.completed", "world": "test", "run_id": 1}}


def _dest(monkeypatch, url="https://example.com/hook", secret="sec"):
    from sherpa import store
    monkeypatch.setattr(store, "get_api_key_webhook",
                        lambda kid: {"webhook_url": url, "webhook_secret": secret} if url else None)


def test_deliver_row_success_sends_once_and_records_delivered_audit(monkeypatch):
    from sherpa import store

    sent = []
    monkeypatch.setattr(webhooks, "_send_once", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda *a, **kw: None)
    _dest(monkeypatch)
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._deliver_row(_row())

    assert len(sent) == 1
    assert sent[0][:2] == ("https://example.com/hook", "sec")
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


def test_deliver_row_send_failure_propagates_so_the_sender_can_retry(monkeypatch):
    def _fail(*a, **kw):
        raise ConnectionError("boom")

    monkeypatch.setattr(webhooks, "_send_once", _fail)
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda *a, **kw: None)
    _dest(monkeypatch)
    with pytest.raises(ConnectionError):
        webhooks._deliver_row(_row())


def test_deliver_row_uses_the_same_request_id_on_every_attempt(monkeypatch):
    rids = []
    monkeypatch.setattr(webhooks, "_send_once", lambda url, secret, body, rid, event: rids.append(rid))
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda *a, **kw: None)
    monkeypatch.setattr("sherpa.store.audit", lambda *a, **kw: None)
    _dest(monkeypatch)
    webhooks._deliver_row(_row(attempts=1))
    webhooks._deliver_row(_row(attempts=2))
    assert len(set(rids)) == 1


def test_deliver_row_invalid_destination_skips_network_and_gives_up(monkeypatch):
    """送信直前の宛先再検証で不許可なら、ネットワークへは一切出さず再送もしない（`GiveUp`）。"""
    sent = []
    monkeypatch.setattr(webhooks, "_send_once", lambda *a, **kw: sent.append(a))

    def _reject(*a, **kw):
        raise webhooks.WebhookUrlInvalid("拒否")

    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", _reject)
    _dest(monkeypatch, url="https://evil.example.com/hook")

    with pytest.raises(outbox_sender.GiveUp, match="destination_not_allowed"):
        webhooks._deliver_row(_row(key_id=3))
    assert sent == []


def test_deliver_row_destination_removed_after_queued_gives_up_without_sending(monkeypatch):
    """積んだ後に宛先が無効になったら（失効・削除・所有者無効化）送らず、再送もしない。"""
    sent = []
    monkeypatch.setattr(webhooks, "_send_once", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(webhooks, "assert_webhook_url_allowed", lambda *a, **kw: None)
    _dest(monkeypatch, url=None)
    with pytest.raises(outbox_sender.GiveUp, match="destination_removed"):
        webhooks._deliver_row(_row())
    assert sent == []


def test_record_gave_up_audits_the_reason(monkeypatch):
    from sherpa import store
    _dest(monkeypatch)
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))
    webhooks._record_gave_up(_row(attempts=4), "ConnectionError")
    args, kwargs = audits[0]
    assert args[1] == "webhook.failed"
    assert kwargs["outcome"] == "failure"
    assert kwargs["reason"] == "ConnectionError"
    assert kwargs["detail"]["attempts"] == 4


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
# 配送待ちへの保存は `_enqueue` が担う。ここでは `_enqueue` を monkeypatch して対象キューと本文だけを検証する。
# 保存・送り手の挙動は `tests/api/test_outbox_delivery.py`。

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
    key_id0, payload0 = enqueued[0]
    assert key_id0 == 1
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

    assert enqueued[0][1]["event"] == "ingest.failed"


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


# ---- 配送待ちへの保存 ----

def test_enqueue_saves_to_the_outbox(monkeypatch):
    saved = []
    monkeypatch.setattr(webhooks.outbox, "add", lambda kind, payload, key_id=None: saved.append((kind, payload, key_id)))
    webhooks._enqueue(1, {"world": "test", "run_id": 1})
    assert saved == [("webhook", {"world": "test", "run_id": 1}, 1)]


def test_enqueue_audits_dropped_when_outbox_is_full(monkeypatch):
    """配送待ちが上限のときは、その1件だけ保存せず `webhook.dropped` を監査記録する（他キー・取り込み自体には影響させない）。"""
    from sherpa import store

    def _full(*a, **kw):
        raise webhooks.outbox.OutboxFull("webhook")

    monkeypatch.setattr(webhooks.outbox, "add", _full)
    _dest(monkeypatch)
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    webhooks._enqueue(9, {"world": "test", "run_id": 3, "event": "ingest.completed"})

    assert len(audits) == 1
    args, kwargs = audits[0]
    assert args[1] == "webhook.dropped"
    assert args[3] == "9"
    assert kwargs["reason"] == "outbox_full"
    assert kwargs["detail"]["world"] == "test"
    assert kwargs["detail"]["run_id"] == 3


def test_enqueue_swallows_save_errors(monkeypatch):
    """保存に失敗しても例外を呼び出し元（取り込み）へ伝えない。"""
    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(webhooks.outbox, "add", _boom)
    webhooks._enqueue(1, {"world": "test"})
