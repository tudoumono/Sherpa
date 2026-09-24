"""バッチ3（2026-07-03）: 利用統計「定着指標」の純粋関数 `store._compute_retention` の単体テスト。

`usage_stats()` 本体は共有 dev DB の既存データ（残骸含む）に集計が引きずられ、再訪率の期待値を
精密に検証しづらいため、週次アクティブユーザー集合→再訪率の計算ロジックはここで DB 無しに固定する
（API 側は tests/api/test_usage_stats.py::test_usage_stats_retention_field_present_with_expected_shape
で応答の形だけを確認する）。
"""
from __future__ import annotations

from datetime import date

from sherpa import store


def _rows(*pairs):
    """(uid, week_start) のタプル列 → store._compute_retention に渡す行リストへ変換。"""
    return [{"uid": uid, "week_start": week_start} for uid, week_start in pairs]


def test_retention_empty_input_returns_empty_weekly_and_none_revisit_rate():
    out = store._compute_retention([])
    assert out == {"weekly": [], "revisit_rate": None}


def test_retention_single_week_has_no_revisit_rate():
    """週が1つしか無い（比較できる「前週」が無い）と revisit_rate は None。"""
    rows = _rows(("a", date(2026, 6, 22)), ("b", date(2026, 6, 22)))
    out = store._compute_retention(rows)
    assert out["weekly"] == [{"week_start": "2026-06-22", "active_users": 2}]
    assert out["revisit_rate"] is None


def test_retention_two_consecutive_weeks_computes_revisit_rate():
    """前週{a,b}・当週{a,c}: a が再訪＝2人中1人＝revisit_rate=0.5。"""
    rows = _rows(
        ("a", date(2026, 6, 22)), ("b", date(2026, 6, 22)),
        ("a", date(2026, 6, 29)), ("c", date(2026, 6, 29)),
    )
    out = store._compute_retention(rows)
    assert out["weekly"] == [
        {"week_start": "2026-06-22", "active_users": 2},
        {"week_start": "2026-06-29", "active_users": 2},
    ]
    assert out["revisit_rate"] == 0.5


def test_retention_all_users_revisit_gives_rate_one():
    rows = _rows(
        ("a", date(2026, 6, 22)), ("b", date(2026, 6, 22)),
        ("a", date(2026, 6, 29)), ("b", date(2026, 6, 29)),
    )
    out = store._compute_retention(rows)
    assert out["revisit_rate"] == 1.0


def test_retention_no_overlap_gives_rate_zero():
    rows = _rows(
        ("a", date(2026, 6, 22)), ("b", date(2026, 6, 22)),
        ("c", date(2026, 6, 29)), ("d", date(2026, 6, 29)),
    )
    out = store._compute_retention(rows)
    assert out["revisit_rate"] == 0.0


def test_retention_gap_week_is_not_treated_as_previous_week():
    """週の間が7日でない（1週分の空白がある）ペアは「前週→当週」として扱わない
    （プールする再訪率の分子/分母に含めない）。3週分あるが連続ペアが1組も無いケース。"""
    rows = _rows(
        ("a", date(2026, 6, 15)),
        ("b", date(2026, 6, 29)),   # 6/15 の2週間後＝連続でない
        ("a", date(2026, 7, 13)),   # 6/29 の2週間後＝連続でない
    )
    out = store._compute_retention(rows)
    assert len(out["weekly"]) == 3
    assert out["revisit_rate"] is None, "非連続の週ペアが再訪率計算に混入した"


def test_retention_pools_multiple_consecutive_pairs_not_averages_per_pair_rate():
    """3週連続: week1{a,b,c,d}(4人)→week2{a}(1人再訪/4人中)→week3{a,b}(week2からa再訪/1人中・
    week2にいないbは新規扱いでnumeratorに含めない)。プール方式: numerator=1(week1→2のa)+1(week2→3のa)=2、
    denominator=4(week1の人数)+1(week2の人数)=5 → revisit_rate=2/5=0.4
    （単純に各ペア率を平均する 0.25/1.0 の平均=0.625 とは異なることを確認＝プール方式であることの検証）。"""
    rows = _rows(
        ("a", date(2026, 6, 1)), ("b", date(2026, 6, 1)), ("c", date(2026, 6, 1)), ("d", date(2026, 6, 1)),
        ("a", date(2026, 6, 8)),
        ("a", date(2026, 6, 15)), ("b", date(2026, 6, 15)),
    )
    out = store._compute_retention(rows)
    assert out["revisit_rate"] == 0.4, f"プール方式の再訪率になっていない: {out['revisit_rate']}"


def test_retention_weekly_sorted_ascending_by_week_start():
    rows = _rows(("a", date(2026, 6, 29)), ("b", date(2026, 6, 15)), ("c", date(2026, 6, 22)))
    out = store._compute_retention(rows)
    assert [w["week_start"] for w in out["weekly"]] == ["2026-06-15", "2026-06-22", "2026-06-29"]
