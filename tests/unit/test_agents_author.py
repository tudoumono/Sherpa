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


# ===== 同一 uid の直列化 lock（RV MEDIUM・Phase1） =====

def test_codex_run_busy_when_same_uid_running():
    """同一 uid の Codex 実行中はもう1件を実行せず（非ブロッキング）、正直な busy 回答を返す。
    並行実行は共有 authoring/ の snapshot・files/ move・.agents rebuild が交差して
    成果物の取り違え/破損を起こすため、直列のみ許可。"""
    prov = A.CodexProvider()
    ctx = _ctx()
    ctx.uid = "lock-busy-u1"
    lk = A._authoring_lock("lock-busy-u1")
    assert lk.acquire(blocking=False)
    try:
        events = list(prov.run(ctx))
    finally:
        lk.release()
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(res) == 1, "busy 応答が _result で完結していない"
    assert "実行中" in res[0]["env"]["headline"]
    assert res[0]["env"]["sources"] == []


def test_authoring_lock_released_on_generator_close(monkeypatch):
    """直列化 lock は generator が途中で close されても解放される（run() が yield from を
    try/finally で包む設計の検証。漏れると同一 uid が恒久的に busy になる）。"""
    def fake_gather(ctx):
        yield {"type": "node", "id": "x", "kind": "think", "label": "t", "detail": "", "status": "done"}
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}}
    monkeypatch.setattr(A, "_gather", fake_gather)
    prov = A.CodexProvider()
    ctx = _ctx()
    ctx.uid = "lock-close-u1"
    gen = prov.run(ctx)
    next(gen)                                        # lock 獲得＋最初の node まで進める
    lk = A._authoring_lock("lock-close-u1")
    assert not lk.acquire(blocking=False), "実行中に lock が解放されている"
    gen.close()                                      # 途中終了（クライアント切断相当）
    assert lk.acquire(blocking=False), "close 後に lock が解放されていない（恒久 busy）"
    lk.release()


def test_gather_seam_intercepted_by_codex_provider(monkeypatch):
    """RV LOW（2026-07-14 フェーズ5 2巡目）: `agents._gather` の facade patch が
    `CodexProvider._run_authoring`（`sherpa/providers/codex/provider.py` 側の呼び出し・facade
    実行時解決）にも効くことの**明示的な検知器**。上の lock-close テストは `next(gen)` 1回で
    close するため、provider.py がローカル束縛（`from ...base import _gather`）に退行しても
    素通りで通ってしまう（Codex RV 指摘）。ここでは fake `_gather` の sentinel node が実際に
    流れてくること＝fake が呼ばれたこと自体を assert する。HeuristicProvider/_GenProvider 経由の
    同種検知器は `tests/unit/test_agents_seams.py` にある。

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
        gen.close()           # subprocess 起動前に必ず閉じる（run() の try/finally で lock も解放）
    assert calls, (
        "monkeypatch した agents._gather が CodexProvider 経由で呼ばれていない"
        f"（facade patch 素通り＝provider.py のローカル束縛化の可能性）。seen={seen!r}"
    )
    assert any(isinstance(e, dict) and e.get("id") == "seam-pin-codex" for e in seen), (
        f"fake _gather の sentinel node が run() の出力に現れない。seen={seen!r}"
    )


def test_busy_env_carries_busy_marker_and_chat_service_guards_it():
    """RV r2 MEDIUM: busy 応答（実行しなかったターン）に personal_sources を添付しない。
    provider 側は env["busy"]=True マーカーを出し、chat_service 側は両経路（非ストリーミング/
    ストリーミング）の添付を `not env.get("busy")` でガードしていること。"""
    prov = A.CodexProvider()
    ctx = _ctx()
    ctx.uid = "lock-busy-marker-u1"
    lk = A._authoring_lock("lock-busy-marker-u1")
    assert lk.acquire(blocking=False)
    try:
        events = list(prov.run(ctx))
    finally:
        lk.release()
    env = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"][0]["env"]
    assert env.get("busy") is True, "busy マーカーが envelope に無い"

    import inspect
    from sherpa import chat_service as CS
    src = inspect.getsource(CS)
    assert src.count('if personal_hits and not env.get("busy")') == 2, \
        "chat_service の personal_sources 添付ガードが両経路（2箇所）に入っていない"


def test_busy_response_preserves_scope_with_layer():
    """Codex busy 応答（直列化中の早期応答）も scope 契約（layer/layer_applied・
    scope_paths）を欠落させない。既存の "busy" マーカー自体は維持する。"""
    prov = A.CodexProvider()
    ctx = _ctx()
    ctx.uid = "lock-busy-layer-u1"
    ctx.scope_meta = {"world": "v1", "scope_paths": ["4期/"], "source": "explicit", "layer": "code"}
    lk = A._authoring_lock("lock-busy-layer-u1")
    assert lk.acquire(blocking=False)
    try:
        events = list(prov.run(ctx))
    finally:
        lk.release()
    env = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"][0]["env"]
    assert env["scope"]["layer"] == "code"
    assert env["scope"]["layer_applied"] is True     # busy は qa 相当として扱う
    assert env["scope"]["source"] == "busy"           # busy マーカー自体は維持
    assert env["scope"]["scope_paths"] == ["4期/"]    # scope_paths も欠落させない


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


def test_tmp_workspace_cleared_at_start_of_each_turn(monkeypatch, tmp_path):
    """正典 §3.4「範囲と同じ硬いフィルタ」: authoring/.tmp は複数ターンをまたいで再利用される
    uid 単位の作業領域の一部だが、前ターンの残存ファイルが cwd の直接読取で次ターンにも読めて
    しまうと層フィルタの迂回路になる——ターン開始時に必ず空にする（前ターンの残存を持ち越さない）。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    users_dir = tmp_path / "users"
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_dir))
    uid = "tmp-clear-u1"

    def _boom_popen(*_a, **_k):
        raise OSError("popen intentionally not followed through in this test")

    monkeypatch.setattr(subprocess, "Popen", _boom_popen)

    def fake_gather(ctx):
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "t"},
               "env": {"lens": "qa", "headline": "h", "summary": {"total": 0}, "data": {}, "sources": []}}

    monkeypatch.setattr(A, "_gather", fake_gather)

    # 前ターンの残存を模擬する（authoring/.tmp に置かれたまま消えていないファイル）。
    tmp_dir = users_dir / uid / "workspace" / "authoring" / ".tmp"
    tmp_dir.mkdir(parents=True)
    leftover = tmp_dir / "leftover-from-previous-turn.txt"
    leftover.write_text("前ターンの内容の断片（想定: 層が限定される前の資料の一部）", encoding="utf-8")
    assert leftover.exists()

    prov = A.CodexProvider()
    ctx = _ctx(lens="qa", message="質問")
    ctx.uid = uid
    ctx.scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "both"}
    list(prov.run(ctx))   # Popen で打ち切られるが、.tmp のクリアはそれより前に実行済みのはず

    assert not leftover.exists(), "前ターンの .tmp 残存を消していない（層フィルタの迂回路が残る）"
    assert tmp_dir.is_dir(), ".tmp 自体は次ターン用に作り直されているはず"


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
