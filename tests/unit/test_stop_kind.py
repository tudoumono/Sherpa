"""終了理由の正規化（STAT-3 T3・`sherpa/stop_kind.py`）。

`messages.answer.stop_kind` に保存する8値の閉じた語彙と、その導出（`resolve`・`from_exception`）を
純粋関数として固定する。DB 不要。
"""
from __future__ import annotations

import http.client
import socket
import urllib.error

import pytest

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
        env = {"data": {"evidence_packet": {"task_id": "main", "stop_reason": reason}}}
        assert stop_kind.resolve(env) == "budget", reason


def test_resolve_no_evidence_stop_reasons():
    for reason in ("evaluation_blocked", "evidence_verification_failed"):
        env = {"data": {"evidence_packet": {"task_id": "main", "stop_reason": reason}}}
        assert stop_kind.resolve(env) == "no_evidence", reason


def test_resolve_other_stop_reasons_stay_completed():
    """no_tool_calls/evaluation_sufficient/truncated/content_filtered/refusal/unknown は
    別扱いにしない（既存の stop_reason 自体が answer に残るため分布はそちらで見分けられる）。"""
    for reason in ("no_tool_calls", "evaluation_sufficient", "truncated",
                  "content_filtered", "refusal", "unknown"):
        env = {"data": {"evidence_packet": {"task_id": "main", "stop_reason": reason}}}
        assert stop_kind.resolve(env) == "completed", reason


# ===== task_id ガード（ハイブリッド下調べ・プラン経路の budget/no_evidence を main と混同しない） =====

def test_resolve_budget_stop_reason_skipped_for_sub_task_id():
    env = {"data": {"evidence_packet": {"task_id": "sub:worker", "stop_reason": "turns_exhausted"}}}
    assert stop_kind.resolve(env) == "completed"


def test_resolve_budget_stop_reason_skipped_for_plan_task_id():
    env = {"data": {"evidence_packet": {"task_id": "plan:a+b", "stop_reason": "budget_exceeded"}}}
    assert stop_kind.resolve(env) == "completed"


def test_resolve_no_evidence_stop_reason_skipped_for_sub_task_id():
    env = {"data": {"evidence_packet": {"task_id": "sub:worker", "stop_reason": "evaluation_blocked"}}}
    assert stop_kind.resolve(env) == "completed"


def test_resolve_budget_stop_reason_applies_for_main_task_id():
    env = {"data": {"evidence_packet": {"task_id": "main", "stop_reason": "turns_exhausted"}}}
    assert stop_kind.resolve(env) == "budget"


def test_is_main_task_predicate():
    assert stop_kind.is_main_task({"task_id": "main"}) is True
    assert stop_kind.is_main_task({"task_id": "sub:worker"}) is False
    assert stop_kind.is_main_task({"task_id": "plan:a+b"}) is False
    assert stop_kind.is_main_task({}) is False


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


def test_from_exception_connection_error_is_transport_error():
    assert stop_kind.from_exception(ConnectionError("network unreachable")) == "transport_error"


def test_from_exception_socket_gaierror_is_transport_error():
    assert stop_kind.from_exception(socket.gaierror("name resolution failed")) == "transport_error"


def test_from_exception_permission_error_is_not_transport_error():
    # PermissionError/FileNotFoundError/OSError(ENOSPC) 等のローカル I/O 障害は OSError のサブクラス
    # だが通信障害ではない——`OSError` 全体を対象にすると「ワークスペースの権限エラー」が
    # 「通信障害」と誤記録される。型を特定できないため None（NULL→unknown）。
    assert stop_kind.from_exception(PermissionError("workspace not writable")) is None


def test_from_exception_file_not_found_error_is_not_transport_error():
    assert stop_kind.from_exception(FileNotFoundError("missing")) is None


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


# ===== resolve() は STOP_KINDS 外の値を返さない（自己検査） =====

def test_resolve_raises_for_value_outside_stop_kinds(monkeypatch):
    """`_resolve_kind` が万一 `STOP_KINDS` に無い値を返しても `resolve()` が例外にする
    （語彙外の値が `messages.answer.stop_kind` に漏れない自己検査）。"""
    monkeypatch.setattr(stop_kind, "_resolve_kind", lambda env: "not_a_real_stop_kind")
    with pytest.raises(ValueError):
        stop_kind.resolve({})


def test_resolve_none_still_passes_through_the_check():
    assert stop_kind.resolve({"busy": True}) is None


# ===== 下調べ役 catch-all は例外の型（timeout/transport_error）を優先して立てる =====
# 従来は `agentic_failure` を "insufficient"/"error" に固定し `from_exception` を一度も呼ばず、
# 下調べ役（Ollama 等）の read timeout が固定値 "error" に丸められていた。

def test_agentic_run_catchall_marks_timeout_from_sub_loop_exception():
    from sherpa.providers.base import Ctx, _GenProvider

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"

        def _sub_agentic_loop(self, ctx):
            raise TimeoutError("下調べ役が応答しない")
            yield {}   # pragma: no cover - ジェネレータにするためのダミー yield（到達しない）

    p = _P()
    p._sub = {"provider": "openai", "key": "sk-x", "url": None, "model": "gpt-5.4-mini",
              "tools": frozenset({"ripgrep_search"}), "guard": {"min_citations": 1, "max_turns": 6,
                                                                "llm_timeout": 60},
              "profile_id": "search-helper-openai", "description": "", "name": "下調べ役"}
    ctx = Ctx(message="バッチ停止の記録は？", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "reason": "t", "input": m},
              dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
              make_sources=lambda docs: [{"doc_id": d} for d in docs])
    events = list(p.run(ctx))
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["agentic_failure"] == "timeout", (
        f"下調べ役のタイムアウトが固定値 'error' に丸められている: {env!r}")
    assert stop_kind.resolve(env) == "timeout"


# ===== 単発清書フォールバックは型が特定できない例外でも completed 扱いにしない =====
# 従来は `stop_kind.from_exception` が None を返す例外（HTTPError の 401/429/5xx・JSON デコード
# エラー等）は無印のまま `resolve()` に渡り "completed" として数えられていた。

def test_single_shot_fallback_marks_unclassified_stream_exception_as_error():
    from sherpa.providers.base import Ctx, _GenProvider

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"

        def _stream(self, prompt, completion=None):
            raise ValueError("型を特定できないストリーム例外")
            yield ""   # pragma: no cover - ジェネレータにするためのダミー yield（到達しない）

    p = _P()
    ctx = Ctx(message="こんにちは", world="v1", knowledge=True,
              route=lambda m: {"lens": "author", "reason": "t", "input": m},
              dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
              make_sources=lambda docs: [{"doc_id": d} for d in docs])
    events = list(p.run(ctx))
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert env["agentic_failure"] == "error", (
        f"型を特定できない例外が無印のまま completed に落ちている: {env!r}")
    assert stop_kind.resolve(env) is None


def test_from_exception_follows_one_level_of_cause():
    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as e:
            raise RuntimeError("hybrid synthesis produced no answer") from e
    except RuntimeError as wrapped:
        assert stop_kind.from_exception(wrapped) == "timeout"
    # 型を特定できない原因は None のまま
    try:
        try:
            raise ValueError("x")
        except ValueError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as wrapped:
        assert stop_kind.from_exception(wrapped) is None
