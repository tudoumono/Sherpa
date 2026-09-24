"""Codex セッション記録（rollout JSONL）から1ターン分の活動要約（`answer["activity"]`・v1・
利用統計の刷新 提案書 `docs/proposals/2026-09-23-利用統計の刷新.md` §3.1）を作る。**読むだけ**
（挙動は変えない・`answer["usage"]`／codex.log には触らない）。

セッション記録の形式は Codex CLI の版で変わり得る——本文・資料名・引数は要約に一切含めない
（ツール名・件数・バイト数・時間だけ）。行/レコード単位・ファイル単位で fail-open にする（読めない
行・未知の種類・型が壊れた `type` フィールドは `unparsed` へ計上し、開けない/壊れたファイルは
読み飛ばす・ターンを落とさない）。呼び出し側（`provider.py` の finally ブロック）は
`_collect_child_token_usage` 呼出と同じ流儀で `summarize_turn` 全体を try/except に包み、それでも
起きた想定外の例外だけを型名・errno でログする（本モジュール自身は最終防波堤を持たない＝二重に
握りつぶさない）。

子（下調べ役）の検出は `_is_child_session_meta` 1箇所だけで判定する——`provider.py` の
`_collect_child_token_usage`（子 usage 合算・既存の戻り値/テストは変えない）もこの関数を呼ぶ
（新形式=`parent_thread_id`+`thread_source=="subagent"`+mtime、旧形式=`spawn_agent` 捕捉 id、の
判定を二重実装しない）。

親スレッドは1ターンに複数使われることがある（resume 失敗→新規スレッドへのフォールバック）——
`summarize_turn` は `parent_thread_ids`（時刻順のリスト）を受け取り、各親とその子を拾ってから
1つの `parent` エントリへ合算する（`_merge_parent_pieces`）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

_log = logging.getLogger("sherpa")

_CHILD_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
_MAX_ROUNDS = 1000   # `rounds` 配列の大きさをこの件数で打ち切る（tokens/tools/compactions の
                      # 集計はファイル最後まで続ける・rounds だけを抑える理由は §3.1 の
                      # 「最大1000件」がJSONサイズの上限であって集計値の打切りではないため）。

# event_msg/response_item の既知だがこのモジュールの集計に寄与しないペイロード種別
# （進捗マーカー・本文そのもの・CLI の版で増減し得る付随情報）。ここに無い種別は `unparsed` へ計上する。
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

# シェル実行系ツール（`function_call`／`custom_tool_call` の `name`）。実測（codex-cli 0.153.4・
# 2026-09-24・`gpt-6-astra`／`gpt-5.6-sol` とも一致確認: `tools.exec_command(...)` を包む
# custom_tool_call の name は常に "exec"）に加え、CLI の版・モデルの違いで揺れうる名前を保険で
# 含める。`errors`／`sandbox_errors`（終了コード由来の集計・`_extract_exec_signal`）はこの集合に
# 載る名前のツールにだけ足す（`_new_tool_stats_basic` の「測れたときだけキーを置く」契約を、
# シェル実行系だけの例外として守る＝他の function_call/custom_tool_call には波及させない）。
_EXEC_TOOL_NAMES = frozenset({"exec", "exec_command", "shell", "local_shell"})

# 行頭 "Exit code: N"（大小文字・前後空白を許容）。実測（`_EXEC_TOOL_NAMES` docstring 参照）では
# 使われない書式だが、CLI の版差の保険として `_extract_exec_signal` が最後に試す。
_EXIT_CODE_LINE_RE = re.compile(r"(?im)^\s*exit code:\s*(-?\d+)\s*$")


def _is_child_session_meta(payload: dict | None, tid, *, child_thread_ids: set,
                            parent_thread_id: str | None, min_mtime: float | None,
                            file_mtime: float) -> bool:
    """このセッション記録（先頭行 `session_meta` の `payload`・`tid`）が「今回の子」と一致するか。
    新形式（`parent_thread_id` 一致 かつ `thread_source=="subagent"` かつ mtime>=min_mtime）と
    旧形式（`tid` が `child_thread_ids` に含まれる）の和集合。呼び出し側は `tid` の重複
    （同じ子を2度数えない）を別途扱う——本関数は単発の判定だけを行う。
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
    """MCP ツール（`mcp_tool_call_end`）専用——Sherpa 自身の応答形式から clipped/truncated/errors/ms
    まで実測できる。"""
    return {"calls": 0, "bytes": 0, "max_bytes": 0, "clipped": 0, "truncated": 0, "errors": 0, "ms": 0}


def _new_tool_stats_basic() -> dict:
    """function_call／custom_tool_call／tool_search 専用——Codex ネイティブの組込みツールで
    Sherpa 自身の MCP 応答形式ではないため、clipped/truncated/ms は測れない（推定で埋めない＝
    キー自体を置かない・calls/bytes/max_bytes だけを持つ）。例外はシェル実行系ツール
    （`_EXEC_TOOL_NAMES`）の `errors`／`sandbox_errors`——終了コードが読めた call があるときだけ
    `_commit_tool_calls` が個別に足す（`_extract_exec_signal` 参照・他のツールには波及しない）。"""
    return {"calls": 0, "bytes": 0, "max_bytes": 0}


def _new_tool_stats_calls_only() -> dict:
    """web_search_call 専用——`response_item` に結果本文が無く大きさを測れないため、
    calls だけを持つ（bytes/max_bytes を含め他のキーは置かない＝推定で埋めない）。"""
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
    """`function_call_output.output`（文字列）・`custom_tool_call_output.output`
    （`{"type":...,"text":...}` のリスト）のどちらの形でも本文サイズを測る。"""
    if isinstance(output, str):
        return _text_bytes(output)
    total = 0
    if isinstance(output, list):
        for block in output:
            if isinstance(block, dict):
                total += _text_bytes(block.get("text"))
    return total


def _record_tool_call(calls: list, *, name, call_id) -> None:
    """`function_call`／`custom_tool_call`／`tool_search_call`（呼出側）を控える。集計はファイルを
    読み終えてから `_commit_tool_calls` で行う——MCP ツールの呼出しは同じ `call_id` で
    `function_call` と `mcp_tool_call_end` の両方に記録されるため、`mcp_tool_call_end` で数えた
    呼出しをここで二重に数えない（記録の順序に依らず判定できるよう、読み終えてから突き合わせる）。
    `call_id` が無い/文字列でなければ回数だけ数える（対応する output の大きさは引けない＝推定で
    埋めない）。"""
    if not isinstance(name, str) or not name:
        return
    calls.append((name, call_id if isinstance(call_id, str) and call_id else None))


def _record_tool_output(outputs: dict, *, call_id, output_bytes: int) -> None:
    if isinstance(call_id, str) and call_id:
        outputs[call_id] = output_bytes


def _output_text_blocks(output) -> list:
    """`function_call_output`／`custom_tool_call_output` の `output`（文字列、または
    `{"type":...,"text":...}` のリスト）を、ブロック単位のテキストのリストへ揃える——
    `_output_bytes` と同じ入力形を受けるが、バイト数でなく本文そのものを返す（呼び出し元
    `_extract_exec_signal` が使い捨てで読むためだけの中間形）。"""
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
    """シェル実行系ツールの output から (終了コード, サンドボックス起因か) を読み取る使い捨ての
    ヘルパー——本文はここでしか見ず、戻り値（整数・真偽値）以外は呼び出し元へ渡さない。

    終了コードの読み方は優先順に複数試す（CLI の版で書き方が変わりうるため）:
      1. 実測（codex-cli 0.153.4）: 各ブロックの全文が JSON で、先頭レベルの整数 `exit_code`
         （`{"chunk_id":...,"exit_code":N,"output":"..."}`）。
      2. 保険: 同 JSON の `metadata.exit_code`。
      3. 保険: 行頭 `Exit code: N`（`_EXIT_CODE_LINE_RE`）。
    どれにも一致しなければ終了コードは `None`（`errors` へ計上しない＝測れたときだけキーを置く）。

    サンドボックス起因は、成功していない（終了コードが 0 以外・または読めない）出力の先頭が
    `bwrap:` のときだけ（bwrap の起動失敗の文面は必ずこの接頭辞で始まる）。成功したコマンドの
    本文に同じ語が含まれていても数えない（誤警告を出さない）。
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
    """`_extract_exec_signal` の結果を call_id ごとに控える（`_commit_tool_calls` がツール名の
    確定後に反映する・MCP ツールと同じ「呼出→出力」の2段階集計と対称）。終了コード・サンドボックス
    判定のどちらも得られなければ何も控えない（測れなかった call は `errors`／`sandbox_errors` の
    計上対象にしない）。"""
    if not (isinstance(call_id, str) and call_id):
        return
    exit_code, sandbox = _extract_exec_signal(output)
    if exit_code is not None or sandbox:
        signals[call_id] = (exit_code, sandbox)


def _commit_tool_calls(tools: dict, calls: list, outputs: dict, mcp_call_ids: set,
                       exec_signals: dict) -> None:
    for name, call_id in calls:
        if call_id is not None and call_id in mcp_call_ids:
            continue   # MCP ツールの呼出し＝`mcp_tool_call_end` 側で計上済み
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
    """Sherpa 自身の MCP 応答形式（`mcp_server.py::_ok`）は CLI の版に依存しない契約——
    `clipped`＝結果 JSON 先頭の `text_truncated`／`byte_clipped`、`truncated`＝`truncated`、
    `errors`＝`isError` または `result.Ok` 欠落（プロトコル層の失敗）。1回として計上したら True。"""
    inv = payload.get("invocation")
    tool = inv.get("tool") if isinstance(inv, dict) else None
    if not isinstance(tool, str) or not tool:
        return False
    stats = tools.setdefault(tool, _new_tool_stats_full())
    for key, zero in _new_tool_stats_full().items():
        stats.setdefault(key, zero)   # 同名の欄が別の形（calls だけ等）で先にあっても落とさない
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
        stats["errors"] += 1   # result.Err 相当（プロトコル層の失敗）
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
    """1セッション JSONL（親 or 子いずれか1体分）を要約する。先頭行が `session_meta` として
    読めなければ None（呼び出し側はこのファイルを諦める＝fail-open）。以降は1行ごとに fail-open
    （読めない行は `unparsed["invalid_json"]` へ計上して次行へ進む・例外は上げない）。

    `min_epoch`（省略時=フィルタしない）: このエポック秒より前の `timestamp` を持つ行は数えない
    （resume で同じファイルに前ターンの行が入っている対策・親子とも渡される契約）。timestamp が
    読めない行は「今回分」と推測せず `unparsed["timestamp"]` へ計上して飛ばす（取りこぼしを許容
    しても今回分だと決め打ちしない）。
    """
    tokens = {k: 0 for k in _CHILD_USAGE_KEYS}
    rounds: list = []
    compactions: list = []
    tools: dict = {}
    unparsed: dict = {}
    tool_calls: list = []      # (tool 名, call_id) の列（function_call/custom_tool_call/tool_search_call）
    tool_outputs: dict = {}    # call_id -> 結果本文のバイト数（..._output）
    exec_signals: dict = {}    # call_id -> (終了コード, サンドボックス起因か)（シェル実行系のみ）
    mcp_call_ids: set = set()  # mcp_tool_call_end で計上済みの call_id
    model = None
    round_count = 0   # `rounds`（1000件で頭打ち）とは独立に数える実際の往復数（compactions の位置用）。

    def _mark_unparsed(kind: str) -> None:
        unparsed[kind] = unparsed.get(kind, 0) + 1

    try:
        # `errors="replace"`: 不正 UTF-8 バイト列を含む行が1件でもあると、厳格デコード
        # （既定の `errors="strict"`）は先読みバッファ単位で失敗し、**その行より前の正常な行**
        # まで読めなくなる（TextIOWrapper の先読みはバッファ全体を一括デコードするため）。
        # 置換文字に落とせば不正行の JSON パースだけが個別に失敗する（下の `except ValueError`
        # で1行ごとに拾う）——ファイル全体を諦めない。
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
                    _mark_unparsed("timestamp")   # 読めない timestamp を今回分と推測しない
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
                            # 数値（bool を除く int）でなければこの往復ごと unparsed へ計上し
                            # 飛ばす——`int(v)` を素通しすると非数値（文字列/リスト/None 等）で
                            # ValueError/TypeError がファイルの外まで上がり、行単位の fail-open が
                            # 破れる（このターンの activity 全体を失う）。
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
                            # 往復1000件の打切りは rounds 配列の大きさだけを抑える——tokens／tools／
                            # compactions の集計はファイルの最後まで続ける（1000件を超えた分も
                            # 集計から欠落させない）。
                            if len(rounds) < _MAX_ROUNDS:
                                rounds.append(row)
                elif ptype == "mcp_tool_call_end":
                    if _handle_mcp_tool_call_end(payload, tools):
                        cid = payload.get("call_id")
                        if isinstance(cid, str) and cid:
                            mcp_call_ids.add(cid)
                elif not isinstance(ptype, str):
                    # `[]`/`{}` 等 hashable でない値は frozenset の `in` 照合で TypeError になる
                    # （集合と照合する前にここで弾く＝要約全体を落とさない）。
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
                    # Web検索（チャットごとに利用者が選べる機能）の呼出し回数だけを数える——
                    # response_item に結果本文が無く大きさを測れない（calls だけのエントリ）。
                    tools.setdefault("web_search", _new_tool_stats_calls_only())["calls"] += 1
                elif not isinstance(ptype, str):
                    _mark_unparsed("invalid_type")
                elif ptype in _IGNORED_RESPONSE_ITEM_TYPES:
                    pass
                else:
                    _mark_unparsed(f"response_item:{ptype}")
            elif rtype == "compacted":
                # 圧縮イベントは「これから始まる往復」に印を付ける（正典§3.1のコメント参照・
                # 直前往復の文脈が縮む＝次往復の入力に効果が現れる）。`round_count`（配列とは独立の
                # 実カウント）を使う——`len(rounds)` は1000で頭打ちのため、1000件超の圧縮位置が
                # すべて1001に潰れてしまう。
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
        # 内部専用（公開する agents[] エントリには含めない）: 複数親スレッドを合算するとき、
        # compactions のずらし幅は rounds 配列の長さ（1000で頭打ち）ではなく実際の往復数を
        # 使う必要があるため `_merge_parent_pieces` へ渡す（`_summarize_turn`/子の要約結果からは
        # 呼び出し側が取り出した後に取り除く）。
        "_round_count": round_count,
    }


def _merge_parent_pieces(pieces: list) -> dict:
    """resume 失敗→フォールバックで1ターンに複数の親スレッドを使った場合の要約を、1つの
    parent エントリへ合算する（呼び出し順＝時刻順であることが前提）。契約は単一ピースと同じ
    （`rounds[i]` はそのターンの実往復 i+1 番目・先頭 `_MAX_ROUNDS` 件まで）——tokens は和、
    rounds は時刻順（=pieces の順）に連結した後で先頭 `_MAX_ROUNDS` 件に再度切り詰める（先の
    ピースだけで既に頭打ちなら、後のピースの往復は配列に入らない）、tools は項目ごとの和
    （`max_bytes` だけ最大）、compactions は前のピースまでの**実際の往復数**（`_round_count`・
    rounds 配列の長さではない＝1000で頭打ちの値を使うと位置がずれる）だけずらす（トークンの
    合計・圧縮位置は再切り詰めの影響を受けない）、model は最後のピースの値。`_round_count` は
    内部専用のため戻り値には含めない。"""
    if len(pieces) == 1:
        pieces[0].pop("_round_count", None)
        return pieces[0]
    merged_tokens = {k: 0 for k in _CHILD_USAGE_KEYS}
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
        # 連結後に再度先頭 _MAX_ROUNDS 件へ切り詰める——ピースごとの切り詰め（各 1000件まで）を
        # 素通しで連結すると、rounds[i] が実往復 i+1 番目からずれる（例: 先のピースが1000件で
        # 頭打ちのとき、後のピースの往復が1001件目以降として配列に紛れ込む）。
        "rounds": merged_rounds[:_MAX_ROUNDS],
        "compactions": merged_compactions,
        "tools": merged_tools,
        "unparsed": merged_unparsed,
    }


def summarize_turn(codex_home: Path, *, parent_thread_ids: list, child_thread_ids: set,
                    turn_started_wall: float, settings: dict, phases_ms: dict) -> dict:
    """`answer["activity"]`（v1・§3.1）を組み立てる。`codex_home/sessions/**/*.jsonl` を1回
    glob し、親（`tid in parent_thread_ids`）と子（`_is_child_session_meta`）を同じ走査で拾う。

    `parent_thread_ids`（時刻順のリスト・1件が普通・resume 失敗でフォールバックしたターンは
    2件になり得る）: 見つかった各親を要約し、`_merge_parent_pieces` で1つの parent エントリへ
    合算する（同じファイルを二重に数えない＝`tid` ごとに1回だけ要約）。子の検出は
    `parent_thread_ids` の**いずれか**と一致すれば拾う（どの親が起動した子かは区別しない）。

    `app_version` は含めない（保存直前に chat_service が全経路共通で埋める・provider 側では
    確定しない値のため二重に書かない）。個々のファイル読取が失敗しても他のファイルの要約は
    続ける（fail-open）——ファイル/行単位の障害は本関数の中で既に捕まえている。呼び出し側
    （`provider.py` の finally ブロック）が `_collect_child_token_usage` 呼出と同じ流儀で本関数
    全体を try/except に包み、それでも起きた想定外の例外だけを型名・errno でログする。
    """
    seen_pids: set = set()
    ordered_parent_ids: list = []
    for pid in parent_thread_ids or ():
        if isinstance(pid, str) and pid and pid not in seen_pids:
            seen_pids.add(pid)
            ordered_parent_ids.append(pid)
    # 子の新形式判定（`parent_thread_id` 突合）に渡す候補——親が1つも無くても旧形式
    # （`child_thread_ids`）の判定は必ず1回は行う（`_is_child_session_meta` は
    # `parent_thread_id=None` でも旧形式側の判定を独立して行える）。
    _child_check_pids = ordered_parent_ids or [None]

    try:
        _cand_paths = list(codex_home.glob("sessions/**/*.jsonl"))
    except OSError:
        _cand_paths = []
    # 候補ごとに stat する——sorted() の key に直接 `p.stat()` を渡すと、1件でも失敗（削除済み・
    # 読取不可等）すると sorted() 自体が例外を出し、except で拾っても cands=[] になり正常な
    # 親子の集計まで失う。失敗したファイルだけをここで除外してから並べ替える（fail-open）。
    _cand_with_mtime: list = []
    for _p in _cand_paths:
        try:
            _cand_with_mtime.append((_p, _p.stat().st_mtime))
        except OSError:
            continue
    _cand_with_mtime.sort(key=lambda item: item[1], reverse=True)
    cands = [p for p, _ in _cand_with_mtime]
    parent_pieces: dict = {}   # tid -> summarize 結果（1ファイルにつき1回だけ・二重に数えない）
    child_agents: list = []
    detected_child_ids: set = set()
    for path in cands:
        # 候補1件ぶんの処理（先頭行の読み取り・親子の判定・`_summarize_session_file` 呼出）を
        # まとめてファイル単位の fail-open にする——要約は派生物なので、1ファイルの失敗
        # （本文を読んでいる途中の OSError＝EIO 等、`_summarize_session_file` の内部で起きる
        # ものも含む）でターン全体の記録を失わない。行単位の読み飛ばし（`unparsed`）と候補一覧
        # 構築時の stat 捕捉（cands を作る側）は個別の契約のままここでは変えない。
        try:
            # `errors="replace"`: 先頭行（session_meta）自体は正常でも、ファイル後方の不正
            # バイト列が同じ先読みバッファに乗ると厳格デコードは1行目の readline() から失敗する
            # （`_summarize_session_file` と同じ理由）——検出そのものを取りこぼさない。
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                meta = json.loads(f.readline())
            payload = meta.get("payload") if isinstance(meta, dict) else None
            tid = payload.get("id") if isinstance(payload, dict) else None
            # `type` の型ガードと同じ扱い: id が空でない文字列でなければこのファイルを読み飛ばす
            # （このあとの `tid not in parent_pieces`／`tid in detected_child_ids`／
            # `detected_child_ids.add(tid)` は dict/set の照合のため、id が非 hashable な値（[] 等）
            # だと判定前に TypeError になり、正常な親の要約まで含めて activity 全体を失う）。
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
                # 子も min_epoch=turn_started_wall（前のターンの行が残っている子の記録を再計上しない）。
                agent = _summarize_session_file(path, min_epoch=turn_started_wall)
                if agent is not None:
                    agent.pop("_round_count", None)   # 内部専用（合算しない子には不要）
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
    """`summarize_turn()` の戻り値から、シェル実行系ツール（`_EXEC_TOOL_NAMES`）の失敗を集計する
    ——codex.log 終了行（`provider.py`）専用のヘルパー。戻り値は
    (本体＝parent の非0終了コード合計, 全エージェント合算のサンドボックス起因失敗合計)。

    `sandbox_errors` は親・子の区別なく合算する——実機事故（依頼文の背景・AppArmor のユーザー
    名前空間制限）の原因は環境全体に効くため、下調べ役（子）だけが踏んでも同じ環境不調の証拠で
    あり、本体が踏むまで待つ理由が無い。一方 `errors`（サンドボックスに限らない一般のコマンド
    失敗）は本体の挙動を見る指標として本体分だけを数える（子の個別コマンド選択の失敗まで
    一括に混ぜない）。

    `activity` が無い・形が壊れている（`summarize_turn` を呼ばなかったターン・Codex 以外の経路）は
    `(0, 0)`——呼び出し元（codex.log 終了行）は必ず1回出す契約を壊さない（fail-open）。
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
