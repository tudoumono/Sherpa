"""`scripts/conversation_trace.py`（make trace）が回答の評価のために足す欄（件数・時間・印だけ）を組み立てる。
設計: docs/design/usage.md「§8 利用者別・会話別の見え方」

守ること:
- 道具の結果の本文・シェルの出力・推論は読まない（件数・時間・印だけ）。
- 新しく出す文字列は呼び出し側の `Masker` を通す（理由の語は語彙 `degrade_reason`・範囲は `masker.scope`）。
"""
from __future__ import annotations

from scripts import turn_timeline

TOKEN_KEYS = ("input", "cached_input", "output", "reasoning")
# 道具の名前 → 検索の種類（表に無い道具は「そのほか」）。
TOOL_KIND = {
    "es_search": "fulltext",
    "ripgrep_search": "string",
    "graph_neighbors": "graph", "graph_resolve": "graph", "graph_impact": "graph",
    "doc_outline": "read", "read_doc": "read", "read_around": "read", "file_head": "read",
    "xlsx_sheets": "read", "xlsx_range": "read", "docx_paragraphs": "read", "pptx_slides": "read",
    "pdf_pages": "read", "pdf_page_image": "read", "compare_documents": "read",
    "list_docs": "list", "folder_tree": "list", "glob_search": "list",
}
KIND_LABEL = (("fulltext", "全文検索"), ("string", "文字列検索"), ("graph", "グラフ"), ("read", "原本の読み取り"),
              ("list", "一覧"), ("other", "そのほか"))
# 全文検索の結果の `degrade_reason`（キーワードだけに落ちた・失敗した理由）。
DEGRADE_REASONS = frozenset({
    "es_unavailable", "es_query_failed", "es_query_rejected", "embedding_not_configured",
    "embedding_cloud_unavailable", "hybrid_query_failed", "vector_feature_mismatch", "query_embed_failed"})
_WINDOW_BEFORE_S = 1.0
_WINDOW_AFTER_S = 2.0
_MODES = ("hybrid", "vector", "keyword")
_MODE_LABEL = (("hybrid", "ハイブリッド"), ("vector", "ベクトルのみ"), ("keyword", "キーワード指定"))


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def turn_window(u, a, audit, answer: dict, epoch) -> tuple:
    """ターンの (開始, 終了, セッション記録を見る窓の始まりと終わり)（エポック秒）。epoch は時刻の文字列→エポック秒。"""
    start = epoch((u or {}).get("created_at")) if u else None
    end = epoch(a.get("created_at")) if a else epoch((audit or {}).get("created_at"))
    dur = answer.get("duration_ms") if isinstance(answer.get("duration_ms"), int) else None
    if start is None and end is not None and dur is not None:
        start = end - dur / 1000
    if start is None:
        start = end
    w_start = (start or 0) - _WINDOW_BEFORE_S
    w_end = end + _WINDOW_AFTER_S if end is not None else w_start - 1
    return start, end, w_start, w_end


def heading_times(u, a, epoch, fmt) -> str:
    """見出しに添える質問と回答の時刻。質問と対にできない（u が無い）・回答が無いときはその欄を出さない。"""
    q = epoch(u.get("created_at")) if u else None
    r = epoch(a.get("created_at")) if a else None
    return (f"  質問時刻 {fmt(q)}" if q else "") + (f"  回答時刻 {fmt(r)}" if r else "")


def uncached(row) -> int | None:
    """実入力（入力 − キャッシュ）。値が揃わない・入力がキャッシュより小さい壊れた値は None。"""
    if not isinstance(row, (list, tuple)) or len(row) < 2 or not (_is_int(row[0]) and _is_int(row[1])) or row[0] < row[1]:
        return None
    return row[0] - row[1]


def uncached_text(row, fmt) -> str:
    u = uncached(row)
    return f" 実入力 {fmt(u)}" if u is not None else ""


def token_dict(row, usage=None) -> dict | None:
    """トークンの行を欄の名前つきにする。実入力は、元の記録にキャッシュの欄があるときだけ足す。"""
    if not row:
        return None
    out = dict(zip(TOKEN_KEYS, row))
    u = uncached(row) if isinstance(usage, dict) and _is_int(usage.get("cached_input_tokens")) else None
    if u is not None:
        out["uncached_input"] = u
    return out


def timing(threads, w_start: float, w_end: float) -> dict | None:
    """ターンの時間帯の道具・シェル・モデルの応答待ちの合計（ms）。数え方は `turn_timeline.analyze` と同じ。実行の記録が無ければ None。"""
    shaped = [{"subagent": bool(t.subagent or t.parent_tid),
               "events": [(ep, rec) for ep, rec in t.events if w_start <= ep <= w_end]} for t in threads]
    try:
        res = turn_timeline.analyze(shaped)
        if not res["attempts"]:
            return None
        total = res["total"]
        return {"tools_ms": round(total["mcp_ms"]), "shell_ms": round(total["exec_ms"]),
                "model_wait_ms": round(total["model_wait_ms"])}
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def kind_of(name) -> str:
    return TOOL_KIND.get(name, "other") if isinstance(name, str) else "other"


def _kind_slot(out: dict, name) -> dict:
    return out.setdefault(kind_of(name), {"calls": 0, "ms": 0, "errors": 0, "truncated": 0})


def kind_stats(calls, truncated, parsed) -> dict:
    """道具の呼び出しを検索の種類ごとに数える（回数・時間の合計・失敗・打ち切り）。
    時間が記録されていない呼び出し（終わりの記録が無いもの）は `no_duration` に数え、時間の合計には入れない。"""
    out: dict = {}
    for c in calls:
        p = parsed(c)
        s = _kind_slot(out, c.name)
        s["calls"] += 1
        if _is_int(c.duration_ms):
            s["ms"] += c.duration_ms
        else:
            s["no_duration"] = s.get("no_duration", 0) + 1
        s["errors"] += 1 if c.is_error or (isinstance(p, dict) and "error" in p) else 0
        s["truncated"] += 1 if truncated(c) else 0
    return out


def kind_stats_from_call_stats(call_stats) -> dict:
    """セッション記録が無いとき、回答の `call_stats.tools`（道具ごとの累計）から同じ形にまとめる。"""
    out: dict = {}
    for t in (call_stats.get("tools") if isinstance(call_stats, dict) else None) or []:
        if not isinstance(t, dict):
            continue
        s = _kind_slot(out, t.get("tool"))
        for k, key in (("calls", "calls"), ("ms", "ms"), ("errors", "errors"), ("truncated", "truncated")):
            s[k] += t[key] if _is_int(t.get(key)) else 0
    return out


def search_modes(calls, parsed, masker) -> dict | None:
    """全文検索の結果の方式の数え方。方式が載っていない呼び出しは「不明」（成功と数えない）。全文検索が無ければ None。"""
    out: dict | None = None
    for c in calls:
        if c.name != "es_search":
            continue
        if out is None:
            out = {"hybrid": 0, "vector": 0, "keyword": 0, "degraded": {}, "unknown": 0}
        p = parsed(c)
        if not isinstance(p, dict) or "error" in p:
            out["unknown"] += 1
        elif isinstance(p.get("degrade_reason"), str) and p["degrade_reason"]:
            key = masker.word(p["degrade_reason"], "degrade_reason")
            out["degraded"][key] = out["degraded"].get(key, 0) + 1
        elif p.get("mode_used") in _MODES:
            out[p["mode_used"]] += 1
        else:
            out["unknown"] += 1
    return out


def turn_facts(*, asked_at, answer: dict, calls, threads, window, masker, truncated, parsed) -> dict:
    """1 ターンの評価の欄。記録が無い欄は出さない。"""
    out: dict = {}
    if asked_at:
        out["asked_at"] = asked_at
    tm = timing(threads, *window) if threads else None
    if tm:
        out["time"] = tm
    kinds = kind_stats(calls, truncated, parsed) if calls else kind_stats_from_call_stats(answer.get("call_stats"))
    if kinds:
        out["tool_kinds"] = kinds
    modes = search_modes(calls, parsed, masker)
    if modes is not None:
        out["es_modes"] = modes
    scope = answer.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("scope_paths"), list):
        paths = scope["scope_paths"]
        out["scope_paths"] = [masker.scope(p) for p in paths if isinstance(p, str)]
        out["scope_whole"] = not paths
    return out


def _kind_text(s: dict, dur) -> str:
    parts = [f"計 {dur(s['ms'])}"] if "ms" in s else []
    parts.append(f"失敗 {s['errors']}")
    if "truncated" in s:
        parts.append(f"打切 {s['truncated']}")
    if s.get("no_duration"):
        parts.append(f"時間の記録なし {s['no_duration']}")
    return "（" + "・".join(parts) + "）"


def fact_lines(facts: dict, dur_ms, dur) -> list[str]:
    """テキスト用の行（時間の内訳・検索の種類・全文検索の方式）。"""
    lines: list[str] = []
    tm = facts.get("time")
    if tm:
        lines.append(f"  時間の内訳: 回答全体 {dur(dur_ms)}／道具 {dur(tm['tools_ms'])}／シェル {dur(tm['shell_ms'])}"
                     f"／モデルの応答待ち {dur(tm['model_wait_ms'])}")
    kinds = facts.get("tool_kinds") or {}
    if kinds:
        lines.append("  検索の種類: " + "／".join(f"{label} {kinds[k]['calls']} 回{_kind_text(kinds[k], dur)}"
                                              for k, label in KIND_LABEL if k in kinds))
    modes = facts.get("es_modes")
    if modes:
        degraded = modes["degraded"]
        parts = [f"{label} {modes[k]}" for k, label in _MODE_LABEL]
        parts.append(f"キーワードに縮退 {sum(degraded.values())}"
                     + (f"（{' '.join(f'{r}={n}' for r, n in degraded.items())}）" if degraded else ""))
        parts.append(f"不明 {modes['unknown']}")
        lines.append("  全文検索の方式: " + "／".join(parts))
    return ["[評価の材料]", *lines] if lines else []


def conversation_total(entries: list[dict]) -> dict:
    """会話 1 本の合計。entries は {duration_ms, tokens, tool_kinds} の並び（ターンごと）。
    `duration_turns`・`token_turns`・`uncached_input_turns` は、その合計に入ったターンの数（`turns` より少なければ記録の無いターンがある）。"""
    out: dict = {"turns": len(entries)}
    durs = [e["duration_ms"] for e in entries if _is_int(e.get("duration_ms"))]
    if durs:
        out["duration_ms"] = sum(durs)
        out["duration_turns"] = len(durs)
    toks = [e["tokens"] for e in entries if isinstance(e.get("tokens"), dict)]
    if toks:
        out["token_turns"] = len(toks)
        out["tokens"] = {k: sum(t.get(k) or 0 for t in toks) for k in TOKEN_KEYS}
        known = [t["uncached_input"] for t in toks if "uncached_input" in t]
        if known:
            out["tokens"]["uncached_input"] = sum(known)
            out["uncached_input_turns"] = len(known)
    kinds: dict = {}
    for e in entries:
        for k, s in (e.get("tool_kinds") or {}).items():
            slot = kinds.setdefault(k, {"calls": 0, "errors": 0})
            slot["calls"] += s["calls"]
            slot["errors"] += s["errors"]
    if kinds:
        out["tool_kinds"] = kinds
    return out


def _partial(total: dict, key: str) -> str:
    n = total.get(key)
    return f"（{n} ターン分）" if _is_int(n) and n < total["turns"] else ""


def total_lines(total: dict, tok, dur) -> list[str]:
    """テキスト用の会話の合計。tok は [入力, キャッシュ, 出力, 推論] の表記、dur は ms の表記。"""
    head = f"[会話の合計]  ターン {total['turns']}"
    if "duration_ms" in total:
        head += f"  回答の時間 {dur(total['duration_ms'])}" + _partial(total, "duration_turns")
    toks = total.get("tokens")
    if toks:
        head += "  " + tok([toks[k] for k in TOKEN_KEYS]) + _partial(total, "token_turns")
    kinds = total.get("tool_kinds") or {}
    if kinds:
        head += "  道具: " + "／".join(f"{label} {kinds[k]['calls']} 回（失敗 {kinds[k]['errors']}）"
                                   for k, label in KIND_LABEL if k in kinds)
    return [head]
