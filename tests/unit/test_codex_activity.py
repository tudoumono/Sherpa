"""`sherpa/providers/codex/activity.py`（利用統計の刷新 提案書
`docs/proposals/2026-09-23-利用統計の刷新.md` §3.1・S1）のテスト。受入条件の再現に絞る
（record 種類ごとの個別テストや内部ヘルパ単体テストは作らない・公開の `summarize_turn` と
偽 Codex/フェイク provider の実行結果だけで確かめる）。

1. `test_summarize_turn_matches_v1_shape`: 1つの偽セッション記録一式（親1体=resume で前ターンの
   行を含む＋子2体=新形式）から §3.1 の形どおりの要約ができることと、要約 JSON に引数・結果
   本文・資料名が一切出ないことを1回でまとめて確認する。末尾で resume 失敗→フォールバックの
   複数親スレッドを1つの parent エントリへ合算することも確認する。
2. `test_fake_codex_success_turn_has_activity_with_children_matching_usage_breakdown`:
   偽 Codex 実行（成功）で `env["activity"]` が載ること・子の検出数が既存の
   `_collect_child_token_usage`（`env["codex_usage_children"]`）と一致すること・
   `env["usage"]` が変わらないこと。
3. `test_fake_codex_context_window_exceeded_turn_has_nonzero_activity_tokens`:
   偽 Codex 実行（`turn.failed`=`context_window_exceeded`）でも `env["activity"]` が載り、
   トークンがセッション記録どおり0でないこと。
4. `test_chat_service_fills_app_version_and_phases_ms_for_both_paths`:
   `chat_service.handle_message`/`stream_message`（保存直前）で `activity.app_version`／
   `phases_ms.total`（=duration_ms）／`phases_ms.post`（=total-prepare-agent）が埋まること
   （provider が activity を作らない経路の最小形 `{v:1, source:"none", phases_ms:{"total":...}}`
   も含む）。
5. `test_confirm_question_turn_carries_provider_activity_into_saved_card`（新規）:
   Codex が確認の質問を返したターンで、provider が作った activity を確認カードの answer へ
   引き継ぐ（質問保存側が source:"none" で作り直さない）。停止ターンでの activity 引継ぎは
   対象外（Codex 経路では停止したターンの assistant は保存されないため停止応答へ足しても
   統計に効かない・停止ターンの消費の保存先は別途の決め事として持ち越す）。
6. `test_mcp_call_recorded_twice_counts_once_and_keeps_parent`: 実 CLI は Sherpa の MCP ツールの
   1回の呼出しを `function_call`（name＝ツール名・namespace＝`mcp__sherpa`）と `mcp_tool_call_end`
   （同じ call_id）の両方に書く。旧実装はこの並びで KeyError を出して親の要約ごと失っていた
   （実害の再現）。1回として数え、親の要約が残ることを確かめる。
7. `test_exec_command_errors_and_sandbox_errors_counted_without_leaking_body`（サンドボックスの
   実行中検知）: 実物の形式（`custom_tool_call` name="exec"・output は `tools.exec_command()` の
   JSON ブロック＝実測 codex-cli 0.153.4）の終了コード 0（成功）・127（一般失敗）・
   サンドボックス起因（bwrap の RTM_NEWADDR・通常の JSON にならない想定）を数え分け、
   コマンドの本文・エラーメッセージが要約 JSON に一切出ないことを確認する。

偽 Codex は `tests/unit/test_codex_usage_delta.py`/`test_codex_workspace_authoring.py` と同じ
「PATH に偽 `codex` 実行ファイルを差し込む」流儀（実 codex は一切呼ばない）。chat_service 側は
`tests/unit/test_chat_service.py` と同じ「store をフェイク差し替え」流儀（PG 不要）。
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


# ===== 記録の組み立てヘルパ（1レコード1行の JSON・実 CLI 0.153.4/0.147.0 で確認した構造） =====

def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


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


def _mcp_tool_call_end_protocol_error(ts, *, tool):
    return {"type": "event_msg", "timestamp": _iso(ts), "payload": {
        "type": "mcp_tool_call_end", "invocation": {"server": "sherpa", "tool": tool},
        "duration": {"secs": 0, "nanos": 0}, "result": {"Err": "transport failure"}}}


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


def _touch_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


def test_mcp_call_recorded_twice_counts_once_and_keeps_parent(tmp_path):
    codex_home = tmp_path / "codexhome"
    base = time.time()
    first, second = json.dumps({"hits": [1, 2]}), json.dumps({"hits": [], "truncated": True})
    records = [
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
    ]
    _write_jsonl(codex_home / "sessions" / "2099" / "01" / "01" / "rollout-parent-mcp.jsonl", records)

    result = AC.summarize_turn(codex_home, parent_thread_ids=["parent-mcp"], child_thread_ids=set(),
                               turn_started_wall=base, settings={}, phases_ms={"prepare": 0, "agent": 0})

    assert [a["role"] for a in result["agents"]] == ["parent"]
    tools = result["agents"][0]["tools"]
    assert tools["ripgrep_search"]["calls"] == 2
    assert tools["ripgrep_search"]["bytes"] == len(first) + len(second)
    assert tools["ripgrep_search"]["truncated"] == 1
    assert tools["exec_command"] == {"calls": 1, "bytes": 2, "max_bytes": 2}


def test_exec_command_errors_and_sandbox_errors_counted_without_leaking_body(tmp_path):
    """実物の形式（`custom_tool_call` name="exec"・output は `tools.exec_command()` の JSON
    ブロック＝実測 codex-cli 0.153.4）から、終了コード 0（成功）・127（一般失敗）・サンドボックス
    起因（bwrap の RTM_NEWADDR・起動失敗のため通常の JSON にならない想定）の3件を数え分ける。
    成功（0）は errors に数えない（`_new_tool_stats_basic` の「測れたときだけキーを置く」契約）。
    本文（コマンド出力・bwrap のエラーメッセージ）は要約（tools 辞書・summarize_turn の戻り値
    全体）に一切現れない——calls/errors/sandbox_errors のような閉じた語彙の整数キーだけを持つ。"""
    codex_home = tmp_path / "codexhome_exec"
    base = time.time()
    secret_output = "SECRET-TOKEN-should-not-leak-into-summary"
    records = [
        _session_meta("parent-exec"),
        _turn_context(base + 1, "gpt-6-astra"),
        _token_count(base + 2, inp=10, cached=0, out=5, reasoning=0),
        # 成功（exit_code=0）——errors/sandbox_errors のどちらにも数えない。
        _custom_tool_call(base + 3, name="exec", call_id="e1"),
        _custom_tool_call_output(base + 4, call_id="e1", blocks=[
            {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
            {"type": "input_text",
             "text": json.dumps({"chunk_id": "a", "exit_code": 0, "output": secret_output})},
        ]),
        # 一般の失敗（exit_code=127・rg 欠落等）——errors に数える／サンドボックス起因ではない。
        _custom_tool_call(base + 5, name="exec", call_id="e2"),
        _custom_tool_call_output(base + 6, call_id="e2", blocks=[
            {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
            {"type": "input_text",
             "text": json.dumps({"chunk_id": "b", "exit_code": 127, "output": "rg: not found"})},
        ]),
        # サンドボックス起因（bwrap 自体が起動失敗——実機未再現のため通常の exit_code JSON には
        # ならない想定で作る＝終了コードが読めなくても sandbox_errors は独立に計上できることの確認）。
        _custom_tool_call(base + 7, name="exec", call_id="e3"),
        _custom_tool_call_output(base + 8, call_id="e3", blocks=[
            {"type": "input_text", "text": "bwrap: Failed RTM_NEWADDR: Operation not permitted"},
        ]),
        # 成功したコマンドの本文にサンドボックスの語が含まれていても数えない（誤警告を出さない）。
        _custom_tool_call(base + 9, name="exec", call_id="e4"),
        _custom_tool_call_output(base + 10, call_id="e4", blocks=[
            {"type": "input_text",
             "text": json.dumps({"chunk_id": "d", "exit_code": 0, "output": "bwrap: execvp RTM_NEWADDR"})},
        ]),
    ]
    _write_jsonl(codex_home / "sessions" / "2099" / "01" / "01" / "rollout-parent-exec.jsonl", records)

    result = AC.summarize_turn(codex_home, parent_thread_ids=["parent-exec"], child_thread_ids=set(),
                               turn_started_wall=base, settings={}, phases_ms={"prepare": 0, "agent": 0})

    tools = result["agents"][0]["tools"]
    assert tools["exec"]["calls"] == 4
    assert tools["exec"]["errors"] == 1                                      # 127 の1件だけ（0は数えない）
    assert tools["exec"]["sandbox_errors"] == 1                              # bwrap の1件だけ
    assert set(tools["exec"].keys()) == {"calls", "bytes", "max_bytes", "errors", "sandbox_errors"}

    dumped = json.dumps(result, ensure_ascii=False)
    assert secret_output not in dumped
    assert "RTM_NEWADDR" not in dumped
    assert "rg: not found" not in dumped


# ===== 1. summarize_turn: 形・フィルタ・打切り・引数/本文の非漏洩を1つのfixtureでまとめて確認 =====

def test_summarize_turn_matches_v1_shape(tmp_path, monkeypatch):
    """受入条件1・2。親1体（resume＝前ターンの行を含む）＋子2体（新形式）の1つの偽セッション
    記録一式から §3.1 の形どおりの要約ができる。ターン開始より前の行は数えない（親・子とも・
    RV是正）。往復1000件の打切りは rounds 配列の大きさだけを抑え、tokens 集計はファイル最後まで
    続ける（子の1体で確認・RV是正）。1000件超の圧縮位置は実際の往復数で決まる（RV是正）。不正な
    token 値（bool を含む非 int）の行は unparsed へ計上し、その往復だけを飛ばす（RV是正）。型が
    壊れた `type`（[]/{}）を持つ行も unparsed へ計上して落とさない（RV是正）。timestamp が読めない
    行は今回分と推測せず unparsed へ計上する（RV是正）。function_call 系のツール統計は
    calls/bytes/max_bytes だけ（測っていない errors 等のキーを0で残さない・RV是正）。要約 JSON に
    引数・結果本文・資料名が一切入らない。末尾で resume 失敗→フォールバックの複数親を1つの
    parent エントリへ合算することも確認する（RV是正）。候補ファイルの stat() が1件失敗しても
    （壊れたシンボリックリンク）他の候補の集計は失わない（RV是正）。web_search_call は
    calls だけの1キー構成で数える（RV是正）。子の本文を読んでいる途中の OSError（EIO 等）でも
    親の要約は残る（`open` だけを差し替える外部境界のモックで再現・RV是正）。"""
    codex_home = tmp_path / "codexhome"
    base = time.time()
    parent_id = "parent-thread-1"
    child_small_id = "child-thread-small"
    child_cap_id = "child-thread-cap"
    secret_query = "SECRET_QUERY_極秘資料名.txt"
    secret_body = "SECRET_BODY_取引先個人情報"

    parent_records = [
        _session_meta(parent_id),
        # 前ターン（turn_started より前）＝数えない。
        _turn_context(base - 500, "gpt-5.4-old"),
        _token_count(base - 500, inp=9999, cached=9999, out=9999, reasoning=9999),
        _mcp_tool_call_end(base - 500, tool="old_tool", text=json.dumps({"ok": True})),
        # 今ターン: token_count／mcp_tool_call_end（成功・isError・プロトコル層失敗・
        # text_truncated／truncated）／compacted／function_call＋output／custom_tool_call＋output／
        # 未知の種類、を一通り含める。
        _turn_context(base + 1, "gpt-5.5-test"),
        _token_count(base + 2, inp=100, cached=20, out=30, reasoning=5),
        _mcp_tool_call_end(base + 3, tool="ripgrep_search",
                           text=json.dumps({"hits": [{"text": secret_body}], "query": secret_query,
                                            "text_truncated": True})),
        _mcp_tool_call_end(base + 4, tool="ripgrep_search", text=json.dumps({"hits": [1, 2, 3]})),
        _mcp_tool_call_end(base + 5, tool="read_doc", text=json.dumps({"error": "not_found"}),
                           is_error=True),
        _mcp_tool_call_end_protocol_error(base + 6, tool="es_search"),
        _mcp_tool_call_end(base + 7, tool="glob_search", text=json.dumps({"matches": [], "truncated": True})),
        _compacted(base + 8),
        _token_count(base + 9, inp=40, cached=5, out=10, reasoning=1),
        _function_call(base + 10, name="exec_command", call_id="call-1"),
        _function_call_output(base + 11, call_id="call-1", output=f"cat {secret_query}\n{secret_body}"),
        _custom_tool_call(base + 12, name="apply_patch", call_id="call-2"),
        _custom_tool_call_output(base + 13, call_id="call-2",
                                 blocks=[{"type": "input_text", "text": "patch applied ok"}]),
        # Web検索（チャットごとに利用者が選べる機能）の呼出し——response_item に結果本文が
        # 無く大きさを測れないため、calls だけを数える（RV是正）。
        {"type": "response_item", "timestamp": _iso(base + 13.5), "payload": {"type": "web_search_call"}},
        # 未知の種類（CLI の形式変化）＝落とさず unparsed へ計上。
        {"type": "event_msg", "timestamp": _iso(base + 14), "payload": {"type": "future_event_kind"}},
        {"type": "some_future_top_level_kind", "timestamp": _iso(base + 15), "payload": {}},
        # 型が壊れた type（[]/{} は hashable でないため frozenset の `in` 照合で TypeError になり
        # 得る・RV是正）——3箇所（event_msg.type／response_item.type／トップレベル type）とも
        # unparsed["invalid_type"] へ計上して次行へ進む。
        {"type": "event_msg", "timestamp": _iso(base + 14.1), "payload": {"type": []}},
        {"type": "response_item", "timestamp": _iso(base + 14.2), "payload": {"type": {}}},
        {"type": {"nested": "bad"}, "timestamp": _iso(base + 14.3), "payload": {}},
    ]
    # 不正な token 値を含む token_count 行を2種（RV是正）。
    # (a) 文字列: 旧実装 `int(last.get(k) or 0)` は `int("not-a-number")` で ValueError を
    #     ファイルの外まで上げ、このターンの activity 全体を失っていた（実害の再現）。
    # (b) bool: `True`/`False` は `int` の部分型だが数値として扱わない——`int(True or 0)` は
    #     例外を出さず静かに `1` へ丸めてしまう（旧実装は素通し・boolを除く明示要件の確認）。
    # いずれもこの往復だけ unparsed へ計上して飛ばし、前後の正常な往復は集計に残る。
    bad_token_count_line_str = json.dumps(
        {"type": "event_msg", "timestamp": _iso(base + 17), "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": "not-a-number", "cached_input_tokens": 1, "output_tokens": 1,
                "reasoning_output_tokens": 0}}}}, ensure_ascii=False)
    bad_token_count_line_bool = json.dumps(
        {"type": "event_msg", "timestamp": _iso(base + 18), "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": True, "cached_input_tokens": 1, "output_tokens": 1,
                "reasoning_output_tokens": 0}}}}, ensure_ascii=False)
    # timestamp が読めない行（min_epoch フィルタ中）は「今回分」と推測せず unparsed["timestamp"]
    # へ計上して飛ばす（RV是正）——大きな値（500）を混ぜて、数えてしまったら他の assert で
    # すぐ分かるようにする。
    bad_timestamp_line = json.dumps(
        {"type": "event_msg", "timestamp": "not-a-valid-timestamp", "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 500, "cached_input_tokens": 500, "output_tokens": 500,
                "reasoning_output_tokens": 500}}}}, ensure_ascii=False)

    parent_path = codex_home / "sessions" / "2099" / "01" / "01" / f"rollout-{parent_id}.jsonl"
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    with open(parent_path, "wb") as f:
        for rec in parent_records:
            f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        # 不正 UTF-8 バイト列を含む行（実装の回帰確認・前後の正常行は読める＝fail-open）。
        f.write(b"\xff\xfe not valid utf-8\n")
        f.write((json.dumps(_token_count(base + 16, inp=7, cached=0, out=0, reasoning=0),
                            ensure_ascii=False) + "\n").encode("utf-8"))
        f.write((bad_token_count_line_str + "\n").encode("utf-8"))
        f.write((bad_token_count_line_bool + "\n").encode("utf-8"))
        f.write((bad_timestamp_line + "\n").encode("utf-8"))
    _touch_mtime(parent_path, base + 100)

    child_small = [
        _session_meta(child_small_id, parent_thread_id=parent_id, thread_source="subagent"),
        _turn_context(base + 1, "gpt-5.4-mini"),
        # 前のターンの残存行（RV是正: 子にも min_epoch=turn_started_wall を渡す——数えたら
        # small の input_tokens が 11 でなく 8899 になり、下の assert で気付ける）。
        _token_count(base - 500, inp=8888, cached=8888, out=8888, reasoning=8888),
        _token_count(base + 2, inp=11, cached=2, out=3, reasoning=0),
        _mcp_tool_call_end(base + 3, tool="read_around", text=json.dumps({"ok": True})),
    ]
    small_path = codex_home / "sessions" / "2099" / "01" / "01" / f"rollout-{child_small_id}.jsonl"
    _write_jsonl(small_path, child_small)
    _touch_mtime(small_path, base + 101)

    # 往復1000件打切り（受入条件1）は子のもう1体で確認する（親は種類の網羅に専念させる）。
    # 1002件の後に compacted を1件挟む——round_count（配列とは独立の実カウント）が1002の
    # 時点なので、圧縮位置は1003になるはず（RV是正・旧実装は len(rounds) が1000で頭打ちのため
    # 1001に固定されていた）。
    cap_records = [_session_meta(child_cap_id, parent_thread_id=parent_id, thread_source="subagent")]
    for i in range(1002):
        cap_records.append(_token_count(base + 1 + i * 0.01, inp=1, cached=0, out=0, reasoning=0))
    cap_records.append(_compacted(base + 1 + 1002 * 0.01))
    cap_records.append(_token_count(base + 1 + 1003 * 0.01, inp=1, cached=0, out=0, reasoning=0))
    cap_path = codex_home / "sessions" / "2099" / "01" / "01" / f"rollout-{child_cap_id}.jsonl"
    _write_jsonl(cap_path, cap_records)
    _touch_mtime(cap_path, base + 102)

    # RV是正: session_meta.payload.id が空でない文字列でない記録（[] 等）が混ざっていても、
    # 集合/辞書との照合が TypeError にならず読み飛ばす——正常な親の要約まで失わないことを確認する。
    bad_id_records = [{"type": "session_meta", "payload": {"id": []}},
                      _token_count(base + 2, inp=999, cached=999, out=999, reasoning=999)]
    bad_id_path = codex_home / "sessions" / "2099" / "01" / "01" / "rollout-bad-id.jsonl"
    _write_jsonl(bad_id_path, bad_id_records)
    _touch_mtime(bad_id_path, base + 103)

    # RV是正: 候補一覧に stat() が失敗するファイル（壊れたシンボリックリンク＝glob には載るが
    # stat() は FileNotFoundError）が混ざっていても、他の候補（親・子）の集計は失わない。
    broken_stat_path = codex_home / "sessions" / "2099" / "01" / "01" / "rollout-broken-stat.jsonl"
    broken_stat_path.symlink_to(codex_home / "sessions" / "2099" / "01" / "01" / "does-not-exist.jsonl")

    settings = {"provider": "codex", "config": "openai", "model": "gpt-5.5-test", "reasoning": "medium",
               "depth": "standard", "review_rounds": 2, "schema_level": 2, "multi_agent": True,
               "budget_per_result": 65536, "max_hits": 45, "window_cap": 60}
    phases_ms = {"prepare": 120, "agent": 5000}

    result = AC.summarize_turn(codex_home, parent_thread_ids=[parent_id], child_thread_ids=set(),
                               turn_started_wall=base, settings=settings, phases_ms=phases_ms)

    assert result["v"] == 1
    assert result["source"] == "codex_rollout"
    assert result["settings"] == settings
    assert result["phases_ms"] == phases_ms
    # RV是正: id が [] の記録（bad_id_records）や stat() が失敗する候補（broken_stat_path）が
    # 同じ codex_home に混ざっていても summarize_turn は例外を出さず（TypeError や sorted() の
    # 例外で activity 全体を失わず）、それらのファイル・候補だけを読み飛ばして3件
    # （parent 1・child 2）のまま——4件目として紛れ込んでもいない。
    assert [a["role"] for a in result["agents"]] == ["parent", "child", "child"]

    parent = result["agents"][0]
    assert parent["model"] == "gpt-5.5-test"                              # 前ターンの turn_context は数えない
    # トークンは今ターンの3 round 分だけ（前ターンの9999は数えない・不正バイト後の1件も数える・
    # timestamp が読めない500は数えない）。
    assert parent["tokens"] == {"input_tokens": 147, "cached_input_tokens": 25,
                                "output_tokens": 40, "reasoning_output_tokens": 6}
    assert parent["rounds"] == [[100, 20, 30, 5], [40, 5, 10, 1], [7, 0, 0, 0]]
    assert parent["compactions"] == [2]                                   # rounds=[100..] の次（2番目）に印

    tools = parent["tools"]
    assert tools["ripgrep_search"]["calls"] == 2
    assert tools["ripgrep_search"]["clipped"] == 1                        # text_truncated:true の1件だけ
    assert tools["read_doc"]["errors"] == 1                               # isError:true
    assert tools["es_search"]["errors"] == 1                              # result.Err（プロトコル層失敗）
    assert tools["glob_search"]["truncated"] == 1                         # truncated:true
    assert tools["exec_command"]["calls"] == 1
    assert tools["exec_command"]["bytes"] == len(f"cat {secret_query}\n{secret_body}".encode("utf-8"))
    assert tools["apply_patch"]["calls"] == 1
    assert tools["apply_patch"]["bytes"] == len("patch applied ok".encode("utf-8"))
    # RV是正: function_call/custom_tool_call 系は測っていない errors/clipped/truncated/ms を
    # 0で残さない（calls/bytes/max_bytes だけ）。MCP ツール（mcp_tool_call_end）は全7キーのまま。
    assert set(tools["exec_command"].keys()) == {"calls", "bytes", "max_bytes"}
    assert set(tools["apply_patch"].keys()) == {"calls", "bytes", "max_bytes"}
    assert set(tools["ripgrep_search"].keys()) == {
        "calls", "bytes", "max_bytes", "clipped", "truncated", "errors", "ms"}
    # RV是正: web_search_call は結果本文が無く大きさを測れないため、calls だけの1キー構成
    # （bytes/max_bytes 等を0で埋めない）。
    assert tools["web_search"] == {"calls": 1}

    assert parent["unparsed"].get("event_msg:future_event_kind") == 1
    assert parent["unparsed"].get("some_future_top_level_kind") == 1
    assert parent["unparsed"].get("invalid_json") == 1                    # 不正バイト行自体は1件だけ記録
    # RV是正: 数値でない token 値（文字列・bool）の往復は unparsed["token_count"] へ2件計上し
    # 飛ばす——tokens/rounds は不正往復を含まない（147/25/40/6・3 round のまま＝上のassertと不変）。
    assert parent["unparsed"].get("token_count") == 2
    # RV是正: 型が壊れた type（event_msg/response_item/トップレベル）は3件とも invalid_type へ。
    assert parent["unparsed"].get("invalid_type") == 3
    # RV是正: timestamp が読めない行は今回分と推測せず timestamp へ計上する（500 は tokens に
    # 混ざっていない＝上の parent["tokens"] assert が不変のまま通ることでも確認できる）。
    assert parent["unparsed"].get("timestamp") == 1

    children = result["agents"][1:]
    small = next(c for c in children if c["tokens"]["input_tokens"] == 11)
    assert small["model"] == "gpt-5.4-mini"
    capped = next(c for c in children if c is not small)
    # RV是正: 往復1000件の打切りは rounds 配列の大きさだけ（1000）——tokens 集計は1003行分
    # 全てを含む（1000で打ち切っていない）。
    assert len(capped["rounds"]) == 1000
    assert capped["tokens"]["input_tokens"] == 1003
    # RV是正: 1000件超の圧縮位置は実際の往復数（round_count）で決まる——1002件目の後に
    # 圧縮を挟んだので位置は1003（`len(rounds)` を使う旧実装だと1000で頭打ちのため1001に固定）。
    assert capped["compactions"] == [1003]

    # 受入条件2: 引数・結果本文・資料名が要約 JSON に一切出ない。
    dumped = json.dumps(result, ensure_ascii=False)
    assert secret_query not in dumped
    assert secret_body not in dumped
    assert "ripgrep_search" in dumped and "exec_command" in dumped        # ツール名は残る（契約どおり）

    # ===== 受入条件1（RV是正）: resume失敗→フォールバックで複数の親スレッドを使ったターンは
    # 1つの parent エントリへ合算する（tokensは和・roundsは時刻順連結・toolsは項目ごとの和＋
    # max_bytesだけ最大・compactionsは前の親の往復数だけずらす・modelは最後の親）。=====
    merge_home = tmp_path / "codexhome_merge"
    parent_a_id, parent_b_id = "parent-a-failed-resume", "parent-b-fallback"
    piece_a = [
        _session_meta(parent_a_id),
        _turn_context(base + 1, "gpt-5.4-old-resume"),
        _token_count(base + 2, inp=10, cached=1, out=1, reasoning=0),
        _compacted(base + 3),   # piece_a 単独の round_count=1 の直後＝piece_a 内では位置2
        _token_count(base + 4, inp=20, cached=2, out=2, reasoning=0),
        _mcp_tool_call_end(base + 5, tool="ripgrep_search", text=json.dumps({"hits": []})),
    ]
    piece_a_path = merge_home / "sessions" / "2099" / "01" / "01" / f"rollout-{parent_a_id}.jsonl"
    _write_jsonl(piece_a_path, piece_a)
    _touch_mtime(piece_a_path, base + 10)

    piece_b = [
        _session_meta(parent_b_id),
        _turn_context(base + 6, "gpt-5.4-fallback-fresh"),
        _token_count(base + 7, inp=100, cached=10, out=10, reasoning=1),
        # bytes が piece_a より大きい＝max_bytes はこちらが勝つはず。
        _mcp_tool_call_end(base + 8, tool="ripgrep_search", text=json.dumps({"hits": [1, 2, 3, 4, 5]})),
    ]
    piece_b_path = merge_home / "sessions" / "2099" / "01" / "01" / f"rollout-{parent_b_id}.jsonl"
    _write_jsonl(piece_b_path, piece_b)
    _touch_mtime(piece_b_path, base + 20)

    merged = AC.summarize_turn(merge_home, parent_thread_ids=[parent_a_id, parent_b_id],
                               child_thread_ids=set(), turn_started_wall=base,
                               settings={}, phases_ms={"prepare": 0, "agent": 0})
    assert [a["role"] for a in merged["agents"]] == ["parent"]   # 二重に数えず1エントリへ合算
    m = merged["agents"][0]
    assert m["model"] == "gpt-5.4-fallback-fresh"                # 最後の親（piece_b）の値
    assert m["tokens"] == {"input_tokens": 130, "cached_input_tokens": 13,
                           "output_tokens": 13, "reasoning_output_tokens": 1}   # 10+20+100 等の和
    assert m["rounds"] == [[10, 1, 1, 0], [20, 2, 2, 0], [100, 10, 10, 1]]   # 時刻順（piece_a→piece_b）に連結
    assert m["compactions"] == [2]                                # piece_a 内の位置がそのまま（offset 0）
    m_bytes_a = len(json.dumps({"hits": []}).encode("utf-8"))
    m_bytes_b = len(json.dumps({"hits": [1, 2, 3, 4, 5]}).encode("utf-8"))
    assert m["tools"]["ripgrep_search"]["calls"] == 2
    assert m["tools"]["ripgrep_search"]["bytes"] == m_bytes_a + m_bytes_b
    assert m["tools"]["ripgrep_search"]["max_bytes"] == max(m_bytes_a, m_bytes_b)

    # ===== RV是正: (a) マージ時の圧縮位置のずらし幅は rounds 配列の長さ（1000で頭打ち）ではなく
    # そのピースの実際の往復数（_round_count）を使う。(b) 連結後の rounds も先頭1000件に再度
    # 切り詰める——契約は「rounds[i] はそのターンの実往復 i+1 番目・先頭1000件まで」。先のピース
    # だけで既に1000件に達しているので、後のピースの往復は配列に入らない（トークン合計・実往復数
    # による圧縮位置は再切り詰めの影響を受けない）。=====
    merge_home2 = tmp_path / "codexhome_merge_capped"
    parent_c_id, parent_d_id = "parent-c-capped", "parent-d-after-cap"
    piece_c = [_session_meta(parent_c_id), _turn_context(base + 30, "gpt-5.4-c")]
    for i in range(1002):   # 実往復数1002（rounds 配列は1000で頭打ち）
        piece_c.append(_token_count(base + 31 + i * 0.001, inp=1, cached=0, out=0, reasoning=0))
    piece_c_path = merge_home2 / "sessions" / "2099" / "01" / "01" / f"rollout-{parent_c_id}.jsonl"
    _write_jsonl(piece_c_path, piece_c)
    _touch_mtime(piece_c_path, base + 40)

    piece_d = [
        _session_meta(parent_d_id),
        _turn_context(base + 41, "gpt-5.4-d"),
        _compacted(base + 41.5),   # piece_d 単独の round_count=0 の時点＝piece_d 内の位置は1
        _token_count(base + 42, inp=5, cached=0, out=0, reasoning=0),
    ]
    piece_d_path = merge_home2 / "sessions" / "2099" / "01" / "01" / f"rollout-{parent_d_id}.jsonl"
    _write_jsonl(piece_d_path, piece_d)
    _touch_mtime(piece_d_path, base + 50)

    merged2 = AC.summarize_turn(merge_home2, parent_thread_ids=[parent_c_id, parent_d_id],
                                child_thread_ids=set(), turn_started_wall=base,
                                settings={}, phases_ms={"prepare": 0, "agent": 0})
    m2 = merged2["agents"][0]
    # piece_c だけで既に1000件（頭打ち）＝連結後の再切り詰めで piece_d の往復は配列に入らない。
    assert len(m2["rounds"]) == 1000
    assert m2["rounds"][-1] == [1, 0, 0, 0]   # piece_c の最後の往復のまま（piece_d の [5,0,0,0] ではない）
    # トークン合計は再切り詰めの影響を受けない（piece_c の1002件＋piece_d の1件＝1007）。
    assert m2["tokens"]["input_tokens"] == 1007
    # piece_d 内の位置（1）を piece_c の実往復数（1002・配列長1000ではない）でずらすと1003。
    assert m2["compactions"] == [1003]

    # ===== RV是正: 候補1件ぶんの処理（先頭行の読み取り・親子の判定・`_summarize_session_file`
    # 呼出）をまとめて fail-open にしたことの確認——子の本文を読んでいる途中の OSError（EIO 等）
    # でも、その候補（子）だけを読み飛ばして正常な親の要約は残る。`open` だけを差し替える外部
    # 境界のモック（summarize_turn/_summarize_session_file 自体は無改造で検証する）。=====
    class _EIOAfterFirstLine:
        """1行目（session_meta）は実ファイルどおりの内容を返し、以降の反復（`for line in f`）で
        OSError を起こす——本文を読んでいる途中の I/O 障害を模す。"""
        def __init__(self, first_line: str):
            self._first_line = first_line
            self._done = False

        def readline(self):
            if not self._done:
                self._done = True
                return self._first_line
            raise OSError(5, "Input/output error")

        def __iter__(self):
            return self

        def __next__(self):
            raise OSError(5, "Input/output error")   # 2行目以降（本文）の読み取り中の障害

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    eio_home = tmp_path / "codexhome_eio"
    eio_parent_id, eio_child_id = "parent-eio", "child-eio-flaky"
    eio_parent = [_session_meta(eio_parent_id), _turn_context(base + 60, "gpt-5.5-eio"),
                 _token_count(base + 61, inp=7, cached=1, out=1, reasoning=0)]
    eio_parent_path = eio_home / "sessions" / "2099" / "01" / "01" / f"rollout-{eio_parent_id}.jsonl"
    _write_jsonl(eio_parent_path, eio_parent)
    _touch_mtime(eio_parent_path, base + 70)

    eio_child = [_session_meta(eio_child_id, parent_thread_id=eio_parent_id, thread_source="subagent"),
                _token_count(base + 62, inp=999, cached=999, out=999, reasoning=999)]
    eio_child_path = eio_home / "sessions" / "2099" / "01" / "01" / f"rollout-{eio_child_id}.jsonl"
    _write_jsonl(eio_child_path, eio_child)
    _touch_mtime(eio_child_path, base + 71)
    eio_child_first_line = eio_child_path.read_text(encoding="utf-8").splitlines()[0]

    _real_open = open

    def _flaky_open(file, *args, **kwargs):
        if file == eio_child_path:
            return _EIOAfterFirstLine(eio_child_first_line)
        return _real_open(file, *args, **kwargs)

    monkeypatch.setattr(AC, "open", _flaky_open, raising=False)
    eio_result = AC.summarize_turn(eio_home, parent_thread_ids=[eio_parent_id], child_thread_ids=set(),
                                   turn_started_wall=base, settings={}, phases_ms={"prepare": 0, "agent": 0})
    # 子の本文読み取り中の OSError は summarize_turn の外へ伝播しない——その候補（子）だけが
    # 抜け、正常な親の要約はそのまま残る（子は1体も出ない＝parent だけ）。
    assert [a["role"] for a in eio_result["agents"]] == ["parent"]
    assert eio_result["agents"][0]["tokens"] == {"input_tokens": 7, "cached_input_tokens": 1,
                                                 "output_tokens": 1, "reasoning_output_tokens": 0}


# ===== 2/3. 偽 Codex 実行: 成功／turn.failed(context_window_exceeded) =====

_SUCCESS_SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

thread_id = "fake-parent-success-1"
print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
sys.stdout.flush()

now = time.time()
codex_home = pathlib.Path(os.environ["CODEX_HOME"])
sdir = codex_home / "sessions" / "2099" / "01" / "01"
sdir.mkdir(parents=True, exist_ok=True)

parent_lines = [
    json.dumps({"type": "session_meta", "payload": {"id": thread_id}}),
    json.dumps({"type": "turn_context", "timestamp": iso(now), "payload": {"model": "gpt-5.5-fake"}}),
    json.dumps({"type": "event_msg", "timestamp": iso(now), "payload": {
        "type": "token_count", "info": {"last_token_usage": {
            "input_tokens": 111, "cached_input_tokens": 22, "output_tokens": 33,
            "reasoning_output_tokens": 4}}}}),
    json.dumps({"type": "event_msg", "timestamp": iso(now), "payload": {
        "type": "mcp_tool_call_end",
        "invocation": {"server": "sherpa", "tool": "ripgrep_search"},
        "duration": {"secs": 0, "nanos": 50000000},
        "result": {"Ok": {"content": [{"type": "text", "text": json.dumps({"hits": []})}],
                          "isError": False}}}}),
]
(sdir / ("rollout-" + thread_id + ".jsonl")).write_text("\n".join(parent_lines) + "\n")

# 新形式の子2体（`_collect_child_token_usage`/`summarize_turn` 共通の parent_thread_id 突合）。
for i, child_id in enumerate(("fake-child-1", "fake-child-2")):
    usage = {"input_tokens": 10 + i, "cached_input_tokens": 1, "output_tokens": 2,
             "reasoning_output_tokens": 0}
    child_lines = [
        json.dumps({"type": "session_meta", "payload": {
            "id": child_id, "parent_thread_id": thread_id, "thread_source": "subagent"}}),
        json.dumps({"type": "event_msg", "timestamp": iso(now), "payload": {
            "type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage}}}),
    ]
    (sdir / ("rollout-" + child_id + ".jsonl")).write_text("\n".join(child_lines) + "\n")

print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message",
                                                      "text": "調べました。分かりました。"}}))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 50,
    "reasoning_output_tokens": 10}}))
sys.exit(0)
'''

_FAILURE_SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

thread_id = "fake-parent-failure-1"
print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
sys.stdout.flush()

now = time.time()
codex_home = pathlib.Path(os.environ["CODEX_HOME"])
sdir = codex_home / "sessions" / "2099" / "01" / "01"
sdir.mkdir(parents=True, exist_ok=True)
lines = [
    json.dumps({"type": "session_meta", "payload": {"id": thread_id}}),
    json.dumps({"type": "turn_context", "timestamp": iso(now), "payload": {"model": "gpt-5.5-fake"}}),
    json.dumps({"type": "event_msg", "timestamp": iso(now), "payload": {
        "type": "token_count", "info": {"last_token_usage": {
            "input_tokens": 90000, "cached_input_tokens": 1000, "output_tokens": 200,
            "reasoning_output_tokens": 50}}}}),
]
(sdir / ("rollout-" + thread_id + ".jsonl")).write_text("\n".join(lines) + "\n")

print(json.dumps({"type": "turn.failed", "error": {
    "code": "boom", "codex_error_info": "context_window_exceeded",
    "message": "Error running remote compact task: Codex ran out of room"}}))
sys.exit(0)
'''


def _write_fake_codex(bin_dir: Path, script_body: str) -> None:
    script = bin_dir / "codex"
    script.write_text(script_body)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _activity_setup(bin_dir: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")   # 平文の偽 codex＝構造化応答は使わない


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
    """受入条件3・4（成功ターン）。env["activity"] が載り、親エージェントのトークンがセッション
    記録どおり。子の検出数（activity.agents の child 件数）は既存の _collect_child_token_usage
    （env["codex_usage_children"]）と一致する——同じ偽 Codex 実行結果から得た2つの観測値の一致で
    判定共有を確認する（内部関数は直接呼ばない）。env["usage"]（既存契約）は変更前と同じ
    turn.completed の値のまま。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _activity_setup(bin_dir, monkeypatch, tmp_path)
    _write_fake_codex(bin_dir, _SUCCESS_SCRIPT)

    # RV是正: prepare は ctx.turn_started_mono（chat_service がプロバイダ呼出し前に取る起点）
    # から測る——ここで明示的に渡し、実際に使われることを確かめる。
    turn_started_mono = time.monotonic()
    ctx = _activity_ctx("activity-success-u1", turn_started_mono=turn_started_mono)
    env = _result_env(list(A.CodexProvider().run(ctx)))

    activity = env.get("activity")
    assert activity is not None, f"activity が載っていない: {env!r}"
    assert activity["v"] == 1 and activity["source"] == "codex_rollout"
    assert "prepare" in activity["phases_ms"] and activity["phases_ms"]["prepare"] >= 0
    assert "agent" in activity["phases_ms"] and activity["phases_ms"]["agent"] >= 0
    parent = next(a for a in activity["agents"] if a["role"] == "parent")
    assert parent["tokens"] == {"input_tokens": 111, "cached_input_tokens": 22,
                                "output_tokens": 33, "reasoning_output_tokens": 4}
    assert parent["tools"]["ripgrep_search"]["calls"] == 1

    activity_children = [a for a in activity["agents"] if a["role"] == "child"]
    assert len(activity_children) == env["codex_usage_children"]["found"] == 2

    usage = env.get("usage")
    assert usage is not None
    # 既存契約（DEPTH-2 S3b）: 子が見つかると env["usage"] は親（turn.completed）＋子2体の合計に
    # なる——500+10+11=521・50+2+2=54（この合算ロジック自体は変更していない・混入していないことの確認）。
    assert usage["input_tokens"] == 521 and usage["output_tokens"] == 54


def test_fake_codex_context_window_exceeded_turn_has_nonzero_activity_tokens(tmp_path, monkeypatch):
    """受入条件4（失敗ターン）。turn.failed(context_window_exceeded) でも env["activity"] が載り、
    トークンがセッション記録どおり0でない。env["usage"] は従来どおり載らない（変更していない）。
    `ctx.turn_started_mono` を渡さない経路（RV是正: Ctx に値が無ければ prepare は測らない）——
    `agent` は常に測れる一方で `prepare` はキー自体が無いことを確認する。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _activity_setup(bin_dir, monkeypatch, tmp_path)
    _write_fake_codex(bin_dir, _FAILURE_SCRIPT)

    ctx = _activity_ctx("activity-failure-u1")   # turn_started_mono 省略＝None
    env = _result_env(list(A.CodexProvider().run(ctx)))

    assert env.get("codex_error_code") == "context_window_exceeded"
    activity = env.get("activity")
    assert activity is not None, f"失敗ターンで activity が載っていない: {env!r}"
    assert "prepare" not in activity["phases_ms"]        # turn_started_mono が無いので測らない
    assert "agent" in activity["phases_ms"]               # agent は常に測れる
    parent = next(a for a in activity["agents"] if a["role"] == "parent")
    assert parent["tokens"]["input_tokens"] == 90000   # セッション記録どおり0でない

    # answer["usage"] は turn.completed が一度も来ていないため従来どおり None のまま（変更していない）。
    assert env.get("usage") is None


# ===== 4. chat_service: 保存直前に app_version/phases_ms を埋める（全経路共通） =====

def _mock_store_no_db(monkeypatch):
    """`tests/unit/test_chat_service.py::_mock_store_no_db` と同じ流儀（PG 不要）。
    戻り値は `store.add_message` に渡された行を挿入順に保持するリスト。"""
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
    """`get_provider(settings)` の代わりに使うフェイク（固定イベント列を yield するだけ）。"""

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
    """受入条件5。provider が activity を作らない経路（API／Ollama 相当・handle_message）は保存
    直前に `{v:1, source:"none", app_version, phases_ms:{"total":duration_ms}}` が入る——
    prepare/agent/post は測っていないのでキー自体を置かない（0埋めで「全部が後処理」という
    誤ったデータにしない・RV是正）。provider が activity（prepare/agent 入り）を作った経路
    （stream_message）はそれを保ったまま `app_version`／`phases_ms.total`／`phases_ms.post`
    （=total-prepare-agent）を保存直前に埋める。"""
    from sherpa import app_version as AV

    _mock_store_no_db(monkeypatch)
    events_none = [_fixed_chat_result("provider に activity 無し")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeChatProvider(events_none))
    out = CS.handle_message(None, "activity 無し経路のテスト", world="v1", conversation_id=999,
                            user_id="admin", knowledge=False)
    answer_none = out["message"]["answer"]
    activity_none = answer_none["activity"]
    assert activity_none["v"] == 1 and activity_none["source"] == "none"
    assert activity_none["app_version"] == AV.current()
    phases_none = activity_none["phases_ms"]
    # RV是正: none 経路は total だけ（prepare/agent/post は0埋めせずキー自体を置かない）。
    assert phases_none == {"total": answer_none["duration_ms"]}
    assert "prepare" not in phases_none and "agent" not in phases_none and "post" not in phases_none

    saved = _mock_store_no_db(monkeypatch)
    provider_activity = {"v": 1, "source": "codex_rollout", "settings": {"model": "gpt-5.5-test"},
                         "phases_ms": {"prepare": 120, "agent": 340}, "agents": []}
    events_full = [_fixed_chat_result("provider に activity 有り", activity=provider_activity)]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeChatProvider(events_full))
    list(CS.stream_message(None, "activity 有り経路のテスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))
    answer_full = saved[-1]["answer"]
    activity_full = answer_full["activity"]
    assert activity_full["source"] == "codex_rollout"                    # provider の値を保つ
    assert activity_full["settings"] == {"model": "gpt-5.5-test"}        # provider の値を保つ
    assert activity_full["app_version"] == AV.current()
    phases_full = activity_full["phases_ms"]
    assert phases_full["prepare"] == 120 and phases_full["agent"] == 340   # provider の値を保つ
    assert phases_full["total"] == answer_full["duration_ms"]
    assert phases_full["post"] == max(0, phases_full["total"] - 120 - 340)
    assert all(phases_full[k] >= 0 for k in ("prepare", "agent", "post", "total"))


# ===== 5. 確認の質問ターンでも provider の activity を捨てない =====
# 既存の4本では確かめられない経路（provider が question イベントで終わり `_result` に至らない）
# のため、新規関数として追加する（目安6本以内）。
#
# 停止ターンでの activity 引継ぎは対象外（意図的）: Codex 経路では停止したターンの assistant は
# 保存されない契約のため、停止応答に answer.activity を足しても DB に残らず統計に効かない一方で
# 応答の形だけが変わる。停止ターンの消費をどこに保存するかは別途の決め事として持ち越す。

def test_confirm_question_turn_carries_provider_activity_into_saved_card(monkeypatch):
    """Codex が確認の質問を返したターンでは、provider が作った activity が question イベント
    経由で確認カードの answer へ引き継がれる（質問の保存側が source:"none" で作り直さない・
    question の中へ入れ子にもしない＝answer.activity 直下）。"""
    from sherpa import app_version as AV

    provider_activity = {"v": 1, "source": "codex_rollout", "settings": {},
                         "phases_ms": {"prepare": 5, "agent": 15}, "agents": []}
    question_event = {"type": "question", "interaction_id": "q1", "mode": "single",
                      "prompt": "確認したいことがあります。",
                      "options": [{"id": "yes", "label": "はい", "description": ""},
                                 {"id": "no", "label": "いいえ", "description": ""}],
                      "allow_free_text": False, "activity": provider_activity}

    saved = _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(CS, "get_provider",
                        lambda settings, **kw: _FakeChatProvider([question_event]))
    out = CS.handle_message(None, "確認質問経路のテスト", world="v1", conversation_id=999,
                            user_id="admin", knowledge=False)
    assert out["message"] is saved[-1]
    answer = saved[-1]["answer"]
    assert answer["lens"] == "clarify"
    assert "activity" not in answer["question"]   # question 配下に入れ子にしない
    activity = answer["activity"]
    assert activity["source"] == "codex_rollout"   # provider の値をそのまま保つ（"none" で作り直さない）
    assert activity["app_version"] == AV.current()
    assert activity["phases_ms"]["prepare"] == 5 and activity["phases_ms"]["agent"] == 15
    assert activity["phases_ms"]["total"] == answer["duration_ms"]
    assert activity["phases_ms"]["post"] == max(0, answer["duration_ms"] - 5 - 15)
