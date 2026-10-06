"""`sherpa/providers/codex/activity.py` の契約テスト（公開の `summarize_turn` と偽 Codex/フェイク provider の
実行結果だけで確かめる）: v1 の形・要約に本文が出ない・MCP 呼出しの二重記録・exec のエラー数え分け・
複数親の合算・偽 Codex 実行（成功／context_window_exceeded）の env["activity"]・chat_service が保存直前に
app_version／phases_ms を埋める（確認の質問ターンも activity を引き継ぐ）。
偽 Codex は PATH に偽 `codex` 実行ファイルを差し込む（実 codex は呼ばない）。chat_service 側は store をフェイク差し替え。
"""
from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import chat_service as CS
from sherpa import store
from sherpa.providers.codex import activity as AC


# ===== 記録の組み立てヘルパ（1レコード1行の JSON） =====

def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _session_path(home: Path, tid: str) -> Path:
    return home / "sessions" / "2099" / "01" / "01" / f"rollout-{tid}.jsonl"


def _write_session(home: Path, tid: str, records: list, mtime: float | None = None) -> Path:
    path = _session_path(home, tid)
    _write_jsonl(path, records)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _summarize(home, parents, *, base, children=None, settings=None, phases_ms=None):
    return AC.summarize_turn(home, parent_thread_ids=parents, child_thread_ids=children or set(),
                             turn_started_wall=base, settings=settings or {},
                             phases_ms=phases_ms or {"prepare": 0, "agent": 0})


def _session_meta(tid, **extra_payload):
    return {"type": "session_meta", "payload": {"id": tid, **extra_payload}}


def _turn_context(ts, model):
    return {"type": "turn_context", "timestamp": _iso(ts), "payload": {"model": model}}


def _token_count(ts, *, inp, cached, out, reasoning):
    row = {"input_tokens": inp, "cached_input_tokens": cached,
           "output_tokens": out, "reasoning_output_tokens": reasoning}
    return {"type": "event_msg", "timestamp": _iso(ts),
            "payload": {"type": "token_count", "info": {"last_token_usage": row, "total_token_usage": row}}}


def _mcp_tool_call_end(ts, *, tool, text, is_error=False, secs=0, nanos=0, call_id=None):
    payload = {"type": "mcp_tool_call_end",
               "invocation": {"server": "sherpa", "tool": tool},
               "duration": {"secs": secs, "nanos": nanos},
               "result": {"Ok": {"content": [{"type": "text", "text": text}], "isError": is_error}}}
    if call_id is not None:
        payload["call_id"] = call_id
    return {"type": "event_msg", "timestamp": _iso(ts), "payload": payload}


def _function_call(ts, *, name, call_id, namespace=None):
    payload = {"type": "function_call", "name": name, "call_id": call_id}
    if namespace is not None:
        payload["namespace"] = namespace
    return {"type": "response_item", "timestamp": _iso(ts), "payload": payload}


def _function_call_output(ts, *, call_id, output):
    return {"type": "response_item", "timestamp": _iso(ts),
            "payload": {"type": "function_call_output", "call_id": call_id, "output": output}}


def _custom_tool_call(ts, *, name, call_id):
    return {"type": "response_item", "timestamp": _iso(ts),
            "payload": {"type": "custom_tool_call", "name": name, "call_id": call_id}}


def _custom_tool_call_output(ts, *, call_id, blocks):
    return {"type": "response_item", "timestamp": _iso(ts),
            "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": blocks}}


def _compacted(ts):
    return {"type": "compacted", "timestamp": _iso(ts), "payload": {}}


def test_mcp_call_recorded_twice_counts_once_and_keeps_parent(tmp_path):
    """実 CLI は MCP ツールの1回の呼出しを function_call（namespace=mcp__sherpa）と mcp_tool_call_end
    （同じ call_id）の両方に書く。旧実装はこの並びで KeyError を出して親の要約ごと失っていた。"""
    base = time.time()
    first, second = json.dumps({"hits": [1, 2]}), json.dumps({"hits": [], "truncated": True})
    _write_session(tmp_path, "parent-mcp", [
        _session_meta("parent-mcp"),
        _turn_context(base + 1, "gpt-5.4"),
        _token_count(base + 2, inp=100, cached=10, out=5, reasoning=1),
        # 実 CLI の並び: function_call → mcp_tool_call_end → function_call_output（同じ call_id）。
        _function_call(base + 3, name="ripgrep_search", call_id="c1", namespace="mcp__sherpa"),
        _mcp_tool_call_end(base + 4, tool="ripgrep_search", text=first, call_id="c1"),
        _function_call_output(base + 5, call_id="c1", output=first),
        # 出力が終了イベントより先に来る並びでも二重に数えない。
        _function_call(base + 6, name="ripgrep_search", call_id="c2", namespace="mcp__sherpa"),
        _function_call_output(base + 7, call_id="c2", output=second),
        _mcp_tool_call_end(base + 8, tool="ripgrep_search", text=second, call_id="c2"),
        # MCP でない組込みツールは従来どおり function_call 側で数える。
        _function_call(base + 9, name="exec_command", call_id="c3"),
        _function_call_output(base + 10, call_id="c3", output="ok"),
    ])

    result = _summarize(tmp_path, ["parent-mcp"], base=base)

    assert [a["role"] for a in result["agents"]] == ["parent"]
    tools = result["agents"][0]["tools"]
    assert tools["ripgrep_search"]["calls"] == 2
    assert tools["ripgrep_search"]["bytes"] == len(first) + len(second)
    assert tools["ripgrep_search"]["truncated"] == 1
    assert tools["exec_command"] == {"calls": 1, "bytes": 2, "max_bytes": 2}


def test_exec_command_errors_and_sandbox_errors_counted_without_leaking_body(tmp_path):
    """exec（custom_tool_call・output は exec_command の JSON ブロック）の終了コード 0（成功）・127（一般失敗）・
    サンドボックス起因（bwrap の RTM_NEWADDR・通常の JSON にならない）を数え分ける。成功は errors に数えず、
    成功コマンドの本文にサンドボックスの語が含まれても数えない。本文は要約に一切現れない。"""
    base = time.time()
    secret_output = "SECRET-TOKEN-should-not-leak-into-summary"
    header = {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"}

    def _out(chunk_id, code, output):
        return {"type": "input_text", "text": json.dumps({"chunk_id": chunk_id, "exit_code": code, "output": output})}

    _write_session(tmp_path, "parent-exec", [
        _session_meta("parent-exec"),
        _turn_context(base + 1, "gpt-6-astra"),
        _token_count(base + 2, inp=10, cached=0, out=5, reasoning=0),
        _custom_tool_call(base + 3, name="exec", call_id="e1"),
        _custom_tool_call_output(base + 4, call_id="e1", blocks=[header, _out("a", 0, secret_output)]),
        _custom_tool_call(base + 5, name="exec", call_id="e2"),
        _custom_tool_call_output(base + 6, call_id="e2", blocks=[header, _out("b", 127, "rg: not found")]),
        _custom_tool_call(base + 7, name="exec", call_id="e3"),
        _custom_tool_call_output(base + 8, call_id="e3", blocks=[
            {"type": "input_text", "text": "bwrap: Failed RTM_NEWADDR: Operation not permitted"}]),
        _custom_tool_call(base + 9, name="exec", call_id="e4"),
        _custom_tool_call_output(base + 10, call_id="e4", blocks=[_out("d", 0, "bwrap: execvp RTM_NEWADDR")]),
    ])

    result = _summarize(tmp_path, ["parent-exec"], base=base)

    tools = result["agents"][0]["tools"]
    assert tools["exec"]["calls"] == 4
    assert tools["exec"]["errors"] == 1                                      # 127 の1件だけ（0は数えない）
    assert tools["exec"]["sandbox_errors"] == 1                              # bwrap の1件だけ
    assert set(tools["exec"].keys()) == {"calls", "bytes", "max_bytes", "errors", "sandbox_errors"}
    dumped = json.dumps(result, ensure_ascii=False)
    assert secret_output not in dumped
    assert "RTM_NEWADDR" not in dumped
    assert "rg: not found" not in dumped


# ===== summarize_turn: 形・フィルタ・打切り・引数/本文の非漏洩 =====

def test_summarize_turn_matches_v1_shape(tmp_path):
    """親1体（resume＝前ターンの行を含む）＋子2体の記録から v1 の形どおりの要約ができる。
    ターン開始より前の行は親・子とも数えない。往復1000件の打切りは rounds 配列の大きさだけを抑え、
    tokens 集計はファイル最後まで続く（圧縮位置は実際の往復数で決まる）。不正な token 値（bool を含む
    非 int）・型が壊れた type・timestamp が読めない行・不正 UTF-8 行・未知の種類は unparsed へ計上して
    その行だけ飛ばす。function_call 系は calls/bytes/max_bytes だけ（測っていないキーを0で残さない）・
    web_search_call は calls だけ。session_meta.payload.id が壊れた記録や stat() が失敗する候補があっても
    他の集計を失わない。要約 JSON に引数・結果本文・資料名が一切入らない。"""
    base = time.time()
    parent_id, child_small_id, child_cap_id = "parent-thread-1", "child-thread-small", "child-thread-cap"
    secret_query = "SECRET_QUERY_極秘資料名.txt"
    secret_body = "SECRET_BODY_取引先個人情報"

    parent_records = [
        _session_meta(parent_id),
        # 前ターン（turn_started より前）＝数えない。
        _turn_context(base - 500, "gpt-5.4-old"),
        _token_count(base - 500, inp=9999, cached=9999, out=9999, reasoning=9999),
        _mcp_tool_call_end(base - 500, tool="old_tool", text=json.dumps({"ok": True})),
        # 今ターン
        _turn_context(base + 1, "gpt-5.5-test"),
        _token_count(base + 2, inp=100, cached=20, out=30, reasoning=5),
        _mcp_tool_call_end(base + 3, tool="ripgrep_search",
                           text=json.dumps({"hits": [{"text": secret_body}], "query": secret_query,
                                            "text_truncated": True})),
        _mcp_tool_call_end(base + 4, tool="ripgrep_search", text=json.dumps({"hits": [1, 2, 3]})),
        _mcp_tool_call_end(base + 5, tool="read_doc", text=json.dumps({"error": "not_found"}), is_error=True),
        {"type": "event_msg", "timestamp": _iso(base + 6), "payload": {   # プロトコル層失敗（result.Err）
            "type": "mcp_tool_call_end", "invocation": {"server": "sherpa", "tool": "es_search"},
            "duration": {"secs": 0, "nanos": 0}, "result": {"Err": "transport failure"}}},
        _mcp_tool_call_end(base + 7, tool="glob_search", text=json.dumps({"matches": [], "truncated": True})),
        _compacted(base + 8),
        _token_count(base + 9, inp=40, cached=5, out=10, reasoning=1),
        _function_call(base + 10, name="exec_command", call_id="call-1"),
        _function_call_output(base + 11, call_id="call-1", output=f"cat {secret_query}\n{secret_body}"),
        _custom_tool_call(base + 12, name="apply_patch", call_id="call-2"),
        _custom_tool_call_output(base + 13, call_id="call-2", blocks=[{"type": "input_text", "text": "patch applied ok"}]),
        # Web検索: 結果本文が無く大きさを測れないため calls だけを数える。
        {"type": "response_item", "timestamp": _iso(base + 13.5), "payload": {"type": "web_search_call"}},
        # 未知の種類（CLI の形式変化）＝落とさず unparsed へ計上。
        {"type": "event_msg", "timestamp": _iso(base + 14), "payload": {"type": "future_event_kind"}},
        {"type": "some_future_top_level_kind", "timestamp": _iso(base + 15), "payload": {}},
        # 型が壊れた type（[]/{} は hashable でない）——3箇所とも unparsed["invalid_type"] へ。
        {"type": "event_msg", "timestamp": _iso(base + 14.1), "payload": {"type": []}},
        {"type": "response_item", "timestamp": _iso(base + 14.2), "payload": {"type": {}}},
        {"type": {"nested": "bad"}, "timestamp": _iso(base + 14.3), "payload": {}},
    ]

    def _bad_token_line(ts, first_value):
        # 文字列は旧実装で ValueError がファイルの外まで上がり activity 全体を失っていた。
        # bool は int の部分型だが数値として扱わない（int(True or 0) は静かに 1 へ丸まる）。
        return json.dumps({"type": "event_msg", "timestamp": _iso(ts), "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": first_value, "cached_input_tokens": 1, "output_tokens": 1,
                "reasoning_output_tokens": 0}}}}, ensure_ascii=False)

    # timestamp が読めない行は「今回分」と推測せず unparsed["timestamp"] へ（500 を混ぜて数えたら分かるように）。
    bad_timestamp_line = json.dumps(
        {"type": "event_msg", "timestamp": "not-a-valid-timestamp", "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 500, "cached_input_tokens": 500, "output_tokens": 500,
                "reasoning_output_tokens": 500}}}}, ensure_ascii=False)

    parent_path = _session_path(tmp_path, parent_id)
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    with open(parent_path, "wb") as f:
        for rec in parent_records:
            f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        f.write(b"\xff\xfe not valid utf-8\n")   # 不正 UTF-8 行（前後の正常行は読める＝fail-open）
        f.write((json.dumps(_token_count(base + 16, inp=7, cached=0, out=0, reasoning=0), ensure_ascii=False) + "\n").encode("utf-8"))
        f.write((_bad_token_line(base + 17, "not-a-number") + "\n").encode("utf-8"))
        f.write((_bad_token_line(base + 18, True) + "\n").encode("utf-8"))
        f.write((bad_timestamp_line + "\n").encode("utf-8"))
    os.utime(parent_path, (base + 100, base + 100))

    _write_session(tmp_path, child_small_id, [
        _session_meta(child_small_id, parent_thread_id=parent_id, thread_source="subagent"),
        _turn_context(base + 1, "gpt-5.4-mini"),
        _token_count(base - 500, inp=8888, cached=8888, out=8888, reasoning=8888),   # 子にも min_epoch（数えたら11でなくなる）
        _token_count(base + 2, inp=11, cached=2, out=3, reasoning=0),
        _mcp_tool_call_end(base + 3, tool="read_around", text=json.dumps({"ok": True})),
    ], mtime=base + 101)

    # 往復1000件打切りは子のもう1体で確認する。1002件の後に compacted を挟む（実往復数1002＝圧縮位置1003）。
    cap_records = [_session_meta(child_cap_id, parent_thread_id=parent_id, thread_source="subagent")]
    cap_records += [_token_count(base + 1 + i * 0.01, inp=1, cached=0, out=0, reasoning=0) for i in range(1002)]
    cap_records.append(_compacted(base + 1 + 1002 * 0.01))
    cap_records.append(_token_count(base + 1 + 1003 * 0.01, inp=1, cached=0, out=0, reasoning=0))
    _write_session(tmp_path, child_cap_id, cap_records, mtime=base + 102)

    # session_meta.payload.id が [] の記録・stat() が失敗する候補（壊れたシンボリックリンク）を混ぜる。
    _write_session(tmp_path, "bad-id", [{"type": "session_meta", "payload": {"id": []}},
                                        _token_count(base + 2, inp=999, cached=999, out=999, reasoning=999)],
                   mtime=base + 103)
    _session_path(tmp_path, "broken-stat").symlink_to(_session_path(tmp_path, "does-not-exist"))

    settings = {"provider": "codex", "config": "openai", "model": "gpt-5.5-test", "reasoning": "medium",
                "depth": "standard", "review_rounds": 2, "schema_level": 2, "multi_agent": True,
                "budget_per_result": 65536, "max_hits": 45, "window_cap": 60}
    phases_ms = {"prepare": 120, "agent": 5000}
    result = _summarize(tmp_path, [parent_id], base=base, settings=settings, phases_ms=phases_ms)

    assert result["v"] == 1
    assert result["source"] == "codex_rollout"
    assert result["settings"] == settings
    assert result["phases_ms"] == phases_ms
    # 壊れた id の記録・stat() 失敗の候補は読み飛ばし、parent 1・child 2 のまま。
    assert [a["role"] for a in result["agents"]] == ["parent", "child", "child"]

    parent = result["agents"][0]
    assert parent["model"] == "gpt-5.5-test"                              # 前ターンの turn_context は数えない
    # 今ターンの3 round 分だけ（前ターンの9999・timestamp が読めない500は数えない・不正バイト後の1件は数える）。
    assert parent["tokens"] == {"input_tokens": 147, "cached_input_tokens": 25,
                                "output_tokens": 40, "reasoning_output_tokens": 6}
    assert parent["rounds"] == [[100, 20, 30, 5], [40, 5, 10, 1], [7, 0, 0, 0]]
    assert parent["compactions"] == [2]

    tools = parent["tools"]
    assert tools["ripgrep_search"]["calls"] == 2
    assert tools["ripgrep_search"]["clipped"] == 1                        # text_truncated:true の1件だけ
    assert tools["read_doc"]["errors"] == 1                               # isError:true
    assert tools["es_search"]["errors"] == 1                              # result.Err（プロトコル層失敗）
    assert tools["glob_search"]["truncated"] == 1
    assert tools["exec_command"]["calls"] == 1
    assert tools["exec_command"]["bytes"] == len(f"cat {secret_query}\n{secret_body}".encode("utf-8"))
    assert tools["apply_patch"]["calls"] == 1
    assert tools["apply_patch"]["bytes"] == len("patch applied ok".encode("utf-8"))
    assert set(tools["exec_command"].keys()) == {"calls", "bytes", "max_bytes"}
    assert set(tools["apply_patch"].keys()) == {"calls", "bytes", "max_bytes"}
    assert set(tools["ripgrep_search"].keys()) == {
        "calls", "bytes", "max_bytes", "clipped", "truncated", "errors", "ms"}   # MCP は全7キー
    assert tools["web_search"] == {"calls": 1}

    assert parent["unparsed"].get("event_msg:future_event_kind") == 1
    assert parent["unparsed"].get("some_future_top_level_kind") == 1
    assert parent["unparsed"].get("invalid_json") == 1
    assert parent["unparsed"].get("token_count") == 2                     # 文字列・bool の往復
    assert parent["unparsed"].get("invalid_type") == 3
    assert parent["unparsed"].get("timestamp") == 1

    children = result["agents"][1:]
    small = next(c for c in children if c["tokens"]["input_tokens"] == 11)
    assert small["model"] == "gpt-5.4-mini"
    capped = next(c for c in children if c is not small)
    assert len(capped["rounds"]) == 1000                                  # 配列は1000で打切り
    assert capped["tokens"]["input_tokens"] == 1003                       # tokens は全行分
    assert capped["compactions"] == [1003]                                # 実往復数1002の次（旧実装は1001に固定）

    dumped = json.dumps(result, ensure_ascii=False)
    assert secret_query not in dumped
    assert secret_body not in dumped
    assert "ripgrep_search" in dumped and "exec_command" in dumped        # ツール名は残る


def test_summarize_turn_merges_multiple_parent_threads_after_resume_fallback(tmp_path):
    """resume 失敗→フォールバックで複数の親スレッドを使ったターンは1つの parent へ合算する
    （tokens は和・rounds は時刻順連結・tools は項目ごとの和＋max_bytes だけ最大・compactions は前の親の
    実往復数だけずらす・model は最後の親）。ずらし幅は rounds 配列長（1000で頭打ち）でなく実往復数を使い、
    連結後の rounds も先頭1000件に切り詰める。"""
    base = time.time()
    _write_session(tmp_path, "parent-a-failed-resume", [
        _session_meta("parent-a-failed-resume"),
        _turn_context(base + 1, "gpt-5.4-old-resume"),
        _token_count(base + 2, inp=10, cached=1, out=1, reasoning=0),
        _compacted(base + 3),
        _token_count(base + 4, inp=20, cached=2, out=2, reasoning=0),
        _mcp_tool_call_end(base + 5, tool="ripgrep_search", text=json.dumps({"hits": []})),
    ], mtime=base + 10)
    _write_session(tmp_path, "parent-b-fallback", [
        _session_meta("parent-b-fallback"),
        _turn_context(base + 6, "gpt-5.4-fallback-fresh"),
        _token_count(base + 7, inp=100, cached=10, out=10, reasoning=1),
        _mcp_tool_call_end(base + 8, tool="ripgrep_search", text=json.dumps({"hits": [1, 2, 3, 4, 5]})),   # bytes が大きい
    ], mtime=base + 20)

    merged = _summarize(tmp_path, ["parent-a-failed-resume", "parent-b-fallback"], base=base)
    assert [a["role"] for a in merged["agents"]] == ["parent"]   # 二重に数えず1エントリへ合算
    m = merged["agents"][0]
    assert m["model"] == "gpt-5.4-fallback-fresh"
    assert m["tokens"] == {"input_tokens": 130, "cached_input_tokens": 13,
                           "output_tokens": 13, "reasoning_output_tokens": 1}
    assert m["rounds"] == [[10, 1, 1, 0], [20, 2, 2, 0], [100, 10, 10, 1]]
    assert m["compactions"] == [2]
    bytes_a = len(json.dumps({"hits": []}).encode("utf-8"))
    bytes_b = len(json.dumps({"hits": [1, 2, 3, 4, 5]}).encode("utf-8"))
    assert m["tools"]["ripgrep_search"]["calls"] == 2
    assert m["tools"]["ripgrep_search"]["bytes"] == bytes_a + bytes_b
    assert m["tools"]["ripgrep_search"]["max_bytes"] == max(bytes_a, bytes_b)

    # 先のピースだけで既に1000件・実往復数1002。後のピース（圧縮位置1・往復1件）の位置は 1002+1=1003。
    home2 = tmp_path / "capped"
    piece_c = [_session_meta("parent-c-capped"), _turn_context(base + 30, "gpt-5.4-c")]
    piece_c += [_token_count(base + 31 + i * 0.001, inp=1, cached=0, out=0, reasoning=0) for i in range(1002)]
    _write_session(home2, "parent-c-capped", piece_c, mtime=base + 40)
    _write_session(home2, "parent-d-after-cap", [
        _session_meta("parent-d-after-cap"),
        _turn_context(base + 41, "gpt-5.4-d"),
        _compacted(base + 41.5),
        _token_count(base + 42, inp=5, cached=0, out=0, reasoning=0),
    ], mtime=base + 50)

    m2 = _summarize(home2, ["parent-c-capped", "parent-d-after-cap"], base=base)["agents"][0]
    assert len(m2["rounds"]) == 1000
    assert m2["rounds"][-1] == [1, 0, 0, 0]       # piece_c の最後の往復のまま（piece_d の [5,0,0,0] ではない）
    assert m2["tokens"]["input_tokens"] == 1007   # 再切り詰めの影響を受けない（1002+5）
    assert m2["compactions"] == [1003]


def test_summarize_turn_keeps_parent_when_child_body_read_fails(tmp_path, monkeypatch):
    """子の本文を読んでいる途中の OSError（EIO 等）でも、その候補（子）だけを読み飛ばして親の要約は残る。
    `open` だけを差し替える外部境界のモック。"""
    base = time.time()

    class _EIOAfterFirstLine:
        """1行目（session_meta）は実ファイルどおり返し、以降の読み取りで OSError を起こす。"""
        def __init__(self, first_line: str):
            self._lines = [first_line]

        def readline(self):
            if self._lines:
                return self._lines.pop()
            raise OSError(5, "Input/output error")

        def __iter__(self):
            return self

        def __next__(self):
            raise OSError(5, "Input/output error")

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    _write_session(tmp_path, "parent-eio", [
        _session_meta("parent-eio"), _turn_context(base + 60, "gpt-5.5-eio"),
        _token_count(base + 61, inp=7, cached=1, out=1, reasoning=0)], mtime=base + 70)
    child_path = _write_session(tmp_path, "child-eio-flaky", [
        _session_meta("child-eio-flaky", parent_thread_id="parent-eio", thread_source="subagent"),
        _token_count(base + 62, inp=999, cached=999, out=999, reasoning=999)], mtime=base + 71)
    first_line = child_path.read_text(encoding="utf-8").splitlines()[0]
    real_open = open

    def _flaky_open(file, *args, **kwargs):
        if file == child_path:
            return _EIOAfterFirstLine(first_line)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(AC, "open", _flaky_open, raising=False)
    result = _summarize(tmp_path, ["parent-eio"], base=base)
    assert [a["role"] for a in result["agents"]] == ["parent"]   # 子は1体も出ない
    assert result["agents"][0]["tokens"] == {"input_tokens": 7, "cached_input_tokens": 1,
                                             "output_tokens": 1, "reasoning_output_tokens": 0}


# ===== 偽 Codex 実行: 成功／turn.failed(context_window_exceeded) =====

_FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

plan = json.loads(pathlib.Path(r"__PLAN__").read_text(encoding="utf-8"))
stamp = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
print(json.dumps({"type": "thread.started", "thread_id": plan["thread_id"]}))
sys.stdout.flush()
sdir = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions" / "2099" / "01" / "01"
sdir.mkdir(parents=True, exist_ok=True)
for tid, records in plan["sessions"].items():
    for r in records:
        if "timestamp" in r:
            r["timestamp"] = stamp   # 実行時刻で打つ（ターン開始より前の行として落とされないように）
    (sdir / ("rollout-" + tid + ".jsonl")).write_text("\n".join(json.dumps(r) for r in records) + "\n")
for ev in plan["events"]:
    print(json.dumps(ev))
sys.exit(0)
'''


def _fake_codex_env(tmp_path: Path, monkeypatch, plan: dict) -> None:
    """plan = {thread_id, sessions: {thread_id: [記録…]}, events: [stdout の JSON イベント…]}"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX.replace("__PLAN__", str(plan_path)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")   # 平文の偽 codex＝構造化応答は使わない


_USAGE_ROW = {"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 50, "reasoning_output_tokens": 10}


def _activity_ctx(uid: str, turn_started_mono: float | None = None):
    from sherpa import agents as A
    return A.Ctx(
        message="活動要約テスト用の質問",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline", "summary": {"total": 0},
            "data": {}, "sources": []},
        knowledge=True, uid=uid, make_sources=lambda docs: [],
        turn_started_mono=turn_started_mono,
    )


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


def test_fake_codex_success_turn_has_activity_with_children_matching_usage_breakdown(tmp_path, monkeypatch):
    """成功ターンで env["activity"] が載り、親のトークンはセッション記録どおり。子の検出数は既存の
    _collect_child_token_usage（env["codex_usage_children"]）と一致する。env["usage"] は子込みの合計。
    prepare は ctx.turn_started_mono（chat_service がプロバイダ呼出し前に取る起点）から測る。"""
    from sherpa import agents as A
    parent = "fake-parent-success-1"
    sessions = {parent: [
        _session_meta(parent),
        _turn_context(0, "gpt-5.5-fake"),
        _token_count(0, inp=111, cached=22, out=33, reasoning=4),
        _mcp_tool_call_end(0, tool="ripgrep_search", text=json.dumps({"hits": []}), nanos=50000000)]}
    for i, child in enumerate(("fake-child-1", "fake-child-2")):   # 新形式の子2体（parent_thread_id 突合）
        sessions[child] = [_session_meta(child, parent_thread_id=parent, thread_source="subagent"),
                           _token_count(0, inp=10 + i, cached=1, out=2, reasoning=0)]
    _fake_codex_env(tmp_path, monkeypatch, {
        "thread_id": parent, "sessions": sessions,
        "events": [{"type": "item.completed", "item": {"id": "m1", "type": "agent_message",
                                                       "text": "調べました。分かりました。"}},
                   {"type": "turn.completed", "usage": _USAGE_ROW}]})

    ctx = _activity_ctx("activity-success-u1", turn_started_mono=time.monotonic())
    env = _result_env(list(A.CodexProvider().run(ctx)))

    activity = env.get("activity")
    assert activity is not None, f"activity が載っていない: {env!r}"
    assert activity["v"] == 1 and activity["source"] == "codex_rollout"
    assert activity["phases_ms"]["prepare"] >= 0
    assert activity["phases_ms"]["agent"] >= 0
    parent = next(a for a in activity["agents"] if a["role"] == "parent")
    assert parent["tokens"] == {"input_tokens": 111, "cached_input_tokens": 22,
                                "output_tokens": 33, "reasoning_output_tokens": 4}
    assert parent["tools"]["ripgrep_search"]["calls"] == 1
    assert len([a for a in activity["agents"] if a["role"] == "child"]) == env["codex_usage_children"]["found"] == 2

    usage = env.get("usage")
    assert usage is not None
    # 子が見つかると env["usage"] は親（turn.completed）＋子2体の合計: 500+10+11=521・50+2+2=54
    assert usage["input_tokens"] == 521 and usage["output_tokens"] == 54


def test_fake_codex_context_window_exceeded_turn_has_nonzero_activity_tokens(tmp_path, monkeypatch):
    """turn.failed(context_window_exceeded) でも env["activity"] が載り、トークンはセッション記録どおり
    0でない。env["usage"] は載らない。ctx.turn_started_mono が無ければ prepare は測らない（agent は測れる）。"""
    from sherpa import agents as A
    parent = "fake-parent-failure-1"
    _fake_codex_env(tmp_path, monkeypatch, {
        "thread_id": parent,
        "sessions": {parent: [_session_meta(parent), _turn_context(0, "gpt-5.5-fake"),
                              _token_count(0, inp=90000, cached=1000, out=200, reasoning=50)]},
        "events": [{"type": "turn.failed", "error": {
            "code": "boom", "codex_error_info": "context_window_exceeded",
            "message": "Error running remote compact task: Codex ran out of room"}}]})

    env = _result_env(list(A.CodexProvider().run(_activity_ctx("activity-failure-u1"))))

    assert env.get("codex_error_code") == "context_window_exceeded"
    activity = env.get("activity")
    assert activity is not None, f"失敗ターンで activity が載っていない: {env!r}"
    assert "prepare" not in activity["phases_ms"]
    assert "agent" in activity["phases_ms"]
    parent = next(a for a in activity["agents"] if a["role"] == "parent")
    assert parent["tokens"]["input_tokens"] == 90000
    assert env.get("usage") is None   # turn.completed が一度も来ていない


# ===== chat_service: 保存直前に app_version/phases_ms を埋める（全経路共通） =====

def _mock_store_no_db(monkeypatch):
    """PG 不要。戻り値は store.add_message に渡された行を挿入順に保持するリスト。"""
    saved: list = []
    counter = [0]

    def fake_add_message(conversation_id, role, content="", lens=None, route=None, trace=None,
                         answer=None, personal=False):
        counter[0] += 1
        row = {"id": counter[0], "conversation_id": conversation_id, "role": role, "content": content,
               "lens": lens, "route": route, "trace": trace, "answer": answer, "personal": personal}
        saved.append(row)
        return row

    monkeypatch.setattr(store, "add_message", fake_add_message)
    monkeypatch.setattr(store, "recent_messages", lambda conversation_id, limit: [])
    monkeypatch.setattr(store, "get_session_id", lambda conversation_id: None)
    monkeypatch.setattr(store, "get_codex_usage_total", lambda conversation_id: None)
    monkeypatch.setattr(store, "get_settings", lambda user_id: {})
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: {})
    monkeypatch.setattr(store, "set_contains_personal_workspace", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_message_personal", lambda message_id: None)
    monkeypatch.setattr(store, "set_session_id", lambda *a, **k: None)
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    monkeypatch.setattr(store, "conversation_is_personal_tainted", lambda conversation_id: False)
    return saved


class _FakeChatProvider:
    def __init__(self, events):
        self._events = events

    def run(self, ctx):
        return iter(self._events)


def _fixed_chat_result(headline, activity=None):
    env = {"headline": headline, "summary": {}, "data": {}, "sources": [],
           "scope": {"world": "v1", "scope_paths": [], "source": "all"}}
    if activity is not None:
        env["activity"] = activity
    return {"type": "_result", "env": env, "decision": {"lens": "qa", "input": "q", "reason": "t"}}


def test_chat_service_fills_app_version_and_phases_ms_for_both_paths(monkeypatch):
    """provider が activity を作らない経路は保存直前に
    {v:1, source:"none", app_version, phases_ms:{"total":duration_ms}} が入る（prepare/agent/post は測って
    いないのでキー自体を置かない）。provider が activity を作った経路はそれを保ったまま
    app_version／phases_ms.total／phases_ms.post（=total-prepare-agent）を埋める。"""
    from sherpa import app_version as AV

    saved_none = _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(CS, "plain_provider_for", lambda *a, **k: None)
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeChatProvider(
        [_fixed_chat_result("provider に activity 無し")]))
    list(CS.stream_message(None, "activity 無し経路のテスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))
    answer_none = saved_none[-1]["answer"]
    activity_none = answer_none["activity"]
    assert activity_none["v"] == 1 and activity_none["source"] == "none"
    assert activity_none["app_version"] == AV.current()
    assert activity_none["phases_ms"] == {"total": answer_none["duration_ms"]}

    saved = _mock_store_no_db(monkeypatch)
    provider_activity = {"v": 1, "source": "codex_rollout", "settings": {"model": "gpt-5.5-test"},
                         "phases_ms": {"prepare": 120, "agent": 340}, "agents": []}
    monkeypatch.setattr(CS, "plain_provider_for", lambda *a, **k: None)
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeChatProvider(
        [_fixed_chat_result("provider に activity 有り", activity=provider_activity)]))
    list(CS.stream_message(None, "activity 有り経路のテスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))
    answer_full = saved[-1]["answer"]
    activity_full = answer_full["activity"]
    assert activity_full["source"] == "codex_rollout"                    # provider の値を保つ
    assert activity_full["settings"] == {"model": "gpt-5.5-test"}
    assert activity_full["app_version"] == AV.current()
    phases_full = activity_full["phases_ms"]
    assert phases_full["prepare"] == 120 and phases_full["agent"] == 340
    assert phases_full["total"] == answer_full["duration_ms"]
    assert phases_full["post"] == max(0, phases_full["total"] - 120 - 340)
    assert all(phases_full[k] >= 0 for k in ("prepare", "agent", "post", "total"))


def test_confirm_question_turn_carries_provider_activity_into_saved_card(monkeypatch):
    """Codex が確認の質問を返したターンでは、provider が作った activity が question イベント経由で確認カードの
    answer.activity へ引き継がれる（"none" で作り直さない・question の中へ入れ子にしない）。
    停止ターンは対象外（Codex 経路では停止したターンの assistant は保存されない）。"""
    from sherpa import app_version as AV

    provider_activity = {"v": 1, "source": "codex_rollout", "settings": {},
                         "phases_ms": {"prepare": 5, "agent": 15}, "agents": []}
    question_event = {"type": "question", "interaction_id": "q1", "mode": "single",
                      "prompt": "確認したいことがあります。",
                      "options": [{"id": "yes", "label": "はい", "description": ""},
                                  {"id": "no", "label": "いいえ", "description": ""}],
                      "allow_free_text": False, "activity": provider_activity}

    saved = _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(CS, "plain_provider_for", lambda *a, **k: None)
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeChatProvider([question_event]))
    events = list(CS.stream_message(None, "確認質問経路のテスト", world="v1", conversation_id=999,
                                    user_id="admin", knowledge=False))
    assert any(e.get("type") == "question" for e in events)
    answer = saved[-1]["answer"]
    assert answer["lens"] == "clarify"
    assert "activity" not in answer["question"]
    activity = answer["activity"]
    assert activity["source"] == "codex_rollout"
    assert activity["app_version"] == AV.current()
    assert activity["phases_ms"]["prepare"] == 5 and activity["phases_ms"]["agent"] == 15
    assert activity["phases_ms"]["total"] == answer["duration_ms"]
    assert activity["phases_ms"]["post"] == max(0, answer["duration_ms"] - 5 - 15)
