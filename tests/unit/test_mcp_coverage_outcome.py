"""調査の台帳へ記録する検索結果の区分: 検索を最後まで実行できなかった 0 件は「見つからない」にしない。"""
from __future__ import annotations

import pytest

from sherpa.mcp_coverage_outcome import coverage_outcome


@pytest.mark.parametrize("name,result,expected", [
    ("es_search", {"hits": [], "degrade_reason": "query_embed_failed", "mode_used": "keyword"}, "error"),
    ("es_search", {"hits": [], "degrade_reason": "hybrid_query_failed"}, "error"),
    ("es_search", {"hits": [], "degrade_reason": "embedding_cloud_unavailable"}, "error"),
    ("es_search", {"hits": [], "degrade_reason": "vector_feature_mismatch"}, "error"),
    ("es_search", {"hits": [], "degrade_reason": "embedding_not_configured"}, "no_hits"),
    ("es_search", {"hits": [{"doc_id": "a", "text": "x"}], "degrade_reason": "query_embed_failed"}, "hit"),
    ("ripgrep_search", {"hits": [], "unreadable_dirs": 1}, "unreadable"),
    ("ripgrep_search", {"hits": [], "unreadable_files": 2}, "unreadable"),
    ("ripgrep_search", {"hits": []}, "no_hits"),
])
def test_zero_hits_after_incomplete_search_is_not_no_hits(name, result, expected):
    assert coverage_outcome(name, result, False) == expected
