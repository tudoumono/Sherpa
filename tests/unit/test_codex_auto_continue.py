"""Codex 自動継続（作業宣言だけで止まったターンを resume で押し進める）と、サイドカー・縮退の契約。

偽 codex を PATH に差し込む（実 codex は呼ばない）。呼び出し回数ごとの応答計画を JSON で渡し、
呼び出し回数は argv_log の行数で数える。`_setup`/`_ctx`/`_run`/`_result_env`/`_read_argv_log`/`_usage`
は test_codex_output_schema.py・test_codex_ledger_gate.py が helper として再利用する。
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402
from sherpa.providers.codex import continuation as CONT  # noqa: E402
from sherpa import chat_service as CS  # noqa: E402

_FAKE_CODEX_MULTI_PY = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

argv_log = pathlib.Path(r"__ARGV_LOG__")
plan_path = pathlib.Path(r"__PLAN_PATH__")
args = sys.argv[1:]
if args and args[-1] == "-":   # プロンプトは argv でなく標準入力から渡される（fake codex 側も同じ規約に合わせる）
    args = args[:-1] + [sys.stdin.read()]
with argv_log.open("a", encoding="utf-8") as f:
    f.write(repr(args) + "\n")
    f.flush()

# 呼び出し回数（自分自身の行を含む・1始まり）＝この起動が何回目かを argv_log の行数で数える。
call_index = len(argv_log.read_text(encoding="utf-8").splitlines())
steps = json.loads(plan_path.read_text(encoding="utf-8"))["steps"]
step = steps[call_index - 1] if call_index - 1 < len(steps) else steps[-1]

sidecar = step.get("sidecar")
if sidecar:
    # サイドカーは codex_home 配下（CODEX_HOME env・run_dir の外＝s3b-fix2）に置かれる契約。
    (pathlib.Path(os.environ["CODEX_HOME"]) / ".mcp_sidecar.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in sidecar) + "\n")

# C17 是正テスト用: model-shell が書込全開の run_dir（`-C` 引数）へ直接偽のサイドカーを
# 書く経路（MCP サーバの env 経由ではなく shell ツールが直接書く想定の再現）。
sidecar_at_rundir = step.get("sidecar_at_rundir")
if sidecar_at_rundir:
    run_dir = pathlib.Path(args[args.index("-C") + 1])
    (run_dir / ".mcp_sidecar.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in sidecar_at_rundir) + "\n")

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
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path: Path, monkeypatch, steps: list, users_dirname: str) -> Path:
    """偽 codex を PATH に差し込み、応答計画（steps）を JSON で渡す。戻り値は argv_log。
    平文 agent_message の語尾ヒューリスティックを検証するため出力スキーマは 0 に固定する。"""
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
        codex_session_id=codex_session_id,
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


def _drive(tmp_path, monkeypatch, steps, uid, cid, *, env=None, prov=None, ctx=None):
    """偽 codex を設定して 1 ターン走らせる。戻り値は (events, env, calls)。"""
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname=f"users_{uid}")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    events = _run(prov or A.CodexProvider(), ctx or _ctx(uid=uid, conversation_id=cid))
    return events, _result_env(events), _read_argv_log(argv_log)


def _question(qid: str, prompt: str) -> dict:
    return {"type": "question", "interaction_id": qid, "mode": "single", "prompt": prompt,
            "allow_free_text": False, "options": [{"id": "yes", "label": "はい", "description": ""}]}


def _questions(events):
    return [e for e in events if isinstance(e, dict) and e.get("type") == "question"]


_SAFE_ANSWER = "確認した結果、問題ありません。"


# ===== _needs_continuation（純関数） =====

@pytest.mark.parametrize("msgs,partial,expected", [
    (["まず資料を確認します。", "次に影響範囲を確認します。"], "", True),
    ([], "まず資料を確認します", True),
    (["現在、関連資料を確認しました。次に影響範囲を調べます。"], "", True),
    (["資料を検索しました。続いて呼び出し元をたどります。"], "", True),
    # 別 message でも連結すれば作業報告＋次アクション
    (["資料を確認しました。", "次に影響範囲を調べます。"], "", True),
    (["これから関連資料を確認し、結果を報告します。"], "", True),
    (["まず影響範囲を洗い出し、結果をまとめます。"], "", True),
    # 結論あり・空・所見つき・作業報告のみ・単文の事実記述は続けない
    (["まず資料を確認します。", "確認した結果、影響はありません。"], "", False),
    ([], "", False),
    (["", "   "], "", False),
    (["税率は起動時に読み込まれます。次に影響範囲を調べます。"], "", False),
    (["関連資料を確認しました。"], "", False),
    (["夜間バッチ NIGHTLY は税率マスタを起動時に確認します。"], "", False),
    (["NIGHTLY は税率マスタを起動時に確認します。", "次に調べます。"], "", False),
    (["資料を確認しました。", "確認した結果、影響はありません。"], "", False),
    (["調査結果を以下に報告します。"], "", False),
    (["NIGHTLY は税率マスタの変更を日次で共有します。"], "", False),
    (["最後に、影響は夜間バッチのみであることを共有します。"], "", False),
])
def test_needs_continuation(msgs, partial, expected):
    assert bool(CONT._needs_continuation(msgs, partial=partial)) is expected


def test_report_back_verbs_do_not_trim_content_sentences_from_headline():
    full = "原因は設定漏れです。影響範囲は夜間バッチのみであることを共有します。"
    assert CONT._pick_codex_headline([full]) == full
    conclusion = "最後に、影響は夜間バッチのみであることを共有します。"
    assert CONT._pick_codex_headline(["影響は日中 API にもあります。", conclusion]) == conclusion


# ===== 継続して結論を拾う／止まる条件 =====

def test_continues_once_and_headline_becomes_the_conclusion(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-FRESH", "agent_messages": ["まず資料を確認します。"],
         "usage": _usage(10, 2, 5, 1)},
        # 継続側の usage はセッション累計（Codex CLI の契約）
        {"thread_id": "TH-FRESH", "agent_messages": ["資料を確認した結果、影響はありません。"],
         "usage": _usage(30, 2, 13, 3)},
    ]
    events, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-ok", 901)

    assert env["headline"] == "資料を確認した結果、影響はありません。"
    assert not env.get("codex_stopped_early")
    assert env.get("codex_session_id") == "TH-FRESH"
    assert len(calls) == 2
    assert "resume" in calls[1] and "TH-FRESH" in calls[1]
    assert calls[1][-1] == CONT._CONTINUE_PROMPT
    usage = env["usage"]   # 累計の最新 snapshot（足し算しない）
    assert {k: usage[k] for k in _usage()} == _usage(30, 2, 13, 3)
    assert "cx-continue-1" in [e.get("id") for e in events if isinstance(e, dict) and e.get("type") == "node"]
    assert env["limits"]["auto_continues"] == 1


def test_continuation_stops_at_limit_and_sets_stopped_early_flag(tmp_path, monkeypatch):
    # 継続 attempt はツールを呼ぶ（ツール無し打ち切りの対象外）ので上限まで回る
    steps = [
        {"thread_id": "TH-CAP", "agent_messages": ["まず資料を確認します。"], "usage": _usage()},
        {"thread_id": "TH-CAP", "agent_messages": ["次に影響範囲を確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
        {"thread_id": "TH-CAP", "agent_messages": ["続いて関連ファイルを確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-cap", 902,
                           env={"SHERPA_CODEX_AUTO_CONTINUE": "2"})
    assert len(calls) == 3
    assert env.get("codex_stopped_early") is True
    assert env["body"] == "続いて関連ファイルを確認します。"   # 本文は書き換えない
    assert [n["kind"] for n in env["notices"]] == ["stopped_early"]   # 途中までの説明は注記として残る
    assert env["headline"].endswith(env["body"]) and "途中までの結果" in env["headline"]
    finalized = CS._finalize(dict(env), {"lens": "qa", "reason": "既定（検索）"})
    assert len([h for h in finalized.get("retry_hints", []) if h["kind"] == "resume"]) == 1


@pytest.mark.parametrize("label,cid,env_vars", [
    ("limit0", 903, {"SHERPA_CODEX_AUTO_CONTINUE": "0"}),
    ("nopersist", None, {}),
])
def test_no_continuation_still_sets_stopped_early_flag(tmp_path, monkeypatch, label, cid, env_vars):
    """上限 0・セッション永続なし（conversation_id=None）は継続せず、途中経過の印を立てる。"""
    steps = [{"thread_id": "TH-OFF", "agent_messages": ["まず資料を確認します。"], "usage": _usage()}]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, f"auto-continue-{label}", cid, env=env_vars)
    assert len(calls) == 1
    assert env.get("codex_stopped_early") is True


def test_no_continuation_when_conclusion_arrives_on_first_attempt(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-DIRECT", "agent_messages": ["確認した結果、影響はありません。"],
              "usage": _usage()}]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-direct", 904)
    assert len(calls) == 1
    assert not env.get("codex_stopped_early")
    assert env["headline"] == "確認した結果、影響はありません。"
    assert "limits" not in env


# ===== サイドカー（`.mcp_sidecar.jsonl`） =====

def _write_stale_sidecar(tmp_path, users_dirname, uid, conv_id, lines) -> Path:
    codex_home = tmp_path / users_dirname / uid / "workspace" / ".codex-sessions" / str(conv_id)
    codex_home.mkdir(parents=True)
    (codex_home / ".mcp_sidecar.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
    return codex_home


def test_sidecar_deleted_at_turn_end_same_finally_as_codex_home(tmp_path, monkeypatch):
    """サイドカーは config.toml/auth.json と同じ finally でターンごとに削除される（次ターンへ doc_id を持ち越さない）。"""
    doc = "4期/01_標準/消費税法.md"
    steps = [{"thread_id": "TH-SIDECAR-TTL", "agent_messages": ["確認した結果、影響はありません。"],
              "sidecar": [{"kind": "read", "tool": "read_doc", "doc_id": doc, "ts": 1.0}],
              "usage": _usage()}]
    _, env, _ = _drive(tmp_path, monkeypatch, steps, "sidecar-ttl-u1", 905)
    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    codex_home = tmp_path / "users_sidecar-ttl-u1" / "sidecar-ttl-u1" / "workspace" / ".codex-sessions" / "905"
    assert codex_home.is_dir()
    assert not (codex_home / ".mcp_sidecar.jsonl").exists()
    assert not (codex_home / "config.toml").exists()


def test_sandbox_disabled_does_not_absorb_forged_sidecar_at_run_dir(tmp_path, monkeypatch):
    """`SHERPA_CODEX_SANDBOX=0` では run_dir 直下の偽サイドカー（ask_user・未読 doc_id）を取り込まない。"""
    steps = [{"thread_id": "TH-FORGE", "agent_messages": [_SAFE_ANSWER],
              "sidecar_at_rundir": [
                  {"kind": "ask_user", "ts": 1.0, "question": _question("forged-q1", "偽装された確認")},
                  {"kind": "read", "tool": "read_doc", "doc_id": "秘匿/未読資料.md", "ts": 1.0},
              ],
              "usage": _usage()}]
    events, env, _ = _drive(tmp_path, monkeypatch, steps, "sandbox-off-forge-u1", None,
                            env={"SHERPA_CODEX_SANDBOX": "0"})
    assert _questions(events) == []
    assert env["headline"] == _SAFE_ANSWER
    assert (env.get("codex_referenced_docs") or {"listed": 0})["listed"] == 0


def test_stale_sidecar_from_prior_turn_not_absorbed_at_turn_start(tmp_path, monkeypatch):
    """前ターンが finally を経ずに残したサイドカー残骸は、ターン開始時に空から始めて吸収しない。"""
    uid, cid = "sidecar-stale-u1", 906
    steps = [{"thread_id": "TH-STALE", "agent_messages": [_SAFE_ANSWER], "usage": _usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_sidecar_stale")
    _write_stale_sidecar(tmp_path, "users_sidecar_stale", uid, cid, [
        {"kind": "ask_user", "ts": 1.0, "question": _question("stale-q1", "前ターンの残骸確認")},
        {"kind": "read", "tool": "read_doc", "doc_id": "前ターン/残骸.md", "ts": 1.0}])
    events = _run(A.CodexProvider(), _ctx(uid=uid, conversation_id=cid))
    assert _questions(events) == []
    env = _result_env(events)
    assert env["headline"] == _SAFE_ANSWER
    assert (env.get("codex_referenced_docs") or {"listed": 0})["listed"] == 0


def test_sidecar_preunlink_permission_error_disables_absorb_and_warns_without_content(
        tmp_path, monkeypatch, caplog):
    """残骸サイドカーの事前 unlink が失敗したターンは取り込みを無効化し、警告は例外型と errno だけ（資料パス・質問文面・パスを含まない）。"""
    doc = "4期/01_標準/消費税法.md"
    uid, cid = "sidecar-stale-perm-u1", 951
    steps = [{"thread_id": "TH-STALE-PERM", "agent_messages": [_SAFE_ANSWER], "usage": _usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_sidecar_stale_perm")
    codex_home = _write_stale_sidecar(tmp_path, "users_sidecar_stale_perm", uid, cid, [
        {"kind": "ask_user", "ts": 1.0, "question": _question("stale-perm-q1", "前ターンの残骸確認")},
        {"kind": "read", "tool": "read_doc", "doc_id": doc, "ts": 1.0}])

    orig_unlink = Path.unlink

    def _unlink_maybe_fail(self, *a, **k):
        if self.name == ".mcp_sidecar.jsonl":
            raise PermissionError(13, "Permission denied")
        return orig_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _unlink_maybe_fail)
    with caplog.at_level("WARNING", logger="sherpa"):
        events = _run(A.CodexProvider(), _ctx(uid=uid, conversation_id=cid))

    assert _questions(events) == []
    env = _result_env(events)
    assert env["headline"] == _SAFE_ANSWER
    assert (env.get("codex_referenced_docs") or {"listed": 0})["listed"] == 0
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "pre-unlink" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "PermissionError" in msg and "errno" in msg
    assert doc not in msg and "前ターンの残骸確認" not in msg and str(codex_home) not in msg


def test_config_write_failure_leaves_sidecar_absorb_disabled(tmp_path, monkeypatch):
    """設定生成が失敗して attempt が一度も走らないターンでは、サイドカーを読まず codex も呼ばない。"""
    steps = [{"thread_id": "TH-SIDECAR", "agent_messages": ["確認しました。"], "usage": _usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps=steps, users_dirname="users_config_boom_sidecar")
    from sherpa.providers.codex import sandbox as sandbox_mod
    from sherpa.providers.codex import usage as usage_mod
    from sherpa.providers.prompts import _NO_PRESEARCH_HEADLINE

    read_calls: list = []
    orig_read = usage_mod._read_mcp_sidecar
    orig_write = sandbox_mod._write_codex_authoring_config
    boom_calls: list = []

    def _spy_read_sidecar(path):
        read_calls.append(path)
        return orig_read(path)

    def _config_boom(*a, **k):
        boom_calls.append(1)
        raise RuntimeError("config generation boom")

    monkeypatch.setattr(sandbox_mod, "_write_codex_authoring_config", _config_boom)
    monkeypatch.setattr(usage_mod, "_read_mcp_sidecar", _spy_read_sidecar)

    env = _result_env(_run(A.CodexProvider(), _ctx(uid="config-boom-sidecar-u1", conversation_id=952)))
    assert _NO_PRESEARCH_HEADLINE in env["headline"]
    assert "エラー" in env["headline"]
    assert boom_calls == [1]
    assert _read_argv_log(argv_log) == []
    assert read_calls == []

    # 差し替えた読取が効いていること（設定生成が通るターンでは、同じ差し替えでサイドカーを読む）。
    monkeypatch.setattr(sandbox_mod, "_write_codex_authoring_config", orig_write)
    _result_env(_run(A.CodexProvider(), _ctx(uid="config-boom-sidecar-u2", conversation_id=953)))
    assert len(_read_argv_log(argv_log)) == 1
    assert read_calls


# ===== 継続中の明示停止・無出力・attempt 境界 =====

def test_user_stop_during_continuation_does_not_set_stopped_early(tmp_path, monkeypatch):
    """継続 attempt の途中の明示停止（stop_event）は codex_stopped_early を立てない。"""
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

    th = threading.Thread(target=lambda: events.extend(prov.run(ctx)), daemon=True)
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and len(_read_argv_log(argv_log)) < 2:
        time.sleep(0.05)
    assert len(_read_argv_log(argv_log)) >= 2
    stop_event.set()
    th.join(timeout=10)
    assert not th.is_alive()
    assert not _result_env(events).get("codex_stopped_early")


def test_continuation_attempt_without_output_marks_stopped_early(tmp_path, monkeypatch):
    """継続 attempt が無出力・非ゼロ終了なら途中結果の印を立て、新規セッションへフォールバックしない。"""
    steps = [
        {"thread_id": "TH-CRASH", "agent_messages": ["まず資料を確認します。"], "usage": _usage()},
        {"exit_code": 1},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-crash", 907)
    assert len(calls) == 2
    assert "resume" in calls[1] and calls[1][-1] == CONT._CONTINUE_PROMPT
    assert "まず資料を確認します。" in env["headline"]
    assert "失敗" in env["headline"]
    assert env.get("codex_stopped_early") is True
    finalized = CS._finalize(dict(env), {"lens": "qa", "reason": "既定（検索）"})
    assert len([h for h in finalized.get("retry_hints", []) if h["kind"] == "resume"]) == 1


def test_report_plus_next_action_message_triggers_continuation(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-REPORT", "agent_messages": ["現在、関連資料を確認しました。次に影響範囲を調べます。"],
         "usage": _usage()},
        {"thread_id": "TH-REPORT", "agent_messages": ["影響範囲は夜間バッチの 2 ジョブです。"],
         "usage": _usage(20, 0, 10, 0)},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-report", 908)
    assert len(calls) == 2
    assert env["headline"] == "影響範囲は夜間バッチの 2 ジョブです。"
    assert not env.get("codex_stopped_early")


def test_continuation_recovers_conclusion_from_last_message_file(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-LM", "agent_messages": ["まず資料を確認します。"], "usage": _usage()},
        {"thread_id": "TH-LM", "last_message": "資料を確認した結果、影響はありません。", "usage": _usage()},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-lastmsg", 909)
    assert len(calls) == 2
    assert env["headline"] == "資料を確認した結果、影響はありません。"
    assert not env.get("codex_stopped_early")


def test_continuation_without_tool_calls_stops_after_one_extra_attempt(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-NOTOOL", "agent_messages": ["まず確認します。次に調べます。"], "usage": _usage()},
        {"thread_id": "TH-NOTOOL", "agent_messages": ["まず確認します。次に調べます。"], "usage": _usage()},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-notool", 910)
    assert len(calls) == 2
    assert env.get("codex_stopped_early") is True


def test_continuation_attempt_item_ids_get_attempt_prefix(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-IID", "agent_messages": ["まず資料を確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
        {"thread_id": "TH-IID", "agent_messages": ["続いて関連ファイルを確認します。"],
         "tool_ids": ["item_0"], "usage": _usage()},
    ]
    events, _, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-iid", 911,
                              env={"SHERPA_CODEX_AUTO_CONTINUE": "1"})
    assert len(calls) == 2
    node_ids = [e.get("id") for e in events if isinstance(e, dict) and e.get("type") == "node"]
    assert "cx-item_0" in node_ids and "cx-a2-item_0" in node_ids


def test_completed_answer_after_earlier_attempt_preamble_ends_without_extra_turns(tmp_path, monkeypatch):
    """前 attempt の作業宣言と連結して、後続 attempt の単文の結論を未完了と誤判定しない。"""
    conclusion = "NIGHTLY は税率マスタを起動時に確認します。"
    steps = [
        {"thread_id": "TH-CROSS", "agent_messages": ["まず仕様書を確認します。"], "usage": _usage()},
        {"thread_id": "TH-CROSS", "agent_messages": [conclusion],
         "tool_ids": ["item_0"], "usage": _usage(20)},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-cross-attempt", 912)
    assert len(calls) == 2
    assert env["headline"] == conclusion
    assert not env.get("codex_stopped_early")


@pytest.mark.parametrize("item_type", ["web_search", "file_change"])
def test_native_web_search_counts_as_tool_execution_for_continuation(tmp_path, monkeypatch, item_type):
    """web_search／file_change item もツール実行扱いで、ツール未実行の打ち切りを誤発動しない。"""
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
    prov = A.CodexProvider(web_search=True, system_settings={"web_search_allowed": True})
    ctx = _ctx(uid="auto-continue-web-search", conversation_id=913,
               message="公式資料を Web で調べ、設定を説明してください。")
    _, env, calls = _drive(tmp_path, monkeypatch, steps, f"auto-continue-{item_type}", 913,
                           env={"SHERPA_CODEX_AUTO_CONTINUE": "3"}, prov=prov, ctx=ctx)
    assert len(calls) == 3
    assert env["headline"] == final
    assert not env.get("codex_stopped_early")


def _item_cmd(cmd):
    return {"type": "item.completed", "item": {
        "id": "item_0", "type": "command_execution", "command": cmd, "status": "completed", "exit_code": 0}}


def test_partial_message_from_previous_attempt_does_not_leak_into_continuation_judgment(tmp_path, monkeypatch):
    final = "依存先を確認しました。"
    steps = [
        {"thread_id": "TH-PART", "agent_messages": ["資料を確認しました。"],
         "extra_events": [{"type": "item.updated", "item": {
             "id": "m-part", "type": "agent_message", "text": "次に影響範囲を調べます。"}}],
         "usage": _usage()},
        {"thread_id": "TH-PART", "last_message": final,
         "extra_events": [_item_cmd("grep -rn NIGHTLY .")], "usage": _usage(20)},
        {"thread_id": "TH-PART", "agent_messages": [final], "usage": _usage(30)},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-partial-leak", 914,
                           env={"SHERPA_CODEX_AUTO_CONTINUE": "3"})
    assert len(calls) == 2
    assert env["headline"] == final
    assert not env.get("codex_stopped_early")


def test_last_message_identical_to_earlier_attempt_still_counts_for_latest_attempt(tmp_path, monkeypatch):
    conclusion = "接続先が DB-B であることを確認しました。"
    steps = [
        {"thread_id": "TH-SAME", "agent_messages": [conclusion, "次に影響範囲を調べます。"], "usage": _usage()},
        {"thread_id": "TH-SAME", "last_message": conclusion,
         "extra_events": [_item_cmd("grep -rn DB-B .")], "usage": _usage(20)},
        {"thread_id": "TH-SAME", "agent_messages": [conclusion], "usage": _usage(30)},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, "auto-continue-same-lastmsg", 915,
                           env={"SHERPA_CODEX_AUTO_CONTINUE": "3"})
    assert len(calls) == 2
    assert env["headline"] == conclusion
    assert not env.get("codex_stopped_early")


@pytest.mark.parametrize("deny_unlink", [False, True])
def test_stale_last_message_from_previous_attempt_is_not_absorbed_as_new_answer(
        tmp_path, monkeypatch, deny_unlink):
    """継続 attempt が -o に何も書かないとき、前 attempt の -o を新しい回答として吸収しない
    （-o の削除が権限エラーで失敗する場合も同じ）。"""
    if deny_unlink:
        real_unlink = Path.unlink

        def _deny_last_message_unlink(self, missing_ok=False):
            if self.name.startswith("last-message-") and self.exists():
                raise PermissionError(13, "Permission denied", str(self))
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _deny_last_message_unlink)
    steps = [
        {"thread_id": "TH-STALE", "agent_messages": ["資料を確認しました。"],
         "last_message": "資料を確認しました。",
         "extra_events": [{"type": "item.updated", "item": {
             "id": "m-part", "type": "agent_message", "text": "次に調べます。"}}],
         "usage": _usage()},
        {"thread_id": "TH-STALE", "usage": _usage(20)},
    ]
    _, env, calls = _drive(tmp_path, monkeypatch, steps, f"auto-continue-stale-lastmsg-{deny_unlink}", 916,
                           env={"SHERPA_CODEX_AUTO_CONTINUE": "3"})
    assert len(calls) == 2
    assert env.get("codex_stopped_early")


def test_mcp_read_docs_collected_across_attempts_even_when_item_ids_repeat(tmp_path, monkeypatch):
    """別プロセスの継続で item id が振り直されても、継続側の別資料を取りこぼさない。"""
    def _read(doc):
        return {"type": "item.completed", "item": {"id": "item_0", "type": "mcp_tool_call", "tool": "read_doc",
                                                    "status": "completed", "arguments": {"doc_id": doc}}}
    d1, d2 = "4期/02_設計/01_基本設計/税計算仕様書.md", "4期/01_標準/消費税法.md"
    steps = [
        {"thread_id": "TH-S2", "agent_messages": ["まず資料を確認します。"],
         "extra_events": [_read(d1)], "usage": _usage(10, 2, 5, 1)},
        {"thread_id": "TH-S2", "agent_messages": ["確認した結果、消費税率は10%です。"],
         "extra_events": [_read(d2)], "usage": _usage(30, 2, 13, 3)},
    ]
    ctx = _ctx(uid="s2-attempts", conversation_id=902)
    ctx.make_sources = lambda docs: [{"doc_id": d, "download_url": f"/dl?rel={d}"} for d in docs]
    _, env, _ = _drive(tmp_path, monkeypatch, steps, "s2-attempts", 902, ctx=ctx)
    assert env["codex_referenced_docs"]["listed"] == 2
    assert {s["doc_id"] for s in env["sources"]} >= {d1, d2}


def test_child_ask_user_via_sidecar_stops_auto_continue_before_next_attempt(tmp_path, monkeypatch):
    """子が同 attempt 中にサイドカーへ ask_user を書いたら、自動継続せず確認カードで終える（_result は保存しない）。"""
    question = _question("child-q1", "この資料で合っていますか")
    steps = [
        {"thread_id": "TH-ASK", "agent_messages": ["まず資料を確認します。"],
         "sidecar": [{"kind": "ask_user", "ts": 1.0, "question": question}], "usage": _usage()},
        {"thread_id": "TH-ASK", "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_child_ask")
    events = _run(A.CodexProvider(), _ctx(uid="child-ask-u1", conversation_id=903))
    assert len(_read_argv_log(argv_log)) == 1
    qs = _questions(events)
    assert len(qs) == 1 and qs[0]["interaction_id"] == "child-q1"
    assert [e for e in events if isinstance(e, dict) and e.get("type") == "_result"] == []


# ===== 縮退の可視化（グラフ・全文検索の不調でも Codex の調査を止めない） =====

def _graph_era_item():
    body = json.dumps({"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"})
    return {"type": "item.completed",
            "item": {"id": "t1", "type": "mcp_tool_call", "tool": "graph_neighbors",
                     "status": "completed", "arguments": {"name": "請求"},
                     "result": {"content": [{"type": "text", "text": body}], "isError": True}}}


def test_graph_schema_era_item_does_not_abort_run_and_marks_degraded(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-ERA", "extra_events": [_graph_era_item()],
              "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage()}]
    _, env, _ = _drive(tmp_path, monkeypatch, steps, "graph-era-u1", 906)
    assert env["headline"] == "確認した結果、影響はありません。"
    assert env["graph_degraded"] == "graph_reingest_required"
    out = CS._finalize(dict(env), {"lens": "qa", "reason": "テスト"})
    assert out["headline"].startswith("関係のつながりの情報が古いため")
    assert out["limits"]["graph_reingest_required"] is True


def test_child_only_graph_failure_from_sidecar_reaches_parent(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-CHILD-ERR",
              "sidecar": [{"kind": "error", "code": "graph_unavailable", "tool": "graph_neighbors", "ts": 1.0}],
              "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage()}]
    _, env, _ = _drive(tmp_path, monkeypatch, steps, "child-err-u1", 907)
    assert env["graph_degraded"] == "graph_unavailable"
    out = CS._finalize(dict(env), {"lens": "qa", "reason": "テスト"})
    assert "接続できなかった" in out["headline"]
    assert out["limits"]["backend_unavailable_graph"] is True


def test_child_only_fulltext_failure_from_sidecar_counts_in_limits(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-CHILD-ES",
              "sidecar": [{"kind": "error", "code": "es_unavailable", "tool": "es_search", "ts": 1.0}],
              "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage()}]
    _, env, _ = _drive(tmp_path, monkeypatch, steps, "child-es-u1", 908)
    assert env["limits"]["backend_unavailable_fulltext"] is True
    assert "graph_degraded" not in env


def test_parent_era_flag_is_not_overwritten_by_child_connection_failure(tmp_path, monkeypatch):
    """親が旧世代グラフエラーを受けたターンでは、子の接続断が来ても「再取り込み待ち」を優先する。"""
    steps = [{"thread_id": "TH-ERA-PRIO", "extra_events": [_graph_era_item()],
              "sidecar": [{"kind": "error", "code": "graph_unavailable", "tool": "graph_neighbors", "ts": 1.0}],
              "agent_messages": ["確認した結果、影響はありません。"], "usage": _usage()}]
    _, env, _ = _drive(tmp_path, monkeypatch, steps, "era-prio-u1", 909)
    assert env["graph_degraded"] == "graph_reingest_required"
