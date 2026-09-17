"""EXT-2b（評価フェーズ再起・メイン査読）の実行基盤テスト。

ハイブリッド（下調べ役あり）の清書前に、メイン LLM が根拠の十分性を査読し、不足なら
不足軸を指定して下調べを再実行する（なお不足なら honest failure）契約を固定する。
発動は調べる深さ（standard=0回／deep=1回／max=2回）に載る。harness は
`tests/unit/test_sub_loop.py` と同型（LLM は stub・コスト0・fixtures world）。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

import sherpa.agentic_search as A  # noqa: E402
from sherpa.agents import Ctx, OpenAIProvider  # noqa: E402
from sherpa.providers import base as PB  # noqa: E402

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
    """下調べ1周分の _post 応答（list_docs → 散文終了＝構造 Evidence で根拠ゲートを通す →
    DEPTH-2 S4b: 一次判断の要求に空の `claims` を返す＝呼び出し回数だけ整合させ `state.claims` へは
    何も伝播しない・下の個別テストの `_synth_prompts`/`data["claims"]` 期待を変えない）。"""
    return [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
        {"choices": [{"message": {"content": '{"claims": []}'}}]},
    ]


def _mk(responses, max_review_rounds=None):
    """`max_review_rounds`（省略可）: 「最大」プロファイルの巡数（system_settings の 1 項目・
    既定 7）をテスト用に短くする。標準／深くは深さ側で固定のため渡さない。"""
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=responses)
    p._sub = dict(_SUB)
    if max_review_rounds is not None:
        p._system_settings = {"max_review_rounds": max_review_rounds}
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
        assert not any(str(e.get("id") or "").startswith("main-review") for e in events)
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
        assert "評価役" in p._synth_prompts[0] and "統括役" in p._synth_prompts[0]
        # 再実行の下調べへ不足軸が伝わる
        rerun_payload = str(bodies[2:])
        assert "前回の調査で不足していた観点" in rerun_payload
        assert "税率の適用開始日" in rerun_payload
        reviews = [e for e in events if str(e.get("id") or "").startswith("main-review")]
        assert any("調べ直します" in (e.get("detail") or "") for e in reviews)
        assert any("答えられる" in (e.get("detail") or "") for e in reviews)
    finally:
        A._post = orig


def test_deep_missing_axes_truncated_at_2000_chars_with_notice():
    """査読が返す `missing`（不足観点）が `_RERUN_MISSING_MAX_CHARS`（2,000字）を超えたら
    切り詰めて「（以下省略）」を付けたうえで再調査へ引き継ぐ（打ち切りを無言にしない契約）。"""
    long_missing = "税率の適用開始日と経過措置の詳細" * 200   # 2,000字を優に超える
    assert len(long_missing) > PB._RERUN_MISSING_MAX_CHARS
    bodies = []
    orig = _install_post(_sub_run_seq() + _sub_run_seq(), bodies)
    try:
        p = _mk([json.dumps({"sufficient": False, "missing": long_missing}, ensure_ascii=False),
                 '{"sufficient": true, "missing": ""}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        expected_missing = long_missing[:PB._RERUN_MISSING_MAX_CHARS] + "（以下省略）"
        rerun_payload = str(bodies[2:])
        assert expected_missing in rerun_payload
        assert long_missing not in rerun_payload   # 全文はそのまま渡さない（無言切断ではなく明示された2,000字）
        reviews = [e for e in events if str(e.get("id") or "").startswith("main-review")]
        assert any(expected_missing in (e.get("detail") or "") for e in reviews)
    finally:
        A._post = orig


def test_deep_missing_axes_under_new_cap_not_truncated():
    """`_RERUN_MISSING_MAX_CHARS`（2,000字）以内の `missing` は切り詰められず全文が再調査へ渡る。"""
    missing_mid = "税率の適用開始日と経過措置" * 60   # 500字は超えるが2,000字は超えない長さで確認
    assert 500 < len(missing_mid) <= PB._RERUN_MISSING_MAX_CHARS
    bodies = []
    orig = _install_post(_sub_run_seq() + _sub_run_seq(), bodies)
    try:
        p = _mk([json.dumps({"sufficient": False, "missing": missing_mid}, ensure_ascii=False),
                 '{"sufficient": true, "missing": ""}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        rerun_payload = str(bodies[2:])
        assert missing_mid in rerun_payload   # 全文がそのまま残る（上限内なので切り詰められない）
        assert "（以下省略）" not in rerun_payload   # 打ち切り注記自体は付かない（tool schema 文言の「省略」とは別）
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
        # M-2是正: dict 集約（後勝ち）だけだと同じ kind の二重記録を見逃す——正本の kind
        # （chat-sub／chat-review）がちょうど1行ずつであることも独立に確認する。
        # DEPTH-2 S5: 巡別記録（chat-round・表示用）は巡ごとに別途1行ずつ増える。
        _canon = [(a, kw) for a, kw in recorded if a[0] != "chat-round"]
        assert len(_canon) == 2
        by_kind = {a[0]: kw for a, kw in _canon}
        assert set(by_kind) == {"chat-sub", "chat-review"}
        # 初回3 POST（list_docs＋散文＋DEPTH-2 S4b 一次判断）＋再調査3 POST＝総6 calls
        # （最後の実行分だけだと3になる）
        assert by_kind["chat-sub"]["calls"] == 6
        # 査読は不足→再調査で2回発動（初回査読＋再査読）。
        assert by_kind["chat-review"]["calls"] == 2
    finally:
        A._post = orig


def test_stop_during_review_aborts_without_synthesis():
    """M3/DEPTH-2 S5: 査読ストリーム中の停止要求で清書へ進まない。停止終端（`TERMINALS` の
    "stopped"）として、追加の LLM 呼び出しをせずコードで組んだ未完了回答の `_result` を返す。"""
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
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["_terminal"] == "stopped"
        assert result["env"]["stopped_by_user"] is True
        assert "打ち切りました" in result["env"]["headline"]
        assert not any(e.get("type") == "answer_delta" for e in events)
        assert len(p._synth_prompts) == 1   # 査読1回のみ・合成は発行しない
    finally:
        A._post = orig


def test_budget_exhausted_rerun_stops_review_loop():
    """M4: 再調査が調査予算で打ち切られたら次の査読へ進まず、集まった分で清書する。"""
    # 再調査はツール呼び出しだけを guard["max_turns"]=6 回返して turns_exhausted で終わる
    rerun_exhaust = [{"choices": [{"message": {"content": "", "tool_calls": [
        {"id": f"c{i}", "function": {"name": "list_docs", "arguments": "{}"}}]}}]}
        for i in range(20)]
    orig = _install_post(_sub_run_seq() + rerun_exhaust)
    try:
        p = _mk(['{"sufficient": false, "missing": "適用範囲"}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "max"})   # max=複数巡でも予算打ち切りで1巡で止まる
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


def test_review_list_docs_evidence_and_gap_reach_synthesis_digest():
    """C RV是正2巡目: 査読自身が list_docs で得た構造的根拠と gap は、どちらも state 止まりにせず
    正規の経路で清書入力（`_synthesis_digest`）へ渡る——構造的根拠は既存の
    `structural_evidence_meta`/`combined_evidence_meta` へ合流し（Evidence Packet の正式な ev-N を
    持つ・下調べ役自身の集計とは異なる条件＝別エントリとして重複排除される）、gap は
    「調査の限界: …」として続けて渡る（`add_tool_result` の契約どおり0件の呼び出しも1件の
    構造的根拠になるため、この2回の読みはそれぞれ集計事実と gap の両方を生む）。"""
    bodies = []
    orig = _install_post(_sub_run_seq() + _sub_run_seq(), bodies)
    try:
        p = _mk([
            '{"action": "list_docs", "path_prefix": "4期"}',                      # 査読1回目の読み
            '{"action": "list_docs", "path_prefix": "zzz-nonexistent-zzz"}',       # 2回目の読み
            '{"sufficient": true, "missing": ""}',                                # 確定
            "CLOUD SYNTH ANSWER",
        ])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        synth_prompt = p._synth_prompts[-1]
        # 査読が呼んだ2条件（path_prefix=4期／zzz-nonexistent-zzz）の集計事実が清書入力に現れる
        # （下調べ役自身の無条件listing＝条件なしの集計とは別エントリとして残る）。
        assert "[list_docs]" in synth_prompt
        assert "path_prefix=4期" in synth_prompt
        assert "path_prefix=zzz-nonexistent-zzz" in synth_prompt
        # 「0件」の gap が「調査の限界: …」として清書入力に現れる。
        assert "調査の限界:" in synth_prompt
        assert "list_docs『zzz-nonexistent-zzz』: 0件" in synth_prompt
        # 構造的根拠は Evidence Packet の正式な ev-N（combined_evidence_meta 経由）も持つ——
        # 査読由来のエントリ（path_prefix=4期 の条件）が正規の evidence[] に含まれている。
        packet_prefixes = {e.get("list_meta", {}).get("prefix")
                          for e in result["env"]["data"]["evidence_packet"]["evidence"]}
        assert "4期" in packet_prefixes and "zzz-nonexistent-zzz" in packet_prefixes
    finally:
        A._post = orig


def test_sub_zero_evidence_review_list_docs_only_still_passes_gate_and_fills_sources():
    """下調べ役自身は citation も構造的根拠も0件（ripgrep_search が0件のみ）でも、査読自身の
    list_docs が成功して構造的根拠を得れば、根拠ゲートを通り（`has_structural_evidence` は
    merge 後の `structural_evidence_meta` から再計算する契約）、査読限定で一致した doc も
    `sources`（`docs` 経由）へ合流する——さもないと下調べ役の値のまま止まり、ゲートが
    誤って「evidence below threshold」を送出し、一致 doc も出典から欠落する。"""
    doc = "4期/01_標準/消費税法.md"   # fixtures/corpus/v1 に実在（verify_doc_exists を通す）

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "ripgrep_search":
            return ({"hits": []}, set(), [], [])   # 下調べ役自身は citation/構造的根拠ともに0件
        if name == "list_docs":
            return ({"count": 1, "docs": [{"rel_path": doc, "doctype": "md", "state": "ready"}]},
                    {doc}, [], [])
        return ({"error": "unsupported in test"}, set(), [], [])

    orig_run_tool = A.run_tool
    A.run_tool = fake_run_tool
    orig_post = _install_post([
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"該当しない語zzz"}'}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
    ])
    try:
        p = _mk([
            '{"action": "list_docs", "path_prefix": "4期"}',
            '{"sufficient": true, "missing": ""}',
            "CLOUD SYNTH ANSWER",
        ])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"   # 誤って evidence below threshold にならない
        assert result["env"]["data"]["citations"] == []            # 下調べ役は citation を一切持たない
        sources = result["env"].get("sources") or []
        assert any(s.get("doc_id") == doc for s in sources)        # 査読限定の一致 doc が sources に含まれる
    finally:
        A.run_tool = orig_run_tool
        A._post = orig_post


def test_review_read_around_doc_merges_into_docs_and_verified_sources():
    """査読自身が read_around で精読した本文は清書入力（`_synthesis_digest`）へ渡る一方、
    `_sufficiency_verdict` 内の `run_tool` 戻り値の docs 集合はそこで使い捨てのため、doc_id を
    `docs`/`verified` へ明示的に合流しないと、出典が「精読済み」（`sources_verified`）にならず、
    査読だけが読んだ文書が `sources` からも欠落する。"""
    bodies = []
    orig = _install_post(_sub_run_seq(), bodies)   # 下調べ役自身は list_docs で根拠ゲートを通す
    try:
        p = _mk([
            f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 3}}',
            '{"sufficient": true, "missing": ""}',
            "CLOUD SYNTH ANSWER",
        ])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        synth_prompt = p._synth_prompts[-1]
        assert "精読:" in synth_prompt and _REVIEW_DOC in synth_prompt   # 査読の精読本文が清書入力に入る
        assert _REVIEW_DOC in (result["env"].get("sources_verified") or [])   # 精読済みとして出典に反映
        sources = result["env"].get("sources") or []
        assert any(s.get("doc_id") == _REVIEW_DOC for s in sources)          # docs 合流で sources にも載る
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
    assert verdict == {"verdict": "sufficient", "sufficient": True, "missing": "", "missing_codes": [], "findings": None}
    assert len(nodes) == 1
    assert nodes[0]["detail"] == f"原文を確かめています: {_REVIEW_DOC}"
    assert len(p._synth_prompts) == 2
    # L-1是正: "消費税法" は doc_id 文字列自体のエコーでも通ってしまう（本文を読んだ証明にならない）
    # ため、本文専用の文字列（ファイル3行目）でアサートする。
    assert "ツール結果" in p._synth_prompts[1] and "税率に関する法令上の規約" in p._synth_prompts[1]
    assert "省略" not in p._synth_prompts[1]   # 上限（8,000字）未満なので打ち切り注記を付けない
    # スタブは _last_usage を更新しない＝報告不能扱い。`roles` は巡別記録（chat-round）専用の内訳。
    assert {k: v for k, v in usage.items() if k != "roles"} == {"calls": 2, "tokens": None}
    assert set(usage["roles"]) == {"orchestrator", "evaluator"}


def test_review_read_result_truncated_at_8000_chars_with_notice(monkeypatch):
    """読み直し結果（ツール結果 JSON）が `_REVIEW_READ_MAX_CHARS`（8,000字）を超えたら切り詰めて
    「（以降 N 字省略）」を付ける（打ち切りを無言にしない契約）。"""
    big_result = {"doc_id": _REVIEW_DOC, "text": "あ" * 9000}
    monkeypatch.setattr(A, "run_tool", lambda *a, **kw: (big_result, set(), [], []))
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=[
        f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 1}}',
        '{"sufficient": true, "missing": ""}'])
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None)
    assert verdict == {"verdict": "sufficient", "sufficient": True, "missing": "", "missing_codes": [], "findings": None}
    full_result_text = json.dumps(big_result, ensure_ascii=False)
    omitted = len(full_result_text) - PB._REVIEW_READ_MAX_CHARS
    expected_tail = full_result_text[:PB._REVIEW_READ_MAX_CHARS] + f"（以降 {omitted} 字省略）"
    assert p._synth_prompts[1].endswith(expected_tail)


def test_review_read_cap_forces_verdict():
    """(b) read 系が上限（4回）を超えたら、次の1回で判定確定を強制する（読み過ぎない）。"""
    responses = [f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": {i}}}'
                for i in range(1, 5)]                                    # 上限どおり4回
    responses.append(f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 9}}')  # 5回目（超過）
    responses.append('{"sufficient": false, "missing": "適用範囲"}')     # 強制された確定
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=responses)
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None)
    assert verdict == {"verdict": "insufficient", "sufficient": False, "missing": "適用範囲",
                       "missing_codes": [], "findings": None}
    assert len(nodes) == 4                       # 実際にツールを呼んだのは上限の4回だけ
    assert len(p._synth_prompts) == 6            # 4読み取り＋超過要求1回＋強制確定1回
    assert "これ以上は読み取れません" in p._synth_prompts[5]
    assert {k: v for k, v in usage.items() if k != "roles"} == {"calls": 6, "tokens": None}


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
    assert {k: v for k, v in usage.items() if k != "roles"} == {"calls": 1, "tokens": None}


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


# ---- DEPTH-2 S1（主張構造での部分回答・docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.5） ----

def test_claims_synthesis_preserves_partial_answer_after_reruns_exhausted():
    """再調査を尽くしてもなお不足のとき、確定/不明が混在する主張構造が得られれば、固定文言では
    なく通常の清書（構造から書く）へ進む——headline は清書結果のまま・envelope に区分と
    理由コードが残る。"""
    bodies = []
    orig = _install_post(_sub_run_seq() + _sub_run_seq() + _sub_run_seq(), bodies)
    try:
        claims_json = json.dumps({"claims": [
            {"id": "c1", "status": "confirmed", "text": "標準税率は10%です。",
             "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""},
            {"id": "c2", "status": "unknown", "text": "軽減税率の適用開始日は資料からは確認できません。",
             "evidence_refs": [], "reason": "", "reason_code": "not_found_in_scope"},
        ]}, ensure_ascii=False)
        p = _mk([
            '{"sufficient": false, "missing": "適用範囲その1"}',
            '{"sufficient": false, "missing": "適用範囲その2"}',
            '{"sufficient": false, "missing": "適用範囲その3"}',
            claims_json,
            "PARTIAL ANSWER WITH CONFIRMED AND UNKNOWN CLAIMS",
        ], max_review_rounds=3)
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "max"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "PARTIAL ANSWER WITH CONFIRMED AND UNKNOWN CLAIMS"
        assert "十分な根拠を確認できませんでした" not in result["env"]["headline"]
        claims = result["env"]["data"]["claims"]
        assert {c["status"] for c in claims} == {"confirmed", "unknown"}
        unknown = next(c for c in claims if c["status"] == "unknown")
        assert unknown["reason_code"] == "not_found_in_scope"
        # 清書プロンプトにも主張構造が渡る（構造から書く・§2.5）。
        assert "主張の構造" in p._synth_prompts[-1]
        reviews = [e for e in events if str(e.get("id") or "").startswith("main-review")]
        assert any("部分回答を構成" in (e.get("label") or "") for e in reviews)
        assert any("答えられる部分だけを構造化" in (e.get("detail") or "") for e in reviews)
    finally:
        A._post = orig


def test_claims_confirmed_referencing_only_unmappable_evidence_is_honest_failure():
    """RV是正2巡目 指摘(1): confirmed の evidence_refs が `state.evidence` 内では実在する ID
    （`set_claims` を通る）でも、それが read（原本精読・Evidence Packet に写像先が無い種別）
    しか指していなければ、`resolve_claim_evidence_ids` 変換後に evidence_refs=[] になる——
    このまま清書・共有へ渡ると「確定（根拠参照なし）」になるため、claims 全体を不正として
    honest failure（`_MainReviewInsufficient`）へ戻す（inferred/unknown は対象外・別テストで確認）。

    再現: 査読自身の read_around（round1）が state に kind="read" の証拠を1件だけ足す
    （ev-2・sub 側 list_docs の ev-1 は構造的根拠なので写像できるが参照しない）。主張構造の
    生成が ev-2 だけを参照する confirmed を返す——`set_claims` は ev-2 が state 内に実在するため
    受理するが、Evidence Packet への変換で refs が空になる。"""
    bodies = []
    orig = _install_post(_sub_run_seq() + _sub_run_seq(), bodies)
    try:
        claims_json = json.dumps({"claims": [
            {"id": "c1", "status": "confirmed", "text": "精読した本文にある記述です。",
             "evidence_refs": ["ev-2"], "reason": "", "reason_code": ""},
        ]}, ensure_ascii=False)
        p = _mk([
            f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 3}}',
            '{"sufficient": false, "missing": "適用範囲その1"}',
            '{"sufficient": false, "missing": "適用範囲その2"}',
            claims_json,
        ])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        with pytest.raises(PB._MainReviewInsufficient):
            list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
    finally:
        A._post = orig


def test_claims_synthesis_call_counted_in_chat_review_metering():
    """主張構造の生成呼び出しも `_sufficiency_verdict` と同じ chat-review 種別へ合算される
    （新しい usage 種別は作らない・既存の usage 計上を変えない）。"""
    from sherpa import metering
    recorded = []

    def _record(*a, **kw):
        recorded.append((a, kw))
    orig_record = metering.record
    metering.record = _record
    orig = _install_post(_sub_run_seq() + _sub_run_seq() + _sub_run_seq())
    try:
        claims_json = json.dumps({"claims": [
            {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"],
             "reason": "", "reason_code": ""}]}, ensure_ascii=False)
        p = _mk([
            '{"sufficient": false, "missing": "a"}',
            '{"sufficient": false, "missing": "b"}',
            '{"sufficient": false, "missing": "c"}',
            claims_json,
            "PARTIAL ANSWER",
        ], max_review_rounds=3)
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "max"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        by_kind = {a[0]: kw for a, kw in recorded}
        assert set(by_kind) == {"chat-sub", "chat-review", "chat-round"}   # chat-round＝巡別記録（表示用）
        # 査読3回（不足×3）＋主張構造の生成1回＝4。
        assert by_kind["chat-review"]["calls"] == 4
    finally:
        A._post = orig
        metering.record = orig_record


def test_claims_synthesis_truncated_json_is_not_success():
    """途中で切れた（不正な）JSON は成功扱いにしない（`_claims_synthesis` 単体）。"""
    p = _ReviewSynth("sk-dummy", "gpt-5.5",
                     responses=['{"claims": [{"id": "c1", "status": "confirmed", "text": "t"}'])
    claims, usage = p._claims_synthesis("質問", "digest")
    assert claims is None
    assert {k: v for k, v in usage.items() if k != "roles"} == {"calls": 1, "tokens": None}


# ===== RV C6（docs/rv/2026-09-17-DEPTH-2.md）: `_claims_synthesis` の停止窓 =====

def test_claims_synthesis_skips_call_when_already_stopped():
    """停止済みなら `_stream` を1回も発行しない（単一 worker で他利用者を待たせない・
    `_sufficiency_verdict` と同じ発行前チェック）。"""
    import threading
    stop = threading.Event()
    stop.set()
    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=['{"claims": []}'])
    claims, usage = p._claims_synthesis("質問", "digest", stop_event=stop)
    assert claims is None and usage is None
    assert p._synth_prompts == []   # 発行していない


def test_claims_synthesis_aborts_mid_stream_on_stop_event():
    """chunk 受信中に停止要求が来たら、残りの chunk を待たずに打ち切る。"""
    import threading
    stop = threading.Event()

    class _StopMidStream(_ReviewSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            yield '{"claims": '
            stop.set()
            yield '[]}'   # 打ち切り後は読まれないはず

    p = _StopMidStream("sk-dummy", "gpt-5.5")
    claims, usage = p._claims_synthesis("質問", "digest", stop_event=stop)
    assert claims is None
    assert {k: v for k, v in usage.items() if k != "roles"} == {"calls": 1, "tokens": None}


def test_deep_still_insufficient_with_broken_claims_json_stays_honest_failure():
    """再調査を尽くし、主張構造の生成も不正な JSON で失敗すれば、従来どおり固定文言の
    honest failure（`_MainReviewInsufficient`）に落ちる——途中で切れた JSON を部分回答の
    採用条件として通さない。"""
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk(['{"sufficient": false, "missing": "適用範囲"}',
                 '{"sufficient": false, "missing": "適用範囲"}',
                 '{"claims": [{"id": "c1", "status": "confirmed", "text": "t"}'])   # 途中で切れた主張JSON
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        with pytest.raises(RuntimeError, match="insufficient"):
            list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
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


# ===== DEPTH-2 S4b（docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.2・§5 S4）:
# worker（下調べ役）の一次判断 =====

def test_worker_primary_judgment_not_exposed_in_standard_depth_without_claims():
    """標準（既定・evaluator の巡は 0）で下調べ役が一次判断を返さなければ、確認は行われず
    （`main-review` ノードも無し）、data["claims"] にも清書のダイジェストにも主張は出ない——
    根拠（citations／構造的根拠）は従来どおり通常に渡る。"""
    orig = _install_post(_sub_run_seq())   # 一次判断は空の `claims`
    try:
        p = _mk(["CLOUD SYNTH ANSWER"])   # standard（既定）
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert "claims" not in result["env"]["data"]
        assert len(p._synth_prompts) == 1   # 確認は発行されない＝清書1回だけ
        assert "【主張の構造（確定/推定/不明）】" not in p._synth_prompts[-1]
        assert "対象文書は34件です。" not in p._synth_prompts[-1]
    finally:
        A._post = orig


def test_worker_primary_judgment_discarded_on_review_fail_open():
    """C15/#25 是正: 査読が fail-open（応答が JSON でない等で verdict=None）で終わったターンは、
    査読の判定を一度も得ていない——`_review_ran` は真にならず、worker の一次判断は清書・公開へ
    渡らない（根拠だけで清書は通常どおり進む）。"""
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _mk(["これは JSON ではありません", "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert "claims" not in result["env"]["data"]
        assert "標準税率は10%です。" not in p._synth_prompts[-1]
    finally:
        A._post = orig


def test_worker_primary_judgment_discarded_when_rerun_claims_unreviewed_before_budget_stop():
    """C16 是正: 初回査読が不足と判定し、再調査で worker の一次判断が更新されるが、再調査自体が
    調査予算（turns_exhausted）で打ち切られて次の査読へ進まない——更新後の一次判断は一度も
    判定を通っていないため、清書・公開へ渡らない（前回の査読実績を使い回さない）。"""
    _SUB_MAX_TURNS = _SUB["guard"]["max_turns"]
    rerun_exhaust = [{"choices": [{"message": {"content": "", "tool_calls": [
        {"id": f"c{i}", "function": {"name": "list_docs", "arguments": "{}"}}]}}]}
        for i in range(_SUB_MAX_TURNS)]
    rerun_claims_json = json.dumps({"claims": [
        {"id": "c1", "status": "confirmed", "text": "標準税率は11%に改定されました。",
         "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}]}, ensure_ascii=False)
    orig = _install_post(
        _sub_run_claims_seq("標準税率は10%です。")
        + rerun_exhaust + [{"choices": [{"message": {"content": rerun_claims_json}}]}])
    try:
        p = _mk(['{"sufficient": false, "missing": "税率の適用開始日"}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert "claims" not in result["env"]["data"]
        assert "標準税率は10%です。" not in p._synth_prompts[-1]
        assert "標準税率は11%に改定されました。" not in p._synth_prompts[-1]
        assert len(p._synth_prompts) == 2   # 査読1回（不足判定）＋合成1回（再査読なし）
    finally:
        A._post = orig


def test_worker_claims_truncated_json_stays_empty_and_proceeds():
    """(2) 主張 JSON が途中で切れた/不正なら claims は空のまま、根拠（list_docs の構造的根拠）
    だけで従来どおり進む（失敗に倒れない・headline は通常どおり清書結果になる）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
        # 途中で切れた JSON（`_claims_synthesis`/`_claims_truncated` 系テストと同型の壊れ方）。
        {"choices": [{"message": {"content": '{"claims": [{"id": "c1", "status": "confirmed", "text": "t"}'}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _mk(["CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert "claims" not in result["env"]["data"]
        # 【主張の構造（確定/推定/不明）】節（`_claims_digest` 非空のときだけ付加）が出ていない
        # ことを確認する（固定の指示文自体には「主張の構造」という語が含まれるため見出し全体で見る）。
        assert "【主張の構造（確定/推定/不明）】" not in p._synth_prompts[-1]
    finally:
        A._post = orig


def test_main_review_prompt_includes_worker_primary_judgment():
    """(3) メイン査読の入力に worker の一次判断が含まれる（プロンプト文字列で確認・
    鵜呑みにせず必要箇所は自分で確認する指示も併記される）。"""
    from sherpa import investigation_state as IS
    state = IS.InvestigationState(question="TAX-RATEは?", scope={"world": "v1"})
    state.add_tool_result("ripgrep_search", {"query": "a"}, {"hits": [{"doc_id": "a.md"}]},
                          [{"doc_id": "a.md", "span": [1, 1], "quote": "A"}], None)
    assert state.set_claims(
        [{"id": "c1", "status": "confirmed", "text": "標準税率は10%です。",
          "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}], origin="worker") is True
    assert state.claims[0].origin == "worker"

    p = _ReviewSynth("sk-dummy", "gpt-5.5", responses=['{"sufficient": true, "missing": ""}'])
    verdict, nodes, usage = p._sufficiency_verdict(
        "TAX-RATEは?", "qa", "(なし)", "v1", scope_paths=[], layer=None, state=state)
    assert verdict == {"verdict": "sufficient", "sufficient": True, "missing": "", "missing_codes": [], "findings": None}
    assert "下調べ役の一次判断" in p._synth_prompts[0]
    assert "標準税率は10%です。" in p._synth_prompts[0]
    assert "鵜呑みにせず" in p._synth_prompts[0]


def test_worker_claims_call_counted_as_extra_chat_sub_call_when_review_runs(monkeypatch):
    """#26 是正: 査読が走る深さ（deep）では下調べ役の一次判断の要求も chat-sub の呼び出し回数に
    含まれる（黙って増えない）。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record", lambda *a, **kw: recorded.append((a, kw)))
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
        {"choices": [{"message": {"content": '{"claims": []}'}}]},
    ]
    orig = _install_post(seq)
    try:
        p = _mk(['{"sufficient": true, "missing": ""}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        by_kind = {a[0]: kw for a, kw in recorded}
        # list_docs（1）＋散文終了（2）＋一次判断の要求（3）＝3。
        assert by_kind["chat-sub"]["calls"] == 3
    finally:
        A._post = orig


def test_worker_claims_call_issued_in_standard_depth(monkeypatch):
    """標準（巡 0）でも下調べ役は一次判断を返す（orchestrator の確認 1 回を通して清書へ渡る
    ため）＝chat-sub は list_docs＋散文終了＋一次判断の要求の3回になる。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record", lambda *a, **kw: recorded.append((a, kw)))
    orig = _install_post(_sub_run_seq())
    try:
        p = _mk(["CLOUD SYNTH ANSWER"])
        ctx = _ctx()   # 既定＝standard
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        by_kind = {a[0]: kw for a, kw in recorded}
        assert by_kind["chat-sub"]["calls"] == 3
    finally:
        A._post = orig


# ===== S4b 敵対 RV 1 巡目 是正（docs/rv/2026-09-17-DEPTH-2.md C13/C14） =====

def _sub_run_claims_seq(claim_text: str):
    """`_sub_run_seq()` と同型だが一次判断の要求に list_docs 由来の構造的根拠（ev-1）を参照する
    有効な confirmed 主張を返す（C13/C14 是正テスト用）。"""
    return _sub_run_claims_seq_ids([("c1", claim_text)])


def _sub_run_claims_seq_ids(id_text_pairs: list[tuple[str, str]]):
    """`_sub_run_claims_seq` の複数主張版——1回の一次判断要求が複数 id の confirmed を返す
    （#24 是正テスト用: 同じ id は置換・別 id は追記の検証に使う）。"""
    claims_json = json.dumps({"claims": [
        {"id": cid, "status": "confirmed", "text": text,
         "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}
        for cid, text in id_text_pairs]}, ensure_ascii=False)
    return [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
        {"choices": [{"message": {"content": claims_json}}]},
    ]


def test_worker_primary_judgment_replaces_same_id_and_appends_new_axis():
    """#24 是正: 再調査（deep=1回・査読を通す）で worker が返す一次判断のうち、初回分と**同じ
    元 id**（"c1"）は再調査分の内容へ置換し（言い直しを新旧そろって confirmed で残さない）、
    初回にしか無い別軸（"c2"）はそのまま維持したうえで再調査分を追記する。"""
    bodies = []
    orig = _install_post(
        _sub_run_claims_seq_ids([("c1", "標準税率は10%です。"), ("c2", "軽減税率は8%です。")])
        + _sub_run_claims_seq_ids([("c1", "標準税率は11%に改定されました。")]), bodies)
    try:
        p = _mk([
            '{"sufficient": false, "missing": "税率の適用開始日"}',
            '{"sufficient": true, "missing": ""}',
            "CLOUD SYNTH ANSWER",
        ])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        claims = result["env"]["data"]["claims"]
        assert len(claims) == 2   # c1（置換後）＋c2（別軸・維持）
        ids = [c["id"] for c in claims]
        assert len(ids) == len(set(ids))   # id が衝突しない
        texts = {c["text"] for c in claims}
        # 同じ id（c1）の言い直しは最新（11%）だけが残り、初回分（10%）は残らない。
        assert texts == {"標準税率は11%に改定されました。", "軽減税率は8%です。"}
        assert all(c["evidence_refs"] for c in claims)
        assert "標準税率は11%に改定されました。" in p._synth_prompts[-1]
        assert "軽減税率は8%です。" in p._synth_prompts[-1]
        assert "標準税率は10%です。" not in p._synth_prompts[-1]
    finally:
        A._post = orig


def test_worker_primary_judgment_kept_when_reinvestigation_claims_invalid():
    """RV #18 是正: 初回は有効な worker 一次判断（10%）を返すが、再調査では途中で切れた不正な
    JSON を返す——今回（不正）の回は取り込まず、初回分（10%）をそのまま維持して清書へ渡る
    （旧い一次判断を居残らせないためと言って、不正な回のせいで有効な既存分まで消さない）。"""
    bodies = []
    seq = _sub_run_claims_seq("標準税率は10%です。") + [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
        {"choices": [{"message": {"content": '{"claims": [{"id": "c1", "status": "confirmed", "text": "t"}'}}]},
    ]
    orig = _install_post(seq, bodies)
    try:
        p = _mk([
            '{"sufficient": false, "missing": "税率の適用開始日"}',
            '{"sufficient": true, "missing": ""}',
            "CLOUD SYNTH ANSWER",
        ])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        claims = result["env"]["data"]["claims"]
        assert len(claims) == 1
        assert claims[0]["text"] == "標準税率は10%です。"
        assert "標準税率は10%です。" in p._synth_prompts[-1]
    finally:
        A._post = orig


def test_worker_confirmed_referencing_only_unmappable_evidence_is_dropped_not_honest_failure():
    """C14 是正: worker（下調べ役）が read_around 精読だけを参照する confirmed を返すと、
    Evidence Packet への変換後に evidence_refs=[] になるのは synthesis 由来のケース
    （`test_claims_confirmed_referencing_only_unmappable_evidence_is_honest_failure`）と同じだが、
    worker 由来は「入力側」なので honest failure に倒さず、主張構造ごと不採用（claims 空）にして
    根拠（読み取った本文そのもの）だけで清書まで進む。"""
    claims_json = json.dumps({"claims": [
        {"id": "c1", "status": "confirmed", "text": "精読した本文にある記述です。",
         "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}]}, ensure_ascii=False)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read_around",
             "arguments": json.dumps({"doc_id": _REVIEW_DOC, "line": 3})}}]}}]},
        {"choices": [{"message": {"content": "LOCAL DRAFT (discarded)"}}]},
        {"choices": [{"message": {"content": claims_json}}]},
    ]
    orig = _install_post(seq)
    try:
        # standard（既定）＝確認1回（十分と判定）を通った一次判断でも、Evidence Packet へ
        # 写像できない根拠だけを参照する confirmed は不採用になる。
        p = _mk(['{"verdict": "sufficient", "missing": "", "findings": []}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert "claims" not in result["env"]["data"]
        assert "【主張の構造（確定/推定/不明）】" not in p._synth_prompts[-1]
    finally:
        A._post = orig


# ===== S4b 敵対 RV 4 巡目 是正（docs/rv/2026-09-17-DEPTH-2.md C18） =====

def test_reinvestigation_claims_prompt_includes_existing_worker_claims_and_replaces_same_id():
    """C18 是正: 再調査（2回目以降）の一次判断要求プロンプトに、直前までの worker 由来の
    主張（id・本文のみ）が渡る——渡さないと再調査の worker は毎回 "c1" から採番し直し、
    置換処理（`_ingest_sub_final_into_state`）が別論点の既存主張まで消しうる。初回は c1（本題）
    のみ、再調査は c1（更新）＋c2（新論点）を返すと、最終 `state.claims` は c1（更新後の内容）
    と c2 の2件（新旧の言い直しが両方残らない）。"""
    bodies = []
    orig = _install_post(
        _sub_run_claims_seq_ids([("c1", "標準税率は10%です。")])
        + _sub_run_claims_seq_ids([("c1", "標準税率は11%に改定されました。"),
                                   ("c2", "軽減税率は8%です。")]), bodies)
    try:
        p = _mk([
            '{"sufficient": false, "missing": "軽減税率の扱い"}',
            '{"sufficient": true, "missing": ""}',
            "CLOUD SYNTH ANSWER",
        ])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        claims = result["env"]["data"]["claims"]
        assert len(claims) == 2
        by_id = {c["id"]: c["text"] for c in claims}
        assert by_id == {"c1": "標準税率は11%に改定されました。", "c2": "軽減税率は8%です。"}
        # 再調査の一次判断要求（この2回目の sub run の3件目の _post）に直前の主張（c1・10%）が渡る。
        rerun_claims_prompt = bodies[-1]["messages"][1]["content"]
        assert "[c1] confirmed: 標準税率は10%です。" in rerun_claims_prompt
        # 初回の一次判断要求（既存主張が無い）には「前回までの主張」節が付かない。
        first_claims_prompt = bodies[2]["messages"][1]["content"]
        assert "前回までの主張" not in first_claims_prompt
    finally:
        A._post = orig


# ===== DEPTH-2 S5: 巡ループ（深さ＝evaluator の巡数・§2.4）=====

def _round_ids(events) -> list:
    return sorted({e["id"] for e in events
                   if e.get("type") == "node" and str(e.get("id") or "").startswith("main-review-r")})


def test_deep_runs_two_evaluator_rounds_with_round_scoped_node_ids():
    """受け入れ条件(2)(7): 深く＝2巡。「1巡目不足→2巡目で終了」で evaluator が2回走り、
    思考ノードの id が巡ごとに分かれる（再読込後も巡が区別できる）。"""
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk(['{"verdict": "insufficient", "missing": "税率の適用開始日", "findings": []}',
                 '{"verdict": "sufficient", "missing": "", "findings": []}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert len(p._synth_prompts) == 3         # 査読2回＋清書1回
        assert _round_ids(events) == ["main-review-r1", "main-review-r2"]
        assert "2巡中の1巡目" in p._synth_prompts[0]
        assert "2巡中の2巡目" in p._synth_prompts[1]
        # 巡ごとに worker の agent_run_id も分かれる（前巡の実行履歴が上書きで消えない）。
        assert {e.get("agent_run_id") for e in events if e.get("agent_run_id")} == {
            "sub:worker:1", "sub:worker:2"}
    finally:
        A._post = orig


def test_undecidable_verdict_ends_rounds_without_further_worker_or_evaluator():
    """受け入れ条件(2): 判定不能は不足に丸めず、その巡で終える（以後の worker/evaluator を
    呼ばない）。清書は通常どおり1回行う。"""
    orig = _install_post(_sub_run_seq())   # 下調べは初回の1周分しか用意しない
    try:
        p = _mk(['{"verdict": "undecidable", "missing": "", "findings": []}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert len(p._synth_prompts) == 2          # 査読1回＋清書1回（2巡目は走らない）
        assert _round_ids(events) == ["main-review-r1"]
        assert any("判断できません" in (e.get("detail") or "") for e in events)
    finally:
        A._post = orig


def test_sufficient_first_round_skips_remaining_rounds():
    """受け入れ条件(2): 十分と判定されたら以後の worker/evaluator を呼ばない。"""
    orig = _install_post(_sub_run_seq())
    try:
        p = _mk(['{"verdict": "sufficient", "missing": "", "findings": []}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert len(p._synth_prompts) == 2
        assert _round_ids(events) == ["main-review-r1"]
    finally:
        A._post = orig


def test_max_profile_round_count_follows_max_review_rounds_setting():
    """受け入れ条件(3): 「最大」の巡数は設定（`max_review_rounds`）で変わる。
    1 に絞れば 1 巡で終わり、2 にすれば 2 巡走る（同じ応答列・同じ深さで比較する）。"""
    for rounds, expected_reviews in ((1, 1), (2, 2)):
        orig = _install_post(_sub_run_seq() * (rounds + 1))
        try:
            claims_json = json.dumps({"claims": [
                {"id": "c1", "status": "unknown", "text": "確認できません。", "evidence_refs": [],
                 "reason": "", "reason_code": "not_found_in_scope"}]}, ensure_ascii=False)
            p = _mk(['{"verdict": "insufficient", "missing": "軸", "findings": []}'] * rounds
                    + [claims_json, "PARTIAL"], max_review_rounds=rounds)
            ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                                   "depth_profile": "max"})
            events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
            assert next(e for e in events if e.get("type") == "_result")["env"]["headline"] == "PARTIAL"
            # 査読 `expected_reviews` 回＋主張構造1回＋清書1回
            assert len(p._synth_prompts) == expected_reviews + 2
        finally:
            A._post = orig


def test_no_intermediate_body_is_streamed_or_returned():
    """受け入れ条件(4): 中間巡の本文は SSE に流さない——`answer_delta` は最終清書のぶんだけで、
    下調べ役のローカル散文も査読の JSON も一切混ざらない。"""
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk(['{"verdict": "insufficient", "missing": "軸", "findings": []}',
                 '{"verdict": "sufficient", "missing": "", "findings": []}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
        assert "".join(deltas) == "CLOUD SYNTH ANSWER"
        blob = json.dumps(events, ensure_ascii=False, default=str)
        assert "LOCAL DRAFT (discarded)" not in blob
        # 査読応答の生 JSON（中間巡の出力そのもの）がイベント列のどこにも出ない。
        assert '"verdict": "insufficient"' not in blob and '"findings"' not in blob
    finally:
        A._post = orig


def test_stop_after_final_round_does_not_return_refuted_claims():
    """受け入れ条件(5): 最終巡直後に停止しても、反証された主張（採用不可）は未完了回答に
    含まれない。追加の LLM 呼び出しもしない。"""
    import threading
    stop = threading.Event()

    class _StopOnSecondReview(_ReviewSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "stop"
            if len(self._synth_prompts) == 2:
                stop.set()   # 2巡目（最終巡）の査読応答中に停止要求が来る
            yield self._responses.pop(0)

    orig = _install_post(_sub_run_claims_seq_ids([("c1", "標準税率は10%です。")]) + _sub_run_seq())
    try:
        p = _StopOnSecondReview("sk-dummy", "gpt-5.5", responses=[
            json.dumps({"verdict": "insufficient", "missing": "適用開始日",
                        "findings": [{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                                      "refutes": True}]}, ensure_ascii=False),
            '{"verdict": "sufficient", "missing": "", "findings": []}',
            "SHOULD NOT SYNTH"])
        p._sub = dict(_SUB)
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"}, stop_event=stop)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["_terminal"] == "stopped"
        assert "標準税率は10%です。" not in result["env"]["headline"]   # 反証済み＝採用不可
        assert "2巡目で打ち切りました" in result["env"]["headline"]
        assert len(p._synth_prompts) == 2   # 査読2回だけ＝未完了回答の生成に LLM を使わない
        assert not any(e.get("type") == "answer_delta" for e in events)
    finally:
        A._post = orig


def test_chat_round_recorded_per_round_and_canonical_usage_once(monkeypatch):
    """受け入れ条件(6): 巡ごとに `chat-round` が1行ずつ記録され（表示用）、正本の使用量
    （chat-sub／chat-review）は1行ずつのまま二重に増えない。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record", lambda *a, **kw: recorded.append((a, kw)))
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk(['{"verdict": "insufficient", "missing": "税率の適用開始日", "findings": []}',
                 '{"verdict": "sufficient", "missing": "", "findings": []}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        rounds = [kw for a, kw in recorded if a[0] == "chat-round"]
        assert [kw["meta"]["round"] for kw in rounds] == [1, 2]
        assert [kw["meta"]["verdict"] for kw in rounds] == ["insufficient", "sufficient"]
        # 不足軸の自由文は meta に載せない（資料名・本文相当の語を台帳に残さない）。
        assert "missing" not in rounds[0]["meta"]
        assert rounds[1]["meta"]["stop"] == "sufficient"
        assert all("worker" in kw["meta"]["roles"] for kw in rounds)
        assert all(kw["calls"] == 1 for kw in rounds)
        # 正本は kind ごとに1行のまま（巡別記録で二重に足さない）。
        assert [a[0] for a, kw in recorded].count("chat-sub") == 1
        assert [a[0] for a, kw in recorded].count("chat-review") == 1
    finally:
        A._post = orig


def test_limits_delta_reports_only_the_increment_within_a_round():
    """受け入れ条件(6): 巡別記録の `limits` は巡内の増分だけ（累積スナップショットを足さない）。"""
    before = {"tool_result_clipped": 2, "total_budget_hit": False, "context_compactions": 0}
    after = {"tool_result_clipped": 5, "total_budget_hit": True, "context_compactions": 0}
    assert PB._limits_delta(before, after) == {"tool_result_clipped": 3, "total_budget_hit": True}
    assert PB._limits_delta(after, after) == {}   # 変化なし＝空（既定のままの項目は出さない）


def test_incomplete_headline_excludes_unknown_and_names_the_round():
    """停止・失敗の未完了回答は採用可（確定/推定）の主張だけを並べ、打ち切りの巡と理由を明示する。"""
    from sherpa import investigation_state as IS
    claims = [IS.Claim(id="c1", status="confirmed", text="確定した内容", evidence_refs=["ev-1"]),
              IS.Claim(id="c2", status="inferred", text="推定の内容", reason="根拠が薄い"),
              IS.Claim(id="c3", status="unknown", text="反証された内容", reason_code="conflict")]
    out = PB._incomplete_headline(claims, 3, "user_stop")
    assert "3巡目で打ち切りました（利用者の操作で停止したため）" in out
    assert "確定した内容" in out and "推定の内容（推定）" in out
    assert "反証された内容" not in out
    assert "確定できた内容はありません" in PB._incomplete_headline([], 1, "budget")


# ===== DEPTH-2 S5 是正（巡ループの終端・反証の保持・下調べ役なしの巡）=====

def test_final_round_synthesis_does_not_readopt_refuted_claim():
    """最終巡で反証された主張は、指摘を渡さない主張構造の生成（`_claims_synthesis`）が同じ id を
    確定として返し直しても採用不可（不明・conflict）のまま——清書にも公開 envelope にも確定として
    出ない。"""
    claims_json = json.dumps({"claims": [
        {"id": "c1", "status": "confirmed", "text": "標準税率は10%です。",
         "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}]}, ensure_ascii=False)
    orig = _install_post(_sub_run_claims_seq_ids([("c1", "標準税率は10%です。")]) + _sub_run_seq())
    try:
        p = _mk([json.dumps({"verdict": "insufficient", "missing": "適用開始日",
                             "findings": [{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                                           "refutes": True}]}, ensure_ascii=False),
                 '{"verdict": "insufficient", "missing": "適用開始日", "findings": []}',
                 claims_json, "PARTIAL"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        claims = {c["id"]: c for c in result["env"]["data"]["claims"]}
        assert claims["c1"]["status"] == "unknown" and claims["c1"]["reason_code"] == "conflict"
    finally:
        A._post = orig


def test_stopped_terminal_excludes_unreviewed_worker_claims():
    """停止終端の未完了回答は「査読を通した採用可の主張」だけから組む——査読が fail-open
    （verdict=None＝未査読）のまま停止したターンでは worker の一次判断を確定として出さない。"""
    import threading
    stop = threading.Event()

    class _StopOnReview(_ReviewSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "stop"
            stop.set()
            yield self._responses.pop(0)

    orig = _install_post(_sub_run_claims_seq_ids([("c1", "標準税率は10%です。")]))
    try:
        p = _StopOnReview("sk-dummy", "gpt-5.5",
                          responses=["NOT JSON AT ALL", "SHOULD NOT SYNTH"])
        p._sub = dict(_SUB)
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"}, stop_event=stop)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["_terminal"] == "stopped"
        assert "標準税率は10%です。" not in result["env"]["headline"]
        assert "確定できた内容はありません" in result["env"]["headline"]
        assert len(p._synth_prompts) == 1   # 未完了回答の生成に LLM を使わない
    finally:
        A._post = orig


def test_stopped_terminal_precedes_review_nodes(monkeypatch):
    """査読中に停止したら、停止終端（`_result`）を読取ノードより先に返す——consumer は停止後の
    最初のイベントで打ち切るため、ノードが先だと未完了回答が保存されない。"""
    import threading
    stop = threading.Event()
    orig_run_tool = A.run_tool

    def _run_tool_then_stop(name, *a, **kw):
        result = orig_run_tool(name, *a, **kw)
        if name == "read_around":   # 査読自身の精読の直後に停止要求が来た状況を模す
            stop.set()
        return result

    monkeypatch.setattr(A, "run_tool", _run_tool_then_stop)
    orig = _install_post(_sub_run_seq())
    try:
        p = _mk([f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 1}}',
                 '{"verdict": "sufficient", "missing": "", "findings": []}'])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"}, stop_event=stop)
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result_at = next(i for i, e in enumerate(events) if e.get("type") == "_result")
        assert events[result_at]["env"]["_terminal"] == "stopped"
        # 停止終端より前に査読の読取ノードを出さない（出すと consumer がそこで打ち切る）。
        assert not [e for e in events[:result_at]
                    if e.get("type") == "node" and (e.get("id") or "").startswith("main-review")]
    finally:
        A._post = orig


def test_standard_depth_stop_does_not_save_incomplete_answer():
    """標準（0 巡）は巡ループを回さない＝停止しても未完了回答（`_terminal="stopped"`）を
    返さない（従来どおり assistant 未保存のまま終える）。"""
    import threading
    stop = threading.Event()

    class _StopOnSynth(_ReviewSynth):
        def _stream(self, prompt, completion=None):
            self._synth_prompts.append(prompt)
            stop.set()
            if completion is not None:
                completion.terminal_seen = True
                completion.reason = "stop"
            yield ""

    orig = _install_post(_sub_run_seq())
    try:
        p = _StopOnSynth("sk-dummy", "gpt-5.5", responses=[""])
        p._sub = dict(_SUB)
        ctx = _ctx(stop_event=stop)   # standard（既定）
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        assert not [e for e in events if e.get("type") == "_result"]
    finally:
        A._post = orig


def test_unreadable_findings_keep_the_round_unreviewed():
    """指摘の形が不正で反証を取り込めなかった巡は「未査読」として扱う——判定は得ていても
    worker の一次判断を確定として公開へ渡さない。"""
    orig = _install_post(_sub_run_claims_seq_ids([("c1", "標準税率は10%です。")]))
    try:
        p = _mk(['{"verdict": "sufficient", "missing": "", "findings": [{"claim_id": "c1"}]}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert "claims" not in result["env"]["data"]
        assert "標準税率は10%です。" not in p._synth_prompts[-1]
    finally:
        A._post = orig


def test_failed_terminal_keeps_confirmed_claims_and_round(monkeypatch):
    """失敗終端でも、巡ループで確認済みの主張と巡番号を調査状態から組み直して返す
    （追加の LLM 呼び出しはしない・固定の設定確認文だけにしない）。"""
    orig = _install_post(_sub_run_claims_seq_ids([("c1", "標準税率は10%です。")]))
    try:
        p = _mk(['{"verdict": "insufficient", "missing": "適用開始日", "findings": []}'])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        _calls = {"n": 0}
        _real = p._sub_agentic_loop

        def _fail_second(*a, **kw):
            _calls["n"] += 1
            if _calls["n"] == 1:
                return _real(*a, **kw)
            raise TimeoutError("sub loop timed out")

        monkeypatch.setattr(p, "_sub_agentic_loop", _fail_second)
        events = list(p.run(ctx))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["_terminal"] == "failed"
        assert "2巡目で打ち切りました" in result["env"]["headline"]
        assert "標準税率は10%です。" in result["env"]["headline"]
    finally:
        A._post = orig


# ===== DEPTH-2 S5 敵対 RV 2 巡目 是正（docs/rv/2026-09-17-DEPTH-2.md C44〜C47・#48〜#52）=====

def _main_run_seq(answer: str = "MAIN ANSWER"):
    """下調べ役なし（`self._sub is None`）の1ターン分の `_post` 応答（list_docs → 回答）。"""
    return [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "list_docs", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": answer}}]},
    ]


def test_claims_synthesis_prompt_carries_existing_ids_and_open_refutations():
    """C44: 最終巡の主張構造の生成へ、既存の主張（id・状態・本文）と未解決の反証を渡す——
    渡さないと id が振り直され、ID 基準の反証の再適用が別論点を不明化して反証対象を再採用する。"""
    claims_json = json.dumps({"claims": [
        {"id": "c1", "status": "confirmed", "text": "標準税率は10%です。",
         "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}]}, ensure_ascii=False)
    orig = _install_post(_sub_run_claims_seq_ids([("c1", "標準税率は10%です。")]) + _sub_run_seq())
    try:
        p = _mk([json.dumps({"verdict": "insufficient", "missing": "適用開始日",
                             "findings": [{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                                           "refutes": True}]}, ensure_ascii=False),
                 '{"verdict": "insufficient", "missing": "適用開始日", "findings": []}',
                 claims_json, "PARTIAL"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        claims_prompt = p._synth_prompts[2]
        assert "【前回までの主張" in claims_prompt and "[c1]" in claims_prompt
        assert "別資料と矛盾" in claims_prompt and "（反証）" in claims_prompt
    finally:
        A._post = orig


def test_review_prompt_carries_finding_ids_and_reuse_rule():
    """C45: 次巡の査読へ既存指摘の id を渡し、同じ指摘は id 維持・別指摘は新規採番を要求する
    ——id の対応が無いと、査読が別の指摘へ同じ id を返したときに旧い反証が消える。"""
    orig = _install_post(_sub_run_claims_seq_ids([("c1", "標準税率は10%です。")]) + _sub_run_seq())
    try:
        p = _mk([json.dumps({"verdict": "insufficient", "missing": "適用開始日",
                             "findings": [{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                                           "refutes": True}]}, ensure_ascii=False),
                 '{"verdict": "sufficient", "missing": "", "findings": []}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        second_review = p._synth_prompts[1]
        assert "(f1)" in second_review
        assert "同じ id をそのまま使い" in second_review
    finally:
        A._post = orig


def test_failed_terminal_drops_confirmed_claims_that_lost_evidence_refs():
    """C47/#48: 失敗終端の未完了本文にも根拠ゲートを適用する——ID 変換で evidence_refs を
    失った confirmed を「ここまでに確認できた範囲」として保存・公開しない。"""
    claims_json = json.dumps({"claims": [
        {"id": "c1", "status": "confirmed", "text": "精読した本文にある記述です。",
         "evidence_refs": ["ev-2"], "reason": "", "reason_code": ""}]}, ensure_ascii=False)
    orig = _install_post(_sub_run_seq() + _sub_run_seq())
    try:
        p = _mk([f'{{"action": "read_around", "doc_id": "{_REVIEW_DOC}", "line": 3}}',
                 '{"verdict": "insufficient", "missing": "適用範囲その1", "findings": []}',
                 '{"verdict": "insufficient", "missing": "適用範囲その2", "findings": []}',
                 claims_json])
        ctx = _ctx(scope_meta={"world": "v1", "scope_paths": [], "source": "all",
                               "depth_profile": "deep"})
        events = list(p.run(ctx))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["_terminal"] == "failed"
        assert "精読した本文にある記述です。" not in result["env"]["headline"]
        assert "十分な根拠を確認できませんでした" in result["env"]["headline"]
    finally:
        A._post = orig


# ===== DEPTH-2 S5b（§2.2）: 標準（巡 0）でも orchestrator の確認を 1 回行う =====

def test_standard_depth_confirmed_claims_reach_synthesis_and_envelope(monkeypatch):
    """標準・下調べ役ありでは、worker の一次判断を orchestrator が 1 回だけ確認し、確認を
    通した主張が清書プロンプト（主張の構造）と envelope の data["claims"] に入る。確認は
    強いモデルの追加呼び出し1回＝chat-review に、一次判断の要求は chat-sub に計上される。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record", lambda *a, **kw: recorded.append((a, kw)))
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _mk(['{"verdict": "sufficient", "missing": "", "findings": []}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx()   # 既定＝standard（巡 0）
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert [c["text"] for c in result["env"]["data"]["claims"]] == ["標準税率は10%です。"]
        assert len(p._synth_prompts) == 2   # 確認1回＋清書1回（再調査には入らない）
        assert "標準税率は10%です。" in p._synth_prompts[-1]
        by_kind = {a[0]: kw for a, kw in recorded}
        assert by_kind["chat-review"]["calls"] == 1
        assert by_kind["chat-sub"]["calls"] == 3
        rounds = [kw for a, kw in recorded if a[0] == "chat-round"]
        assert len(rounds) == 1 and rounds[0]["meta"]["round"] == 0
        assert rounds[0]["meta"]["verdict"] == "sufficient"
    finally:
        A._post = orig


def test_standard_depth_insufficient_confirmation_does_not_reinvestigate():
    """標準の確認は「確認」だけ＝不足と判定しても再調査（下調べ役の再実行）には入らず、
    確認を通った一次判断のまま通常どおり清書へ進む。"""
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _mk(['{"verdict": "insufficient", "missing": "適用開始日", "findings": []}',
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert [c["text"] for c in result["env"]["data"]["claims"]] == ["標準税率は10%です。"]
        assert len(p._synth_prompts) == 2   # 確認1回＋清書1回（再査読も再調査も無い）
    finally:
        A._post = orig


def test_standard_depth_claims_discarded_on_confirmation_fail_open():
    """標準の確認が fail-open（応答が JSON でない＝verdict=None）で終わったターンは、確認を
    一度も通していない——worker の一次判断は清書・公開へ渡らない（根拠だけで清書は進む）。"""
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _mk(["これは JSON ではありません", "CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["headline"] == "CLOUD SYNTH ANSWER"
        assert "claims" not in result["env"]["data"]
        assert "標準税率は10%です。" not in p._synth_prompts[-1]
    finally:
        A._post = orig


def test_standard_depth_refuted_claim_falls_to_unknown_conflict():
    """標準の確認が反証（`refutes`）を返した主張は、同じ規律で不明（理由コード conflict）へ
    落ちる＝確定として清書・公開へ渡らない。"""
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _mk([json.dumps({"verdict": "sufficient", "missing": "",
                             "findings": [{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                                           "refutes": True}]}, ensure_ascii=False),
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        claims = result["env"]["data"]["claims"]
        assert [(c["status"], c["reason_code"]) for c in claims] == [("unknown", "conflict")]
    finally:
        A._post = orig


def test_standard_confirm_apply_findings_failure_records_review_failed(monkeypatch):
    """C61 是正: 標準の確認で判定（verdict）は得られても `findings` の形が不正で
    `apply_findings` が False を返した回は、chat-round の stop を実際の状態（未査読）に
    合わせて `review_failed` として記録する——`sufficient` のまま記録すると、一次判断を
    確認済みであるかのように見せてしまう（巡ループ側は `apply_findings` を経ない
    `verdict is None` の回だけを `review_failed` にする規律とは別に、標準の確認は
    `apply_findings` の成否そのものが「確認できたか」を意味するため区別する）。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record", lambda *a, **kw: recorded.append((a, kw)))
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _mk([json.dumps({"verdict": "sufficient", "missing": "", "findings": "bad-shape"},
                            ensure_ascii=False),
                 "CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        result = next(e for e in events if e.get("type") == "_result")
        assert "claims" not in result["env"]["data"]   # 未査読のまま公開しない（既存規律）
        rounds = [kw for a, kw in recorded if a[0] == "chat-round"]
        assert len(rounds) == 1
        assert rounds[0]["meta"]["stop"] == "review_failed"
    finally:
        A._post = orig


def test_standard_confirm_usage_role_is_orchestrator_not_evaluator(monkeypatch):
    """C62 是正: 標準の確認（判定のみ・再調査なし）は evaluator の巡という語彙が成立しない
    ため、chat-round の役割別内訳を orchestrator に集約して記録する（正本の chat-review
    合計は変わらない・表示用の内訳だけの区別）。"""
    from sherpa import metering
    recorded = []
    monkeypatch.setattr(metering, "record", lambda *a, **kw: recorded.append((a, kw)))
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _mk(['{"verdict": "sufficient", "missing": "", "findings": []}', "CLOUD SYNTH ANSWER"])
        ctx = _ctx()
        events = list(p._agentic_run(ctx, {"lens": "qa", "input": ctx.message, "reason": "test"}))
        list(events)
        by_kind = {a[0]: kw for a, kw in recorded if a[0] == "chat-review"}
        assert by_kind["chat-review"]["calls"] == 1
        rounds = [kw for a, kw in recorded if a[0] == "chat-round"]
        assert len(rounds) == 1
        roles = rounds[0]["meta"]["roles"]
        assert roles.get("orchestrator", {}).get("calls") == 1
        assert "evaluator" not in roles
    finally:
        A._post = orig


class _FailAtSynth(_ReviewSynth):
    """`fail_at` 番目の `_stream` 呼び出しで通信失敗を模す stub（清書失敗の終端を再現する）。"""

    def __init__(self, *a, fail_at=None, **kw):
        super().__init__(*a, **kw)
        self._fail_at = fail_at

    def _stream(self, prompt, completion=None):
        n = len(self._synth_prompts)
        self._synth_prompts.append(prompt)
        if completion is not None:
            completion.terminal_seen = True
            completion.reason = "stop"
        if self._fail_at == n:
            raise RuntimeError("upstream transport error")
        yield self._responses.pop(0)


def test_standard_confirmed_synthesis_failure_omits_round_loop_wording():
    """#64 是正: 標準（巡0）で確認が通っても、直後の清書失敗の未完了本文は巡ループの
    「N巡目で打ち切りました」という語彙を使わない（標準に巡という概念は無いため）——
    `_incomplete_terminal_body` を `_review_rounds > 0` のときだけにする（停止終端が
    既に持つ同じガードと揃える）。固定文言だけが本文になる。"""
    orig = _install_post(_sub_run_claims_seq("標準税率は10%です。"))
    try:
        p = _FailAtSynth(
            "sk-dummy", "gpt-5.5",
            responses=['{"verdict": "sufficient", "missing": "", "findings": []}'],
            fail_at=1)
        p._sub = dict(_SUB)
        ctx = _ctx()
        events = list(p.run(ctx))
        result = next(e for e in events if e.get("type") == "_result")
        assert result["env"]["_terminal"] == "failed"
        assert "巡目で打ち切りました" not in result["env"]["headline"]
        assert "下調べAIでの調査がうまくいきませんでした" in result["env"]["headline"]
    finally:
        A._post = orig
