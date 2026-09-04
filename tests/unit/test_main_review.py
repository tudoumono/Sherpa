"""EXT-2b（評価フェーズ再起・メイン査読）の実行基盤テスト。

ハイブリッド（下調べ役あり）の清書前に、メイン LLM が根拠の十分性を査読し、不足なら
不足軸を指定して下調べを再実行する（なお不足なら honest failure）契約を固定する。
発動は調べる深さ（standard=0回／deep=1回／max=2回）に載る。harness は
`tests/unit/test_sub_loop.py` と同型（LLM は stub・コスト0・fixtures world）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

import sherpa.agentic_search as A  # noqa: E402
from sherpa.agents import Ctx, OpenAIProvider  # noqa: E402

_ALL_TOOLS = frozenset({"list_docs", "ripgrep_search", "read_around", "es_search",
                        "graph_neighbors", "ask_user"})

_SUB = {"provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5",
        "tools": frozenset(_ALL_TOOLS), "guard": {"min_citations": 1, "max_turns": 6, "llm_timeout": 60},
        "profile_id": "worker"}


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


class _ReviewSynth(OpenAIProvider):
    """`_stream` を応答列で差し替える（査読応答→…→最終合成の順に消費される）。"""

    def __init__(self, *a, responses=(), **kw):
        super().__init__(*a, **kw)
        self._responses = list(responses)
        self._synth_prompts: list = []

    def _stream(self, prompt, completion=None):
        self._synth_prompts.append(prompt)
        if completion is not None:
            completion.terminal_seen = True
            completion.reason = "stop"
        yield self._responses.pop(0)

    def _attribute(self, text, digest, ev_map, call_budget=None):
        return set()


class _ReviewSynthWithUsage(_ReviewSynth):
    """`_stream` が本物の usage 相当（`_last_usage`）も残す（EXT-2c usage 記録テスト用）。"""

    def _stream(self, prompt, completion=None):
        self._synth_prompts.append(prompt)
        if completion is not None:
            completion.terminal_seen = True
            completion.reason = "stop"
        self._last_usage = {"input_tokens": 10, "cached_input_tokens": 0,
                            "output_tokens": 5, "reasoning_output_tokens": 0}
        yield self._responses.pop(0)


# EXT-2c テスト用の実在 doc_id（fixtures/corpus/v1、SHERPA_USE_FIXTURES=1 で解決される）。
_REVIEW_DOC = "4期/01_標準/消費税法.md"


@pytest.fixture(autouse=True)
def _hermetic_es_graph(monkeypatch):
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)


def _install_post(seq, bodies=None):
    orig = A._post

    def _fake(url, headers, body, timeout=90):
        if bodies is not None:
            bodies.append(body)
        return seq.pop(0)

    A._post = _fake
    return orig


def _sub_run_seq():
    """下調べ1周分の _post 応答（list_docs → 散文終了＝構造 Evidence で根拠ゲートを通す）。"""
    return [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
    ]


def _mk(responses):
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=responses)
    p._sub = dict(_SUB)
    return p


def test_standard_profile_skips_review():
    """standard（既定）は査読を発動しない＝ _stream は最終合成の1回だけ（従来挙動不変）。"""
    orig = _install_post(_sub_run_seq())
    try:
        p = _mk(["CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert len(p._synth_prompts) == 1
        assert not any(e.get("id") == "main-review" for e in events)
    finally:
        A._post = orig


def test_deep_insufficient_once_reruns_with_missing_axes():
    """deep: 査読が不足→不足軸つきで下調べを1回再実行→再査読 sufficient→清書。"""
    bodies = []
    orig = _install_post(_sub_run_seq() + _sub_run_seq(), bodies)
    try:
        p = _mk(['{"sufficient": false, "missing": "税率の適用開始日"}',
                 '{"sufficient": true, "missing": ""}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        # 査読2回＋合成1回
        assert len(p._synth_prompts) == 3
        assert "査読者" in p._synth_prompts[0]
        # 再実行の下調べへ不足軸が伝わる
        rerun_payload = str(bodies[2:])
        assert "前回の調査で不足していた観点" in rerun_payload
        assert "税率の適用開始日" in rerun_payload
        reviews = [e for e in events if e.get("id") == "main-review"]
        assert any("調べ直します" in (e.get("detail") or "") for e in reviews)
        assert any("答えられる" in (e.get("detail") or "") for e in reviews)
    finally:
        A._post = orig


def test_deep_still_insufficient_is_honest_failure():
    """deep: 再調査してもなお不足→清書せず honest failure（RuntimeError を送出）。"""
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk(['{"sufficient": false, "missing": "適用範囲"}',
                 '{"sufficient": false, "missing": "適用範囲"}'])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        with pytest.raises(RuntimeError, match="insufficient"):
            list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
    finally:
        A._post = orig


def test_fold_sub_usage_sums_and_keeps_none_contract():
    """H1: 実行ごとの chat-sub 消費の合算。tokens は1回でも不明なら合計も None。"""
    from sherpa.providers.base import _fold_sub_usage
    t = {"calls": 0, "tokens": None, "unknown": False}
    t = _fold_sub_usage(t, {"calls": 2, "tokens": {"prompt_tokens": 10, "completion_tokens": 5}})
    t = _fold_sub_usage(t, {"calls": 3, "tokens": {"prompt_tokens": 1, "completion_tokens": 2}})
    assert t["calls"] == 5 and t["tokens"] == {"prompt_tokens": 11, "completion_tokens": 7}
    t = _fold_sub_usage(t, {"calls": 1, "tokens": None})   # 不明が混ざる
    assert t["calls"] == 6 and t["tokens"] is None and t["unknown"]
    t = _fold_sub_usage(t, {"calls": 2, "tokens": {"prompt_tokens": 9}})   # 以後も不明のまま
    assert t["calls"] == 8 and t["tokens"] is None
    assert _fold_sub_usage(t, None) is t and _fold_sub_usage(t, {"calls": 0}) is t


def test_metering_records_initial_and_rerun_runs(monkeypatch):
    """H1: 再調査があっても metering は初回＋再調査の総 calls を1回で記録する（chat-sub）。
    EXT-2c: 査読自体の `_stream` 消費（chat-review）も別の1行として記録される。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record",
                        lambda *a, **kw: recorded.append((a, kw)))
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk(['{"sufficient": false, "missing": "税率の適用開始日"}',
                 '{"sufficient": true, "missing": ""}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        # M-2是正: dict 集約（後勝ち）だけだと同じ kind の二重記録を見逃す——record 呼び出し自体が
        # ちょうど2回（chat-sub 1回＋chat-review 1回）であることも独立に確認する。
        assert len(recorded) == 2
        by_kind = {a[0]: kw for a, kw in recorded}
        assert set(by_kind) == {"chat-sub", "chat-review"}
        # 初回2 POST＋再調査2 POST＝総4 calls（最後の実行分だけだと2になる）
        assert by_kind["chat-sub"]["calls"] == 4
        # 査読は不足→再調査で2回発動（初回査読＋再査読）。
        assert by_kind["chat-review"]["calls"] == 2
    finally:
        A._post = orig


def test_stop_during_review_aborts_without_synthesis():
    """M3: 査読ストリーム中の停止要求で清書へ進まない（_result も出さない）。"""
    import threading
    stop = threading.Event()

    class _StopDuringReview(_ReviewSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "stop"
            stop.set()   # 査読応答の途中で停止要求が来る
            yield self._responses.pop(0)

    orig = _install_post(_sub_run_seq())
    try:
        p = _StopDuringReview("sk-dummy", "gpt-5.5",
                              responses=['{"sufficient": true, "missing": ""}', "SHOULD NOT SYNTH"])
        p._sub = dict(_SUB)
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"},
                   stop_event=stop)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert not any(e.get("type") == "_result" for e in events)
        assert not any(e.get("type") == "answer_delta" for e in events)
        assert len(p._synth_prompts) == 1   # 査読1回のみ・合成は発行しない
    finally:
        A._post = orig


def test_budget_exhausted_rerun_stops_review_loop():
    """M4: 再調査が調査予算で打ち切られたら次の査読へ進まず、集まった分で清書する。"""
    # 再調査はツール呼び出しだけを guard["max_turns"]=6 回返して turns_exhausted で終わる
    rerun_exhaust = [{"choices": [{"message": {"content": "", "tool_calls": [
        {"id": f"c{i}", "function": {"name": "list_docs", "arguments": "{}"}}]}}]}
        for i in range(20)]   # max profile は max_turns が 6×3=18 へ倍率適用される（SC-6c）＋余裕
    orig = _install_post(_sub_run_seq() + rerun_exhaust)
    try:
        p = _mk(['{"sufficient": false, "missing": "適用範囲"}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "max"})   # max=2回許容でも予算打ち切りで1回で止まる
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert len(p._synth_prompts) == 2   # 査読1回＋合成1回（予算打ち切り後の再査読なし）
    finally:
        A._post = orig


def test_run_honest_failure_message_distinguishes_insufficient():
    """M5: 「再調査後もなお不足」は設定障害の文言ではなく根拠不足の文言で返す。"""
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk(['{"sufficient": false, "missing": "適用範囲"}',
                 '{"sufficient": false, "missing": "適用範囲"}'])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p.run(ctx))
        result = next(e for e in events if e.get("type") == "_result")
        assert "十分な根拠を確認できませんでした" in result["env"]["headline"]
        assert "OFFにしてください" not in result["env"]["headline"]
    finally:
        A._post = orig


def test_synth_citation_view_puts_rerun_evidence_first():
    """RV2 M1: 清書ビューは再調査の新規 citation を先頭へ（公開 env の順は呼び出し元で不変）。"""
    from sherpa.providers.base import _synth_citation_view
    old1, old2, new1 = {"doc_id": "a"}, {"doc_id": "b"}, {"doc_id": "c"}
    cites = [old1, old2, new1]
    view = _synth_citation_view(cites, {id(new1)})
    assert view == [new1, old1, old2]
    assert cites == [old1, old2, new1]          # 元 list は不変
    assert _synth_citation_view(cites, set()) is cites            # rerun なし＝素通し
    assert _synth_citation_view(cites, {id(object())}) is cites   # 生存 citation に該当なし＝素通し


def test_deep_unparsable_verdict_fails_open():
    """査読応答が JSON でない＝判定不能→fail-open で従来どおり清書へ進む。"""
    orig = _install_post(_sub_run_seq())
    try:
        p = _mk(["ただの文章で JSON ではない", "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert len(p._synth_prompts) == 2
    finally:
        A._post = orig


# ---- EXT-2c（査読フェーズの限定ツール精読）----

def test_review_read_around_feeds_result_into_final_verdict():
    """(a) read_around 要求→ツール実行結果がプロンプトへ追記され、その内容を踏まえた最終判定に至る。"""
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=[
        f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 1}}',
        '{"sufficient": true, "missing": ""}'])
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None)
    assert verdict == {"sufficient": True, "missing": ""}
    assert len(nodes) == 1
    assert nodes[0]["detail"] == f"原文を確かめています: {_REVIEW_DOC}"
    assert len(p._synth_prompts) == 2
    # L-1是正: "消費税法" は doc_id 文字列自体のエコーでも通ってしまう（本文を読んだ証明にならない）
    # ため、本文専用の文字列（ファイル3行目）でアサートする。
    assert "ツール結果" in p._synth_prompts[1] and "税率に関する法令上の規約" in p._synth_prompts[1]
    assert usage == {"calls": 2, "tokens": None}   # スタブは _last_usage を更新しない＝報告不能扱い


def test_review_read_cap_forces_verdict():
    """(b) read 系が上限（4回）を超えたら、次の1回で判定確定を強制する（読み過ぎない）。"""
    responses = [f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": {i}}}'
                for i in range(1, 5)]                                    # 上限どおり4回
    responses.append(f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 9}}')  # 5回目（超過）
    responses.append('{"sufficient": false, "missing": "適用範囲"}')     # 強制された確定
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=responses)
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None)
    assert verdict == {"sufficient": False, "missing": "適用範囲"}
    assert len(nodes) == 4                       # 実際にツールを呼んだのは上限の4回だけ
    assert len(p._synth_prompts) == 6            # 4読み取り＋超過要求1回＋強制確定1回
    assert "これ以上は読み取れません" in p._synth_prompts[5]
    assert usage == {"calls": 6, "tokens": None}


def test_review_read_cap_exceeded_twice_fails_open():
    """(b') 強制確定の指示にもなお読もうとした場合は、無限ループにせず fail-open で打ち切る。"""
    responses = [f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": {i}}}'
                for i in range(1, 5)]
    responses.append(f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 9}}')   # 超過1回目
    responses.append(f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 10}}')  # 強制後もなお読もうとする
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=responses)
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None)
    assert verdict is None
    assert len(nodes) == 4
    assert len(p._synth_prompts) == 6   # 6回目で fail-open（7回目は発行しない）


def test_review_tool_execution_failure_fails_open(monkeypatch):
    """(c) 精読ツール実行が例外を送出したら査読を省略する（fail-open・従来の通信/JSON不備と同じ扱い）。"""
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(A, "run_tool", _boom)
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=[
        f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 1}}'])
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None)
    assert verdict is None
    assert nodes == []
    assert usage == {"calls": 1, "tokens": None}


def test_review_usage_recorded_via_metering(monkeypatch):
    """(d) 査読（read 込み）の _stream 消費が chat-review として metering に記録される。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record", lambda *a, **kw: recorded.append((a, kw)))
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _ReviewSynthWithUsage("sk-dummy", "gpt-5.5", responses=[
            f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 1}}',
            '{"sufficient": false, "missing": "税率の適用開始日"}',
            '{"sufficient": true, "missing": ""}',
            "CLOUD SYNTH ANSWER"])
        p._sub = dict(_SUB)
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        kinds = [a[0] for a, kw in recorded]
        # M-2是正: dict 集約だけでは同じ kind の二重記録（後勝ちで上書き）を見逃す——各 kind が
        # ちょうど1回ずつ記録されたことを出現回数で独立に確認する。
        assert kinds.count("chat-sub") == 1
        assert kinds.count("chat-review") == 1
        by_kind = {a[0]: {"provider": a[1], "model": a[2], "tokens": a[3], **kw} for a, kw in recorded}
        review = by_kind["chat-review"]
        assert review["provider"] == "openai" and review["model"] == "gpt-5.5"
        assert review["calls"] == 3   # 初回査読（read1回＋判定1回）＋再査読1回（sufficient・reads無し）
        assert review["tokens"] == {"input_tokens": 30, "cached_input_tokens": 0,
                                    "output_tokens": 15, "reasoning_output_tokens": 0}
    finally:
        A._post = orig


def test_review_stop_event_during_read_loop_aborts_immediately(monkeypatch):
    """(e) stop_event はループ先頭でも観測する＝読み取り後の次呼び出しへは進まない。"""
    import threading
    stop = threading.Event()
    orig_run_tool = A.run_tool

    def _run_tool_then_stop(*a, **kw):
        # ツール実行と次ループの間で停止要求が来た状況を模す。
        result = orig_run_tool(*a, **kw)
        stop.set()
        return result

    monkeypatch.setattr(A, "run_tool", _run_tool_then_stop)
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=[
        f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 1}}',
        '{"sufficient": true, "missing": ""}'])   # 消費されないはず
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None, stop_event=stop)
    assert verdict is None
    assert len(nodes) == 1              # 読み取りは1回だけ実行された
    assert len(p._synth_prompts) == 1   # 2回目の _stream（確定判定の消費）は発行されない
