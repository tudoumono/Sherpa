"""終了理由の正規化（STAT-3 T3・`sherpa/stop_kind.py`）。

`messages.answer.stop_kind` に保存する8値の閉じた語彙と、その導出（`resolve`・`from_exception`）を
純粋関数として固定する。DB 不要。
"""
from __future__ import annotations

import http.client
import socket
import urllib.error

from sherpa import stop_kind


def test_stop_kinds_is_closed_8_value_vocabulary():
    assert stop_kind.STOP_KINDS == {
        "completed", "stopped_by_user", "budget", "no_evidence",
        "transport_error", "timeout", "codex_silent", "codex_partial",
    }


# ===== resolve() =====

def test_resolve_defaults_to_completed_for_empty_env():
    assert stop_kind.resolve({}) == "completed"


def test_resolve_codex_silent_failure_takes_priority():
    env = {"codex_silent_failure": True, "codex_stopped_early": True,
           "data": {"evidence_packet": {"stop_reason": "turns_exhausted"}}}
    assert stop_kind.resolve(env) == "codex_silent"


def test_resolve_codex_stopped_early_maps_to_codex_partial():
    env = {"codex_stopped_early": True}
    assert stop_kind.resolve(env) == "codex_partial"


def test_resolve_budget_stop_reasons():
    for reason in ("turns_exhausted", "budget_exceeded", "tools_per_turn_exceeded"):
        env = {"data": {"evidence_packet": {"stop_reason": reason}}}
        assert stop_kind.resolve(env) == "budget", reason


def test_resolve_no_evidence_stop_reasons():
    for reason in ("evaluation_blocked", "evidence_verification_failed"):
        env = {"data": {"evidence_packet": {"stop_reason": reason}}}
        assert stop_kind.resolve(env) == "no_evidence", reason


def test_resolve_other_stop_reasons_stay_completed():
    """no_tool_calls/evaluation_sufficient/truncated/content_filtered/refusal/unknown は
    別扱いにしない（既存の stop_reason 自体が answer に残るため分布はそちらで見分けられる）。"""
    for reason in ("no_tool_calls", "evaluation_sufficient", "truncated",
                  "content_filtered", "refusal", "unknown"):
        env = {"data": {"evidence_packet": {"stop_reason": reason}}}
        assert stop_kind.resolve(env) == "completed", reason


def test_resolve_missing_evidence_packet_defaults_to_completed():
    assert stop_kind.resolve({"data": {}}) == "completed"
    assert stop_kind.resolve({"data": None}) == "completed"


# ===== from_exception() =====

def test_from_exception_timeout_error_direct():
    assert stop_kind.from_exception(TimeoutError("deadline")) == "timeout"


def test_from_exception_socket_timeout_is_timeout_error_alias():
    # Python 3.10+: socket.timeout is an alias of TimeoutError.
    assert socket.timeout is TimeoutError
    assert stop_kind.from_exception(socket.timeout("deadline")) == "timeout"


def test_from_exception_urlerror_wrapping_timeout_is_timeout():
    exc = urllib.error.URLError(TimeoutError("deadline"))
    assert stop_kind.from_exception(exc) == "timeout"


def test_from_exception_urlerror_wrapping_other_reason_is_transport_error():
    exc = urllib.error.URLError(ConnectionRefusedError("refused"))
    assert stop_kind.from_exception(exc) == "transport_error"


def test_from_exception_os_error_is_transport_error():
    assert stop_kind.from_exception(OSError("network unreachable")) == "transport_error"


def test_from_exception_http_client_exception_is_transport_error():
    assert stop_kind.from_exception(http.client.BadStatusLine("garbage")) == "transport_error"


def test_from_exception_unrelated_exception_returns_none():
    assert stop_kind.from_exception(RuntimeError("boom")) is None
    assert stop_kind.from_exception(ValueError("bad")) is None


def test_resolve_returns_none_for_busy_turn():
    # Codex 直列化で実行していないターンは完了として数えない（8値も増やさない）
    assert stop_kind.resolve({"busy": True, "codex_silent_failure": True}) is None


def test_resolve_agentic_failure_error_returns_none():
    # API 経路の honest failure（型を運べない失敗）は completed にしない＝NULL（集計側 unknown）
    env = {"agentic_failure": "error",
           "data": {"evidence_packet": {"stop_reason": "evaluation_sufficient"}}}
    assert stop_kind.resolve(env) is None


def test_resolve_agentic_failure_insufficient_maps_to_no_evidence():
    assert stop_kind.resolve({"agentic_failure": "insufficient"}) == "no_evidence"


def test_resolve_agentic_failure_typed_transport_values_pass_through():
    assert stop_kind.resolve({"agentic_failure": "timeout"}) == "timeout"
    assert stop_kind.resolve({"agentic_failure": "transport_error"}) == "transport_error"


# ===== `_plain_run`（素の会話）が定型文へ落ちたターンに印を付ける =====

def _plain_ctx():
    from sherpa.providers.base import Ctx
    return Ctx(message="こんにちは", world="w", route=lambda m: {"lens": "chat"},
               dispatch=lambda *a, **k: None, knowledge=False)


class _EmptyPlain:
    label = "test"
    _last_usage = None

    def _plain_stream(self, message):
        return iter(())

    def _plain_text(self, message=""):
        return "まだ接続されていません"


class _RaisingPlain(_EmptyPlain):
    def _plain_stream(self, message):
        raise TimeoutError("timed out")


class _OkPlain(_EmptyPlain):
    def _plain_stream(self, message):
        yield "本文"


def _plain_env(provider):
    from sherpa.providers import base
    res = [ev for ev in base._plain_run(provider, _plain_ctx()) if ev.get("type") == "_result"]
    return res[0]["env"]


def test_plain_run_marks_empty_stream_as_failure():
    env = _plain_env(_EmptyPlain())
    assert env["agentic_failure"] == "error"
    assert stop_kind.resolve(env) is None


def test_plain_run_marks_timeout_stream_exception_as_timeout():
    env = _plain_env(_RaisingPlain())
    assert env["agentic_failure"] == "timeout"
    assert stop_kind.resolve(env) == "timeout"


def test_plain_run_leaves_successful_turn_unmarked():
    env = _plain_env(_OkPlain())
    assert "agentic_failure" not in env
    assert stop_kind.resolve(env) == "completed"


def test_from_exception_http_error_response_is_not_transport_error():
    # 401/429/5xx は応答が返っている＝通信障害ではない（型だけでは原因を区別できないため None）
    exc = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)
    assert stop_kind.from_exception(exc) is None
