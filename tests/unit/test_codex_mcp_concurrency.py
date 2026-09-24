"""Codex 実行中に MCP `mcp_tool_call` が何本同時に in-flight だったか（`max_in_flight`）と
総数（`total`）を1行のログに出す契約の検証。実 codex は使わず、PATH に偽 codex 実行ファイルを
差し込む（`tests/unit/test_codex_resume.py`・`tests/unit/test_codex_silent_output_failure.py`
と同じ流儀）。
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402


def _ctx(uid: str, message: str = "MCP 並走計測テスト") -> "A.Ctx":
    """DB 不要な最小 Ctx（conversation_id 無し＝per-request 使い捨て CODEX_HOME・resume 対象外）。"""
    return A.Ctx(
        message=message,
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True,
        uid=uid,
    )


def _setup(tmp_path: Path, monkeypatch, users_dirname: str = "users") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")   # 平文の偽 codex＝構造化応答は使わない
    return bin_dir


def _write_fake_codex(bin_dir: Path, script_body: str) -> None:
    script = bin_dir / "codex"
    script.write_text(script_body)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(prov, ctx) -> list:
    return list(prov.run(ctx))


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


def _emit(obj) -> str:
    return f"print({json.dumps(json.dumps(obj))})\n"


_CONCURRENT_SCRIPT = "#!/usr/bin/env python3\n" + "".join([
    _emit({"type": "item.started", "item": {"id": "A", "type": "mcp_tool_call",
                                             "tool": "read_around", "status": "in_progress"}}),
    _emit({"type": "item.started", "item": {"id": "B", "type": "mcp_tool_call",
                                             "tool": "ripgrep_search", "status": "in_progress"}}),
    _emit({"type": "item.completed", "item": {"id": "A", "type": "mcp_tool_call",
                                               "tool": "read_around", "status": "completed"}}),
    _emit({"type": "item.completed", "item": {"id": "B", "type": "mcp_tool_call",
                                               "tool": "ripgrep_search", "status": "completed"}}),
    _emit({"type": "item.completed", "item": {"id": "3", "type": "agent_message",
                                               "text": "concurrent-mcp-done"}}),
])

_SEQUENTIAL_SCRIPT = "#!/usr/bin/env python3\n" + "".join([
    _emit({"type": "item.started", "item": {"id": "A", "type": "mcp_tool_call",
                                             "tool": "read_around", "status": "in_progress"}}),
    _emit({"type": "item.completed", "item": {"id": "A", "type": "mcp_tool_call",
                                               "tool": "read_around", "status": "completed"}}),
    _emit({"type": "item.started", "item": {"id": "B", "type": "mcp_tool_call",
                                             "tool": "ripgrep_search", "status": "in_progress"}}),
    _emit({"type": "item.completed", "item": {"id": "B", "type": "mcp_tool_call",
                                               "tool": "ripgrep_search", "status": "completed"}}),
    _emit({"type": "item.completed", "item": {"id": "3", "type": "agent_message",
                                               "text": "sequential-mcp-done"}}),
])

_ASK_USER_SCRIPT = "#!/usr/bin/env python3\n" + _emit(
    {"type": "item.completed", "item": {"id": "1", "type": "mcp_tool_call", "tool": "ask_user",
                                        "status": "completed",
                                        "arguments": {"prompt": "確認してください"}}})


def _mcp_call_log_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("codex mcp calls:")]


def test_concurrent_mcp_tool_calls_log_max_in_flight_two(tmp_path, monkeypatch, caplog):
    """id=A/B が両方 started の後に completed する（A 完了前に B が始まる）と max_in_flight=2。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_concurrent")
    _write_fake_codex(bin_dir, _CONCURRENT_SCRIPT)
    prov = A.CodexProvider()
    ctx = _ctx(uid="mcpcnt-concurrent")

    with caplog.at_level(logging.INFO):
        events = _run(prov, ctx)
    env = _result_env(events)
    assert env["headline"] == "concurrent-mcp-done"

    msgs = _mcp_call_log_messages(caplog)
    assert len(msgs) == 1, f"計測ログが1行のはず: {caplog.records!r}"
    assert "total=2" in msgs[0] and "max_in_flight=2" in msgs[0], msgs[0]


def test_sequential_mcp_tool_calls_log_max_in_flight_one(tmp_path, monkeypatch, caplog):
    """id=A が完了してから id=B が始まる（重ならない）と max_in_flight=1（total は変わらず2）。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_sequential")
    _write_fake_codex(bin_dir, _SEQUENTIAL_SCRIPT)
    prov = A.CodexProvider()
    ctx = _ctx(uid="mcpcnt-sequential")

    with caplog.at_level(logging.INFO):
        events = _run(prov, ctx)
    env = _result_env(events)
    assert env["headline"] == "sequential-mcp-done"

    msgs = _mcp_call_log_messages(caplog)
    assert len(msgs) == 1
    assert "total=2" in msgs[0] and "max_in_flight=1" in msgs[0], msgs[0]


def test_ask_user_early_return_still_logs_exactly_one_line(tmp_path, monkeypatch, caplog):
    """codex_question 早期 return（ask_user）でも計測ログは1行出る（「1実行あたり1行」を保つ）。
    ログはサブプロセス後始末の直後・早期 return より前の共通位置にあるため、質問ターンでも到達する。
    偽 codex は id="1" の mcp_tool_call を completed のみ（started 無し）で1件返すので
    total=1・max_in_flight=0（in-flight に入らないまま完了扱い）になる。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_ask_user")
    _write_fake_codex(bin_dir, _ASK_USER_SCRIPT)
    prov = A.CodexProvider()
    ctx = _ctx(uid="mcpcnt-askuser")

    with caplog.at_level(logging.INFO):
        events = _run(prov, ctx)

    questions = [e for e in events if isinstance(e, dict) and e.get("type") == "question"]
    assert len(questions) == 1, f"ask_user から question イベントが1件出るはず: {events!r}"
    assert [e for e in events if isinstance(e, dict) and e.get("type") == "_result"] == []

    msgs = _mcp_call_log_messages(caplog)
    assert len(msgs) == 1, f"ask_user ターンでも計測ログは1行出るはず: {caplog.records!r}"
    assert "total=1" in msgs[0] and "max_in_flight=0" in msgs[0], msgs[0]


# ===== 自動継続（`_CONTINUE_PROMPT`）をまたぐ id 再利用 =====
# Codex CLI は codex exec プロセスごとに item id を採番し直す（例: 両方の attempt が "item_0" を
# 使う）。attempt をまたいで id 集合を共有すると、2回目の "item_0" を「1回目と同じ呼び出し」と
# 誤認して total を過少計上する——ここでは同じ id が2つの attempt に登場するケースを固定する。

def _write_fake_codex_multi(bin_dir: Path, argv_log: Path, plan_path: Path, plan: dict) -> None:
    """`tests/unit/test_codex_auto_continue.py` と同じ流儀（呼び出し回数ごとの応答計画を JSON
    ファイルで渡し、argv_log の行数で「今何回目の起動か」を数える）。本ファイル専用の偽 codex
    （cross-file import はしない）は生イベント列（`events`）をそのまま再生するだけの薄い形にする。
    """
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    script = bin_dir / "codex"
    script.write_text(
        _FAKE_CODEX_MULTI_PY.replace("__ARGV_LOG__", str(argv_log)).replace("__PLAN_PATH__", str(plan_path)))
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


_FAKE_CODEX_MULTI_PY = r'''#!/usr/bin/env python3
import json
import pathlib
import sys

argv_log = pathlib.Path("__ARGV_LOG__")
plan_path = pathlib.Path("__PLAN_PATH__")
args = sys.argv[1:]
with argv_log.open("a", encoding="utf-8") as f:
    f.write(repr(args) + "\n")
    f.flush()

call_index = len(argv_log.read_text(encoding="utf-8").splitlines())
steps = json.loads(plan_path.read_text(encoding="utf-8"))["steps"]
step = steps[call_index - 1] if call_index - 1 < len(steps) else steps[-1]

for obj in step.get("events", []):
    print(json.dumps(obj))
    sys.stdout.flush()

sys.exit(step.get("exit_code", 0))
'''


def _read_argv_log(argv_log: Path) -> list:
    if not argv_log.exists():
        return []
    return [eval(line) for line in argv_log.read_text().splitlines() if line.strip()]


def test_same_item_id_across_auto_continue_attempts_counts_total_two(tmp_path, monkeypatch, caplog):
    """初回 attempt と自動継続 attempt（別プロセス＝id 採番がリセットされる）が同じ id "item_0" を
    使っても、2つの別々の mcp_tool_call として total=2 に数える（1 に過少計上しない）。
    双方とも「started してから completed」なので、同時に in-flight にはならず max_in_flight=1。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    plan_path = tmp_path / "plan.json"
    plan = {"steps": [
        {"events": [
            {"type": "thread.started", "thread_id": "TH-MCPCONT"},
            {"type": "item.started", "item": {"id": "item_0", "type": "mcp_tool_call",
                                               "tool": "read_around", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "item_0", "type": "mcp_tool_call",
                                                 "tool": "read_around", "status": "completed"}},
            {"type": "item.completed", "item": {"id": "m0", "type": "agent_message",
                                                 "text": "まず資料を確認します。"}},
        ]},
        {"events": [
            {"type": "thread.started", "thread_id": "TH-MCPCONT"},
            {"type": "item.started", "item": {"id": "item_0", "type": "mcp_tool_call",
                                               "tool": "ripgrep_search", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "item_0", "type": "mcp_tool_call",
                                                 "tool": "ripgrep_search", "status": "completed"}},
            {"type": "item.completed", "item": {"id": "m1", "type": "agent_message",
                                                 "text": "確認した結果、影響はありません。"}},
        ]},
    ]}
    _write_fake_codex_multi(bin_dir, argv_log, plan_path, plan)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users_mcp_continue"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")   # 平文の偽 codex＝構造化応答は使わない
    prov = A.CodexProvider()
    ctx = A.Ctx(
        message="自動継続 id 再利用テスト", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid="mcpcnt-continue", conversation_id=909)

    with caplog.at_level(logging.INFO):
        events = _run(prov, ctx)
    env = _result_env(events)
    assert env["headline"] == "確認した結果、影響はありません。"   # 継続が実際に起きたことの傍証
    assert len(_read_argv_log(argv_log)) == 2, "初回＋継続1回で2プロセスのはず"

    msgs = _mcp_call_log_messages(caplog)
    assert len(msgs) == 1, f"計測ログが1行のはず: {caplog.records!r}"
    assert "total=2" in msgs[0], f"attempt をまたぐ id 再利用で過少計上している: {msgs[0]}"
    assert "max_in_flight=1" in msgs[0], msgs[0]
