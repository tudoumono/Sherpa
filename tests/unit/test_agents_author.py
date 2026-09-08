"""P1-a（Codex 強化計画 Phase1・作成系の一級市民化）単体テスト。

lens='author' の前提条件ゲート:
  - 頭脳=Codex かつ ナレッジ参照 ON のときだけファイル作成を試みる。
  - 他頭脳（Heuristic/OpenAI等の _GenProvider 系）は author 判定でもファイルを作らず、
    従来 qa 相当の下書きで回答する（headline 冒頭に案内を前置）。
  - Codex＋ナレッジ OFF は「資料に基づいて作成するため、ナレッジ参照をオンにしてください」と正直に返す
    （作成系の語を含まない素の雑談は従来どおりの汎用案内のまま）。
  - CodexProvider は author のとき reasoning/timeout を SHERPA_CODEX_REASONING_AUTHOR／
    SHERPA_CODEX_TIMEOUT_AUTHOR（既定 medium/600秒）に切り替える。通常レンズは現行のまま。

subprocess を起動する CodexProvider.run() の分岐は、既存 test_codex_workspace_authoring.py と
同じ「ソース検査」方式で確認する（実 codex CLI 起動は対象外・E2E はコーディネーターが後で実施）。
"""
from __future__ import annotations

import os
import shutil
import subprocess

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402


def _ctx(lens="author", knowledge=True, make_sources=None, message="消費税率の一覧をExcelにまとめて"):
    """route/dispatch を固定応答にした最小 Ctx（DB/Neo4j 不要）。"""
    return A.Ctx(
        message=message,
        world="v1",
        route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "headline": "該当箇所が2件見つかりました。",
            "summary": {"total": 2}, "data": {"citations": []}, "sources": [],
        },
        knowledge=knowledge,
        make_sources=make_sources,
    )


# ===== _TOOLS / _LENS_INTENT =====

def test_tools_and_lens_intent_have_author_entry():
    assert "author" in A._TOOLS and A._TOOLS["author"], "author の tools ノードが無い"
    assert A._LENS_INTENT.get("author"), "author の意図メッセージが無い"


# ===== _gather の検索経路トグル trace（調べ方ブロック §3.6・SC-6e）=====

def _ctx_with_blocked_dispatch(lens="qa"):
    """`dispatch`（chat_service._dispatch 相当）が honest-failure envelope（`_tools_blocked`
    サイドカーつき）を返す最小 Ctx。"""
    return A.Ctx(
        message="消費税率とは？",
        world="v1",
        route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "headline": "資料の「使う検索」がすべてOFF/利用できません（「詳細」で grep・全文のいずれかを有効にしてください）。",
            "summary": {"total": 0}, "data": {}, "sources": [], "_tools_blocked": True,
        },
        knowledge=True,
    )


def test_gather_tools_blocked_replaces_done_detail_with_blocked_message():
    """`dispatch` が `_tools_blocked=True` を返すと、"done" ノードの detail が「N件を確認」ではなく
    ブロックを説明する固定文言になる（実際には何も検索していないのに完了したかのような trace を
    出さない・SC-6e）。"""
    events = list(A.HeuristicProvider().run(_ctx_with_blocked_dispatch()))
    tool_done = [e for e in events if e.get("type") == "node" and e.get("kind") == "tool"
                and e.get("status") == "done"]
    assert tool_done, "tool ノードが無い"
    for n in tool_done:
        assert "件を確認" not in n["detail"]
        assert "使う検索が無効" in n["detail"]


def test_gather_tools_blocked_sidecar_not_leaked_to_public_env():
    """`_tools_blocked` は `_gather` が pop する内部専用サイドカーで、公開 `_result.env` には残らない。"""
    events = list(A.HeuristicProvider().run(_ctx_with_blocked_dispatch()))
    result = next(e for e in events if e.get("type") == "_result")
    assert "_tools_blocked" not in result["env"]


def test_gather_not_blocked_keeps_existing_done_wording():
    """`_tools_blocked` が無い（既定・従来どおり）envelope では "件を確認" のまま（byte-identical 回帰）。"""
    events = list(A.HeuristicProvider().run(_ctx(lens="qa")))
    tool_done = [e for e in events if e.get("type") == "node" and e.get("kind") == "tool"
                and e.get("status") == "done"]
    assert tool_done and all("件を確認" in n["detail"] for n in tool_done)


# ===== HeuristicProvider: 他頭脳 fallback 文言 =====

def test_heuristic_provider_prepends_author_fallback_note():
    events = list(A.HeuristicProvider().run(_ctx(lens="author")))
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["headline"].startswith(A._AUTHOR_FALLBACK_NOTE)
    assert "該当箇所が2件見つかりました。" in result["env"]["headline"]
    assert result["decision"]["lens"] == "author"


def test_heuristic_provider_non_author_lens_unaffected():
    events = list(A.HeuristicProvider().run(_ctx(lens="qa")))
    result = next(e for e in events if e.get("type") == "_result")
    assert not result["env"]["headline"].startswith("ファイル作成は頭脳")
    assert result["env"]["headline"] == "該当箇所が2件見つかりました。"


# ===== _GenProvider: 他頭脳 fallback 文言 + agentic 除外 =====

class _FakeGen(A._GenProvider):
    """subprocess/HTTP を使わない _GenProvider のテスト用具象クラス。"""
    label = "FakeGen"

    def _stream(self, prompt):
        yield "生成した下書き文。"

    def _agentic_loop(self, ctx):
        raise AssertionError("author は agentic_run に入ってはいけない（未対応ツール）")


def test_gen_provider_prepends_author_fallback_note_and_skips_agentic():
    # make_sources を与えて agentic 経路が「有効な状況」でも author は _agentic_run を使わないことを確認。
    ctx = _ctx(lens="author", make_sources=lambda docs: [])
    events = list(_FakeGen().run(ctx))
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["headline"].startswith(A._AUTHOR_FALLBACK_NOTE)
    assert "生成した下書き文。" in result["env"]["headline"]
    # ライブ表示にも note が反映される（answer_delta の最初のチャンクが note）。
    deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
    assert deltas and deltas[0] == A._AUTHOR_FALLBACK_NOTE


def test_gen_provider_qa_lens_still_uses_agentic_when_available():
    """回帰: author 以外（qa）は従来どおり make_sources 有りなら agentic 経路を試みる
    （_agentic_loop が呼ばれて例外→フォールバック node が出ることで間接確認）。"""
    ctx = _ctx(lens="qa", make_sources=lambda docs: [])
    events = list(_FakeGen().run(ctx))
    # _agentic_loop が AssertionError を投げても _agentic_run 全体は Exception 節で捕捉されフォールバックする。
    assert any(e.get("id") == "fallback" for e in events if e.get("type") == "node"), \
        "qa で agentic 経路が試みられていない（フォールバック node が出ていない）"


# ===== CodexProvider._plain_text: 参照OFFで呼ばれた場合の安全網 =====
# 2026-08-15 決定: Codex 構成は資料参照ON固定（画面はトグルON固定・`routers/chat.py::_knowledge_for`
# がサーバ側でも強制）。この経路は内部呼び出しや古いクライアント向けの安全網として残るだけなので、
# 依頼の種類で文言を出し分けない（旧実装は作成系だけ別案内を返していた）。

def test_codex_plain_text_is_uniform_regardless_of_message():
    p = A.CodexProvider()
    texts = {p._plain_text(msg) for msg in
             ("消費税率の一覧をExcelで作って", "こんにちは、元気？", "")}
    assert len(texts) == 1, f"依頼内容で文言が変わっている: {texts}"
    txt = texts.pop()
    assert "常に社内資料を参照" in txt          # なぜ素の会話にならないかを伝える
    assert "OpenAI" in txt                      # 雑談したい人の行き先を示す


def test_plain_run_passes_ctx_message_to_plain_text():
    """_plain_run が provider._plain_text(ctx.message) を呼ぶ（引数無し呼び出しに戻っていないこと）。"""
    import inspect
    src = inspect.getsource(A._plain_run)
    assert "provider._plain_text(ctx.message)" in src


# ===== CodexProvider: author のときだけ reasoning/timeout を切り替える（ソース検査） =====

def test_codex_run_has_author_reasoning_timeout_branch():
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert 'decision["lens"] == "author"' in src, "author 判定の分岐が run() に無い"
    assert "SHERPA_CODEX_REASONING_AUTHOR" in src, "author 専用 reasoning env が無い"
    assert "SHERPA_CODEX_TIMEOUT_AUTHOR" in src, "author 専用 timeout env が無い"
    # threading.Timer は切り替え後の _timeout（self._timeout ではない）を使うこと。
    assert "threading.Timer(_timeout," in src, "Timer が author 分岐後の _timeout を使っていない"


def test_codex_reasoning_author_env_default_and_override(monkeypatch):
    """env 未設定時は既定 'medium'・設定時はその値を使う（実際の分岐ロジックを直接評価）。"""
    import os as _os
    monkeypatch.delenv("SHERPA_CODEX_REASONING_AUTHOR", raising=False)
    monkeypatch.delenv("SHERPA_CODEX_TIMEOUT_AUTHOR", raising=False)

    def _compute(is_author, self_reason, self_timeout):
        _reason_raw = (_os.environ.get("SHERPA_CODEX_REASONING_AUTHOR", "medium")
                      if is_author else self_reason)
        _reason = "low" if str(_reason_raw).lower() == "minimal" else _reason_raw
        _timeout = (float(_os.environ.get("SHERPA_CODEX_TIMEOUT_AUTHOR", "600"))
                   if is_author else self_timeout)
        return _reason, _timeout

    # author=True・env 未設定 → 既定 medium/600。
    assert _compute(True, "low", 180.0) == ("medium", 600.0)
    # author=True・env 設定あり → その値。
    monkeypatch.setenv("SHERPA_CODEX_REASONING_AUTHOR", "high")
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT_AUTHOR", "900")
    assert _compute(True, "low", 180.0) == ("high", 900.0)
    # author=False（通常レンズ） → 従来どおり self._reason/self._timeout のまま。
    assert _compute(False, "low", 180.0) == ("low", 180.0)


# ===== 調べる深さ（調べ方ブロック §3.2・SC-6c）: Codex reasoning の per-turn 上書き =====

def test_codex_run_wires_depth_profile_into_reasoning_branch():
    """author/通常いずれの基準値にも `depth_profile_mod.codex_reasoning_for()` の上書きが
    掛かること（ソース検査・実 codex CLI 起動は対象外）。標準の基準値は `effective_base()`
    （system_settings の管理画面編集）を経由すること。"""
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert "depth_profile_mod.codex_reasoning_for(" in src, "調べる深さの per-turn 上書きが無い"
    assert "depth_profile_mod.effective_base(" in src, \
        "通常レンズの基準値が管理画面の基準値編集（system_settings）を経由していない"


def test_codex_reasoning_depth_profile_override_standard_deep_max(monkeypatch):
    """標準/深く/最大それぞれで、CodexProvider の実際の分岐と同じ式（`_base_reason` の解決 →
    `codex_reasoning_for`）を評価する（純関数の組み合わせ・実 codex CLI 起動は対象外）。"""
    from sherpa import depth_profile as D

    def _compute(is_author, self_reason, system_settings, profile):
        base_reason = (__import__("os").environ.get("SHERPA_CODEX_REASONING_AUTHOR", "medium") if is_author
                      else D.effective_base(system_settings, "codex_reasoning", self_reason))
        reason_raw = D.codex_reasoning_for(base_reason, profile)
        return "low" if str(reason_raw).lower() == "minimal" else reason_raw

    monkeypatch.delenv("SHERPA_CODEX_REASONING_AUTHOR", raising=False)
    # 通常レンズ: 標準=self._reason のまま・深く=high・最大=xhigh。
    assert _compute(False, "low", None, "standard") == "low"
    assert _compute(False, "low", None, "deep") == "high"
    assert _compute(False, "low", None, "max") == "xhigh"
    # 管理画面の基準値編集（system_settings）が標準時の基準値を上書きする。
    assert _compute(False, "low", {"depth_base_codex_reasoning": "medium"}, "standard") == "medium"
    # author は基準値が別軸（env）だが、調べる深さの上書き自体は一律に掛かる。
    assert _compute(True, "low", None, "standard") == "medium"   # author 既定
    assert _compute(True, "low", None, "deep") == "high"
    assert _compute(True, "low", None, "max") == "xhigh"


# ===== P1-c: author 専用プロンプト（FS 版・MCP 版） =====

def test_prompt_fs_author_instructs_file_creation_and_skills():
    p = A.CodexProvider()
    prompt = p._prompt("消費税率の一覧をExcelにまとめて", "author", {"data": {}}, "v1")
    assert "authoring 直下" in prompt, "成果物を authoring 直下に作る指示が無い"
    assert ".agents/skills" in prompt, "スキル活用の案内が無い"
    assert "作成したファイル名" in prompt and "内容の要約" in prompt, "完了報告の指示が無い"
    assert "消費税率の一覧をExcelにまとめて" in prompt
    # containment/grounding の短縮形は author でも維持される（多層防御）。
    assert "指定資料フォルダ以外は読まない" in prompt
    assert "推測しない" in prompt


def test_prompt_fs_non_author_unchanged_shape():
    """回帰: author 以外（qa/impact/troubleshoot）は従来どおり質問に答える指示のまま。"""
    p = A.CodexProvider()
    for lens in ("qa", "impact", "troubleshoot"):
        prompt = p._prompt("消費税率を変えたい", lens, {"data": {}}, "v1")
        assert "質問に答えて" in prompt
        assert "authoring 直下に作成してください" not in prompt, f"{lens} に作成指示が混入した"
        assert ".agents/skills" not in prompt, f"{lens} にスキル案内が混入した"


def test_prompt_mcp_author_instructs_file_creation_and_skills():
    p = A.CodexProvider()
    prompt = p._prompt_mcp("消費税率の一覧をExcelにまとめて", "author", "v1")
    assert "authoring 直下" in prompt
    assert ".agents/skills" in prompt
    assert "作成したファイル名" in prompt and "内容の要約" in prompt
    assert "消費税率の一覧をExcelにまとめて" in prompt
    # MCP ツール活用の案内（list_docs/graph_neighbors 等）は author でも維持される。
    assert "graph_neighbors" in prompt
    assert "list_docs" in prompt
    assert "MCP ツール以外でのファイル直接読み取りは禁止" in prompt
    assert "推測しない" in prompt


def test_prompt_mcp_non_author_unchanged_shape():
    p = A.CodexProvider()
    for lens in ("qa", "impact", "troubleshoot"):
        prompt = p._prompt_mcp("消費税率を変えたい", lens, "v1")
        assert "authoring 直下に作成してください" not in prompt, f"{lens} に作成指示が混入した"
        assert ".agents/skills" not in prompt, f"{lens} にスキル案内が混入した"
        assert "graph_neighbors" in prompt   # 既存の MCP ツール案内は健在


# ===== 実行ごとの作業領域（run_dir）: 同一 uid の並走（直列化 lock 撤去の置き換え） =====

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
    time.sleep(0.5)   # 並走ウィンドウを作る（他方の実行が同じ uid で割り込めることを確認するため）
print(json.dumps({{"type": "thread.started", "thread_id": "TH-PARALLEL"}}))
print(json.dumps({{"type": "item.completed",
                   "item": {{"id": "1", "type": "agent_message", "text": "done-ok"}}}}))
sys.exit(0)
'''


def _write_fake_codex_cwd(bin_dir, argv_log):
    """test_codex_resume.py の偽 codex 流儀＋cwd も記録する版（run dir 分離の確認用）。"""
    import stat
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_CWD_PY.format(argv_log=str(argv_log)))
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _read_cwd_log(argv_log):
    import json
    if not argv_log.exists():
        return []
    return [json.loads(line) for line in argv_log.read_text().splitlines() if line.strip()]


def test_same_uid_concurrent_runs_get_separate_run_dirs_and_neither_is_busy(tmp_path, monkeypatch):
    """RV MEDIUM の同一 uid 直列化 lock は撤去し、実行ごとに専用の作業領域（authoring/run-*）を
    割り当てる方式に置き換えた——同一 uid の2実行が時間的に重なっても busy にならず、それぞれ
    別々の run dir を cwd にして両方完走し、終了後は両方とも掃除される。"""
    import pathlib
    import threading
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    _write_fake_codex_cwd(bin_dir, argv_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # 偽 codex は平文 agent_message を返す（run dir/台帳登録/会話ロックの検証が目的で出力スキーマは
    # 対象外）。`--output-schema`（既定 ON）だと平文は未完了扱いになり headline が変わってしまうため
    # 無効化する（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3 のスキーマ無効時契約）。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    uid = "parallel-run-u1"
    results: dict = {}

    def _drive(key, message):
        ctx = A.Ctx(
            message=message, world="v1",
            route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
            dispatch=lambda lens_, inp: {
                "lens": lens_, "headline": "dispatch-headline",
                "summary": {"total": 0}, "data": {}, "sources": [],
            },
            knowledge=True, uid=uid,
        )
        results[key] = list(A.CodexProvider().run(ctx))

    th = threading.Thread(target=_drive, args=("slow", "PARALLEL_SLOW 遅い方の実行"), daemon=True)
    th.start()

    deadline = time.time() + 10
    while time.time() < deadline and len(_read_cwd_log(argv_log)) < 1:
        time.sleep(0.02)
    assert len(_read_cwd_log(argv_log)) == 1, "1本目（slow）の起動が確認できない（テスト前提が崩れている）"

    # slow 側がまだ sleep 中（完走前）のうちに、同じ uid で2本目を同期実行する——
    # 直列化 lock が残っていればここで busy 応答になるはず。
    _drive("fast", "速い方の実行")

    th.join(timeout=10)
    assert not th.is_alive(), "slow 側が想定時間内に完走しない"

    calls = _read_cwd_log(argv_log)
    assert len(calls) == 2, f"2回とも codex exec が実行されるはず: {calls!r}"

    def _env_of(key):
        res = [e for e in results[key] if isinstance(e, dict) and e.get("type") == "_result"]
        assert len(res) == 1, f"{key}: _result が1件でない: {results[key]!r}"
        return res[0]["env"]

    for label, env in (("slow", _env_of("slow")), ("fast", _env_of("fast"))):
        assert env.get("busy") is not True, f"{label} 側が busy になっている（直列化 lock の名残）"
        assert "実行中" not in env["headline"], f"{label} 側が busy 文言のまま: {env['headline']!r}"
        assert env["headline"] == "done-ok", f"{label} 側が完走していない: {env!r}"

    cwd_slow, cwd_fast = calls[0]["cwd"], calls[1]["cwd"]
    assert cwd_slow != cwd_fast, "2本の実行が同じ cwd を共有している（run dir が分離されていない）"
    for cwd in (cwd_slow, cwd_fast):
        p = pathlib.Path(cwd)
        assert p.name.startswith("run-"), f"cwd が authoring/run-* 形式でない: {cwd}"
        assert p.parent.name == "authoring", f"cwd の親が authoring/ でない: {cwd}"
        assert not p.exists(), f"実行後も run dir が残っている（cleanup 未実施）: {cwd}"


# ===== 会話単位ロック: 永続 CODEX_HOME の同時使用を防ぐ =====
# run dir 自体は実行ごとに独立なので並走可能だが、永続 CODEX_HOME
# （`.codex-sessions/{conversation_id}`・R1b の会話継続）は同一会話の複数ターンで固定パスを
# 共有する。同一会話の2実行が重なると config.toml の unlink→再作成・session JSONL の同時書込等が
# 競合しうる（O_EXCL は直前の unlink で排他にならない）ため、conversation_id 単位の非ブロッキング
# lock で「永続 CODEX_HOME を使う実行」だけを直列化する（uid・lens とは無関係）。

def test_same_conversation_second_run_is_rejected_while_first_holds_lock(monkeypatch, tmp_path):
    """同一 conversation_id の2実行が重なると、2本目は Codex を起動せず拒否応答になる。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    def _no_popen(*_a, **_k):
        raise AssertionError("会話ロックで拒否されるはずが Codex CLI が起動されている")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    from sherpa.providers.codex import provider as PV
    lk = PV._conversation_lock(5001)
    assert lk.acquire(blocking=False)
    try:
        prov = A.CodexProvider()
        ctx = _ctx(lens="qa", message="質問")
        ctx.uid = "conv-busy-u1"
        ctx.conversation_id = 5001
        events = list(prov.run(ctx))
    finally:
        lk.release()
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(res) == 1, "会話ロック拒否の応答が _result で完結していない"
    assert res[0]["env"].get("busy") is True
    assert "この会話の別の回答を実行中です" in res[0]["env"]["headline"]
    assert res[0]["env"]["scope"]["source"] == "busy"
    assert res[0]["decision"]["lens"] == "qa"   # busy 応答も実際の decision.lens を引き継ぐ（旧実装は固定 "qa"）


def test_different_conversation_ids_do_not_share_the_lock(monkeypatch, tmp_path):
    """別会話（別 conversation_id）は互いに無関係な CODEX_HOME・run dir を使うため、
    片方がロックを保持していてももう片方は待たされない（同一 uid でも別会話なら並走可）。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    def _boom_popen(*_a, **_k):
        raise OSError("popen intentionally not followed through in this test")

    monkeypatch.setattr(subprocess, "Popen", _boom_popen)

    from sherpa.providers.codex import provider as PV
    lk_other = PV._conversation_lock(6001)
    assert lk_other.acquire(blocking=False)
    try:
        prov = A.CodexProvider()
        ctx = _ctx(lens="qa", message="質問")
        ctx.uid = "conv-other-u1"
        ctx.conversation_id = 6002   # 別会話 → lk_other とは無関係
        events = list(prov.run(ctx))
    finally:
        lk_other.release()
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(res) == 1, f"完走していない: {events!r}"
    assert res[0]["env"].get("busy") is not True, "別会話なのに busy 応答になっている"


def test_conversation_lock_held_through_result_event(tmp_path, monkeypatch):
    """会話ロックは `_result` を送出し終える（generator が完了し finally が走る）まで保持される。
    途中（成果物処理〜`_result` 送出前）で解放すると、同じ conversation_id の次ターンが
    古い `codex_session_id`／履歴のまま割り込める窓ができる。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    _write_fake_codex_cwd(bin_dir, argv_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # 偽 codex は平文 agent_message を返す（run dir/台帳登録/会話ロックの検証が目的で出力スキーマは
    # 対象外）。`--output-schema`（既定 ON）だと平文は未完了扱いになり headline が変わってしまうため
    # 無効化する（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3 のスキーマ無効時契約）。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    from sherpa.providers.codex import provider as PV
    conversation_id = 777001
    ctx = A.Ctx(
        message="質問", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid="conv-hold-u1", conversation_id=conversation_id,
    )
    gen = A.CodexProvider().run(ctx)
    events = []
    result_seen = False
    for _ in range(200):
        ev = next(gen)
        events.append(ev)
        if isinstance(ev, dict) and ev.get("type") == "_result":
            result_seen = True
            break
    assert result_seen, f"_result に到達しなかった: {events!r}"

    lk = PV._conversation_lock(conversation_id)
    assert not lk.acquire(blocking=False), "_result 送出直後にもう会話ロックが解放されている"

    try:
        next(gen)
        raise AssertionError("_result の後にもう1件 yield された（想定外）")
    except StopIteration:
        pass   # generator 完了＝finally 実行＝ここでロックが解放される

    assert lk.acquire(blocking=False), "generator 完了後も会話ロックが解放されていない"
    lk.release()


# ===== 成果物 move／台帳登録の失敗 =====

_FAKE_CODEX_WRITES_FILE_PY = r'''#!/usr/bin/env python3
import json
import pathlib
import sys

pathlib.Path("output.txt").write_text("created by fake codex", encoding="utf-8")
print(json.dumps({"type": "item.completed",
                   "item": {"id": "1", "type": "agent_message", "text": "ファイルを作成しました。"}}))
sys.exit(0)
'''


def _write_fake_codex_creates_file(bin_dir):
    import stat
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_WRITES_FILE_PY)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_created_file_registration_failure_keeps_run_dir_and_appends_note(monkeypatch, tmp_path):
    """move は成功したが台帳登録（`record_workspace_file`）に失敗した場合、
    (a) files/ に台帳の無い孤児を残さず run_dir 側へ戻す、(b) run_dir 自体は削除せず
    回収用に残す、(c) 回答本文の末尾に固定注記を付ける。"""
    import pathlib

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_codex_creates_file(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # 偽 codex は平文 agent_message を返す（run dir/台帳登録/会話ロックの検証が目的で出力スキーマは
    # 対象外）。`--output-schema`（既定 ON）だと平文は未完了扱いになり headline が変わってしまうため
    # 無効化する（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3 のスキーマ無効時契約）。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    users_dir = tmp_path / "users"
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_dir))

    import contextlib
    from sherpa import store

    @contextlib.contextmanager
    def _fake_lock(_uid, _rel):
        yield

    def _boom_record(*_a, **_k):
        raise RuntimeError("db down (test)")

    monkeypatch.setattr(store, "workspace_file_lock", _fake_lock)
    monkeypatch.setattr(store, "no_live_upload_for_path", lambda *_a, **_k: True)
    monkeypatch.setattr(store, "record_workspace_file", _boom_record)

    uid = "created-file-fail-u1"
    ctx = A.Ctx(
        message="質問", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid=uid,
    )
    envs = [e["env"] for e in A.CodexProvider().run(ctx) if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(envs) == 1, "完走していない"
    env = envs[0]
    from sherpa.providers.codex import provider as PV
    assert PV._CREATED_FILES_FAILURE_NOTE in env["headline"], f"注記が付いていない: {env['headline']!r}"

    users_root = pathlib.Path(users_dir).resolve()
    run_dirs = list((users_root / uid / "workspace" / "authoring").glob("run-*"))
    assert len(run_dirs) == 1, f"run dir が想定どおり残っていない（削除されてしまった）: {run_dirs!r}"
    assert (run_dirs[0] / "output.txt").is_file(), "登録失敗後、ファイルが run_dir 側へ戻っていない"
    files_dir = users_root / uid / "workspace" / "files"
    assert not (files_dir / "output.txt").exists(), "登録失敗なのに files/ に台帳無しの孤児が残っている"


def test_files_dir_unavailable_keeps_run_dir_and_appends_note(monkeypatch, tmp_path):
    """`files/` が使えない（symlink 等で `_dest_dir is None`）場合を黙って成功扱いにしない——
    成果物は run_dir に残ったまま（move していない）なので、保存失敗として run_dir を保持し、
    回答本文へ固定注記を付ける。"""
    import pathlib

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_codex_creates_file(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # 偽 codex は平文 agent_message を返す（run dir/台帳登録/会話ロックの検証が目的で出力スキーマは
    # 対象外）。`--output-schema`（既定 ON）だと平文は未完了扱いになり headline が変わってしまうため
    # 無効化する（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3 のスキーマ無効時契約）。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    users_dir = tmp_path / "users"
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_dir))

    uid = "files-unavailable-u1"
    # files/ を symlink にして使えない状態を模す（ws_files.is_symlink() → None 扱い → _dest_dir=None）。
    ws = users_dir / uid / "workspace"
    ws.mkdir(parents=True)
    evil = tmp_path / "evil-files-target"
    evil.mkdir()
    (ws / "files").symlink_to(evil)

    ctx = A.Ctx(
        message="質問", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid=uid,
    )
    envs = [e["env"] for e in A.CodexProvider().run(ctx) if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(envs) == 1, "完走していない"
    env = envs[0]
    from sherpa.providers.codex import provider as PV
    assert PV._CREATED_FILES_FAILURE_NOTE in env["headline"], f"注記が付いていない: {env['headline']!r}"

    users_root = pathlib.Path(users_dir).resolve()
    run_dirs = list((users_root / uid / "workspace" / "authoring").glob("run-*"))
    assert len(run_dirs) == 1, f"run dir が想定どおり残っていない（削除されてしまった）: {run_dirs!r}"
    assert (run_dirs[0] / "output.txt").is_file(), "成果物が run_dir に残っていない（黙って消えた）"


def test_move_back_failure_after_registration_failure_is_logged_and_keeps_note(monkeypatch, tmp_path, caplog):
    """台帳登録失敗後の差し戻し（2回目の move）が失敗しても握り潰さない——明示的に warning へ
    記録し（相対パスのみ）、その場合も `_created_files_failed=True` のまま注記が付く。
    差し戻し例外そのもの（`shutil.move` の実際の失敗と同様、文字列表現に絶対パスを含む）は
    そのまま `%s` で出さず、型（`type(exc).__name__`）と errno だけを記録することも確認する。"""
    import logging
    import pathlib

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_codex_creates_file(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # 偽 codex は平文 agent_message を返す（run dir/台帳登録/会話ロックの検証が目的で出力スキーマは
    # 対象外）。`--output-schema`（既定 ON）だと平文は未完了扱いになり headline が変わってしまうため
    # 無効化する（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3 のスキーマ無効時契約）。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    users_dir = tmp_path / "users"
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_dir))

    import contextlib
    import shutil as _shutil_mod
    from sherpa import store

    @contextlib.contextmanager
    def _fake_lock(_uid, _rel):
        yield

    def _boom_record(*_a, **_k):
        raise RuntimeError("db down (test)")

    _orig_move = _shutil_mod.move
    move_calls = {"n": 0}

    def _move_second_call_fails(src, dst):
        move_calls["n"] += 1
        if move_calls["n"] == 2:   # 1回目=run_dir→files/（成功させる）・2回目=差し戻し（失敗させる）
            # 実際の shutil.move の失敗（PermissionError 等）は例外の文字列表現に絶対パス
            # （src/dst 両方）を含む——ここでも同じ形を再現し、警告ログにそのまま出ないことを確認する。
            raise OSError(f"[Errno 13] Permission denied: '{src}' -> '{dst}'")
        return _orig_move(src, dst)

    monkeypatch.setattr(store, "workspace_file_lock", _fake_lock)
    monkeypatch.setattr(store, "no_live_upload_for_path", lambda *_a, **_k: True)
    monkeypatch.setattr(store, "record_workspace_file", _boom_record)
    monkeypatch.setattr(_shutil_mod, "move", _move_second_call_fails)

    uid = "move-back-fail-u1"
    ctx = A.Ctx(
        message="質問", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid=uid,
    )
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        envs = [e["env"] for e in A.CodexProvider().run(ctx) if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(envs) == 1, "完走していない"
    env = envs[0]
    from sherpa.providers.codex import provider as PV
    assert PV._CREATED_FILES_FAILURE_NOTE in env["headline"], f"注記が付いていない: {env['headline']!r}"

    assert any("moved back" in r.message and "orphaned in files" in r.message for r in caplog.records), \
        "差し戻し失敗が warning として明示的に記録されていない"
    for r in caplog.records:
        if "orphaned in files" in r.message:
            assert str(users_dir) not in r.message, "warning にフルパス（users_dir）が出ている"
            # 例外（絶対パス入り）の文字列表現そのものが出ていない（型と errno だけを記録する契約）。
            assert "Permission denied" not in r.message, "例外の文字列表現がそのまま warning に出ている"
            assert "Errno 13" not in r.message, "例外の文字列表現がそのまま warning に出ている"
            assert "type=OSError" in r.message and "errno=" in r.message, \
                f"例外の型・errno が記録されていない: {r.message!r}"

    users_root = pathlib.Path(users_dir).resolve()
    files_dir = users_root / uid / "workspace" / "files"
    assert (files_dir / "output.txt").is_file(), "差し戻しに失敗したファイルが files/ に見当たらない（想定どおり孤児として残るはず）"


def test_created_file_outright_move_failure_is_logged_without_leaking_path(monkeypatch, tmp_path, caplog):
    """成果物の最初の move（run_dir → files/）自体が失敗した場合（台帳登録の失敗ではなく move
    そのものの失敗）も、差し戻し失敗と同じく相対パス＋例外の型・errno だけを記録する——
    実際の `shutil.move` の失敗（`OSError`/`shutil.Error`）は文字列表現に失敗した src/dst の
    絶対パスを含むため、そのまま %s で出さないことを確認する。"""
    import logging
    import pathlib

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_codex_creates_file(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # 偽 codex は平文 agent_message を返す（run dir/台帳登録/会話ロックの検証が目的で出力スキーマは
    # 対象外）。`--output-schema`（既定 ON）だと平文は未完了扱いになり headline が変わってしまうため
    # 無効化する（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3 のスキーマ無効時契約）。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    users_dir = tmp_path / "users"
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_dir))

    import contextlib
    import shutil as _shutil_mod
    from sherpa import store

    @contextlib.contextmanager
    def _fake_lock(_uid, _rel):
        yield

    def _move_fails_outright(src, dst):
        # 実際の shutil.move の失敗（PermissionError 等）は例外の文字列表現に絶対パス
        # （src/dst 両方）を含む——ここでも同じ形を再現し、警告ログにそのまま出ないことを確認する。
        raise OSError(f"[Errno 13] Permission denied: '{src}' -> '{dst}'")

    monkeypatch.setattr(store, "workspace_file_lock", _fake_lock)
    monkeypatch.setattr(store, "no_live_upload_for_path", lambda *_a, **_k: True)
    monkeypatch.setattr(_shutil_mod, "move", _move_fails_outright)

    uid = "outright-move-fail-u1"
    ctx = A.Ctx(
        message="質問", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid=uid,
    )
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        envs = [e["env"] for e in A.CodexProvider().run(ctx) if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(envs) == 1, "完走していない"
    env = envs[0]
    from sherpa.providers.codex import provider as PV
    assert PV._CREATED_FILES_FAILURE_NOTE in env["headline"], f"注記が付いていない: {env['headline']!r}"

    matched = [r for r in caplog.records if "move/registration failed" in r.message]
    assert matched, "move 失敗が warning として記録されていない"
    for r in matched:
        assert str(users_dir) not in r.message, "warning にフルパス（users_dir）が出ている"
        assert "Permission denied" not in r.message, "例外の文字列表現がそのまま warning に出ている"
        assert "Errno 13" not in r.message, "例外の文字列表現がそのまま warning に出ている"
        assert "type=OSError" in r.message and "errno=" in r.message, \
            f"例外の型・errno が記録されていない: {r.message!r}"

    users_root = pathlib.Path(users_dir).resolve()
    run_dirs = list((users_root / uid / "workspace" / "authoring").glob("run-*"))
    assert len(run_dirs) == 1, f"run dir が想定どおり残っていない（削除されてしまった）: {run_dirs!r}"
    assert (run_dirs[0] / "output.txt").is_file(), "move 失敗後、ファイルが run_dir 側に残っていない"


def test_gather_seam_intercepted_by_codex_provider(monkeypatch):
    """RV LOW（2026-07-14 フェーズ5 2巡目）: `agents._gather` の facade patch が
    `CodexProvider._run_authoring`（`sherpa/providers/codex/provider.py` 側の呼び出し・facade
    実行時解決）にも効くことの**明示的な検知器**。`next(gen)` を数回進めるだけの浅い消費だと、
    provider.py がローカル束縛（`from ...base import _gather`）に退行しても素通りで通ってしまう
    （Codex RV 指摘）。ここでは fake `_gather` の sentinel node が実際に流れてくること＝fake が
    呼ばれたこと自体を assert する。HeuristicProvider/_GenProvider 経由の同種検知器は
    `tests/unit/test_agents_seams.py` にある。

    RV MEDIUM（3巡目）: 退行時（実 _gather がローカル束縛で走る場合）は可視イベント消費後の
    next() が subprocess.Popen に到達しうる＝実環境に codex CLI があると本物が起動してしまう。
    そのため subprocess.Popen 自体も monkeypatch で封じる（到達＝AssertionError＝検知として扱う。
    通常時は sentinel が最初のイベントなので Popen には近づかない）。"""
    calls = []

    def _no_popen(*_a, **_k):
        raise AssertionError("subprocess.Popen に到達（退行時の実 CLI 起動を封じるガード）")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    def fake_gather(ctx):
        calls.append(ctx)
        yield {"type": "node", "id": "seam-pin-codex", "kind": "think",
               "label": "t", "detail": "", "status": "done"}
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}}

    monkeypatch.setattr(A, "_gather", fake_gather)
    prov = A.CodexProvider()
    ctx = _ctx(lens="qa")
    ctx.uid = "seam-pin-codex-u1"
    gen = prov.run(ctx)
    seen = []
    try:
        for _ in range(8):    # sentinel が来るまで最大8イベント（退行時は実 _gather の実イベントが流れる）
            ev = next(gen)
            seen.append(ev)
            if isinstance(ev, dict) and ev.get("id") == "seam-pin-codex":
                break
    finally:
        gen.close()           # subprocess 起動前に必ず閉じる（退行時に実 codex を起動させない）
    assert calls, (
        "monkeypatch した agents._gather が CodexProvider 経由で呼ばれていない"
        f"（facade patch 素通り＝provider.py のローカル束縛化の可能性）。seen={seen!r}"
    )
    assert any(isinstance(e, dict) and e.get("id") == "seam-pin-codex" for e in seen), (
        f"fake _gather の sentinel node が run() の出力に現れない。seen={seen!r}"
    )


def test_run_authoring_refuses_when_mcp_disabled_and_layer_restricted(monkeypatch):
    """正典 §3.4「範囲と同じ硬いフィルタ」: MCP 無効（SHERPA_CODEX_MCP=0）で層（探す対象）が
    docs/code に限定されたターンは、直接 grep 経路では層を技術的に強制できないため Codex を
    一切起動せず、固定文言の honest failure を返す（未計測＝Popen に一度も到達しない・
    decision.reason に理由が残る＝監査で追える）。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_CODEX_MCP", "0")

    def _no_popen(*_a, **_k):
        raise AssertionError("MCP 無効＋層限定なのに Codex CLI が起動されている（実行しない契約に違反）")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    def fake_gather(ctx):
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}}

    monkeypatch.setattr(A, "_gather", fake_gather)
    prov = A.CodexProvider()
    ctx = _ctx(lens="qa", message="消費税率とは")
    ctx.uid = "mcp-off-layer-restricted-u1"
    ctx.scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    events = list(prov.run(ctx))
    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "_result")
    # 利用者向け文言は専門用語ゼロ（MCP を出さない）——具体語は decision.reason（監査専用）にのみ残す。
    assert result["env"]["headline"] == (
        "この構成では探す対象の限定はできません。管理者に設定の確認を依頼してください。")
    assert result["decision"]["reason"] == "MCP 無効時は探す対象の限定に対応できません"
    assert result["env"]["scope"]["layer"] == "code"
    assert result["env"]["scope"]["layer_applied"] is True
    assert result["env"]["data"] == {} and result["env"]["sources"] == []   # honest failure＝根拠なし
    assert "usage" not in result["env"]                                    # 未計測（Codex を起動していない）


def test_run_authoring_proceeds_when_mcp_disabled_but_layer_is_both(monkeypatch):
    """既定（layer=both・省略含む）は従来どおり——MCP 無効でも honest failure にしない。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_CODEX_MCP", "0")
    reached = []

    def _capture_popen(*_a, **_k):
        reached.append(True)
        raise OSError("popen intentionally not followed through in this test")

    monkeypatch.setattr(subprocess, "Popen", _capture_popen)

    def fake_gather(ctx):
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}}

    monkeypatch.setattr(A, "_gather", fake_gather)
    prov = A.CodexProvider()
    ctx = _ctx(lens="qa", message="消費税率とは")
    ctx.uid = "mcp-off-layer-both-u1"
    ctx.scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "both"}
    list(prov.run(ctx))
    assert reached, "layer=both では従来どおり Codex 起動を試みるはずが honest failure で早期終了した"


def test_run_dir_ignores_stale_authoring_leftovers(tmp_path, monkeypatch):
    """正典 §3.4「範囲と同じ硬いフィルタ」: 以前は authoring/.tmp を複数ターンで共有し、ターン開始時に
    毎回空にすることで前ターンの残存が層フィルタの迂回路にならないよう防いでいた。実行ごとに新規の
    run_dir（authoring/run-*）を cwd にする方式では、そもそも前ターン（や旧方式）の残存パスが
    今回の cwd になることが無い——旧方式の残存を模しておいても、今回のターンが実際に使う cwd は
    それとは別の新規 run-* ディレクトリになり、旧残存には一切触れない。"""
    import pathlib

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    _write_fake_codex_cwd(bin_dir, argv_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # 偽 codex は平文 agent_message を返す（run dir/台帳登録/会話ロックの検証が目的で出力スキーマは
    # 対象外）。`--output-schema`（既定 ON）だと平文は未完了扱いになり headline が変わってしまうため
    # 無効化する（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3 のスキーマ無効時契約）。
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    users_dir = tmp_path / "users"
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_dir))
    uid = "tmp-clear-u1"

    # 旧方式（authoring/.tmp を複数ターンで共有）の残存を模しておく。
    legacy_tmp = users_dir / uid / "workspace" / "authoring" / ".tmp"
    legacy_tmp.mkdir(parents=True)
    leftover = legacy_tmp / "leftover-from-previous-turn.txt"
    leftover.write_text("前ターンの内容の断片（想定: 層が限定される前の資料の一部）", encoding="utf-8")

    ctx = A.Ctx(
        message="質問", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid=uid,
        scope_meta={"world": "v1", "scope_paths": [], "source": "all", "layer": "both"},
    )
    envs = [e["env"] for e in A.CodexProvider().run(ctx) if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(envs) == 1 and envs[0]["headline"] == "done-ok", f"完走していない: {envs!r}"

    calls = _read_cwd_log(argv_log)
    assert len(calls) == 1, f"codex exec が1回だけ呼ばれるはず: {calls!r}"
    used_cwd = pathlib.Path(calls[0]["cwd"])
    assert used_cwd != legacy_tmp, "旧方式の authoring/.tmp をそのまま cwd に使ってしまっている"
    assert used_cwd.parent.name == "authoring" and used_cwd.name.startswith("run-"), (
        f"cwd が authoring/run-* 形式でない: {used_cwd}")
    assert not used_cwd.exists(), f"実行後も run dir が残っている（cleanup 未実施）: {used_cwd}"
    assert leftover.exists(), "旧方式の残存ファイルに手を出してしまっている（触れない契約のはず）"


def test_run_authoring_refuses_when_sandbox_disabled_and_layer_restricted(monkeypatch):
    """正典 §3.4: MCP が有効でも sandbox 無効（SHERPA_CODEX_SANDBOX=0）の fallback は
    `-s workspace-write`（読取全開）のため、MCP 経由の層フィルタと無関係に直接ファイル参照で
    迂回できる。MCP 無効時と同じ honest failure 分岐に統合し、Codex を一切起動しない。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    monkeypatch.delenv("SHERPA_CODEX_MCP", raising=False)   # MCP は既定 ON のまま

    def _no_popen(*_a, **_k):
        raise AssertionError("sandbox 無効＋層限定なのに Codex CLI が起動されている（実行しない契約に違反）")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    def fake_gather(ctx):
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}}

    monkeypatch.setattr(A, "_gather", fake_gather)
    prov = A.CodexProvider()
    ctx = _ctx(lens="qa", message="消費税率とは")
    ctx.uid = "sandbox-off-layer-restricted-u1"
    ctx.scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "docs"}
    events = list(prov.run(ctx))
    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "_result")
    assert result["decision"]["reason"] == "sandbox 無効時は探す対象の限定に対応できません"
    assert result["env"]["scope"]["layer"] == "docs"
    assert result["env"]["scope"]["layer_applied"] is True


def test_honest_failure_user_facing_text_has_no_internal_jargon(monkeypatch):
    """利用者向け固定文言・進捗表示には「MCP」「sandbox」という内部語を出さない
    （専門用語ゼロ・docs/04 §6）。具体的な理由は decision.reason（監査・管理者ログ専用）にのみ残す。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("SHERPA_CODEX_MCP", "0")

    def _no_popen(*_a, **_k):
        raise AssertionError("Codex CLI が起動されている")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    def fake_gather(ctx):
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}}

    monkeypatch.setattr(A, "_gather", fake_gather)
    prov = A.CodexProvider()
    ctx = _ctx(lens="qa", message="消費税率とは")
    ctx.uid = "jargon-free-u1"
    ctx.scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    events = list(prov.run(ctx))

    user_facing_texts = [e["text"] for e in events
                         if isinstance(e, dict) and e.get("type") == "answer_delta"]
    user_facing_texts += [n["detail"] for n in events
                          if isinstance(n, dict) and n.get("type") == "node"]
    for text in user_facing_texts:
        assert "MCP" not in text and "sandbox" not in text.lower(), text
    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "_result")
    assert result["env"]["headline"] == (
        "この構成では探す対象の限定はできません。管理者に設定の確認を依頼してください。")
    assert "MCP" in result["decision"]["reason"]   # 管理者ログ向けの理由には具体語を残してよい
