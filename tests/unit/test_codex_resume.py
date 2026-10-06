"""会話継続（Codex ネイティブ resume）の契約テスト。

  A. `store.set_session_id`/`get_session_id` の round-trip（実 Postgres・down なら skip）。
  B. `chat_service` が会話の codex_session_id を `Ctx` へ渡し、provider が返した新 session id を永続化する。
  C. `CodexProvider._run_authoring` の実プロセス管理（偽 codex 実行ファイル方式）:
     conversation_id 付きは `--ephemeral` を付けず `thread.started` から id を捕捉／直前 session があれば
     `codex exec resume <sid>`／resume 失敗は 1 回だけ新規セッションへフォールバック／conversation_id 無しは
     従来どおり使い捨て CODEX_HOME＋`--ephemeral`。
  D. 会話ごとの永続 CODEX_HOME パスが permission profile の read/write root に紛れ込まない。
"""
from __future__ import annotations

import ast
import os
import stat
import threading
import time
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

from sherpa import agents as A  # noqa: E402
from sherpa.providers.codex import sandbox as SB  # noqa: E402
from sherpa import chat_service as CS  # noqa: E402
from sherpa import store  # noqa: E402
from sherpa.agents import Ctx  # noqa: E402
from sherpa.providers.prompts import _NO_PRESEARCH_HEADLINE  # noqa: E402


def _try_init():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _new_conv():
    _try_init()
    return store.create_conversation(user_id="admin", world="v1", title="r1b resume test")["id"]


# ===== A. session id round-trip =====

def test_get_session_id_none_for_new_and_unknown_conversation():
    assert store.get_session_id(_new_conv()) is None
    assert store.get_session_id(-1) is None


def test_set_then_get_session_id_round_trips_and_overwrites():
    cid = _new_conv()
    store.set_session_id(cid, "019f65c8-f4bc-7641-ab8b-806f2aa6b290")
    assert store.get_session_id(cid) == "019f65c8-f4bc-7641-ab8b-806f2aa6b290"
    store.set_session_id(cid, "sid-old")
    store.set_session_id(cid, "sid-new")
    assert store.get_session_id(cid) == "sid-new"


# ===== B. chat_service 配線 =====

class _FakeSessionProvider:
    def __init__(self, new_sid: str | None):
        self.seen_ctx: Ctx | None = None
        self._new_sid = new_sid

    def run(self, ctx: Ctx):
        self.seen_ctx = ctx
        env = {"lens": "qa", "headline": "ok", "summary": {"total": 0}, "data": {},
               "sources": [], "scope": {"world": ctx.world, "scope_paths": [], "source": "all"}}
        if self._new_sid:
            env["codex_session_id"] = self._new_sid
        yield {"type": "_result", "env": env,
               "decision": {"lens": "qa", "input": ctx.message, "reason": "test"}}


def _fake_provider(monkeypatch, new_sid: str | None) -> _FakeSessionProvider:
    fake = _FakeSessionProvider(new_sid=new_sid)
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: fake)
    # 資料参照オフの頭脳選択（`plain_provider_for`）は別テストで検証する。ここでは差し替えた `get_provider` を使う。
    monkeypatch.setattr(CS, "plain_provider_for", lambda *a, **k: None)
    return fake


def test_stream_message_passes_prior_session_id_into_ctx(monkeypatch):
    cid = _new_conv()
    store.set_session_id(cid, "sid-prior")
    fake = _fake_provider(monkeypatch, None)
    list(CS.stream_message(session=None, message="続きです", conversation_id=cid, knowledge=False))
    assert fake.seen_ctx is not None
    assert fake.seen_ctx.codex_session_id == "sid-prior"
    assert fake.seen_ctx.conversation_id == cid


def test_stream_message_persists_new_session_id_from_env(monkeypatch):
    cid = _new_conv()
    _fake_provider(monkeypatch, "sid-fresh-001")
    list(CS.stream_message(session=None, message="新規です", conversation_id=cid, knowledge=False))
    assert store.get_session_id(cid) == "sid-fresh-001"


def test_stream_message_does_not_touch_session_id_when_env_has_none(monkeypatch):
    # Codex 以外の provider は env に codex_session_id を含めない＝既存値を None で上書きしない。
    cid = _new_conv()
    store.set_session_id(cid, "sid-keep")
    _fake_provider(monkeypatch, None)
    list(CS.stream_message(session=None, message="OpenAI 頭脳のターンです", conversation_id=cid, knowledge=False))
    assert store.get_session_id(cid) == "sid-keep"


def test_stream_message_passes_and_persists_session_id(monkeypatch):
    cid = _new_conv()
    store.set_session_id(cid, "sid-stream-prior")
    fake = _fake_provider(monkeypatch, "sid-stream-new")
    events = list(CS.stream_message(session=None, message="ストリーム継続", conversation_id=cid, knowledge=False))
    assert fake.seen_ctx.codex_session_id == "sid-stream-prior"
    assert any(e.get("type") == "answer" for e in events)
    assert store.get_session_id(cid) == "sid-stream-new"


def test_session_id_persist_failure_is_fail_open(monkeypatch):
    cid = _new_conv()
    _fake_provider(monkeypatch, "sid-boom")

    def _boom(*a, **kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "set_session_id", _boom)
    events = list(CS.stream_message(session=None, message="失敗しても続く", conversation_id=cid, knowledge=False))
    answer = next(e for e in events if e.get("type") == "answer")
    assert answer["message"]["content"] == "ok"


# ===== C. CodexProvider 実プロセス管理（偽 codex） =====

_FAKE_CODEX_PY = r'''#!/usr/bin/env python3
import json
import pathlib
import sys
import time

argv_log = pathlib.Path(r"{argv_log}")
args = sys.argv[1:]
with argv_log.open("a", encoding="utf-8") as f:
    f.write(repr(args) + "\n")
    f.flush()

_prompt_text = sys.stdin.read() if args and args[-1] == "-" else (args[-1] if args else "")
with pathlib.Path(r"{argv_log}" + ".history").open("a", encoding="utf-8") as f:
    f.write(("with" if "【直前の会話" in _prompt_text else "without") + "\n")
if "TRIGGER_ASK_USER_BREAK" in _prompt_text:
    # ask_user で早期 break するターン: mcp_tool_call を 1 件返して即終了する。
    print(json.dumps({{"type": "item.completed", "item": {{
        "id": "1", "type": "mcp_tool_call", "tool": "ask_user", "status": "completed",
        "arguments": {{"prompt": "確認してください"}}}}}}))
    sys.exit(0)

if "resume" in args:
    i = args.index("resume")
    sid = args[i + 1] if i + 1 < len(args) else None
    if sid == "SID-GOOD":
        print(json.dumps({{"type": "thread.started", "thread_id": sid}}))
        print(json.dumps({{"type": "item.completed", "item": {{
            "id": "c0", "type": "command_execution", "command": "ls", "status": "completed", "exit_code": 0}}}}))
        print(json.dumps({{"type": "item.completed",
                           "item": {{"id": "1", "type": "agent_message", "text": "resumed-ok"}}}}))
        sys.exit(0)
    if sid == "SID-STALL":
        # ログ書込み後に長時間停止（stop_event の watcher による kill を待つ）。
        time.sleep(30)
        sys.exit(1)
    if sid == "SID-PARTIAL-FAIL":
        # thread.started だけ出して agent_message 無しで非ゼロ終了（got_any_line だけの判定では見逃す形）。
        print(json.dumps({{"type": "thread.started", "thread_id": sid}}))
        sys.exit(1)
    # 消失セッションへの resume は空 stdout・exit 1（実機確認済み）。
    sys.exit(1)

print(json.dumps({{"type": "thread.started", "thread_id": "TH-FRESH"}}))
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "c0", "type": "command_execution", "command": "ls", "status": "completed", "exit_code": 0}}}}))
print(json.dumps({{"type": "item.completed",
                   "item": {{"id": "1", "type": "agent_message", "text": "fresh-ok"}}}}))
sys.exit(0)
'''


def _setup(tmp_path: Path, monkeypatch, users_dirname: str = "users") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_PY.format(argv_log=str(argv_log)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    # 偽 codex は平文 agent_message を返す（出力スキーマは対象外）＝スキーマ無効。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    # auth.json symlink の作成有無をホストの `~/.codex` に依存させない。
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    (real_codex_home / "auth.json").write_text('{"fake":"auth"}')
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    return argv_log


def _ctx(uid: str, conversation_id=None, codex_session_id=None, message="R1b resume テスト", **extra) -> "A.Ctx":
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
        **extra,
    )


def _run(prov, ctx) -> list:
    return list(prov.run(ctx))


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


def _read_argv_log(argv_log: Path) -> list[list[str]]:
    if not argv_log.exists():
        return []
    return [ast.literal_eval(line) for line in argv_log.read_text().splitlines() if line.strip()]


def _history_flags(argv_log: Path) -> list[str]:
    """各 codex 起動のプロンプトに履歴ブロックが入っていたか（"with"／"without"・起動順）。"""
    return (argv_log.parent / "argv.log.history").read_text().split()


_HISTORY = [{"role": "user", "content": "前の質問"}, {"role": "assistant", "content": "前の回答"}]


def _ws(uid: str) -> Path:
    return Path(os.environ["SHERPA_USERS_DIR"]).resolve() / uid / "workspace"


def _conv_home(uid: str, cid: int) -> Path:
    return _ws(uid) / ".codex-sessions" / str(cid)


def _assert_creds_removed(home: Path) -> None:
    # creds を含む config.toml と実 auth.json への symlink はターン後に必ず消える。
    assert home.is_dir(), "会話ごとの CODEX_HOME 自体は残る（セッション実体を保持）"
    assert not (home / "config.toml").exists(), "config.toml がターン後も残っている"
    assert not (home / "auth.json").exists(), "auth.json がターン後も残っている"


def test_fresh_conversation_captures_session_id_and_skips_ephemeral(tmp_path, monkeypatch):
    argv_log = _setup(tmp_path, monkeypatch, "users_fresh")
    env = _result_env(_run(A.CodexProvider(), _ctx("r1b-fresh", conversation_id=101)))
    assert env["headline"] == "fresh-ok"
    assert env.get("codex_session_id") == "TH-FRESH"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, calls
    assert "resume" not in calls[0]
    assert "--ephemeral" not in calls[0]
    _assert_creds_removed(_conv_home("r1b-fresh", 101))


def test_resume_success_uses_existing_session_without_retry(tmp_path, monkeypatch):
    argv_log = _setup(tmp_path, monkeypatch, "users_resume_ok")
    env = _result_env(_run(A.CodexProvider(), _ctx("r1b-resume-ok", 202, "SID-GOOD", history=_HISTORY)))
    assert env["headline"] == "resumed-ok"
    assert _history_flags(argv_log) == ["without"]  # resume 先のセッションが履歴を持つ
    assert env.get("codex_session_id") == "SID-GOOD"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, calls
    assert "resume" in calls[0] and "SID-GOOD" in calls[0]
    assert "--ephemeral" not in calls[0]
    _assert_creds_removed(_conv_home("r1b-resume-ok", 202))


@pytest.mark.parametrize("sid", ["SID-GONE", "SID-PARTIAL-FAIL"])
def test_resume_failure_falls_back_to_fresh_session_once(tmp_path, monkeypatch, sid):
    # 消失（空 stdout・exit 1）／部分失敗（thread.started のみ・非ゼロ終了）のどちらでも、
    # そのターン内で 1 回だけ resume 無しの新規セッションへフォールバックする。
    argv_log = _setup(tmp_path, monkeypatch, "users_resume_fail")
    env = _result_env(_run(A.CodexProvider(), _ctx("r1b-resume-fail", 303, sid, history=_HISTORY)))
    assert env["headline"] == "fresh-ok", env
    assert _history_flags(argv_log) == ["without", "with"]  # 作り直した新規セッションには履歴を入れる
    assert env.get("codex_session_id") == "TH-FRESH"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, calls
    assert "resume" in calls[0] and sid in calls[0]
    assert "resume" not in calls[1]
    _assert_creds_removed(_conv_home("r1b-resume-fail", 303))


def test_ask_user_early_break_cleans_up_config_and_auth(tmp_path, monkeypatch):
    argv_log = _setup(tmp_path, monkeypatch, "users_ask_user_break")
    ctx = _ctx("r1b-ask-user", 909, None, message="TRIGGER_ASK_USER_BREAK 何か調べてください")
    events = _run(A.CodexProvider(), ctx)
    assert len([e for e in events if isinstance(e, dict) and e.get("type") == "question"]) == 1, events
    assert [e for e in events if isinstance(e, dict) and e.get("type") == "_result"] == []   # question で終了
    assert len(_read_argv_log(argv_log)) == 1   # 再試行しない
    _assert_creds_removed(_conv_home("r1b-ask-user", 909))


def test_exception_after_config_write_still_cleans_up(tmp_path, monkeypatch):
    # config.toml/auth.json の書込み直後に例外が起き Codex を起動できなくても、外側 finally が削除する。
    argv_log = _setup(tmp_path, monkeypatch, "users_config_then_boom")
    from sherpa.providers.codex import sandbox as sandbox_mod
    orig = sandbox_mod._write_codex_authoring_config
    boom_calls: list = []

    def _write_then_boom(*args, **kwargs):
        orig(*args, **kwargs)   # 実際に書いて実在させてから落とす
        boom_calls.append(1)
        raise RuntimeError("boom-after-config-write")

    monkeypatch.setattr(sandbox_mod, "_write_codex_authoring_config", _write_then_boom)
    env = _result_env(_run(A.CodexProvider(), _ctx("r1b-config-boom", 1010)))
    assert boom_calls == [1]
    assert _NO_PRESEARCH_HEADLINE in env["headline"], env
    assert "RuntimeError" in env["headline"]
    assert env["completion"] == "failed"
    assert _read_argv_log(argv_log) == []
    _assert_creds_removed(_conv_home("r1b-config-boom", 1010))


def test_no_conversation_id_keeps_legacy_ephemeral_behavior(tmp_path, monkeypatch):
    argv_log = _setup(tmp_path, monkeypatch, "users_legacy")
    env = _result_env(_run(A.CodexProvider(), _ctx("r1b-legacy")))
    assert env["headline"] == "fresh-ok"
    assert "codex_session_id" not in env   # resume 対象外
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1
    assert "--ephemeral" in calls[0]
    ws_dir = _ws("r1b-legacy")
    assert not list(ws_dir.glob(".codexhome-*")), "per-request CODEX_HOME が実行後も残っている"
    assert not (ws_dir / ".codex-sessions").exists()


@pytest.mark.parametrize("prior_sid", [None, "SID-GOOD"])
def test_fallback_sandbox_disabled_does_not_persist_or_resume_ephemeral_session(tmp_path, monkeypatch, prior_sid):
    # `SHERPA_CODEX_SANDBOX=0` は常に `--ephemeral`＝捕捉した thread_id は resume 不能なので env/DB へ
    # 載せない。既存 session id があっても resume を試みない。
    argv_log = _setup(tmp_path, monkeypatch, "users_fallback_sandbox_off")
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    env = _result_env(_run(A.CodexProvider(), _ctx("r1b-fallback", 606, prior_sid)))
    assert env["headline"] == "fresh-ok"
    assert "codex_session_id" not in env
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1
    assert "--ephemeral" in calls[0]
    assert "-s" in calls[0] and "workspace-write" in calls[0]
    assert "resume" not in calls[0]
    assert not (_ws("r1b-fallback") / ".codex-sessions").exists()


@pytest.mark.parametrize("which, cid", [("session_dir", 707), ("sessions_root", 808)])
def test_symlinked_session_path_blocks_codex_entirely(tmp_path, monkeypatch, which, cid):
    # `.codex-sessions/{cid}` または `.codex-sessions` 自体が symlink なら Codex を一切起動しない（fail-closed）。
    argv_log = _setup(tmp_path, monkeypatch, "users_symlink")
    uid = "r1b-symlink"
    evil = tmp_path / "evil"
    evil.mkdir()
    if which == "session_dir":
        root = _ws(uid) / ".codex-sessions"
        root.mkdir(parents=True)
        (root / str(cid)).symlink_to(evil)
    else:
        _ws(uid).mkdir(parents=True)
        (_ws(uid) / ".codex-sessions").symlink_to(evil)
    env = _result_env(_run(A.CodexProvider(), _ctx(uid, cid)))
    assert env["headline"] == _NO_PRESEARCH_HEADLINE
    assert _read_argv_log(argv_log) == []
    assert not (evil / "config.toml").exists(), "symlink の指す先（外部）に config.toml を書いてしまった"


def test_deleted_conversation_blocks_codex_and_releases_lock(tmp_path, monkeypatch):
    from sherpa.providers.codex.turn_prepare import _conversation_lock

    argv_log = _setup(tmp_path, monkeypatch, "users_conv_gone")
    monkeypatch.setattr(store, "owns_conversation", lambda uid, cid: False)
    uid, cid = "r1b-conv-gone", 20999
    env = _result_env(_run(A.CodexProvider(), _ctx(uid, cid)))
    assert _read_argv_log(argv_log) == []
    assert not _conv_home(uid, cid).exists(), "削除済み会話の .codex-sessions/{cid} を作り直してしまった"
    lock = _conversation_lock(cid)
    assert lock.acquire(blocking=False), "run() 終了後も会話ロックが解放されていない"
    lock.release()
    assert env["headline"] == _NO_PRESEARCH_HEADLINE


def test_resume_retry_skipped_when_stopped_mid_attempt(tmp_path, monkeypatch):
    # 停止要求（ctx.stop_event）で resume 試行が空振りに終わってもフォールバック再試行しない。
    # 1 回目の起動を argv ログで確認してから stop_event をセットし「起動前 kill」のレースを避ける。
    argv_log = _setup(tmp_path, monkeypatch, "users_stop_resume")
    stop_event = threading.Event()
    ctx = _ctx("r1b-stop", 404, "SID-STALL", stop_event=stop_event)
    events: list = []
    th = threading.Thread(target=lambda: events.extend(_run(A.CodexProvider(), ctx)), daemon=True)
    th.start()

    deadline = time.time() + 10
    while time.time() < deadline and len(_read_argv_log(argv_log)) < 1:
        time.sleep(0.02)
    assert len(_read_argv_log(argv_log)) == 1, "1回目の resume 試行が起動した形跡が無い"

    stop_event.set()
    th.join(timeout=20)
    assert not th.is_alive(), "stop_event 経路が想定時間内に完走しない"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, calls
    assert "resume" in calls[0] and "SID-STALL" in calls[0]


# ===== D. sandbox 読取封じ込め =====

def test_persistent_codex_home_path_not_leaked_into_permission_profile(tmp_path):
    codex_home = tmp_path / "users" / "u1" / "workspace" / ".codex-sessions" / "42"
    SB._write_codex_authoring_config(codex_home, ["/kb/abs/path"], "low", False, "test", None)
    cfg = (codex_home / "config.toml").read_text()
    assert str(codex_home) not in cfg, "会話ごとの CODEX_HOME 自身のパスが profile に書き込まれている"
    assert '":root" = "deny"' in cfg
    assert '"/kb/abs/path" = "read"' in cfg
    assert '"." = "write"' in cfg
