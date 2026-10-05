"""Codex セッション記録（rollout JSONL）から1ターン分の活動要約（`answer["activity"]`）を作る。読むだけで、`answer["usage"]`／codex.log には触らない。
要約にはツール名・件数・バイト数・時間だけを含める（本文・資料名・引数は含めない）。
行・ファイル単位で fail-open（読めない行・未知の種類・壊れた `type` は `unparsed` に計上し、開けないファイルは読み飛ばす）。
子（下調べ役）の判定は `_is_child_session_meta` の1箇所だけで行う。
親スレッドは1ターンに複数使われうる（resume 失敗→新規スレッド）ため、`summarize_turn` は `parent_thread_ids`（時刻順）を受け取り `_merge_parent_pieces` で1つの `parent` に合算する。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

_log = logging.getLogger("sherpa")

_CHILD_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
_MAX_ROUNDS = 1000  # `rounds` 配列をこの件数で打ち切る（tokens/tools/compactions の集計は最後まで続ける）。

# 集計に寄与しない既知のペイロード種別。ここに無い種別は `unparsed` へ計上する。
_IGNORED_EVENT_MSG_TYPES = frozenset({
    "task_started", "task_complete", "item_completed", "thread_settings_applied",
    "turn_aborted", "thread_goal_updated", "user_message", "agent_message",
})
_IGNORED_RESPONSE_ITEM_TYPES = frozenset({
    "reasoning", "message", "agent_message", "compaction",
})
_IGNORED_TOP_TYPES = frozenset({
    "turn_context", "world_state", "session_meta",
    "inter_agent_communication_metadata", "token_usage_record",
})

# シェル実行系ツール（`function_call`／`custom_tool_call` の `name`）。`errors`／`sandbox_errors` はこの集合のツールにだけ足す（測れたときだけキーを置く）。
_EXEC_TOOL_NAMES = frozenset({"exec", "exec_command", "shell", "local_shell"})

# 行頭 "Exit code: N"。`_extract_exec_signal` が最後に試す。
_EXIT_CODE_LINE_RE = re.compile(r"(?im)^\s*exit code:\s*(-?\d+)\s*$")


def _is_child_session_meta(payload: dict | None, tid, *, child_thread_ids: set,
                            parent_thread_id: str | None, min_mtime: float | None,
                            file_mtime: float) -> bool:
    """このセッション記録（先頭行 `session_meta` の `payload`・`tid`）が「今回の子」と一致するか。
    新形式（`parent_thread_id` 一致・`thread_source=="subagent"`・mtime>=min_mtime）と旧形式（`tid` が `child_thread_ids` に含まれる）の和集合。単発の判定だけを行う。
    """
    if tid is None:
        return False
    is_old_match = tid in child_thread_ids
    is_new_match = (parent_thread_id is not None and not is_old_match
                     and isinstance(payload, dict)
                     and payload.get("parent_thread_id") == parent_thread_id
                     and payload.get("thread_source") == "subagent"
                     and (min_mtime is None or file_mtime >= min_mtime))
    return is_old_match or is_new_match


def _new_tool_stats_full() -> dict:
    """MCP ツール（`mcp_tool_call_end`）専用。clipped/truncated/errors/ms まで実測できる。"""
    return {"calls": 0, "bytes": 0, "max_bytes": 0, "clipped": 0, "truncated": 0, "errors": 0, "ms": 0}


def _new_tool_stats_basic() -> dict:
    """function_call／custom_tool_call／tool_search 専用。clipped/truncated/ms は測れない（推定で埋めない＝キーを置かない）。
    シェル実行系の `errors`／`sandbox_errors` だけ、終了コードが読めたときに `_commit_tool_calls` が足す。
    """
    return {"calls": 0, "bytes": 0, "max_bytes": 0}


def _new_tool_stats_calls_only() -> dict:
    """web_search_call 専用。結果本文が無いため calls だけを持つ。"""
    return {"calls": 0}


def _parse_epoch(ts) -> float | None:
    """ISO8601（'Z' 終端 UTC）文字列をエポック秒へ。読めない形式は None。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _text_bytes(text) -> int:
    if not isinstance(text, str):
        return 0
    return len(text.encode("utf-8", errors="replace"))


def _output_bytes(output) -> int:
    """`function_call_output.output`（文字列）・`custom_tool_call_output.output`（リスト）のどちらの形でも本文サイズを測る。"""
    if isinstance(output, str):
        return _text_bytes(output)
    total = 0
    if isinstance(output, list):
        for block in output:
            if isinstance(block, dict):
                total += _text_bytes(block.get("text"))
    return total


def _record_tool_call(calls: list, *, name, call_id) -> None:
    """`function_call`／`custom_tool_call`／`tool_search_call`（呼出側）を控える。
    MCP ツールは `function_call` と `mcp_tool_call_end` の両方に記録されるため、集計はファイルを読み終えてから `_commit_tool_calls` で突き合わせる（二重計上しない）。`call_id` が無ければ回数だけ数える。
    """
    if not isinstance(name, str) or not name:
        return
    calls.append((name, call_id if isinstance(call_id, str) and call_id else None))


def _record_tool_output(outputs: dict, *, call_id, output_bytes: int) -> None:
    if isinstance(call_id, str) and call_id:
        outputs[call_id] = output_bytes


def _output_text_blocks(output) -> list:
    """`function_call_output`／`custom_tool_call_output` の `output` を、ブロック単位のテキストのリストへ揃える。"""
    if isinstance(output, str):
        return [output]
    blocks: list = []
    if isinstance(output, list):
        for block in output:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    blocks.append(t)
    return blocks


def _extract_exec_signal(output) -> tuple:
    """シェル実行系ツールの output から (終了コード, サンドボックス起因か) を読み取る。本文はここでしか見ず、整数・真偽値だけを返す。
    終了コードは ① 各ブロックの全文 JSON の `exit_code` ② `metadata.exit_code` ③ 行頭 `Exit code: N` の順に試し、どれにも一致しなければ `None`。
    サンドボックス起因は、成功していない出力の先頭が `bwrap:` のときだけ。
    """
    exit_code = None
    heads: list = []
    for block in _output_text_blocks(output):
        stripped = block.strip()
        body = stripped
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                ec = parsed.get("exit_code")
                if not (isinstance(ec, int) and not isinstance(ec, bool)):
                    meta = parsed.get("metadata")
                    ec = meta.get("exit_code") if isinstance(meta, dict) else None
                if exit_code is None and isinstance(ec, int) and not isinstance(ec, bool):
                    exit_code = ec
                if isinstance(parsed.get("output"), str):
                    body = parsed["output"].strip()
        if exit_code is None:
            m = _EXIT_CODE_LINE_RE.search(block)
            if m:
                try:
                    exit_code = int(m.group(1))
                except ValueError:
                    pass
        heads.append(body)
    failed = exit_code is None or exit_code != 0
    sandbox = failed and any(h.startswith("bwrap:") for h in heads)
    return exit_code, sandbox


def _record_exec_signal(signals: dict, *, call_id, output) -> None:
    """`_extract_exec_signal` の結果を call_id ごとに控える（`_commit_tool_calls` がツール名の確定後に反映する）。どちらも得られなければ控えない。"""
    if not (isinstance(call_id, str) and call_id):
        return
    exit_code, sandbox = _extract_exec_signal(output)
    if exit_code is not None or sandbox:
        signals[call_id] = (exit_code, sandbox)


def _commit_tool_calls(tools: dict, calls: list, outputs: dict, mcp_call_ids: set,
                       exec_signals: dict) -> None:
    for name, call_id in calls:
        if call_id is not None and call_id in mcp_call_ids:
            continue  # MCP ツールの呼出し＝`mcp_tool_call_end` 側で計上済み
        stats = tools.setdefault(name, _new_tool_stats_basic())
        stats["calls"] = stats.get("calls", 0) + 1
        size = outputs.pop(call_id, None) if call_id is not None else None
        if size is not None:
            stats["bytes"] = stats.get("bytes", 0) + size
            stats["max_bytes"] = max(stats.get("max_bytes", 0), size)
        if name in _EXEC_TOOL_NAMES and call_id is not None:
            signal = exec_signals.pop(call_id, None)
            if signal is not None:
                exit_code, sandbox = signal
                if exit_code is not None and exit_code != 0:
                    stats["errors"] = stats.get("errors", 0) + 1
                if sandbox:
                    stats["sandbox_errors"] = stats.get("sandbox_errors", 0) + 1


def _handle_mcp_tool_call_end(payload: dict, tools: dict) -> bool:
    """Sherpa の MCP 応答形式（`mcp_server.py::_ok`）から、`clipped`＝`text_truncated`／`byte_clipped`、`truncated`＝`truncated`、`errors`＝`isError` または `result.Ok` 欠落を読む。1回として計上したら True。"""
    inv = payload.get("invocation")
    tool = inv.get("tool") if isinstance(inv, dict) else None
    if not isinstance(tool, str) or not tool:
        return False
    stats = tools.setdefault(tool, _new_tool_stats_full())
    for key, zero in _new_tool_stats_full().items():
        stats.setdefault(key, zero)  # 同名の欄が別の形で先にあっても落とさない
    stats["calls"] += 1
    dur = payload.get("duration")
    if isinstance(dur, dict):
        try:
            secs = int(dur.get("secs") or 0)
            nanos = int(dur.get("nanos") or 0)
            stats["ms"] += secs * 1000 + nanos // 1_000_000
        except (TypeError, ValueError):
            pass
    result = payload.get("result")
    ok = result.get("Ok") if isinstance(result, dict) else None
    if not isinstance(ok, dict):
        stats["errors"] += 1  # result.Err 相当（プロトコル層の失敗）
        return True
    if ok.get("isError"):
        stats["errors"] += 1
    content = ok.get("content")
    first_text = None
    if isinstance(content, list) and content and isinstance(content[0], dict):
        t = content[0].get("text")
        if isinstance(t, str):
            first_text = t
    if first_text is None:
        return True
    size = _text_bytes(first_text)
    stats["bytes"] += size
    stats["max_bytes"] = max(stats["max_bytes"], size)
    try:
        parsed = json.loads(first_text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        if parsed.get("text_truncated") or parsed.get("byte_clipped"):
            stats["clipped"] += 1
        if parsed.get("truncated"):
            stats["truncated"] += 1
    return True


def _summarize_session_file(path: Path, *, min_epoch: float | None) -> dict | None:
    """1セッション JSONL（親 or 子の1体分）を要約する。先頭行が `session_meta` として読めなければ None。以降は行ごとに fail-open（読めない行は `unparsed["invalid_json"]`）。
    `min_epoch`（省略時=フィルタしない）より前の `timestamp` の行は数えない。timestamp が読めない行は `unparsed["timestamp"]` に計上して飛ばす。
    """
    tokens = dict.fromkeys(_CHILD_USAGE_KEYS, 0)
    rounds: list = []
    compactions: list = []
    tools: dict = {}
    unparsed: dict = {}
    tool_calls: list = []  # (tool 名, call_id) の列
    tool_outputs: dict = {}  # call_id -> 結果本文のバイト数
    exec_signals: dict = {}  # call_id -> (終了コード, サンドボックス起因か)（シェル実行系のみ）
    mcp_call_ids: set = set()  # mcp_tool_call_end で計上済みの call_id
    model = None
    round_count = 0  # `rounds` とは独立に数える実際の往復数（compactions の位置用）

    def _mark_unparsed(kind: str) -> None:
        unparsed[kind] = unparsed.get(kind, 0) + 1

    try:
        # `errors="replace"`: 不正 UTF-8 の行があってもファイル全体を読めなくしない（その行の JSON パースだけが失敗する）。
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        try:
            meta = json.loads(f.readline())
        except ValueError:
            return None
        if not isinstance(meta, dict) or meta.get("type") != "session_meta":
            return None
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                _mark_unparsed("invalid_json")
                continue
            if not isinstance(rec, dict):
                _mark_unparsed("invalid_json")
                continue
            if min_epoch is not None:
                epoch = _parse_epoch(rec.get("timestamp"))
                if epoch is None:
                    _mark_unparsed("timestamp")  # 読めない timestamp を今回分と推測しない
                    continue
                if epoch < min_epoch:
                    continue
            rtype = rec.get("type")
            payload = rec.get("payload")
            if rtype == "event_msg":
                ptype = payload.get("type") if isinstance(payload, dict) else None
                if ptype == "token_count":
                    info = payload.get("info") if isinstance(payload, dict) else None
                    last = info.get("last_token_usage") if isinstance(info, dict) else None
                    if isinstance(last, dict):
                        row = []
                        for k in _CHILD_USAGE_KEYS:
                            v = last.get(k)
                            # 数値でなければこの往復を unparsed に計上して飛ばす（行単位の fail-open を破らない）。
                            if not (isinstance(v, int) and not isinstance(v, bool)):
                                row = None
                                break
                            row.append(v)
                        if row is None:
                            _mark_unparsed("token_count")
                        else:
                            for k, v in zip(_CHILD_USAGE_KEYS, row):
                                tokens[k] += v
                            round_count += 1
                            # 往復1000件の打切りは rounds 配列だけを抑える（tokens／tools／compactions の集計は続ける）。
                            if len(rounds) < _MAX_ROUNDS:
                                rounds.append(row)
                elif ptype == "mcp_tool_call_end":
                    if _handle_mcp_tool_call_end(payload, tools):
                        cid = payload.get("call_id")
                        if isinstance(cid, str) and cid:
                            mcp_call_ids.add(cid)
                elif not isinstance(ptype, str):
                    # hashable でない値は集合照合の前に弾く。
                    _mark_unparsed("invalid_type")
                elif ptype in _IGNORED_EVENT_MSG_TYPES:
                    pass
                else:
                    _mark_unparsed(f"event_msg:{ptype}")
            elif rtype == "response_item":
                ptype = payload.get("type") if isinstance(payload, dict) else None
                call_id = payload.get("call_id") if isinstance(payload, dict) else None
                if ptype in ("function_call", "custom_tool_call"):
                    _record_tool_call(tool_calls, name=payload.get("name"), call_id=call_id)
                elif ptype in ("function_call_output", "custom_tool_call_output"):
                    _output = payload.get("output")
                    _record_tool_output(tool_outputs, call_id=call_id, output_bytes=_output_bytes(_output))
                    _record_exec_signal(exec_signals, call_id=call_id, output=_output)
                elif ptype == "tool_search_call":
                    _record_tool_call(tool_calls, name="tool_search", call_id=call_id)
                elif ptype == "tool_search_output":
                    try:
                        size = _text_bytes(json.dumps(payload.get("tools"), ensure_ascii=False))
                    except (TypeError, ValueError):
                        size = 0
                    _record_tool_output(tool_outputs, call_id=call_id, output_bytes=size)
                elif ptype == "web_search_call":
                    # Web検索の呼出し回数だけを数える（calls だけのエントリ）。
                    tools.setdefault("web_search", _new_tool_stats_calls_only())["calls"] += 1
                elif not isinstance(ptype, str):
                    _mark_unparsed("invalid_type")
                elif ptype in _IGNORED_RESPONSE_ITEM_TYPES:
                    pass
                else:
                    _mark_unparsed(f"response_item:{ptype}")
            elif rtype == "compacted":
                # 圧縮イベントは「これから始まる往復」に印を付ける。位置は `round_count`（`len(rounds)` は1000で頭打ち）で数える。
                compactions.append(round_count + 1)
            elif rtype == "turn_context":
                if (model is None and isinstance(payload, dict)
                        and isinstance(payload.get("model"), str)):
                    model = payload["model"]
            elif not isinstance(rtype, str):
                _mark_unparsed("invalid_type")
            elif rtype in _IGNORED_TOP_TYPES:
                pass
            else:
                _mark_unparsed(rtype)
    finally:
        f.close()
    _commit_tool_calls(tools, tool_calls, tool_outputs, mcp_call_ids, exec_signals)

    return {
        "model": model,
        "tokens": tokens,
        "rounds": rounds,
        "compactions": compactions,
        "tools": tools,
        "unparsed": unparsed,
        # 内部専用（公開する agents[] エントリには含めない）: 複数親スレッドの合算時に compactions をずらす実往復数を `_merge_parent_pieces` へ渡す。
        "_round_count": round_count,
    }


def _merge_parent_pieces(pieces: list) -> dict:
    """複数の親スレッドの要約を1つの parent エントリへ合算する（呼び出し順＝時刻順）。
    tokens は和、rounds は連結後に先頭 `_MAX_ROUNDS` 件へ切り詰め、tools は項目ごとの和（`max_bytes` だけ最大）、compactions は前のピースまでの実往復数（`_round_count`）だけずらし、model は最後のピースの値。`_round_count` は戻り値に含めない。
    """
    if len(pieces) == 1:
        pieces[0].pop("_round_count", None)
        return pieces[0]
    merged_tokens = dict.fromkeys(_CHILD_USAGE_KEYS, 0)
    merged_rounds: list = []
    merged_compactions: list = []
    merged_tools: dict = {}
    merged_unparsed: dict = {}
    round_offset = 0
    for piece in pieces:
        for k in _CHILD_USAGE_KEYS:
            merged_tokens[k] += piece["tokens"][k]
        merged_compactions.extend(c + round_offset for c in piece["compactions"])
        merged_rounds.extend(piece["rounds"])
        round_offset += piece["_round_count"]
        for name, stats in piece["tools"].items():
            dst = merged_tools.get(name)
            if dst is None:
                merged_tools[name] = dict(stats)
                continue
            for key, val in stats.items():
                if key == "max_bytes":
                    dst["max_bytes"] = max(dst.get("max_bytes", 0), val)
                else:
                    dst[key] = dst.get(key, 0) + val
        for kind, count in piece["unparsed"].items():
            merged_unparsed[kind] = merged_unparsed.get(kind, 0) + count
    return {
        "model": pieces[-1]["model"],
        "tokens": merged_tokens,
        # 連結後に再度先頭 _MAX_ROUNDS 件へ切り詰める（rounds[i] が実往復 i+1 番目からずれないように）。
        "rounds": merged_rounds[:_MAX_ROUNDS],
        "compactions": merged_compactions,
        "tools": merged_tools,
        "unparsed": merged_unparsed,
    }


def summarize_turn(codex_home: Path, *, parent_thread_ids: list, child_thread_ids: set,
                    turn_started_wall: float, settings: dict, phases_ms: dict) -> dict:
    """`answer["activity"]` を組み立てる。`codex_home/sessions/**/*.jsonl` を1回 glob し、親（`tid in parent_thread_ids`）と子（`_is_child_session_meta`）を同じ走査で拾う。
    各親は `tid` ごとに1回だけ要約して `_merge_parent_pieces` で合算し、子は親のいずれかと一致すれば拾う。
    `app_version` は含めない（chat_service が保存直前に埋める）。ファイル/行単位の障害は本関数内で捕まえる（fail-open）。
    """
    seen_pids: set = set()
    ordered_parent_ids: list = []
    for pid in parent_thread_ids or ():
        if isinstance(pid, str) and pid and pid not in seen_pids:
            seen_pids.add(pid)
            ordered_parent_ids.append(pid)
    # 子の新形式判定に渡す候補。親が無くても旧形式（`child_thread_ids`）の判定は1回行う。
    _child_check_pids = ordered_parent_ids or [None]

    try:
        _cand_paths = list(codex_home.glob("sessions/**/*.jsonl"))
    except OSError:
        _cand_paths = []
    # 候補ごとに stat し、失敗したファイルだけ除外してから並べ替える（fail-open）。
    _cand_with_mtime: list = []
    for _p in _cand_paths:
        try:
            _cand_with_mtime.append((_p, _p.stat().st_mtime))
        except OSError:
            continue
    _cand_with_mtime.sort(key=lambda item: item[1], reverse=True)
    cands = [p for p, _ in _cand_with_mtime]
    parent_pieces: dict = {}  # tid -> summarize 結果（1ファイルにつき1回だけ）
    child_agents: list = []
    detected_child_ids: set = set()
    for path in cands:
        # 候補1件ぶんの処理をまとめてファイル単位の fail-open にする（要約は派生物なので、1ファイルの失敗でターン全体の記録を失わない）。
        try:
            # `errors="replace"`: 後方の不正バイト列があっても検出を取りこぼさない。
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                meta = json.loads(f.readline())
            payload = meta.get("payload") if isinstance(meta, dict) else None
            tid = payload.get("id") if isinstance(payload, dict) else None
            # id が空でない文字列でなければこのファイルを読み飛ばす（非 hashable な値で TypeError にしない）。
            if not isinstance(tid, str) or not tid:
                continue
            if tid in ordered_parent_ids and tid not in parent_pieces:
                agent = _summarize_session_file(path, min_epoch=turn_started_wall)
                if agent is not None:
                    parent_pieces[tid] = agent
                continue
            if tid in detected_child_ids:
                continue
            file_mtime = path.stat().st_mtime
            if any(_is_child_session_meta(payload, tid, child_thread_ids=child_thread_ids,
                                           parent_thread_id=pid, min_mtime=turn_started_wall,
                                           file_mtime=file_mtime) for pid in _child_check_pids):
                detected_child_ids.add(tid)
                # 子も min_epoch=turn_started_wall（前のターンの行を再計上しない）。
                agent = _summarize_session_file(path, min_epoch=turn_started_wall)
                if agent is not None:
                    agent.pop("_round_count", None)  # 内部専用（合算しない子には不要）
                    agent["role"] = "child"
                    child_agents.append(agent)
        except Exception as exc:
            _log.warning("codex activity: skipping unreadable session candidate: %s errno=%s",
                        type(exc).__name__, getattr(exc, "errno", None))
            continue

    pieces_in_order = [parent_pieces[pid] for pid in ordered_parent_ids if pid in parent_pieces]
    parent_agent = None
    if pieces_in_order:
        parent_agent = _merge_parent_pieces(pieces_in_order)
        parent_agent["role"] = "parent"
    agents = ([parent_agent] if parent_agent is not None else []) + child_agents
    return {
        "v": 1,
        "source": "codex_rollout",
        "settings": settings,
        "phases_ms": phases_ms,
        "agents": agents,
    }


def exec_failure_counts(activity: dict | None) -> tuple:
    """`summarize_turn()` の戻り値から、シェル実行系ツールの失敗を集計する（codex.log 終了行専用）。
    戻り値＝(本体＝parent の非0終了コード合計, 全エージェント合算のサンドボックス起因失敗合計)。
    `sandbox_errors` は環境全体に効くため親・子を合算し、`errors` は本体分だけを数える。
    `activity` が無い・壊れている場合は `(0, 0)`。
    """
    if not isinstance(activity, dict):
        return 0, 0
    agents = activity.get("agents")
    if not isinstance(agents, list):
        return 0, 0
    exec_failed = 0
    sandbox_failed = 0
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        tools = agent.get("tools")
        if not isinstance(tools, dict):
            continue
        is_parent = agent.get("role") == "parent"
        for name, stats in tools.items():
            if name not in _EXEC_TOOL_NAMES or not isinstance(stats, dict):
                continue
            sv = stats.get("sandbox_errors")
            if isinstance(sv, int) and not isinstance(sv, bool):
                sandbox_failed += sv
            if is_parent:
                ev = stats.get("errors")
                if isinstance(ev, int) and not isinstance(ev, bool):
                    exec_failed += ev
    return exec_failed, sandbox_failed
