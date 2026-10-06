"""影響一覧（`answer.impact_list`）を、Codex がグラフの道具で影響をたどった結果の記録から作る。
設計: docs/design/codex.md「影響一覧」
"""
from __future__ import annotations

from ... import tool_call_log
from ...mcp_server import LISTED_DOC_TOOLS, READ_DOC_TOOLS
from .structured import _parse_evidence_ref

IMPACT_LIST_MAX_ROWS = 300  # 表に出す行の上限（超えた分は `more` の件数）

# 網羅しきれなかった理由（決まった言い方だけ・自由な文にしない）。
REASON_NOT_TRACED = "グラフで影響をたどった記録がありません"
REASON_NO_RECORD = "道具の呼び出しの記録を取れなかったため、影響をたどったかを確かめられません"
REASON_RECORD_MISSING = "結果が記録に残っていないグラフの呼び出しがあります"
REASON_RECORD_GAP = "道具の記録に欠けがあります"
REASON_CHILD_UNKNOWN = "子のエージェントの調べ結果が取れていない可能性があります"
REASON_START_NOT_FOUND = "起点の識別子がグラフに見つかりませんでした"
REASON_CALL_FAILED = "グラフの呼び出しに失敗しました"
REASON_NO_CLAIMS = "回答の根拠の記録が無いため、根拠に使った行は区別できません"
_LIMIT_REASON = {
    "timeout": "グラフの検索が時間内に終わらず、一部しかたどれていません",
    "row_cap": "グラフの検索の件数の天井に達し、一部しかたどれていません",
    "depth": "深さの上限で止まり、先にまだ影響が残っている可能性があります",
    "result_cap": "返す件数の上限で、一部の行を省いています",
    "graph_unavailable": "グラフに接続できず、調べられませんでした",
    "graph_reingest_required": "グラフが古い形式のため調べられませんでした（取り込み直しが必要です）",
    "plugin_failed": "解析に失敗した部分があります",
    "source_unparsed": "解析できなかったソースがあります",
}
_ROW_REASON = {
    "candidate": "グラフでたどれました",
    "inspected": "Codex が資料を開いて確かめました",
    "used": "回答の根拠に出てきます",
    "unmapped": "資料の場所が分からず、回答の根拠と結べません",
}
_UNMAPPED_NO_CLAIMS = "回答の根拠の記録が無く、対応を判定できません"


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _opened_paths(st) -> set[str]:
    """Codex が読み取り道具で開いた資料（成功した呼び出し・親の `--json` と道具の記録の両方）。"""
    out = {_norm(d) for d in st._mcp_read_docs if isinstance(d, str) and d}
    rows = st.call_log.rows if st.call_log is not None else []
    for r in rows:
        tool = r.get("tool")
        if (tool in READ_DOC_TOOLS or tool == "compare_documents") and tool not in LISTED_DOC_TOOLS and r.get("status") != "error":
            for d in r.get("docs") or []:
                if isinstance(d, dict) and isinstance(d.get("doc"), str):
                    out.add(_norm(d["doc"]))
    return out


def _evidence_paths(env) -> tuple[set[str], bool]:
    """回答の主張の根拠（`claims[].evidence_refs`）と照らし合わせの参照に出てくる資料。2 つ目は、根拠の記録（主張か照らし合わせ）があるか。"""
    data = env.get("data") if isinstance(env.get("data"), dict) else {}
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    recon = data.get("reconciliation") if isinstance(data.get("reconciliation"), list) else []
    out: set[str] = set()
    for c in claims:
        for ref in (c.get("evidence_refs") if isinstance(c, dict) and isinstance(c.get("evidence_refs"), list) else []):
            parsed = _parse_evidence_ref(ref)
            if parsed is not None:
                out.add(parsed[0])
    for r in recon:
        for side in ("spec", "source"):
            doc = r.get(side, {}).get("doc_id") if isinstance(r, dict) and isinstance(r.get(side), dict) else None
            if isinstance(doc, str) and doc:
                out.add(_norm(doc))
    return out, bool(claims or recon)


def _limit_reasons(entry: dict) -> list[str]:
    out = []
    for lim in entry.get("limits") or []:
        kind = lim.get("kind")
        text = _LIMIT_REASON.get(kind)
        if text is None:
            continue
        if kind == "source_unparsed" and isinstance(lim.get("count"), int):
            text = f"解析できなかったソースが {lim['count']} 件あります"
        out.append(text)
    if entry.get("tool") == "graph_impact" and entry.get("depth_truncated") is True and "depth" not in {
            lim.get("kind") for lim in entry.get("limits") or []}:
        out.append(_LIMIT_REASON["depth"])
    if isinstance(entry.get("unresolved"), int) and entry["unresolved"] > 0:
        out.append(f"解決できなかった参照が {entry['unresolved']} 件あります")
    err = entry.get("error")
    if err == "graph_start_not_found":
        out.append(REASON_START_NOT_FOUND)
    elif err in ("graph_unavailable", "graph_reingest_required"):
        if _LIMIT_REASON[err] not in out:
            out.append(_LIMIT_REASON[err])
    elif err:
        out.append(REASON_CALL_FAILED)
    if not entry.get("complete", True) and not out:
        out.append("グラフの結果に未完了の印があります")
    return out


def build_impact_list(st, env: dict, decision) -> dict | None:
    """グラフの道具の結果（`graph_resolve`・`graph_impact`）から影響一覧 `{v, traced, rows, reasons, more}` を作る。付けないときは None。
    ① 道具の記録から影響をたどった結果を集める ② 起点と影響先を行にして、開いた・根拠に使った・対応不明を付ける
    ③ 網羅しきれなかった理由を決まった言い方で並べる（たどっていない・記録が取れないときは `traced=False`＝「影響なし」と出さない）
    作成のときは付けない。影響の調べ方（lens=impact）以外は、たどったときだけ付ける。
    """
    lens = decision.get("lens") if isinstance(decision, dict) else None
    if lens == "author":
        return None
    merged = st.call_log
    graph = list(merged.graph) if merged is not None else []
    impact = [g for g in graph if g.get("tool") == "graph_impact" and isinstance(g.get("start"), dict)]
    traced = bool(impact)
    if not traced and lens != "impact":
        return None

    reasons: list[str] = []

    def add(text: str) -> None:
        if text not in reasons:
            reasons.append(text)

    if merged is None:
        add(REASON_NO_RECORD)
    else:
        called = [r for r in merged.rows if r.get("tool") in tool_call_log.GRAPH_TOOLS]
        parent_n = sum(1 for tools in st._parent_mcp_tools.values() for t in tools if t in tool_call_log.GRAPH_TOOLS)
        if len(called) > len(graph) or parent_n > len(called):
            add(REASON_RECORD_MISSING)
        if merged.graph_broken or merged.missing:
            add(REASON_RECORD_GAP)
        if st._tool_use.child_spawned and (st._tool_use.record_unreliable or merged.missing or merged.graph_broken):
            add(REASON_CHILD_UNKNOWN)
    for g in graph:
        for text in _limit_reasons(g):
            add(text)
    if not traced:
        if merged is not None:
            add(REASON_NOT_TRACED)
        return {"v": 1, "traced": False, "rows": [], "reasons": reasons, "more": 0}

    opened = _opened_paths(st)
    used, claims_known = _evidence_paths(env)
    if not claims_known:
        add(REASON_NO_CLAIMS)

    def state_of(path: str, origin: bool) -> tuple[str, str]:
        if not path:
            return "unmapped", _ROW_REASON["unmapped"]
        p = _norm(path)
        if p in used:
            return "used", _ROW_REASON["used"]
        if p in opened:
            return "inspected", _ROW_REASON["inspected"]
        if not claims_known:
            return "unmapped", _UNMAPPED_NO_CLAIMS
        return "candidate", "影響をたどった起点です" if origin else _ROW_REASON["candidate"]

    rows: list[dict] = []
    seen: set = set()

    def put(n: dict, origin: bool) -> None:
        key = n.get("cid") or (n.get("name"), n.get("path"))
        if key in seen:
            return
        seen.add(key)
        path = n.get("path") or ""
        state, reason = state_of(path, origin)
        if not origin and isinstance(n.get("distance"), int) and state == "candidate":
            reason = f"起点から {n['distance']} 段でたどれました"
        rows.append({"name": n["name"], "path": path, "role": "origin" if origin else "affected",
                     "state": state, "reason": reason})

    for g in impact:
        put(g["start"], True)
    for g in impact:
        for n in g.get("rows") or []:
            put(n, False)
    more = max(0, len(rows) - IMPACT_LIST_MAX_ROWS)
    return {"v": 1, "traced": True, "rows": rows[:IMPACT_LIST_MAX_ROWS], "reasons": reasons, "more": more}
