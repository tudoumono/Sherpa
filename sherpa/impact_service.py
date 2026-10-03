"""影響分析エンジン: world＋範囲フィルタの影響たどり。

実体は `ingest.world_neo4j`。本モジュールは入口（`run_impact`）と結果カテゴリ表（`CATEGORY`）を提供する。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
import re


# 影響たどりの既定深さ（管理画面の基準値が未設定のときに使う）
IMPACT_MAX_DEPTH = 10
# 調べる深さ（`depth_profile.scaled_depth`）の絶対上限
IMPACT_MAX_DEPTH_ABS_MAX = 64

# 種別ラベル → 結果カテゴリ。world_neo4j/lens_service が再利用する
CATEGORY = {
    "Module": "ソース", "Copybook": "ソース", "DataItem": "ソース",
    "Batch": "バッチ", "Document": "文書", "Table": "テーブル", "Config": "設定",
}

# 推定トレースで拾う「コード成果物」ラベル（概念/文書は対象外）
_CODE_LABELS = {"Module", "Copybook", "DataItem", "Batch", "Table"}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")  # COBOL 風識別子（3文字以上）
# 実在ノード名と一致しても「関連」と見なさない一般語
_STOP = {"DATA", "CODE", "RATE", "TOTAL", "INPUT", "OUTPUT", "FILE", "DATE", "TIME", "NAME", "TYPE",
         "FLAG", "AREA", "ITEM", "LIST", "NUM", "KEY", "VAL", "MAX", "MIN", "AVG", "ALL", "NEW", "OLD",
         "API", "SQL", "CSV", "PDF", "URL", "HTTP", "JSON", "XML", "COBOL", "JCL", "REC", "SUM", "AMT"}


from .ingest.identifiers import normalize_code_name as _norm


def _truncated_search_note(doc_ids: list) -> str | None:
    """`grep_search(truncated_docs=...)` の申告 → 利用者向け平文の注記1件（`lens_service` の同名関数と同一・循環回避で複製）。
    打切りが無ければ `None`。内部語彙は出さない。
    """
    if not doc_ids:
        return None
    if len(doc_ids) == 1:
        return f"「{doc_ids[0]}」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    shown = "」「".join(doc_ids[:5])
    more = f" ほか{len(doc_ids) - 5}件" if len(doc_ids) > 5 else ""
    return f"次の資料は大きすぎて全体を検索できていません（先頭部分のみ）: 「{shown}」{more}"


def presumed_impact(session, term, world, scope_prefixes=None, max_items=20, truncated_docs=None):
    """●確実が0件のとき、資料から関連コードを推定して返す。

    業務語を grep → 同じ文/節に出るコード識別子を実在グラフノードに裏付けて「関連の可能性（推定）」とする（決定的・LLM 不使用）。
    各件に根拠（doc/行/引用）。ノード解決は範囲フィルタ＋世代込みで、同名が複数世代で曖昧なら捨てる。一般語は除外。
    `truncated_docs`（省略可）を渡すと、grep が打ち切った文書の doc_id が追記される。
    """
    from .grep_tool import grep_search
    from . import scope as scope_mod
    from .ingest.world_neo4j import _run_read_capped, _scope_pred
    sp = scope_mod.normalize_scope_paths(scope_prefixes) or None
    hits = grep_search(term, world, scope_paths=sp, max_hits=30, truncated_docs=truncated_docs)
    if not hits:
        return []
    by_gen, by_name = {}, {}  # (top,norm名)→node ／ norm名→[node...]（曖昧判定用）
    # `_run_read_capped` で timeout＋緊急天井を付ける（presumed は最善努力。超過は呼び出し元 `run_impact` が扱う）
    for r in _run_read_capped(
            session,
            "MATCH (n:Entity {world_id:$w}) WHERE n.name IS NOT NULL "
            f"  AND {_scope_pred('n')} "
            "RETURN n.name AS name, [l IN labels(n) WHERE l<>'Entity'][0] AS label, "
            "  n.canonical_id AS cid, n.path AS path, n.top_scope AS top",
            world=world, w=world, prefixes=list(scope_prefixes or [])):
        if r["label"] not in _CODE_LABELS:
            continue
        node = {"name": r["name"], "label": r["label"], "cid": r["cid"],
                "path": r["path"], "top_scope": r["top"]}
        by_gen.setdefault((r["top"], _norm(r["name"])), node)
        by_name.setdefault(_norm(r["name"]), []).append(node)
    if not by_name:
        return []
    found = {}
    for h in hits:
        text = h.get("text") or ""
        doc = h.get("doc_id") or ""
        doc_top = doc.split("/", 1)[0]  # 共起した文書の世代（top_scope）
        for m in _TOKEN_RE.findall(text):
            nk = _norm(m)
            if nk in _STOP:
                continue
            node = by_gen.get((doc_top, nk))  # まず同世代で解決
            if node is None:
                cands = by_name.get(nk)
                if not cands or len(cands) > 1:  # 世代跨ぎで曖昧なら繋がない
                    continue
                node = cands[0]
            key = node["cid"] or (node["top_scope"], nk)
            if key in found:
                continue
            quote = next((ln.strip() for ln in text.splitlines() if m in ln), text)[:160]
            found[key] = {"name": node["name"], "label": node["label"],
                          "category": CATEGORY.get(node["label"], node["label"]), "judgement": "presumed",
                          "path": node["path"], "top_scope": node["top_scope"],
                          "evidence": [{"doc": doc, "line": h.get("line"), "quote": quote}]}
            if len(found) >= max_items:
                return list(found.values())
    return list(found.values())


def run_impact(session, term, world, scope_prefixes=None,
               depth=IMPACT_MAX_DEPTH, include_deprecated=False):
    """起点語 → 影響結果（world＋範囲フィルタ・emit_result 形）。`ingest.world_neo4j` に委譲する。

    既定は active のみ（`include_deprecated=True` で deprecated/hidden_candidate も含む）。`scope_prefixes` で絞る（空＝world 全体）。
    構造的な影響が0件なら `presumed` を添える。推定の grep が打ち切った文書があれば `notes`（平文の注記1件）を添える。
    """
    from .ingest.world_neo4j import GraphQueryOverloadError, run_world_impact  # 遅延 import（循環回避）
    result = run_world_impact(session, term, world, scope_prefixes, depth, include_deprecated)
    if not result.get("items"):
        truncated_docs: list = []
        try:
            result["presumed"] = presumed_impact(session, term, world, scope_prefixes,
                                                  truncated_docs=truncated_docs)
        except GraphQueryOverloadError:
            # overload（timeout/緊急天井）は「0件」と区別するため握り潰さず re-raise する
            raise
        except Exception:
            result["presumed"] = []
        note = _truncated_search_note(truncated_docs)
        if note:
            result["notes"] = [note]
    return result
