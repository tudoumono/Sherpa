"""ツール並列実行（D1・同一応答内の独立したツール呼び出しの同時実行）の単体テスト。

`run_tool` を monkeypatch し、LLM 応答は `_post`（openai_style/gemini）／`client.messages.create`
（anthropic_style）を固定シーケンスで差し替える。cites/cards/docs は空のまま返す——本ファイルの
主眼は並列実行の機構（壁時計・呼び出し順対応・停止契約・例外分離）そのものであり、Committed
Evidence 化ゲート／帰属呼び出し（citation が空なら発動しない・`attribute_*` docstring 参照）には
意図的に触れない。
"""
from __future__ import annotations

import json
import os
import threading
import time

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
import pytest  # noqa: E402
from sherpa import agentic_search as A   # noqa: E402
from sherpa.ingest.world_neo4j import GraphSchemaEraError   # noqa: E402


# ---- anthropic_style 用の最小 fake（test_agentic_search.py の _ABlock/_AResp/_AClient と同型）----

class _ABlock:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class _AResp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason


class _AMessages:
    def __init__(self, seq):
        self._seq, self.calls = list(seq), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._seq.pop(0)


class _AClient:
    def __init__(self, seq):
        self.messages = _AMessages(seq)


def _openai_calls(queries):
    return [{"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": json.dumps({"query": q})}}
           for i, q in enumerate(queries)]


# `toolset` を明示指定して `tool_availability()`（ES/graph 可用性の probe・環境によっては数百ms
# かかる）を経由させない——本ファイルは並列実行そのものの壁時計を計測するため、無関係な probe の
# 所要時間が閾値に紛れ込むのを避ける（`openai_style`/`anthropic_style`/`gemini` の `toolset` 明示
# 指定時は可用性判定を一切参照しない・docstring 参照）。
_OPENAI_TOOLSET = A.openai_tools()
_GEMINI_TOOLSET = A.gemini_tools()


# ===== (a)(b): 3方言それぞれで固定——壁時計は直列合計より短く、結果は元の呼び出し順・正しい id =====
# 完了順は投入順の逆（Q2 が最初に終わる）にして、順序保持が完了順に依存しないことを積極的に確認する。

_SLEEPS = {"Q0": 0.3, "Q1": 0.2, "Q2": 0.1}
_SERIAL_SUM = sum(_SLEEPS.values())   # 0.6秒


def test_openai_style_parallel_batch_is_faster_and_preserves_order(monkeypatch):
    calls = _openai_calls(["Q0", "Q1", "Q2"])
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": calls}}]},
        {"choices": [{"message": {"content": "完了しました。"}}]},
    ]
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(body)
        return seq.pop(0)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        time.sleep(_SLEEPS[q])
        return ({"hits": [], "q": q}, set(), [], [])

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    started = time.monotonic()
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                 toolset=_OPENAI_TOOLSET))
    elapsed = time.monotonic() - started
    assert elapsed < _SERIAL_SUM * 0.85   # 並列なら最長0.3秒程度・直列なら0.6秒超
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了しました。"
    tool_msgs = [m for m in bodies[1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1", "c2"]
    assert [json.loads(m["content"])["q"] for m in tool_msgs] == ["Q0", "Q1", "Q2"]


def test_anthropic_style_parallel_batch_is_faster_and_preserves_order(monkeypatch):
    blocks = [_ABlock("tool_use", name="ripgrep_search", input={"query": q}, id=f"tu{i}")
             for i, q in enumerate(["Q0", "Q1", "Q2"])]
    seq = [
        _AResp(blocks, stop_reason="tool_use"),
        _AResp([_ABlock("text", "完了しました。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        time.sleep(_SLEEPS[q])
        return ({"hits": [], "q": q}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    started = time.monotonic()
    events = list(A.anthropic_style(client, "m", A.SYSTEM, "調べて", "v1", None,
                                    toolset=_OPENAI_TOOLSET))
    elapsed = time.monotonic() - started
    assert elapsed < _SERIAL_SUM * 0.85
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了しました。"
    tool_results = client.messages.calls[1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in tool_results] == ["tu0", "tu1", "tu2"]
    assert [json.loads(b["content"])["q"] for b in tool_results] == ["Q0", "Q1", "Q2"]


def test_gemini_parallel_batch_is_faster_and_preserves_order(monkeypatch):
    parts = [{"functionCall": {"name": "ripgrep_search", "args": {"query": q}}}
            for q in ["Q0", "Q1", "Q2"]]
    seq = [
        {"candidates": [{"content": {"parts": parts}}]},
        {"candidates": [{"content": {"parts": [{"text": "完了しました。"}]}, "finishReason": "STOP"}]},
    ]
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(body)
        return seq.pop(0)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        time.sleep(_SLEEPS[q])
        return ({"hits": [], "q": q}, set(), [], [])

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    started = time.monotonic()
    events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "調べて", "v1", None,
                           toolset=_GEMINI_TOOLSET))
    elapsed = time.monotonic() - started
    assert elapsed < _SERIAL_SUM * 0.85
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了しました。"
    resp_parts = bodies[1]["contents"][-1]["parts"]
    assert [p["functionResponse"]["response"]["q"] for p in resp_parts] == ["Q0", "Q1", "Q2"]


# ===== (c): ask_user を含む応答は並列化されない（従来どおり質問で終わる）=====

def test_openai_style_ask_user_in_batch_stays_serial(monkeypatch):
    """ask_user を含む応答は同一バッチでも並列化しない——先行する2件は既存の副作用契約どおり
    実行されるが（`test_openai_style_mixed_tool_calls_and_ask_user_discards_prior_results` と同型）、
    壁時計が直列合計に近いことも確認し、誤って並列化されていないことを積極的に確認する。"""
    calls = _openai_calls(["Q0", "Q1"])
    calls.append({"id": "c2", "function": {"name": "ask_user", "arguments": json.dumps(
        {"prompt": "範囲は？", "mode": "single", "options": [{"label": "全体"}, {"label": "設計"}]})}})
    seq = [{"choices": [{"message": {"content": "", "tool_calls": calls}}]}]
    invoked = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        invoked.append(args["query"])
        time.sleep(0.3)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    started = time.monotonic()
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                 toolset=_OPENAI_TOOLSET))
    elapsed = time.monotonic() - started
    assert invoked == ["Q0", "Q1"]   # ask_user 前の2件は実行される（既存の副作用契約）
    assert elapsed >= 0.55           # 並列化されていれば0.3秒程度で終わるはず＝直列実行の証拠
    q = next(e["question"] for e in events if "question" in e)
    assert q["mode"] == "single"
    assert not any("final" in e for e in events)


# ===== (d): SHERPA_TOOL_PARALLEL=1 で従来どおり直列 =====

def test_openai_style_parallel_disabled_by_env_stays_serial(monkeypatch):
    monkeypatch.setattr(A, "SHERPA_TOOL_PARALLEL", 1)
    calls = _openai_calls(["Q0", "Q1", "Q2"])
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": calls}}]},
        {"choices": [{"message": {"content": "完了。"}}]},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    def fake_run_tool(name, args, world, scope_paths, **kw):
        time.sleep(0.3)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    started = time.monotonic()
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                 toolset=_OPENAI_TOOLSET))
    elapsed = time.monotonic() - started
    assert elapsed >= 0.85   # 3本 x 0.3秒の直列合計に近い＝並列化されていない
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了。"


# ===== (e): 停止イベントが投入の途中で立ったら、未投入分は実行されない =====

def test_openai_style_stop_event_mid_batch_blocks_unsubmitted_calls():
    """generator を明示的に3回 next() して3本分の active ノードを消費し切った直後（＝call0・call1は
    投入済み・call2は「ノード yield 直後の再確認」がまだ実行されていない）に停止要求を立てる——
    投入済みの2件だけが実行され、call2 は投入されないはず（投入済みは完了を待ってから、結果を
    出さずに終了する・既存の stop_event 契約と同型）。"""
    stop_event = threading.Event()
    calls = _openai_calls(["Q0", "Q1", "Q2"])
    seq = [{"choices": [{"message": {"content": "", "tool_calls": calls}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    invoked = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        invoked.append(args["query"])
        time.sleep(0.05)
        return ({"hits": []}, set(), [], [])

    A.run_tool = fake_run_tool
    try:
        gen = A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                             stop_event=stop_event, toolset=_OPENAI_TOOLSET)
        for _ in range(3):
            ev = next(gen)
            assert "node" in ev
        stop_event.set()
        events = list(gen)
        assert events == []             # 停止契約: 結果を出さず終了する
        assert invoked == ["Q0", "Q1"]  # 投入済みの2件だけが実行され、call2は投入されない
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool


# ===== (f): 1本が例外でも他の呼び出しは完了する =====

def test_openai_style_one_worker_exception_does_not_block_others(monkeypatch):
    calls = _openai_calls(["OK0", "BOOM", "OK2"])
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": calls}}]},
        {"choices": [{"message": {"content": "完了。"}}]},
    ]
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(body)
        return seq.pop(0)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        if q == "BOOM":
            raise RuntimeError("boom")
        return ({"hits": [], "q": q}, set(), [], [])

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                 toolset=_OPENAI_TOOLSET))
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了。"   # 例外があっても run 全体は完走する
    tool_msgs = {m["tool_call_id"]: json.loads(m["content"]) for m in bodies[1]["messages"]
                if m.get("role") == "tool"}
    assert "error" in tool_msgs["c1"]                                   # 例外を起こした呼び出しは error 結果
    assert tool_msgs["c0"]["q"] == "OK0" and tool_msgs["c2"]["q"] == "OK2"   # 他2件は正常完了


# ===== RV是正(高): ThreadPoolExecutor は contextvars を継承しない =====

def test_openai_style_parallel_workers_see_pinned_world_root(monkeypatch, tmp_path):
    """`worlds.pin_world_root`（`contextvars.ContextVar` 実装）で固定した world root は、
    `copy_context().run(...)` を経由しないと `ThreadPoolExecutor` のワーカーに伝わらず、
    ワーカー内の `worlds.world_dir()` が pin を見失って別 root/fallback を解決しうる。
    pin 中に2本並列実行しても、両方のワーカーから見える解決結果が pin した root と一致することを
    固定する。"""
    root = tmp_path / "pinned"
    root.mkdir()
    calls = _openai_calls(["Q0", "Q1"])
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": calls}}]},
        {"choices": [{"message": {"content": "完了。"}}]},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    seen_roots = []
    lock = threading.Lock()

    def fake_run_tool(name, args, world, scope_paths, **kw):
        resolved = A.worlds.world_dir(world)   # pin が伝わっていれば root・伝わっていなければ別解決/None
        with lock:
            seen_roots.append(resolved)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    with A.worlds.pin_world_root("v1", root):
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                     toolset=_OPENAI_TOOLSET))
    assert seen_roots == [root, root]   # 両ワーカーとも pin した root をそのまま見る
    assert next(e for e in events if "final" in e)["final"] == "完了。"


# ===== RV是正(高): GraphSchemaEraError は通常のツールエラーへ丸めず再送出する =====

def test_openai_style_graph_schema_era_error_propagates_through_parallel_batch(monkeypatch):
    """`graph_neighbors` が旧世代グラフを検出して `GraphSchemaEraError` を送出したら、並列バッチに
    別ツールが混在していても run 全体へ再送出する——`providers/base.py::_agentic_run` の
    `except GraphSchemaEraError: raise`（`GraphQueryOverloadError` と同じ fail-loud 経路）が
    受け取れるよう、他の呼び出しの例外と同じ `{"error": ...}` へ丸めてはいけない。"""
    calls = [
        {"id": "c0", "function": {"name": "graph_neighbors", "arguments": json.dumps({"entity": "x"})}},
        {"id": "c1", "function": {"name": "ripgrep_search", "arguments": json.dumps({"query": "Q1"})}},
    ]
    seq = [{"choices": [{"message": {"content": "", "tool_calls": calls}}]}]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "graph_neighbors":
            raise GraphSchemaEraError(world, None, lens="troubleshoot")
        time.sleep(0.05)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    with pytest.raises(GraphSchemaEraError):
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                            toolset=A.openai_tools(with_graph=True)))


# ===== RV是正(中): 投入完了後・待機中に立った stop_event も結果収集の前に見る =====

def test_openai_style_stop_event_set_while_workers_running_yields_no_done_nodes(monkeypatch):
    """全件投入済み（投入時は `stop_event` 未設定＝`_stopped` は偽のまま）で `ThreadPoolExecutor` の
    完了待ち中に停止要求が来た場合も、結果収集の前に stop_event を再確認し、done ノード
    （ヒット件数等）・final を一切出さずに終了する（既存の停止契約と同型）。"""
    stop_event = threading.Event()
    calls = _openai_calls(["Q0", "Q1"])
    seq = [{"choices": [{"message": {"content": "", "tool_calls": calls}}]}]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        if q == "Q0":
            time.sleep(0.05)
            stop_event.set()   # 投入完了後（ワーカー実行中）に停止要求が来た、を模す
        else:
            time.sleep(0.15)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                 stop_event=stop_event, toolset=_OPENAI_TOOLSET))
    # 投入時点では stop_event 未設定のため active ノード2件は出るが、それ以外（done ノード・final）
    # は一切出ない。
    assert len(events) == 2
    assert all("node" in e for e in events)
    assert not any("final" in e for e in events)


# ===== RV是正(中・2巡目): ワーカー例外の生文字列は次の外部 LLM 送信本文へ出さない =====

_DUMMY_ABS_PATH = "/home/tudo/secretproject/data/kb/world1/segredo.txt"


def test_openai_style_worker_exception_detail_not_sent_to_next_llm_request(monkeypatch):
    """ワーカー（`run_tool`）が絶対パスを含む例外（例: `FileNotFoundError`）を送出しても、次ターンの
    `_post` 送信本文にその生文字列が現れない——ツール結果は固定文言に丸められ、詳細はサーバー
    ログにだけ残る。"""
    calls = _openai_calls(["OK0", "BOOM"])
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": calls}}]},
        {"choices": [{"message": {"content": "完了。"}}]},
    ]
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(body)
        return seq.pop(0)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        if q == "BOOM":
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{_DUMMY_ABS_PATH}'")
        return ({"hits": [], "q": q}, set(), [], [])

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "調べて", "v1", None,
                                 toolset=_OPENAI_TOOLSET))
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了。"
    tool_msgs = {m["tool_call_id"]: m["content"] for m in bodies[1]["messages"] if m.get("role") == "tool"}
    assert json.loads(tool_msgs["c1"]) == {"error": "ツール実行に失敗しました"}   # 固定文言のみ
    assert _DUMMY_ABS_PATH not in json.dumps(bodies[1])   # 例外の生文字列が本文に含まれない


def test_anthropic_style_worker_exception_detail_not_sent_to_next_llm_request(monkeypatch):
    blocks = [
        _ABlock("tool_use", name="ripgrep_search", input={"query": "OK0"}, id="tu0"),
        _ABlock("tool_use", name="ripgrep_search", input={"query": "BOOM"}, id="tu1"),
    ]
    seq = [
        _AResp(blocks, stop_reason="tool_use"),
        _AResp([_ABlock("text", "完了。")], stop_reason="end_turn"),
    ]
    client = _AClient(seq)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        if q == "BOOM":
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{_DUMMY_ABS_PATH}'")
        return ({"hits": [], "q": q}, set(), [], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    events = list(A.anthropic_style(client, "m", A.SYSTEM, "調べて", "v1", None,
                                    toolset=_OPENAI_TOOLSET))
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了。"
    tool_results = client.messages.calls[1]["messages"][-1]["content"]
    by_id = {b["tool_use_id"]: b["content"] for b in tool_results}
    assert json.loads(by_id["tu1"]) == {"error": "ツール実行に失敗しました"}
    # `messages` にはブロック（`_ABlock`）がそのまま入っており丸ごとの JSON 化はできないため、
    # 実際に本ファイルが構築する tool_result（次ターン送信本文のうち例外を経由しうる部分）だけを
    # 検査する。
    assert _DUMMY_ABS_PATH not in json.dumps(tool_results)


def test_gemini_worker_exception_detail_not_sent_to_next_llm_request(monkeypatch):
    parts = [
        {"functionCall": {"name": "ripgrep_search", "args": {"query": "OK0"}}},
        {"functionCall": {"name": "ripgrep_search", "args": {"query": "BOOM"}}},
    ]
    seq = [
        {"candidates": [{"content": {"parts": parts}}]},
        {"candidates": [{"content": {"parts": [{"text": "完了。"}]}, "finishReason": "STOP"}]},
    ]
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(body)
        return seq.pop(0)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        q = args["query"]
        if q == "BOOM":
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{_DUMMY_ABS_PATH}'")
        return ({"hits": [], "q": q}, set(), [], [])

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "調べて", "v1", None,
                           toolset=_GEMINI_TOOLSET))
    final = next(e for e in events if "final" in e)
    assert final["final"] == "完了。"
    resp_parts = bodies[1]["contents"][-1]["parts"]
    responses = [p["functionResponse"]["response"] for p in resp_parts]
    assert {"error": "ツール実行に失敗しました"} in responses
    assert _DUMMY_ABS_PATH not in json.dumps(bodies[1])
