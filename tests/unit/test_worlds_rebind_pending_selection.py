"""`worlds._select_rebind_pending`（rebind 失敗確定に使う保留分の優先順位）を pin する。

複合失敗ケース（新root試行が Neo4j load まで成功→PG replace で失敗・旧root復旧がそれより
早い段階で力尽きる）で、実際に Graph へ反映済みの snapshot を優先して選ぶことを確認する
（DB/Neo4j 不要・純粋な選択ロジックのみ）。
"""
from __future__ import annotations

from sherpa import worlds


def _pending(published_snapshot=None, tag="x"):
    return {"status": "failed", "extraction_snapshot": {"tag": tag},
            "published_snapshot": published_snapshot, "source_doc_ids": None,
            "confirm_sig": None, "confirm_manifest": None,
            "confirm_doc_count": None, "confirm_scan_report": None}


def test_recovery_published_wins_over_attempt_published():
    recovery = _pending({"nodes": 1, "edges": 1}, tag="recovery")
    attempt = _pending({"nodes": 9, "edges": 9}, tag="attempt")
    assert worlds._select_rebind_pending(recovery, attempt) is recovery


def test_attempt_published_wins_when_recovery_not_published():
    """複合失敗ケース: 新root試行は Neo4j load まで成功済み（published_snapshot 有り）だが
    PG replace で失敗、旧root復旧はそれより早い段階（office_md 等）で力尽きて
    published_snapshot が無い——復旧を無条件優先すると実在する Graph の件数を消してしまう。"""
    recovery = _pending(None, tag="recovery")
    attempt = _pending({"nodes": 3, "edges": 5}, tag="attempt")
    assert worlds._select_rebind_pending(recovery, attempt) is attempt


def test_recovery_detail_wins_when_neither_published():
    recovery = _pending(None, tag="recovery")
    attempt = _pending(None, tag="attempt")
    assert worlds._select_rebind_pending(recovery, attempt) is recovery


def test_attempt_used_when_recovery_missing():
    attempt = _pending({"nodes": 2, "edges": 2}, tag="attempt")
    assert worlds._select_rebind_pending(None, attempt) is attempt


def test_fallback_when_both_missing():
    result = worlds._select_rebind_pending(None, None)
    assert result == worlds._REBIND_PENDING_FALLBACK
    assert result is not worlds._REBIND_PENDING_FALLBACK   # コピーを返す（呼び出し元の変更が定数を汚さない）
