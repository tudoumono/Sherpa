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
_KIND_KEYS = ("calls", "ms", "errors", "truncated")
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
    """ターンの時間帯の道具・シェル・モデルの応答待ちの合計（ms）。数え方は `turn_timeline.analyze` と同じで、この 3 つは本体の分だけ。
    下調べ役がいれば、その数と道具の時間を `sub_threads`・`sub_tools_ms` に分けて出す。実行の記録が無い・読めなければ None。"""
    shaped = [{"subagent": bool(t.subagent or t.parent_tid),
               "events": [(ep, rec) for ep, rec in t.events if w_start <= ep <= w_end]} for t in threads]
    try:
        res = turn_timeline.analyze(shaped)
        if not res["attempts"]:
            return None
        total, sub = res["total"], res["children"]
        out = {"tools_ms": round(total["mcp_ms"]), "shell_ms": round(total["exec_ms"]),
               "model_wait_ms": round(total["model_wait_ms"])}
        if sub["threads"]:
            out.update(sub_threads=sub["threads"], sub_tools_ms=round(sub["mcp_ms"]))
        return out
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def kind_of(name) -> str:
    return TOOL_KIND.get(name, "other") if isinstance(name, str) else "other"


def kind_stats(call_stats) -> dict:
    """回答の `call_stats.tools`（アプリが記録した道具ごとの累計）を検索の種類ごとにまとめる。
    回数・時間・失敗・打ち切りの数え方はアプリの記録のまま。記録に無い欄は足さない。"""
    out: dict = {}
    for t in (call_stats.get("tools") if isinstance(call_stats, dict) else None) or []:
        if not isinstance(t, dict) or not _is_int(t.get("calls")):
            continue
        s = out.setdefault(kind_of(t.get("tool")), {"calls": 0})
        for key in _KIND_KEYS:
            if _is_int(t.get(key)):
                s[key] = s.get(key, 0) + t[key]
    return out


def result_errors(calls, parsed) -> dict:
    """結果に `error`／`error_code` の欄があった呼び出しの数（`kinds`＝検索の種類ごと）。道具が失敗の印を付けずに返した不調も入る。
    `checked` は結果を読めた呼び出しの数、`unknown` は結果が無い・読めない呼び出しの数。"""
    out: dict = {"checked": 0, "unknown": 0, "kinds": {}}
    for c in calls:
        p = parsed(c)
        if not isinstance(p, dict):
            out["unknown"] += 1
            continue
        out["checked"] += 1
        if "error" in p or "error_code" in p:
            k = kind_of(c.name)
            out["kinds"][k] = out["kinds"].get(k, 0) + 1
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


def turn_facts(*, asked_at, answer: dict, calls, threads, window, masker, parsed) -> dict:
    """1 ターンの評価の欄。記録が無い欄は出さない。"""
    out: dict = {}
    if asked_at:
        out["asked_at"] = asked_at
    tm = timing(threads, *window) if threads else None
    if tm:
        open_calls = sum(1 for c in calls if not _is_int(c.duration_ms))
        if open_calls:
            tm["no_end_calls"] = open_calls
        out["time"] = tm
    kinds = kind_stats(answer.get("call_stats"))
    if kinds:
        out["tool_kinds"] = kinds
    if calls:
        out["result_errors"] = result_errors(calls, parsed)
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
    if "errors" in s:
        parts.append(f"失敗 {s['errors']}")
    if "truncated" in s:
        parts.append(f"打切 {s['truncated']}")
    return "（" + "・".join(parts) + "）" if parts else ""


def fact_lines(facts: dict, dur_ms, dur) -> list[str]:
    """テキスト用の行（時間の内訳・検索の種類・全文検索の方式）。"""
    lines: list[str] = []
    tm = facts.get("time")
    if tm:
        lines.append(f"  時間の内訳: 回答全体 {dur(dur_ms)}／道具 {dur(tm['tools_ms'])}／シェル {dur(tm['shell_ms'])}"
                     f"／モデルの応答待ち {dur(tm['model_wait_ms'])}"
                     + (f"（本体の分。下調べ役 {tm['sub_threads']} の道具 {dur(tm['sub_tools_ms'])} は別）" if "sub_threads" in tm else "")
                     + (f"（終わりの記録が無い呼び出し {tm['no_end_calls']} 件は時間に入っていない）" if "no_end_calls" in tm else ""))
    kinds = facts.get("tool_kinds") or {}
    if kinds:
        lines.append("  検索の種類: " + "／".join(f"{label} {kinds[k]['calls']} 回{_kind_text(kinds[k], dur)}"
                                              for k, label in KIND_LABEL if k in kinds))
    soft = facts.get("result_errors")
    if soft:
        kinds_e = soft["kinds"]
        lines.append("  結果に不調の印があった呼び出し: "
                     + ("／".join(f"{label} {kinds_e[k]}" for k, label in KIND_LABEL if k in kinds_e) or "なし")
                     + f"（結果を読めた {soft['checked']} 件のうち・読めない {soft['unknown']} 件）")
    modes = facts.get("es_modes")
    if modes:
        degraded = modes["degraded"]
        parts = [f"{label} {modes[k]}" for k, label in _MODE_LABEL]
        parts.append(f"縮退・失敗 {sum(degraded.values())}"
                     + (f"（{' '.join(f'{r}={n}' for r, n in degraded.items())}）" if degraded else ""))
        parts.append(f"不明 {modes['unknown']}")
        lines.append("  全文検索の方式: " + "／".join(parts))
    return ["[評価の材料]", *lines] if lines else []


def conversation_total(entries: list[dict]) -> dict:
    """会話 1 本の合計。entries は {duration_ms, tokens, tool_kinds} の並び（ターンごと）。
    `duration_turns`・`token_turns`・`uncached_input_turns`・`tool_turns` は、その合計に入ったターンの数（`turns` より少なければ記録の無いターンがある）。"""
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
    with_tools = [e["tool_kinds"] for e in entries if e.get("tool_kinds")]
    for tk in with_tools:
        for k, s in tk.items():
            slot = kinds.setdefault(k, {"calls": 0})
            for key in _KIND_KEYS:
                if key in s:
                    slot[key] = slot.get(key, 0) + s[key]
    if kinds:
        out["tool_kinds"] = kinds
        out["tool_turns"] = len(with_tools)
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
        head += "  道具" + _partial(total, "tool_turns") + ": " + "／".join(f"{label} {kinds[k]['calls']} 回{_kind_text(kinds[k], dur)}"
                                   for k, label in KIND_LABEL if k in kinds)
    return [head]
