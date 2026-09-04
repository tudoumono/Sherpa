"""影響分析エンジン（鏡モデル）: world＋範囲フィルタの影響たどり（MIRROR-MODEL §2-§3）。

実体は `ingest.world_neo4j`（world_id＋scope_prefixes の正準 Cypher）。本モジュールは入口（`run_impact`）と
結果カテゴリ表（`CATEGORY`）を提供する。判定: static→●確実(sure)／llm 経路→○要確認(review)（ONTOLOGY §3）。
特定テーマの名前はコードに持たない（名寄せ表は呼び出し側がデータで渡す）。
"""
import os
import re


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`ingest.world_neo4j._env_int` と同一セマンティクス）。

    `ingest.world_neo4j` は本モジュールを import する側（`from ..impact_service import CATEGORY`）の
    ため、ここで `ingest.world_neo4j` を import すると循環 import になる。同じ検証ロジックを複製する
    （`lens_service`/`agentic_search`/`grep_tool`/`es_index` と同型・_NEO4J_QUERY_TIMEOUT_S と同じ
    「同じ env 変数を複数モジュールで共用」パターン）。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# 影響たどりの既定深さ。`ingest.world_neo4j.IMPACT_MAX_DEPTH` と同じ env 変数を共用する
# （両モジュールは循環 import のため定数を共有できず、同一ロジックを複製して同じ値に揃える）。
IMPACT_MAX_DEPTH = _env_int("SHERPA_IMPACT_MAX_DEPTH", 8, 1, 64)
# env-parse hi 引数と同じ値。調べる深さ（`depth_profile.scaled_depth`）が加算適用後に一度だけ
# 適用する絶対上限として使う（管理画面の基準値編集が Field 上限まで・調べる深さ「最大」＝+4 の
# 組み合わせで env-parse の上限を超えて伸びるのを防ぐ）。
IMPACT_MAX_DEPTH_ABS_MAX = 64

# 種別ラベル → 結果カテゴリ（MVP-DETAIL §4.2）。world_neo4j/lens_service が再利用する。
# K13（2026-09-04-グラフのソース正典化.md §4）で供給源を失った概念（Function/Screen/Report/
# BusinessRule/Parameter/Standard/Incident）は刈った——語彙（`ingest.model.NODE_LABELS`）と揃える。
CATEGORY = {
    "Module": "ソース", "Copybook": "ソース", "DataItem": "ソース",
    "Batch": "バッチ", "Document": "文書", "Table": "テーブル",
}

# 推定トレースで拾う「コード成果物」ラベル（変更対象になり得るもの）。概念/文書は対象外。
_CODE_LABELS = {"Module", "Copybook", "DataItem", "Batch", "Table"}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")   # COBOL 風識別子（3文字以上）
# 実在ノード名と一致しても「関連」と見なさない一般語（誤検出抑止・RV Med）。
_STOP = {"DATA", "CODE", "RATE", "TOTAL", "INPUT", "OUTPUT", "FILE", "DATE", "TIME", "NAME", "TYPE",
         "FLAG", "AREA", "ITEM", "LIST", "NUM", "KEY", "VAL", "MAX", "MIN", "AVG", "ALL", "NEW", "OLD",
         "API", "SQL", "CSV", "PDF", "URL", "HTTP", "JSON", "XML", "COBOL", "JCL", "REC", "SUM", "AMT"}


from .ingest.identifiers import normalize_code_name as _norm   # 正規化は identifiers に集約（単一の真実源・RV DRY）


def _truncated_search_note(doc_ids: list) -> str | None:
    """`grep_search(truncated_docs=...)` の申告 → 利用者向け平文の注記1件。`lens_service` の
    同名関数と同一セマンティクス（`lens_service._env_int` と同じ理由で複製する——
    `lens_service` は本モジュールを import する側（`from .impact_service import CATEGORY`）の
    ため、ここで `lens_service` を import すると循環 import になる）。打切りが無ければ `None`
    （呼び出し元はこのとき `notes` キー自体を作らない＝加算的変更）。内部語彙（`file_truncated`／
    cap／バイト）は出さない（docs/04-画面の原則.md の平文原則）。
    """
    if not doc_ids:
        return None
    if len(doc_ids) == 1:
        return f"「{doc_ids[0]}」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    shown = "」「".join(doc_ids[:5])
    more = f" ほか{len(doc_ids) - 5}件" if len(doc_ids) > 5 else ""
    return f"次の資料は大きすぎて全体を検索できていません（先頭部分のみ）: 「{shown}」{more}"


def presumed_impact(session, term, world, scope_prefixes=None, max_items=20, truncated_docs=None):
    """**●確実が0件のとき**、資料から関連コードを**推定**して返す（橋＝対応づけが無くても 0 で突き放さない）。

    業務語を grep → 同じ文/節に出るコード識別子を**実在グラフノードに裏付け**して「関連の可能性（推定）」に。
    決定的（grep＋名前一致・LLM 不使用）。各件に根拠（doc/行/引用）。判定は `presumed`（●確実とは別表示）。
    **scope/path 同一性を守る**: ノード取得は範囲フィルタ＋世代(top_scope)込みで解決し、同名が複数世代で曖昧なら
    任意選択せず捨てる（鏡＝曖昧は繋がない・RV High）。一般語は除外（RV Med）。

    `truncated_docs`（省略可・`grep_search` と同型の out-param）を渡すと、grep が打ち切った文書の
    doc_id が重複なく追記される——`run_impact` が `notes`（平文の注記）へ翻訳する。
    """
    from .grep_tool import grep_search
    from . import scope as scope_mod
    from .ingest.world_neo4j import _run_read_capped, _scope_pred
    sp = scope_mod.normalize_scope_paths(scope_prefixes) or None
    hits = grep_search(term, world, scope_paths=sp, max_hits=30, truncated_docs=truncated_docs)
    if not hits:
        return []
    by_gen, by_name = {}, {}                            # (top,norm名)→node ／ norm名→[node...]（曖昧判定用）
    # secRV 範囲外是正（2026-07-19・Neo4j 安全弁）: `_run_read_capped` で timeout＋緊急天井を付与する。
    # ここは presumed（●確実0件の時だけ動く最善努力の推定）なので、天井/timeout で
    # `GraphQueryOverloadError` が上がっても呼び出し元 `run_impact` の既存 `except Exception:
    # result["presumed"] = []` がそのまま吸収する（コアの sure/review 判定＝world_impact 自体は
    # 別途 fail-loud・presumed は「見つからなかった」と「調べられなかった」を区別しない既存仕様のまま）。
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
        doc_top = doc.split("/", 1)[0]                  # 共起した文書の世代（top_scope）
        for m in _TOKEN_RE.findall(text):
            nk = _norm(m)
            if nk in _STOP:
                continue
            node = by_gen.get((doc_top, nk))            # まず同世代で解決（文書と同じ top_scope の実ノード）
            if node is None:
                cands = by_name.get(nk)
                if not cands or len(cands) > 1:         # 世代跨ぎで曖昧＝任意に繋がない（鏡）
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
    """起点語 → 影響結果（world＋範囲フィルタ・emit_result 形）。`ingest.world_neo4j` に委譲。

    既定は active のみ。`include_deprecated=True` で deprecated/hidden_candidate も含む（AT-5）。
    `scope_prefixes`（フォルダ prefix）で world を絞る（空＝world 全体・MIRROR §3）。
    **構造的な影響が0件**なら `presumed`（資料からの関連推定）を添える（業務語の橋が無くても答えを返す）。

    推定トレースの grep が打ち切られた文書があれば `notes`（平文の注記1件・`_truncated_search_note`）
    を添える（打切りが無ければ `notes` キー自体を作らない＝加算的変更）。
    """
    from .ingest.world_neo4j import GraphQueryOverloadError, run_world_impact   # 遅延 import（循環回避）
    result = run_world_impact(session, term, world, scope_prefixes, depth, include_deprecated)
    if not result.get("items"):
        truncated_docs: list = []
        try:
            result["presumed"] = presumed_impact(session, term, world, scope_prefixes,
                                                  truncated_docs=truncated_docs)
        except GraphQueryOverloadError:
            # secRV 範囲外是正 追補（2026-07-19・RV指摘 MED-1）: presumed は best-effort だが、
            # overload（timeout/緊急天井）まで下の broad except で [] に握り潰すと「関連コードは
            # 見つからなかった」（0件）と「調べられなかった」（安全弁で打ち切り）の区別がつかず
            # 偽陰性になる。fail-loud 契約を貫くため re-raise し、呼び出し側（routers/impact.py の
            # 503・chat_service のチャット固定文言）へ可視化させる。
            raise
        except Exception:
            result["presumed"] = []
        note = _truncated_search_note(truncated_docs)
        if note:
            result["notes"] = [note]
    return result
