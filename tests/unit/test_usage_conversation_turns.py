"""S4（2026-09-11-利用統計の拡充.md T4）: 会話セッション統計の純粋関数の単体テスト。

`store._compute_conversation_turn_stats` は DB を介さず、行リスト（{"user_turns",
"codex_session_id"}）から conversation_turns（avg/median/max/p90）と resume_rate を計算する
（`_compute_retention` と同じ理由で、DB 抜きにロジックをここで固定する
＝tests/unit/test_usage_retention.py 参照）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from sherpa import store
from sherpa.store import usage as _usage_store


def _rows(*pairs):
    """(user_turns, codex_session_id) のタプル列 → 呼び出し用の行リストへ変換。"""
    return [{"user_turns": turns, "codex_session_id": sid} for turns, sid in pairs]


def test_conversation_turns_empty_input_returns_all_none():
    turns, resume_rate = store._compute_conversation_turn_stats([])
    assert turns == {"avg": None, "median": None, "max": None, "p90": None}
    assert resume_rate is None


def test_conversation_turns_avg_median_max_p90():
    """3 会話（ターン 1・2・5）: avg=8/3・median=2・max=5。

    p90 は最近傍順位法（`_percentile`）: n=3, ceil(0.9*3)=3 → 昇順3番目(=最大)なので 5.0
    （n が小さいと p90 が max と一致するのは定義上の性質・詳細は `_percentile` docstring 参照）。
    """
    rows = _rows((1, None), (2, None), (5, None))
    turns, _ = store._compute_conversation_turn_stats(rows)
    assert turns["avg"] == pytest.approx(8 / 3)
    assert turns["median"] == 2.0
    assert turns["max"] == 5
    assert turns["p90"] == 5.0


def test_conversation_turns_single_conversation():
    rows = _rows((4, None))
    turns, _ = store._compute_conversation_turn_stats(rows)
    assert turns == {"avg": 4.0, "median": 4.0, "max": 4, "p90": 4.0}


def test_resume_rate_denominator_excludes_single_turn_conversations():
    """user ターン1回の会話は resume_rate の分母に入らない（2ターン目以降が無いため）。"""
    rows = _rows((1, "sess-a"), (1, None))
    _, resume_rate = store._compute_conversation_turn_stats(rows)
    assert resume_rate is None, "分母0（該当会話なし）は None のはず"


def test_resume_rate_numerator_counts_conversations_with_codex_session_id():
    rows = _rows((2, "sess-a"), (5, None), (3, "sess-b"))
    _, resume_rate = store._compute_conversation_turn_stats(rows)
    # 分母3（全会話が2ターン以上）・分子2（sess-a・sess-b）。
    assert resume_rate == pytest.approx(2 / 3)


def test_resume_rate_zero_when_no_conversation_has_session_id():
    rows = _rows((2, None), (3, None))
    _, resume_rate = store._compute_conversation_turn_stats(rows)
    assert resume_rate == 0.0


# ===== STAT-4 U1（2026-09-12-利用統計の拡充2.md §2/§3）: 回答時間分布の純粋関数 =====
# `_compute_response_time_stats` は facade（`sherpa.store.__init__`）へ再エクスポートしていない
# private 関数のため、tests/unit/test_store_surface.py の公開名 golden に触れないよう
# submodule を直接 import する（`sherpa.store.usage_events` を直接 import する既存テストと同型）。

def test_response_time_stats_empty_input_returns_all_none_and_zero_n():
    assert _usage_store._compute_response_time_stats([]) == {
        "avg": None, "median": None, "max": None, "p90": None, "n": 0}


def test_response_time_stats_avg_median_max_p90():
    """durations [1000, 2000, 5000]: avg=8000/3・median=2000・max=5000。

    p90 は最近傍順位法（`_percentile`）: n=3, ceil(0.9*3)=3 → 昇順3番目(=最大)なので 5000.0
    （`test_conversation_turns_avg_median_max_p90` と同じ性質）。
    """
    stats = _usage_store._compute_response_time_stats([1000, 2000, 5000])
    assert stats["avg"] == pytest.approx(8000 / 3)
    assert stats["median"] == 2000.0
    assert stats["max"] == 5000
    assert stats["p90"] == 5000.0
    assert stats["n"] == 3


def test_response_time_stats_single_value():
    assert _usage_store._compute_response_time_stats([4000]) == {
        "avg": 4000.0, "median": 4000.0, "max": 4000, "p90": 4000.0, "n": 1}


def test_response_time_stats_does_not_mutate_input_order():
    """入力順序に依存しない（内部でソートする・呼び出し側の元リストは変更しない）。"""
    values = [5000, 1000, 3000]
    stats = _usage_store._compute_response_time_stats(values)
    assert values == [5000, 1000, 3000]
    assert stats["max"] == 5000
    assert stats["median"] == 3000.0
