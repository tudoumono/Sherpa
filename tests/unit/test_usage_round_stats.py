"""巡別記録（chat-round）の深さ×経路別集計・不明理由コード分布・期間解決（days／from・to）の
純粋関数の単体テスト（DB を介さず `_compute_round_stats`/`_compute_final_reason_codes`/
`_usage_period` を直接検証する・`_compute_retention`/`_compute_conversation_turn_stats` と同じ理由）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from sherpa.store import usage as _usage_store


def _round_row(*, ts=None, provider="openai", input_tokens=10, output_tokens=5,
              elapsed_ms=100, meta=None, turn_message_id=1, depth_profile="deep"):
    return {"ts": ts, "provider": provider, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "elapsed_ms": elapsed_ms, "meta": meta or {},
            "turn_message_id": turn_message_id, "depth_profile": depth_profile}


def test_compute_round_stats_empty_input():
    out = _usage_store._compute_round_stats([])
    assert out == {"by_depth_provider": [], "by_round": [], "round_distribution": [],
                  "unmatched_rounds": 0}


def test_compute_round_stats_aggregates_by_depth_provider():
    rows = [
        _round_row(meta={"round": 1, "citations_delta": 2,
                         "claims": {"confirmed": 1, "inferred": 0, "unknown": 1,
                                   "reason_codes": {"budget": 1}}},
                  input_tokens=10, output_tokens=5, elapsed_ms=100, turn_message_id=1),
        _round_row(meta={"round": 2, "citations_delta": 3,
                         "claims": {"confirmed": 2, "inferred": 0, "unknown": 0,
                                   "reason_codes": {}}},
                  input_tokens=20, output_tokens=10, elapsed_ms=200, turn_message_id=1),
    ]
    out = _usage_store._compute_round_stats(rows)
    assert out["unmatched_rounds"] == 0
    row = out["by_depth_provider"][0]
    assert row["depth_profile"] == "deep" and row["provider"] == "openai"
    assert row["rounds"] == 2
    assert row["citations_delta_total"] == 5
    assert row["citations_delta_avg"] == 2.5
    assert row["elapsed_ms_total"] == 300 and row["elapsed_ms_avg"] == 150
    assert row["input_tokens"] == 30 and row["output_tokens"] == 15
    assert row["claims"] == {"confirmed": 3, "inferred": 0, "unknown": 1}
    assert row["reason_codes"] == {"budget": 1}
    # 同一ターン（turn_message_id=1）が2巡到達 → round_distribution は (deep, openai, 2): 1件。
    assert out["round_distribution"] == [
        {"depth_profile": "deep", "provider": "openai", "rounds_reached": 2, "turns": 1}]


def test_compute_round_stats_unmatched_turn_message_id_counts_separately():
    """対応する assistant 返信が見つからない行（`turn_message_id=None`）は `unmatched_rounds` に
    数えるだけで `round_distribution` には出さない（活動量の by_depth_provider には出る）。"""
    rows = [_round_row(meta={"round": 1}, turn_message_id=None)]
    out = _usage_store._compute_round_stats(rows)
    assert out["unmatched_rounds"] == 1
    assert out["round_distribution"] == []
    assert out["by_depth_provider"][0]["rounds"] == 1


def test_compute_round_stats_missing_depth_and_provider_fold_to_unknown():
    rows = [_round_row(provider=None, depth_profile=None, meta={"round": 1})]
    out = _usage_store._compute_round_stats(rows)
    assert out["by_depth_provider"][0]["depth_profile"] == "unknown"
    assert out["by_depth_provider"][0]["provider"] == "unknown"


def test_compute_round_stats_ignores_malformed_meta_values():
    """`meta` の型不正（非数値の citations_delta・非 dict の claims・bool 混入）は静かに無視し、
    例外にも0への誤変換にもしない（他の usage.py 集計と同じ「非数値/欠落は無視」防御）。"""
    rows = [_round_row(meta={"round": "not-an-int", "citations_delta": "nope",
                             "claims": {"confirmed": True, "reason_codes": {"x": "nope"}}})]
    out = _usage_store._compute_round_stats(rows)
    row = out["by_depth_provider"][0]
    assert row["citations_delta_total"] == 0
    assert row["claims"] == {"confirmed": 0, "inferred": 0, "unknown": 0}   # bool は int 扱いしない
    assert row["reason_codes"] == {}
    # round が非 int のため到達巡数は既定の1巡として数える。
    assert out["round_distribution"] == [
        {"depth_profile": "deep", "provider": "openai", "rounds_reached": 1, "turns": 1}]


def test_compute_round_stats_aggregates_limits_verdicts_stops_and_by_round():
    """巡別記録は深さ×経路の合計だけでなく、巡番号別（`by_round`）・limits の巡内増分
    合算・不足軸（verdict/stop の分類別件数・本文は含まない）も返す（§2.9「2巡は効くか」の
    判断材料）。"""
    rows = [
        _round_row(meta={"round": 1, "citations_delta": 2, "verdict": "insufficient",
                         "stop": "rerun", "limits": {"tool_result_clipped": 2, "auto_continues": 1},
                         "claims": {"confirmed": 1, "inferred": 0, "unknown": 0, "reason_codes": {}}},
                  turn_message_id=1),
        _round_row(meta={"round": 2, "citations_delta": 1, "verdict": "sufficient",
                         "stop": "sufficient", "limits": {"tool_result_clipped": 1,
                                                          "total_budget_hit": True},
                         "claims": {"confirmed": 1, "inferred": 0, "unknown": 0, "reason_codes": {}}},
                  turn_message_id=1),
    ]
    out = _usage_store._compute_round_stats(rows)

    dp_row = out["by_depth_provider"][0]
    assert dp_row["limits"] == {"tool_result_clipped": 3, "auto_continues": 1, "total_budget_hit": 1}
    assert dp_row["verdicts"] == {"insufficient": 1, "sufficient": 1}
    assert dp_row["stops"] == {"rerun": 1, "sufficient": 1}

    by_round = {r["round_no"]: r for r in out["by_round"]}
    assert set(by_round) == {1, 2}
    assert by_round[1]["rounds"] == 1 and by_round[1]["limits"] == {"tool_result_clipped": 2,
                                                                    "auto_continues": 1}
    assert by_round[1]["verdicts"] == {"insufficient": 1}
    assert by_round[2]["limits"] == {"tool_result_clipped": 1, "total_budget_hit": 1}
    assert by_round[2]["verdicts"] == {"sufficient": 1}
    # 本文（missing の自由記述文）はどのバケットにも出ない（`missing_codes`＝閉じた分類キーは
    # 部分文字列として "missing" を含むため、JSON キー `"missing":` の形だけを見る）。
    import json as _json
    import re as _re
    assert not _re.search(r'"missing"\s*:', _json.dumps(out))


def test_compute_round_stats_aggregates_missing_codes_without_free_text():
    """不足軸（`missing_codes`）は本文を持たない閉じた分類として巡番号別に集計される
    （閉集合への丸め自体は記録元＝`providers.base._normalized_verdict` が行う——ここでは
    `reason_codes` と同じ思想で、文字列要素だけを集計し非文字列は静かに無視する）。
    自由文（`meta.missing`）はどのバケットの値にも現れない。"""
    rows = [
        _round_row(meta={"round": 1, "verdict": "insufficient", "stop": "rerun",
                         "missing": "ここが不足しています",
                         "missing_codes": ["unexplored", "insufficient"],
                         "claims": {"confirmed": 0, "inferred": 0, "unknown": 0, "reason_codes": {}}},
                  turn_message_id=1),
        _round_row(meta={"round": 2, "verdict": "insufficient", "stop": "rounds_exhausted",
                         "missing": "こちらも不足",
                         "missing_codes": ["unexplored", 123],
                         "claims": {"confirmed": 0, "inferred": 0, "unknown": 0, "reason_codes": {}}},
                  turn_message_id=1),
    ]
    out = _usage_store._compute_round_stats(rows)

    dp_row = out["by_depth_provider"][0]
    assert dp_row["missing_codes"] == {"unexplored": 2, "insufficient": 1}

    by_round = {r["round_no"]: r for r in out["by_round"]}
    assert by_round[1]["missing_codes"] == {"unexplored": 1, "insufficient": 1}
    assert by_round[2]["missing_codes"] == {"unexplored": 1}
    # 自由文はどこにも出ない（`missing_codes` の分類名だけ）。
    import json as _json
    assert "不足しています" not in _json.dumps(out)
    assert "こちらも不足" not in _json.dumps(out)


def test_compute_round_stats_by_round_folds_missing_round_no_to_none():
    """`meta.round` が取れない（非 int/欠落）行は `round_no=None` の別バケットへ畳み込む
    （型不正で例外にしない・他の欠落フィールドと同じ「unknown」畳み込み思想）。"""
    rows = [_round_row(meta={"round": "not-an-int"})]
    out = _usage_store._compute_round_stats(rows)
    assert len(out["by_round"]) == 1
    assert out["by_round"][0]["round_no"] is None


def test_compute_round_stats_limits_ignores_unknown_keys_and_malformed_values():
    """`limits` の未知キー・非数値/非 bool 値は静かに無視する（他の集計と同じ防御思想）。"""
    rows = [_round_row(meta={"round": 1, "limits": {"not_a_real_limit": 5,
                                                    "tool_result_clipped": "nope",
                                                    "synthesis_truncated": False}})]
    out = _usage_store._compute_round_stats(rows)
    assert out["by_depth_provider"][0]["limits"] == {}


def _claims_row(*, provider="openai", depth_profile="max", claims):
    return {"provider": provider, "depth_profile": depth_profile, "claims": claims}


def test_compute_final_reason_codes_counts_unknown_claims_only():
    rows = [
        _claims_row(claims=[
            {"status": "confirmed", "reason_code": ""},
            {"status": "unknown", "reason_code": "budget"},
            {"status": "unknown", "reason_code": "budget"},
            {"status": "unknown", "reason_code": "conflict"},
        ]),
    ]
    out = _usage_store._compute_final_reason_codes(rows)
    assert {"depth_profile": "max", "provider": "openai", "reason_code": "budget", "claims": 2} in out
    assert {"depth_profile": "max", "provider": "openai", "reason_code": "conflict", "claims": 1} in out
    assert len(out) == 2


def test_compute_final_reason_codes_skips_malformed_claims():
    rows = [_claims_row(claims=[None, "not-a-dict", {"status": "unknown"}, 42]),
            _claims_row(claims=None)]
    out = _usage_store._compute_final_reason_codes(rows)
    # 理由コード欠落（キー自体が無い）は "unknown" へ畳み込む。
    assert out == [{"depth_profile": "max", "provider": "openai", "reason_code": "unknown", "claims": 1}]


def test_compute_final_reason_codes_empty_input():
    assert _usage_store._compute_final_reason_codes([]) == []


# ===== 期間の解決（`days` と `from`/`to`）=====

def test_usage_period_days_keeps_existing_shape_and_adds_from_to():
    """`days` 指定の `start`/`end`/`days`（JST 暦日・`end` は含む終了日）は従来どおり。
    そこへ実際に使った半開区間 `from`/`to` が加わる（画面の日別チャートの描画範囲は不変）。"""
    start_ts, end_ts, period = _usage_store._usage_period(30)
    b_start, b_start_date, b_end_date, b_end = _usage_store._usage_period_bounds(30)
    assert (start_ts, end_ts) == (b_start, b_end)
    assert period["start"] == b_start_date.isoformat()
    assert period["end"] == b_end_date.isoformat()
    assert period["days"] == 30
    assert period["from"] == b_start.isoformat()
    assert period["to"] == b_end.isoformat()


def test_usage_period_from_to_uses_exact_bounds():
    start_ts, end_ts, period = _usage_store._usage_period(
        30, time_from="2026-09-18T13:30:00+09:00", time_to="2026-09-18T18:00:00+09:00")
    assert start_ts.isoformat() == "2026-09-18T13:30:00+09:00"
    assert end_ts.isoformat() == "2026-09-18T18:00:00+09:00"
    # JST 暦日の表示は「区間に含まれる最後の瞬間」の日（`to` は排他的上限）。
    assert period["start"] == "2026-09-18" and period["end"] == "2026-09-18"
    assert period["days"] == 1
    assert period["from"] == "2026-09-18T13:30:00+09:00"
    assert period["to"] == "2026-09-18T18:00:00+09:00"


def test_usage_period_utc_and_jst_spellings_are_equivalent():
    """同じ瞬間を UTC で書いても JST で書いても同じ境界になる（オフセットを解釈している）。"""
    jst = _usage_store._usage_period(30, time_from="2026-09-18T09:00:00+09:00",
                                     time_to="2026-09-19T09:00:00+09:00")
    utc = _usage_store._usage_period(30, time_from="2026-09-18T00:00:00+00:00",
                                     time_to="2026-09-19T00:00:00+00:00")
    assert jst[0] == utc[0] and jst[1] == utc[1]


@pytest.mark.parametrize("time_from,time_to", [
    ("2026-09-18T00:00:00+09:00", None),
    (None, "2026-09-18T00:00:00+09:00"),
])
def test_usage_period_requires_both_from_and_to(time_from, time_to):
    with pytest.raises(_usage_store.UsagePeriodError):
        _usage_store._usage_period(30, time_from=time_from, time_to=time_to)


@pytest.mark.parametrize("value", [
    "2026-09-18T00:00:00",       # オフセットなし
    "2026-09-18",                # 日付のみ（オフセットなし）
    "not-a-datetime",
    "",
])
def test_usage_period_rejects_values_without_offset_or_unparsable(value):
    with pytest.raises(_usage_store.UsagePeriodError):
        _usage_store._usage_period(30, time_from=value, time_to="2026-09-19T00:00:00+09:00")


def test_usage_period_rejects_reversed_and_empty_range():
    with pytest.raises(_usage_store.UsagePeriodError):
        _usage_store._usage_period(30, time_from="2026-09-19T00:00:00+09:00",
                                   time_to="2026-09-18T00:00:00+09:00")
    with pytest.raises(_usage_store.UsagePeriodError):   # from == to（空区間）
        _usage_store._usage_period(30, time_from="2026-09-18T00:00:00+09:00",
                                   time_to="2026-09-18T00:00:00+09:00")


def test_usage_period_rejects_span_over_upper_limit():
    """上限は `days` と同じ365日。ちょうど365日は通り、超えると拒否する。"""
    ok = _usage_store._usage_period(30, time_from="2026-01-01T00:00:00+09:00",
                                    time_to="2027-01-01T00:00:00+09:00")
    assert ok[2]["from"] == "2026-01-01T00:00:00+09:00"
    with pytest.raises(_usage_store.UsagePeriodError):
        _usage_store._usage_period(30, time_from="2026-01-01T00:00:00+09:00",
                                   time_to="2027-01-02T00:00:00+09:00")
