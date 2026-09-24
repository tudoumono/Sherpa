"""Codex 自動継続（「途中経過だけ」で止まったターンをセッションの続きで自動的に押し進める）。

背景（実環境観測）: `codex exec --json` は調査中に「次に○○を調べます」という作業宣言を
`agent_message` として出し、ツールを呼ばずにターンを閉じることがある。正常終了
（returncode 0）でこれが起きると、`_pick_codex_headline` の規則③（結論文が1つも無ければ
最後の1件をそのまま返す）でその宣言文がそのまま回答になり、利用者が「続けて」を連投しないと
結論に届かなかった。`providers/codex/provider.py::CodexProvider._run_authoring` は、正常終了・
作業宣言だけ・セッション永続ありの条件がそろうとき、Codex セッションの続き（resume）を
`SHERPA_CODEX_AUTO_CONTINUE`（既定3・0で無効）回まで自動で呼ぶ。尽くしてもなお結論に届かない
ときは `env["codex_stopped_early"]` を立て、`chat_service._finalize` が STOP-1/SC-6d と同じ形
（headline は書き換えない・retry_hints に kind="resume" を追加）で案内する
（`web/chat/render.js::codexStoppedEarlyNoteHTML` が本文直下に注記を出す）。TIMEOUT-1
（2026-09-11）で経過時間だけの打ち切り（旧 `codex_timed_out`）は撤去済み——利用者の明示停止
（stop_event）は `codex_stopped_early` と別扱いのまま残る。

既存 tests/unit/test_codex_resume.py と同じ「偽 codex 実行ファイルを PATH に差し込む」流儀
（実 codex は一切呼ばない）。ただし本ファイルの偽 codex は呼び出しごとに異なる応答（作業宣言→
結論、作業宣言の連続、無出力異常終了等）を返す必要があるため、呼び出し回数ごとの応答計画を JSON
ファイルで渡す独自の偽 codex を使う（他ファイルの固定応答スクリプトは再利用しない・呼び出し回数は
argv_log の行数で数える）。
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest
from pathlib import Path

# test_codex_resume.py と同じ流儀（setdefault のみ・モジュールレベル直書きは pytest 一括収集時に
# プロセス全体へ漏れるため禁止）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402
from sherpa import chat_service as CS  # noqa: E402

# ===== 偽 codex（呼び出し回数ごとに応答を切り替える・応答計画は JSON ファイルで渡す） =====

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

# 呼び出し回数（自分自身の行を含む・1始まり）＝この起動が何回目かを argv_log の行数で数える。
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

for tool_id in step.get("tool_ids", []):
    print(json.dumps({"type": "item.completed",
                       "item": {"id": tool_id, "type": "command_execution",
                                "command": "ls", "status": "completed", "exit_code": 0}}))
    sys.stdout.flush()

for event in step.get("extra_events", []):
    print(json.dumps(event))
    sys.stdout.flush()

last_message = step.get("last_message")
if last_message is not None and "-o" in args:
    out_path = pathlib.Path(args[args.index("-o") + 1])
    out_path.write_text(last_message, encoding="utf-8")

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
    """偽 codex を PATH に差し込み、呼び出しごとの応答計画（steps）を JSON で渡す。戻り値は argv_log。

    本ファイルの偽 codex は平文（JSON でない）agent_message を返す（語尾ヒューリスティックの検証が
    目的）。`--output-schema`（既定 ON・docs/proposals/2026-09-08-Codex出力スキーマ.md）が有効だと
    平文は「構造化 message でない」＝未完了として扱われ、本ファイルの契約（語尾一覧による継続判定）が
    検証できなくなるため、ここで明示的に無効化する（同提案 §2-3 の「スキーマ無効時は現行ヒューリス
    ティックのまま」契約のテスト）。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    _write_fake_codex(bin_dir, argv_log, plan_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    return argv_log


def _ctx(uid: str, conversation_id: int | None, message: str = "自動継続テスト",
         codex_session_id: str | None = None) -> "A.Ctx":
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
        codex_session_id=codex_session_id,   # §2-3 是正テスト用: resume 先を明示できるようにする（既定 None＝従来どおり）
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


# ===== _needs_continuation（純関数・`_pick_codex_headline` が規則③に落ちる条件と対） =====

def test_needs_continuation_true_when_all_messages_are_progress_only():
    assert A._needs_continuation(
        ["まず資料を確認します。", "次に影響範囲を確認します。"])


def test_needs_continuation_false_when_a_conclusion_is_present():
    assert not A._needs_continuation(
        ["まず資料を確認します。", "確認した結果、影響はありません。"])


def test_needs_continuation_false_when_no_messages_at_all():
    assert not A._needs_continuation([])
    assert not A._needs_continuation(["", "   "])


def test_needs_continuation_considers_partial_message():
    assert A._needs_continuation([], partial="まず資料を確認します")


# ===== 1. 継続して結論を拾う =====

def test_continues_once_and_headline_becomes_the_conclusion(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-FRESH", "agent_messages": ["まず資料を確認します。"],
         "usage": _usage(10, 2, 5, 1)},
        # 継続（resume）側の usage はセッション累計＝1回目の分を含む値が来る（Codex CLI の契約）
        {"thread_id": "TH-FRESH", "agent_messages": ["資料を確認した結果、影響はありません。"],
         "usage": _usage(30, 2, 13, 3)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_ok")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-ok", conversation_id=901)

    events = _run(prov, ctx)
    env = _result_env(events)

    assert env["headline"] == "資料を確認した結果、影響はありません。"
    assert not env.get("codex_stopped_early")
    assert env.get("codex_session_id") == "TH-FRESH"   # 1回目（＝唯一）に捕捉した id のまま

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"作業宣言→結論で2回のはず: {calls!r}"
    assert "resume" in calls[1] and "TH-FRESH" in calls[1]
    assert calls[1][-1] == A._CONTINUE_PROMPT, "2回目の末尾引数が継続プロンプトでない"

    usage = env["usage"]                      # 最新 snapshot（累計）を採用＝足して 40 にしない
    assert usage["input_tokens"] == 30
    assert usage["cached_input_tokens"] == 2
    assert usage["output_tokens"] == 13
    assert usage["reasoning_output_tokens"] == 3

    think_ids = [e.get("id") for e in events if isinstance(e, dict) and e.get("type") == "node"]
    assert "cx-continue-1" in think_ids
    # 利用統計「打ち切りの内訳」計測: 自動継続を1回発行したターンは limits.auto_continues == 1。
    assert env["limits"]["auto_continues"] == 1


# ===== 2. 上限まで作業宣言のまま =====

def test_continuation_stops_at_limit_and_sets_stopped_early_flag(tmp_path, monkeypatch):
    # 継続 attempt（2回目・3回目）はツールを呼ぶ（`tool_ids`）——ツール無し打ち切り是正の対象は
    # あくまで「継続 attempt がツールを1つも呼ばない」場合なので、ここでは上限まで回る現行契約を保つ。
    steps = [
        {"thread_id": "TH-CAP", "agent_messages": ["まず資料を確認します。"], "usage": _usage()},
        {"thread_id": "TH-CAP", "agent_messages": ["次に影響範囲を確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
        {"thread_id": "TH-CAP", "agent_messages": ["続いて関連ファイルを確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_cap")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "2")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-cap", conversation_id=902)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 3, f"初回＋上限2で3回のはず: {calls!r}"
    assert env.get("codex_stopped_early") is True
    # 規則③は不変（最後の作業宣言をそのまま headline にする・本文は書き換えない）。
    assert env["headline"] == "続いて関連ファイルを確認します。"

    finalized = CS._finalize(dict(env), {"lens": "qa", "reason": "既定（検索）"})
    resume_hints = [h for h in finalized.get("retry_hints", []) if h["kind"] == "resume"]
    assert len(resume_hints) == 1, f"resume hint が1件ちょうどでない: {finalized.get('retry_hints')!r}"


# ===== 3. 無効化（SHERPA_CODEX_AUTO_CONTINUE=0） =====

def test_continuation_disabled_by_zero_limit_still_sets_flag(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-OFF", "agent_messages": ["まず資料を確認します。"], "usage": _usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_off")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "0")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-off", conversation_id=903)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"上限0は継続しないはず: {calls!r}"
    assert env.get("codex_stopped_early") is True


# ===== 4. セッション永続なし（conversation_id=None） =====

def test_no_continuation_without_session_persistence_still_sets_flag(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-NOPERSIST", "agent_messages": ["まず資料を確認します。"],
              "usage": _usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_nopersist")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-nopersist", conversation_id=None)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"conversation_id 無しは継続しないはず: {calls!r}"
    assert env.get("codex_stopped_early") is True


# ===== 5. 結論が最初から出る（回帰） =====

def test_no_continuation_when_conclusion_arrives_on_first_attempt(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-DIRECT", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_direct")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-direct", conversation_id=904)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"最初から結論が出れば継続しないはず: {calls!r}"
    assert not env.get("codex_stopped_early")
    assert env["headline"] == "確認した結果、影響はありません。"
    # 自動継続が1回も発行されないターンは limits キー自体を作らない（旧行=0件と同じ集計に乗る）。
    assert "limits" not in env


# ===== 6. 継続中に利用者が明示停止 =====

def test_user_stop_during_continuation_does_not_set_stopped_early(tmp_path, monkeypatch):
    """TIMEOUT-1: 経過時間だけの打ち切り（旧 timeout）は撤去済み——継続 attempt の途中で利用者が
    明示停止（stop_event）した場合は、「AI が自力で途中終了した」ことを示す `codex_stopped_early`
    を立てない（`_stopped_final` が除外する・別の終了理由として扱う）。"""
    steps = [
        {"thread_id": "TH-STOP", "agent_messages": ["まず資料を確認します。"], "usage": _usage()},
        {"thread_id": "TH-STOP", "agent_messages": ["次に影響範囲を確認します。"], "sleep": 30},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_stop")
    prov = A.CodexProvider()
    stop_event = threading.Event()
    ctx = _ctx(uid="auto-continue-stop", conversation_id=905)
    ctx.stop_event = stop_event

    events: list = []

    def _drive():
        for ev in prov.run(ctx):
            events.append(ev)

    th = threading.Thread(target=_drive, daemon=True)
    th.start()

    # 継続 attempt（2回目・sleep 30 の偽 codex）が起動するまで待ってから停止要求する
    # （argv_log の行数＝これまでの呼び出し回数）。
    deadline = time.time() + 10
    while time.time() < deadline and len(_read_argv_log(argv_log)) < 2:
        time.sleep(0.05)
    assert len(_read_argv_log(argv_log)) >= 2, "継続 attempt が始まらない（テスト前提が崩れている）"
    stop_event.set()

    th.join(timeout=10)
    assert not th.is_alive(), "stop_event 経路が想定時間内に完走しない（_spawn_stop_watcher が効いていない疑い）"

    env = _result_env(events)
    assert not env.get("codex_stopped_early"), "利用者の明示停止なのに codex_stopped_early が立っている"


# ===== 7. 継続 attempt が無出力・異常終了（resume 失敗や CLI のクラッシュ） =====

def test_continuation_attempt_without_output_marks_stopped_early(tmp_path, monkeypatch):
    """継続 attempt が JSON を1行も出さず非ゼロ終了しても、蓄積済みの作業宣言を注記なしの最終回答に
    しない＝「途中までの結果」の印（codex_stopped_early）を立てて続きを促す。新規セッションへの
    フォールバックはしない（呼び出しは初回＋継続の 2 回で止まる）。"""
    steps = [
        {"thread_id": "TH-CRASH", "agent_messages": ["まず資料を確認します。"], "usage": _usage()},
        {"exit_code": 1},   # 無出力・exit 1
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_crash")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-crash", conversation_id=907)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"初回＋継続1回で止まるはず（フォールバック再試行なし）: {calls!r}"
    assert "resume" in calls[1] and calls[1][-1] == A._CONTINUE_PROMPT
    assert env["headline"] == "まず資料を確認します。"     # 本文は書き換えない
    assert env.get("codex_stopped_early") is True
    finalized = CS._finalize(dict(env), {"lens": "qa", "reason": "既定（検索）"})
    assert len([h for h in finalized.get("retry_hints", []) if h["kind"] == "resume"]) == 1


# ===== 8. 「作業報告＋次アクション」型の途中経過（実環境で観測された文型） =====

def test_needs_continuation_report_plus_next_action_is_progress():
    assert A._needs_continuation(["現在、関連資料を確認しました。次に影響範囲を調べます。"])
    assert A._needs_continuation(["資料を検索しました。続いて呼び出し元をたどります。"])


def test_needs_continuation_findings_or_report_alone_are_not_progress():
    # 所見（〜ます／〜です で終わる事実）を含むなら結論＝続けない
    assert not A._needs_continuation(["税率は起動時に読み込まれます。次に影響範囲を調べます。"])
    # 作業報告だけで次アクションが無い＝結論として扱う（見出しの選び方と同じ）
    assert not A._needs_continuation(["関連資料を確認しました。"])
    # 単文の事実記述（語尾が「確認します」でも作業報告文を伴わない）＝結論として扱う（_is_progress_only と同じ安全側）
    assert not A._needs_continuation(["夜間バッチ NIGHTLY は税率マスタを起動時に確認します。"])


def test_report_plus_next_action_message_triggers_continuation(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-REPORT", "agent_messages": ["現在、関連資料を確認しました。次に影響範囲を調べます。"],
         "usage": _usage()},
        {"thread_id": "TH-REPORT", "agent_messages": ["影響範囲は夜間バッチの 2 ジョブです。"],
         "usage": _usage(20, 0, 10, 0)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_report")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-report", conversation_id=908)

    env = _result_env(_run(prov, ctx))

    assert len(_read_argv_log(argv_log)) == 2
    assert env["headline"] == "影響範囲は夜間バッチの 2 ジョブです。"
    assert not env.get("codex_stopped_early")


# ===== 9. _needs_continuation は message 境界をまたいで判定する =====

def test_needs_continuation_report_and_next_action_split_across_messages():
    # 「資料を確認しました」（単文なら結論扱い）と「次に調べます」が**別 message**でも、連結すれば
    # 作業報告＋次アクションとして継続対象になる（message 単位の判定では前者が結論扱いで見落としていた）。
    assert A._needs_continuation(
        ["資料を確認しました。", "次に影響範囲を調べます。"])


def test_needs_continuation_fact_then_next_action_split_across_messages():
    # 「NIGHTLY は税率マスタを起動時に確認します。」は単文・手順マーカー無し・進行形語尾の事実記述
    # ＝ message 単位の保護で即結論扱い（False）。後続 message に「次に調べます。」という次アクション
    # 宣言があっても、連結前の単文保護が先に効くため継続対象にはならない（結論を含む attempt を
    # 完成扱いにする＝過去の作業宣言との連結で未完了化しないことの回帰防止）。
    assert not A._needs_continuation(
        ["NIGHTLY は税率マスタを起動時に確認します。", "次に調べます。"])


def test_needs_continuation_false_when_conclusion_split_across_messages():
    assert not A._needs_continuation(
        ["資料を確認しました。", "確認した結果、影響はありません。"])


# ===== 10. `-o` 最終メッセージだけに結論が出るケースを継続ループが拾う =====

def test_continuation_recovers_conclusion_from_last_message_file(tmp_path, monkeypatch):
    """継続 attempt が `--json` に agent_message を出さず `-o` にだけ結論を書いた場合でも、
    毎 attempt 直後に `-o` を吸収するため継続条件が解消し、上限まで回らず正しい結論で止まる。"""
    steps = [
        {"thread_id": "TH-LM", "agent_messages": ["まず資料を確認します。"], "usage": _usage()},
        {"thread_id": "TH-LM", "last_message": "資料を確認した結果、影響はありません。",
         "usage": _usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_lastmsg")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-lastmsg", conversation_id=909)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"作業宣言→(-oのみ)結論で2回で止まるはず: {calls!r}"
    assert env["headline"] == "資料を確認した結果、影響はありません。"
    assert not env.get("codex_stopped_early")


# ===== 11. 継続 attempt がツールを1つも呼ばなければ打ち切る（正常な手順説明への無駄打ち防止） =====

def test_continuation_without_tool_calls_stops_after_one_extra_attempt(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-NOTOOL", "agent_messages": ["まず確認します。次に調べます。"], "usage": _usage()},
        {"thread_id": "TH-NOTOOL", "agent_messages": ["まず確認します。次に調べます。"], "usage": _usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_notool")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-notool", conversation_id=910)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"継続 attempt がツール無しなら初回＋継続1回で打ち切るはず: {calls!r}"
    assert env.get("codex_stopped_early") is True


# ===== 12. 継続前の item id が上書きされない（attempt ごとに id 空間を分離） =====

def test_continuation_attempt_item_ids_get_attempt_prefix(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-IID", "agent_messages": ["まず資料を確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
        {"thread_id": "TH-IID", "agent_messages": ["続いて関連ファイルを確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_iid")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "1")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-iid", conversation_id=911)

    events = _run(prov, ctx)

    assert len(_read_argv_log(argv_log)) == 2
    node_ids = [e.get("id") for e in events if isinstance(e, dict) and e.get("type") == "node"]
    assert "cx-item_0" in node_ids, f"初回 attempt の id が現行どおりでない: {node_ids!r}"
    assert "cx-a2-item_0" in node_ids, f"継続 attempt の id が接頭辞で分離されていない: {node_ids!r}"


# ===== 13. 過去 attempt の作業宣言と連結して、完成した結論を未完了扱いにしない =====

def test_completed_answer_after_earlier_attempt_preamble_ends_without_extra_turns(tmp_path, monkeypatch):
    """初回 attempt の作業宣言（「まず仕様書を確認します。」）は attempt をまたいで `_agent_msgs` に
    蓄積されるが、2回目 attempt の判定は最新 attempt の message だけを見る。2回目が単文の事実記述
    （進行形の語尾でも手順マーカー無し＝結論扱い）で終われば、初回の宣言と連結して継続対象と
    誤判定しない。"""
    conclusion = "NIGHTLY は税率マスタを起動時に確認します。"
    steps = [
        {"thread_id": "TH-CROSS", "agent_messages": ["まず仕様書を確認します。"], "usage": _usage()},
        {"thread_id": "TH-CROSS", "agent_messages": [conclusion],
         "tool_ids": ["item_0"], "usage": _usage(20)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_cross_attempt")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-cross-attempt", conversation_id=912)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"作業宣言→結論で2回のはず（3回目は起きない）: {calls!r}"
    assert env["headline"] == conclusion
    assert not env.get("codex_stopped_early")


# ===== 14. ネイティブ Web 検索もツール実行として数え、継続の打ち切りを誤らない =====

@pytest.mark.parametrize("item_type", ["web_search", "file_change"])
def test_native_web_search_counts_as_tool_execution_for_continuation(tmp_path, monkeypatch, item_type):
    """継続 attempt が `command_execution`／`mcp_tool_call` を1つも呼ばずとも、Codex CLI ネイティブの
    `web_search`／`file_change` item を出せば「ツールを実行した」とみなし、ツール未実行での打ち切り
    （`test_continuation_without_tool_calls_stops_after_one_extra_attempt`）を誤発動しない。"""
    final = "公式資料によると、設定 A が接続先を指定します。"
    steps = [
        {"thread_id": "TH-WEB", "agent_messages": ["まず公式資料を調べます。"], "usage": _usage()},
        {"thread_id": "TH-WEB", "agent_messages": ["次に見つかった資料を確認します。"],
         "extra_events": [{"type": "item.completed", "item": {
             "id": "item_0", "type": item_type, "query": "OpenAI official documentation",
             "action": {"type": "search", "query": "OpenAI official documentation"}}}],
         "usage": _usage(20)},
        {"thread_id": "TH-WEB", "agent_messages": [final], "usage": _usage(30)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname=f"users_continue_{item_type}")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "3")
    prov = A.CodexProvider(web_search=True, system_settings={"web_search_allowed": True})
    ctx = _ctx(uid="auto-continue-web-search", conversation_id=913,
              message="公式資料を Web で調べ、設定を説明してください。")

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 3, f"web_search item もツール実行扱いで3回目まで進むはず: {calls!r}"
    assert env["headline"] == final
    assert not env.get("codex_stopped_early")


# ===== attempt 境界: 前 attempt の未完 message と過去と同文の -o が最新 attempt の判定に混ざらない =====

def test_partial_message_from_previous_attempt_does_not_leak_into_continuation_judgment(tmp_path, monkeypatch):
    """初回が completed「確認しました」＋updated だけの「次に調べます」（completed 無し）で止まり、
    継続 attempt がツール実行＋ `-o` だけの結論で終わる場合、前 attempt の未完 message を判定に
    持ち越さず、2回で結論に止まる（`codex_stopped_early` 無し）。"""
    final = "依存先を確認しました。"
    steps = [
        {"thread_id": "TH-PART", "agent_messages": ["資料を確認しました。"],
         "extra_events": [{"type": "item.updated", "item": {
             "id": "m-part", "type": "agent_message", "text": "次に影響範囲を調べます。"}}],
         "usage": _usage()},
        {"thread_id": "TH-PART", "last_message": final,
         "extra_events": [{"type": "item.completed", "item": {
             "id": "item_0", "type": "command_execution", "command": "grep -rn NIGHTLY .",
             "status": "completed", "exit_code": 0}}],
         "usage": _usage(20)},
        {"thread_id": "TH-PART", "agent_messages": [final], "usage": _usage(30)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_partial_leak")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "3")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-partial-leak", conversation_id=914)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"前 attempt の未完 message を持ち越さず 2 回で止まるはず: {calls!r}"
    assert env["headline"] == final
    assert not env.get("codex_stopped_early")


def test_last_message_identical_to_earlier_attempt_still_counts_for_latest_attempt(tmp_path, monkeypatch):
    """継続 attempt の `-o` が過去 attempt の completed message と同文でも、最新 attempt の分として
    判定に入る（過去との重複を理由に落とすと最新 attempt が無出力扱いになり、初回の次アクション
    まで再評価されて余分な継続／`codex_stopped_early` になる）。"""
    conclusion = "接続先が DB-B であることを確認しました。"
    steps = [
        {"thread_id": "TH-SAME", "agent_messages": [conclusion, "次に影響範囲を調べます。"], "usage": _usage()},
        {"thread_id": "TH-SAME", "last_message": conclusion,
         "extra_events": [{"type": "item.completed", "item": {
             "id": "item_0", "type": "command_execution", "command": "grep -rn DB-B .",
             "status": "completed", "exit_code": 0}}],
         "usage": _usage(20)},
        {"thread_id": "TH-SAME", "agent_messages": [conclusion], "usage": _usage(30)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_same_lastmsg")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "3")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-same-lastmsg", conversation_id=915)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"過去と同文の -o でも最新 attempt の結論として 2 回で止まるはず: {calls!r}"
    assert env["headline"] == conclusion
    assert not env.get("codex_stopped_early")


def test_stale_last_message_from_previous_attempt_is_not_absorbed_as_new_answer(tmp_path, monkeypatch):
    """継続 attempt が `-o` に何も書かずに終わった場合、前 attempt が残した `-o` の内容を最新 attempt の
    回答として吸収しない（吸収すると未完了のまま「完了」扱いになり `codex_stopped_early` が消える）。"""
    steps = [
        {"thread_id": "TH-STALE", "agent_messages": ["資料を確認しました。"],
         "last_message": "資料を確認しました。",
         "extra_events": [{"type": "item.updated", "item": {
             "id": "m-part", "type": "agent_message", "text": "次に調べます。"}}],
         "usage": _usage()},
        {"thread_id": "TH-STALE", "usage": _usage(20)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_stale_lastmsg")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "3")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-stale-lastmsg", conversation_id=916)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"無出力の継続はツール未実行で打ち切られるはず: {calls!r}"
    assert env.get("codex_stopped_early"), "前 attempt の stale な -o を結論扱いしてはいけない"


def test_stale_last_message_is_skipped_when_cleanup_fails(tmp_path, monkeypatch):
    """attempt 開始時の `-o` 削除が権限エラー等で失敗しても、残った前 attempt の本文を最新 attempt の
    回答として吸収しない（吸収すると未完了なのに `codex_stopped_early` が消える）。"""
    import pathlib

    real_unlink = pathlib.Path.unlink

    def _deny_last_message_unlink(self, missing_ok=False):
        if self.name.startswith("last-message-") and self.exists():
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", _deny_last_message_unlink)
    steps = [
        {"thread_id": "TH-STALE2", "agent_messages": ["資料を確認しました。"],
         "last_message": "資料を確認しました。",
         "extra_events": [{"type": "item.updated", "item": {
             "id": "m-part", "type": "agent_message", "text": "次に調べます。"}}],
         "usage": _usage()},
        {"thread_id": "TH-STALE2", "usage": _usage(20)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_continue_stale_unlink_fail")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "3")
    prov = A.CodexProvider()
    ctx = _ctx(uid="auto-continue-stale-unlink-fail", conversation_id=917)

    env = _result_env(_run(prov, ctx))

    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"無出力の継続はツール未実行で打ち切られるはず: {calls!r}"
    assert env.get("codex_stopped_early"), "消せなかった前 attempt の -o を結論扱いしてはいけない"


def test_needs_continuation_detects_report_back_declaration():
    """「これから…確認し、結果を報告します。」のような「調べてから伝える」宣言も途中経過として拾う
    （手順マーカー付き・語尾が報告系）。マーカー無しの単文「以下に報告します。」は事実記述として保護。"""
    assert A._needs_continuation(["これから関連資料を確認し、結果を報告します。"])
    assert A._needs_continuation(["まず影響範囲を洗い出し、結果をまとめます。"])
    assert not A._needs_continuation(["調査結果を以下に報告します。"])
    assert not A._needs_continuation(["NIGHTLY は税率マスタの変更を日次で共有します。"])


def test_report_back_verbs_do_not_trim_content_sentences_from_headline():
    """報告系語尾は手順マーカー付きの宣言でだけ次アクション扱い。結論の末尾文（「〜を共有します」）は
    `_pick_codex_headline`（`_trim_trailing_progress`）で落とされない。"""
    full = "原因は設定漏れです。影響範囲は夜間バッチのみであることを共有します。"
    assert A._pick_codex_headline([full]) == full
    assert not A._needs_continuation([full])


def test_report_back_with_saigo_ni_marker_is_a_conclusion():
    """「最後に、…を共有します。」は結論の締め＝次アクションではない（最新結論を捨てず・継続しない）。"""
    conclusion = "最後に、影響は夜間バッチのみであることを共有します。"
    assert A._pick_codex_headline(["影響は日中 API にもあります。", conclusion]) == conclusion
    assert not A._needs_continuation([conclusion])


def test_mcp_read_docs_collected_across_attempts_even_when_item_ids_repeat(tmp_path, monkeypatch):
    """S2: 自動継続（別 codex exec プロセス）では item id が振り直される＝同じ id の read_doc でも
    継続側の別資料を取りこぼさない（収集済み id の記憶は attempt ごと）。"""
    def _read(doc):
        return {"type": "item.completed", "item": {"id": "item_0", "type": "mcp_tool_call", "tool": "read_doc",
                                                    "status": "completed", "arguments": {"doc_id": doc}}}
    steps = [
        {"thread_id": "TH-S2", "agent_messages": ["まず資料を確認します。"],
         "extra_events": [_read("4期/02_設計/01_基本設計/税計算仕様書.md")], "usage": _usage(10, 2, 5, 1)},
        {"thread_id": "TH-S2", "agent_messages": ["確認した結果、消費税率は10%です。"],
         "extra_events": [_read("4期/01_標準/消費税法.md")], "usage": _usage(30, 2, 13, 3)},
    ]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_s2_attempts")
    prov = A.CodexProvider()
    ctx = _ctx(uid="s2-attempts", conversation_id=902)
    ctx.make_sources = lambda docs: [{"doc_id": d, "download_url": f"/dl?rel={d}"} for d in docs]
    env = _result_env(_run(prov, ctx))
    assert env["codex_referenced_docs"]["listed"] == 2
    assert {s["doc_id"] for s in env["sources"]} >= {"4期/02_設計/01_基本設計/税計算仕様書.md", "4期/01_標準/消費税法.md"}
