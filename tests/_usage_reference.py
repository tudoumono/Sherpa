"""利用統計の SQL 集計（`sherpa/store/usage.py`）を検算するための Python 参照実装。

本番の集計は SQL が担う。ここの関数は SQL 集計の定義を Python で表したもので、SQL との一致を確かめる
テストと、DB を介さない純粋関数のテストが基準として使う（製品コードからは呼ばない）。
"""
from __future__ import annotations

import math
import statistics

from sherpa.store.usage import (
    _CLAIM_STATUS_KEYS,
    _ROUND_LIMIT_KEYS,
    _merge_final_missing_agg,
    _new_round_bucket,
)


def _compute_retention(week_user_rows) -> dict:
    # 参照実装（本番は SQL＝`usage_stats` の retention_rows）。テストが基準として使う。
    """定着指標（JST 週次アクティブユーザー推移＋再訪率）を `week_user_rows`（`{"uid", "week_start"}` の行・`week_start` は `date`）から計算する純粋関数。
    weekly: 週開始日（JST 月曜）ごとのアクティブユーザー数の昇順リスト。
    revisit_rate: 連続する週ペア（7日差のペアのみ）をプールした再訪率（前週アクティブの延べ人数のうち翌週もアクティブだった割合）。週ペアが無ければ None。
    """
    week_users: dict = {}
    for r in week_user_rows:
        week_users.setdefault(r["week_start"], set()).add(r["uid"])
    sorted_weeks = sorted(week_users.keys())
    weekly = [{"week_start": w.isoformat(), "active_users": len(week_users[w])} for w in sorted_weeks]
    revisit_numerator = 0
    revisit_denominator = 0
    for i in range(len(sorted_weeks) - 1):
        prev_w, next_w = sorted_weeks[i], sorted_weeks[i + 1]
        if (next_w - prev_w).days != 7:
            continue
        prev_users, next_users = week_users[prev_w], week_users[next_w]
        revisit_numerator += len(prev_users & next_users)
        revisit_denominator += len(prev_users)
    revisit_rate = (revisit_numerator / revisit_denominator) if revisit_denominator > 0 else None
    return {"weekly": weekly, "revisit_rate": revisit_rate}


# 分布・巡集計の参照実装（Python）。本番の集計は SQL が担う。`_percentile`・`_compute_conversation_turn_stats`・`_compute_response_time_stats`・`_compute_round_stats`・`_compute_final_*` は SQL 集計の定義を表し、SQL との一致を確かめるテストが基準として使う（本番コードからは呼ばない）。
def _percentile(sorted_values: list[int], pct: float) -> float:
    """最近傍順位法（線形補間なし）で百分位を計算する。昇順配列の `ceil(pct * n)` 番目（1始まり）の値を返す。空配列で呼ばないこと。"""
    n = len(sorted_values)
    idx = max(0, min(n - 1, math.ceil(pct * n) - 1))
    return float(sorted_values[idx])


def _compute_conversation_turn_stats(conversation_rows) -> tuple[dict, float | None]:
    """会話あたりの user ターン数分布と resume_rate を計算する。
    `conversation_rows` は期間内に user ターンが1件以上ある会話（origin='own'・deleted_at IS NULL）の `{"user_turns", "codex_session_id"}` の行（1会話1行）。`user_turns` は期間内の user メッセージ数。
    resume_rate: user ターン数2以上の会話のうち `codex_session_id` が設定されている割合（分母 0 なら None）。セッション ID の保持率であり、再開の成功率ではない。conversation_turns に session_eligible（分母）と session_recorded（分子）を含める。
    """
    counts = sorted((r["user_turns"] or 0) for r in conversation_rows)
    if counts:
        conversation_turns = {
            "avg": sum(counts) / len(counts),
            "median": float(statistics.median(counts)),
            "max": counts[-1],
            "p90": _percentile(counts, 0.9),
        }
    else:
        conversation_turns = {"avg": None, "median": None, "max": None, "p90": None}
    eligible = [r for r in conversation_rows if (r["user_turns"] or 0) >= 2]
    denom = len(eligible)
    resumed = sum(1 for r in eligible if r["codex_session_id"] is not None)
    resume_rate = (resumed / denom) if denom > 0 else None
    conversation_turns.update(session_eligible=denom, session_recorded=resumed)
    return conversation_turns, resume_rate


def _compute_response_time_stats(durations: list[int]) -> dict:
    """回答時間（ミリ秒）の分布統計（avg/median/max/p90・件数）を計算する。`durations` が空なら avg/median/max/p90=None・n=0。`_percentile`（最近傍順位法）を使う。`provider` キーは呼び出し側が追加する。"""
    n = len(durations)
    if n == 0:
        return {"avg": None, "median": None, "max": None, "p90": None, "n": 0}
    sorted_vals = sorted(durations)
    return {
        "avg": sum(sorted_vals) / n,
        "median": float(statistics.median(sorted_vals)),
        "max": sorted_vals[-1],
        "p90": _percentile(sorted_vals, 0.9),
        "n": n,
    }


def _accumulate_round(agg: dict, r, meta: dict) -> None:
    """`chat-round` 行1件を集計バケットへ足す（深さ×経路／深さ×経路×巡番号の両方で共有）。"""
    agg["rounds"] += 1
    cd = meta.get("citations_delta")
    if isinstance(cd, (int, float)) and not isinstance(cd, bool):
        agg["citations_delta_total"] += cd
    if r["elapsed_ms"] is not None:
        agg["elapsed_ms_total"] += int(r["elapsed_ms"])
        agg["elapsed_n"] += 1
    inp, outp = r["input_tokens"], r["output_tokens"]
    if inp is not None or outp is not None:
        agg["input_tokens"] += int(inp or 0)
        agg["output_tokens"] += int(outp or 0)
        agg["tokens_n"] += 1
    claims = meta.get("claims") or {}
    for status in _CLAIM_STATUS_KEYS:
        v = claims.get(status)
        if isinstance(v, int) and not isinstance(v, bool):
            agg["claims"][status] += v
    for code, n in (claims.get("reason_codes") or {}).items():
        if isinstance(n, int) and not isinstance(n, bool):
            agg["reason_codes"][code] = agg["reason_codes"].get(code, 0) + n
    # 巡内の limits 増分を合算する。
    limits = meta.get("limits")
    if isinstance(limits, dict):
        for k, v in limits.items():
            if k not in _ROUND_LIMIT_KEYS:
                continue
            if isinstance(v, bool):
                if v:
                    agg["limits"][k] = agg["limits"].get(k, 0) + 1
            elif isinstance(v, (int, float)):
                agg["limits"][k] = agg["limits"].get(k, 0) + v
    # 不足軸（本文を含まない分類）: evaluator の判定（verdict）と巡を止めた理由（stop）。
    verdict = meta.get("verdict")
    if isinstance(verdict, str) and verdict:
        agg["verdicts"][verdict] = agg["verdicts"].get(verdict, 0) + 1
    stop = meta.get("stop")
    if isinstance(stop, str) and stop:
        agg["stops"][stop] = agg["stops"].get(stop, 0) + 1
    # 不足軸の閉じた分類（本文を含まない）。自由文の `missing` は集計しない。
    for code in meta.get("missing_codes") or []:
        if isinstance(code, str) and code:
            agg["missing_codes"][code] = agg["missing_codes"].get(code, 0) + 1


def _compute_round_stats(round_rows) -> dict:
    """巡別記録（`chat-round`）の表示専用集計。
    `round_rows` は1行=1巡のイベント（`depth_profile`・`provider`・`input_tokens`/`output_tokens`・`elapsed_ms`・`meta`・`turn_message_id`）。
    返り値:
    - `by_depth_provider`: 深さ×経路別の活動量（巡数・引用増分・所要時間・トークン・主張の区分内訳・不明理由コード・limits 増分・verdict/stop の分類別件数・不足軸の分類別件数）。
    - `by_round`: 深さ×経路×巡番号（`meta.round`）別の同じ活動量（`round_no` が取れない行は `None`）。
    - `round_distribution`: 深さ×経路×「そのターンで到達した巡数」ごとのターン件数（`turn_message_id` が取れない行は `unmatched_rounds` に計上するだけで数えない）。
    - `unmatched_rounds`: 対応付け失敗件数。
    `depth_profile`/`provider` の欠落は `"unknown"` へ畳み込む。
    """
    by_dp: dict[tuple, dict] = {}
    by_round: dict[tuple, dict] = {}
    turns: dict[int, dict] = {}
    unmatched_rounds = 0
    for r in round_rows:
        meta = r["meta"] or {}
        # 深さは利用者が選んだ語彙そのまま（版の印は付けない）。
        depth = r["depth_profile"] or "unknown"
        provider = r["provider"] or "unknown"

        agg = by_dp.setdefault((depth, provider), _new_round_bucket(depth, provider))
        _accumulate_round(agg, r, meta)

        rd = meta.get("round")
        round_no = rd if isinstance(rd, int) and not isinstance(rd, bool) else None
        ragg = by_round.setdefault((depth, provider, round_no), _new_round_bucket(depth, provider))
        ragg["round_no"] = round_no
        _accumulate_round(ragg, r, meta)

        turn_id = r["turn_message_id"]
        if turn_id is None:
            unmatched_rounds += 1
            continue
        t = turns.setdefault(turn_id, {"depth_profile": depth, "provider": provider, "max_round": 0})
        if round_no is not None and round_no > t["max_round"]:
            t["max_round"] = round_no

    def _finalize(agg: dict) -> dict:
        return {**agg,
                "elapsed_ms_avg": (agg["elapsed_ms_total"] / agg["elapsed_n"]) if agg["elapsed_n"] else None,
                "citations_delta_avg": (agg["citations_delta_total"] / agg["rounds"]) if agg["rounds"] else None}

    by_depth_provider = [
        _finalize(agg) for agg in sorted(by_dp.values(), key=lambda a: (a["depth_profile"], a["provider"]))
    ]
    by_round_list = [
        _finalize(agg) for agg in sorted(
            by_round.values(),
            key=lambda a: (a["depth_profile"], a["provider"], a["round_no"] is None, a["round_no"] or 0))
    ]

    dist_counter: dict[tuple, dict[int, int]] = {}
    for t in turns.values():
        key = (t["depth_profile"], t["provider"])
        n = t["max_round"] or 1  # 到達巡数が観測できなければ最低1巡とみなす。
        dist_counter.setdefault(key, {})
        dist_counter[key][n] = dist_counter[key].get(n, 0) + 1
    round_distribution = [
        {"depth_profile": dp, "provider": pv, "rounds_reached": n, "turns": cnt}
        for (dp, pv), dist in sorted(dist_counter.items())
        for n, cnt in sorted(dist.items())
    ]

    return {"by_depth_provider": by_depth_provider, "by_round": by_round_list,
            "round_distribution": round_distribution, "unmatched_rounds": unmatched_rounds}


def _compute_final_reason_codes(final_claims_rows) -> list[dict]:
    """最終回答の主張のうち不明（`status='unknown'`）の理由コードを、深さ×経路×理由コードで合算する（主張単位）。行の `unknown_reasons` は `turn_metrics.claims_unknown_reasons`（理由コード→件数）。"""
    agg: dict[tuple, dict[str, int]] = {}
    for r in final_claims_rows:
        depth = r["depth_profile"] or "unknown"
        provider = r["provider"] or "unknown"
        bucket = agg.setdefault((depth, provider), {})
        for code, n in (r["unknown_reasons"] or {}).items():
            bucket[code] = bucket.get(code, 0) + int(n)
    return [
        {"depth_profile": dp, "provider": pv, "reason_code": code, "claims": n}
        for (dp, pv), bucket in sorted(agg.items())
        for code, n in sorted(bucket.items())
    ]


def _compute_final_missing_codes(final_claims_rows) -> dict[tuple, dict[str, int]]:
    """最終回答の `evidence_gate.missing_codes`（Codex 経路の最終ゲート）を深さ×経路で合算する。行の `gate_missing_codes` は `turn_metrics.gate_missing_codes`（コード→件数）。NULL（API 経路・旧データ）はスキップする。"""
    agg: dict[tuple, dict[str, int]] = {}
    for r in final_claims_rows:
        codes = r["gate_missing_codes"]
        if not isinstance(codes, dict):
            continue
        depth = r["depth_profile"] or "unknown"
        provider = r["provider"] or "unknown"
        bucket = agg.setdefault((depth, provider), {})
        for code, n in codes.items():
            bucket[code] = bucket.get(code, 0) + int(n)
    return agg


def _merge_final_missing_codes(rounds_stats: dict, final_claims_rows) -> None:
    """最終回答由来の不足軸（`_compute_final_missing_codes`）を、既存の巡別集計（`rounds_stats["by_depth_provider"]`）へ深さ×経路で合流させる。対応するバケットが無い（深さ, "codex"）組は新規バケットとして追加する（巡別の指標は0のまま）。"""
    _merge_final_missing_agg(rounds_stats, _compute_final_missing_codes(final_claims_rows))
