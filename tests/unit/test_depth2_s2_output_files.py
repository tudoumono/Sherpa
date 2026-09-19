"""DEPTH-2 S2（§2.7・docs/proposals/2026-09-17-深さの再定義とレビュー巡.md）単体テスト。

- write_output_file ツール本体（`agentic_search._run_write_output_file`）: 個人 workspace の
  files/ 台帳（`personal_workspace_files`）へ登録・marp:true の pdf/pptx 化（marp_render はモック）。
  要 Postgres（`_try_init` が DB down を skip）。
- author レンズが反復ツール検索へ接続され、`_AUTHOR_FALLBACK_NOTE`（下書き案内）を前置せず
  env["created_files"]/env["wrote_files"] が立つこと。成果物の登録は worker ではなく
  orchestrator ＝清書側で行う（API/Ollama は常にハイブリッド1経路）。
- 清書本文の `length` 打ち切り→追記継続（`_GenProvider._continue_truncated_headline`）: 重複
  させない・回数上限で止まる・未完了のまま（completion.truncated 相当）を完成として数えない。
  主張構造（claims JSON・S1）はこの継続の対象外——本ファイルでは触れない（既存 S1 テストの
  無改修グリーンで担保・投入する `_agentic_loop` の final は claims を持たない）。
"""
from __future__ import annotations

import json
import os
import time

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest

from sherpa import agentic_search as A
from sherpa import store
from sherpa.providers.base import Ctx, _GenProvider
from sherpa.providers.prompts import _AUTHOR_FALLBACK_NOTE, _AUTHOR_NO_EVIDENCE_HEADLINE

_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 実在ファイル


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(tag: str) -> str:
    uid = f"unit-depth2s2-{tag}"
    store.upsert_user(uid, display_name="D2S2", password_hash="x", status="active")
    return uid


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


# ===== write_output_file ツール本体 =====

def test_write_output_file_registers_ledger_row_and_download_url(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    result = A._run_write_output_file({"filename": "一覧.md", "content": "# 消費税率\n8%/10%"}, uid)
    assert "error" not in result, result
    assert result["rel_path"] == "一覧.md"
    assert result["download_url"].startswith("/workspace/files/")
    assert result["download_url"].endswith("/download")

    rows = store.list_workspace_files(uid)
    assert any(r["rel_path"] == "一覧.md" for r in rows)
    dst = tmp_path / "users" / uid / "workspace" / "files" / "一覧.md"
    assert dst.read_text(encoding="utf-8") == "# 消費税率\n8%/10%"


def test_write_output_file_no_uid_is_error_and_does_not_raise():
    result = A._run_write_output_file({"filename": "x.md", "content": "本文"}, None)
    assert "error" in result


def test_write_output_file_rejects_path_traversal_filename(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    for bad in ("../escape.md", "a/b.md", "..", "."):
        result = A._run_write_output_file({"filename": bad, "content": "x"}, uid)
        assert "error" in result, f"{bad!r} が拒否されていない: {result}"


def test_write_output_file_avoids_overwrite_with_suffix(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    r1 = A._run_write_output_file({"filename": "重複.md", "content": "1回目"}, uid)
    r2 = A._run_write_output_file({"filename": "重複.md", "content": "2回目"}, uid)
    assert r1["rel_path"] == "重複.md"
    assert r2["rel_path"] != "重複.md"   # 上書きせず別名（重複_1.md 等）
    assert r2["rel_path"].startswith("重複")
    rows = {r["rel_path"] for r in store.list_workspace_files(uid)}
    assert r1["rel_path"] in rows and r2["rel_path"] in rows


def test_write_output_file_rejects_symlinked_workspace_of_another_user(tmp_path, monkeypatch):
    """C38 是正: `users/{uid}/workspace` が他人の workspace への symlink に差し替えられていると、
    「実体が users_dir 配下か」だけの検査（旧実装）は素通りしてしまう——実体が**本人の**
    `users_dir/{uid}/workspace/` に一致することまで検証し、他人の workspace へは書けない。"""
    _try_init()
    users_root = tmp_path / "users"
    tag = _sfx()
    uid_a, uid_b = _mk_user(tag + "a"), _mk_user(tag + "b")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_root))

    (users_root / uid_b / "workspace").mkdir(parents=True)
    (users_root / uid_a).mkdir(parents=True)
    (users_root / uid_a / "workspace").symlink_to(users_root / uid_b / "workspace")

    result = A._run_write_output_file({"filename": "乗っ取り.md", "content": "x"}, uid_a)
    assert "error" in result, f"他人 workspace への symlink 経由の書込みが拒否されていない: {result}"
    assert not (users_root / uid_b / "workspace" / "files" / "乗っ取り.md").exists()
    rows = store.list_workspace_files(uid_a)
    assert not any(r["rel_path"] == "乗っ取り.md" for r in rows)


def test_write_output_file_rejects_toctou_swap_of_files_dir_after_check(tmp_path, monkeypatch):
    """C42 是正: C38 の「各階層が symlink でないか確認してから resolve() で実体を照合する」検査は、
    検査（stat/resolve）と実際の作成（open/write）の間に window があり、その間に `files`（または
    その親）が他人 workspace への symlink へ差し替えられても検出できない（TOCTOU）——旧実装では
    `resolve()` が差替え後の実体を辿ってしまい、検査を素通りして他人の workspace/files へ書けた。

    ここでは `files` を開く直前（前段の `workspace` オープンという「検査」の直後）に、攻撃者が
    `files` という名前を他人 workspace の files への symlink へ差し替える窓を再現する。dir_fd による
    段階的オープン（各階層を `O_NOFOLLOW` で個別に開く）なら、差し替えられた名前を開こうとした
    瞬間に ELOOP で拒否され、他人側にはファイルが作られない。"""
    _try_init()
    users_root = tmp_path / "users"
    tag = _sfx()
    uid_a, uid_b = _mk_user(tag + "a"), _mk_user(tag + "b")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_root))

    (users_root / uid_b / "workspace" / "files").mkdir(parents=True)
    (users_root / uid_a / "workspace").mkdir(parents=True)

    real_open_dir_fd = A._open_workspace_dir_fd

    def _swap_files_then_open(parent_fd: int, name: str) -> int:
        if name == "files":
            # 前段（`workspace`）の検査が終わった直後・`files` を開く直前に、攻撃者が
            # `files` という名前を他人 workspace の files への symlink へ差し替える。
            (users_root / uid_a / "workspace" / "files").symlink_to(
                users_root / uid_b / "workspace" / "files")
        return real_open_dir_fd(parent_fd, name)

    monkeypatch.setattr(A, "_open_workspace_dir_fd", _swap_files_then_open)

    result = A._run_write_output_file({"filename": "乗っ取り2.md", "content": "x"}, uid_a)
    assert "error" in result, f"TOCTOU 差替え後も書込みが拒否されていない: {result}"
    assert not (users_root / uid_b / "workspace" / "files" / "乗っ取り2.md").exists()
    rows = store.list_workspace_files(uid_a)
    assert not any(r["rel_path"] == "乗っ取り2.md" for r in rows)


def test_write_output_file_registration_failure_does_not_leave_orphan(tmp_path, monkeypatch):
    """台帳登録（`record_workspace_file`）が失敗したら error を返し、files/ に台帳の無い
    孤児ファイルを残さない（fail-closed・Codex 経路と同じ規律）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    def _boom(*_a, **_k):
        raise RuntimeError("db down (test)")

    monkeypatch.setattr(store, "record_workspace_file", _boom)
    result = A._run_write_output_file({"filename": "失敗.md", "content": "x"}, uid)
    assert "error" in result
    dst = tmp_path / "users" / uid / "workspace" / "files" / "失敗.md"
    assert not dst.exists(), "登録失敗なのにファイルが残っている（孤児）"


# ===== marp:true → marp_render 経路（マージ・モック） =====

def test_write_output_file_marp_true_calls_marp_render_and_registers_rendered(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    from sherpa import marp_render
    calls = {"render": 0}

    def fake_render_outputs(md_paths, *, marp_bin, chrome_path, theme_dirs, containment_root, timeout=180):
        calls["render"] += 1
        out = []
        for p in md_paths:
            pdf = p.with_suffix(".pdf")
            pdf.write_bytes(b"%PDF-fake")
            out.append(pdf)
        return out

    monkeypatch.setattr(marp_render, "is_marp_markdown", lambda p: True)
    monkeypatch.setattr(marp_render, "render_outputs", fake_render_outputs)

    result = A._run_write_output_file(
        {"filename": "スライド.md", "content": "---\nmarp: true\n---\n# タイトル", "marp": True}, uid)

    assert "error" not in result, result
    assert calls["render"] == 1, "marp_render.render_outputs が呼ばれていない"
    assert result.get("rendered"), f"rendered が無い: {result}"
    assert result["rendered"][0]["rel_path"] == "スライド.pdf"
    assert result["rendered"][0]["download_url"].startswith("/workspace/files/")

    rows = {r["rel_path"] for r in store.list_workspace_files(uid)}
    assert "スライド.md" in rows and "スライド.pdf" in rows


def test_write_output_file_marp_render_failure_is_fail_open(tmp_path, monkeypatch):
    """marp 変換の失敗は注記だけで継続する（.md 自体の保存は成功のまま・fail-open）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    from sherpa import marp_render

    def _boom(*_a, **_k):
        raise RuntimeError("marp crashed (test)")

    monkeypatch.setattr(marp_render, "is_marp_markdown", lambda p: True)
    monkeypatch.setattr(marp_render, "render_outputs", _boom)

    result = A._run_write_output_file(
        {"filename": "壊れる.md", "content": "---\nmarp: true\n---\n# x", "marp": True}, uid)
    assert "error" not in result, "marp 失敗で全体がエラーになってはいけない（fail-open）"
    assert result.get("marp_note"), "marp 失敗の注記が無い"
    rows = {r["rel_path"] for r in store.list_workspace_files(uid)}
    assert "壊れる.md" in rows   # .md 自体は保存されている


# ===== author レンズの反復ツール検索への接続（(a)+(b)）=====

class _AuthorOpenAI(_GenProvider):
    """subprocess/HTTP を使わない _GenProvider 具象クラス（`agentic_search.openai_style` を
    実際に通す・`_post` だけをモックする）。`OpenAIProvider._agentic_loop`（`sherpa/providers/openai.py`）
    と同じく `uid=ctx.uid` を転送する（write_output_file の書き先を決める）。"""
    label, model, provider_id = "T", "gpt-test", "openai"
    _natural_completion_reasons = frozenset({"stop"})

    def _agentic_loop(self, ctx):
        return A.openai_style(
            "http://x", {}, self.model, A.SYSTEM, ctx.message, ctx.world,
            (ctx.scope_meta or {}).get("scope_paths"), max_turns=4,
            tools_availability=ctx.tools_availability, uid=ctx.uid)


def _author_ctx(uid, message="消費税率の一覧をExcelにまとめて"):
    return Ctx(
        message=message, world="v1", knowledge=True,
        route=lambda m: {"lens": "author", "reason": "test", "input": m, "confident": True},
        dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}},
        make_sources=lambda docs: [], uid=uid,
        scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
        tools_availability={"grep": True, "fulltext": False, "graph": False})


def test_author_lens_calls_write_output_file_registers_and_skips_fallback_note(tmp_path, monkeypatch):
    """受け入れ条件(1): author レンズの依頼（モック LLM が write_output_file を呼ぶ）で
    個人 workspace の files 台帳に成果物が登録され、env["created_files"] に download URL が入り、
    _AUTHOR_FALLBACK_NOTE（下書き案内）が本文に含まれない。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    calls = {"n": 0}

    def fake_post(url, headers, body, timeout=90):
        calls["n"] += 1
        names = [t["function"]["name"] for t in body.get("tools", [])]
        if calls["n"] == 1:
            assert "write_output_file" in names, f"ツール一覧に write_output_file が無い: {names}"
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "write_output_file",
                                          "arguments": '{"filename": "一覧.md", "content": "# 消費税率\\n8%/10%"}'}}
            ]}}]}
        return {"choices": [{"message": {"content": "一覧.md を作成しました。"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(A, "_post", fake_post)

    events = list(_AuthorOpenAI().run(_author_ctx(uid)))
    result = next(e for e in events if e.get("type") == "_result")
    env = result["env"]
    assert _AUTHOR_FALLBACK_NOTE not in env["headline"]
    assert env.get("created_files"), f"created_files が無い: {env}"
    card = env["created_files"][0]
    assert card["name"] == "一覧.md"
    assert card["download_url"].startswith("/workspace/files/")
    assert env.get("wrote_files")

    rows = store.list_workspace_files(uid)
    assert any(r["rel_path"] == "一覧.md" for r in rows)


# ===== 清書本文の length 打ち切り→追記継続 =====

class _ContinuationFake(_GenProvider):
    """`_agentic_loop` は truncated 済みの final を1回返し、`_stream`（継続の各ラウンド）は
    呼ぶたびに固定文言＋finish_reason を返す（回数は `self.stream_calls` に記録）。"""
    label, model, provider_id = "T", "m", "openai"
    _natural_completion_reasons = frozenset({"stop"})

    def __init__(self, pieces):
        super().__init__()
        self.stream_calls = 0
        self._pieces = pieces   # [(text, finish_reason), ...]

    def _agentic_loop(self, ctx):
        yield {"final": "最初の部分は途中で切れ", "docs": {_REAL_DOC}, "searched": True,
              "cites": [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "障害記録"}], "cards": [],
              "stop_reason": "truncated"}

    def _stream(self, prompt, completion=None):
        text, reason = self._pieces[min(self.stream_calls, len(self._pieces) - 1)]
        self.stream_calls += 1
        if completion is not None:
            completion.terminal_seen = True
            completion.reason = reason
        yield text


def _qa_ctx(message="続きを教えて"):
    return Ctx(
        message=message, world="v1", knowledge=True,
        route=lambda m: {"lens": "qa", "reason": "test", "input": m, "confident": True},
        dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}},
        make_sources=lambda docs: [],
        tools_availability={"grep": True, "fulltext": False, "graph": False})


def test_length_truncated_headline_continues_without_duplicating_and_resolves(monkeypatch):
    """受け入れ条件(3) 前半: length で切れた本文の続きが追記され（重複しない）、最終的に
    自然完了へ再分類される。"""
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "3")
    provider = _ContinuationFake([("続き1", "length"), ("続き2", "length"), ("続き3で完了", "stop")])
    events = list(provider.run(_qa_ctx()))
    result = next(e for e in events if e.get("type") == "_result")
    env = result["env"]
    assert env["headline"] == "最初の部分は途中で切れ続き1続き2続き3で完了"
    deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
    assert deltas == ["最初の部分は途中で切れ", "続き1", "続き2", "続き3で完了"], deltas
    assert provider.stream_calls == 3
    # 解消後は自然完了へ再分類（未完了のまま完成として数えない、の裏側＝解消したら完成として数える）。
    assert env["data"]["evidence_packet"]["stop_reason"] == "no_tool_calls"


def test_length_truncated_headline_stops_at_round_cap_and_stays_incomplete(monkeypatch):
    """受け入れ条件(3) 後半: 常に length で切れ続けても、回数上限（既存の
    `SHERPA_CODEX_AUTO_CONTINUE` を流用）で止まり、未完了のまま完成として数えない
    （stop_reason は "truncated" のまま＝完了扱いにならない）。"""
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "2")
    provider = _ContinuationFake([("常に途中", "length")])   # 何度呼んでも length のまま
    events = list(provider.run(_qa_ctx()))
    result = next(e for e in events if e.get("type") == "_result")
    env = result["env"]
    assert provider.stream_calls == 2, f"上限2ラウンドで止まっていない: {provider.stream_calls}"
    assert env["headline"] == "最初の部分は途中で切れ常に途中常に途中"
    assert env["data"]["evidence_packet"]["stop_reason"] == "truncated", \
        "上限到達後も未完了のまま＝完成として数えてはいけない"


def test_length_continuation_not_triggered_when_not_truncated(monkeypatch):
    """対照: stop_reason が truncated でなければ継続ラウンドを一切発行しない（無駄な追加呼び出し無し）。"""
    provider = _ContinuationFake([("呼ばれないはず", "length")])

    class _NoTruncFake(_ContinuationFake):
        def _agentic_loop(self, ctx):
            yield {"final": "完結した本文", "docs": {_REAL_DOC}, "searched": True,
                  "cites": [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "障害記録"}], "cards": [],
                  "stop_reason": "no_tool_calls"}

    provider = _NoTruncFake([("呼ばれないはず", "length")])
    events = list(provider.run(_qa_ctx()))
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["headline"] == "完結した本文"
    assert provider.stream_calls == 0, "truncated でないのに継続ラウンドが発行された"


# ===== 根拠ゲート: write_output_file は author レンズだけの代替根拠 =====

def test_qa_lens_single_write_output_file_does_not_satisfy_evidence_gate():
    """C39/#44 是正: `write_output_file` が台帳登録に成功した成果物（`created_files`）は、
    lens=="author" のときだけ根拠ゲートの代替になる——qa 等は citation/構造的根拠/精読の
    どれも無いまま書込み1回だけで根拠ゲートを迂回できない（evidence below threshold のまま）。"""
    class _QaWriteOnly(_GenProvider):
        label, model, provider_id = "T", "m", "openai"
        _natural_completion_reasons = frozenset({"stop"})

        def _agentic_loop(self, ctx):
            yield {"final": "作成しました", "docs": set(), "searched": True, "cites": [],
                  "cards": [], "stop_reason": "no_tool_calls",
                  "created_files": [{"rel_path": "一覧.md",
                                     "download_url": "/workspace/files/1/download"}]}

    provider = _QaWriteOnly()
    ctx = _qa_ctx()
    with pytest.raises(RuntimeError, match="evidence below threshold"):
        list(provider._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))


# ===== #45: 継続しないターンで synthesis_truncated を偽計上しない =====

def test_synthesis_truncated_not_recorded_when_turn_does_not_continue(monkeypatch):
    """#45 是正: 継続ラウンドを発行しないターン（`stop_reason != "truncated"`）では、清書
    ダイジェスト自体を組まない——組んでダイジェストの打ち切りだけを申告すると、実際には走って
    いない追記継続の分が打ち切りの内訳（利用統計 `limits.synthesis_truncated`）へ偽計上される。"""
    from sherpa import agentic_search as AS

    calls = {"n": 0}

    def _always_truncated(*a, **kw):
        calls["n"] += 1
        return "digest", {}, True   # ダイジェスト自体は常に打ち切りありと申告する（対照用の細工）

    monkeypatch.setattr(AS, "build_synthesis_digest", _always_truncated)

    class _NoTruncFake(_ContinuationFake):
        def _agentic_loop(self, ctx):
            yield {"final": "完結した本文", "docs": {_REAL_DOC}, "searched": True,
                  "cites": [{"doc_id": _REAL_DOC, "span": [1, 1], "quote": "障害記録"}], "cards": [],
                  "stop_reason": "no_tool_calls"}

    provider = _NoTruncFake([("呼ばれないはず", "length")])
    events = list(provider.run(_qa_ctx()))
    result = next(e for e in events if e.get("type") == "_result")
    assert calls["n"] == 0, "継続しないターンでダイジェストを組んでいる（無駄な計算・偽計上の温床）"
    assert not result["env"].get("limits", {}).get("synthesis_truncated"), \
        f"継続しないターンで synthesis_truncated が偽計上された: {result['env'].get('limits')}"


def test_synthesis_truncated_not_recorded_when_auto_continue_cap_is_zero(monkeypatch):
    """C43 是正: `stop_reason == "truncated"` でダイジェストを組んでも（`_will_continue` 真）、
    `SHERPA_CODEX_AUTO_CONTINUE=0`（上限0）では継続ラウンドが1回も発行されない
    （`_cont_rounds == 0`）——このとき `synthesis_truncated` を計上すると、実際には走っていない
    継続の分が打ち切りの内訳（利用統計）へ偽計上される。#45 の是正は `stop_reason` だけで
    ゲートしており、この上限0のケースを見落としていた。"""
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "0")
    from sherpa import agentic_search as AS

    def _always_truncated(*a, **kw):
        return "digest", {}, True   # ダイジェスト自体は常に打ち切りありと申告する（対照用の細工）

    monkeypatch.setattr(AS, "build_synthesis_digest", _always_truncated)

    provider = _ContinuationFake([("呼ばれないはず", "length")])   # 上限0＝_stream は1回も呼ばれない
    events = list(provider.run(_qa_ctx()))
    result = next(e for e in events if e.get("type") == "_result")
    assert provider.stream_calls == 0, "上限0なのに継続ラウンドが発行された"
    assert result["env"]["data"]["evidence_packet"]["stop_reason"] == "truncated"
    assert not result["env"].get("limits", {}).get("synthesis_truncated"), \
        f"継続0ラウンドのターンで synthesis_truncated が偽計上された: {result['env'].get('limits')}"


# ===== author（作成系）× 頭脳自身を worker にしたハイブリッド =====

_LAW_DOC = "4期/01_標準/消費税法.md"
_RERUN_MARK = "前回の調査で不足していた観点"   # 再調査依頼（`rerun_instruction`）の目印


def _self_worker_sub() -> dict:
    """`get_provider` が `search_helper` 空のときに組むのと同じ worker（頭脳自身）。"""
    from sherpa import search_helper
    return search_helper.self_worker("openai", "gpt-test", key="sk-x")


# 本体（orchestrator）自身のソース確認が必須になった契約のため、判定の前に必ず 1 回だけ読む
# ソース種別の実在ファイル（fixtures/corpus/v1）。
_SRC_DOC = "4期/03_開発/01_ソース/TAXCALC.cbl"


class _HybridAuthor(_AuthorOpenAI):
    """頭脳自身を worker にしたハイブリッド（API/Ollama の唯一の経路）。`_stream` は
    `(本文, 完了理由)` の列を順に返す＝evaluator の判定 → 清書 → 追記継続の順に消費される。

    必須の「本体自身のソース確認」の回だけは応答列を消費せずソース本文の読取指示を返す
    （`prompts` にも積まない＝各テストが数えている「判定・清書・追記継続」の並びを保つ）。
    """

    def __init__(self, stream_seq):
        super().__init__()
        self._sub = _self_worker_sub()
        self._seq = list(stream_seq)
        self.prompts: list = []

    def _stream(self, prompt, completion=None):
        if "ソース種別のファイル" in prompt and "【ツール結果】" not in prompt:
            yield json.dumps({"action": "read_around", "doc_id": _SRC_DOC, "line": 10})
            return
        self.prompts.append(prompt)
        text, reason = self._seq.pop(0)
        if completion is not None:
            completion.terminal_seen = True
            completion.reason = reason
        yield text


def _author_ctx_depth(uid, depth="quick", stop_event=None, lens="author"):
    """作成系（author）の Ctx（`depth` 省略＝クイック＝見直し 0 巡・"standard" で巡ループを回す）。"""
    scope = {"world": "v1", "scope_paths": [], "source": "all"}
    if depth:
        scope["depth_profile"] = depth
    return Ctx(
        message="消費税率の一覧をExcelにまとめて", world="v1", knowledge=True,
        route=lambda m: {"lens": lens, "reason": "test", "input": m, "confident": True},
        dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}},
        make_sources=lambda docs: [], uid=uid, scope_meta=scope, stop_event=stop_event,
        tools_availability={"grep": True, "fulltext": False, "graph": False})


def _worker_round(tool_call):
    """worker（下調べ役）1巡分の `_post` 応答列: ツール1回 → 散文（破棄される）→ 一次判断。"""
    return [tool_call,
            {"choices": [{"message": {"content": "WORKER DRAFT (discarded)"},
                          "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": '{"claims": []}'}}]}]


def test_author_hybrid_self_worker_registers_output_file(tmp_path, monkeypatch):
    """author は「頭脳自身を worker にしたハイブリッド」で動き、成果物の登録（`write_output_file`
    と同じ台帳・カード・ダウンロード導線）は worker ではなく orchestrator ＝清書側で行う。
    ファイル名は清書側の LLM 呼び出し1回で決める。下書き案内（`_AUTHOR_FALLBACK_NOTE`）へは
    縮退しない。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    provider = _HybridAuthor([("# 消費税率\n8%/10%", "stop"),
                              ('{"filename": "消費税率一覧.md", "marp": false}', "stop")])
    events = list(provider.run(_author_ctx_depth(uid)))
    env = next(e for e in events if e.get("type") == "_result")["env"]
    assert _AUTHOR_FALLBACK_NOTE not in env["headline"]
    assert env["headline"] == "# 消費税率\n8%/10%"      # 短い成果物は本文もそのまま出す
    assert [c["name"] for c in env.get("created_files") or []] == ["消費税率一覧.md"], \
        f"清書側の成果物登録が行われていない: {env.get('created_files')}"
    assert env.get("wrote_files") == ["消費税率一覧.md"]
    assert env.get("_personal_rounds") is True
    rows = store.list_workspace_files(uid)
    assert any(r["rel_path"] == "消費税率一覧.md" for r in rows)
    # 根拠種別の不足の注記は清書へ渡さない＝成果物の中身にも本文にも入らない（規律は主張単位の
    # 格下げで効かせる）。`prompts[0]` が清書、`prompts[1]` がファイル名決め。
    assert "【根拠の不足】" not in provider.prompts[0]
    assert "確認できていません" not in env["headline"]


def test_qa_hybrid_does_not_register_output_file(tmp_path, monkeypatch):
    """対照: 作成系でないレンズ（qa）の清書は短い本文をファイル登録しない（従来どおり
    清書予算超過のときだけオフロードする）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "read_around",
                                  "arguments": '{"doc_id": "%s", "line": 3}' % _LAW_DOC}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    provider = _HybridAuthor([("消費税率は10%です。", "stop")])
    env = next(e for e in provider.run(_author_ctx_depth(uid, lens="qa"))
               if e.get("type") == "_result")["env"]
    assert not env.get("created_files")
    assert not env.get("wrote_files")


def _round_of(body) -> str:
    """この `_post` 呼び出しがどの巡の下調べか（帰属判定の呼び出しは "attr"）。
    呼び出し回数で数えると、巡の途中に挟まる帰属判定1回分だけ番号がずれる。"""
    if [t["function"]["name"] for t in body.get("tools", [])] == ["submit_attribution"]:
        return "attr"
    return "r2" if _RERUN_MARK in str(body.get("messages", [])) else "r1"


def test_rerun_round_read_evidence_reaches_continuation_prompt(tmp_path, monkeypatch):
    """追記継続の入力には**再調査巡の**精読本文が入る——初回 final の控えだけを渡していると、
    2巡目に読んだ原文が続きの生成へ渡らない（初回より薄い根拠で書き足してしまう）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "1")
    seen = {"r1": 0, "r2": 0}

    def fake_post(url, headers, body, timeout=90):
        rnd = _round_of(body)
        if rnd == "attr":
            return {"choices": [{"message": {"content": ""}}]}
        seen[rnd] += 1
        doc = _REAL_DOC if rnd == "r1" else _LAW_DOC   # 巡ごとに別の資料を精読する
        if seen[rnd] == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c0", "function": {"name": "read_around",
                                          "arguments": '{"doc_id": "%s", "line": 3}' % doc}}
            ]}}]}
        if seen[rnd] == 2:
            return {"choices": [{"message": {"content": "WORKER DRAFT (discarded)"},
                                 "finish_reason": "stop"}]}
        return {"choices": [{"message": {"content": '{"claims": []}'}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    provider = _HybridAuthor([
        ('{"sufficient": false, "missing": "税率の適用開始日", "findings": []}', "stop"),
        ('{"sufficient": true, "missing": "", "findings": []}', "stop"),
        ("2巡目の途中まで", "length"),      # 清書が length で切れる
        ("続き", "stop")])
    list(provider.run(_author_ctx_depth(uid, depth="standard")))
    cont_prompts = provider.prompts[3:]     # 査読2回＋清書1回のあとが追記継続
    assert cont_prompts, "追記継続が発行されていない"
    assert "法令上の規約" in cont_prompts[0], \
        "再調査巡の精読本文が追記継続の入力に入っていない"


def test_stop_during_continuation_returns_stopped_terminal(tmp_path, monkeypatch):
    """追記継続の最中に停止要求が来たら停止終端を返す——停止確認をせずに通常終端を返すと、
    consumer（`chat_service`）が停止後の結果を破棄して回答が1件も残らない。"""
    import threading
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "2")
    stop = threading.Event()

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"障害"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    class _StopDuringContinuation(_HybridAuthor):
        def _stream(self, prompt, completion=None):
            if len(self.prompts) == 2:      # 追記継続の応答中に利用者が停止する
                stop.set()
            return super()._stream(prompt, completion)

    provider = _StopDuringContinuation([
        ('{"sufficient": true, "missing": "", "findings": []}', "stop"),
        ("途中まで", "length"),
        ("続き", "stop")])
    events = list(provider.run(_author_ctx_depth(uid, depth="standard", stop_event=stop)))
    env = next(e for e in events if e.get("type") == "_result")["env"]
    assert env.get("_terminal") == "stopped", \
        f"追記継続中の停止が停止終端になっていない: {env.get('_terminal')}"
    assert env.get("stopped_by_user") is True


def test_author_hybrid_marp_output_is_rendered_and_noted(tmp_path, monkeypatch):
    """作成系の成果物は清書側（orchestrator）が `write_output_file` として登録する——ファイル名と
    `marp: true` は清書側の LLM 呼び出し1回で決まり、marp の pdf/pptx 変換経路を通って
    `rendered` が成果物カードに、`marp_note` が回答本文に載る（marp_render はモック）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    from sherpa import marp_render
    calls = {"render": 0}

    def fake_render_outputs(md_paths, *, marp_bin, chrome_path, theme_dirs, containment_root, timeout=180):
        calls["render"] += 1
        out = []
        for p in md_paths:
            pdf = p.with_suffix(".pdf")
            pdf.write_bytes(b"%PDF-fake")
            out.append(pdf)
        return out

    monkeypatch.setattr(marp_render, "is_marp_markdown", lambda p: True)
    monkeypatch.setattr(marp_render, "render_outputs", fake_render_outputs)

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    provider = _HybridAuthor([("---\nmarp: true\n---\n# 消費税率", "stop"),
                              ('{"filename": "消費税率スライド.md", "marp": true}', "stop")])
    events = list(provider.run(_author_ctx_depth(uid)))
    env = next(e for e in events if e.get("type") == "_result")["env"]

    assert calls["render"] == 1, "marp の変換経路が呼ばれていない"
    assert [c["name"] for c in env.get("created_files") or []] == \
        ["消費税率スライド.md", "消費税率スライド.pdf"], env.get("created_files")
    assert env.get("wrote_files") == ["消費税率スライド.md", "消費税率スライド.pdf"]
    assert "pptx" in env["headline"], f"marp の注記が回答に載っていない: {env['headline']}"
    rows = {r["rel_path"] for r in store.list_workspace_files(uid)}
    assert {"消費税率スライド.md", "消費税率スライド.pdf"} <= rows
    # ファイル名を決める呼び出しは確定した本文を入力にする（本文を作り直させない）。
    assert "消費税率" in provider.prompts[-1]


def test_author_hybrid_save_failure_is_failed_terminal_keeping_body(tmp_path, monkeypatch):
    """成果物を登録できなかった作成系のターンは正常終端にしない（本文は残したまま失敗の印と
    理由の注記を付ける）——保存できていないのに「作成しました」と見えるのを防ぐ。"""
    _try_init()
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    provider = _HybridAuthor([("# 消費税率\n8%/10%", "stop"),
                              ('{"filename": "一覧.md", "marp": false}', "stop")])
    # uid なし＝登録先が特定できない（`_run_write_output_file` が明示エラーを返す実失敗）。
    env = next(e for e in provider.run(_author_ctx_depth(None)) if e.get("type") == "_result")["env"]

    assert env.get("_terminal") == "failed", f"失敗終端になっていない: {env.get('_terminal')}"
    assert env.get("agentic_failure"), "失敗の印が無い"
    assert "# 消費税率" in env["headline"], "本文が破棄されている"
    assert "保存できませんでした" in env["headline"], env["headline"]
    assert not env.get("created_files")


def test_author_uses_self_worker_even_when_cheap_helper_is_configured(tmp_path, monkeypatch):
    """作成系は設定に依らず「頭脳自身を worker」にする——安いモデルの下調べ役を設定していても
    author のターンだけ頭脳と同じ接続・モデルの worker に差し替え、ターン後は元へ戻す。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    cheap = {"tools": frozenset({"ripgrep_search"}), "guard": {"min_citations": 1, "max_turns": 4,
                                                              "llm_timeout": 60},
             "profile_id": "search-helper-ollama", "provider": "ollama",
             "url": "http://localhost:11434", "model": "qwen2.5"}
    provider = _HybridAuthor([("# 消費税率", "stop"), ('{"filename": "一覧.md"}', "stop")])
    provider._sub = dict(cheap)
    provider._key = "sk-x"   # `get_provider` が差し替え先へ渡すのと同じ頭脳自身の接続情報

    events = list(provider.run(_author_ctx_depth(uid)))
    run_ids = {e.get("agent_run_id") for e in events if e.get("agent_run_id")}
    assert run_ids == {"sub:search-helper-self:1"}, f"頭脳自身の worker で動いていない: {run_ids}"
    env = next(e for e in events if e.get("type") == "_result")["env"]
    assert [c["name"] for c in env.get("created_files") or []] == ["一覧.md"], env.get("created_files")
    assert provider._sub == cheap, "ターン後に元の下調べ役設定へ戻っていない"


def test_author_stop_during_attribution_does_not_register_output(tmp_path, monkeypatch):
    """帰属処理の最中に利用者が停止したら、成果物を登録せず停止終端で終える
    （停止したターンの成果物を個人 workspace に残さない）。"""
    import threading
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    stop = threading.Event()

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    class _StopDuringAttribution(_HybridAuthor):
        def _attribute(self, text, digest, ev_map, call_budget=None):
            stop.set()   # 帰属呼び出しの最中に利用者が停止する
            return set()

    provider = _StopDuringAttribution([('{"verdict": "sufficient", "missing": "", "findings": []}', "stop"),
                                       ("# 消費税率", "stop")])
    events = list(provider.run(_author_ctx_depth(uid, depth="standard", stop_event=stop)))
    env = next(e for e in events if e.get("type") == "_result")["env"]
    assert env.get("_terminal") == "stopped", f"停止終端になっていない: {env.get('_terminal')}"
    assert not env.get("created_files")
    assert not store.list_workspace_files(uid), "停止したターンで成果物が登録されている"


def _no_citation_worker_seq():
    """corpus citation が1件も出ない worker 1巡分（根拠ゲートの対象になる author ターン用）。"""
    return _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search",
                                  "arguments": '{"query":"ZZZ-NO-SUCH-TOKEN-ZZZ"}'}}]}}]})


def test_author_without_created_file_does_not_bypass_evidence_gate(tmp_path, monkeypatch):
    """根拠ゲートの免除は「作成系かつ実際に成果物を登録できたターン」だけ——登録に至らなかった
    author のターンは根拠0件のまま＝通常終端で通さず、未検証の生成本文も回答として残さない。"""
    _try_init()
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _no_citation_worker_seq()
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    provider = _HybridAuthor([("# 消費税率", "stop"), ('{"filename": "一覧.md"}', "stop")])
    # uid なし＝登録先が特定できない（`_run_write_output_file` の明示エラー）＝登録に至らない。
    env = next(e for e in provider.run(_author_ctx_depth(None)) if e.get("type") == "_result")["env"]
    assert env.get("agentic_failure"), f"根拠0件の author が通常終端で通った: {env}"
    assert not env.get("created_files")
    assert _AUTHOR_NO_EVIDENCE_HEADLINE in env["headline"], env["headline"]
    assert "# 消費税率" not in env["headline"], \
        f"根拠ゲート未達の生成本文が回答として残っている: {env['headline']}"


def test_author_with_created_file_passes_evidence_gate_without_citations(tmp_path, monkeypatch):
    """作成系の根拠ゲートは清書・登録の**後**に最終判定する——corpus citation が0件でも成果物を
    登録できたターンは正常終端（新規に文書を「作る」依頼は引用元を持たないのが普通）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _no_citation_worker_seq()
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    provider = _HybridAuthor([("# 消費税率", "stop"),
                              ('{"filename": "一覧.md", "marp": false}', "stop")])
    env = next(e for e in provider.run(_author_ctx_depth(uid)) if e.get("type") == "_result")["env"]
    assert not env.get("agentic_failure"), f"登録できた author が失敗終端になった: {env}"
    assert [c["name"] for c in env.get("created_files") or []] == ["一覧.md"], env.get("created_files")
    assert env["headline"] == "# 消費税率"
    assert any(r["rel_path"] == "一覧.md" for r in store.list_workspace_files(uid))


def test_author_stop_during_filename_call_does_not_register_output(tmp_path, monkeypatch):
    """ファイル名を決める呼び出し（非ゼロ時間）の最中に利用者が停止したら、成果物を登録せず
    停止終端で終える（停止したターンの成果物を個人 workspace に残さない）。"""
    import threading
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    stop = threading.Event()

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    class _StopDuringFilename(_HybridAuthor):
        def _stream(self, prompt, completion=None):
            chunks = list(super()._stream(prompt, completion))
            if "ファイル名を決める担当" in prompt:
                stop.set()   # ファイル名を決める呼び出しの最中に利用者が停止する
            yield from chunks

    provider = _StopDuringFilename([('{"verdict": "sufficient", "missing": "", "findings": []}', "stop"),
                                    ("# 消費税率", "stop"),
                                    ('{"filename": "一覧.md", "marp": false}', "stop")])
    events = list(provider.run(_author_ctx_depth(uid, depth="standard", stop_event=stop)))
    env = next(e for e in events if e.get("type") == "_result")["env"]
    assert env.get("_terminal") == "stopped", f"停止終端になっていない: {env.get('_terminal')}"
    assert not env.get("created_files")
    assert not store.list_workspace_files(uid), "停止したターンで成果物が登録されている"


def test_author_filename_call_usage_is_metered_once(tmp_path, monkeypatch):
    """ファイル名を決める呼び出し1回分は清書 usage にも chat-sub にも乗らない別消費——
    orchestrator の消費として `chat-review` へ1行だけ記録する（未計上にしない）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    from sherpa import metering
    recorded: list = []
    monkeypatch.setattr(metering, "record",
                        lambda kind, *a, **kw: recorded.append((kind, kw.get("calls"))))

    provider = _HybridAuthor([("# 消費税率", "stop"),
                              ('{"filename": "一覧.md", "marp": false}', "stop")])
    list(provider.run(_author_ctx_depth(uid)))   # クイック（0 巡）＝evaluator 分の chat-review は無い
    review = [r for r in recorded if r[0] == "chat-review"]
    assert len(review) == 1, f"ファイル名決定の呼び出しが計上されていない: {recorded}"
    assert review[0][1] == 1, review


def test_author_save_failure_after_synthesis_timeout_keeps_exception_kind(tmp_path, monkeypatch):
    """清書が通信例外（timeout）で切れたターンで登録にも失敗したとき、終了理由の印は例外型
    （timeout）のまま残す——登録失敗の "error" で上書きすると打ち切りの内訳で通信障害が消える。"""
    _try_init()
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    seq = _worker_round({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"消費税"}'}}]}}]})
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    class _TimeoutSynthesis(_HybridAuthor):
        def _stream(self, prompt, completion=None):
            if "ファイル名を決める担当" in prompt:
                yield from super()._stream(prompt, completion)
                return
            yield "# 消費税率"
            raise TimeoutError("timed out")

    provider = _TimeoutSynthesis([('{"filename": "一覧.md"}', "stop")])
    # uid なし＝登録に失敗する（清書の例外型と登録失敗が重なるターン）。
    env = next(e for e in provider.run(_author_ctx_depth(None)) if e.get("type") == "_result")["env"]
    assert env.get("_terminal") == "failed", env
    assert env.get("agentic_failure") == "timeout", \
        f"清書の例外型が登録失敗で上書きされた: {env.get('agentic_failure')}"
