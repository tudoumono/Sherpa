"""`worlds._select_rebind_pending`（rebind 失敗確定に使う保留分の優先順位）を pin する。

複合失敗ケース（新root試行が Neo4j load まで成功→PG replace で失敗・旧root復旧がそれより
早い段階で力尽きる）で、実際に Graph へ反映済みの snapshot を優先して選ぶことを確認する
（DB/Neo4j 不要・純粋な選択ロジックのみ）。

`_finalize_pending_run`（rebind の terminal 化＋Webhook 通知の唯一の発火点）の
`doc_count` 算出（RV是正#9）も併せて検証する。
"""
from __future__ import annotations

from sherpa import store, webhooks, worlds


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


def _stub_finalize(monkeypatch):
    monkeypatch.setattr(store, "finish_ingest_run", lambda run_id, **kw: {"id": run_id, **kw})
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world",
                        lambda run_id, world, **kw: {"id": run_id, **kw})
    notified = []
    monkeypatch.setattr(webhooks, "notify_run_terminal",
                        lambda world, run_id, op, status, **kw: notified.append(
                            {"world": world, "run_id": run_id, "op": op, "status": status, **kw}))
    return notified


def test_finalize_pending_run_reports_zero_doc_count_for_known_empty_result(monkeypatch):
    """RV是正#9: `source_doc_ids` が空リスト（実際に0件で成功・既知）なら `doc_count=0` を
    通知する——旧実装は `len([]) or None` で 0 が None に潰れ、「0件で成功」と「件数不明」を
    見分けられなかった。"""
    notified = _stub_finalize(monkeypatch)
    pending = _pending({"nodes": 0, "edges": 0}, tag="empty")
    pending["source_doc_ids"] = []   # 実際に0件（既知）

    worlds._finalize_pending_run(1, "w-empty", pending, status="auto_published")

    assert notified[0]["doc_count"] == 0


def test_finalize_pending_run_reports_none_doc_count_when_unknown(monkeypatch):
    """`source_doc_ids` が None（未算出・不明）なら `doc_count=None` のまま通知する。"""
    notified = _stub_finalize(monkeypatch)
    pending = _pending(None, tag="unknown")
    pending["source_doc_ids"] = None

    worlds._finalize_pending_run(2, "w-unknown", pending, status="failed")

    assert notified[0]["doc_count"] is None


def test_finalize_pending_run_reports_nonzero_doc_count(monkeypatch):
    notified = _stub_finalize(monkeypatch)
    pending = _pending({"nodes": 3, "edges": 3}, tag="some")
    pending["source_doc_ids"] = ["a.md", "b.md", "c.md"]

    worlds._finalize_pending_run(3, "w-some", pending, status="auto_published")

    assert notified[0]["doc_count"] == 3
