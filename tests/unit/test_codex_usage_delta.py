"""Codex の resume ターンの usage をターン差分にする（`CodexProvider._run_authoring` の usage 集計）。

背景（提案書 docs/proposals/2026-09-07-Codex途中経過で止まる.md §3.5）: `turn.completed.usage` は
セッション累計（`last_total_token_usage.total`）で、`codex exec resume` は前回までの累計を復元して
から加算する。R1b の resume を使う2ターン目以降は、そのターンの `answer.usage` にセッション累計が
乗り、ターンごとの利用統計が過大計上される。`CodexProvider` は resume が効いた（フォールバックして
いない）ターンだけ、前ターンの累計（`ctx.codex_usage_prev_total`）との差分を `env["usage"]` にする。
`env["codex_usage_total"]` には常に今回の累計を残し、次ターンの差分計算の元にする。

既存 tests/unit/test_codex_auto_continue.py と同じ「呼び出し回数ごとの応答計画を JSON ファイルで
渡す偽 codex」の流儀を使う（実 codex は一切呼ばない。他ファイルの固定応答スクリプトは再利用しない
方針のため、本ファイル専用のコピーを持つ）。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

# test_codex_resume.py / test_codex_auto_continue.py と同じ流儀（setdefault のみ・モジュール
# レベル直書きは pytest 一括収集時にプロセス全体へ漏れるため禁止）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402

# ===== 偽 codex（呼び出し回数ごとに応答を切り替える・応答計画は JSON ファイルで渡す）=====
# tests/unit/test_codex_auto_continue.py の _FAKE_CODEX_MULTI_PY と同じ流儀（本ファイル専用コピー）。
_FAKE_CODEX_MULTI_PY = r'''#!/usr/bin/env python3
import json
import pathlib
import sys
import time

argv_log = pathlib.Path(r"__ARGV_LOG__")
plan_path = pathlib.Path(r"__PLAN_PATH__")
args = sys.argv[1:]
with argv_log.open("a", encoding="utf-8") as f:
    f.write(repr(args) + "\n")
    f.flush()

call_index = len(argv_log.read_text(encoding="utf-8").splitlines())
steps = json.loads(plan_path.read_text(encoding="utf-8"))["steps"]
step = steps[call_index - 1] if call_index - 1 < len(steps) else steps[-1]

tid = step.get("thread_id")
if tid:
    print(json.dumps({"type": "thread.started", "thread_id": tid}))
    sys.stdout.flush()

for i, text in enumerate(step.get("agent_messages", [])):
    print(json.dumps({"type": "item.completed",
                       "item": {"id": f"m{i}", "type": "agent_message", "text": text}}))
    sys.stdout.flush()

sleep_s = step.get("sleep")
if sleep_s:
    time.sleep(sleep_s)   # 自然終了させない（timeout kill を待つ）
    sys.exit(step.get("exit_code", 1))

usage = step.get("usage")
if usage:
    print(json.dumps({"type": "turn.completed", "usage": usage}))
    sys.stdout.flush()

sys.exit(step.get("exit_code", 0))
'''


def _write_fake_codex(bin_dir: Path, argv_log: Path, plan_path: Path) -> None:
    script = bin_dir / "codex"
    script.write_text(
        _FAKE_CODEX_MULTI_PY.replace("__ARGV_LOG__", str(argv_log))
                            .replace("__PLAN_PATH__", str(plan_path)))
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path: Path, monkeypatch, steps: list, users_dirname: str) -> Path:
    """偽 codex を PATH に差し込み、呼び出しごとの応答計画（steps）を JSON で渡す。戻り値は argv_log。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    _write_fake_codex(bin_dir, argv_log, plan_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    # 平文の偽 codex で usage 差分の契約を見るテスト＝出力スキーマ（構造化応答）は使わない。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    return argv_log


def _ctx(uid: str, conversation_id, codex_session_id=None, codex_usage_prev_total=None,
        message: str = "usage 差分テスト", scope_meta=None) -> "A.Ctx":
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
        conversation_id=conversation_id,
        codex_session_id=codex_session_id,
        codex_usage_prev_total=codex_usage_prev_total,
        scope_meta=scope_meta,
    )


def _run(prov, ctx) -> list:
    return list(prov.run(ctx))


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


def _read_argv_log(argv_log: Path) -> list:
    if not argv_log.exists():
        return []
    return [eval(line) for line in argv_log.read_text().splitlines() if line.strip()]


def _usage(input_tokens=10, cached_input_tokens=0, output_tokens=5, reasoning_output_tokens=0) -> dict:
    return {"input_tokens": input_tokens, "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens, "reasoning_output_tokens": reasoning_output_tokens}


def _prev_total(session_id, input_tokens=10, cached_input_tokens=2, output_tokens=5,
               reasoning_output_tokens=1) -> dict:
    """前ターンが記録した `env["codex_usage_total"]`（`ctx.codex_usage_prev_total` の形）。"""
    return {"session_id": session_id, "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens, "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens}


# ===== (a) resume 成功・prev あり（session_id 一致）→ ターン差分 =====

def test_resume_success_with_matching_prev_total_uses_delta(tmp_path, monkeypatch):
    steps = [{"thread_id": "SID-1", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage(30, 2, 13, 3)}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_ok")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-ok", conversation_id=701, codex_session_id="SID-1",
              codex_usage_prev_total=_prev_total("SID-1"))

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"resume 成功時は1回だけのはず: {calls!r}"
    assert "resume" in calls[0] and "SID-1" in calls[0]

    assert env["codex_usage_total"] == {
        "session_id": "SID-1", "input_tokens": 30, "cached_input_tokens": 2,
        "output_tokens": 13, "reasoning_output_tokens": 3}, "累計は差分化せずそのまま載るはず"
    usage = env["usage"]
    assert usage["provider"] == "codex"
    assert usage["input_tokens"] == 20              # max(0, 30-10)
    assert usage["cached_input_tokens"] == 0        # max(0, 2-2)
    assert usage["output_tokens"] == 8              # max(0, 13-5)
    assert usage["reasoning_output_tokens"] == 2    # max(0, 3-1)


# ===== (b) 新規セッション（prev None）→ usage＝累計・total 載る =====

def test_fresh_session_without_prev_total_uses_raw_accumulated_usage(tmp_path, monkeypatch):
    steps = [{"thread_id": "SID-NEW", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage(30, 2, 13, 3)}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_fresh")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-fresh", conversation_id=702, codex_session_id=None,
              codex_usage_prev_total=None)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1
    assert "resume" not in calls[0], "resume 先が無いのに resume 引数が付いている"

    assert env["codex_usage_total"] == {
        "session_id": "SID-NEW", "input_tokens": 30, "cached_input_tokens": 2,
        "output_tokens": 13, "reasoning_output_tokens": 3}
    assert env["usage"]["input_tokens"] == 30, "新規セッションは累計そのまま（差分にしない）"
    assert env["usage"]["output_tokens"] == 13


# ===== STAT-3 S1（利用統計の拡充）: env["usage"] へ depth_profile/reasoning を足す =====

def test_deep_depth_profile_overrides_reasoning_to_high_in_usage(tmp_path, monkeypatch):
    """`scope_meta["depth_profile"]="deep"` は `model_reasoning_effort` を "high" へ per-turn 上書き
    （`depth_profile.codex_reasoning_for`）し、その実際に渡した値が `env["usage"]["reasoning"]` に
    載る。基準値（既定 "low"）と異なるため `reasoning_base` も残る。"""
    steps = [{"thread_id": "SID-DEEP", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage(30, 2, 13, 3)}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_deep")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-deep", conversation_id=703, scope_meta={"depth_profile": "deep"})

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert any("model_reasoning_effort=high" in a for a in calls[0]), \
        f"実際に codex exec へ渡した引数に high が無い: {calls[0]!r}"
    assert env["usage"]["depth_profile"] == "deep"
    assert env["usage"]["reasoning"] == "high"
    assert env["usage"]["reasoning_base"] == "low"


def test_standard_depth_profile_omits_reasoning_base_when_unchanged(tmp_path, monkeypatch):
    """標準プロファイルは基準値のまま上書きしない＝`reasoning_base` は冗長なため省略する。"""
    steps = [{"thread_id": "SID-STD", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage(30, 2, 13, 3)}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_standard")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-standard", conversation_id=704, scope_meta={"depth_profile": "standard"})

    env = _result_env(_run(prov, ctx))

    assert env["usage"]["depth_profile"] == "standard"
    assert env["usage"]["reasoning"] == "low"
    assert "reasoning_base" not in env["usage"]


# ===== (c) resume 失敗→フォールバック新規セッション→ prev があっても差分にしない =====

def test_resume_fallback_ignores_prev_total_even_when_present(tmp_path, monkeypatch):
    steps = [
        {"exit_code": 1},   # resume 試行: 無出力・exit 1 ＝ resume 失敗（フォールバックを誘発）
        {"thread_id": "SID-FALLBACK", "agent_messages": ["確認した結果、影響はありません。"],
         "usage": _usage(30, 2, 13, 3)},   # フォールバック（resume 無しの新規セッション）
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_fallback")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-fallback", conversation_id=703, codex_session_id="SID-OLD",
              codex_usage_prev_total=_prev_total("SID-OLD"))

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"resume 失敗→フォールバックで2回呼ばれるはず: {calls!r}"
    assert "resume" in calls[0] and "SID-OLD" in calls[0]
    assert "resume" not in calls[1]

    assert env["codex_usage_total"]["session_id"] == "SID-FALLBACK"
    assert env["usage"]["input_tokens"] == 30, "フォールバック後は prev があっても累計そのまま"
    assert env["usage"]["output_tokens"] == 13


# ===== (d) prev の session_id が不一致 → 差分にしない =====

def test_prev_total_session_id_mismatch_uses_raw_accumulated_usage(tmp_path, monkeypatch):
    steps = [{"thread_id": "SID-2", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage(30, 2, 13, 3)}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_mismatch")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-mismatch", conversation_id=704, codex_session_id="SID-2",
              codex_usage_prev_total=_prev_total("SID-DIFFERENT"))

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1
    assert "resume" in calls[0] and "SID-2" in calls[0]

    assert env["usage"]["input_tokens"] == 30, "prev の session_id 不一致は差分にしない"
    assert env["usage"]["output_tokens"] == 13


# ===== (e) 自動継続 2 attempt → 最新 snapshot を基に差分 =====

def test_auto_continue_uses_latest_snapshot_for_delta(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "SID-3", "agent_messages": ["まず資料を確認します。"], "usage": _usage(10, 2, 5, 1)},
        # 継続（resume）側の usage はセッション累計＝1回目の分を含む値が来る（Codex CLI の契約）。
        {"thread_id": "SID-3", "agent_messages": ["確認した結果、影響はありません。"],
         "usage": _usage(30, 2, 13, 3)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_continue")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-continue", conversation_id=705, codex_session_id="SID-3",
              codex_usage_prev_total=_prev_total("SID-3"))

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"作業宣言→結論で継続2回のはず: {calls!r}"
    assert "resume" in calls[0] and "SID-3" in calls[0]
    assert "resume" in calls[1] and "SID-3" in calls[1]
    assert calls[1][-1] == A._CONTINUE_PROMPT

    assert env["codex_usage_total"] == {
        "session_id": "SID-3", "input_tokens": 30, "cached_input_tokens": 2,
        "output_tokens": 13, "reasoning_output_tokens": 3}, "累計は最新 snapshot（足し合わせない）"
    usage = env["usage"]
    assert usage["input_tokens"] == 20      # 最新 snapshot(30) - prev(10)。1回目の分(10)を足さない
    assert usage["cached_input_tokens"] == 0
    assert usage["output_tokens"] == 8      # 13 - 5
    assert usage["reasoning_output_tokens"] == 2   # 3 - 1
