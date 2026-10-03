"""`sherpa/agentic_search.py::_post`（テストが差し替える単発の送信 choke point・自前ではリトライしない）と
`_retry_after_seconds`（429 の Retry-After 解釈・埋め込みの再送が使う）の単体テスト。
"""
from __future__ import annotations

import os
import urllib.error

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

from sherpa import agentic_search as A  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """バックオフの実待ちでテストを遅くしない（sleep したかどうかは呼び出し回数/引数で確認する）。"""
    calls = []
    monkeypatch.setattr(A.time, "sleep", lambda sec: calls.append(sec))
    return calls


def _http_error(code: int, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", headers, None)


# ===== 1. 分類関数の単体テスト =====

def test_retry_after_seconds_parses_numeric_and_caps():
    import email.message
    h = email.message.Message()
    h["Retry-After"] = "3"
    assert A._retry_after_seconds(_http_error(429, h)) == 3.0
    h2 = email.message.Message()
    h2["Retry-After"] = "9999"
    assert A._retry_after_seconds(_http_error(429, h2)) == A._RETRY_AFTER_CAP_SEC   # 上限で頭打ち


def test_retry_after_seconds_none_when_missing_or_unparseable():
    import email.message

    assert A._retry_after_seconds(_http_error(429)) is None
    h = email.message.Message()
    h["Retry-After"] = "not-a-date-or-number"
    assert A._retry_after_seconds(_http_error(429, h)) is None


def test_retry_after_seconds_rejects_nan_negative_and_infinity():
    """`float()` は "nan"/"inf"/"-inf" 等も受理してしまうため、これらを不正値として None
    （指数バックオフへのフォールバック）にする。"""
    import email.message

    for bad in ("nan", "inf", "-inf", "infinity", "-5"):
        h = email.message.Message()
        h["Retry-After"] = bad
        assert A._retry_after_seconds(_http_error(429, h)) is None, bad


# ===== 2. `_post` 自体はリトライしない（単発 choke point・テストが差し替える契約を保つ） =====

def test_post_is_single_attempt_no_retry(monkeypatch):
    attempts = []

    def fake_post_json(url, headers, body, timeout):
        attempts.append(1)
        raise _http_error(429)   # 再試行対象のエラーでも _post 自体はリトライしない

    monkeypatch.setattr(A.llm, "post_json", fake_post_json)
    with pytest.raises(urllib.error.HTTPError):
        A._post("http://x", {}, {}, timeout=10)
    assert len(attempts) == 1


# ===== 3. `_send`（`openai_style` 経由）: 呼び出し予算・usage・stop_event の内側でリトライする =====

def _final_of(events):
    return next(ev for ev in events if "final" in ev)

