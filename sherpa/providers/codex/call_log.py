"""ターンの終わりの道具の呼び出しの記録の仕上げ（プロセスごとのファイルを 1 本にまとめる・親か子かを決める・サーバーのログへ 1 呼び出し 1 行を書く）。
設計: docs/design/codex.md「道具の呼び出しの記録」
"""
from __future__ import annotations

import re
from pathlib import Path

from ...ingest import text_kind
from ... import tool_call_log
from ..base import _log, _log_codex

_LOG_MAX_LINES = 2000  # 1 ターンにログへ書く呼び出しの行の上限（超えた分は件数だけ）


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]")


def _c(value) -> str:
    """ログの 1 項目から制御文字（改行を含む）を除き、1 呼び出しを必ず 1 行に固定する。"""
    return _CTRL_RE.sub(" ", str(value))


def _q(value) -> str:
    """検索語・範囲を引用符つきの 1 項目にする（`make diag` の伏せ字が引用符ごと伏せる形）。"""
    return '"' + _c(value).replace('"', "'") + '"'


def _call_line(st, ctx, row: dict) -> str:
    """呼び出し 1 回の `key=value` の行。資料は最後（`make diag` は `doc=` から行末までを伏せる）。資料の中身・グラフの行は書かない。"""
    args = row.get("args") if isinstance(row.get("args"), dict) else {}
    parts = [f"tool_call conv={ctx.conversation_id}", f"turn={st.turn_uid}", f"attempt={row.get('attempt')}",
             f"call={row['call_id']}", f"role={row.get('role', 'undetermined')}", f"tool={row['tool']}",
             f"status={row.get('status')}", f"count={row.get('count')}", f"ms={row.get('ms')}"]
    if row.get("error_kind"):
        parts.append(f"error={row['error_kind']}")
    for key in ("depth", "item"):
        if args.get(key) not in (None, ""):
            parts.append(f"{key}={args[key]}")
    if args.get("query"):
        parts.append(f"query={_q(args['query'])}")
    if args.get("range"):
        parts.append(f"range={_q(args['range'])}")
    if row.get("docs_omitted"):
        parts.append(f"docs_omitted={row['docs_omitted']}")
    docs = []
    for d in row.get("docs") or []:
        name = d.get("doc") if isinstance(d, dict) else None
        if not isinstance(name, str) or not name or text_kind.is_sensitive_doc_id(name):
            continue
        docs.append(f"{name}@{d['range']}" if d.get("range") else name)
    if not docs and isinstance(args.get("doc"), str) and not text_kind.is_sensitive_doc_id(args["doc"]):
        docs.append(args["doc"])
    if docs:
        parts.append("doc=" + ", ".join(docs))
    return _c(" ".join(parts))


def finalize_call_log(ctx, st) -> None:
    """① プロセスごとのファイルを 1 本にまとめる ② 親か子かを決める ③ 1 呼び出し 1 行をアプリのログへ書く。失敗してもターンは落とさない。"""
    if st._call_log_dir is None:
        return
    try:
        merged = tool_call_log.merge_call_logs(st._call_log_dir, st.turn_uid)
        tool_call_log.assign_roles(merged, st._parent_mcp_tools,
                                   parent_record_reliable=not st._tool_use.record_unreliable)
        tool_call_log.count_parent_shortfall(merged, st._parent_mcp_tools)
        tool_call_log.merge_graph_results(st._call_log_dir, st.turn_uid, merged)
        st.call_log = merged
        tool_call_log.remove_dir(st._call_log_dir)
        if not merged.rows and not merged.missing:
            return
        for row in merged.rows[:_LOG_MAX_LINES]:
            _log_codex.info("%s", _call_line(st, ctx, row))
        roles = [r.get("role") for r in merged.rows]
        _log_codex.info(
            "tool_calls conv=%s turn=%s calls=%s parent=%s child=%s undetermined=%s log_omitted=%s call_log_missing=%s%s",
            ctx.conversation_id, st.turn_uid, len(merged.rows), roles.count("parent"), roles.count("child"),
            roles.count("undetermined"), max(0, len(merged.rows) - _LOG_MAX_LINES), merged.missing,
            " sandbox=off" if st.codex_home is None else "")
    except Exception as exc:
        _log.warning("tool call log finalize failed: %s", type(exc).__name__)


def call_record_sections(st) -> dict | None:
    """調査の記録へ足す「調べた経路」「見つかった資料」（`detail.calls`）。呼び出しも欠けも無ければ None。
    設計: docs/design/codex.md「調べた経路と見つかった資料」
    """
    merged = st.call_log
    if merged is None or (not merged.rows and not merged.missing):
        return None
    route, omitted = tool_call_log.route_rows(merged)
    opened = tool_call_log.opened_docs(merged) | set(st._mcp_read_docs)
    found, hidden, hit_omitted = tool_call_log.found_docs(merged, opened)
    sections: dict = {"v": 1, "calls": len(merged.rows), "missing": merged.missing, "route": route,
                      "found": found[:tool_call_log.FOUND_RECORD_MAX]}
    if omitted:
        sections["route_omitted"] = omitted
    if len(found) > tool_call_log.FOUND_RECORD_MAX:
        sections["found_more"] = len(found) - tool_call_log.FOUND_RECORD_MAX
    if hidden:
        sections["found_hidden"] = hidden
    if tool_call_log.opened_incomplete(merged):
        sections["opened_unknown"] = True
    if hit_omitted:
        sections["found_hits_omitted"] = hit_omitted
    return sections


def call_stats(st) -> dict | None:
    """回答に残す道具ごとの累計 `{v, calls, missing, opened, opened_unknown, tools:[{tool, role, calls, found, ms, errors, truncated}]}`（利用統計の材料・検索語・資料名は持たない）。記録が無ければ None。"""
    merged = st.call_log
    if merged is None or (not merged.rows and not merged.missing):
        return None
    return {"v": 1, "calls": len(merged.rows), "missing": merged.missing,
            "opened": len(tool_call_log.opened_docs(merged) | set(st._mcp_read_docs)),
            "opened_unknown": tool_call_log.opened_incomplete(merged),
            "tools": tool_call_log.tool_totals(merged)}


def _strings(value) -> set[str]:
    """入れ子の list/tuple/set の中の文字列を集める（「参照した資料」の行の候補群は入れ子）。"""
    if isinstance(value, str):
        return {value}
    out: set[str] = set()
    if isinstance(value, (list, tuple, set)):
        for v in value:
            out |= _strings(v)
    return out


def found_docs_for_answer(st, referenced) -> tuple[list[dict], int, int]:
    """回答の `found_docs`（Codex の読み取り記録が無い見つかった資料）。Sherpa の読み取り道具で開いておらず `referenced`（回答の「参照した資料」の候補・入れ子でもよい）にも無い資料だけ。
    戻り値は `([{path}], 秘匿で伏せた件数, 上限で省いた件数)`。
    """
    merged = st.call_log
    if merged is None or tool_call_log.opened_incomplete(merged):
        return [], 0, 0
    referenced = _strings(referenced)
    opened = tool_call_log.opened_docs(merged) | set(st._mcp_read_docs)
    found, hidden, _ = tool_call_log.found_docs(merged, opened)
    rest = [e["doc"] for e in found if not e["opened"] and e["doc"] not in referenced]
    return ([{"path": d} for d in rest[:tool_call_log.FOUND_ANSWER_MAX]], hidden,
            max(0, len(rest) - tool_call_log.FOUND_ANSWER_MAX))


REFERENCED_DOCS_MAX = 50  # 回答の `referenced_docs` に出す資料の上限（超えた分は `referenced_docs_more`）
REFERENCED_RANGES_MAX = 20  # 1 資料に出す行の範囲の上限（超えた分は `ranges_more`）


def _line_range(value):
    """`"12-40"`・`"12"` を `(開始, 終了)` にする。読めない値は None。"""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,9})\s*(?:-\s*(\d{1,9}))?\s*", value)
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return (start, end) if 0 < start <= end else None


def merge_line_ranges(values) -> list[tuple[int, int]]:
    """行の範囲の文字列を、重なる・隣り合うものをまとめて開始の順に並べる。読めないものは捨てる。"""
    found = sorted(r for r in (_line_range(v) for v in values) if r)
    out: list[list[int]] = []
    for start, end in found:
        if out and start <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


def _tool_only_doc(doc_id: str) -> bool:
    """Excel・Word・PowerPoint・PDF など、読み取り道具でしか中身を読めない資料か。"""
    from ...ingest import office_md
    return Path(doc_id).suffix.lower() in office_md.OFFICE_EXT


def referenced_docs_for_answer(st, verified_refs) -> tuple[list[dict], int]:
    """回答の `referenced_docs`（確かめを通った資料ごとに、Codex が開いた行の範囲と、開いた記録の無い Excel・Word・PowerPoint・PDF の印）。戻り値は `(行, 上限で省いた件数)`。
    設計: docs/design/chat.md「出典」
    """
    merged = st.call_log
    by_doc: dict[str, list] = {}
    if merged is not None:
        for r in merged.rows:
            if r.get("tool") in tool_call_log.OPEN_TOOLS and r.get("status") != "error":
                for d in r.get("docs") or []:
                    if isinstance(d, dict) and isinstance(d.get("doc"), str):
                        by_doc.setdefault(d["doc"], []).append(d.get("range"))
    record_complete = merged is not None and not tool_call_log.opened_incomplete(merged)
    opened = tool_call_log.opened_docs(merged) | set(st._mcp_read_docs) if merged is not None else set(st._mcp_read_docs)
    rows = []
    for path in verified_refs:
        ranges = merge_line_ranges(by_doc.get(path, []))
        row = {"path": path, "ranges": [list(x) for x in ranges[:REFERENCED_RANGES_MAX]],
               "unopened": bool(record_complete and _tool_only_doc(path) and path not in opened)}
        if len(ranges) > REFERENCED_RANGES_MAX:
            row["ranges_more"] = len(ranges) - REFERENCED_RANGES_MAX
        rows.append(row)
    return rows[:REFERENCED_DOCS_MAX], max(0, len(rows) - REFERENCED_DOCS_MAX)
