"""Codex の usage 集計の契約テスト（`CodexProvider._run_authoring`）。
resume が効いたターンだけ前ターンの累計（ctx.codex_usage_prev_total）との差分を env["usage"] にし、
env["codex_usage_total"] には常に今回の累計を残す。子スレッド（旧形式 spawn_agent・新形式 rollout の
parent_thread_id 突合）の usage は親へ合算し内訳を残す。codex.log の start/end 行に実効値・内訳が出て本文は漏れない。
呼び出し回数ごとの応答計画を JSON で渡す偽 codex（実 codex は呼ばない）。
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

import pytest

# setdefault のみ（モジュールレベル直書きは pytest 一括収集時にプロセス全体へ漏れるため禁止）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402
from sherpa.providers.codex import continuation as CONT  # noqa: E402

_FAKE_CODEX_MULTI_PY = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

argv_log = pathlib.Path(r"__ARGV_LOG__")
plan_path = pathlib.Path(r"__PLAN_PATH__")
args = sys.argv[1:]
if args and args[-1] == "-":   # プロンプトは argv でなく標準入力から渡される
    args = args[:-1] + [sys.stdin.read()]
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

# 旧形式: spawn_agent の collab_tool_call item。新形式（実機 0.153.4）は spawn_agent item が無く
# `wait` の collab_tool_call だけ（collab_spawns を渡さず wait_calls だけを渡す）。
for spawn in step.get("collab_spawns", []):
    print(json.dumps({"type": "item.completed", "item": {
        "id": spawn.get("id", "collab"), "type": "collab_tool_call", "tool": "spawn_agent",
        "receiver_thread_ids": spawn.get("receiver_thread_ids", [])}}))
    sys.stdout.flush()

for i in range(step.get("wait_calls", 0)):
    print(json.dumps({"type": "item.completed", "item": {
        "id": f"wait{i}", "type": "collab_tool_call", "tool": "wait", "receiver_thread_ids": []}}))
    sys.stdout.flush()

# 子スレッドの session JSONL を CODEX_HOME/sessions 配下に書く。parent_thread_id/thread_source は渡された
# ときだけ載せる（旧形式は省く）。"usage" を渡さない子＝走行中／usage を書く前に落ちた rollout。
for child in step.get("child_sessions", []):
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        sdir = pathlib.Path(codex_home) / "sessions" / "2026" / "01" / "01"
        sdir.mkdir(parents=True, exist_ok=True)
        meta_payload = {"id": child["thread_id"]}
        for k in ("parent_thread_id", "thread_source"):
            if k in child:
                meta_payload[k] = child[k]
        lines = [json.dumps({"payload": meta_payload})]
        if "usage" in child:
            lines.append(json.dumps({"payload": {"type": "token_count",
                                                  "info": {"total_token_usage": child["usage"]}}}))
        (sdir / f"rollout-{child['thread_id']}.jsonl").write_text("\n".join(lines) + "\n")

# 道具ゼロの促しを出さないよう、1 回目の実行は既定で道具を 1 回使う（`no_tools` で 0 回にする）。
if call_index == 1 and not step.get("no_tools"):
    print(json.dumps({"type": "item.completed", "item": {
        "id": "t0-default", "type": "command_execution", "command": "ls", "status": "completed",
        "exit_code": 0}}))
    sys.stdout.flush()

for i, text in enumerate(step.get("agent_messages", [])):
    print(json.dumps({"type": "item.completed",
                       "item": {"id": f"m{i}", "type": "agent_message", "text": text}}))
    sys.stdout.flush()

usage = step.get("usage")
if usage:
    print(json.dumps({"type": "turn.completed", "usage": usage}))
    sys.stdout.flush()

sys.exit(step.get("exit_code", 0))
'''

OK_MSG = "確認した結果、影響はありません。"


def _setup(tmp_path: Path, monkeypatch, steps: list, users_dirname: str) -> Path:
    """偽 codex を PATH に差し込み、呼び出しごとの応答計画（steps）を JSON で渡す。戻り値は argv_log。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_MULTI_PY.replace("__ARGV_LOG__", str(argv_log))
                      .replace("__PLAN_PATH__", str(plan_path)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")   # 平文の偽 codex＝構造化応答は使わない
    return argv_log


def _ctx(uid: str, conversation_id, codex_session_id=None, codex_usage_prev_total=None, scope_meta=None):
    return A.Ctx(
        message="usage 差分テスト",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {"lens": lens_, "headline": "dispatch-headline",
                                     "summary": {"total": 0}, "data": {}, "sources": []},
        knowledge=True, uid=uid, conversation_id=conversation_id,
        codex_session_id=codex_session_id, codex_usage_prev_total=codex_usage_prev_total,
        scope_meta=scope_meta,
    )


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
    """前ターンが記録した env["codex_usage_total"]（ctx.codex_usage_prev_total の形）。"""
    return {"session_id": session_id, **_usage(input_tokens, cached_input_tokens, output_tokens,
                                               reasoning_output_tokens)}


def _child(thread_id, input_tokens, output_tokens, *, parent=None, cached=0, reasoning=0, with_usage=True):
    c = {"thread_id": thread_id}
    if parent:
        c.update(parent_thread_id=parent, thread_source="subagent")
    if with_usage:
        c["usage"] = _usage(input_tokens, cached, output_tokens, reasoning)
    return c


def _step(thread_id, usage=None, **extra) -> dict:
    return {"thread_id": thread_id, "agent_messages": [OK_MSG], "usage": usage or _usage(30, 2, 13, 3), **extra}


def _exec(tmp_path, monkeypatch, steps, uid, cid, provider=None, **ctx_kw):
    """偽 codex でターンを1回流す。戻り値は (env, argv 呼び出し一覧)。"""
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname=f"users_{uid}")
    events = list((provider or A.CodexProvider()).run(_ctx(uid=uid, conversation_id=cid, **ctx_kw)))
    return _result_env(events), _read_argv_log(argv_log)


def _tok(i, c, o, r) -> dict:
    return {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o, "reasoning_output_tokens": r}


# ===== resume の usage 差分 =====

def test_resume_success_with_matching_prev_total_uses_delta(tmp_path, monkeypatch):
    env, calls = _exec(tmp_path, monkeypatch, [_step("SID-1")], "delta-ok", 701,
                       codex_session_id="SID-1", codex_usage_prev_total=_prev_total("SID-1"))

    assert len(calls) == 1, f"resume 成功時は1回だけのはず: {calls!r}"
    assert "resume" in calls[0] and "SID-1" in calls[0]
    assert env["codex_usage_total"] == {"session_id": "SID-1", **_tok(30, 2, 13, 3)}, "累計は差分化せずそのまま"
    usage = env["usage"]
    assert usage["provider"] == "codex"
    assert usage["input_tokens"] == 20              # max(0, 30-10)
    assert usage["cached_input_tokens"] == 0        # max(0, 2-2)
    assert usage["output_tokens"] == 8              # max(0, 13-5)
    assert usage["reasoning_output_tokens"] == 2    # max(0, 3-1)


@pytest.mark.parametrize("name, steps, ctx_kw, n_calls, first_resumes, total_sid", [
    # 新規セッション（prev None）→ usage＝累計・resume 引数なし
    ("fresh", [_step("SID-NEW")], {}, 1, False, "SID-NEW"),
    # resume 失敗（無出力・exit 1）→フォールバック新規セッション→ prev があっても差分にしない
    ("fallback", [{"exit_code": 1}, _step("SID-FALLBACK")],
     {"codex_session_id": "SID-OLD", "codex_usage_prev_total": _prev_total("SID-OLD")}, 2, True, "SID-FALLBACK"),
    # prev の session_id が不一致 → 差分にしない
    ("mismatch", [_step("SID-2")],
     {"codex_session_id": "SID-2", "codex_usage_prev_total": _prev_total("SID-DIFFERENT")}, 1, True, "SID-2"),
])
def test_usage_is_raw_accumulated_when_delta_does_not_apply(tmp_path, monkeypatch, name, steps, ctx_kw,
                                                            n_calls, first_resumes, total_sid):
    env, calls = _exec(tmp_path, monkeypatch, steps, f"delta-{name}", 702, **ctx_kw)

    assert len(calls) == n_calls, calls
    assert ("resume" in calls[0]) is first_resumes
    if name == "fallback":
        assert "resume" in calls[0] and "SID-OLD" in calls[0]
        assert "resume" not in calls[1]
    assert env["codex_usage_total"]["session_id"] == total_sid
    assert env["usage"]["input_tokens"] == 30, "差分にせず累計そのまま"
    assert env["usage"]["output_tokens"] == 13


def test_auto_continue_uses_latest_snapshot_for_delta(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "SID-3", "agent_messages": ["まず資料を確認します。"], "usage": _usage(10, 2, 5, 1)},
        _step("SID-3"),   # 継続（resume）側の usage はセッション累計＝1回目の分を含む値（Codex CLI の契約）
    ]
    env, calls = _exec(tmp_path, monkeypatch, steps, "delta-continue", 705,
                       codex_session_id="SID-3", codex_usage_prev_total=_prev_total("SID-3"))

    assert len(calls) == 2, f"作業宣言→結論で継続2回のはず: {calls!r}"
    assert all("resume" in c and "SID-3" in c for c in calls)
    assert calls[1][-1] == CONT._CONTINUE_PROMPT
    assert env["codex_usage_total"] == {"session_id": "SID-3", **_tok(30, 2, 13, 3)}, "累計は最新 snapshot"
    usage = env["usage"]
    assert usage["input_tokens"] == 20      # 最新 snapshot(30) - prev(10)。1回目の分(10)を足さない
    assert usage["cached_input_tokens"] == 0
    assert usage["output_tokens"] == 8      # 13 - 5
    assert usage["reasoning_output_tokens"] == 2   # 3 - 1


@pytest.mark.parametrize("profile", ["deep", "standard"])
def test_depth_profile_keeps_configured_reasoning_in_usage(tmp_path, monkeypatch, profile):
    """推論レベルは深さで変えない: model_reasoning_effort は基準値（既定 medium）のまま codex exec へ渡り、
    env["usage"]["reasoning"] もその値・基準値のままなので reasoning_base は付かない。"""
    env, calls = _exec(tmp_path, monkeypatch, [_step(f"SID-{profile}")], f"delta-{profile}", 703,
                       scope_meta={"depth_profile": profile})

    assert any("model_reasoning_effort=medium" in a for a in calls[0]), calls[0]
    assert env["usage"]["depth_profile"] == profile
    assert env["usage"]["reasoning"] == "medium"
    assert "reasoning_base" not in env["usage"]


# ===== 子スレッド（spawn_agent）の usage 合算 =====

def test_child_thread_usage_merged_into_answer_usage_with_breakdown(tmp_path, monkeypatch):
    """同ターン中に spawn_agent された子2本の session JSONL を親へ合算する（env["usage"]＝親＋子）。内訳
    （親／子／未取得件数）が別途残り、次ターンの差分計算の元（codex_usage_total）は子の分を混ぜない。"""
    step = _step("SID-PARENT", collab_spawns=[{"id": "c1", "receiver_thread_ids": ["CHILD-1"]},
                                              {"id": "c2", "receiver_thread_ids": ["CHILD-2"]}],
                 child_sessions=[_child("CHILD-1", 100, 20, reasoning=5), _child("CHILD-2", 50, 10, reasoning=2)])
    env, calls = _exec(tmp_path, monkeypatch, [step], "child-usage-u1", 801)

    assert len(calls) == 1
    assert env["codex_usage_total"] == {"session_id": "SID-PARENT", **_tok(30, 2, 13, 3)}
    assert env["codex_usage_children"] == {"found": 2, "missing": 0, **_tok(150, 0, 30, 7)}
    usage = env["usage"]
    assert usage["input_tokens"] == 180
    assert usage["cached_input_tokens"] == 2
    assert usage["output_tokens"] == 43
    assert usage["reasoning_output_tokens"] == 10
    assert usage["codex_usage_breakdown"] == {
        "parent": _tok(30, 2, 13, 3), "children": _tok(150, 0, 30, 7),
        "children_found": 2, "children_missing": 0}


def test_child_thread_usage_missing_child_counted_without_estimation(tmp_path, monkeypatch):
    """子の session JSONL が見つからない（未取得）ときは推定で埋めず件数だけ missing に残す。"""
    step = _step("SID-PARENT-2", collab_spawns=[{"id": "c1", "receiver_thread_ids": ["CHILD-FOUND"]},
                                                {"id": "c2", "receiver_thread_ids": ["CHILD-LOST"]}],
                 child_sessions=[_child("CHILD-FOUND", 40, 8, reasoning=1)])   # CHILD-LOST は書かない
    env, _ = _exec(tmp_path, monkeypatch, [step], "child-usage-u2", 802)

    assert env["codex_usage_children"] == {"found": 1, "missing": 1, **_tok(40, 0, 8, 1)}
    assert env["usage"]["input_tokens"] == 70    # 30(親) + 40(見つかった子だけ)
    assert env["usage"]["output_tokens"] == 21   # 13 + 8


@pytest.mark.parametrize("scope_meta", [None, {"depth_profile": "deep"}])
def test_no_child_spawn_keeps_usage_unchanged(tmp_path, monkeypatch, scope_meta):
    """collab_tool_call が一度も出ない実行（deep でも evaluator を spawn しない）は codex_usage_children /
    codex_usage_breakdown が出ない＝既存の usage 計上のまま（Sherpa は spawn を強制発火しない）。"""
    env, _ = _exec(tmp_path, monkeypatch, [_step("SID-NOCHILD")], "no-child-u1", 803, scope_meta=scope_meta)

    assert "codex_usage_children" not in env
    assert "codex_usage_breakdown" not in env["usage"]
    assert env["usage"]["input_tokens"] == 30


def test_child_thread_usage_breakdown_on_resume_uses_delta_not_cumulative(tmp_path, monkeypatch):
    """resume ターンで子が合算されるとき、内訳の「親」分はセッション累計（前ターン分を含む）でなく、
    差分化済みのこのターンの計上値から作る（累計だと親分が「合計－子分」より過大になる）。"""
    step = _step("SID-RESUME-CHILD", usage=_usage(130, 2, 13, 3),   # セッション累計（前ターン分 100 を含む）
                 collab_spawns=[{"id": "c1", "receiver_thread_ids": ["CHILD-1"]}],
                 child_sessions=[_child("CHILD-1", 40, 8, reasoning=1)])
    env, _ = _exec(tmp_path, monkeypatch, [step], "resume-child-usage-u1", 804, codex_session_id="SID-RESUME-CHILD",
                   codex_usage_prev_total=_prev_total("SID-RESUME-CHILD", input_tokens=100,
                                                      cached_input_tokens=2, output_tokens=5,
                                                      reasoning_output_tokens=1))

    usage = env["usage"]
    assert usage["input_tokens"] == 70     # 親差分(130-100=30) + 子(40)
    assert usage["output_tokens"] == 16    # 親差分(8) + 子(8)
    assert usage["codex_usage_breakdown"]["parent"] == _tok(30, 0, 8, 2), "内訳の親分が累計のまま"
    assert usage["codex_usage_breakdown"]["children"] == _tok(40, 0, 8, 1)


def test_cache_write_delta_adds_children_once_and_stays_unknown_without_prev(tmp_path, monkeypatch):
    """キャッシュ書き込み量も他のトークンと同じ規則（累計→ターン差分・親子合算）に通す。次ターンの差分の元（codex_usage_total）は親の累計のままで子を混ぜない。
    前ターンの累計に書き込み量が無い（不明）なら差分は不明のまま（0 にしない・項目ごと載せない）。"""
    cw = lambda n: {"cache_write_tokens": n}   # noqa: E731
    step = _step("SID-CW", usage={**_usage(130, 2, 13, 3), **cw(30)},
                 collab_spawns=[{"id": "c1", "receiver_thread_ids": ["CHILD-1"]}],
                 child_sessions=[{"thread_id": "CHILD-1", "usage": {**_usage(40, 0, 8, 1), **cw(5)}}])
    env, _ = _exec(tmp_path, monkeypatch, [step], "cw-delta-u1", 805, codex_session_id="SID-CW",
                   codex_usage_prev_total={**_prev_total("SID-CW", input_tokens=100), **cw(10)})
    assert env["codex_usage_total"]["cache_write_tokens"] == 30
    assert env["usage"]["cache_write_tokens"] == 25                       # 親差分(30-10) + 子(5)
    assert env["usage"]["codex_usage_breakdown"]["parent"]["cache_write_tokens"] == 20
    assert env["usage"]["codex_usage_breakdown"]["children"]["cache_write_tokens"] == 5

    (tmp_path / "second").mkdir()
    env2, _ = _exec(tmp_path / "second", monkeypatch, [_step("SID-CW2", usage={**_usage(130, 2, 13, 3), **cw(30)})],
                    "cw-delta-u2", 806, codex_session_id="SID-CW2",
                    codex_usage_prev_total=_prev_total("SID-CW2", input_tokens=100))   # 前ターンは書き込み量が不明
    assert "cache_write_tokens" not in env2["usage"]
    assert env2["codex_usage_total"]["cache_write_tokens"] == 30


# ===== 新形式: spawn_agent item が出ない CLI でも rollout の parent_thread_id 突合で子を数える =====

def _codex_log_lines(caplog, prefix: str) -> list:
    return [r.getMessage() for r in caplog.records
            if r.name == "sherpa.codex" and r.getMessage().startswith(prefix)]


def test_new_format_child_detection_via_rollout_parent_thread_id(tmp_path, monkeypatch, caplog):
    """spawn_agent item が一切出ない（wait だけ）ターンでも、子 rollout の parent_thread_id/thread_source が
    親の thread id と一致すれば子として検出し usage を合算する。codex.log 終了行の spawn_agents は実際に
    見つけた子の数になる。"""
    tid = "SID-NEWFMT-1"
    step = _step(tid, wait_calls=2, no_tools=True, child_sessions=[_child("CHILD-NF-1", 60, 12, parent=tid, reasoning=2),
                                                    _child("CHILD-NF-2", 40, 8, parent=tid, reasoning=1)])
    with caplog.at_level(logging.INFO):
        env, calls = _exec(tmp_path, monkeypatch, [step], "newfmt-child-u1", 1101,
                           provider=A.CodexProvider(system_settings={}))

    assert len(calls) == 1  # 子だけの実行は「使っていない」と断定せず、道具ゼロの促しを出さない

    assert env["codex_usage_children"] == {"found": 2, "missing": 0, **_tok(100, 0, 20, 3)}
    assert env["usage"]["input_tokens"] == 130   # 30(親) + 100(子)
    assert env["usage"]["output_tokens"] == 33   # 13 + 20
    ends = _codex_log_lines(caplog, "end ")
    assert len(ends) == 1, ends
    for token in ("spawn_agents=2", "children_found=2", "children_missing=0"):
        assert token in ends[0], ends[0]


def test_new_format_child_detected_without_usage_counts_as_spawned_not_missing_entirely(tmp_path, monkeypatch, caplog):
    """「起動を検出した」ことと「usage を読めた」ことを混同しない。usage を書く前に落ちた／走行中の rollout
    （session_meta はあるが token_count が無い）は spawn_agents（検出数）1のまま、children_found（合算できた数）
    だけ0・children_missing（usage 未取得）が1になる（found=0 で spawn_agents まで0にすると「起動していない」と
    区別が付かない）。"""
    tid = "SID-NEWFMT-NOUSAGE"
    step = _step(tid, wait_calls=1, no_tools=True, child_sessions=[_child("CHILD-NOUSAGE-1", 0, 0, parent=tid, with_usage=False)])
    with caplog.at_level(logging.INFO):
        env, _ = _exec(tmp_path, monkeypatch, [step], "newfmt-nousage-u1", 1104,
                       provider=A.CodexProvider(system_settings={}))

    assert env["codex_usage_children"] == {"found": 0, "missing": 1, **_tok(0, 0, 0, 0)}
    assert env["usage"]["input_tokens"] == 30   # usage が取れないので親分のみ
    assert env["usage"]["output_tokens"] == 13
    ends = _codex_log_lines(caplog, "end ")
    assert len(ends) == 1, ends
    for token in ("spawn_agents=1", "children_found=0", "children_missing=1"):
        assert token in ends[0], ends[0]


def test_new_format_and_old_format_children_are_unioned_not_duplicated(tmp_path, monkeypatch):
    """旧形式（spawn_agent item の receiver_thread_ids）で捕捉した子と、新形式（parent_thread_id 突合）でしか
    見えない子が混在しても、和集合で数えて二重に数えない。"""
    tid = "SID-NEWFMT-MIX"
    step = _step(tid, collab_spawns=[{"id": "c1", "receiver_thread_ids": ["CHILD-MIX-OLD"]}],
                 child_sessions=[_child("CHILD-MIX-OLD", 60, 12, parent=tid, reasoning=2),
                                 _child("CHILD-MIX-NEW", 40, 8, parent=tid, reasoning=1)])
    env, _ = _exec(tmp_path, monkeypatch, [step], "newfmt-mix-u1", 1102)

    assert env["codex_usage_children"] == {"found": 2, "missing": 0, **_tok(100, 0, 20, 3)}


def test_new_format_resumed_turn_does_not_recount_prior_turn_children(tmp_path, monkeypatch):
    """resume は同じ thread id を跨ターンで使い回す。新形式の判定だけで子を数えると、前ターンの子 rollout が
    残っている限り、子を spawn していない次ターンでも再計上してしまう——_collect_child_token_usage の
    min_mtime（今ターン開始の壁時計）が防ぐ（今ターンより前に書かれた rollout は対象外）。"""
    tid = "SID-NEWFMT-RESUME"
    steps = [
        _step(tid, no_tools=True, child_sessions=[_child("CHILD-RS-1", 60, 12, parent=tid, reasoning=2),
                                   _child("CHILD-RS-2", 40, 8, parent=tid, reasoning=1)]),
        {"thread_id": tid, "agent_messages": ["確認した結果、追加の影響はありません。"], "usage": _usage(50, 2, 20, 3)},
    ]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_newfmt_resume")
    prov = A.CodexProvider()
    uid, cid = "newfmt-resume-u1", 1103

    env1 = _result_env(list(prov.run(_ctx(uid=uid, conversation_id=cid))))
    assert env1["codex_usage_children"]["found"] == 2, "1ターン目は子2体を数えられているはず"

    # 1ターン目の子 rollout を過去へ巻き戻す（mtime 分解能では「今ターンより前」が実時間だけでは保証できない）。
    codex_home = tmp_path / "users_newfmt_resume" / uid / "workspace" / ".codex-sessions" / str(cid)
    past = time.time() - 3600
    for p in codex_home.glob("sessions/**/*.jsonl"):
        os.utime(p, (past, past))

    env2 = _result_env(list(prov.run(_ctx(uid=uid, conversation_id=cid, codex_session_id=tid,
                                          codex_usage_prev_total=_prev_total(tid, 30, 2, 13, 3)))))

    assert "codex_usage_children" not in env2, "前ターンの子 rollout を再計上している（min_mtime が効いていない）"
    assert env2["usage"]["input_tokens"] == 20   # 50-30（親のみの差分・子の混入なし）
    assert env2["usage"]["output_tokens"] == 7   # 20-13


# ===== multi_agent 既定有効化・review_rounds の受け渡し =====

@pytest.mark.parametrize("profile, rounds", [("standard", 2), ("quick", 0)])
def test_depth_profile_passes_multi_agent_on_and_review_rounds_to_agents_md(tmp_path, monkeypatch, profile, rounds):
    """AGENTS.md 生成へ multi_agent=True（深さに関わらず常時 on）と review_rounds
    （depth_profile.review_rounds_for の戻り値）をそのまま渡す。"""
    from sherpa import codex_agents_md
    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, **kw):
        captured.update(kw)
        return orig_write(authoring, **kw)
    monkeypatch.setattr(codex_agents_md, "write_agents_md", _spy)

    _exec(tmp_path, monkeypatch, [{"thread_id": "TH-AGENTS", "agent_messages": [OK_MSG], "usage": _usage()}],
          f"agents-md-{profile}", 901, scope_meta={"depth_profile": profile})

    assert captured.get("multi_agent") is True
    assert captured.get("review_rounds") == rounds


# ===== 予算天井の窓連動の撤去・codex.log 開始/終了行の実効値可視化 =====

def test_start_log_budget_per_result_uses_admin_baseline_unaffected_by_window(tmp_path, monkeypatch, caplog):
    """既定モデルでも 1件あたりの実効予算は Codex 経路の天井（64KiB）。window_source=none/window_cli=none は
    窓を Sherpa が渡さないことを表し、累計予算・呼び出し回数の上限は Codex 経路では渡さない（常に none）。"""
    step = {"thread_id": "SID-LOG-START", "agent_messages": [OK_MSG], "usage": _usage()}
    with caplog.at_level(logging.INFO):
        _exec(tmp_path, monkeypatch, [step], "log-start-unknown", 1001, provider=A.CodexProvider(system_settings={}))

    starts = _codex_log_lines(caplog, "start ")
    assert len(starts) == 1, starts
    line = starts[0]
    for token in ("window_source=none", "window_cli=none", "budget_per_result=65536", "budget_total=none",
                  "reasoning=medium", "max_calls=none"):
        assert token in line, line
    assert "max_hits=" in line and "window_cap=" in line, line


def test_end_log_shows_parent_and_child_token_breakdown_and_never_contains_answer_body(tmp_path, monkeypatch, caplog):
    """終了行に親/子のトークン内訳と children_found/children_missing が出る。codex.log へ出る全レコード
    （start/end）に回答本文の秘匿マーカーが一切現れない。"""
    step = _step("SID-LOG-END", collab_spawns=[{"id": "c1", "receiver_thread_ids": ["CHILD-LOG-1"]}],
                 child_sessions=[_child("CHILD-LOG-1", 100, 20, cached=7, reasoning=5)],
                 agent_messages=["確認した結果、影響はありません。SECRET-DOC-NAME-XYZ と TOPSECRET-MARKER-999 には触れません。"])
    with caplog.at_level(logging.INFO):
        _exec(tmp_path, monkeypatch, [step], "log-end-children", 1004, provider=A.CodexProvider(system_settings={}))

    ends = _codex_log_lines(caplog, "end ")
    assert len(ends) == 1, ends
    for token in ("input=30", "cached_input=2", "output=13", "reasoning_output=3", "child_input=100",
                  "child_cached_input=7", "child_output=20", "child_reasoning_output=5",
                  "children_found=1", "children_missing=0"):
        assert token in ends[0], ends[0]
    codex_records = [r.getMessage() for r in caplog.records if r.name == "sherpa.codex"]
    assert codex_records, "codex.log 相当のレコードが1件も無い"
    for msg in codex_records:
        assert "SECRET-DOC-NAME-XYZ" not in msg and "TOPSECRET-MARKER-999" not in msg, f"回答本文がログへ漏れている: {msg!r}"
