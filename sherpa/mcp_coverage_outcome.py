"""調査の台帳へ記録する、道具の呼び出し結果の区分（`investigation_ledger.COVERAGE_OUTCOMES` の 7 区分）。
設計: docs/design/rag.md「読み取り部品の道具」
"""
from __future__ import annotations

from . import agentic_search

_ITEM_HITS_TOOLS = frozenset({"ripgrep_search", "es_search"})  # `hits` 配列を持つ形
# 単一文書読取の形（原本の Office／PDF の読み取り・見出し・比較を含む）。
_ITEM_READ_TOOLS = frozenset({"read_doc", "read_around", "file_head", "doc_outline", "compare_documents",
                              "xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages", "pdf_page_image"})
_ITEM_LIST_TOOLS = frozenset({"list_docs", "glob_search", "folder_tree"})  # 一覧の形（打ち切りだけを区分にする）
# 単一文書読取の error は「その doc_id を読めなかった」とみなす。検索・グラフ系の error は、`error_code` が既知の「読めない」系コードのときだけ unreadable にする。
_ITEM_UNREADABLE_ERROR_CODES = frozenset({"read_io_failed"})
# 要求した意味の検索を実行できなかった縮退理由。語の検索の 0 件は「見つからない」の根拠にしない（`embedding_not_configured` は未設定の状態で、失敗ではない）。
_ES_SEMANTIC_FAILED_REASONS = frozenset({
    "query_embed_failed", "hybrid_query_failed", "embedding_cloud_unavailable", "vector_feature_mismatch"})


def coverage_outcome(name: str, result, is_error: bool) -> str | None:
    """`item` 付き呼出しの結果を `investigation_ledger.COVERAGE_OUTCOMES` の 7 区分へ落とす。`None` は記録しない対象（呼び出し側の引数誤り）。
    母集団側の打切り（`truncated`）・1 件あたりのバイト予算の打切り（`text_truncated`／`file_truncated`／`byte_clipped`／`partial_hit`／`partial_line`）は、
    利用統計「打ち切りの内訳」（`agentic_search._SEARCH_TRUNCATED_TOOLS`/`_BYTE_CLIP_TOOLS`）と同じ判定キーを使う。
    `timeout` はこの経路では発生しないが、`UNVERIFIED_REASON_CODES` の語彙に対応する区分として残す。
    """
    if not isinstance(result, dict):
        return "error"
    if is_error:
        if result.get("error") == "tool_deadline_exceeded":
            return "timeout"
        if name in _ITEM_READ_TOOLS:
            if result.get("error_code") == agentic_search._READ_INVALID_ARGS_ERROR_CODE:
                return None  # 呼び出し側の引数誤り（範囲外・型不正）は読めない扱いにしない
            return "unreadable"
        if result.get("error_code") in _ITEM_UNREADABLE_ERROR_CODES:
            return "unreadable"
        return "error"
    if name in _ITEM_HITS_TOOLS:
        # 0 件の判定より前に、道具の失敗・母集団側の打切り・バイト予算での打切りを判定する（ヒットがあっても `truncated_docs` があれば truncated）。
        if name == "es_search" and result.get("degrade_reason") in (
                "es_unavailable", "es_query_failed", "es_query_rejected"):
            return "error"
        if result.get("truncated"):
            return "limit"
        if result.get("truncated_docs"):
            return "truncated"
        hits = result.get("hits")
        if isinstance(hits, list) and len(hits) == 0:
            if name == "es_search" and result.get("degrade_reason") in _ES_SEMANTIC_FAILED_REASONS:
                return "error"
            if name == "ripgrep_search" and (result.get("unreadable_files") or result.get("unreadable_dirs")):
                return "unreadable"  # 読めない場所が範囲にあるので、0 件は「見つからない」の根拠にしない
            return "no_hits"
        if isinstance(hits, list) and any(
                isinstance(h, dict) and (h.get("text_truncated") or h.get("file_truncated"))
                for h in hits):
            return "truncated"
        return "hit"
    if name == "name_lookup":
        if result.get("degrade_reason"):
            return "error"  # 台帳が未作成の 0 件は「見つからない」の根拠にしない
        if result.get("truncated") or result.get("candidates_truncated") or result.get("next_offset") is not None:
            return "limit"
        return "hit" if result.get("matches") or result.get("candidates") else "no_hits"
    if name in _ITEM_LIST_TOOLS:
        return "truncated" if (result.get("truncated") or result.get("next_offset") is not None) else "hit"
    if name in ("graph_resolve", "graph_impact"):
        if result.get("error_code"):  # `graph_unavailable`/`graph_internal_error`（isError ではない縮退）
            return "error"
        coverage = result.get("coverage")
        if isinstance(coverage, dict) and coverage.get("complete") is False:
            return "limit"  # 時間切れ・天井・深さの先・件数の上限は、空でも「該当なし・影響なし」（確認済みの 0 件）にしない
        if result.get("truncated"):
            return "limit"
        items = result.get("candidates" if name == "graph_resolve" else "impact")
        if isinstance(items, list) and len(items) == 0:
            return "no_hits"
        return "hit"
    if name == "graph_neighbors":
        if result.get("error_code"):  # `graph_unavailable`/`graph_internal_error`（isError ではない縮退）
            return "error"
        coverage = result.get("coverage")
        if isinstance(coverage, dict) and coverage.get("complete") is False:
            return "limit"  # 時間切れの空・天井の部分結果を「近傍なし」（確認済みの 0 件）にしない
        neighbors = result.get("neighbors")
        if isinstance(neighbors, list) and len(neighbors) == 0:
            return "no_hits"
        if result.get("truncated"):
            return "limit"
        return "hit"
    # read_doc/read_around/file_head（単一文書読取の形）
    if (result.get("text_truncated") or result.get("file_truncated") or result.get("truncated")
            or result.get("byte_clipped") or result.get("partial_hit") or result.get("partial_line")):
        return "truncated"
    return "hit"
