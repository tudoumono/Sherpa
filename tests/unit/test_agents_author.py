"""CodexProvider（lens='author' を含む）の契約テスト。

実 codex CLI は起動しない（偽 codex スクリプト／Popen 封じ／ソース検査）。
"""
from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import pathlib
import shutil
import stat
import subprocess
import threading
import time

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402
from sherpa.providers import base as BASE  # noqa: E402
from _det_provider import DeterministicTestProvider  # noqa: E402

_ENV = {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}


def _ctx(lens="author", knowledge=True, message="消費税率の一覧をExcelにまとめて", **kw):
    return A.Ctx(
        message=message,
        world="v1",
        route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 2}, "data": {"citations": []}, "sources": [],
        },
        knowledge=knowledge,
        **kw,
    )


def _ctx_with_blocked_dispatch(lens="qa"):
    return A.Ctx(
        message="消費税率とは？",
        world="v1",
        route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "headline": "資料の「使う検索」がすべてOFF/利用できません。",
            "summary": {"total": 0}, "data": {}, "sources": [], "_tools_blocked": True,
        },
        knowledge=True,
    )


def _tool_done(events):
    return [e for e in events if e.get("type") == "node" and e.get("kind") == "tool"
            and e.get("status") == "done"]


def _result_envs(events):
    return [e["env"] for e in events if isinstance(e, dict) and e.get("type") == "_result"]


# ===== _TOOLS / _LENS_INTENT =====

def test_tools_and_lens_intent_have_author_entry():
    assert "author" in BASE._TOOLS and BASE._TOOLS["author"]
    assert BASE._LENS_INTENT.get("author")


# ===== _gather の検索経路 trace =====

def test_gather_tools_blocked_trace_and_sidecar():
    """`_tools_blocked` の envelope は「N件を確認」ではなくブロック文言になり、サイドカーは公開 env に残らない。"""
    events = list(DeterministicTestProvider().run(_ctx_with_blocked_dispatch()))
    tool_done = _tool_done(events)
    assert tool_done
    for n in tool_done:
        assert "件を確認" not in n["detail"]
        assert "使う検索が無効" in n["detail"]
    result = next(e for e in events if e.get("type") == "_result")
    assert "_tools_blocked" not in result["env"]


def test_gather_not_blocked_keeps_existing_done_wording():
    tool_done = _tool_done(list(DeterministicTestProvider().run(_ctx(lens="qa"))))
    assert tool_done and all("件を確認" in n["detail"] for n in tool_done)


# ===== _plain_text（参照 OFF の安全網） =====

def test_codex_plain_text_is_uniform_regardless_of_message():
    p = A.CodexProvider()
    texts = {p._plain_text(msg) for msg in ("消費税率の一覧をExcelで作って", "こんにちは、元気？", "")}
    assert len(texts) == 1
    txt = texts.pop()
    assert "常に社内資料を参照" in txt
    assert "OpenAI" in txt


def test_plain_run_passes_ctx_message_to_plain_text():
    assert "provider._plain_text(ctx.message)" in inspect.getsource(BASE._plain_run)


# ===== reasoning（深さでは変えない・クイックだけ 1 段下げる） =====

def _compute_reason(is_author, self_reason, system_settings, profile):
    """CodexProvider の分岐と同じ式（基準値の解決 → `codex_reasoning_for`）。"""
    from sherpa import depth_profile as D
    base_reason = ("medium" if is_author
                  else D.effective_base(system_settings, "codex_reasoning", self_reason))
    reason_raw = D.codex_reasoning_for(base_reason, profile)
    return "low" if str(reason_raw).lower() == "minimal" else reason_raw


@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_codex_reasoning_is_fixed_by_admin_base_at_every_depth(profile):
    assert _compute_reason(False, "low", None, profile) == "low"
    assert _compute_reason(False, "low", {"depth_base_codex_reasoning": "medium"}, profile) == "medium"
    assert _compute_reason(False, "low", {"depth_base_codex_reasoning": "xhigh"}, profile) == "xhigh"
    assert _compute_reason(True, "low", None, profile) == "medium"


def test_codex_reasoning_drops_one_level_for_quick():
    assert _compute_reason(False, "low", None, "quick") == "low"
    assert _compute_reason(False, "low", {"depth_base_codex_reasoning": "medium"}, "quick") == "low"
    assert _compute_reason(False, "low", {"depth_base_codex_reasoning": "xhigh"}, "quick") == "high"
    assert _compute_reason(True, "low", None, "quick") == "low"


# ===== プロンプト（MCP 版） =====

_AUTHOR_MSG = "消費税率の一覧をExcelにまとめて"


def test_prompt_mcp_author_instructs_file_creation_and_skills():
    prompt = A.CodexProvider()._prompt_mcp(_AUTHOR_MSG, "author", "v1")
    assert "authoring 直下" in prompt
    assert ".agents/skills" in prompt
    assert "作成したファイル名" in prompt and "内容の要約" in prompt
    assert _AUTHOR_MSG in prompt
    assert "graph_neighbors" in prompt and "list_docs" in prompt
    assert "原本は直接読んでよい" in prompt
    assert "確定した事実と推定は分けて書く" in prompt
    assert "推測しない" not in prompt


@pytest.mark.parametrize("lens", ["qa", "impact", "troubleshoot"])
def test_prompt_non_author_has_no_authoring_instructions(lens):
    p = A.CodexProvider()
    mcp = p._prompt_mcp("消費税率を変えたい", lens, "v1")
    assert "authoring 直下に作成してください" not in mcp
    assert "配下のスキル（xlsx/docx/pptx の" not in mcp
    assert "investigate-" in mcp
    assert "graph_neighbors" in mcp


def test_prompt_referenced_docs_instruction():
    """MCP 版は direct_read=True のときだけ「参照した資料」を案内する。"""
    p = A.CodexProvider()
    assert "参照した資料" in p._prompt_mcp("消費税率を変えたい", "qa", "v1", direct_read=True)
    assert "参照した資料" not in p._prompt_mcp("消費税率を変えたい", "qa", "v1", direct_read=False)


# ===== 偽 codex を使った run の検証 =====

_FAKE_CODEX_CWD_PY = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

argv_log = pathlib.Path(r"{argv_log}")
args = sys.argv[1:]
with argv_log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({{"args": args, "cwd": os.getcwd()}}) + "\n")
    f.flush()

prompt_text = args[-1] if args else ""
if "PARALLEL_SLOW" in prompt_text:
    time.sleep(0.5)
print(json.dumps({{"type": "thread.started", "thread_id": "TH-PARALLEL"}}))
print(json.dumps({{"type": "item.completed",
                   "item": {{"id": "1", "type": "agent_message", "text": "done-ok"}}}}))
sys.exit(0)
'''

_FAKE_CODEX_WRITES_FILE_PY = r'''#!/usr/bin/env python3
import json
import pathlib
import sys

pathlib.Path("output.txt").write_text("created by fake codex", encoding="utf-8")
print(json.dumps({"type": "item.completed",
                   "item": {"id": "1", "type": "agent_message", "text": "ファイルを作成しました。"}}))
sys.exit(0)
'''


def _install_fake_codex(tmp_path, monkeypatch, script):
    """偽 codex を PATH 先頭に置き、スキーマ無効（平文応答を完了扱いにする）・users dir を tmp に向ける。
    戻り値は (argv_log, users_dir)。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    exe = bin_dir / "codex"
    exe.write_text(script.format(argv_log=str(argv_log)) if "{argv_log}" in script else script)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    users_dir = tmp_path / "users"
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_dir))
    return argv_log, users_dir


def _read_cwd_log(argv_log):
    if not argv_log.exists():
        return []
    return [json.loads(line) for line in argv_log.read_text().splitlines() if line.strip()]


def _run_ctx(uid, lens="qa", message="質問", **kw):
    return A.Ctx(
        message=message, world="v1",
        route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid=uid, **kw,
    )


def test_same_uid_concurrent_runs_get_separate_run_dirs_and_neither_is_busy(tmp_path, monkeypatch):
    """同一 uid の 2 実行が重なっても busy にならず、別々の run dir（authoring/run-*）で完走し、終了後に掃除される。"""
    argv_log, _ = _install_fake_codex(tmp_path, monkeypatch, _FAKE_CODEX_CWD_PY)
    uid = "parallel-run-u1"
    results: dict = {}

    def _drive(key, message):
        results[key] = list(A.CodexProvider().run(_run_ctx(uid, message=message)))

    th = threading.Thread(target=_drive, args=("slow", "PARALLEL_SLOW 遅い方の実行"), daemon=True)
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and len(_read_cwd_log(argv_log)) < 1:
        time.sleep(0.02)
    assert len(_read_cwd_log(argv_log)) == 1
    _drive("fast", "速い方の実行")   # slow が sleep 中のうちに同じ uid で 2 本目
    th.join(timeout=10)
    assert not th.is_alive()

    calls = _read_cwd_log(argv_log)
    assert len(calls) == 2
    for key in ("slow", "fast"):
        envs = _result_envs(results[key])
        assert len(envs) == 1
        env = envs[0]
        assert env.get("busy") is not True
        assert "実行中" not in env["headline"]
        assert env["headline"] == "done-ok"

    cwd_slow, cwd_fast = calls[0]["cwd"], calls[1]["cwd"]
    assert cwd_slow != cwd_fast
    for cwd in (cwd_slow, cwd_fast):
        p = pathlib.Path(cwd)
        assert p.name.startswith("run-") and p.parent.name == "authoring"
        assert not p.exists()


# ===== 会話単位ロック =====

def _stub_which_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))


def test_same_conversation_second_run_is_rejected_while_first_holds_lock(monkeypatch, tmp_path):
    _stub_which_codex(monkeypatch, tmp_path)

    def _no_popen(*_a, **_k):
        raise AssertionError("会話ロックで拒否されるはずが Codex CLI が起動されている")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)
    from sherpa.providers.codex import turn_prepare as TP
    lk = TP._conversation_lock(5001)
    assert lk.acquire(blocking=False)
    try:
        ctx = _ctx(lens="qa", message="質問")
        ctx.uid = "conv-busy-u1"
        ctx.conversation_id = 5001
        events = list(A.CodexProvider().run(ctx))
    finally:
        lk.release()
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(res) == 1
    assert res[0]["env"].get("busy") is True
    assert "この会話の別の回答を実行中です" in res[0]["env"]["headline"]
    assert res[0]["env"]["scope"]["source"] == "busy"
    assert res[0]["decision"]["lens"] == "qa"


def test_different_conversation_ids_do_not_share_the_lock(monkeypatch, tmp_path):
    _stub_which_codex(monkeypatch, tmp_path)

    def _boom_popen(*_a, **_k):
        raise OSError("popen intentionally not followed through in this test")

    monkeypatch.setattr(subprocess, "Popen", _boom_popen)
    from sherpa.providers.codex import turn_prepare as TP
    lk_other = TP._conversation_lock(6001)
    assert lk_other.acquire(blocking=False)
    try:
        ctx = _ctx(lens="qa", message="質問")
        ctx.uid = "conv-other-u1"
        ctx.conversation_id = 6002
        events = list(A.CodexProvider().run(ctx))
    finally:
        lk_other.release()
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(res) == 1
    assert res[0]["env"].get("busy") is not True


def test_conversation_lock_held_through_result_event(tmp_path, monkeypatch):
    """会話ロックは `_result` を送出し終える（generator 完了）まで保持される。"""
    _install_fake_codex(tmp_path, monkeypatch, _FAKE_CODEX_CWD_PY)
    from sherpa.providers.codex import turn_prepare as TP
    conversation_id = 777001
    gen = A.CodexProvider().run(_run_ctx("conv-hold-u1", conversation_id=conversation_id))
    events = []
    result_seen = False
    for _ in range(200):
        ev = next(gen)
        events.append(ev)
        if isinstance(ev, dict) and ev.get("type") == "_result":
            result_seen = True
            break
    assert result_seen, events

    lk = TP._conversation_lock(conversation_id)
    assert not lk.acquire(blocking=False)
    with pytest.raises(StopIteration):
        next(gen)
    assert lk.acquire(blocking=False)
    lk.release()


# ===== 成果物 move／台帳登録の失敗 =====

@contextlib.contextmanager
def _fake_lock(_uid, _rel):
    yield


def _run_author_with_created_file(tmp_path, monkeypatch, uid, *, record_boom=False, move=None):
    """偽 codex が output.txt を作る author 実行。store の台帳・move を必要なぶんだけ差し替える。
    戻り値は (env, users_dir)。"""
    _, users_dir = _install_fake_codex(tmp_path, monkeypatch, _FAKE_CODEX_WRITES_FILE_PY)
    from sherpa import store
    if record_boom or move is not None:
        monkeypatch.setattr(store, "workspace_file_lock", _fake_lock)
        monkeypatch.setattr(store, "no_live_upload_for_path", lambda *_a, **_k: True)
    if record_boom:
        def _boom_record(*_a, **_k):
            raise RuntimeError("db down (test)")
        monkeypatch.setattr(store, "record_workspace_file", _boom_record)
    if move is not None:
        monkeypatch.setattr(shutil, "move", move)
    envs = _result_envs(A.CodexProvider().run(_run_ctx(uid, lens="author")))
    assert len(envs) == 1
    return envs[0], users_dir


def _run_dirs(users_dir, uid):
    return list((pathlib.Path(users_dir).resolve() / uid / "workspace" / "authoring").glob("run-*"))


def _assert_no_path_or_exc_text_in_log(records, users_dir, marker):
    matched = [r for r in records if marker in r.message]
    assert matched
    for r in matched:
        assert str(users_dir) not in r.message
        assert "Permission denied" not in r.message and "Errno 13" not in r.message
        assert "type=OSError" in r.message and "errno=" in r.message, r.message


def test_created_file_registration_failure_keeps_run_dir_and_appends_note(monkeypatch, tmp_path):
    """台帳登録失敗: files/ に孤児を残さず run_dir 側へ戻し、run_dir を残し、回答末尾に注記を付ける。"""
    uid = "created-file-fail-u1"
    env, users_dir = _run_author_with_created_file(tmp_path, monkeypatch, uid, record_boom=True)
    from sherpa.providers.codex import turn_consts as TC
    assert TC._CREATED_FILES_FAILURE_NOTE in env["headline"]
    run_dirs = _run_dirs(users_dir, uid)
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "output.txt").is_file()
    assert not (pathlib.Path(users_dir).resolve() / uid / "workspace" / "files" / "output.txt").exists()


def test_files_dir_unavailable_keeps_run_dir_and_appends_note(monkeypatch, tmp_path):
    """`files/` が symlink で使えない場合は黙って成功扱いにせず、run_dir を保持して注記を付ける。"""
    uid = "files-unavailable-u1"
    _, users_dir = _install_fake_codex(tmp_path, monkeypatch, _FAKE_CODEX_WRITES_FILE_PY)
    ws = users_dir / uid / "workspace"
    ws.mkdir(parents=True)
    evil = tmp_path / "evil-files-target"
    evil.mkdir()
    (ws / "files").symlink_to(evil)
    envs = _result_envs(A.CodexProvider().run(_run_ctx(uid, lens="author")))
    assert len(envs) == 1
    from sherpa.providers.codex import turn_consts as TC
    assert TC._CREATED_FILES_FAILURE_NOTE in envs[0]["headline"]
    run_dirs = _run_dirs(users_dir, uid)
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "output.txt").is_file()
    assert not any(evil.iterdir())


def test_move_back_failure_after_registration_failure_is_logged_and_keeps_note(monkeypatch, tmp_path, caplog):
    """登録失敗後の差し戻し move も失敗した場合: warning に記録（絶対パス・例外文字列は出さず型と errno のみ）し注記は付く。"""
    orig_move = shutil.move
    calls = {"n": 0}

    def _second_move_fails(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(f"[Errno 13] Permission denied: '{src}' -> '{dst}'")
        return orig_move(src, dst)

    uid = "move-back-fail-u1"
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        env, users_dir = _run_author_with_created_file(
            tmp_path, monkeypatch, uid, record_boom=True, move=_second_move_fails)
    from sherpa.providers.codex import turn_consts as TC
    assert TC._CREATED_FILES_FAILURE_NOTE in env["headline"]
    assert any("moved back" in r.message and "orphaned in files" in r.message for r in caplog.records)
    _assert_no_path_or_exc_text_in_log(caplog.records, users_dir, "orphaned in files")
    assert (pathlib.Path(users_dir).resolve() / uid / "workspace" / "files" / "output.txt").is_file()


def test_created_file_outright_move_failure_is_logged_without_leaking_path(monkeypatch, tmp_path, caplog):
    """最初の move 自体の失敗も、型と errno だけを記録し（パス・例外文字列を出さず）run_dir にファイルを残す。"""
    def _move_fails(src, dst):
        raise OSError(f"[Errno 13] Permission denied: '{src}' -> '{dst}'")

    uid = "outright-move-fail-u1"
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        env, users_dir = _run_author_with_created_file(tmp_path, monkeypatch, uid, move=_move_fails)
    from sherpa.providers.codex import turn_consts as TC
    assert TC._CREATED_FILES_FAILURE_NOTE in env["headline"]
    _assert_no_path_or_exc_text_in_log(caplog.records, users_dir, "move/registration failed")
    run_dirs = _run_dirs(users_dir, uid)
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "output.txt").is_file()


# ===== facade seam / 層限定の honest failure =====

def _stub_gather(monkeypatch, **_):
    def fake_gather(ctx, **_kw):
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": dict(_ENV)}
    monkeypatch.setattr(A, "_gather", fake_gather)


def test_gather_seam_intercepted_by_codex_provider(monkeypatch):
    """`agents._gather` の facade patch が CodexProvider._run_authoring にも効く（ローカル束縛への退行検知）。"""
    calls = []

    def _no_popen(*_a, **_k):
        raise AssertionError("subprocess.Popen に到達（退行時の実 CLI 起動を封じるガード）")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    def fake_gather(ctx, **_kw):
        calls.append(ctx)
        yield {"type": "node", "id": "seam-pin-codex", "kind": "think",
               "label": "t", "detail": "", "status": "done"}
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": dict(_ENV)}

    monkeypatch.setattr(A, "_gather", fake_gather)
    ctx = _ctx(lens="qa")
    ctx.uid = "seam-pin-codex-u1"
    gen = A.CodexProvider().run(ctx)
    seen = []
    try:
        for _ in range(8):
            ev = next(gen)
            seen.append(ev)
            if isinstance(ev, dict) and ev.get("id") == "seam-pin-codex":
                break
    finally:
        gen.close()
    assert calls, seen
    assert any(isinstance(e, dict) and e.get("id") == "seam-pin-codex" for e in seen), seen


def test_run_authoring_refuses_when_hard_filter_unavailable_and_layer_restricted(monkeypatch):
    """sandbox 無効で層（docs/code）が限定されたターンは Codex を起動せず honest failure を返す。
    利用者向け文言に内部語（MCP/sandbox）を出さず、理由は decision.reason にだけ残す。"""
    layer, reason = "docs", "sandbox 無効時は探す対象の限定に対応できません"
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")

    def _no_popen(*_a, **_k):
        raise AssertionError("層限定なのに Codex CLI が起動されている")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)
    _stub_gather(monkeypatch)
    ctx = _ctx(lens="qa", message="消費税率とは")
    ctx.uid = "layer-restricted-u1"
    ctx.scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": layer}
    events = list(A.CodexProvider().run(ctx))
    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "_result")
    assert result["env"]["headline"] == (
        "この構成では探す対象の限定はできません。管理者に設定の確認を依頼してください。")
    assert result["decision"]["reason"] == reason
    assert result["env"]["scope"]["layer"] == layer
    assert result["env"]["scope"]["layer_applied"] is True
    assert result["env"]["data"] == {} and result["env"]["sources"] == []
    assert "usage" not in result["env"]
    user_facing = [e["text"] for e in events if isinstance(e, dict) and e.get("type") == "answer_delta"]
    user_facing += [n["detail"] for n in events if isinstance(n, dict) and n.get("type") == "node"]
    for text in user_facing:
        assert "MCP" not in text and "sandbox" not in text.lower(), text


def test_run_dir_ignores_stale_authoring_leftovers(tmp_path, monkeypatch):
    """旧方式の authoring/.tmp 残存を cwd に使わず、触れない。今回の cwd は新規 run-* で、終了後に掃除される。"""
    argv_log, users_dir = _install_fake_codex(tmp_path, monkeypatch, _FAKE_CODEX_CWD_PY)
    uid = "tmp-clear-u1"
    legacy_tmp = users_dir / uid / "workspace" / "authoring" / ".tmp"
    legacy_tmp.mkdir(parents=True)
    leftover = legacy_tmp / "leftover-from-previous-turn.txt"
    leftover.write_text("前ターンの内容の断片", encoding="utf-8")

    ctx = _run_ctx(uid, scope_meta={"world": "v1", "scope_paths": [], "source": "all", "layer": "both"})
    envs = _result_envs(A.CodexProvider().run(ctx))
    assert len(envs) == 1 and envs[0]["headline"] == "done-ok"
    calls = _read_cwd_log(argv_log)
    assert len(calls) == 1
    used_cwd = pathlib.Path(calls[0]["cwd"])
    assert used_cwd != legacy_tmp
    assert used_cwd.parent.name == "authoring" and used_cwd.name.startswith("run-")
    assert not used_cwd.exists()
    assert leftover.exists()
