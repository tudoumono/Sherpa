"""`Provider._sub`/`_GenProvider._agentic_run`（sub ループ→根拠ゲート→クラウド単発合成／各種縮退
→フォールバック）、ツール制限の二重強制、chat-sub metering の実行基盤テスト。

検索アシスタント（`sherpa/search_helper.py`）が現在使う実行エンジン（`_sub_loop`/`_sub_agentic_loop`）
の回帰検出を担う。`p._sub` は本ファイルが直接組み立てる辞書（`search_helper.resolve()` と同じ形＝
`provider`/`url`/`model`/`tools`/`guard`/`profile_id`）で与え、`get_provider()` 配線自体（設定1項目→
`_sub` 解決）は `tests/unit/test_search_helper.py` が別途固定する＝ここは実行エンジン単体の契約のみ
を対象にする。

LLM は stub（`agentic_search._post`/`_stream` を差し替え・コスト0）。`Ctx` は
`tests/unit/test_agentic_search.py:254-276` と全く同じ方法で構築する。
"""
from __future__ import annotations

import os
import threading

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

import sherpa.agentic_search as A  # noqa: E402
from sherpa import agents  # noqa: E402
from sherpa.agents import Ctx, OpenAIProvider  # noqa: E402


def _ctx(**overrides) -> Ctx:
    base = dict(
        message="TAX-RATEは?", world="v1", knowledge=True,
        route=lambda m: {"lens": "qa", "input": m, "reason": "test"},
        dispatch=lambda lens, inp: {"summary": {"total": 0}, "data": {}, "sources": []},
        scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
        make_sources=lambda docs: [{"doc_id": d} for d in docs],
    )
    base.update(overrides)
    return Ctx(**base)


# agentic_search の実ツール名（既知集合・固定・sherpa/agentic_search.py の TOOLS 相当）。
_ALL_TOOLS = frozenset({"list_docs", "ripgrep_search", "glob_search", "doc_outline", "read_doc",
                        "read_around", "es_search", "graph_neighbors", "ask_user"})

_SUB = {"provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5",
        "tools": frozenset(_ALL_TOOLS), "guard": {"min_citations": 1, "max_turns": 6, "llm_timeout": 60},
        "profile_id": "worker"}


class _FakeSynth(OpenAIProvider):
    """`_stream` を差し替えて合成呼び出しを記録するテスト用サブクラス
    （`test_agentic_search.py:325-395` の `_FakeStreamProvider` と同型）。"""
    _synth_text = "CLOUD SYNTH ANSWER"
    _synth_usage = None
    _synth_prompts: list

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._synth_prompts = []

    def _stream(self, prompt, completion=None):
        self._synth_prompts.append(prompt)
        if self._synth_usage:
            self._last_usage = self._synth_usage
        if completion is not None:
            # 既定は自然完了（_CompletionState）——個別テストが打ち切り/未完了を検証
            # したい場合は `_stream` をさらにオーバーライドして `completion` を明示的に操作する。
            completion.terminal_seen = True
            completion.reason = "stop"
        for ch in ([self._synth_text] if self._synth_text else []):
            yield ch


@pytest.fixture(autouse=True)
def _hermetic_es_graph(monkeypatch):
    """`_sub_agentic_loop` のツール定義配列を決定的にする（実 ES/Neo4j 到達可否に依存しない）。"""
    monkeypatch.setattr(A, "es_index", A.es_index)
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)


@pytest.fixture(autouse=True)
def _no_real_retry_sleep(monkeypatch):
    """`_send` の限定リトライのバックオフで実待ちしない（本ファイルの再試行系テストを高速化）。"""
    monkeypatch.setattr(A.time, "sleep", lambda sec: None)


def _install_post(seq):
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    return orig


def _restore_post(orig):
    A._post = orig


# ===== OFF: byte-identical 回帰 =====

def test_off_agentic_stream_unchanged():
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        assert p._sub is None
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        result = next(e for e in events if e.get("type") == "_result")
        assert deltas == ["LOCAL"]
        assert result["env"]["headline"] == "LOCAL"
        assert "usage_sub" not in result["env"]
        assert p._synth_prompts == []   # _stream（合成）は一度も呼ばれない
    finally:
        _restore_post(orig)


# ===== ON: 成功パス =====

def test_on_loop_hits_sub_endpoint_and_single_synthesis():
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append((url, dict(body), timeout))
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}
        return {"choices": [{"message": {"content": "LOCAL PROSE (must be discarded)"}}],
                "prompt_eval_count": 10, "eval_count": 5}

    orig = A._post
    A._post = fake_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        # サブの tool 呼び出し1回 + no-tool 応答1回。合成自体（_FakeSynth._stream）は _post を
        # 経由しない（`self._stream` を直接差し替えているため）ので calls には乗らない——帰属は
        # `OpenAIProvider._attribute`（`llm.openai_url` を叩く openai_style 版）を使うが、
        # `p` は `_FakeSynth`（`_attribute` は未上書き）なので実際には `A._post` 経由で発火する。
        assert len(calls) == 3
        assert all(u == "http://localhost:11434/api/chat" for u, _, _ in calls[:2])
        assert all(b["model"] == "qwen2.5" for _, b, _ in calls[:2])
        assert all(b.get("stream") is False for _, b, _ in calls[:2])   # ollama=True 形状
        # guard.llm_timeout 注入。`_send` は timeout を全体 deadline として扱う（`_post` 発行の
        # たびに残り時間を計算し直す）ため、経過時間の分だけ 60 よりわずかに小さくなり得る。
        assert all(59 < t <= 60 for _, _, t in calls[:2])
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert deltas == ["CLOUD SYNTH ANSWER"]
        assert len(p._synth_prompts) == 1
        assert "TAX-RATE" in p._synth_prompts[0]   # citation 引用を含む合成プロンプト
        assert "LOCAL PROSE" not in "".join(deltas)   # ローカル散文は絶対に出ない
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert result["env"]["usage_sub"] == {
            "provider": "ollama", "model": "qwen2.5", "input_tokens": 10,
            "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0,
            "is_local": "local", "profile": "worker"}   # どのプロファイルの消費かを usage_sub に残す
        # answer.usage は主合成呼び出しの単一オブジェクト契約（_stream が self._last_usage をセットしなければ無し）
        assert "usage" not in result["env"]
    finally:
        A._post = orig


def test_usage_sub_profile_prefers_display_name_over_internal_slug():
    """`usage_sub.profile` は render.js::usageSubMetaHTML がそのまま画面に出す表示名
    （専門用語ゼロ）——`name`（`search_helper.resolve()` が
    実際に持つ表示名。例「下調べ役」）があれば内部 slug（profile_id・例
    "search-helper-openai"）より必ず優先する。`name` が無いプロファイル（`_SUB` 等）は
    従来どおり profile_id へフォールバックする（直前のテストが固定済み）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL PROSE (must be discarded)"}}],
         "prompt_eval_count": 10, "eval_count": 5},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "profile_id": "search-helper-openai", "name": "下調べ役"}
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["usage_sub"]["profile"] == "下調べ役"
        assert "search-helper-openai" not in result["env"]["usage_sub"]["profile"]
    finally:
        _restore_post(orig)


def test_on_loop_synthesis_usage_present_when_stream_sets_last_usage():
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}], "prompt_eval_count": 1, "eval_count": 1},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        p._synth_usage = {"provider": "openai", "model": "gpt-5.5", "input_tokens": 3,
                          "cached_input_tokens": 0, "output_tokens": 4, "reasoning_output_tokens": 0}
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["usage"] == p._synth_usage
        assert result["env"]["usage_sub"]["provider"] == "ollama"
    finally:
        _restore_post(orig)


# ===== 根拠ゲート =====

def test_on_gate_zero_citations_returns_honest_failure_without_main_ai_retry(monkeypatch):
    """下調べ役ありで根拠ゲートが「evidence below threshold」を送出したとき、ゲート自体が
    「根拠不足」という正当なシグナルであっても、下調べ役ありのターンをメインAI（高コスト）の
    単発 grep 経由の全量やり直しへ黙って切り替えない＝honest failure にする（`fake_gather` は
    一度も呼ばれない＝メインAIへの黙った切替は起きない）。

    `ripgrep_search`（ヒット無し）を使う——`list_docs`/`graph_neighbors` は EXT-2 の
    `has_structural_evidence` 信号（citation を伴わない正当な根拠）を立てるため、根拠ゼロの
    ゲート失敗を再現するにはそれ以外のツールでヒット無しにする必要がある。
    """
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"NO-SUCH-HIT-XYZ"}'}}]}}]},
        {"choices": [{"message": {"content": "summary with no citations"}}]},
    ]
    orig = _install_post(seq)
    gather_called = []

    def fake_gather(ctx):
        gather_called.append(True)
        yield {"type": "node", "id": "fb", "kind": "tool", "label": "fallback", "detail": "", "status": "done"}
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "fallback"},
               "env": {"lens": "qa", "headline": "決定的フォールバック回答", "summary": {"total": 0},
                      "data": {}, "sources": [], "scope": {"world": "v1", "scope_paths": [], "source": "all"}}}

    monkeypatch.setattr(agents, "_gather", fake_gather)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p.run(ctx))
        assert gather_called == []   # メインAIへの黙った切替（単発 grep フォールスルー）は起きない
        result = next(e for e in events if e.get("type") == "_result")
        assert "下調べ" in result["env"]["headline"]
        assert "usage_sub" not in result["env"]
    finally:
        _restore_post(orig)


def test_gate_claimless_graph_card_passes_via_graph_node_evidence(monkeypatch):
    """裏付け doc を1件も主張しない graph_neighbors card 単独（citation 0件）でも、Neo4j から
    実際に返ったノードであること自体を `source_type=graph` の構造 Evidence として計上し根拠
    ゲートを通す——根拠ゲートは `has_structural_evidence` のみを参照する契約であり、`cards` の
    存在自体はゲート例外にしない（サブループ経路）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "グラフから確認しました。"}}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "graph_neighbors":
            # cid（実際の agentic 経路と同じく lens_service.neighbor_cards が付与する内部専用の
            # Neo4j canonical_id）が無ければ既定 ON の機械検証で昇格しない。
            return ({"nodes": []}, set(), [],
                   [{"name": "TAXCALC", "label": "Module", "category": "プログラム",
                     "evidence": {"edges": [], "grep": []},
                     "cid": "module:v1:04_運用/taxcalc.cob#TAXCALC"}])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "troubleshoot", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"   # ゲートを通り合成まで到達
        packet = result["env"]["data"]["evidence_packet"]
        assert len(packet["evidence"]) == 1
        assert packet["evidence"][0]["source_type"] == "graph"
        assert packet["evidence"][0]["source_path"] is None
        assert packet["evidence"][0]["matched_doc_ids"] == ["module:v1:04_運用/taxcalc.cob#TAXCALC"]
        assert packet["evidence"][0]["verification_method"] == "graph_node_verified"
        assert result["env"]["data"]["candidates"][0]["name"] == "TAXCALC"   # card 自体は残る
    finally:
        _restore_post(orig)


def test_gate_claimless_graph_card_without_cid_does_not_pass(monkeypatch):
    """機械検証（常時実施）では、裏付け doc も cid も無い card を非一意な `label:name` で昇格させず
    根拠ゲートを通さない（サブループ経路）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "グラフから確認しました。"}}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool_no_cid(name, args, world, scope_paths, **kw):
        if name == "graph_neighbors":
            return ({"nodes": []}, set(), [],
                   [{"name": "TAXCALC", "label": "Module", "category": "プログラム",
                     "evidence": {"edges": [], "grep": []}}])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool_no_cid)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        with pytest.raises(RuntimeError, match="evidence below threshold"):
            list(p._agentic_run(ctx, {"lens": "troubleshoot", "input": ctx.message, "reason": "test"}))
    finally:
        _restore_post(orig)


def test_hybrid_synthesis_attribution_digest_surfaces_structural_only_evidence(monkeypatch):
    """list_docs-only（citation 0件）の hybrid 応答は `_facts()` の文面に doc_id が一切現れない
    （citation が無いため）——外側クラウド合成プロンプト自体には Evidence digest も doc_id も一切
    乗らない（本文中に制御タグを書かせない設計簡素化）。帰属は回答完了後の別の非ストリーム呼び出し
    （`self._attribute`）に渡す Evidence digest（`ev-N: 事実`・list_docs の集計 Evidence を含む）
    で判定し、申告された ev-N は `matched_doc_ids` 経由で doc_id へ逆引きされて sources_verified／
    Packet の `used` に正しく反映される。digest 本文は**ツール結果と同じ露出**（設計簡素化・
    2026-08-24）——生 doc_id がそのまま出る。"""
    doc = "4期/04_運用/障害記録.md"

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "list_docs":
            return ({"count": 1, "docs": [{"rel_path": doc, "doctype": "source"}]}, {doc}, [], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
    ]
    orig = _install_post(seq)

    class _EvAwareSynth(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "stop"
            yield "資料が1件あります。"

        def _attribute(self, text, digest, ev_map, call_budget=None):
            self.attribution_text = text
            self.attribution_digest = digest
            ev_id = next(k for k, v in ev_map.items() if doc in v)
            return {ev_id}

    try:
        p = _EvAwareSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert p._synth_prompts, "合成プロンプトが一度も記録されなかった"
        prompt = p._synth_prompts[-1]
        assert "該当なし" in prompt   # citation 0件なので _facts() 自体には doc_id が出ない
        assert "Evidence digest" not in prompt and doc not in prompt   # 合成プロンプトには digest を渡さない
        assert p.attribution_text == "資料が1件あります。"   # 確定した回答本文を渡す
        assert "該当 1 件" in p.attribution_digest and "列挙 1 件" in p.attribution_digest
        assert doc in p.attribution_digest   # digest はツール結果と同じ露出＝生 doc_id がそのまま出る
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "資料が1件あります。"
        assert result["env"]["sources_verified"] == [doc]
        packet = result["env"]["data"]["evidence_packet"]
        assert packet["evidence"][0]["matched_doc_ids"] == [doc]
        assert packet["evidence"][0]["used"] is True
    finally:
        _restore_post(orig)


def test_attribution_call_drives_sources_verified_not_local_draft(monkeypatch):
    """sources_verified/Packet の根拠判定は**表示する最終回答**（外側クラウド合成）の帰属呼び出し
    （`self._attribute`）の結果を使う契約——サブループのローカル草稿（破棄される散文）が何を申告
    していようと一切関係ない（帰属は表示回答の完了後にサーバー側で発行する別呼び出しであり、
    ローカル草稿の内容を参照すらしない）。

    フェイク `_attribute` は実際の外部 LLM の帰属呼び出しを模して、digest／text（帰属専用コピー・
    どちらも生の doc_id/basename のまま＝設計簡素化・2026-08-24）から、回答本文に basename が
    現れる doc_id だけを ev_map 経由で拾う。`_stream` が返す回答本文には doc_b の実ファイル名
    （basename）だけが現れ doc_a には触れないため、doc_b だけが帰属される。"""
    doc_a = "4期/04_運用/請求書.md"
    doc_b = "4期/04_運用/手数料改定障害記録.md"

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc_a, doc_b},
                   [{"doc_id": doc_a, "span": [1, 1], "quote": "x", "ext": ".md"},
                    {"doc_id": doc_b, "span": [1, 1], "quote": "y", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}],
         "prompt_eval_count": 10, "eval_count": 5},
    ]
    orig = _install_post(seq)

    class _EvAwareSynth(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "stop"
            yield "手数料改定障害記録.mdをもとに回答します。"

        def _attribute(self, text, digest, ev_map, call_budget=None):
            self.attribution_text = text
            used = set()
            for ev_id, docs in ev_map.items():
                if any(os.path.basename(d) in text for d in docs):
                    used.add(ev_id)
            return used

    try:
        p = _EvAwareSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert "used_evidence" not in "".join(deltas).lower()
        assert p.attribution_text == "手数料改定障害記録.mdをもとに回答します。"   # 帰属コピーは本文そのもの（redact のみ）
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "手数料改定障害記録.mdをもとに回答します。"   # 表示本文は不変
        assert result["env"]["sources_verified"] == [doc_b]        # 帰属した doc_b だけが根拠
        assert doc_a not in result["env"]["sources_verified"]      # 破棄した草稿の内容は無関係
    finally:
        _restore_post(orig)


def test_cloud_synthesis_default_no_attribution_still_flushes_full_text(monkeypatch):
    """`_attribute` を明示オーバーライドしない既定実装（`_GenProvider._attribute` は常に空集合）
    のときも、クラウド合成の本文は全文そのまま headline に flush される（保留なし・byte
    同等）——申告が無いので sources_verified は read_around の doc のみへ縮退する
    （本テストは read_around も無いため空）。"""
    doc_a = "4期/04_運用/障害記録.md"

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc_a},
                   [{"doc_id": doc_a, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}],
         "prompt_eval_count": 10, "eval_count": 5},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        p._synth_text = "CLOUD SYNTH ANSWER without attribution."
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER without attribution."   # 全文 flush
        assert result["env"]["sources_verified"] == []
    finally:
        _restore_post(orig)


def test_attribute_safe_swallows_exception_and_logs(caplog):
    """`_GenProvider._attribute_safe` は `_attribute`（`OpenAIProvider._attribute`/
    `OllamaProvider._attribute` が接続先ヘルパ `llm.openai_url`/`llm.openai_headers`/
    `llm.ollama_url` を呼び出し引数として評価する際に送出しうる `RuntimeError`/`SsrfBlocked` 等を
    含む）が送出する例外を、呼び出し元（plan/hybrid の合成）の「delta 送信後は再 raise しない」
    契約に合わせて空集合へ縮退する。"""
    import logging

    class _RaisingAttribute(OpenAIProvider):
        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise RuntimeError("OpenAI I/O is blocked")

    p = _RaisingAttribute("sk-dummy", "gpt-5.5")
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = p._attribute_safe("answer text", "digest text", {"e1": {}})
    assert out == set()
    assert any("attribution" in r.getMessage() for r in caplog.records)


def test_hybrid_synthesis_attribute_exception_keeps_body_and_degrades_to_empty_attribution(
        monkeypatch, caplog):
    """ハイブリッド合成（`_agentic_run` 末尾）は、既に回答本文の delta を送信済みのため、
    `self._attribute`（`_attribute_safe` 経由）が例外を送出しても再 raise せず、帰属だけを
    空集合へ縮退して `_result` を返す（回答本文はそのまま維持する）。plan 経路
    （`_agentic_run_plan`）も同じ `_attribute_safe` を使うため同型で保護される。"""
    import logging

    doc_a = "4期/04_運用/障害記録.md"

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc_a},
                   [{"doc_id": doc_a, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
    ]
    orig = _install_post(seq)

    class _RaisingAttribute(_FakeSynth):
        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise RuntimeError("OpenAI I/O is blocked")

    try:
        p = _RaisingAttribute("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        p._synth_text = "CLOUD SYNTH ANSWER."
        ctx = _ctx()
        with caplog.at_level(logging.WARNING, logger="sherpa"):
            events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER."   # 本文は維持される（再raiseしない）
        assert result["env"]["sources_verified"] == []              # 帰属は空集合へ縮退
        assert any("attribution" in r.getMessage() for r in caplog.records)
    finally:
        _restore_post(orig)


# ===== 各種ハードエラー縮退 =====

def test_hybrid_synthesis_stop_event_headline_is_partial_stream_so_far():
    """停止契約（拡張設計 §4.4・設計簡素化）: ハイブリッド合成も「停止＝その時点までに配信した
    本文」がそのまま headline になる（保留・確定処理は無い単純な契約）。
    停止時は帰属呼び出しを行わない（部分本文を「確定した回答」として扱わない）。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    stop_event = threading.Event()

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    class _StopMidChunk(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            stop_event.set()
            yield "回答本文"

        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise AssertionError("停止時は帰属呼び出しを行わないはず")

    import sherpa.agentic_search as A
    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _StopMidChunk("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx(stop_event=stop_event)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert deltas == ["回答本文"]
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "回答本文"
        assert result["env"]["headline"] == "".join(deltas)   # headline と配信本文が一致する
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_stop_event_set_immediately_after_stream_completes_skips_attribution():
    """ストリームが正常に完結した直後（`stopped` フラグは False のまま）に停止要求が来た
    競合を、帰属**直前**の再確認で捕捉する——ループ内の `stop_event` チェックは最後のチャンクの
    次に `next()` を呼んだときに StopIteration で抜けるため、そのチェックには一度も引っかからず
    `stopped` フラグは False のまま帰属直前まで進んでしまう（`stopped` フラグだけを見るチェックでは
    捕まえられない窓のため、帰属直前に stop_event 自体を再確認する）。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    stop_event = threading.Event()

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    class _StopAfterStream(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            yield "回答本文"
            stop_event.set()   # ストリーム完結の直後（`stopped` フラグは立たない）に停止要求が来た、を模す

        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise AssertionError("ストリーム完結直後の停止では帰属呼び出しを行わないはず")

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _StopAfterStream("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx(stop_event=stop_event)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert deltas == ["回答本文"]
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "回答本文"
        assert result["env"]["sources_verified"] == []   # 帰属を行っていないので根拠は付かない
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_skips_attribution_when_stream_completion_reason_is_truncated():
    """`_stream` が正常終了（`stopped`/`failed` は共に False）しても、実装先の Provider が
    `completion`（`_CompletionState`）に終端は観測済み・理由は打ち切り系の値（"length"）を記録
    すれば、本文は headline として採用しつつ帰属呼び出しは省略する（部分本文を確定回答として
    帰属しない）——`openai.py`/`ollama.py`/`gemini.py`/`bedrock.py` の各 `_stream` が方言別の
    完了通知から `completion.terminal_seen`/`completion.reason` を拾う契約を、ここでは合成
    ストリームの中で直接模して固定する。サブループ（下調べ役）自身の投稿は自然完了
    （"no_tool_calls"）でも、実際に画面へ出す本文を生成したのは打ち切られたクラウド最終合成の
    ため、Evidence Packet の `stop_reason` は "truncated" へ再分類される（サブループの投稿を
    そのまま握り続けない）。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    class _TruncatedStream(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            yield "途中で切れた回答"
            if completion is not None:   # OpenAI/Ollama 互換の打ち切り理由を模す
                completion.terminal_seen = True
                completion.reason = "length"

        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise AssertionError("打ち切り完了では帰属呼び出しを行わないはず")

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _TruncatedStream("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "途中で切れた回答"
        assert result["env"]["sources_verified"] == []   # 帰属を行っていないので根拠は付かない
        assert result["env"]["data"]["evidence_packet"]["stop_reason"] == "truncated"
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_keeps_sub_stop_reason_when_final_completion_is_natural():
    """最終合成（クラウド）が自然完了（`completion.reason` が allowlist 内・既定 "stop"）なら、
    Evidence Packet の `stop_reason` はサブループが確定した値（"no_tool_calls"）のまま——
    再分類は「打ち切りだと判別できたときだけ」で、自然完了時は上書きしない（回帰防止）。
    サブループ自身の最終応答にも明示的に `finish_reason: "stop"` を付け、サブループ側の
    stop_reason 自体が本当に自然完了由来の "no_tool_calls" であることを保証する（省略すると
    非自然完了の理由欠落経路を通り "unknown" になってしまい、本テストの前提が崩れる）。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}, "finish_reason": "stop"}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["data"]["evidence_packet"]["stop_reason"] == "no_tool_calls"
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_skips_attribution_when_stream_ends_without_terminal_frame():
    """本文チャンクの後、`_stream` が終端フレームを一度も観測しないまま（`completion` を
    一切操作しないまま）ジェネレータが終わった場合（上流/プロキシが接続を打ち切った等）、
    `completion.terminal_seen` は既定の False のまま——「完了」を許可リスト的に
    `terminal_seen is True` でのみ判定するため、理由が None（打ち切り理由の集合のどれにも
    一致しない）でも「未完了」として帰属を省略する。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    class _SilentEofStream(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            yield "本文チャンクの後、前触れなく EOF"
            # `completion` に一切触れない＝終端フレームを観測できなかった実装を模す。

        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise AssertionError("終端フレーム未観測では帰属呼び出しを行わないはず")

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _SilentEofStream("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "本文チャンクの後、前触れなく EOF"
        assert result["env"]["sources_verified"] == []
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_skips_attribution_when_completion_reason_is_unknown():
    """終端フレームは観測できた（`terminal_seen=True`）が、理由が自然完了 allowlist
    （"stop"/"STOP"/"end_turn"/"stop_sequence"）に無い未知の値だった場合も未完了として帰属を
    省略する——「打ち切り理由の denylist」ではなく「自然完了理由の allowlist」なので、想定外の
    新しい理由文字列（例: 将来 API が追加する値）もデフォルトで安全側に倒れる。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    class _UnknownReasonStream(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            yield "壊れた終端理由"
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "content_filter"   # allowlist に無い未知の理由

        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise AssertionError("未知の完了理由では帰属呼び出しを行わないはず")

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _UnknownReasonStream("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "壊れた終端理由"
        assert result["env"]["sources_verified"] == []
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_skips_attribution_when_reason_is_valid_for_a_different_dialect():
    """完了理由が自然完了 allowlist の**和集合には含まれる**が、この Provider（`_FakeSynth` は
    `OpenAIProvider` を継承＝allowlist は `{"stop"}`）の方言としては不正な値（"end_turn"＝
    Anthropic/Bedrock 用）だった場合、4方言の和集合ではなく Provider 固有の allowlist で判定する
    ため帰属を省略する（hybrid 経路・和集合判定に戻すと他方言の正当値で誤ってゲートが開く）。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    class _WrongDialectReasonStream(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            yield "他方言なら自然完了の理由"
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "end_turn"   # Anthropic/Bedrock の自然完了理由（OpenAI方言としては不正）

        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise AssertionError("別方言の正当値では帰属呼び出しを行わないはず")

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _WrongDialectReasonStream("sk-dummy", "gpt-5.5")
        assert p._natural_completion_reasons == frozenset({"stop"})   # OpenAIProvider の allowlist
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "他方言なら自然完了の理由"
        assert result["env"]["sources_verified"] == []
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_evidence_packet_stays_1to1_with_digest_when_truncated(monkeypatch):
    """61件超の Evidence（digest の行数上限 `_ATTRIBUTION_MAX_ITEMS`=60 を超える）でも、
    Evidence Packet の `evidence[]` は digest が実際に採用した ev-N（`adopted_ev_ids`）とだけ揃える
    ——digest に無い ev-N を Packet に残さない（「Packet の各エントリが digest の1行へ対応する」
    監査契約を打ち切り時も保つ）。省いた分は `remaining_gaps` に件数注記を足す。`evidence_committed`
    サイドカー（`env["_evidence_committed"]["evidence_ids"]`）も同じ `adopted_ev_ids` で絞り込まれ、
    Packet と自動的に一致する契約を打ち切り時も保つ。`evidence_selected`（組み立て時点は絞り込み前の
    全件数）も、絞り込み後に Packet へ実際に載った件数へ更新される（先に組んだ全件数のまま
    食い違わない）。"""
    # 合成の doc_id は架空のため `verify_citation` を直接差し替えて機械検証を素通りさせる
    # （常時実施＝TOGGLE-RM で明示 OFF の退避口を撤去済み）。
    monkeypatch.setattr(A, "verify_citation",
                        lambda citation, world, _content_cache=None: {"exists": True, "method": "grep"})
    n = 61
    docs_all = {f"4期/{i}.md" for i in range(n)}
    cites_all = [{"doc_id": f"4期/{i}.md", "span": [1, 1], "quote": f"q{i}", "ext": ".md"}
                for i in range(n)]

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, docs_all, cites_all, [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
    ]
    orig = _install_post(seq)

    class _EvMapCapturingSynth(_FakeSynth):
        def _attribute(self, text, digest, ev_map, call_budget=None):
            self.captured_ev_map = ev_map
            return set()   # 帰属の中身自体はこのテストの対象ではない

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _EvMapCapturingSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        packet = result["env"]["data"]["evidence_packet"]
        adopted = set(p.captured_ev_map.keys())
        assert 0 < len(adopted) < n   # 実際に打ち切りが起きている（全件は採用されない）
        packet_ev_ids = {e["evidence_id"] for e in packet["evidence"]}
        assert packet_ev_ids == adopted   # Packet は digest の採用集合とちょうど一致する
        omitted = n - len(adopted)
        assert any(f"帰属対象外 {omitted} 件" in g for g in packet["remaining_gaps"])
        sidecar = result["env"]["_evidence_committed"]
        assert set(sidecar["evidence_ids"]) == adopted   # サイドカーも Packet と同じ採用集合
        assert set(sidecar["evidence_ids"]) == packet_ev_ids
        assert packet["evidence_selected"] == len(packet["evidence"])   # 絞り込み後の実件数へ更新
        assert packet["evidence_selected"] == len(adopted)
        assert packet["evidence_selected"] != n   # 打ち切り前の全件数のままではない
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_hybrid_synthesis_exception_mid_stream_keeps_partial_body_no_attribution():
    """合成ストリームの途中で例外が起きても、既に配信済みの部分本文は破棄せず採用する
    （plan/hybrid 従来からの契約）。ただし部分本文は「確定した回答」ではないため帰属呼び出しは
    行わない（read_around のみへ縮退）。"""
    doc = "4期/04_運用/障害記録.md"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, {doc}, [{"doc_id": doc, "span": [1, 1], "quote": "x", "ext": ".md"}], [])
        return ({"error": f"unexpected tool {name}"}, set(), [], [])

    class _MidFail(_FakeSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            yield "回答"
            raise RuntimeError("boom mid-stream")

        def _attribute(self, text, digest, ev_map, call_budget=None):
            raise AssertionError("例外で本文が確定しなかった場合は帰属呼び出しを行わないはず")

    import sherpa.agentic_search as A
    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    try:
        p = _MidFail("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert deltas == ["回答"]
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "回答"
        assert result["env"]["sources_verified"] == []   # 帰属なし＝read_around のみへ縮退（今回は0）
    finally:
        A.run_tool = orig_run_tool
        _restore_post(orig)


def test_on_sub_endpoint_error_returns_honest_failure_without_main_ai_retry(monkeypatch):
    """下調べ役（`self._sub`）付きのターンが失敗したら、外してメインAI（高コスト）で黙って
    再実行しない（`_gather`＝単発 grep へのフォールスルーも含めて起きない）＝honest failure
    （fail-closed）で停止する。`fake_gather` が一度も呼ばれないことも併せて固定する。"""
    import urllib.error

    def raising_post(url, headers, body, timeout=90):
        raise urllib.error.URLError("connection refused")

    orig = A._post
    A._post = raising_post
    gather_called = []

    def fake_gather(ctx):
        gather_called.append(True)
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "fallback"},
               "env": {"lens": "qa", "headline": "決定的フォールバック回答", "summary": {"total": 0},
                      "data": {}, "sources": [], "scope": {"world": "v1", "scope_paths": [], "source": "all"}}}

    monkeypatch.setattr(agents, "_gather", fake_gather)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p.run(ctx))   # run() から例外が漏れないことを確認
        result = next(e for e in events if e.get("type") == "_result")
        # メインAIへの黙った切替（単発 grep フォールスルー）は起きない。
        assert gather_called == []
        # 原因（下調べAIの失敗）を特定できる、利用者向けの専門用語ゼロなメッセージで停止する。
        assert "下調べ" in result["env"]["headline"]
        assert "OFF" in result["env"]["headline"] or "設定" in result["env"]["headline"]
        # scope を含める（欠落させると会話再表示時に UI が「全体」と解釈し、再試行で検索範囲が
        # World 全体へ広がる）。qa レンズは層フィルタが実効するため layer_applied=True が足される。
        assert result["env"]["scope"] == {**ctx.scope_meta, "layer_applied": True}
    finally:
        A._post = orig


def test_sub_failure_honest_response_preserves_narrow_scope(monkeypatch):
    """`ctx.scope_meta` がフォルダ絞り込み（世界全体でない scope_paths）を持つ場合、honest
    failure 応答の `env["scope"]` はそれをそのまま引き継ぐ（既定の「全体」へ丸めない）。"""
    import urllib.error

    def raising_post(url, headers, body, timeout=90):
        raise urllib.error.URLError("connection refused")

    orig = A._post
    A._post = raising_post
    narrow_scope = {"world": "v1", "scope_paths": ["4期/04_運用/"], "source": "scope"}
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx(scope_meta=narrow_scope)
        result = next(e for e in p.run(ctx) if e.get("type") == "_result")
        # qa レンズは層フィルタが実効するため layer_applied=True が足される（scope 自体は不変）。
        assert result["env"]["scope"] == {**narrow_scope, "layer_applied": True}
    finally:
        A._post = orig


def test_on_sub_url_not_allowlisted_returns_honest_failure_without_main_ai_retry(monkeypatch):
    """OLLAMA_URL/SHERPA_VLM_OLLAMA_URL を明示的に delenv して env 由来 allowlist 加算に
    非hermeticにならないようにする（unit conftest はこの2変数をクリアしない）。

    SSRF ガードで下調べ役の宛先が拒否された場合も、単発 grep 経由のメインAI再実行
    （api.openai.com への発行）へ黙って倒さず honest failure で停止する（下記コメント参照）。

    sub-loop の ollama 分岐は openai 分岐と対称に `self._system_settings`（コンストラクタの
    fresh snapshot）を使う契約——DB を読みに行ったら検知できるよう `store.get_system_settings`
    を fail probe にし、空 allowlist はコンストラクタへ明示注入する。
    """
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("SHERPA_VLM_OLLAMA_URL", raising=False)

    def _fail_if_called():
        raise AssertionError("sub-loop の ollama_url が system_settings 省略で DB を読んだ")
    monkeypatch.setattr("sherpa.store.get_system_settings", _fail_if_called)

    calls = []
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: calls.append(url)
    gather_called = []

    def fake_gather(ctx):
        gather_called.append(True)
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "fallback"},
               "env": {"lens": "qa", "headline": "決定的フォールバック回答", "summary": {"total": 0},
                      "data": {}, "sources": [], "scope": {"world": "v1", "scope_paths": [], "source": "all"}}}

    monkeypatch.setattr(agents, "_gather", fake_gather)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5", system_settings={})   # ollama_allowlist 未設定=空
        p._sub = {**_SUB, "url": "http://intra.example:11434"}
        ctx = _ctx()
        events = list(p.run(ctx))
        result = next(e for e in events if e.get("type") == "_result")
        # 許可されていない宛先へは1度も発行しない（SSRF ガードの本旨）。
        assert not any("intra.example" in u for u in calls), calls
        # 下調べ役が使えない時にメインAI（api.openai.com）へ黙って切り替えて再実行しない
        # （単発 grep へのフォールスルーも起きない）。
        assert calls == [], calls
        assert gather_called == []
        assert "下調べ" in result["env"]["headline"]
    finally:
        A._post = orig


def test_on_synthesis_zero_output_returns_honest_failure_without_main_ai_retry(monkeypatch):
    """合成が0出力（`hybrid synthesis produced no answer`）で失敗したときも、下調べ役ありの
    ターンをメインAIの高コスト経路へ黙って切り替えず honest failure にする
    （`fake_gather` は呼ばれない）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    gather_called = []

    def fake_gather(ctx):
        gather_called.append(True)
        yield {"type": "_env", "decision": {"lens": "qa", "input": ctx.message, "reason": "fallback"},
               "env": {"lens": "qa", "headline": "決定的フォールバック回答", "summary": {"total": 0},
                      "data": {}, "sources": [], "scope": {"world": "v1", "scope_paths": [], "source": "all"}}}

    monkeypatch.setattr(agents, "_gather", fake_gather)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        p._synth_text = None   # _stream が何もyieldしない
        ctx = _ctx()
        events = list(p.run(ctx))
        assert gather_called == []   # メインAIへの黙った切替は起きない
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert deltas == ["下調べAIでの調査がうまくいきませんでした。設定を確認するか、下調べ機能をOFFにしてください。"]
        result = next(e for e in events if e.get("type") == "_result")
        assert "下調べ" in result["env"]["headline"]
    finally:
        _restore_post(orig)


def test_on_synthesis_midstream_failure_keeps_partial():
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        class _MidFail(_FakeSynth):
            def _stream(self, prompt, completion=None):
                self._synth_prompts.append(prompt)
                yield "part"
                raise RuntimeError("boom mid-stream")

        p = _MidFail("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert deltas == ["part"]
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "part"
        assert not any(e.get("id") == "fallback" for e in events if e.get("type") == "node")
    finally:
        _restore_post(orig)


# ===== ツール制限の二重強制 =====

def test_toolset_filtered_to_profile_permission():
    """(a): ループへ渡すツール定義配列がプロファイルの tools で絞られる。"""
    seq = [{"choices": [{"message": {"content": "no tools available so I answer directly"}}]}]
    orig = _install_post(seq)
    captured = []
    real_post = A._post

    def spy_post(url, headers, body, timeout=90):
        captured.append(body["tools"])
        return real_post(url, headers, body, timeout=timeout)

    A._post = spy_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset({"ripgrep_search", "list_docs"})}
        ctx = _ctx()
        list(p._sub_agentic_loop(ctx))
        # SC-6e: 順序も固定する（`all_tools` の正準順から絞り込むため決定的・
        # set 比較では insert 位置の回帰を検出できない）。es_search/graph_neighbors/
        # ask_user/read_around 無し。
        names = [t["function"]["name"] for t in captured[0]]
        assert names == ["list_docs", "ripgrep_search"]
    finally:
        _restore_post(orig)


def test_toolset_reflects_conversation_tools_pref(monkeypatch):
    """SC-6e: `ctx.scope_meta["tools"]`（会話ごとの検索経路トグル）がプロファイル許可（`sub["tools"]`）と
    さらに AND される。プロファイルが全ツールを許可していても、会話が grep を OFF にしていれば
    ripgrep_search はループへ渡すツール定義配列に載らない。"""
    seq = [{"choices": [{"message": {"content": "no tools available so I answer directly"}}]}]
    orig = _install_post(seq)
    captured = []
    real_post = A._post

    def spy_post(url, headers, body, timeout=90):
        captured.append(body["tools"])
        return real_post(url, headers, body, timeout=timeout)

    A._post = spy_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset(_ALL_TOOLS)}   # プロファイルは全許可
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                              "tools": {"grep": False, "fulltext": True, "graph": True}})
        list(p._sub_agentic_loop(ctx))
        # SC-6e: 順序付き list で固定する（ripgrep_search が抜けた分だけ詰まった
        # 正準順のまま：list_docs→graph_neighbors→es_search→doc_outline→read_doc→read_around）。
        names = [t["function"]["name"] for t in captured[0]]
        assert names == ["list_docs", "graph_neighbors", "es_search", "doc_outline", "read_doc", "read_around"]
    finally:
        _restore_post(orig)


def test_toolset_conversation_tools_pref_omitted_is_full_on(monkeypatch):
    """`ctx.scope_meta` に `tools` キーが無い（既存呼び出し元・旧会話）場合は全ON＝従来どおり
    プロファイル許可のみで絞られる（byte-identical 回帰）。"""
    seq = [{"choices": [{"message": {"content": "no tools available so I answer directly"}}]}]
    orig = _install_post(seq)
    captured = []
    real_post = A._post

    def spy_post(url, headers, body, timeout=90):
        captured.append(body["tools"])
        return real_post(url, headers, body, timeout=timeout)

    A._post = spy_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset({"ripgrep_search", "list_docs"})}
        ctx = _ctx()   # scope_meta に "tools" キー無し
        list(p._sub_agentic_loop(ctx))
        names = [t["function"]["name"] for t in captured[0]]
        assert names == ["list_docs", "ripgrep_search"]
    finally:
        _restore_post(orig)


def test_disallowed_tool_call_rejected_and_loop_continues():
    """(b): モデルがツール定義配列に無いツール名を呼んでも run_tool を呼ばず拒否結果でループ継続する。

    拒否時のノードは `_tool_node`（args を使う豊かな表示）ではなく、args を一切使わない固定文言の
    ノードになる（label はツール名そのもの・detail は固定文言＝モデル生成の args を漏らさない）。
    """
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"SENTINEL_ARG_XYZZY"}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset({"ripgrep_search"})}   # graph_neighbors は許可外
        ctx = _ctx()
        events = list(p._sub_agentic_loop(ctx))
        final = next(e for e in events if "final" in e)
        assert final["searched"] is True and final["cites"]   # 2ターン目の ripgrep_search は成立
        nodes = [e["node"] for e in events if "node" in e]
        # ツール名もモデル生成値＝label も完全固定文言にする（漏洩防止）。
        rejected = [n for n in nodes if n["label"] == "許可外のツール呼び出し"]
        assert rejected and rejected[0]["detail"] == "許可されていないため拒否しました"
        assert not any("graph_neighbors" in str(n) for n in nodes)   # ツール名すら node に出さない
        assert "SENTINEL_ARG_XYZZY" not in str(nodes)   # モデル生成の args も含まれない
    finally:
        _restore_post(orig)


def test_ask_user_excluded_when_not_in_profile_tools():
    """ask_user がプロファイル tools に無ければツール定義配列から除外される。"""
    seq = [{"choices": [{"message": {"content": "final answer no tools"}}]}]
    orig = _install_post(seq)
    captured = []
    real_post = A._post

    def spy_post(url, headers, body, timeout=90):
        captured.append(body["tools"])
        return real_post(url, headers, body, timeout=timeout)

    A._post = spy_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset({"ripgrep_search"})}
        ctx = _ctx()
        list(p._sub_agentic_loop(ctx))
        names = {t["function"]["name"] for t in captured[0]}
        assert "ask_user" not in names
    finally:
        _restore_post(orig)


def test_ask_user_hallucination_rejected_when_can_ask_false():
    """`_sub_agentic_loop` の `can_ask` は常に False へ構造的に強制される（belt-and-suspenders）。
    ask_user を定義配列から除外する（(a)）だけでなく、`sub["tools"]` 自体は ask_user を許可して
    いてもモデルが幻覚呼び出しした場合に allowed_tools（=実際に提示した toolset）が拒否する（(b)）
    ことで、実際の質問イベントとして扱わずループ継続することを確認する。メッセージに「確認ID:」が
    含まれるかどうかは can_ask の決め手ではない（常に False）ため、本テストのメッセージは通常の
    メッセージと同じ結果になる一例として与える。
    """
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ask_user",
             "arguments": '{"prompt":"どちら？","mode":"single","options":[{"label":"A"},{"label":"B"}]}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset(_ALL_TOOLS)}   # プロファイル自体は ask_user を許可
        ctx = _ctx(message="確認ID:xyz 続きをお願いします")   # can_ask はメッセージ内容に関わらず常に False
        events = list(p._sub_agentic_loop(ctx))
        assert not any("question" in e for e in events)   # 実際の質問としては扱われない
        final = next(e for e in events if "final" in e)
        assert final["searched"] is True and final["cites"]   # 拒否後、2ターン目の ripgrep_search が成立
    finally:
        _restore_post(orig)


def test_rejected_ask_user_prompt_never_leaks_into_any_event():
    """拒否された幻覚 ask_user 呼び出しのモデル生成引数（prompt 等）が、思考ノード（trace 保存対象）・
    question・answer_delta・_result のいずれにも一切現れないことを確認する（「question が無い」だけ
    では、ノードの detail に prompt 文字列が漏れているケースを見逃す＝直接 sentinel 文字列の不在を
    全イベントに対して検査する）。
    """
    sentinel = "SENTINEL-LEAK-CHECK-9f3c2a"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ask_user",
             "arguments": f'{{"prompt":"{sentinel}","mode":"single",'
                          f'"options":[{{"label":"A"}},{{"label":"B"}}]}}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset(_ALL_TOOLS)}   # プロファイル自体は ask_user を許可
        ctx = _ctx(message="確認ID:xyz 続きをお願いします")   # can_ask=False（_can_ask が False を返す）
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert not any(sentinel in str(e) for e in events)
        assert sentinel not in (p._synth_prompts[0] if p._synth_prompts else "")
    finally:
        _restore_post(orig)


def test_es_search_hallucination_rejected_when_es_unavailable(monkeypatch):
    """ES 到達不可で es_search を定義配列から除外する（(a)）だけでなく、`sub["tools"]` 自体は
    es_search を許可していてもモデルが幻覚呼び出しした場合に allowed_tools が拒否し、`run_tool`
    （実 I/O）を一度も呼ばずにループ継続することを確認する。
    """
    monkeypatch.setattr(A.es_index, "available", lambda: False)
    called = []

    def _spy_search(*a, **kw):
        called.append(1)
        raise AssertionError("es_index.search が呼ばれた＝拒否ロジックをすり抜けた")

    monkeypatch.setattr(A.es_index, "search", _spy_search)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "es_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset(_ALL_TOOLS)}   # プロファイル自体は es_search を許可
        ctx = _ctx()
        events = list(p._sub_agentic_loop(ctx))
        final = next(e for e in events if "final" in e)
        assert final["searched"] is True and final["cites"]
        assert called == []   # es_index.search（実 I/O）は一度も呼ばれない
    finally:
        _restore_post(orig)


# ===== guard 注入 =====

def test_guard_max_turns_injected_bounds_loop():
    """max_turns=1 のプロファイルは1ターンで打ち切り（tool_calls が続いても2ターン目を発行しない）。"""
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}

    orig = A._post
    A._post = fake_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "guard": {"min_citations": 1, "max_turns": 1, "llm_timeout": 60}}
        ctx = _ctx()
        events = list(p._sub_agentic_loop(ctx))
        final = next(e for e in events if "final" in e)
        assert len(calls) == 1 and final["final"] == ""   # 打ち切り＝最終回答は空
    finally:
        A._post = orig


def test_guard_omitted_equals_env_default():
    """guard 省略時（None）は env 既定と同値に解決される（`search_helper._resolve_guard`）。"""
    from sherpa import search_helper as SH
    resolved = SH._resolve_guard()
    assert resolved == {"min_citations": 1, "max_turns": A.MAX_TURNS,
                        "llm_timeout": int(os.environ.get("SHERPA_LLM_TIMEOUT", "60"))}


# ===== 調べる深さ（調べ方ブロック §3.2）: 検索アシスタント（_sub）有効時の倍率配線 =====
# `_sub_loop`（`_sub_agentic_loop` 経由）は通常の `_agentic_loop` を迂回するため、独立して
# max_turns/max_hits/window_cap への配線を固定する（**admin 未設定時**は standard で guard 値の
# まま・admin 設定時は管理基準値が優先・deep/max だけ倍率が一度だけ効く）。

def test_sub_loop_scales_max_turns_hits_window_with_depth_profile(monkeypatch):
    """system_settings に depth_base_max_turns が無ければ guard["max_turns"]（既定 6）が基準値
    のまま・deep/max だけ倍率が乗る。hits/window は通常の _agentic_loop と同じ実効基準値
    （system_settings 未設定＝env 既定）を使う。"""
    from sherpa import depth_profile as D
    captured = {}

    def fake_openai_style(*a, **kw):
        captured.update(kw)
        return iter([])

    monkeypatch.setattr(A, "openai_style", fake_openai_style)
    p = _FakeSynth("sk-dummy", "gpt-5.5")
    p._sub = dict(_SUB)   # guard.max_turns = 6
    for profile, expected_turns in (("standard", 6), ("deep", 12), ("max", 18)):
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all", "depth_profile": profile})
        list(p._sub_agentic_loop(ctx))
        assert captured.get("max_turns") == expected_turns, profile
        assert captured.get("max_hits") == D.scaled_ratio(A.MAX_HITS, profile), profile
        assert captured.get("window_cap") == D.scaled_ratio(A.READ_WINDOW, profile), profile


def test_sub_loop_max_turns_prefers_admin_base_over_guard_when_set(monkeypatch):
    """system_settings に depth_base_max_turns があれば guard["max_turns"] より優先する
    （管理者が反復基準値を下げても検索アシスタント有効時だけ外れる、ということがないように）。
    倍率は基準値解決後の値へ一度だけ掛ける（guard 値へは掛けない）。"""
    from sherpa import depth_profile as D
    captured = {}

    def fake_openai_style(*a, **kw):
        captured.update(kw)
        return iter([])

    monkeypatch.setattr(A, "openai_style", fake_openai_style)
    p = _FakeSynth("sk-dummy", "gpt-5.5")
    p._sub = dict(_SUB)   # guard.max_turns = 6（system_settings 優先時は無視されるべき）
    p._system_settings = {"depth_base_max_turns": 5}
    for profile, expected_turns in (("standard", 5), ("deep", 10), ("max", 15)):
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all", "depth_profile": profile})
        list(p._sub_agentic_loop(ctx))
        assert captured.get("max_turns") == expected_turns, profile
        assert captured.get("max_turns") != D.scaled_turns(_SUB["guard"]["max_turns"], profile), profile


def test_sub_loop_depth_profile_omitted_keeps_guard_max_turns_unchanged():
    """scope_meta に depth_profile が無い（旧会話・呼び出し互換）場合は standard 扱い＝
    guard["max_turns"] を変えない（既存呼び出し元との byte-identical 維持）。"""
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured["max_turns_calls"] = captured.get("max_turns_calls", 0) + 1
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}

    orig = A._post
    A._post = fake_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "guard": {"min_citations": 1, "max_turns": 1, "llm_timeout": 60}}
        ctx = _ctx()   # scope_meta に depth_profile キー無し
        events = list(p._sub_agentic_loop(ctx))
        final = next(e for e in events if "final" in e)
        assert captured["max_turns_calls"] == 1 and final["final"] == ""   # guard=1 のまま打ち切り
    finally:
        A._post = orig


# ===== personal_facts 二重挿入回避 =====

def test_personal_facts_not_duplicated_in_hybrid_synthesis():
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx(personal_facts="秘密の個人メモ: XYZ")
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert len(p._synth_prompts) == 1
        # 元の message には無く personal_facts 由来の追記だけがブロックとして1回だけ現れる。
        assert p._synth_prompts[0].count("秘密の個人メモ: XYZ") == 1
    finally:
        _restore_post(orig)


# ===== stop_event 事前ガード =====

class _StreamCallFlag(_FakeSynth):
    """`_stream` が呼ばれたかを例外でなく属性で記録する（呼び出し元は `except Exception: pass` で
    包むため、例外を投げても検知できない＝flag 方式にする）。"""
    stream_called = False

    def _stream(self, prompt, completion=None):
        self.stream_called = True
        yield "SHOULD NOT BE CALLED"


def test_stop_event_already_set_before_tool_loop_skips_everything():
    """開始前から stop_event が立っていれば、ツールループにも合成にも一切進まない
    （サブループ自身の事前チェック＝`agentic_search.openai_style` の既存ガード）。
    understand/intent の2ノードは _agentic_run 冒頭で無条件に yield される既存仕様（不変）。
    2026-08-15: 下調べ役が付いているターンは「下調べ役に任せる」ノードも先に出す（誰が資料を
    読んでいるかを思考の流れで分かるようにするため）。ツール実行・合成へ進まない点は変わらない。"""
    calls = []
    orig = A._post
    A._post = lambda url, headers, body, timeout=90: calls.append(1)
    try:
        stop_event = threading.Event()
        stop_event.set()
        p = _StreamCallFlag("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx(stop_event=stop_event)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert [e["id"] for e in events] == ["understand", "intent", "search-helper"]
        assert calls == [] and p.stream_called is False
        assert not any(e.get("type") in ("answer_delta", "_result") for e in events)
    finally:
        A._post = orig


def test_stop_event_set_between_gate_and_synthesis_skips_stream_call(monkeypatch):
    """根拠ゲート通過後・合成 `_stream` 発行前の小さな窓で stop_event が立った場合に、合成呼び出しを
    発行しない（`_usage_meta` 呼び出し＝ゲート後・合成前の処理フックへ副作用として stop_event.set()
    を仕込み、この窓を決定的に再現する）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}], "prompt_eval_count": 1, "eval_count": 1},
    ]
    orig = _install_post(seq)
    stop_event = threading.Event()

    import sherpa.providers.base as base_mod
    real_usage_meta = base_mod._usage_meta

    def spying_usage_meta(*a, **kw):
        stop_event.set()   # 根拠ゲート通過後・合成発行前の窓を模す
        return real_usage_meta(*a, **kw)

    monkeypatch.setattr(base_mod, "_usage_meta", spying_usage_meta)
    try:
        p = _StreamCallFlag("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx(stop_event=stop_event)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert p.stream_called is False
        assert not any(e.get("type") == "answer_delta" for e in events)
        assert not any(e.get("type") == "_result" for e in events)   # acc 空・停止済み＝plain return
    finally:
        _restore_post(orig)


# ===== chat-sub metering（ループ終了時に成否問わず記録） =====

def test_metering_records_chat_sub_on_gate_fail(monkeypatch):
    # `list_docs`/`graph_neighbors` は has_structural_evidence を立てるため使わない（上の
    # test_on_gate_zero_citations_falls_back と同じ理由）。
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"NO-SUCH-HIT-XYZ"}'}}]}}]},
        {"choices": [{"message": {"content": "no citations"}}], "prompt_eval_count": 7, "eval_count": 2},
    ]
    orig = _install_post(seq)
    recorded = []

    def spy_record(kind, provider, model, usage, *, user_id=None, world=None, calls=1):
        recorded.append((kind, provider, model, usage, user_id, world))

    monkeypatch.setattr("sherpa.metering.record", spy_record)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx(uid="u1")
        with pytest.raises(RuntimeError):
            list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert len(recorded) == 1
        kind, provider, model, usage, uid, world = recorded[0]
        assert kind == "chat-sub" and provider == "ollama" and model == "qwen2.5"
        assert usage == {"input_tokens": 7, "cached_input_tokens": 0, "output_tokens": 2,
                         "reasoning_output_tokens": 0}
        assert uid == "u1" and world == "v1"
    finally:
        _restore_post(orig)


def test_metering_not_recorded_when_sub_never_ran(monkeypatch):
    """SSRF 事前ブロックでループが1回も呼び出しを試みなければ chat-sub を記録しない（calls=0）。"""
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("SHERPA_VLM_OLLAMA_URL", raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    recorded = []
    monkeypatch.setattr("sherpa.metering.record",
                        lambda *a, **kw: recorded.append((a, kw)))
    p = _FakeSynth("sk-dummy", "gpt-5.5")
    p._sub = {**_SUB, "url": "http://intra.example:11434"}
    ctx = _ctx()
    with pytest.raises(A.llm.SsrfBlocked):
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
    assert recorded == []


def test_metering_records_calls_one_tokens_none_on_first_post_failure(monkeypatch):
    """初回 `_post` が接続断（`URLError`＝再試行対象）で失敗し続けると、限定リトライ（既定2回・
    計3回試行）を使い切ってから伝播する。`calls` は物理送信のたびにインクリメントするため
    （1物理送信=1消費）3（＝実際に試みた回数）になり、`tokens` は成功した呼び出しが無いので
    None（報告不能マーカー）で1行記録される。"""
    import urllib.error

    def failing_post(url, headers, body, timeout=90):
        raise urllib.error.URLError("boom first call")

    orig = A._post
    A._post = failing_post
    recorded = []
    monkeypatch.setattr(
        "sherpa.metering.record",
        lambda kind, provider, model, usage, **kw: recorded.append((kind, provider, model, usage, kw)))
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        with pytest.raises(urllib.error.URLError):
            list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert len(recorded) == 1
        kind, provider, model, usage, kw = recorded[0]
        assert kind == "chat-sub" and provider == "ollama"
        assert usage is None   # 報告不能マーカー（成功した呼び出しが無い）
        assert kw["calls"] == 3
    finally:
        A._post = orig


def test_metering_records_partial_usage_and_correct_calls_on_midloop_failure(monkeypatch):
    """1ターン目成功後、2ターン目以降が接続断（`URLError`＝再試行対象）で失敗し続けると、
    限定リトライ（既定2回・計3回試行）を使い切ってから伝播する。1ターン目までの成功分は
    `usage_acc` へ既に反映済みなので記録される。`calls` は物理送信のたびにインクリメントする
    ため（1物理送信=1消費）、1ターン目の1回＋2ターン目の試行3回＝4になる。"""
    import urllib.error

    call_n = [0]

    def flaky_post(url, headers, body, timeout=90):
        call_n[0] += 1
        if call_n[0] == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}],
                    "prompt_eval_count": 5, "eval_count": 3}
        raise urllib.error.URLError("boom mid-loop")

    orig = A._post
    A._post = flaky_post
    recorded = []
    monkeypatch.setattr(
        "sherpa.metering.record",
        lambda kind, provider, model, usage, **kw: recorded.append((kind, provider, model, usage, kw)))
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)
        ctx = _ctx()
        with pytest.raises(urllib.error.URLError):
            list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert len(recorded) == 1
        kind, provider, model, usage, kw = recorded[0]
        assert kind == "chat-sub" and provider == "ollama"
        assert usage == {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 3,
                         "reasoning_output_tokens": 0}
        assert kw["calls"] == 4   # 1ターン目1回＋2ターン目の試行3回（1回目+リトライ2回）
    finally:
        A._post = orig


def test_metering_records_usage_across_rejected_ask_user_turn(monkeypatch):
    """サブ経路の `can_ask` は常に False へ構造的に強制されるため、ask_user はサブ経路では
    `{"question": ...}` を起こせず、許可外ツールとして拒否されループ継続するだけになる。ask_user が
    拒否された後もループが正常に継続し、成功した全ターン（拒否ターンの `_post` 自体は成功している）
    分の chat-sub 計測（calls/tokens）が漏れなく記録されることを確認する。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}],
         "prompt_eval_count": 4, "eval_count": 2},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ask_user",
             "arguments": '{"prompt":"どちら？","mode":"single","options":[{"label":"A"},{"label":"B"}]}'}}]}}],
         "prompt_eval_count": 3, "eval_count": 1},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    recorded = []
    monkeypatch.setattr(
        "sherpa.metering.record",
        lambda kind, provider, model, usage, **kw: recorded.append((kind, provider, model, usage, kw)))
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = dict(_SUB)   # プロファイル自体は tools に ask_user を含む（それでも拒否される）
        ctx = _ctx()          # 「確認ID:」なし＝can_ask はメッセージ内容に関わらず常に False
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert not any(e.get("type") == "question" for e in events)   # ask_user はもう question を起こせない
        assert len(recorded) == 1
        kind, provider, model, usage, kw = recorded[0]
        assert kind == "chat-sub" and provider == "ollama"
        assert usage == {"input_tokens": 7, "cached_input_tokens": 0, "output_tokens": 3,
                         "reasoning_output_tokens": 0}
        assert kw["calls"] == 3   # ripgrep成功 + ask_user拒否（_post自体は成功）+ 最終ターンの3回
    finally:
        _restore_post(orig)


def test_sub_path_ask_user_disabled_even_without_confirmation_marker():
    """`_sub_agentic_loop` は `can_ask` を常に False へ構造的に強制する（belt-and-suspenders）。
    `sub["tools"]` に `ask_user` を明示的に含み、かつメッセージに「確認ID:」（確認済み再送の判定
    条件）が無くても、`ask_user` はモデルへ提示するツール定義配列（`body["tools"]`）に一切載らない
    ことを確認する。
    """
    seq = [{"choices": [{"message": {"content": "final answer no tools"}}]}]
    orig = _install_post(seq)
    captured = []
    real_post = A._post

    def spy_post(url, headers, body, timeout=90):
        captured.append(body["tools"])
        return real_post(url, headers, body, timeout=timeout)

    A._post = spy_post
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset(_ALL_TOOLS)}   # プロファイル自体は ask_user を許可
        ctx = _ctx()   # 「確認ID:」なしのメッセージでも can_ask は常に False
        list(p._sub_agentic_loop(ctx))
        names = {t["function"]["name"] for t in captured[0]}
        assert "ask_user" not in names
    finally:
        _restore_post(orig)



def test_hallucinated_tool_name_sentinel_never_leaks():
    """`function.name` もモデル生成値。未知の長い名前（sentinel）を返されても、全イベントの
    どこにも現れないこと（拒否ノードは完全固定文言）。"""
    sentinel = "SENTINEL_MODEL_NAME_超長い任意文字列XYZZY"
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": sentinel, "arguments": '{"a":1}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL"}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _FakeSynth("sk-dummy", "gpt-5.5")
        p._sub = {**_SUB, "tools": frozenset({"ripgrep_search"})}
        ctx = _ctx()
        events = list(p._sub_agentic_loop(ctx))
        assert sentinel not in str(events)   # node/question/final どこにも出ない
        final = next(e for e in events if "final" in e)
        assert final["searched"] is True     # 拒否後もループ継続して検索は成立
    finally:
        _restore_post(orig)


# ===== get_provider→_sub 配線の契約 =====
# 上のテスト群は `_sub` を手組みの `_SUB` 辞書で与える（`_sub_loop` 単体の契約を軽量に確認するため）。
# ここでは (a) 手組みの辞書が不完全だと `_sub_loop` は補完も検出もせず KeyError で落ちること
# （＝配線側の正しさを保証する責務は producer 側にしかない）、(b) `search_helper.resolve()` 経由で
# `get_provider()` が組み立てた**実物**の `_sub` を `_sub_agentic_loop` が実際に消費できることを
# end-to-end で固定する（形の検証自体は `tests/unit/test_search_helper.py::_assert_sub_shape` が
# 別途担う）。

def test_incomplete_sub_dict_raises_instead_of_silently_falling_back():
    """`_sub_loop` は `sub` 辞書の必須キー欠落を検出も補完もしない＝配線が壊れて不完全な辞書
    （例: `{"provider": "ollama"}` のみ）を渡すと KeyError で落ちる（黙ってメインへ縮退したり
    しない）。したがって配線の正しさは producer 側（`get_provider()` が組み立てる実物の `_sub`）を
    検証するしかない＝`test_search_helper.py::_assert_sub_shape` の必要性の根拠。"""
    p = _FakeSynth("sk-dummy", "gpt-5.5")
    p._sub = {"provider": "ollama"}   # tools/model/guard/url が全て欠落
    ctx = _ctx()
    with pytest.raises(KeyError):
        list(p._sub_agentic_loop(ctx))


def test_get_provider_wired_sub_is_actually_consumable_by_sub_loop(monkeypatch):
    """`search_helper.resolve()` 経由で `get_provider()` が組み立てた実物の `_sub`
    （手組みの `_SUB` ではない）を `_sub_agentic_loop` が実際に消費できることを end-to-end で
    固定する。"""
    from sherpa.providers import get_provider

    # WEB-1: `get_provider()` の1ターン唯一の読取点は `store._read_system_settings_fresh()`
    # （共有キャッシュを介さない生の読取・TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"personal_api_keys_allowed": True})
    seq = [{"choices": [{"message": {"content": "LOCAL"}}]}]
    orig = _install_post(seq)
    try:
        p = get_provider({"agent": "openai", "openai_api_key": "sk-x", "search_helper": "ollama",
                          "ollama_url": "http://localhost:11434", "ollama_model": "qwen2.5"})
        assert p._sub is not None
        events = list(p._sub_agentic_loop(_ctx()))
        final = next(e for e in events if "final" in e)
        assert isinstance(final["final"], str)
    finally:
        _restore_post(orig)


# ===== `_run_sub_plan`（S4-b・複数プロファイル計画）: 1ステップ失敗の可視化 =====
# 「1ステップの失敗は計画全体を止めない」契約（続行）——ただし握り潰さずログ＋実行トレースの
# 両方に残す。

def test_run_sub_plan_step_init_failure_is_logged_and_traced_but_plan_continues(monkeypatch, caplog):
    """1つ目のプロファイルの起動自体が失敗（SSRF ブロック等・`_sub_loop` 呼び出しが同期的に
    例外を送出）しても、計画は2つ目のプロファイルへ続行し証拠を合算する（挙動不変）。
    この失敗は `sherpa` ロガーへの警告と、`sub:{profile_id}:step-failed` の実行トレースノードの
    両方に残る。"""
    import logging

    p = _FakeSynth("sk-dummy", "gpt-5.5")
    ctx = _ctx()
    sub_broken = {**_SUB, "profile_id": "broken"}
    sub_ok = {**_SUB, "profile_id": "ok"}

    def fake_sub_loop(step_ctx, sub, usage_acc, **kw):
        if sub["profile_id"] == "broken":
            raise OSError("boom")
        def _gen():
            yield {"final": "", "docs": {"doc-ok"}, "searched": True, "cites": [], "cards": []}
        return _gen()

    monkeypatch.setattr(p, "_sub_loop", fake_sub_loop)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        events = list(p._run_sub_plan(ctx, [sub_broken, sub_ok]))

    nodes = [e["node"] for e in events if "node" in e]
    assert any(n["id"] == "sub:broken:step-failed" for n in nodes), nodes
    final = next(e for e in events if "final" in e)
    assert final["docs"] == {"doc-ok"}   # 続行して sub_ok の証拠は合算される（挙動不変）
    assert any("broken" in r.message for r in caplog.records), caplog.records


def test_run_sub_plan_step_mid_iteration_failure_is_logged_and_traced_but_plan_continues(monkeypatch, caplog):
    """起動は成功したが途中（`next()`）で失敗するケース（ネットワーク断・JSON 破損等）でも同様に
    握り潰さず可視化しつつ、計画は続行する（挙動不変）。"""
    import logging

    p = _FakeSynth("sk-dummy", "gpt-5.5")
    ctx = _ctx()
    sub_broken = {**_SUB, "profile_id": "broken"}
    sub_ok = {**_SUB, "profile_id": "ok"}

    def fake_sub_loop(step_ctx, sub, usage_acc, **kw):
        if sub["profile_id"] == "broken":
            def _gen():
                yield {"node": {"id": "understand", "kind": "think", "label": "x",
                               "detail": "", "status": "done"}}
                raise OSError("boom mid-iter")
            return _gen()
        def _gen_ok():
            yield {"final": "", "docs": {"doc-ok"}, "searched": True, "cites": [], "cards": []}
        return _gen_ok()

    monkeypatch.setattr(p, "_sub_loop", fake_sub_loop)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        events = list(p._run_sub_plan(ctx, [sub_broken, sub_ok]))

    nodes = [e["node"] for e in events if "node" in e]
    assert any(n["id"] == "sub:broken:step-failed" for n in nodes), nodes
    final = next(e for e in events if "final" in e)
    assert final["docs"] == {"doc-ok"}
    assert any("broken" in r.message for r in caplog.records), caplog.records


# ===== 層フィルタの単一判定点（_ctx_with_effective_layer）=====

def test_agentic_run_forces_both_layer_for_impact_lens_regardless_of_request():
    """impact レンズは _agentic_loop へ渡る ctx.scope_meta["layer"] を強制的に both へ揃える
    （回答メタの layer_applied=False と実際の検索挙動を一致させる）。"""
    from sherpa.providers.base import Ctx, _GenProvider

    captured = {}

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"
        _natural_completion_reasons = frozenset({"stop"})

        def _agentic_loop(self, ctx):
            captured["layer"] = (ctx.scope_meta or {}).get("layer")

            def _gen():
                yield {"final": "回答", "docs": set(), "searched": True, "cites": [],
                      "cards": [], "has_structural_evidence": True}
            return _gen()

    p = _P()
    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    ctx = Ctx(message="消費税率を変えたら？", world="v1", knowledge=True, scope_meta=scope_meta,
             route=lambda m: {"lens": "impact", "reason": "t", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
             make_sources=lambda docs: [{"doc_id": d} for d in docs])
    events = list(p.run(ctx))
    assert captured["layer"] == "both"                        # 要求は code だが impact には渡さない
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["scope"]["layer"] == "code"          # メタには要求値をそのまま残す
    assert result["env"]["scope"]["layer_applied"] is False


def test_agentic_run_forces_both_layer_for_troubleshoot_lens():
    from sherpa.providers.base import Ctx, _GenProvider

    captured = {}

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"
        _natural_completion_reasons = frozenset({"stop"})

        def _agentic_loop(self, ctx):
            captured["layer"] = (ctx.scope_meta or {}).get("layer")

            def _gen():
                yield {"final": "回答", "docs": set(), "searched": True, "cites": [],
                      "cards": [], "has_structural_evidence": True}
            return _gen()

    p = _P()
    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "docs"}
    ctx = Ctx(message="夜間バッチが止まった", world="v1", knowledge=True, scope_meta=scope_meta,
             route=lambda m: {"lens": "troubleshoot", "reason": "t", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
             make_sources=lambda docs: [{"doc_id": d} for d in docs])
    events = list(p.run(ctx))
    assert captured["layer"] == "both"
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["scope"]["layer"] == "docs"
    assert result["env"]["scope"]["layer_applied"] is False


def test_agentic_run_passes_through_real_layer_for_qa_lens():
    """qa レンズは要求どおりの layer 値が _agentic_loop まで届く（layer_applied=True と一致）。"""
    from sherpa.providers.base import Ctx, _GenProvider

    captured = {}

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"
        _natural_completion_reasons = frozenset({"stop"})

        def _agentic_loop(self, ctx):
            captured["layer"] = (ctx.scope_meta or {}).get("layer")

            def _gen():
                yield {"final": "回答", "docs": set(), "searched": True, "cites": [],
                      "cards": [], "has_structural_evidence": True}
            return _gen()

    p = _P()
    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    ctx = Ctx(message="消費税率とは？", world="v1", knowledge=True, scope_meta=scope_meta,
             route=lambda m: {"lens": "qa", "reason": "t", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
             make_sources=lambda docs: [{"doc_id": d} for d in docs])
    events = list(p.run(ctx))
    assert captured["layer"] == "code"
    result = next(e for e in events if e.get("type") == "_result")
    assert result["env"]["scope"]["layer"] == "code"
    assert result["env"]["scope"]["layer_applied"] is True


def test_ctx_with_effective_layer_reuses_ctx_when_already_matching():
    """既に一致していれば dataclasses.replace で新しい ctx を作り直さない（無駄なコピー回避）。"""
    from sherpa.providers.base import Ctx, _ctx_with_effective_layer

    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "both"}
    ctx = Ctx(message="m", world="v1", route=lambda m: {}, dispatch=lambda l, i: {},
             scope_meta=scope_meta)
    assert _ctx_with_effective_layer(ctx, "impact") is ctx    # both のまま＝差し替え不要


def test_agentic_run_plan_forces_both_layer_for_impact_lens(monkeypatch):
    """S4-c の plan 経路（_agentic_run_plan→_run_sub_plan→_sub_loop）も同じ単一
    判定点（_ctx_with_effective_layer）を通り、非適用レンズでは both に揃う。"""
    p = _FakeSynth("sk-dummy", "gpt-5.5")
    captured = {}

    def fake_sub_loop(step_ctx, sub, usage_acc, **kw):
        captured["layer"] = (step_ctx.scope_meta or {}).get("layer")

        def _gen():
            yield {"final": "", "docs": {"doc.md"}, "searched": True, "cites": [], "cards": [],
                  "has_structural_evidence": True}
        return _gen()

    monkeypatch.setattr(p, "_sub_loop", fake_sub_loop)
    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    ctx = _ctx(scope_meta=scope_meta)
    decision = {"lens": "impact", "input": ctx.message, "reason": "t"}
    sub = {**_SUB, "profile_id": "worker"}
    list(p._agentic_run_plan(ctx, decision, ctx.message, [sub]))
    assert captured["layer"] == "both"


def test_agentic_run_plan_passes_through_real_layer_for_qa_lens(monkeypatch):
    p = _FakeSynth("sk-dummy", "gpt-5.5")
    captured = {}

    def fake_sub_loop(step_ctx, sub, usage_acc, **kw):
        captured["layer"] = (step_ctx.scope_meta or {}).get("layer")

        def _gen():
            yield {"final": "", "docs": {"doc.md"}, "searched": True, "cites": [], "cards": [],
                  "has_structural_evidence": True}
        return _gen()

    monkeypatch.setattr(p, "_sub_loop", fake_sub_loop)
    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    ctx = _ctx(scope_meta=scope_meta)
    decision = {"lens": "qa", "input": ctx.message, "reason": "t"}
    sub = {**_SUB, "profile_id": "worker"}
    list(p._agentic_run_plan(ctx, decision, ctx.message, [sub]))
    assert captured["layer"] == "code"
