"""R1b（会話継続・Codex ネイティブ resume）の break-and-confirm テスト
（docs/proposals/2026-07-13-横断レビュー対応.md §3 R1b）。

CLAUDE.md の「resume で継続」契約と実装を一致させるスライス。検証範囲:

  A. `store.set_session_id`/`get_session_id` の round-trip（DB 実体・要 Postgres）。
  B. `chat_service.handle_message`/`stream_message` が会話の codex_session_id を読んで
     `Ctx.codex_session_id` に渡し、provider が返した新しい session id を `store.set_session_id`
     で永続化すること（DB 実体・fake provider で検証）。
  C. `CodexProvider._run_authoring` の実プロセス管理（偽 codex 実行ファイル方式・
     test_codex_kill_timeout.py と同じ流儀）:
     - conversation_id 付きターンは `--ephemeral` を付けず、`--json` の `thread.started` から
       session/thread id を捕捉して env に返す。
     - 直前の session_id があれば `codex exec resume <sid> <prompt>` で resume する。
     - resume 先セッションが消失している（実機確認済み: 空 stdout・exit 1）場合は、
       **1回だけ**自動的に resume 無しの新規セッションへフォールバックする
       （break-and-confirm: フォールバック分岐を無効化すると本ファイルの
       `test_resume_failure_falls_back_to_fresh_session` が赤くなる）。
     - conversation_id 無し（既存の直接呼出し・DB 不要系テスト）は従来どおり
       per-request 使い捨て CODEX_HOME＋`--ephemeral`（無改修）。
  D. sandbox 読取封じ込め: 会話ごとの永続 CODEX_HOME パスが permission profile の
     read/write root に紛れ込まないこと（`_write_codex_authoring_config` は無改修だが、
     R1b で渡す codex_home パスの形が変わったため回帰確認する）。

DB 系（A・B）は `SHERPA_USE_FIXTURES=1` 互換モードの実 Postgres を使う（down なら skip）。
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

from sherpa import agents as A  # noqa: E402
from sherpa import chat_service as CS  # noqa: E402
from sherpa import store  # noqa: E402
from sherpa.agents import Ctx  # noqa: E402


def _try_init():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _new_conv():
    _try_init()
    return store.create_conversation(user_id="admin", world="v1", title="r1b resume test")["id"]


# ===== A. store.set_session_id / get_session_id round-trip =====

def test_get_session_id_none_for_new_conversation():
    cid = _new_conv()
    assert store.get_session_id(cid) is None


def test_get_session_id_returns_none_for_unknown_conversation_id():
    _try_init()
    assert store.get_session_id(-1) is None


def test_set_then_get_session_id_round_trips():
    cid = _new_conv()
    store.set_session_id(cid, "019f65c8-f4bc-7641-ab8b-806f2aa6b290")
    assert store.get_session_id(cid) == "019f65c8-f4bc-7641-ab8b-806f2aa6b290"


def test_set_session_id_overwrites_previous_value():
    cid = _new_conv()
    store.set_session_id(cid, "sid-old")
    store.set_session_id(cid, "sid-new")
    assert store.get_session_id(cid) == "sid-new"


# ===== B. chat_service wiring: Ctx.codex_session_id 読取＋新セッション id の永続化 =====

class _FakeSessionProvider:
    """CodexProvider の代わりに `_result` だけ返す最小 provider（provider 差し替えは
    test_history_priming.py と同じ流儀の Ctx 検査に加え、session id の往復を検証する）。"""

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


def test_handle_message_passes_prior_session_id_into_ctx(monkeypatch):
    cid = _new_conv()
    store.set_session_id(cid, "sid-prior")
    fake = _FakeSessionProvider(new_sid=None)
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: fake)

    CS.handle_message(session=None, message="続きです", conversation_id=cid, knowledge=False)

    assert fake.seen_ctx is not None
    assert fake.seen_ctx.codex_session_id == "sid-prior", "会話の直前 session id が Ctx へ渡っていない"
    assert fake.seen_ctx.conversation_id == cid


def test_handle_message_persists_new_session_id_from_env(monkeypatch):
    cid = _new_conv()
    fake = _FakeSessionProvider(new_sid="sid-fresh-001")
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: fake)

    CS.handle_message(session=None, message="新規です", conversation_id=cid, knowledge=False)

    assert store.get_session_id(cid) == "sid-fresh-001", (
        "provider が返した codex_session_id が会話に永続化されていない")


def test_handle_message_does_not_touch_session_id_when_env_has_none(monkeypatch):
    """他 provider（Codex 以外）は env に codex_session_id を含めない＝既存値のまま（誤って None 上書きしない）。"""
    cid = _new_conv()
    store.set_session_id(cid, "sid-keep")
    fake = _FakeSessionProvider(new_sid=None)
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: fake)

    CS.handle_message(session=None, message="OpenAI 頭脳のターンです", conversation_id=cid, knowledge=False)

    assert store.get_session_id(cid) == "sid-keep"


def test_stream_message_passes_and_persists_session_id(monkeypatch):
    cid = _new_conv()
    store.set_session_id(cid, "sid-stream-prior")
    fake = _FakeSessionProvider(new_sid="sid-stream-new")
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: fake)

    events = list(CS.stream_message(session=None, message="ストリーム継続", conversation_id=cid, knowledge=False))

    assert fake.seen_ctx.codex_session_id == "sid-stream-prior"
    assert any(e.get("type") == "answer" for e in events)
    assert store.get_session_id(cid) == "sid-stream-new"


def test_session_id_persist_failure_is_fail_open(monkeypatch):
    """store.set_session_id が失敗しても本ターンの回答は成立する（fail-open・例外を外へ漏らさない）。"""
    cid = _new_conv()
    fake = _FakeSessionProvider(new_sid="sid-boom")
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: fake)

    def _boom(*a, **kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "set_session_id", _boom)
    result = CS.handle_message(session=None, message="失敗しても続く", conversation_id=cid, knowledge=False)
    assert result["message"]["content"] == "ok"


# ===== C. CodexProvider 実プロセス管理（偽 codex 実行ファイル方式） =====

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

_prompt_text = args[-1] if args else ""
if "TRIGGER_ASK_USER_BREAK" in _prompt_text:
    # RV再検証 LOW（2巡目）: ask_user で早期 break するターンの cleanup 検証用。resume の有無に
    # 関わらず（prompt 末尾の文言だけを見て）ask_user の mcp_tool_call を1件返して即終了する。
    print(json.dumps({{"type": "item.completed", "item": {{
        "id": "1", "type": "mcp_tool_call", "tool": "ask_user", "status": "completed",
        "arguments": {{"prompt": "確認してください"}}}}}}))
    sys.exit(0)

if "resume" in args:
    i = args.index("resume")
    sid = args[i + 1] if i + 1 < len(args) else None
    if sid == "SID-GOOD":
        print(json.dumps({{"type": "thread.started", "thread_id": sid}}))
        print(json.dumps({{"type": "item.completed",
                           "item": {{"id": "1", "type": "agent_message", "text": "resumed-ok"}}}}))
        sys.exit(0)
    if sid == "SID-STALL":
        # stop_event 検証用: ログ書込み後に長時間停止（stdout は出さない）＝watcher の kill で
        # 中断されるのを待つ（自然終了させない）。
        time.sleep(30)
        sys.exit(1)
    if sid == "SID-PARTIAL-FAIL":
        # RV再検証 LOW-4 検証用: JSON は1行以上出す（thread.started のみ＝got_any_line=True）が
        # agent_message は無いまま非ゼロ終了する（将来の Codex CLI がエラー系イベントを出す
        # ようになった場合を模す）。got_any_line だけの判定では resume 失敗を見逃す想定。
        print(json.dumps({{"type": "thread.started", "thread_id": sid}}))
        sys.exit(1)
    # 実機確認済み（2026-07-15）: 消失セッションへの resume は空 stdout・exit 1。
    sys.exit(1)

print(json.dumps({{"type": "thread.started", "thread_id": "TH-FRESH"}}))
print(json.dumps({{"type": "item.completed",
                   "item": {{"id": "1", "type": "agent_message", "text": "fresh-ok"}}}}))
sys.exit(0)
'''


def _write_fake_codex(bin_dir: Path, argv_log: Path) -> None:
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_PY.format(argv_log=str(argv_log)))
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path: Path, monkeypatch, users_dirname: str = "users") -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    _write_fake_codex(bin_dir, argv_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "30")
    # RV再検証 HIGH-1 検証用: `_write_codex_authoring_config` は実 `CODEX_HOME/auth.json`（無ければ
    # `~/.codex/auth.json`）が存在する時だけ symlink を張る。ホストの実 `~/.codex` 状態に依存させず
    # 決定的にテストするため、ここで独自の「実 CODEX_HOME」を用意して auth.json を置く。
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    (real_codex_home / "auth.json").write_text('{"fake":"auth"}')
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    return bin_dir, argv_log


def _ctx(uid: str, conversation_id=None, codex_session_id=None, message="R1b resume テスト") -> "A.Ctx":
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


def _read_argv_log(argv_log: Path) -> list[list[str]]:
    if not argv_log.exists():
        return []
    return [eval(line) for line in argv_log.read_text().splitlines() if line.strip()]


def test_fresh_conversation_captures_session_id_and_skips_ephemeral(tmp_path, monkeypatch):
    """conversation_id 付き・resume 先なし（初回ターン）: --ephemeral を付けず、
    thread.started から捕捉した id を env["codex_session_id"] に載せる。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_fresh")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-fresh", conversation_id=101, codex_session_id=None)

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "fresh-ok"
    assert env.get("codex_session_id") == "TH-FRESH"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"1回だけ codex exec が呼ばれるはず: {calls!r}"
    assert "resume" not in calls[0], "resume 先が無いのに resume 引数が付いている"
    assert "--ephemeral" not in calls[0], "conversation_id 付きターンで --ephemeral が付いている"

    codex_home = Path(os.environ["SHERPA_USERS_DIR"]).resolve() / "r1b-fresh" / "workspace" / ".codex-sessions" / "101"
    assert codex_home.is_dir(), "会話ごとの CODEX_HOME が作られていない"
    assert not (codex_home / "config.toml").exists(), "creds を含む config.toml がターン後も残っている"
    # RV再検証 HIGH-1（2026-07-15）: auth.json（実 auth.json への symlink）も毎ターン削除される
    # （次ターンは _write_codex_authoring_config が再作成するので消して問題ない）。
    assert not (codex_home / "auth.json").exists(), "auth.json symlink がターン後も残っている（HIGH-1 未是正）"


def test_resume_success_uses_existing_session_without_retry(tmp_path, monkeypatch):
    """resume 先セッションが生きている場合は1回の resume 実行だけで完了する（フォールバック再試行なし）。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_resume_ok")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-resume-ok", conversation_id=202, codex_session_id="SID-GOOD")

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "resumed-ok"
    assert env.get("codex_session_id") == "SID-GOOD"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"resume 成功時は1回だけのはず（余計な再試行が走っている）: {calls!r}"
    assert "resume" in calls[0] and "SID-GOOD" in calls[0]
    assert "--ephemeral" not in calls[0]

    codex_home = (Path(os.environ["SHERPA_USERS_DIR"]).resolve()
                  / "r1b-resume-ok" / "workspace" / ".codex-sessions" / "202")
    assert not (codex_home / "config.toml").exists()
    assert not (codex_home / "auth.json").exists(), "resume 成功パスでも auth.json がターン後に残っている"


def test_resume_failure_falls_back_to_fresh_session(tmp_path, monkeypatch):
    """break-and-confirm: resume 先セッションが消失（空 stdout・exit 1）していたら、
    そのターン内で自動的に1回だけ resume 無しの新規セッションへフォールバックする。

    このテストはフォールバック分岐（provider.py `_run_authoring` の
    `if resume_sid and not got_any_line ...` ブロック）を消す/壊すと確実に赤くなる
    （resume 失敗のまま応答が「（未応答のため決定的回答に切替）」になり headline が変わる）。
    """
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_resume_gone")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-resume-gone", conversation_id=303, codex_session_id="SID-GONE")

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "fresh-ok", f"resume 失敗から新規セッションへのフォールバックが効いていない: {env!r}"
    assert env.get("codex_session_id") == "TH-FRESH"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"resume 失敗→フォールバックで2回呼ばれるはず: {calls!r}"
    assert "resume" in calls[0] and "SID-GONE" in calls[0], "1回目は resume 先を指定して試みるはず"
    assert "resume" not in calls[1], "2回目（フォールバック）は resume を付けないはず"

    codex_home = (Path(os.environ["SHERPA_USERS_DIR"]).resolve()
                  / "r1b-resume-gone" / "workspace" / ".codex-sessions" / "303")
    assert codex_home.is_dir(), "フォールバック後も会話ごとの CODEX_HOME は残るはず（セッション実体を保持）"
    assert not (codex_home / "config.toml").exists()
    assert not (codex_home / "auth.json").exists(), "フォールバック実行後も auth.json が残っている"


def test_partial_failure_with_nonzero_exit_and_no_agent_output_falls_back(tmp_path, monkeypatch):
    """RV再検証 LOW-4: got_any_line=True（thread.started は出た）でも、非ゼロ終了かつ
    agent_message が1つも無ければ resume 失敗とみなしてフォールバックする
    （`got_any_line` 単独判定だと見逃す将来のケースを模す）。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_partial_fail")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-partial-fail", conversation_id=505, codex_session_id="SID-PARTIAL-FAIL")

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "fresh-ok", f"部分失敗からのフォールバックが効いていない: {env!r}"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 2, f"部分失敗→フォールバックで2回呼ばれるはず: {calls!r}"
    assert "resume" in calls[0] and "SID-PARTIAL-FAIL" in calls[0]
    assert "resume" not in calls[1]


def test_ask_user_early_break_cleans_up_config_and_auth(tmp_path, monkeypatch):
    """RV再検証（2巡目）LOW: ask_user で早期 break（`for line in proc.stdout: ... break`）した
    ターンでも、config.toml と auth.json はターン終了時に削除されていることを直接確認する
    （偽 codex は prompt 末尾に `TRIGGER_ASK_USER_BREAK` が含まれる時だけ ask_user の
    mcp_tool_call を1件返して即終了する）。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_ask_user_break")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-ask-user", conversation_id=909, codex_session_id=None,
               message="TRIGGER_ASK_USER_BREAK 何か調べてください")

    events = _run(prov, ctx)

    questions = [e for e in events if isinstance(e, dict) and e.get("type") == "question"]
    assert len(questions) == 1, f"ask_user から question イベントが1件出るはず: {events!r}"
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert results == [], "ask_user ターンは question で終了し _result は出さない契約のはず"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"ask_user ターンは1回だけ呼ばれるはず（再試行しない）: {calls!r}"

    codex_home = (Path(os.environ["SHERPA_USERS_DIR"]).resolve()
                  / "r1b-ask-user" / "workspace" / ".codex-sessions" / "909")
    assert codex_home.is_dir(), "会話ごとの CODEX_HOME 自体は残るはず（セッション実体を保持）"
    assert not (codex_home / "config.toml").exists(), "ask_user 早期 break 後も config.toml が残っている"
    assert not (codex_home / "auth.json").exists(), "ask_user 早期 break 後も auth.json が残っている"


def test_exception_after_config_write_still_cleans_up(tmp_path, monkeypatch):
    """RV再検証（2巡目）LOW: config.toml/auth.json の書込み成功**直後**に例外が起きて Codex を
    一切起動できなかった場合でも、外側 finally が config.toml と auth.json を削除することを
    直接確認する（fake codex が一切呼ばれていないことも同時に確認＝実行前の失敗パスの証拠）。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_config_then_boom")
    from sherpa.providers.codex import provider as provider_mod
    from sherpa.providers.codex import sandbox as sandbox_mod
    _orig_write_config = sandbox_mod._write_codex_authoring_config

    def _write_then_boom(*args, **kwargs):
        _orig_write_config(*args, **kwargs)   # 実際に config.toml/auth.json を書く（実在させる）
        raise RuntimeError("boom-after-config-write")

    monkeypatch.setattr(provider_mod, "_write_codex_authoring_config", _write_then_boom)
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-config-boom", conversation_id=1010, codex_session_id=None)

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "dispatch-headline", (
        f"config write 直後の例外は決定的回答にフォールバックするはず: {env!r}")
    calls = _read_argv_log(argv_log)
    assert calls == [], f"config write 直後に例外が起きたら codex exec は一切呼ばないはず: {calls!r}"

    codex_home = (Path(os.environ["SHERPA_USERS_DIR"]).resolve()
                  / "r1b-config-boom" / "workspace" / ".codex-sessions" / "1010")
    assert codex_home.is_dir()
    assert not (codex_home / "config.toml").exists(), "例外後も config.toml が残っている（外側 finally 未是正）"
    assert not (codex_home / "auth.json").exists(), "例外後も auth.json が残っている（外側 finally 未是正）"


def test_no_conversation_id_keeps_legacy_ephemeral_behavior(tmp_path, monkeypatch):
    """conversation_id 無し（既存の直接呼出し）は従来どおり per-request 使い捨て＋--ephemeral のまま。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_legacy")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-legacy", conversation_id=None, codex_session_id=None)

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "fresh-ok"
    assert "codex_session_id" not in env, "conversation_id 無しのターンで session id を env に載せていない（resume 対象外）"
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1
    assert "--ephemeral" in calls[0], "conversation_id 無しのターンでは --ephemeral を付けたまま（回帰）"

    users_dir = Path(os.environ["SHERPA_USERS_DIR"]).resolve()
    ws_dir = users_dir / "r1b-legacy" / "workspace"
    assert not list(ws_dir.glob(".codexhome-*")), "per-request CODEX_HOME が実行後も残っている（rmtree 崩れ）"
    assert not (ws_dir / ".codex-sessions").exists(), "conversation_id 無しなのに永続セッションディレクトリができている"


def test_fallback_sandbox_disabled_does_not_persist_ephemeral_session_id(tmp_path, monkeypatch):
    """RV再検証 MEDIUM-2: `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は常に `--ephemeral` 実行のため、
    そこで捕捉した thread_id はディスクに残らず resume 不能。`_persist_session` だけで判定すると、
    この使い捨て thread_id を env 経由で DB に保存してしまい、サンドボックス復帰後の resume が
    必ず失敗し続ける穴があった（`_session_persistence_enabled` で fallback 経路を除外する）。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_fallback_sandbox_off")
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-fallback", conversation_id=606, codex_session_id=None)

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "fresh-ok"
    assert "codex_session_id" not in env, (
        "fallback（サンドボックス無効）経路で使い捨て thread_id が env に載っている（MEDIUM-2 未是正）")
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1
    assert "--ephemeral" in calls[0], "fallback 経路は常に --ephemeral のはず"
    assert "-s" in calls[0] and "workspace-write" in calls[0], "fallback 経路は -s workspace-write のはず"

    # 会話ごとの永続ディレクトリは作られない（fallback 経路は resume 対象外のまま）。
    users_dir = Path(os.environ["SHERPA_USERS_DIR"]).resolve()
    assert not (users_dir / "r1b-fallback" / "workspace" / ".codex-sessions").exists()


def test_fallback_resume_sid_ignored_even_if_present(tmp_path, monkeypatch):
    """MEDIUM-2 の関連ケース: fallback 経路では、会話に既存の（サンドボックス有効時に捕捉された）
    session id があっても resume を試みない（そもそも resume 不能な使い捨て実行のため）。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_fallback_with_sid")
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    prov = A.CodexProvider()
    ctx = _ctx(uid="r1b-fallback2", conversation_id=607, codex_session_id="SID-GOOD")

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "fresh-ok", "fallback 経路で resume を試みてしまっている"
    assert "codex_session_id" not in env
    calls = _read_argv_log(argv_log)
    assert len(calls) == 1
    assert "resume" not in calls[0], "fallback 経路は resume_sid があっても resume を試みないはず"


def test_symlinked_conversation_session_dir_blocks_codex_entirely(tmp_path, monkeypatch):
    """RV再検証 MEDIUM-3: `.codex-sessions/{cid}` が symlink だと Codex を一切起動しない
    （`ws_authoring is None` と同じ fail-closed 扱い＝決定的回答にフォールバック）。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_symlink_cid")
    users_dir = Path(os.environ["SHERPA_USERS_DIR"]).resolve()
    uid = "r1b-symlink-cid"
    sessions_root = users_dir / uid / "workspace" / ".codex-sessions"
    sessions_root.mkdir(parents=True)
    evil_target = tmp_path / "evil"
    evil_target.mkdir()
    (sessions_root / "707").symlink_to(evil_target)

    prov = A.CodexProvider()
    ctx = _ctx(uid=uid, conversation_id=707, codex_session_id=None)

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "dispatch-headline", (
        "symlink 混入時は Codex を起動せず決定的回答に落ちるはず（MEDIUM-3 未是正）")
    calls = _read_argv_log(argv_log)
    assert calls == [], f"symlink 混入時は codex exec を一切呼ばないはず: {calls!r}"
    assert not (evil_target / "config.toml").exists(), "symlink の指す先（外部）に config.toml を書いてしまった"


def test_symlinked_sessions_root_blocks_codex_entirely(tmp_path, monkeypatch):
    """RV再検証 MEDIUM-3: `.codex-sessions` 自体が symlink でも同様に fail-closed で拒否する。"""
    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_symlink_root")
    users_dir = Path(os.environ["SHERPA_USERS_DIR"]).resolve()
    uid = "r1b-symlink-root"
    ws = users_dir / uid / "workspace"
    ws.mkdir(parents=True)
    evil_target = tmp_path / "evil-root"
    evil_target.mkdir()
    (ws / ".codex-sessions").symlink_to(evil_target)

    prov = A.CodexProvider()
    ctx = _ctx(uid=uid, conversation_id=808, codex_session_id=None)

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "dispatch-headline"
    calls = _read_argv_log(argv_log)
    assert calls == []


def test_resume_retry_skipped_when_stopped_mid_attempt(tmp_path, monkeypatch):
    """途中停止（ctx.stop_event）で resume 試行が空振りに終わっても、フォールバック再試行はしない
    （停止要求を無視して新規セッションを起動しない）。

    偽 codex は resume 先 sid="SID-STALL" のとき、argv ログ書込み後に長時間 sleep して stdout を
    出さない（＝watcher の kill で中断される想定・自然終了しない）。1回目の呼出しが実際に始まった
    ことを argv ログで確認してから stop_event をセットすることで、
    「プロセス起動前に kill される」レースを避ける（test_codex_kill_timeout.py と同じ手法）。
    """
    import threading

    _bin_dir, argv_log = _setup(tmp_path, monkeypatch, users_dirname="users_stop_resume")
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "120")   # Timer が先に発火して停止経路の検証を汚染しないよう大きく
    prov = A.CodexProvider()
    stop_event = threading.Event()
    ctx = _ctx(uid="r1b-stop", conversation_id=404, codex_session_id="SID-STALL")
    ctx = A.Ctx(**{**ctx.__dict__, "stop_event": stop_event})

    events: list = []

    def _drive():
        events.extend(_run(prov, ctx))

    th = threading.Thread(target=_drive, daemon=True)
    th.start()

    deadline = time.time() + 10
    while time.time() < deadline and len(_read_argv_log(argv_log)) < 1:
        time.sleep(0.02)
    assert len(_read_argv_log(argv_log)) == 1, "1回目の resume 試行が起動した形跡が無い（テスト前提が崩れている）"

    stop_event.set()   # ここで初めて停止要求（1回目の呼出しは既に開始済み）
    th.join(timeout=20)
    assert not th.is_alive(), "stop_event 経路が想定時間内に完走しない"

    calls = _read_argv_log(argv_log)
    assert len(calls) == 1, f"stop_event が立っているターンはフォールバック再試行しないはず: {calls!r}"
    assert "resume" in calls[0] and "SID-STALL" in calls[0]


# ===== D. sandbox 読取封じ込め（会話ごとの永続 CODEX_HOME パスが profile に漏れない） =====

def test_persistent_codex_home_path_not_leaked_into_permission_profile(tmp_path):
    """R1b で codex_home が `workspace/.codex-sessions/{cid}` という新しい形になっても、
    生成される permission profile の read/write root は KB と authoring（"."）のみで、
    codex_home 自身のパス文字列が read/write 許可として紛れ込まないこと。"""
    codex_home = tmp_path / "users" / "u1" / "workspace" / ".codex-sessions" / "42"
    A._write_codex_authoring_config(codex_home, ["/kb/abs/path"], "low", False, "test", None)
    cfg = (codex_home / "config.toml").read_text()
    assert str(codex_home) not in cfg, "会話ごとの CODEX_HOME 自身のパスが profile に書き込まれている（read/write 許可の漏洩）"
    assert '":root" = "deny"' in cfg
    assert '"/kb/abs/path" = "read"' in cfg
    assert '"." = "write"' in cfg
