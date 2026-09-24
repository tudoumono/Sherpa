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
import logging
import os
import stat
import time
from pathlib import Path

# test_codex_resume.py / test_codex_auto_continue.py と同じ流儀（setdefault のみ・モジュール
# レベル直書きは pytest 一括収集時にプロセス全体へ漏れるため禁止）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402

# ===== 偽 codex（呼び出し回数ごとに応答を切り替える・応答計画は JSON ファイルで渡す）=====
# tests/unit/test_codex_auto_continue.py の _FAKE_CODEX_MULTI_PY と同じ流儀（本ファイル専用コピー）。
_FAKE_CODEX_MULTI_PY = r'''#!/usr/bin/env python3
import json
import os
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

# DEPTH-2 S3b: 子スレッドの session JSONL を CODEX_HOME/sessions 配下に書く（`_collect_child_token_usage`
# が glob する実際のレイアウトを最小限で再現）。`spawn_agent` の collab_tool_call item も出す
# （旧形式）。新形式（`spawn_agent` item が無く CLI が `wait` の collab_tool_call だけを返す・
# 実機 0.153.4 で確認済み）を模すときは `collab_spawns` を渡さず `wait_calls` だけを渡す。
for spawn in step.get("collab_spawns", []):
    print(json.dumps({"type": "item.completed", "item": {
        "id": spawn.get("id", "collab"), "type": "collab_tool_call", "tool": "spawn_agent",
        "receiver_thread_ids": spawn.get("receiver_thread_ids", [])}}))
    sys.stdout.flush()

for i in range(step.get("wait_calls", 0)):
    print(json.dumps({"type": "item.completed", "item": {
        "id": f"wait{i}", "type": "collab_tool_call", "tool": "wait", "receiver_thread_ids": []}}))
    sys.stdout.flush()

for child in step.get("child_sessions", []):
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        sdir = pathlib.Path(codex_home) / "sessions" / "2026" / "01" / "01"
        sdir.mkdir(parents=True, exist_ok=True)
        # 実機の session_meta 構造（新形式の子判定に使うフィールドのみ）: `parent_thread_id`/
        # `thread_source` はテストが明示的に渡したときだけ載せる（旧形式ケースは省いたまま）。
        meta_payload = {"id": child["thread_id"]}
        if "parent_thread_id" in child:
            meta_payload["parent_thread_id"] = child["parent_thread_id"]
        if "thread_source" in child:
            meta_payload["thread_source"] = child["thread_source"]
        lines = [json.dumps({"payload": meta_payload})]
        # "usage" を渡さない子＝走行中／usage を書く前に落ちた rollout の再現（session_meta は
        # あるが token_count イベントがまだ無い＝検出はできるが usage は読めない）。
        if "usage" in child:
            lines.append(json.dumps({"payload": {"type": "token_count",
                                                  "info": {"total_token_usage": child["usage"]}}}))
        (sdir / f"rollout-{child['thread_id']}.jsonl").write_text("\n".join(lines) + "\n")

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

def test_deep_depth_profile_keeps_configured_reasoning_in_usage(tmp_path, monkeypatch):
    """推論レベルは深さで変えない——「深く」でも `model_reasoning_effort` は基準値（既定 "low"）の
    まま渡り、`env["usage"]["reasoning"]` もその値になる（上書きが無いので `reasoning_base` は
    付かない）。"""
    steps = [{"thread_id": "SID-DEEP", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage(30, 2, 13, 3)}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_delta_deep")
    prov = A.CodexProvider()
    ctx = _ctx(uid="delta-deep", conversation_id=703, scope_meta={"depth_profile": "deep"})

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert any("model_reasoning_effort=low" in a for a in calls[0]), \
        f"実際に codex exec へ渡した引数が基準値 low のままになっていない: {calls[0]!r}"
    assert env["usage"]["depth_profile"] == "deep"
    assert env["usage"]["reasoning"] == "low"
    assert "reasoning_base" not in env["usage"]


def test_standard_depth_profile_omits_reasoning_base_when_unchanged(tmp_path, monkeypatch):
    """基準値のまま渡るターンは `reasoning_base` を冗長なため省略する。"""
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


# ===== DEPTH-2 S3b: 子スレッド（spawn_agent）の usage 合算 =====
# `docs/proposals/2026-09-17-深さの再定義とレビュー巡.md` §2.6/§9.1・受け入れ条件(4)。

def test_child_thread_usage_merged_into_answer_usage_with_breakdown(tmp_path, monkeypatch):
    """親の `turn.completed.usage` に、同ターン中に spawn_agent された子2本の session JSONL
    （token_count の total_token_usage）を合算する。`env["usage"]` は親＋子の合計になり、内訳
    （親／子／未取得件数）が別途残る。`env["codex_usage_total"]`（次ターンの差分計算の元）は
    子の分を混ぜず親のスナップショットのままにする。"""
    steps = [{
        "thread_id": "SID-PARENT",
        "collab_spawns": [
            {"id": "c1", "receiver_thread_ids": ["CHILD-1"]},
            {"id": "c2", "receiver_thread_ids": ["CHILD-2"]},
        ],
        "child_sessions": [
            {"thread_id": "CHILD-1",
             "usage": {"input_tokens": 100, "cached_input_tokens": 0,
                       "output_tokens": 20, "reasoning_output_tokens": 5}},
            {"thread_id": "CHILD-2",
             "usage": {"input_tokens": 50, "cached_input_tokens": 0,
                       "output_tokens": 10, "reasoning_output_tokens": 2}},
        ],
        "agent_messages": ["確認した結果、影響はありません。"],
        "usage": _usage(30, 2, 13, 3),
    }]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_child_usage")
    prov = A.CodexProvider()
    ctx = _ctx(uid="child-usage-u1", conversation_id=801)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1

    # 既存の差分計算の元＝親のみのスナップショットのまま（壊れていない）。
    assert env["codex_usage_total"] == {
        "session_id": "SID-PARENT", "input_tokens": 30, "cached_input_tokens": 2,
        "output_tokens": 13, "reasoning_output_tokens": 3}

    assert env["codex_usage_children"] == {
        "found": 2, "missing": 0,
        "input_tokens": 150, "cached_input_tokens": 0,
        "output_tokens": 30, "reasoning_output_tokens": 7,
    }
    usage = env["usage"]
    # 親(30/2/13/3) + 子(150/0/30/7) を一度だけ合算。
    assert usage["input_tokens"] == 180
    assert usage["cached_input_tokens"] == 2
    assert usage["output_tokens"] == 43
    assert usage["reasoning_output_tokens"] == 10
    assert usage["codex_usage_breakdown"] == {
        "parent": {"input_tokens": 30, "cached_input_tokens": 2,
                   "output_tokens": 13, "reasoning_output_tokens": 3},
        "children": {"input_tokens": 150, "cached_input_tokens": 0,
                     "output_tokens": 30, "reasoning_output_tokens": 7},
        "children_found": 2, "children_missing": 0,
    }


def test_child_thread_usage_missing_child_counted_without_estimation(tmp_path, monkeypatch):
    """spawn_agent された子の session JSONL が見つからない（未取得）ときは、推定で埋めず件数だけ
    `missing` に残す。見つかった分だけ合算する。"""
    steps = [{
        "thread_id": "SID-PARENT-2",
        "collab_spawns": [
            {"id": "c1", "receiver_thread_ids": ["CHILD-FOUND"]},
            {"id": "c2", "receiver_thread_ids": ["CHILD-LOST"]},   # session JSONL を書かない＝未取得
        ],
        "child_sessions": [
            {"thread_id": "CHILD-FOUND",
             "usage": {"input_tokens": 40, "cached_input_tokens": 0,
                       "output_tokens": 8, "reasoning_output_tokens": 1}},
        ],
        "agent_messages": ["確認した結果、影響はありません。"],
        "usage": _usage(30, 2, 13, 3),
    }]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_child_usage_missing")
    prov = A.CodexProvider()
    ctx = _ctx(uid="child-usage-u2", conversation_id=802)

    env = _result_env(_run(prov, ctx))

    assert env["codex_usage_children"] == {
        "found": 1, "missing": 1,
        "input_tokens": 40, "cached_input_tokens": 0,
        "output_tokens": 8, "reasoning_output_tokens": 1,
    }
    usage = env["usage"]
    assert usage["input_tokens"] == 70    # 30(親) + 40(見つかった子だけ)
    assert usage["output_tokens"] == 21   # 13 + 8


def test_no_child_spawn_keeps_usage_unchanged(tmp_path, monkeypatch):
    """`collab_tool_call` が一度も出ない（multi_agent 無効の現状の通常実行）ときは
    `codex_usage_children`/`codex_usage_breakdown` が一切出ない＝既存の usage 計上のまま。"""
    steps = [{"thread_id": "SID-NOCHILD", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage(30, 2, 13, 3)}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_no_child")
    prov = A.CodexProvider()
    ctx = _ctx(uid="no-child-u1", conversation_id=803)

    env = _result_env(_run(prov, ctx))

    assert "codex_usage_children" not in env
    assert "codex_usage_breakdown" not in env["usage"]
    assert env["usage"]["input_tokens"] == 30


def test_child_thread_usage_breakdown_on_resume_uses_delta_not_cumulative(tmp_path, monkeypatch):
    """C11 是正: resume ターン（prev_total あり・session_id 一致）で子スレッドが合算されるとき、
    内訳の「親」分は `codex_usage`（セッション累計・前ターン分を含む）ではなく、既に
    差分化済みの `env["usage"]`（このターンの計上値）から作る。累計をそのまま使うと、前ターンの
    分が混入して親分が「合計 usage － 子分」より過大になる（提案書 §2.6/§9.1・RV C11）。"""
    steps = [{
        "thread_id": "SID-RESUME-CHILD",
        "collab_spawns": [{"id": "c1", "receiver_thread_ids": ["CHILD-1"]}],
        "child_sessions": [
            {"thread_id": "CHILD-1",
             "usage": {"input_tokens": 40, "cached_input_tokens": 0,
                       "output_tokens": 8, "reasoning_output_tokens": 1}},
        ],
        "agent_messages": ["確認した結果、影響はありません。"],
        "usage": _usage(130, 2, 13, 3),   # セッション累計（前ターン分 100 を含む）
    }]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_resume_child_usage")
    prov = A.CodexProvider()
    ctx = _ctx(uid="resume-child-usage-u1", conversation_id=804,
              codex_session_id="SID-RESUME-CHILD",
              codex_usage_prev_total=_prev_total("SID-RESUME-CHILD", input_tokens=100,
                                                  cached_input_tokens=2, output_tokens=5,
                                                  reasoning_output_tokens=1))

    env = _result_env(_run(prov, ctx))

    # このターンの親分の差分計上値（130-100=30・累計 130 をそのまま使うと過大になる）。
    usage = env["usage"]
    assert usage["input_tokens"] == 70     # 親差分(30) + 子(40)
    assert usage["output_tokens"] == 16    # 親差分(8) + 子(8)
    assert usage["codex_usage_breakdown"]["parent"] == {
        "input_tokens": 30, "cached_input_tokens": 0,
        "output_tokens": 8, "reasoning_output_tokens": 2}, \
        "内訳の親分が累計のまま（前ターン分が混入している）"
    assert usage["codex_usage_breakdown"]["children"] == {
        "input_tokens": 40, "cached_input_tokens": 0,
        "output_tokens": 8, "reasoning_output_tokens": 1}


# ===== fix/child-count-rollout: `--json` に `spawn_agent` item が出ない CLI（実機 0.153.4・
# 2026-09-21 に確認）でも rollout の parent_thread_id 突合で子を数える =====
# `_codex_log_lines` は下の「予算天井の窓連動」節で定義（モジュール読込後に解決されるため
# 定義順は問わない・既存の `test_end_log_shows_parent_and_child_token_breakdown` と同じ流儀）。

def test_new_format_child_detection_via_rollout_parent_thread_id(tmp_path, monkeypatch, caplog):
    """`spawn_agent` の collab_tool_call item が一切出ない（`wait` だけ・`_child_thread_ids` が
    空のまま）ターンでも、子 rollout の先頭行 `parent_thread_id`/`thread_source` が親の thread id
    と一致すれば子として検出し、usage を合算する。codex.log 終了行の `spawn_agents` も
    `_child_thread_ids` の要素数（0）ではなく実際に見つけた子の数（=children_found）になる。"""
    steps = [{
        "thread_id": "SID-NEWFMT-1",
        "wait_calls": 2,
        "child_sessions": [
            {"thread_id": "CHILD-NF-1", "parent_thread_id": "SID-NEWFMT-1",
             "thread_source": "subagent",
             "usage": {"input_tokens": 60, "cached_input_tokens": 0,
                       "output_tokens": 12, "reasoning_output_tokens": 2}},
            {"thread_id": "CHILD-NF-2", "parent_thread_id": "SID-NEWFMT-1",
             "thread_source": "subagent",
             "usage": {"input_tokens": 40, "cached_input_tokens": 0,
                       "output_tokens": 8, "reasoning_output_tokens": 1}},
        ],
        "agent_messages": ["確認した結果、影響はありません。"],
        "usage": _usage(30, 2, 13, 3),
    }]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_newfmt_child")
    prov = A.CodexProvider(system_settings={})
    ctx = _ctx(uid="newfmt-child-u1", conversation_id=1101)

    with caplog.at_level(logging.INFO):
        env = _result_env(_run(prov, ctx))

    assert env["codex_usage_children"] == {
        "found": 2, "missing": 0,
        "input_tokens": 100, "cached_input_tokens": 0,
        "output_tokens": 20, "reasoning_output_tokens": 3,
    }
    usage = env["usage"]
    assert usage["input_tokens"] == 130   # 30(親) + 100(子)
    assert usage["output_tokens"] == 33   # 13 + 20

    ends = _codex_log_lines(caplog, "end ")
    assert len(ends) == 1, ends
    line = ends[0]
    assert "spawn_agents=2" in line, line
    assert "children_found=2" in line, line
    assert "children_missing=0" in line, line


def test_new_format_child_detected_without_usage_counts_as_spawned_not_missing_entirely(
        tmp_path, monkeypatch, caplog):
    """RV是正: 「起動を検出した」ことと「usage を読めた」ことを混同しない。子がまだ走行中、または
    usage を書く前に落ちた rollout（`session_meta` は新形式の条件（parent_thread_id/thread_source/
    mtime）を満たすが `token_count` イベントがまだ無い）は、起動自体は確認できる——
    `spawn_agents`（検出数）は1のまま、`children_found`（usage を合算できた数）だけ0になり
    `children_missing`（起動は確認できたが usage 未取得）が1になる。found=0 のとき spawn_agents
    まで0にしてしまうと「起動していない」と区別が付かなくなる（是正前の退行）。"""
    steps = [{
        "thread_id": "SID-NEWFMT-NOUSAGE",
        "wait_calls": 1,
        "child_sessions": [
            {"thread_id": "CHILD-NOUSAGE-1", "parent_thread_id": "SID-NEWFMT-NOUSAGE",
             "thread_source": "subagent"},   # "usage" を渡さない＝token_count イベントが無い rollout
        ],
        "agent_messages": ["確認した結果、影響はありません。"],
        "usage": _usage(30, 2, 13, 3),
    }]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_newfmt_nousage")
    prov = A.CodexProvider(system_settings={})
    ctx = _ctx(uid="newfmt-nousage-u1", conversation_id=1104)

    with caplog.at_level(logging.INFO):
        env = _result_env(_run(prov, ctx))

    assert env["codex_usage_children"] == {
        "found": 0, "missing": 1,
        "input_tokens": 0, "cached_input_tokens": 0,
        "output_tokens": 0, "reasoning_output_tokens": 0,
    }
    # usage が取れないので親分のみ（子は加算されない）。
    assert env["usage"]["input_tokens"] == 30
    assert env["usage"]["output_tokens"] == 13

    ends = _codex_log_lines(caplog, "end ")
    assert len(ends) == 1, ends
    line = ends[0]
    assert "spawn_agents=1" in line, line   # 起動は検出できている（0 にしない）
    assert "children_found=0" in line, line
    assert "children_missing=1" in line, line


def test_new_format_and_old_format_children_are_unioned_not_duplicated(tmp_path, monkeypatch):
    """同じターンに旧形式（`spawn_agent` item・`receiver_thread_ids`）で捕捉した子と、新形式
    （rollout の `parent_thread_id` 突合）でしか見えない子が混在しても、和集合で数えて二重に
    数えない（同じ子 id が両方の条件に一致しても1回だけ合算する）。"""
    steps = [{
        "thread_id": "SID-NEWFMT-MIX",
        "collab_spawns": [{"id": "c1", "receiver_thread_ids": ["CHILD-MIX-OLD"]}],
        "child_sessions": [
            # 旧形式でも捕捉される子（spawn_agent item の receiver_thread_ids に載っている）。
            {"thread_id": "CHILD-MIX-OLD", "parent_thread_id": "SID-NEWFMT-MIX",
             "thread_source": "subagent",
             "usage": {"input_tokens": 60, "cached_input_tokens": 0,
                       "output_tokens": 12, "reasoning_output_tokens": 2}},
            # 新形式でしか見えない子（spawn_agent item には載らない）。
            {"thread_id": "CHILD-MIX-NEW", "parent_thread_id": "SID-NEWFMT-MIX",
             "thread_source": "subagent",
             "usage": {"input_tokens": 40, "cached_input_tokens": 0,
                       "output_tokens": 8, "reasoning_output_tokens": 1}},
        ],
        "agent_messages": ["確認した結果、影響はありません。"],
        "usage": _usage(30, 2, 13, 3),
    }]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_newfmt_mix")
    prov = A.CodexProvider()
    ctx = _ctx(uid="newfmt-mix-u1", conversation_id=1102)

    env = _result_env(_run(prov, ctx))

    assert env["codex_usage_children"] == {
        "found": 2, "missing": 0,
        "input_tokens": 100, "cached_input_tokens": 0,
        "output_tokens": 20, "reasoning_output_tokens": 3,
    }


def test_new_format_resumed_turn_does_not_recount_prior_turn_children(tmp_path, monkeypatch):
    """resume は同じ thread id を跨ターンで使い回す（`tests/unit/test_codex_resume.py` の
    `SID-GOOD` ケースと同じ、実機確認済みの契約）。新形式の判定（`parent_thread_id` 突合）だけで
    子を数えると、前ターンで見つけた子の rollout が消えずに残っている限り、子を1つも spawn して
    いない次ターンでも同じ子を再計上してしまう——`_collect_child_token_usage` の `min_mtime`
    （今ターン開始の壁時計）がこれを防ぐ（今ターンより前に書かれた rollout は新形式判定の対象外）。
    """
    steps = [
        {   # 1ターン目: 新形式の子2体を spawn
            "thread_id": "SID-NEWFMT-RESUME",
            "child_sessions": [
                {"thread_id": "CHILD-RS-1", "parent_thread_id": "SID-NEWFMT-RESUME",
                 "thread_source": "subagent",
                 "usage": {"input_tokens": 60, "cached_input_tokens": 0,
                           "output_tokens": 12, "reasoning_output_tokens": 2}},
                {"thread_id": "CHILD-RS-2", "parent_thread_id": "SID-NEWFMT-RESUME",
                 "thread_source": "subagent",
                 "usage": {"input_tokens": 40, "cached_input_tokens": 0,
                           "output_tokens": 8, "reasoning_output_tokens": 1}},
            ],
            "agent_messages": ["確認した結果、影響はありません。"],
            "usage": _usage(30, 2, 13, 3),
        },
        {   # 2ターン目: resume で同じ thread id・今回は子を1体も spawn していない
            "thread_id": "SID-NEWFMT-RESUME",
            "agent_messages": ["確認した結果、追加の影響はありません。"],
            "usage": _usage(50, 2, 20, 3),
        },
    ]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_newfmt_resume")
    prov = A.CodexProvider()
    uid, cid = "newfmt-resume-u1", 1103

    env1 = _result_env(_run(prov, _ctx(uid=uid, conversation_id=cid)))
    assert env1["codex_usage_children"]["found"] == 2, "1ターン目は子2体を数えられているはず"

    # 1ターン目の子 rollout を過去へ巻き戻す——同一テスト内は2ターンの実行が速く、ファイル
    # システムの mtime 分解能によっては「今ターンより前」が実時間だけでは保証できないため。
    codex_home = (tmp_path / "users_newfmt_resume" / uid / "workspace"
                 / ".codex-sessions" / str(cid))
    past = time.time() - 3600
    for p in codex_home.glob("sessions/**/*.jsonl"):
        os.utime(p, (past, past))

    ctx2 = _ctx(uid=uid, conversation_id=cid, codex_session_id="SID-NEWFMT-RESUME",
               codex_usage_prev_total=_prev_total("SID-NEWFMT-RESUME", input_tokens=30,
                                                   cached_input_tokens=2, output_tokens=13,
                                                   reasoning_output_tokens=3))
    env2 = _result_env(_run(prov, ctx2))

    assert "codex_usage_children" not in env2, \
        "前ターンの子 rollout を今ターンの子として再計上している（min_mtime が効いていない）"
    assert env2["usage"]["input_tokens"] == 20   # 50-30（親のみの差分・子の混入なし）
    assert env2["usage"]["output_tokens"] == 7   # 20-13


# ===== DEPTH-2 S6（§2.6）: multi_agent 既定有効化・review_rounds の受け渡し =====
# 提案書 2026-09-17-深さの再定義とレビュー巡.md §2.6・§5 S6・受け入れ条件(1)(3)。

def test_standard_depth_profile_passes_multi_agent_and_two_review_rounds_to_agents_md(tmp_path, monkeypatch):
    """既定（標準）は AGENTS.md 生成へ `multi_agent=True`・`review_rounds=2`
    （`depth_profile.review_rounds_for` の戻り値）をそのまま渡す。"""
    from sherpa import codex_agents_md
    from sherpa.providers.codex import provider as PV
    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, **kw):
        captured.update(kw)
        return orig_write(authoring, **kw)
    monkeypatch.setattr(PV.codex_agents_md, "write_agents_md", _spy)

    steps = [{"thread_id": "TH-DEEP-AGENTS",
             "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_deep_agents_md")
    prov = A.CodexProvider()
    ctx = _ctx(uid="deep-agents-md", conversation_id=901, scope_meta={"depth_profile": "standard"})

    _run(prov, ctx)

    assert captured.get("multi_agent") is True
    assert captured.get("review_rounds") == 2


def test_quick_depth_profile_keeps_multi_agent_on_with_zero_review_rounds(tmp_path, monkeypatch):
    """クイック（§2.6「常時」有効化の裁定）でも `multi_agent=True` のまま、`review_rounds` だけ 0 に
    なる（深さに関わらず multi_agent 自体は常時 on＝Codex(OpenAI) 構成なら常に有効）。"""
    from sherpa import codex_agents_md
    from sherpa.providers.codex import provider as PV
    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, **kw):
        captured.update(kw)
        return orig_write(authoring, **kw)
    monkeypatch.setattr(PV.codex_agents_md, "write_agents_md", _spy)

    steps = [{"thread_id": "TH-STD-AGENTS",
             "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_standard_agents_md")
    prov = A.CodexProvider()
    ctx = _ctx(uid="standard-agents-md", conversation_id=902,
               scope_meta={"depth_profile": "quick"})

    _run(prov, ctx)

    assert captured.get("multi_agent") is True
    assert captured.get("review_rounds") == 0


def test_deep_depth_profile_two_evaluator_spawns_are_captured_as_children(tmp_path, monkeypatch):
    """深く（見直しの回数 2）で evaluator が2回 spawn される想定を偽 codex で再現する。
    2本の子スレッド（session JSONL あり）がどちらも見つかった（found）扱いになる——
    Sherpa 側は spawn 回数を強制しない（指示のみ）ため、この検収は「2回 spawn されたときに
    正しく数えられる」ことの確認（受け入れ条件(3)前半）。"""
    steps = [{
        "thread_id": "SID-DEEP-ROUNDS",
        "collab_spawns": [
            {"id": "eval1", "receiver_thread_ids": ["EVAL-1"]},
            {"id": "eval2", "receiver_thread_ids": ["EVAL-2"]},
        ],
        "child_sessions": [
            {"thread_id": "EVAL-1",
             "usage": {"input_tokens": 20, "cached_input_tokens": 0,
                       "output_tokens": 4, "reasoning_output_tokens": 1}},
            {"thread_id": "EVAL-2",
             "usage": {"input_tokens": 15, "cached_input_tokens": 0,
                       "output_tokens": 3, "reasoning_output_tokens": 1}},
        ],
        "agent_messages": ["1回目は不足と判定し見直した後、確認した結果、影響はありません。"],
        "usage": _usage(30, 2, 13, 3),
    }]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_deep_two_rounds")
    prov = A.CodexProvider()
    ctx = _ctx(uid="deep-two-rounds", conversation_id=903, scope_meta={"depth_profile": "deep"})

    env = _result_env(_run(prov, ctx))

    assert env["codex_usage_children"]["found"] == 2
    assert env["codex_usage_children"]["missing"] == 0


def test_no_evaluator_spawn_when_sufficient_stays_zero_children(tmp_path, monkeypatch):
    """深く（見直しの回数 2）でも、evaluator を一度も spawn しない実行（十分・確認・予算・停止
    に相当）は子スレッドが一切記録されない——`collab_tool_call` を Sherpa 側が強制発火しない
    ことの確認（受け入れ条件(3)後半）。"""
    steps = [{"thread_id": "SID-DEEP-NOEVAL",
             "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage(30, 2, 13, 3)}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_deep_no_eval")
    prov = A.CodexProvider()
    ctx = _ctx(uid="deep-no-eval", conversation_id=904, scope_meta={"depth_profile": "deep"})

    env = _result_env(_run(prov, ctx))

    assert "codex_usage_children" not in env


# ===== 予算天井の窓連動の撤去・codex.log 開始/終了行の実効値可視化 =====
# 旧実装は窓が不明なとき Codex 専用の固定天井（64KiB）へ落ちていた（実環境で `agentic_budget_
# total=4MiB` を保存していても実際は 1MiB で打ち切られていた原因）。利用者裁定「AI が持つ文脈窓を
# Sherpa が制限しない」（`docs/proposals/2026-09-22-Codex経路の精度・網羅性と費用の改善.md`）で
# この天井・窓連動ごと撤去済み——常にコード既定／管理画面の基準値をそのまま使う。開始行の
# `window_source`/`window_cli` は常に `none`（窓は Codex CLI 任せ・Sherpa は渡さない）。

def _codex_log_lines(caplog, prefix: str) -> list:
    """`sherpa.codex` ロガーが出したレコードのうち、指定の行頭（"start "/"end "）に一致する
    ものだけを返す。"""
    return [r.getMessage() for r in caplog.records
            if r.name == "sherpa.codex" and r.getMessage().startswith(prefix)]


def test_start_log_budget_per_result_uses_admin_baseline_unaffected_by_window(tmp_path, monkeypatch, caplog):
    """既定モデル（gpt-5.5）でも、1件あたりの実効予算は Codex 経路の天井（64KiB・V0.9 と同じ値）。
    開始行の
    `window_source=none`/`window_cli=none` は窓を Sherpa が渡さないことを表す。累計予算・呼び出し回数の
    上限は Codex 経路では渡さない（撤去済み・常に `none`）。"""
    steps = [{"thread_id": "SID-LOG-START", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_log_start_unknown")
    prov = A.CodexProvider(system_settings={})
    ctx = _ctx(uid="log-start-unknown", conversation_id=1001)

    with caplog.at_level(logging.INFO):
        _run(prov, ctx)

    starts = _codex_log_lines(caplog, "start ")
    assert len(starts) == 1, starts
    line = starts[0]
    assert "window_source=none" in line, line
    assert "window_cli=none" in line, line
    assert "budget_per_result=65536" in line, line
    assert "budget_total=none" in line, line
    assert "reasoning=low" in line, line
    assert "max_calls=none" in line, line
    assert "max_hits=" in line and "window_cap=" in line, line


def test_start_log_max_calls_always_none_even_for_quick_depth(tmp_path, monkeypatch, caplog):
    """呼び出し回数の上限は撤去済み——クイックであっても開始行は常に `max_calls=none`
    （調査を終了させる上限を外す・受け入れ条件3の固定）。"""
    steps = [{"thread_id": "SID-LOG-QUICK", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_log_start_quick")
    prov = A.CodexProvider(system_settings={})
    ctx = _ctx(uid="log-start-quick", conversation_id=1003, scope_meta={"depth_profile": "quick"})

    with caplog.at_level(logging.INFO):
        _run(prov, ctx)

    starts = _codex_log_lines(caplog, "start ")
    assert len(starts) == 1, starts
    line = starts[0]
    assert "max_calls=none" in line, line


def test_end_log_shows_parent_and_child_token_breakdown(tmp_path, monkeypatch, caplog):
    """終了行に親/子のトークン内訳（入力/キャッシュ済み入力/出力/推論出力）と
    children_found/children_missing が出る。回答本文（agent_message）は含まれない。"""
    steps = [{
        "thread_id": "SID-LOG-END",
        "collab_spawns": [{"id": "c1", "receiver_thread_ids": ["CHILD-LOG-1"]}],
        "child_sessions": [
            {"thread_id": "CHILD-LOG-1",
             "usage": {"input_tokens": 100, "cached_input_tokens": 7,
                       "output_tokens": 20, "reasoning_output_tokens": 5}},
        ],
        "agent_messages": ["確認した結果、影響はありません。SECRET-DOC-NAME-XYZ には触れません。"],
        "usage": _usage(30, 2, 13, 3),
    }]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_log_end_children")
    prov = A.CodexProvider(system_settings={})
    ctx = _ctx(uid="log-end-children", conversation_id=1004)

    with caplog.at_level(logging.INFO):
        _run(prov, ctx)

    ends = _codex_log_lines(caplog, "end ")
    assert len(ends) == 1, ends
    line = ends[0]
    assert "input=30" in line, line
    assert "cached_input=2" in line, line
    assert "output=13" in line, line
    assert "reasoning_output=3" in line, line
    assert "child_input=100" in line, line
    assert "child_cached_input=7" in line, line
    assert "child_output=20" in line, line
    assert "child_reasoning_output=5" in line, line
    assert "children_found=1" in line, line
    assert "children_missing=0" in line, line
    assert "SECRET-DOC-NAME-XYZ" not in line, "本文が終了行へ漏れている"


def test_codex_log_records_never_contain_answer_body(tmp_path, monkeypatch, caplog):
    """codex.log へ出る全レコード（start/end 双方）に回答本文の秘匿マーカーが一切現れないこと
    （既存の no-leak テストと同じ流儀）。"""
    steps = [{"thread_id": "SID-LOG-NOLEAK",
             "agent_messages": ["これは機密文書 TOPSECRET-MARKER-999 の内容です。"],
             "usage": _usage(30, 2, 13, 3)}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_log_noleak")
    prov = A.CodexProvider(system_settings={})
    ctx = _ctx(uid="log-noleak", conversation_id=1005)

    with caplog.at_level(logging.INFO):
        _run(prov, ctx)

    codex_records = [r.getMessage() for r in caplog.records if r.name == "sherpa.codex"]
    assert codex_records, "codex.log 相当のレコードが1件も無い"
    for msg in codex_records:
        assert "TOPSECRET-MARKER-999" not in msg, f"回答本文がログへ漏れている: {msg!r}"
