"""利用統計（admin 専用）。

不変条件: メッセージ本文・会話タイトルは一切 SELECT しない（件数・日時・種別のみ集計）。
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .. import stop_kind
from .db import _connect, _ensure

_USAGE_AUDIT_ACTIONS = ("auth.login", "document.downloaded", "workspace.file_uploaded", "share.created")
_JST = timezone(timedelta(hours=9))

# chat.turn 監査の detail.provider は書込側
# （`chat_service._audit_chat_turn`・`sherpa.agents.AGENT_PROVIDERS`）で allowlist 正規化済みのはずだが、
# 過去の保存済み不正値（env 誤設定等）が残っている可能性があるため、集計（読み出し）側でも
# 同じ allowlist で畳み込む二重防御。store.py は他の sherpa.* を import しない設計
# （循環 import 回避・store は最下層）のため、`sherpa.agents.AGENT_PROVIDERS` の値をここに複製する。
# 値を変更したらそちらも合わせて確認すること。
_USAGE_KNOWN_PROVIDERS = ("heuristic", "codex", "openai", "gemini", "bedrock", "ollama")


def _usage_period_bounds(days: int):
    """JST の「(今日 − (days−1)) の日初」〜「明日の日初（排他的上限）」を期間として返す
    （上限も含めて全クエリで統一）。

    daily・users・totals・audit 由来集計の**全てが同じ境界**を使うことで、表（users）とグラフ（daily）の
    合計が常に一致するようにする（`now() - make_interval(days)` のようなローリング境界だと、
    フロントの「JST 暦日で days 個分」描画と食い違い、最古日の部分バケットが暗黙に drop される）。
    アプリサーバの UTC 時刻（`datetime.now(timezone.utc)`）から計算するため DB セッション timezone に
    依存しない。

    下限のみで上限が無いと、クロックスキューやテスト由来の未来時刻行（`created_at` が「今日」より
    先）が「期間内」に混入してしまう。`end_exclusive_ts`（「明日」の
    JST 00:00:00・排他的上限）を全クエリの `WHERE ... < %s` に使うことで、期間は常に
    `[start_ts, end_exclusive_ts)` という半開区間に固定する。

    returns (start_ts, start_date, end_date, end_exclusive_ts):
      start_ts          = 期間下限（timestamptz・JST 00:00:00・inclusive）
      start_date        = 期間下限の JST 暦日（date）
      end_date          = 「今日」の JST 暦日（date）
      end_exclusive_ts  = 期間上限（timestamptz・「明日」の JST 00:00:00・exclusive）
    """
    today_jst = datetime.now(timezone.utc).astimezone(_JST).date()
    start_date = today_jst - timedelta(days=days - 1)
    start_ts = datetime(start_date.year, start_date.month, start_date.day, tzinfo=_JST)
    tomorrow_jst = today_jst + timedelta(days=1)
    end_exclusive_ts = datetime(tomorrow_jst.year, tomorrow_jst.month, tomorrow_jst.day, tzinfo=_JST)
    return start_ts, start_date, today_jst, end_exclusive_ts


class UsagePeriodError(ValueError):
    """`from`/`to` による期間指定が規則に反するときに送出する（呼び出し側が 422 へ写す）。"""


# `from`/`to` で指定できる期間の上限（日数指定の上限＝`_clamp_days`・API の `days` と同じ365日）。
_USAGE_PERIOD_MAX_DAYS = 365


def _parse_period_bound(value, field: str) -> datetime:
    """ISO 8601 の日時文字列を解釈する。**UTC オフセット必須**（`+09:00`・`Z` 等）——オフセットの
    無い値は JST か UTC かを推測するしかなく、推測を誤ると期間が9時間ずれた集計を正しい顔で返す。
    """
    if not isinstance(value, str) or not value.strip():
        raise UsagePeriodError(f"{field} は ISO 8601 の日時（オフセット付き）で指定してください")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise UsagePeriodError(f"{field} は ISO 8601 の日時（オフセット付き）で指定してください") from None
    if dt.tzinfo is None:
        raise UsagePeriodError(f"{field} にはタイムゾーンオフセット（例: +09:00）が必要です")
    return dt


def _usage_period(days=None, *, time_from=None, time_to=None):
    """集計期間を決めて `(start_ts, end_exclusive_ts, period)` を返す。

    既定は `days`（JST 暦日・`_usage_period_bounds` と完全に同じ境界）。`time_from`/`time_to`
    （ISO 8601・オフセット必須）を渡したときは JST 暦日へ丸めず `[from, to)` をそのまま境界に
    使う（AP を差し替えて比較するときに切替時刻で期間を分けて読むため）。規則:

      - `from`/`to` は両方必須（片方だけは誤読の元＝エラー）。
      - 区間は半開 `[from, to)`（`to` ちょうどの記録は含まない）。`from` < `to`。
      - 期間の長さは最大 `_USAGE_PERIOD_MAX_DAYS` 日（`days` の上限と揃える）。

    `days` との排他は呼び出し側（API/ツール）が「利用者が明示的に指定したか」で判定する
    （既定値の補完と区別できるのは呼び出し側だけのため）。

    `period` は JST 暦日の `start`/`end`/`days`（既存の形・`end` は**含む**終了日＝画面の日別
    チャートがこの範囲でゼロ埋め描画する）を変えずに保ったうえで、**実際に使った半開区間**
    （`from`/`to`・ISO 8601・オフセット付き）を必ず加える。
    """
    if time_from is None and time_to is None:
        start_ts, start_date, end_date, end_exclusive_ts = _usage_period_bounds(days)
        return start_ts, end_exclusive_ts, {
            "start": start_date.isoformat(), "end": end_date.isoformat(), "days": days,
            "from": start_ts.isoformat(), "to": end_exclusive_ts.isoformat()}
    if time_from is None or time_to is None:
        raise UsagePeriodError("from と to は両方指定してください")
    start_ts = _parse_period_bound(time_from, "from")
    end_exclusive_ts = _parse_period_bound(time_to, "to")
    if start_ts >= end_exclusive_ts:
        raise UsagePeriodError("from は to より前の日時で指定してください")
    if end_exclusive_ts - start_ts > timedelta(days=_USAGE_PERIOD_MAX_DAYS):
        raise UsagePeriodError(f"期間は最大 {_USAGE_PERIOD_MAX_DAYS} 日です")
    start_date = start_ts.astimezone(_JST).date()
    # `to` は排他的上限のため、暦日表示の終端は「区間に含まれる最後の瞬間」の JST 暦日。
    end_date = (end_exclusive_ts - timedelta(microseconds=1)).astimezone(_JST).date()
    return start_ts, end_exclusive_ts, {
        "start": start_date.isoformat(), "end": end_date.isoformat(),
        "days": (end_date - start_date).days + 1,
        "from": start_ts.isoformat(), "to": end_exclusive_ts.isoformat()}


# lens 内訳の対応付け: 「conversation 内で各 user メッセージの直後に来る
# 最初の assistant メッセージ」だけをその user ターンの返答として数える。assistant 単独行（対応する
# user メッセージが無い・または既に他の user メッセージの返答として数えられた2件目以降の assistant 行）は
# lens 内訳に混入させない。turn_no は「その行より前（自分を含む）に何件の user メッセージがあったか」の
# 累積カウントで、user 行と直後の assistant 行が同じ turn_no を持つことを利用してペアリングする。
# `answer`（JSONB）もペアリングして持ち回る＝ゼロヒット率（lens != 'chat' の
# ターンで assistant answer.sources が空）を同じターン対応付けロジックで計算するため。
# PERF-1（台帳#17）: `numbered` の基点スキャンを「期間内にメッセージを1件でも持つ会話」に絞る。
# `touched`（DISTINCT conversation_id・期間の WHERE 条件のみ）への明示 JOIN として書く（呼び出し側は
# この CTE 用に `(start_ts, end_exclusive_ts)` を渡す・外側 WHERE 用の分と合わせて計4個）。
# `m.conversation_id IN (サブクエリ)` という同値な書き方もあるが、それだと Postgres が
# messages 全件を走査してから IN 判定するプランしか選ばず、索引は「期間の候補行を出す」側にしか
# 効かず「touched 会話に限定して走査する」側の効果が出ない（EXPLAIN で確認済み）。`touched` への
# 明示 JOIN にすると、`msg_conv`（conversation_id 索引）を使って touched 会話に限定した走査を
# Postgres が選べるようになる（実際にどのプラン形状・結合方式を選ぶかはデータ分布次第で
# Postgres 自身が判断する・特定の結合方式を前提にしない）。`touched` は DISTINCT のため
# `messages m` の行を複製しない＝結果セットは IN 版と同一。
#
# **契約の範囲**: messages 全体に対する線形の物理読取（テーブル全体を辿る Scan）自体は
# 残り得る（Postgres が選ぶプラン次第）。本スライスが保証するのは、`numbered` の
# turn_no 累積カウント計算（WindowAgg）とその後段6集計（下記 usage_stats() 参照）へ
# **投入される行数**を、全 messages N 行から「期間内に触れた会話」T 行へ削減すること
# （`touched` の JOIN 条件がその境界。会話の活動が期間に集中していれば T は N に近づき得る＝
# 常に T ≪ N が保証されるわけではない）。
#
# **会話単位**（行単位ではない）で絞る不変条件: id の採番順と created_at の単調性はスキーマ上
# 保証されない。行単位で `m.created_at >= start_ts` を足すと、ID順で「期間内user（返信なし）→
# 期間外user→期間内assistant」のように created_at が id 順と逆転する並びで、期間外 user 行だけが
# 取り除かれて `turn_no` の累積カウントが後続行でずれ、本来ペアの無かった期間内 user 行に別ターンの
# assistant 応答が誤結合し得る。会話単位フィルタは対象会話に属する行を**1件も間引かない**ため
# `turn_no` の計算は全件走査と完全に同じになり、ペアリングは常に一致する。除外されるのは
# 「期間内メッセージが1件も無い会話」のみで、そのような会話はどの user 行も `turn_created_at` が
# 期間外になるため最終 WHERE でどのみち出力から落ちる対象＝除外しても出力は変わらない
# （id/created_at の単調性に一切依存しない）。
_USAGE_TURN_CTE = (
    "WITH touched AS ("
    "  SELECT DISTINCT conversation_id FROM messages WHERE created_at >= %s AND created_at < %s"
    "), numbered AS ("
    "  SELECT m.id, m.conversation_id, m.role, m.lens, m.personal, m.answer, m.created_at, "
    "    c.user_id, c.version, "
    "    SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) "
    "      OVER (PARTITION BY m.conversation_id ORDER BY m.id "
    "            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS turn_no "
    "  FROM touched t JOIN messages m ON m.conversation_id = t.conversation_id "
    "  JOIN conversations c ON c.id = m.conversation_id "
    "  WHERE c.deleted_at IS NULL AND c.origin='own' "
    "), assistant_replies AS ("
    "  SELECT DISTINCT ON (conversation_id, turn_no) conversation_id, turn_no, lens, answer "
    "  FROM numbered WHERE role='assistant' AND turn_no > 0 "
    "  ORDER BY conversation_id, turn_no, id "
    "), turns AS ("
    "  SELECT n.user_id, n.version, n.conversation_id, n.created_at AS turn_created_at, "
    "    n.personal AS user_personal, ar.lens, ar.answer "
    "  FROM numbered n LEFT JOIN assistant_replies ar "
    "    ON ar.conversation_id = n.conversation_id AND ar.turn_no = n.turn_no "
    "  WHERE n.role='user' "
    ")"
)


# messages.answer->'usage' からトークン使用量を集計する SQL 断片。
# answer->'usage' は `{provider, model, input_tokens, cached_input_tokens, output_tokens,
# reasoning_output_tokens}`（agents._usage_meta）。想定外データ（非数値・欠落）は 0 に畳む
# （`~ '^[0-9]+$'` を先に確認してから ::bigint・zero_hit の非配列ガードと同じ防御思想）。
# フィールド名はコード内リテラル（ユーザー入力ではない）＝f-string 埋め込みは安全。
def _usage_tok(field: str) -> str:
    return (f"CASE WHEN (answer->'usage'->>'{field}') ~ '^[0-9]+$' "
            f"THEN (answer->'usage'->>'{field}')::bigint ELSE 0 END")


# 会話の `kinds`（用途別内訳）の並び順: input+output（報告不能=None は 0）の降順・同値は kind 名。
# バイト予算内へ間引く側（agentic_search）は先頭から残すため、この順にしておかないと
# 間引きで最も重い用途が落ちうる（`usage_by_user`/`usage_conversations` の行ソートと同じ思想）。
def _kind_sort_key(k: dict) -> tuple:
    return (-((k.get("input") or 0) + (k.get("output") or 0)), k.get("kind") or "")


# 利用者停止（`chat.turn` 監査の `detail.stopped=true`）を数える SQL。`detail.message_id_user`
# （停止時の user 発言の message id・`chat_service._audit_chat_turn` が常に書く）で `messages` に
# 結合し、その `created_at` を `turns`/`stop_kinds` と同じ `turn_created_at` 境界として使う——
# 期間境界の直前に始まり直後に停止したターンが、監査時刻基準では「期間内の停止」に誤って
# 数えられる食い違いを無くす。過去行（列追加前＝ detail に message_id_user が無い、または
# 数値でない）は結合が成立せず `a.created_at`（従来どおり監査時刻）へフォールバックする
# （遡及しない）。呼び出し側は境界2引数の後に `AND c.user_id = %s` 等を追加できる。
def _stopped_turns_sql() -> str:
    return (
        "SELECT COUNT(*) AS n FROM audit_log a "
        "JOIN conversations c ON a.resource_id = 'conv:' || c.id::text "
        "LEFT JOIN messages um ON um.conversation_id = c.id "
        "  AND um.id = CASE WHEN a.detail->>'message_id_user' ~ '^[0-9]+$' "
        "    THEN (a.detail->>'message_id_user')::bigint END "
        "WHERE a.action='chat.turn' AND a.detail->>'stopped' = 'true' "
        "  AND c.deleted_at IS NULL AND c.origin='own' "
        "  AND COALESCE(um.created_at, a.created_at) >= %s "
        "  AND COALESCE(um.created_at, a.created_at) < %s"
    )


# limits（`answer->'limits'`・agentic_search.InvestigationState.limits・利用統計「打ち切りの
# 内訳」専用の計測カウンタ）の SQL 抽出。旧行（キー自体が無い）は NULL のまま拾い FILTER で
# 除外される＝0件として集計に混じる（`docs/notes`「限定/計測」契約・制限そのものは変えない）。
# `_usage_tok` と同じ「非数値/欠落は無視」防御（bool は 'true'/'false' 文字列のみ真偽扱い）。
_USAGE_LIMIT_INT_FIELDS = ("tool_result_clipped", "context_compactions", "search_truncated",
                           "auto_continues", "duplicate_tool_call")
# 縮退（バックエンド不調）の計数（`investigation_state._BACKEND_LIMIT_FIELD`／
# `GRAPH_REINGEST_LIMIT_FIELD` と同じ語彙）。意味論は「このターンで初めて検出されたか」＝
# 初回検出の計数で、障害が起きた巡の数ではない（`providers/base.py::_limits_delta` が
# 偽→真になった巡だけ載せるため、同じ障害が続いても2巡目以降は計上されない）。
# `tool_calls_exhausted`（クイックを本当に速くする・変更D③）はバックエンド不調ではなく
# MCP ツール呼び出し回数の上限到達（Codex 経路のみ・`mcp_server.py`）。
_USAGE_LIMIT_BOOL_FIELDS = ("total_budget_hit", "synthesis_truncated", "depth_escalated",
                            "backend_unavailable_fulltext", "backend_unavailable_graph",
                            "graph_reingest_required", "tool_calls_exhausted")


def _usage_limit_int(field: str) -> str:
    return (f"CASE WHEN (answer->'limits'->>'{field}') ~ '^[0-9]+$' "
            f"THEN (answer->'limits'->>'{field}')::bigint ELSE 0 END")


def _usage_limit_bool(field: str) -> str:
    return f"(answer->'limits'->>'{field}') = 'true'"


def _usage_limits_select_cols() -> str:
    cols = []
    for f in _USAGE_LIMIT_INT_FIELDS:
        expr = _usage_limit_int(f)
        cols.append(f"COUNT(*) FILTER (WHERE {expr} > 0) AS {f}_turns")
        cols.append(f"COALESCE(SUM({expr}), 0) AS {f}_total")
    for f in _USAGE_LIMIT_BOOL_FIELDS:
        cols.append(f"COUNT(*) FILTER (WHERE {_usage_limit_bool(f)}) AS {f}_turns")
    return ", ".join(cols)


def _usage_token_sum_cols() -> str:
    return ("COUNT(*) AS turns, "
            f"SUM({_usage_tok('input_tokens')}) AS input, "
            f"SUM({_usage_tok('cached_input_tokens')}) AS cached_input, "
            f"SUM({_usage_tok('output_tokens')}) AS output, "
            f"SUM({_usage_tok('reasoning_output_tokens')}) AS reasoning_output")


# usage を持つ user ターン（対応 assistant 返答に answer.usage がある）だけを集計対象にする WHERE 追加句。
_USAGE_TOKEN_WHERE = " AND jsonb_typeof(answer->'usage')='object' "


def _compute_retention(week_user_rows) -> dict:
    """定着指標（JST 週次アクティブユーザー推移＋再訪率）を `week_user_rows`
    （`{"uid", "week_start"}` の行・`week_start` は `date`）から計算する。

    純粋関数として切り出す＝DB を介さず単体テストできる（`usage_stats` 本体は共有 dev DB の
    既存データに引きずられて再訪率の期待値を精密に検証しづらいため、ロジックはここで確定させる）。

    weekly: 週開始日（JST 月曜）ごとのアクティブユーザー数の昇順リスト。
    revisit_rate: **連続する**週ペア（7日差のペアのみ・間が空いた週は「前週」として扱わない）を
    プールした再訪率（前週アクティブの延べ人数のうち翌週もアクティブだった延べ人数の割合）。
    週ペアが1組も無ければ None（計算不能）。
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
        if (next_w - prev_w).days != 7:          # 間が空いた週は「前週」として扱わない（連続週のみ）
            continue
        prev_users, next_users = week_users[prev_w], week_users[next_w]
        revisit_numerator += len(prev_users & next_users)
        revisit_denominator += len(prev_users)
    revisit_rate = (revisit_numerator / revisit_denominator) if revisit_denominator > 0 else None
    return {"weekly": weekly, "revisit_rate": revisit_rate}


def _percentile(sorted_values: list[int], pct: float) -> float:
    """最近傍順位法（線形補間なし）で百分位を計算する。

    昇順配列の `ceil(pct * n)` 番目（1始まり）の値を返す＝浮動小数の補間誤差を持ち込まない
    決定的な定義。呼び出し側は空配列で呼ばないこと（`n=0` は割当不能＝呼び出し側で None 分岐）。
    """
    n = len(sorted_values)
    idx = max(0, min(n - 1, math.ceil(pct * n) - 1))
    return float(sorted_values[idx])


def _compute_conversation_turn_stats(conversation_rows) -> tuple[dict, float | None]:
    """会話あたりの user ターン数分布と resume_rate を計算する。

    `conversation_rows` は「期間内に user ターンが1件以上ある会話（origin='own'・deleted_at IS NULL）」
    に絞った `{"user_turns", "codex_session_id"}` の行（1会話1行）。`user_turns` は**期間内**の
    user メッセージ数（他の period 系集計と同じ境界）。

    resume_rate: user ターン数2以上（＝2ターン目以降が有り得る）の会話のうち、
    `codex_session_id` が設定されている割合。分母（該当会話数）が0なら None（推定しない）。
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
    return conversation_turns, resume_rate


def _compute_response_time_stats(durations: list[int]) -> dict:
    """回答時間（ミリ秒）の分布統計（avg/median/max/p90・件数）を計算する。

    `durations` は空でもよい（0件なら avg/median/max/p90=None・n=0・`_compute_conversation_turn_stats`
    の空入力時と同じ扱い）。`_percentile`（最近傍順位法）を使い、分布統計の計算方式を
    他の集計（`_compute_conversation_turn_stats`）と揃える。呼び出し側で `provider` キーを
    追加してから返す（本関数は provider を持たない・全体/経路別のどちらにも使う共通ロジック）。
    """
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


# 主張の区分キー（`investigation_state.Claim.status`）。`_compute_round_stats`/
# `_compute_final_reason_codes` の両方でこの3値だけを合算する（他の値は来ない契約・
# `investigation_state._CLAIM_STATUSES` と同じ語彙だが usage.py は investigation_state を
# import しない＝leaf モジュール原則を保つため値を直接持つ）。
_CLAIM_STATUS_KEYS = ("confirmed", "inferred", "unknown")


# 巡内 limits 増分（査読の巡が meta に書く増分）のキー集合。int 系は合算、bool 系は
# 「この巡で当たった」件数（True の巡数）として合算する（_compute_round_stats/_accumulate_round
# が共有）。
_ROUND_LIMIT_KEYS = _USAGE_LIMIT_INT_FIELDS + _USAGE_LIMIT_BOOL_FIELDS


def _new_round_bucket(depth: str, provider: str) -> dict:
    return {
        "depth_profile": depth, "provider": provider, "rounds": 0,
        "citations_delta_total": 0, "elapsed_ms_total": 0, "elapsed_n": 0,
        "input_tokens": 0, "output_tokens": 0, "tokens_n": 0,
        "claims": {k: 0 for k in _CLAIM_STATUS_KEYS}, "reason_codes": {},
        "limits": {}, "verdicts": {}, "stops": {}, "missing_codes": {},
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
    # 巡内の limits 増分（記録元が既に「増分/新たに真になった」だけを書いている）を合算。
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
    # どちらも固定語彙のラベル文字列で自由記述本文ではない。
    verdict = meta.get("verdict")
    if isinstance(verdict, str) and verdict:
        agg["verdicts"][verdict] = agg["verdicts"].get(verdict, 0) + 1
    stop = meta.get("stop")
    if isinstance(stop, str) and stop:
        agg["stops"][stop] = agg["stops"].get(stop, 0) + 1
    # 不足軸の閉じた分類（本文を含まない）。自由文の `missing` はここでは集計しない
    # （表示専用の生値のまま）。
    for code in meta.get("missing_codes") or []:
        if isinstance(code, str) and code:
            agg["missing_codes"][code] = agg["missing_codes"].get(code, 0) + 1


def _compute_round_stats(round_rows) -> dict:
    """巡別記録（`chat-round`）の表示専用集計。

    `round_rows` は1行=1巡のイベント（`depth_profile`・`provider`・`input_tokens`/
    `output_tokens`・`elapsed_ms`・`meta`・`turn_message_id`）。

    返り値:
      - `by_depth_provider`: 深さ×経路別の活動量（巡数・引用増分合計/平均・所要時間合計/平均・
        トークン合計・主張の区分内訳・不明理由コードの巡別合算・limits 増分の合算・
        verdict/stop の分類別件数・不足軸の分類別件数（`missing_codes`・本文なし））。
      - `by_round`: 深さ×経路×**巡番号**（`meta.round`）別の同じ活動量。「2巡目は1巡目より
        効くか」を判断するための軸（`round_no` が取れない行は `None` へ畳み込む）。
      - `round_distribution`: 深さ×経路×「そのターンで到達した巡数」ごとのターン件数
        （`meta.round` の最大値を1ターンの到達巡数とみなす・`turn_message_id` が取れない行
        （対応する assistant 返信が見つからない＝母集団に含まれない古い/異常データ）は
        `unmatched_rounds` へ計上するだけでこの分布には数えない）。
      - `unmatched_rounds`: 上記の対応付け失敗件数（0 が通常。多ければ join 前提の見直しが要る）。

    `depth_profile`/`provider` の欠落は `"unknown"` へ畳み込む（他の集計の allowlist 畳み込みと
    同じ思想・不正値で行が消えたり例外になったりしない）。
    """
    by_dp: dict[tuple, dict] = {}
    by_round: dict[tuple, dict] = {}
    turns: dict[int, dict] = {}
    unmatched_rounds = 0
    for r in round_rows:
        meta = r["meta"] or {}
        # 深さは利用者が選んだ語彙そのもの（版の印は付けない）——`"standard"` の意味が 0 巡から
        # 2 巡へ変わった前後は同じキーに畳まれる。版をまたぐ比較は品質採点の `condition`
        # （`QUALITY_RUN_CONDITIONS`）側で分ける。
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
        n = t["max_round"] or 1   # 到達巡数が観測できなければ最低1巡とみなす（回が1件はある以上）
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
    """最終回答の主張（`answer->'data'->'claims'`）のうち不明（`status='unknown'`）の
    理由コードを、深さ×経路×理由コードで合算する（主張単位）。

    行の `claims` が非配列/欠落（旧データ）は静かにスキップする——
    `jsonb_typeof(...)='array'` で SQL 側が既に絞っているが、要素自体が非 dict の防御的想定外
    データも同様にスキップする（他の集計と同じ「非数値/欠落は無視」防御）。
    """
    agg: dict[tuple, dict[str, int]] = {}
    for r in final_claims_rows:
        depth = r["depth_profile"] or "unknown"
        provider = r["provider"] or "unknown"
        bucket = agg.setdefault((depth, provider), {})
        for claim in (r["claims"] or []):
            if not isinstance(claim, dict) or claim.get("status") != "unknown":
                continue
            code = claim.get("reason_code") or "unknown"
            if not isinstance(code, str):
                continue
            bucket[code] = bucket.get(code, 0) + 1
    return [
        {"depth_profile": dp, "provider": pv, "reason_code": code, "claims": n}
        for (dp, pv), bucket in sorted(agg.items())
        for code, n in sorted(bucket.items())
    ]


# `usage_stats()`/`usage_depth_rounds()` が共有する `chat-round` 取得 SQL。
#
# 期間境界は「巡が属する user ターン」の `created_at`（`_USAGE_TURN_CTE` 等の他集計と同じ境界に
# 揃える）を使う——巡イベント自身の `ts` で絞ると、同じユーザーターンの巡別記録と最終回答
# （`turns`/`conv_turn_rows` 系）が異なる期間境界に割れ、両者を突き合わせる集計（例:
# reason_codes の final/rounds 比較）の母集団がずれる。「所属する user ターン」は
# 「その巡の ts 以前で最も新しい同会話の user メッセージ」（`user_msgs` の LATERAL）。見つからない
# （過去データ・想定外の順序）場合は巡自身の `ts` へ後退する＝従来どおり必ず何らかの期間値を持つ。
#
# `rounds` CTE は `e.ts >= start_ts`（下限のみ）で事前に絞る——`turn_created_at <= e.ts` が
# 常に成り立つため、下限は最終 WHERE の `turn_created_at >= start_ts` を満たす行を取りこぼさず、
# 期間外の巡別記録を全履歴から走査する分を減らせる。上限は付けない（`turn_created_at` は
# `e.ts` より過去になり得るため、`e.ts < end_exclusive_ts` を先に切ると期間内のターンに属す
# 巡を落としかねない——最終 WHERE の `turn_created_at < end_exclusive_ts` だけで絞る）。
#
# assistant 対応付け（最終 LATERAL）は「巡の ts 以降・かつ同会話の**次の** user メッセージより前」
# に限定する（上限なしの `m.created_at >= r.ts` だけだと、このターンの assistant が
# 保存されなかった行＝利用者の停止等で assistant 未保存の巡が、次ターンの assistant に
# 誤って結合し水増しされる）。範囲内に assistant が無ければ `turn_message_id` は NULL のまま
# （`_compute_round_stats` 側で `unmatched_rounds` に計上する）。
def _round_rows_query(c, start_ts, end_exclusive_ts):
    return c.execute(
        "WITH rounds AS ("
        "  SELECT e.id, e.ts, e.provider, e.input_tokens, e.output_tokens, e.elapsed_ms, e.meta, "
        "    e.conversation_id "
        "  FROM usage_events e WHERE e.kind = 'chat-round' AND e.ts >= %s"
        "), touched_convs AS ("
        "  SELECT DISTINCT conversation_id FROM rounds"
        "), user_msgs AS ("
        "  SELECT m.conversation_id, m.created_at, "
        "    LEAD(m.created_at) OVER (PARTITION BY m.conversation_id ORDER BY m.created_at) "
        "      AS next_user_created_at "
        "  FROM messages m JOIN touched_convs t ON t.conversation_id = m.conversation_id "
        "  WHERE m.role = 'user'"
        "), owning AS ("
        "  SELECT r.id AS round_id, r.ts, r.provider, r.input_tokens, r.output_tokens, r.elapsed_ms, "
        "    r.meta, r.conversation_id, COALESCE(u.created_at, r.ts) AS turn_created_at, "
        "    u.next_user_created_at "
        "  FROM rounds r LEFT JOIN LATERAL ("
        "    SELECT um.created_at, um.next_user_created_at FROM user_msgs um "
        "    WHERE um.conversation_id = r.conversation_id AND um.created_at <= r.ts "
        "    ORDER BY um.created_at DESC LIMIT 1"
        "  ) u ON true"
        ") "
        "SELECT o.ts, o.provider, o.input_tokens, o.output_tokens, o.elapsed_ms, o.meta, "
        "  ta.id AS turn_message_id, ta.answer->'usage'->>'depth_profile' AS depth_profile "
        "FROM owning o "
        "JOIN conversations c ON c.id = o.conversation_id "
        "LEFT JOIN LATERAL ("
        "  SELECT m.id, m.answer FROM messages m "
        "  WHERE m.conversation_id = o.conversation_id AND m.role = 'assistant' "
        "    AND m.created_at >= o.ts "
        "    AND (o.next_user_created_at IS NULL OR m.created_at < o.next_user_created_at) "
        "  ORDER BY m.created_at ASC LIMIT 1"
        ") ta ON true "
        "WHERE c.deleted_at IS NULL AND c.origin = 'own' "
        "  AND o.turn_created_at >= %s AND o.turn_created_at < %s",
        (start_ts, start_ts, end_exclusive_ts),
    ).fetchall()


# 最終回答の主張（`answer->'data'->'claims'`）のうち不明の理由コードを数えるための取得 SQL
# （`usage_stats()`/`usage_depth_rounds()` が共有）。`gate_missing_codes`（S1b・Codex 経路の
# 最終ゲート・`answer->'data'->'evidence_gate'->'missing_codes'`）も同じ行から拾う——Codex は
# `chat-round` を発生させない経路のため、巡別記録（`_round_rows_query`）には不足軸が載らず、
# ここが唯一の取得点になる（API 経路にはこのキー自体が無く NULL のまま＝`_compute_round_stats`
# 側の `missing_codes` 集計と二重計上にならない）。
def _final_claims_rows_query(c, start_ts, end_exclusive_ts):
    return c.execute(
        _USAGE_TURN_CTE + " "
        "SELECT answer->'usage'->>'provider' AS provider, "
        "  answer->'usage'->>'depth_profile' AS depth_profile, "
        "  answer->'data'->'claims' AS claims, "
        "  answer->'data'->'evidence_gate'->'missing_codes' AS gate_missing_codes "
        "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s "
        "  AND jsonb_typeof(answer->'data'->'claims') = 'array'",
        (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
    ).fetchall()


def _compute_final_missing_codes(final_claims_rows) -> dict[tuple, dict[str, int]]:
    """S1b: 最終回答の `evidence_gate.missing_codes`（Codex 経路の最終ゲート・巡を発生させない
    ため `chat-round` には載らない）を深さ×経路で合算する。非配列/欠落（API 経路・v1・旧データ）
    は静かにスキップする（`_compute_final_reason_codes` と同じ「非配列は無視」防御）。
    """
    agg: dict[tuple, dict[str, int]] = {}
    for r in final_claims_rows:
        codes = r["gate_missing_codes"]
        if not isinstance(codes, list):
            continue
        depth = r["depth_profile"] or "unknown"
        provider = r["provider"] or "unknown"
        bucket = agg.setdefault((depth, provider), {})
        for code in codes:
            if isinstance(code, str) and code:
                bucket[code] = bucket.get(code, 0) + 1
    return agg


def _merge_final_missing_codes(rounds_stats: dict, final_claims_rows) -> None:
    """S1b: 最終回答由来の不足軸（`_compute_final_missing_codes`）を、既存の巡別集計
    （`rounds_stats["by_depth_provider"]`・表示は `web/usage.js::reviewCounts(r.missing_codes)`
    のまま＝新しい表は作らない）へ深さ×経路で合流させる。Codex は `chat-round` を発生させない
    ため、既存集計に対応するバケットが無い（深さ, "codex"）組は新規バケットとして追加する
    （`rounds` 等の巡別指標は0のまま＝Codex 側にその意味の値が無いことを表す）。
    """
    agg = _compute_final_missing_codes(final_claims_rows)
    if not agg:
        return
    by_key = {(a["depth_profile"], a["provider"]): a for a in rounds_stats["by_depth_provider"]}
    for key, codes in agg.items():
        bucket = by_key.get(key)
        if bucket is None:
            bucket = _new_round_bucket(*key)
            bucket["elapsed_ms_avg"] = None
            bucket["citations_delta_avg"] = None
            by_key[key] = bucket
            rounds_stats["by_depth_provider"].append(bucket)
        for code, n in codes.items():
            bucket["missing_codes"][code] = bucket["missing_codes"].get(code, 0) + n
    rounds_stats["by_depth_provider"].sort(key=lambda a: (a["depth_profile"], a["provider"]))


def _round_reason_codes(rounds_stats: dict, final_claims_rows) -> dict:
    """理由コード分布の2軸（`final`＝最終回答の主張／`rounds`＝巡別記録の主張内訳の合算）。"""
    return {
        "final": _compute_final_reason_codes(final_claims_rows),
        "rounds": [
            {"depth_profile": a["depth_profile"], "provider": a["provider"],
             "reason_code": code, "claims": n}
            for a in rounds_stats["by_depth_provider"]
            for code, n in sorted(a["reason_codes"].items())
        ],
    }


def usage_stats(days: int = 30, *, time_from: str | None = None, time_to: str | None = None) -> dict:
    """期間内の利用統計を集計する（本文/タイトルは含めない）。

    `time_from`/`time_to`（ISO 8601・オフセット必須）を渡すと `days` の代わりに `[from, to)` を
    期間に使う（`_usage_period` 参照）。規則違反は `UsagePeriodError`。

    users: ターン数（role='user' メッセージ数）降順。totals: 期間合計。
    daily: 日別ターン数＋日別アクティブユーザー数。
    period: 集計対象の JST 暦日範囲（start/end・フロントの日別チャートはこの範囲でゼロ埋め描画する）。

    lens 内訳・personal 利用ターン数は「各 user ターンに対応する最初の assistant 返答」だけを数える
    （_USAGE_TURN_CTE 参照・assistant 単独行の混入防止）。

    active_days・daily の日付境界は **user メッセージのみ**を **JST（Asia/Tokyo）**で区切り、
    **`_usage_period_bounds` で計算した固定の JST 暦日下限**を users/daily/audit すべてに使う
    （表とグラフの合計を一致させる）。

    集計の前提:
      - `c.origin='own'` に限定（sanitized_snapshot は本文コピー済みの内部成果物で、同じ owner の
        別 conversation として messages が二重に存在するため、含めると owner の turns/daily/active_days
        が水増しされる。received_share は自分名義の messages を持たないため実害は無いが明示的に除外）。
      - `conversations`/`active_days`/`last_active` は role='user' 基準に統一し、
        `HAVING` で「期間内に user turn が 0 件」の行（assistant のみ該当した見せかけの活動）を除外する。

    「利用の傾向」指標（既存の境界/origin/turn 規約を再利用・N+1 は
    避けるが単一クエリ主義ではない＝固定本数の追加クエリ）:
      - `zero_hit`（全体）／各 user 行の `knowledge_turns`/`zero_hit_turns`/`zero_hit_rate`:
        ナレッジ参照オンのターン（lens != 'chat'）のうち assistant answer.sources が空の割合
        （_USAGE_TURN_CTE の `answer` を使い、既存の user_rows 集計に FILTER 列を追加するだけ＝新規クエリ無し）。
        `answer->'sources'` が NULL・欠落・非配列（想定外データ）でも 500 にしない
        （`jsonb_typeof(...)='array'` を先に確認してから `jsonb_array_length` を呼ぶ・
        素朴な `COALESCE(jsonb_array_length(...), 0)` は非配列で例外になる）。
      - `heatmap`: user メッセージ数を JST 曜日(0=日〜6=土)×時間帯(0-23)で集計（sparse・0 件のセルは
        返さない＝フロントでゼロ埋め）。
      - `worlds`: `turns`（conversations.version）別ターン数の内訳（world が1つでも正直に1行返す）。
      - `providers`: `chat.turn` 監査の `detail->>'provider'` 別ターン数。書込側（`AGENT_PROVIDERS`）で
        allowlist 正規化済みのはずだが、集計（読み出し）側でも同じ allowlist で畳み込む二重防御
        （`_USAGE_KNOWN_PROVIDERS`・Python 側の `or "unknown"` では NULL しか
        拾えず、allowlist 外の異なる不正値が別行のまま残ってしまう）。stopped ターンも
        `detail.stopped` に関わらず母数に含む（画面側で注記）。
      - `retention`: JST 週（Postgres `date_trunc('week', ...)` ＝月曜始まり）ごとのアクティブユーザー数の
        推移と、**連続する**週ペア（7日差のペアのみ・間が空いた週は「前週」として扱わない）をプールした
        再訪率（前週アクティブの延べ人数のうち、翌週もアクティブだった延べ人数の割合）。週ペアが無ければ
        `revisit_rate=None`。
      - `downloads`: `document.downloaded` 監査の期間合計＋日別内訳（「出典クリック数」計測基盤が無いため
        原本DL数で代替＝新規テレメトリは追加しない）。

    会話セッション指標:
      - `conversation_turns`: 期間内の user ターン数（`turn_created_at` が期間内）について、
        会話あたりの件数の avg／median／max／p90（対象は「期間内に user ターンが1件以上ある会話」・
        会話の全履歴ではない）。対象会話が無ければ全て None。
      - `resume_rate`: user ターン数2以上の会話のうち `conversations.codex_session_id` が設定されている
        割合。対象会話が無ければ None（推定しない）。`_compute_conversation_turn_stats` 参照。

    `docs/proposals/2026-09-12-利用統計の拡充2.md` §2/§3:
      - `tokens.by_user_kind`: ユーザー別 × 用途別（kind）の calls/tokens/elapsed_ms（`tokens.by_kind`
        と同じ材料・同じ扱い）。chat 行（`messages.answer->'usage'` 由来）は `token_by_user`
        （`user_rows` と同じ `turns` CTE 集計）から `kind='chat'` として合流し、それ以外の kind は
        `usage_events`（`user_id IS NOT NULL` のみ＝集計できない匿名呼び出しは含めない）を
        `user_id, kind` で集計する。`by_kind` と同じ内訳を利用者ごとに分けた形だが、user_id が
        NULL の行（取り込み時の埋め込み・画像読み取り・rag_render 等の利用者に紐付かない呼び出し）は
        含まれないため、同一 kind の合計は `by_kind` の当該行**以下**になりうる。並びは `(uid, kind)`。
      - `response_time`: 期間内の assistant 行（`c.origin='own'・deleted_at IS NULL`＝他の集計と
        同じ母集団）の `answer->>'duration_ms'`（1ターンの壁時計所要時間・`chat_service.py` が
        埋め込む）から、全体（`overall`）と経路別（`by_provider`＝`answer->'usage'->>'provider'`・
        取れなければ `'unknown'`）の avg/median/p90/max/件数を計算する（`_compute_response_time_stats`・
        最近傍順位法は `_percentile` と共通）。利用者の明示停止・実行中のターンは assistant を
        保存しないため対象に含まれず、duration が保存されなかった行（想定外データ）も
        `~ '^[0-9]+$'` で弾いて除外する（0件なら avg/median/p90/max=None・n=0）。
      - `conversations_top`: 期間内に user ターンが1件以上ある会話（`conversation_turns`/`resume_rate`
        と同じ母集団）について、会話 id・uid・world・user ターン数・用途別（kind）内訳
        （`kinds`＝chat は `turns` の `answer->'usage'` 合計・他は `usage_events` を
        `conversation_id` で集計・null の意味は `tokens.by_kind` と同じ）・回答時間の平均
        （`duration_ms` が無い行は除外）を、トークン合計（`kinds` 内の input+output の合算・
        報告不能＝None は合算時のみ0扱い）の降順で上位20件。`usage_events.conversation_id` が
        NULL の行（列追加前の過去データ・遡及なし）はどの会話にも合流しない
        （`conversation_id = ANY(%s)` の対象会話 id 一覧に含まれないため）。タイトル・本文は含まない。

    ターンの終了理由:
      - `stop_kinds`: `messages.answer->>'stop_kind'`（`sherpa/stop_kind.py` の閉じた8値・
        `chat_service._finalize` が保存）の分布。assistant 返答が存在するターン（`answer IS NOT NULL`）
        のみを対象にする。利用者の明示停止は `stopped_turns` 側だけで数える（二重計上しない）——
        巡ループの停止終端は assistant（`stop_kind='stopped_by_user'`）を保存するため、この分布
        からは明示的に除外する。実行中のターンは `answer` が無い。確認カード（`lens='clarify'`）も
        母数から外す。allowlist（`stop_kind.STOP_KINDS`）外の値（NULL・語彙外の不正値のいずれも）は
        `'unknown'` へ畳み込む（allowlist 外を集約する `providers_usage` と同じ思想）——
        未計測経路・過去データのほか、busy（Codex 直列化で実行していないターン）と API 経路の
        honest failure（型を特定できない失敗）も対象。
      - `stopped_turns`: 利用者の明示停止（`chat.turn` 監査の `detail.stopped=true`）の件数——
        `stop_kinds` の分布から `stopped_by_user` を除いてこちらへ一本化した別集計。境界は
        `turns`/`stop_kinds` と同じ `turn_created_at`（`detail.message_id_user` で結合した
        user 発言の created_at・`_stopped_turns_sql` 参照）。

    期間の**基準時刻は指標ごとに違う**（同じ半開区間 `[from, to)` を、各指標が自然に属する時刻へ
    当てる）:
      - ターン由来の集計（users/daily/lens/limits/stop_kinds/回答時間ほか）: user 発言の
        `turn_created_at`。
      - `usage_events` 由来（chat-sub/chat-review/intent/embed ほか）: イベント自身の `ts`。
      - 巡集計（`rounds`・kind='chat-round'）: **その巡が属する user 発言の `created_at`**
        （`_round_rows_query`）——巡イベント自身の `ts` が `to` を越えていても、所属する user 発言が
        期間内なら数える（最終回答由来の集計と母集団を揃えるため）。
      - 品質採点（`quality_runs`）: **実行期間（`executed_from`/`executed_to`）の完全包含**
        （`depth_quality_stats`）——登録時刻では絞らない。

    全クエリは `_usage_period_bounds` の `[start_ts, end_exclusive_ts)` という同じ半開区間で絞る
    （下限のみだと、クロックスキュー/テスト由来の未来時刻行が
    「期間内」に混入し得る。DL/provider/heatmap/retention/world/user/daily すべて同じ上下限）。
    """
    _ensure()
    start_ts, end_exclusive_ts, period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        user_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT user_id AS uid, "
            "  COUNT(*) AS turns, "
            "  COUNT(DISTINCT conversation_id) AS conversations, "
            "  COUNT(DISTINCT (turn_created_at AT TIME ZONE 'Asia/Tokyo')::date) AS active_days, "
            "  MAX(turn_created_at) AS last_active, "
            "  COUNT(*) FILTER (WHERE lens='impact') AS lens_impact, "
            "  COUNT(*) FILTER (WHERE lens='qa') AS lens_qa, "
            "  COUNT(*) FILTER (WHERE lens='troubleshoot') AS lens_troubleshoot, "
            "  COUNT(*) FILTER (WHERE lens='chat') AS lens_chat, "
            "  COUNT(*) FILTER (WHERE user_personal) AS personal_turns, "
            "  ARRAY_REMOVE(ARRAY_AGG(DISTINCT version), NULL) AS worlds, "
            "  COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens != 'chat') AS knowledge_turns, "
            "  COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens != 'chat' AND "
            "    CASE WHEN jsonb_typeof(answer->'sources')='array' "
            "         THEN jsonb_array_length(answer->'sources') ELSE 0 END = 0) AS zero_hit_turns "
            "FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s "
            "GROUP BY user_id "
            "ORDER BY turns DESC, user_id",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        daily_rows = c.execute(
            "SELECT (m.created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, "
            "  COUNT(*) AS turns, "
            "  COUNT(DISTINCT c.user_id) AS active_users "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.created_at >= %s AND m.created_at < %s AND c.deleted_at IS NULL "
            "  AND m.role='user' AND c.origin='own' "
            "GROUP BY (m.created_at AT TIME ZONE 'Asia/Tokyo')::date ORDER BY date",
            (start_ts, end_exclusive_ts),
        ).fetchall()
        audit_rows = c.execute(
            "SELECT actor_user_id AS uid, action, COUNT(*) AS n FROM audit_log "
            "WHERE created_at >= %s AND created_at < %s AND action = ANY(%s) "
            "  AND actor_user_id IS NOT NULL "
            "GROUP BY actor_user_id, action",
            (start_ts, end_exclusive_ts, list(_USAGE_AUDIT_ACTIONS)),
        ).fetchall()
        name_rows = c.execute("SELECT uid, display_name FROM users").fetchall()
        world_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT version AS world, COUNT(*) AS turns FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s AND version IS NOT NULL "
            "GROUP BY version ORDER BY turns DESC, version",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        provider_rows = c.execute(
            "SELECT CASE WHEN detail->>'provider' = ANY(%s) THEN detail->>'provider' ELSE 'unknown' END AS provider, "
            "  COUNT(*) AS n FROM audit_log "
            "WHERE created_at >= %s AND created_at < %s AND action='chat.turn' "
            "GROUP BY provider ORDER BY n DESC",
            (list(_USAGE_KNOWN_PROVIDERS), start_ts, end_exclusive_ts),
        ).fetchall()
        # 終了理由（`turns.answer->>'stop_kind'`・`stop_kind.py` の8値）の分布。`turns`（`_USAGE_TURN_CTE`）
        # 経由にすることで、`turns`/`stopped_turns` と同じ `turn_created_at`（user 行の created_at）を
        # 境界に使う——期間境界を跨ぐターン（user 行が期間内・assistant 行が期間外）でも
        # turns 側と同じ側に計上され、両者の合計が食い違わない。`answer IS NOT NULL` で
        # 「assistant 返答が存在するターン」だけに絞る——利用者の明示停止・実行中のターンは
        # assistant を保存しないため `answer` が無く（LEFT JOIN の不一致）、絞りが無いと
        # allowlist 外の畳み込みで 'unknown' に混入し `stopped_turns` と二重計上になる。
        # 巡ループの停止終端は assistant を保存するため `stopped_by_user` を明示的に除く
        # （停止ターンは `stopped_turns` 側だけで数える契約）。allowlist（`stop_kind.STOP_KINDS`）
        # 外の値（語彙外の不正値・想定されない NULL のいずれも）は 'unknown' へ畳み込む
        # （既存の provider_rows の allowlist 畳み込みと同じ思想）。確認カード
        # （lens='clarify'＝意図確認の一時停止・終了理由を持たない正常な行）は母数から外す。
        stop_kind_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT CASE WHEN answer->>'stop_kind' = ANY(%s) THEN answer->>'stop_kind' "
            "  ELSE 'unknown' END AS stop_kind, COUNT(*) AS n "
            "FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s "
            "  AND answer IS NOT NULL "
            "  AND lens IS DISTINCT FROM 'clarify' "
            "  AND answer->>'stop_kind' IS DISTINCT FROM 'stopped_by_user' "
            "GROUP BY stop_kind ORDER BY n DESC",
            (start_ts, end_exclusive_ts, list(stop_kind.STOP_KINDS), start_ts, end_exclusive_ts),
        ).fetchall()
        # limits（「打ち切りの内訳」・経路別）: `stop_kind_rows` から `stopped_by_user` の除外だけを
        # 外した population（turn_created_at 境界・answer IS NOT NULL・clarify 除外——巡ループの
        # 停止終端が保存した行も含む＝停止ターンで当たった制限も内訳に残す）に
        # `answer->'usage'->>'provider'` 別の集計を足す
        # （`token_by_model` と同じ provider 抽出キー・専用フィールドは持たない＝重複させない）。
        limits_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT COALESCE(answer->'usage'->>'provider', 'unknown') AS provider, "
            "  COUNT(*) AS turns, " + _usage_limits_select_cols() + " "
            "FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s "
            "  AND answer IS NOT NULL "
            "  AND lens IS DISTINCT FROM 'clarify' "
            "GROUP BY provider ORDER BY turns DESC",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        # 利用者停止（`stopped_by_user`）は上の分布から除いてある（assistant 未保存の停止に加え、
        # 巡ループの停止終端が保存する行も除外する）——監査
        # `chat.turn`（`detail.stopped=true`）から別途数える（`_stopped_turns_sql` 参照・
        # `turns`/`stop_kinds` と同じ `turn_created_at` 境界）。`audit_log` は削除伝播の対象外
        # （台帳の削除伝播は原本/MD/ES/Neo4j までで、監査ログは残置する契約）のため、
        # `conversations` と JOIN して `turns`/`stop_kinds` と同じ population（`deleted_at IS NULL
        # AND origin='own'`）に絞る——会話が後で削除されたり共有受領（origin != 'own'）だったりする分を
        # 母数から外す。`resource_id` は `chat.turn` 監査の書込側（`chat_service.py`／`routers/chat.py`）
        # が常に `f"conv:{conversation_id}"` 形式で書く契約。
        stopped_turns_row = c.execute(
            _stopped_turns_sql(), (start_ts, end_exclusive_ts)
        ).fetchone()
        heatmap_rows = c.execute(
            "SELECT EXTRACT(DOW FROM (m.created_at AT TIME ZONE 'Asia/Tokyo'))::int AS weekday, "
            "  EXTRACT(HOUR FROM (m.created_at AT TIME ZONE 'Asia/Tokyo'))::int AS hour, "
            "  COUNT(*) AS n "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.created_at >= %s AND m.created_at < %s AND c.deleted_at IS NULL "
            "  AND m.role='user' AND c.origin='own' "
            "GROUP BY weekday, hour",
            (start_ts, end_exclusive_ts),
        ).fetchall()
        week_user_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT DISTINCT user_id AS uid, "
            "  date_trunc('week', turn_created_at AT TIME ZONE 'Asia/Tokyo')::date AS week_start "
            "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        download_daily_rows = c.execute(
            "SELECT (created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, COUNT(*) AS n FROM audit_log "
            "WHERE created_at >= %s AND created_at < %s AND action='document.downloaded' "
            "GROUP BY date ORDER BY date",
            (start_ts, end_exclusive_ts),
        ).fetchall()
        # トークン使用量（answer->'usage'）を provider/model 別・上位ユーザー別・日別で集計。
        #   入力/出力トークン数のみ集計する（金額換算はしない）。
        #   usage を持たないターン（heuristic・停止・旧データ）は自然に除外。
        token_model_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT answer->'usage'->>'provider' AS provider, answer->'usage'->>'model' AS model, "
            + _usage_token_sum_cols() + " FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s" + _USAGE_TOKEN_WHERE +
            "GROUP BY provider, model ORDER BY input DESC, output DESC",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        token_user_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT user_id AS uid, " + _usage_token_sum_cols() + " FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s" + _USAGE_TOKEN_WHERE +
            "GROUP BY user_id ORDER BY (SUM(" + _usage_tok('input_tokens') + ") + SUM("
            + _usage_tok('output_tokens') + ")) DESC, user_id",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        token_daily_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT (turn_created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, "
            f"SUM({_usage_tok('input_tokens')}) AS input, SUM({_usage_tok('output_tokens')}) AS output "
            "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s" + _USAGE_TOKEN_WHERE +
            "GROUP BY date ORDER BY date",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        # チャット以外の LLM 呼び出し（intent 分類・
        # グラフ抽出・概念候補提案・埋め込み・admin グラフ質問・VLM）を kind 別に集計。usage_events は
        # kind='chat' を含まない（chat は token_model_rows 由来で別途合成する・二重計上なし）。
        # kind='chat-round'（査読の巡別記録）も除く——巡の消費は既に
        # `chat-sub`（worker）・`chat-review`（evaluator/orchestrator）・`answer.usage`（清書）
        # として正本に載っており、巡別記録は表示・分析用の別イベントで二重に足さない。
        # elapsed_ms は計測スコープ外の行（NULL）を
        # 自然に除いて集計する（SUM/AVG は NULL を無視・COUNT(列) は非 NULL 行数＝`elapsed_n`）。
        usage_event_rows = c.execute(
            "SELECT kind, provider, model, SUM(calls) AS calls, "
            "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
            "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output, "
            "  SUM(elapsed_ms) AS elapsed_ms_total, AVG(elapsed_ms) AS elapsed_ms_avg, "
            "  COUNT(elapsed_ms) AS elapsed_n "
            "FROM usage_events WHERE ts >= %s AND ts < %s AND kind <> 'chat-round' "
            "GROUP BY kind, provider, model ORDER BY kind, input DESC NULLS LAST",
            (start_ts, end_exclusive_ts),
        ).fetchall()
        # ユーザー別 × 用途別（kind）内訳（usage_events 側）。`user_id IS NOT NULL` で
        # 絞る——匿名呼び出し（ext:等ユーザー本人以外・世界単位のバックグラウンド処理）は
        # どの利用者にも属さないため by_user_kind には出せない（by_kind 側では引き続き集計対象）。
        usage_event_user_kind_rows = c.execute(
            "SELECT user_id AS uid, kind, SUM(calls) AS calls, "
            "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
            "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output, "
            "  SUM(elapsed_ms) AS elapsed_ms_total, AVG(elapsed_ms) AS elapsed_ms_avg, "
            "  COUNT(elapsed_ms) AS elapsed_n "
            "FROM usage_events WHERE ts >= %s AND ts < %s AND user_id IS NOT NULL AND kind <> 'chat-round' "
            "GROUP BY user_id, kind ORDER BY user_id, kind",
            (start_ts, end_exclusive_ts),
        ).fetchall()
        # 回答時間（`answer->>'duration_ms'`・chat_service.py が埋め込む1ターンの壁時計）。
        # 対象は他の集計と同じ母集団（origin='own'・deleted_at IS NULL）の assistant 行のみ。
        # 停止/実行中のターンは assistant 自体が無く、duration_ms が数値でない行（想定外データ）は
        # 正規表現で弾く——`_usage_tok` と同じ防御思想（非数値/欠落は集計対象から静かに除く）。
        response_time_rows = c.execute(
            "SELECT answer->'usage'->>'provider' AS provider, (answer->>'duration_ms')::bigint AS duration_ms "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.created_at >= %s AND m.created_at < %s AND m.role='assistant' "
            "  AND c.deleted_at IS NULL AND c.origin='own' "
            "  AND m.lens IS DISTINCT FROM 'clarify' "   # 確認カードは回答前の一時停止＝回答時間ではない
            "  AND answer->>'duration_ms' ~ '^[0-9]+$'",
            (start_ts, end_exclusive_ts),
        ).fetchall()
        # 会話あたりの user ターン数分布・resume_rate。`turns`（`_USAGE_TURN_CTE`）由来にすることで、
        # `totals.conversations`／`users[].conversations`（`user_rows`）と同じ母集団（期間内の
        # `turn_created_at`・origin='own'・deleted_at IS NULL）に揃える——ここで数える user_turns は
        # 「期間内の user ターン数」であり、会話の全履歴ではない（`c.id` で GROUP BY＝主キーへの
        # 関数従属により `codex_session_id` を非集約のまま選べる）。
        conversation_turn_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT c.id AS cid, c.codex_session_id, COUNT(*) AS user_turns "
            "FROM turns JOIN conversations c ON c.id = turns.conversation_id "
            "WHERE turn_created_at >= %s AND turn_created_at < %s "
            "GROUP BY c.id",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        # 会話ごとの補助 AI 使用量（`docs/proposals/2026-09-12-利用統計の拡充2.md` §2 (b)）。対象は
        # 「期間内に user ターンが1件以上ある会話」（他の会話系集計と同じ母集団）——1行=1会話。
        # chat（messages.answer->'usage'）の合計と回答時間平均（duration_ms・欠落行は AVG が自然に除外）
        # をここで集計し、それ以外の kind（usage_events 由来）は下の conv_kind_rows で別途取得して
        # Python 側で合流する（`tokens.by_kind`/`by_user_kind` と同じ「chat は turns 由来・他は
        # usage_events 由来」という合成方針）。
        conv_turn_rows = c.execute(
            _USAGE_TURN_CTE + " "
            "SELECT conversation_id AS cid, user_id AS uid, version AS world, "
            "  COUNT(*) AS user_turns, "
            "  COUNT(*) FILTER (WHERE jsonb_typeof(answer->'usage')='object') AS chat_calls, "
            f"  SUM({_usage_tok('input_tokens')}) AS chat_input, "
            f"  SUM({_usage_tok('cached_input_tokens')}) AS chat_cached_input, "
            f"  SUM({_usage_tok('output_tokens')}) AS chat_output, "
            f"  SUM({_usage_tok('reasoning_output_tokens')}) AS chat_reasoning_output, "
            "  AVG(CASE WHEN lens IS DISTINCT FROM 'clarify' AND answer->>'duration_ms' ~ '^[0-9]+$' "
            "    THEN (answer->>'duration_ms')::bigint END) AS avg_response_time_ms "
            "FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s "
            "GROUP BY conversation_id, user_id, version",
            (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
        ).fetchall()
        _conv_cids = [r["cid"] for r in conv_turn_rows]
        # usage_events は明示的に `conv_turn_rows` が返した会話 id（=期間内に user ターンがある会話）に
        # 限定して JOIN する——`conversation_id IS NULL` の行（列追加前の過去データ・遡及なし契約）は
        # この ANY(%s) にどのみち一致しないため自然に除外される（どの会話にも混ざらない）。
        conv_kind_rows = (
            c.execute(
                "SELECT conversation_id AS cid, kind, SUM(calls) AS calls, "
                "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
                "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output, "
                "  SUM(elapsed_ms) AS elapsed_ms_total, AVG(elapsed_ms) AS elapsed_ms_avg, "
                "  COUNT(elapsed_ms) AS elapsed_n "
                "FROM usage_events WHERE ts >= %s AND ts < %s AND conversation_id = ANY(%s) "
                "  AND kind <> 'chat-round' "
                "GROUP BY conversation_id, kind",
                (start_ts, end_exclusive_ts, _conv_cids),
            ).fetchall()
            if _conv_cids else []
        )
        # 巡別記録（`chat-round`）の表示専用集計——深さ（`answer->'usage'->>'depth_profile'`）×
        # 経路（provider）別の巡数分布・活動量（引用増分・所要時間・トークン）・主張の区分/理由
        # コード内訳。期間境界・assistant 対応付けの規約は `_round_rows_query` 参照（他集計と同じ
        # 「所属する user ターン」基準の境界に揃える）。
        round_rows = _round_rows_query(c, start_ts, end_exclusive_ts)
        # 最終回答の主張のうち不明（`status='unknown'`）の理由コード分布（主張単位）。深さ・経路
        # （`answer->'usage'`）別に数える——巡別（chat-round）の集計とは別軸（こちらは全巡を経た
        # 最終回答時点の判定・巡ごとの是正で覆った分は数えない）。
        final_claims_rows = _final_claims_rows_query(c, start_ts, end_exclusive_ts)

    display_names = {r["uid"]: r["display_name"] for r in name_rows}
    _aux_key = {"auth.login": "logins", "document.downloaded": "downloads",
                "workspace.file_uploaded": "uploads", "share.created": "shares"}
    aux_by_uid: dict[str, dict] = {}
    for r in audit_rows:
        d = aux_by_uid.setdefault(r["uid"], {"logins": 0, "downloads": 0, "uploads": 0, "shares": 0})
        key = _aux_key.get(r["action"])
        if key:
            d[key] = r["n"]

    users = []
    total_turns = 0
    total_conversations = 0
    total_knowledge_turns = 0
    total_zero_hit_turns = 0
    for r in user_rows:
        uid = r["uid"]
        turns = r["turns"] or 0
        conversations = r["conversations"] or 0
        knowledge_turns = r["knowledge_turns"] or 0
        zero_hit_turns = r["zero_hit_turns"] or 0
        total_turns += turns
        total_conversations += conversations
        total_knowledge_turns += knowledge_turns
        total_zero_hit_turns += zero_hit_turns
        aux = aux_by_uid.get(uid, {"logins": 0, "downloads": 0, "uploads": 0, "shares": 0})
        users.append({
            "uid": uid,
            "display_name": display_names.get(uid) or uid,
            "turns": turns,
            "conversations": conversations,
            "active_days": r["active_days"] or 0,
            "last_active": r["last_active"],
            "lens": {
                "impact": r["lens_impact"] or 0,
                "qa": r["lens_qa"] or 0,
                "troubleshoot": r["lens_troubleshoot"] or 0,
                "chat": r["lens_chat"] or 0,
            },
            "personal_turns": r["personal_turns"] or 0,
            "worlds": sorted(r["worlds"] or []),
            "logins": aux["logins"],
            "downloads": aux["downloads"],
            "uploads": aux["uploads"],
            "shares": aux["shares"],
            "knowledge_turns": knowledge_turns,
            "zero_hit_turns": zero_hit_turns,
            "zero_hit_rate": (zero_hit_turns / knowledge_turns) if knowledge_turns > 0 else None,
        })
    totals = {"turns": total_turns, "active_users": len(users), "conversations": total_conversations}
    daily = [{"date": str(r["date"]), "turns": r["turns"] or 0, "active_users": r["active_users"] or 0}
            for r in daily_rows]
    # フロントの日別チャートはこの範囲でゼロ埋め描画する（クライアント側で「今日」を再計算させない・
    # RV ラウンド3 MEDIUM: サーバ算出の境界とフロント描画範囲を一致させる）。
    # `period` は `_usage_period` が組み立て済み（`days` 指定でも `from`/`to` 指定でも `start`/`end`
    # の意味は不変＝JST 暦日・`end` は含む終了日。実際に使った半開区間は `from`/`to`）。

    zero_hit = {
        "knowledge_turns": total_knowledge_turns,
        "zero_hit_turns": total_zero_hit_turns,
        "rate": (total_zero_hit_turns / total_knowledge_turns) if total_knowledge_turns > 0 else None,
    }
    worlds_usage = [{"world": r["world"], "turns": r["turns"] or 0} for r in world_rows]
    # allowlist 外/NULL の畳み込みは SQL 側（CASE式・GROUP BY）で
    # 完結している＝同じ 'unknown' に集約された複数の元値が別行として残ることはない（二重集計の防止）。
    providers_usage = [{"provider": r["provider"], "turns": r["n"] or 0} for r in provider_rows]
    heatmap = [{"weekday": r["weekday"], "hour": r["hour"], "count": r["n"] or 0} for r in heatmap_rows]
    # 終了理由の分布＋利用者停止の件数（`stop_kind.py` の8値・'unknown' は畳み込み済み）。
    stop_kinds = [{"stop_kind": r["stop_kind"], "turns": r["n"] or 0} for r in stop_kind_rows]
    stopped_turns = (stopped_turns_row["n"] or 0) if stopped_turns_row else 0

    # limits（「打ち切りの内訳」・経路別・利用統計 U 系と同じ形＝行=provider の list）。
    # `*_turns`＝回数系は1回以上・bool系は真だったターン数、`*_total`＝回数系の合計回数。
    by_provider_limits = [
        {"provider": r["provider"], "turns": r["turns"] or 0,
         **{f"{f}_turns": r[f"{f}_turns"] or 0 for f in _USAGE_LIMIT_INT_FIELDS},
         **{f"{f}_total": int(r[f"{f}_total"] or 0) for f in _USAGE_LIMIT_INT_FIELDS},
         **{f"{f}_turns": r[f"{f}_turns"] or 0 for f in _USAGE_LIMIT_BOOL_FIELDS}}
        for r in limits_rows
    ]
    limits_stats = {"by_provider": by_provider_limits}

    # 定着指標: JST 週（月曜始まり）ごとのアクティブユーザー集合→週次人数の推移＋連続週ペアの再訪率。
    retention = _compute_retention(week_user_rows)

    download_daily = [{"date": str(r["date"]), "count": r["n"] or 0} for r in download_daily_rows]
    downloads = {"total": sum(r["count"] for r in download_daily), "daily": download_daily}

    # トークン使用量（provider/model 別・上位ユーザー別・日別）。金額換算はしない。
    token_by_model = [{"provider": r["provider"] or "unknown", "model": r["model"] or "",
                       "turns": r["turns"] or 0, "input": int(r["input"] or 0),
                       "cached_input": int(r["cached_input"] or 0), "output": int(r["output"] or 0),
                       "reasoning_output": int(r["reasoning_output"] or 0)}
                      for r in token_model_rows]
    token_by_user = [{"uid": r["uid"], "display_name": display_names.get(r["uid"]) or r["uid"],
                      "turns": r["turns"] or 0, "input": int(r["input"] or 0),
                      "cached_input": int(r["cached_input"] or 0), "output": int(r["output"] or 0),
                      "reasoning_output": int(r["reasoning_output"] or 0)}
                     for r in token_user_rows]
    token_daily = [{"date": str(r["date"]), "input": int(r["input"] or 0), "output": int(r["output"] or 0)}
                   for r in token_daily_rows]
    # S1: 用途別（kind）内訳。chat 行は token_by_model（messages.answer->'usage' 由来）から合成し、
    # usage_events 由来の行（intent/extract/propose/embed/graph_ask/vlm）と結合する。usage_events 側は
    # 全 NULL 合計（＝報告不能マーカーのみのグループ）をそのまま None として保持する（0 に丸めない）。
    # STAT-3 S2: chat 行（messages.answer->'usage' 由来）は elapsed_ms を持たない（別契約・T1の
    # 対象外）＝elapsed_n=0・total/avg は None のまま（「計測なし」と「計測して0だった」を区別）。
    token_by_kind = [{"kind": "chat", "provider": m["provider"], "model": m["model"], "calls": m["turns"],
                      "input": m["input"], "cached_input": m["cached_input"], "output": m["output"],
                      "reasoning_output": m["reasoning_output"],
                      "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0}
                     for m in token_by_model]
    token_by_kind += [{"kind": r["kind"], "provider": r["provider"] or "unknown", "model": r["model"] or "",
                       "calls": int(r["calls"] or 0),
                       "input": int(r["input"]) if r["input"] is not None else None,
                       "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
                       "output": int(r["output"]) if r["output"] is not None else None,
                       "reasoning_output": (int(r["reasoning_output"]) if r["reasoning_output"] is not None
                                            else None),
                       "elapsed_ms_total": (int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None
                                            else None),
                       "elapsed_ms_avg": (float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None
                                          else None),
                       "elapsed_n": int(r["elapsed_n"] or 0)}
                      for r in usage_event_rows]
    # ユーザー別 × 用途別（kind）内訳。token_by_kind と同じ合成（chat 行は token_by_user
    # と同じ材料から kind='chat' として合流・それ以外は usage_event_user_kind_rows 由来）——
    # user_id が NULL の行（取り込み時の埋め込み・画像読み取り等）を含まないため、同一 kind の
    # 合計は token_by_kind の当該行以下になりうる。
    token_by_user_kind = [{"uid": r["uid"], "display_name": display_names.get(r["uid"]) or r["uid"],
                          "kind": "chat", "calls": r["turns"] or 0, "input": int(r["input"] or 0),
                          "cached_input": int(r["cached_input"] or 0), "output": int(r["output"] or 0),
                          "reasoning_output": int(r["reasoning_output"] or 0),
                          "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0}
                         for r in token_user_rows]
    token_by_user_kind += [{"uid": r["uid"], "display_name": display_names.get(r["uid"]) or r["uid"],
                           "kind": r["kind"], "calls": int(r["calls"] or 0),
                           "input": int(r["input"]) if r["input"] is not None else None,
                           "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
                           "output": int(r["output"]) if r["output"] is not None else None,
                           "reasoning_output": (int(r["reasoning_output"]) if r["reasoning_output"] is not None
                                                else None),
                           "elapsed_ms_total": (int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None
                                                else None),
                           "elapsed_ms_avg": (float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None
                                              else None),
                           "elapsed_n": int(r["elapsed_n"] or 0)}
                          for r in usage_event_user_kind_rows]
    token_by_user_kind.sort(key=lambda row: (row["uid"], row["kind"]))
    tokens = {
        "totals": {
            "turns": sum(r["turns"] for r in token_by_model),
            "input": sum(r["input"] for r in token_by_model),
            "cached_input": sum(r["cached_input"] for r in token_by_model),
            "output": sum(r["output"] for r in token_by_model),
            "reasoning_output": sum(r["reasoning_output"] for r in token_by_model),
        },
        "by_model": token_by_model, "by_user": token_by_user, "daily": token_daily,
        "by_kind": token_by_kind, "by_user_kind": token_by_user_kind,
    }

    # 会話あたりの user ターン数分布（avg/median/max/p90）と resume_rate。
    conversation_turns, resume_rate = _compute_conversation_turn_stats(conversation_turn_rows)

    # 回答時間（duration_ms）の分布。全体＋経路（provider）別。
    all_durations: list[int] = []
    durations_by_provider: dict[str, list[int]] = {}
    for r in response_time_rows:
        d = int(r["duration_ms"])
        all_durations.append(d)
        durations_by_provider.setdefault(r["provider"] or "unknown", []).append(d)
    overall_response_time = _compute_response_time_stats(all_durations)
    overall_response_time["provider"] = None
    by_provider_response_time = []
    for p in sorted(durations_by_provider, key=lambda k: (-len(durations_by_provider[k]), k)):
        row = _compute_response_time_stats(durations_by_provider[p])
        row["provider"] = p
        by_provider_response_time.append(row)
    response_time = {"overall": overall_response_time, "by_provider": by_provider_response_time}

    # 会話ごとの補助 AI 使用量（`conversations_top`）。chat 行（conv_turn_rows）を土台に、
    # usage_events 由来の kind 行（conv_kind_rows・conv_turn_rows が返した会話 id に限定済み）を
    # 合流し、トークン合計（chat の input+output と usage_events の input+output の合算・
    # 報告不能＝None は 0 として加算＝並び順専用の内部値であり応答の各行 input/output はそのまま
    # None を保つ）の降順で上位20件へ切り詰める。
    conv_map: dict[int, dict] = {}
    conv_token_total: dict[int, int] = {}
    for r in conv_turn_rows:
        cid = r["cid"]
        chat_input = int(r["chat_input"] or 0)
        chat_output = int(r["chat_output"] or 0)
        kinds: list[dict] = []
        if (r["chat_calls"] or 0) > 0:
            kinds.append({
                "kind": "chat", "calls": int(r["chat_calls"] or 0),
                "input": chat_input, "cached_input": int(r["chat_cached_input"] or 0),
                "output": chat_output, "reasoning_output": int(r["chat_reasoning_output"] or 0),
                "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0,
            })
        conv_map[cid] = {
            "conversation_id": cid,
            "uid": r["uid"],
            "display_name": display_names.get(r["uid"]) or r["uid"],
            "world": r["world"],
            "user_turns": r["user_turns"] or 0,
            "kinds": kinds,
            "response_time_avg_ms": (float(r["avg_response_time_ms"])
                                     if r["avg_response_time_ms"] is not None else None),
        }
        conv_token_total[cid] = chat_input + chat_output
    for r in conv_kind_rows:
        entry = conv_map.get(r["cid"])
        if entry is None:
            continue   # conv_turn_rows に無い会話 id（安全側・実際には ANY(%s) 済みで起こらない）
        entry["kinds"].append({
            "kind": r["kind"], "calls": int(r["calls"] or 0),
            "input": int(r["input"]) if r["input"] is not None else None,
            "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
            "output": int(r["output"]) if r["output"] is not None else None,
            "reasoning_output": int(r["reasoning_output"]) if r["reasoning_output"] is not None else None,
            "elapsed_ms_total": int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None else None,
            "elapsed_ms_avg": float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None else None,
            "elapsed_n": int(r["elapsed_n"] or 0),
        })
        conv_token_total[r["cid"]] += (r["input"] or 0) + (r["output"] or 0)
    for entry in conv_map.values():
        entry["kinds"].sort(key=_kind_sort_key)
    conversations_top = sorted(
        conv_map.values(),
        key=lambda e: (-conv_token_total[e["conversation_id"]], e["conversation_id"]),
    )[:20]

    # 巡別記録（表示専用）の深さ×経路別集計＋理由コードの主張単位分布（巡別＝chat-round の
    # meta 由来／最終＝最終回答の data.claims 由来。二軸とも課金集計（tokens.*）とは独立＝
    # 正本に触れない）。
    rounds_stats = _compute_round_stats(round_rows)
    rounds_stats["reason_codes"] = _round_reason_codes(rounds_stats, final_claims_rows)
    _merge_final_missing_codes(rounds_stats, final_claims_rows)

    return {
        "users": users, "totals": totals, "daily": daily, "period": period,
        "zero_hit": zero_hit, "worlds": worlds_usage, "providers": providers_usage,
        "heatmap": heatmap, "retention": retention, "downloads": downloads, "tokens": tokens,
        "conversation_turns": conversation_turns, "resume_rate": resume_rate,
        "stop_kinds": stop_kinds, "stopped_turns": stopped_turns,
        "response_time": response_time, "limits": limits_stats,
        "conversations_top": conversations_top,
        "rounds": rounds_stats,
        "quality_runs": depth_quality_stats(days, time_from=time_from, time_to=time_to),
    }


# 品質採点の入口。1巡 vs 3巡等の正解付き比較は既存の実測枠の運用に委ねる——ここは採点結果の
# **集計済みカウント**だけを受け取って積む/読む（質問文・回答本文はテーブル自体が列を持たない）。
_QUALITY_COUNT_FIELDS = ("correct", "wrong_assertion", "missing", "regressed", "unrated")

# 採点した条件の閉集合（自由文にしない＝表記ゆれで集計が割れるのを防ぐ）。`main`＝見直しの無い
# AP、`depth2-*`＝見直しを持つ AP の深さ別（`depth2-quick` が見直し 0 巡・`depth2-standard` は
# 見直し 2 巡）。
QUALITY_RUN_CONDITIONS = ("main", "depth2-quick", "depth2-standard", "depth2-deep", "depth2-max")


def record_depth_quality_run(rounds, counts: dict | None, *, condition: str,
                             executed_from: str, executed_to: str,
                             cost_usd: float | None = None,
                             ts=None, run_id: str | None = None,
                             audit_actor: str | None = None) -> bool:
    """1採点ラン分の集計済みカウントを1行 INSERT する。

    `rounds`: 比較した巡数（0以上——見直しを一度も回さない条件（`main`・`depth2-quick`）は0）。
    `counts`: `_QUALITY_COUNT_FIELDS` の一部/全部（欠落キーは0・非負整数以外は0に丸める＝壊れた
    入力で例外にしない）。`condition`: `QUALITY_RUN_CONDITIONS` のいずれか（閉集合外は
    `UsagePeriodError` ではなく `ValueError`）。`executed_from`/`executed_to`: 質問セットを実行した
    期間（ISO 8601・オフセット必須・`[from, to)`）——集計はこの実行期間で照会する（登録時刻 `ts`
    ではない＝後日登録しても元の実行期間で取れる）。`cost_usd`: 任意（費用集計・inf/nan・負値・
    非数値は登録前に None へ丸める＝以後の集計 SUM が壊れないための防御）。`ts` はテスト用
    （省略時は DB の `now()`）。

    `run_id`（省略可）: 呼び出し側が指定する冪等キー。`run_id IS NOT NULL` の部分ユニーク索引
    （`depth_quality_runs.run_id`）により、同じ `run_id` の再送は2行目を作らない
    （`POST /admin/usage/quality-runs` の監査書込み失敗後の再送で二重計上しないための対策・
    `run_id` 省略時（None）は従来どおり毎回新規行）。

    `audit_actor`（省略可）: 指定すると、この INSERT と**同一トランザクション**で監査ログ
    （`admin.usage_quality_run_recorded`）も書く（`_facade._audit_insert`・
    `store/settings.py::set_system_settings` と同じ「登録と監査を割り離さない」流儀）。監査の
    INSERT が失敗すれば例外が送出され、`depth_quality_runs` 側の INSERT もロールバックされる
    （fail-closed：監査に残せない記録を残さない）。`run_id` が重複でスキップされた場合は
    監査ログも書かない（実際には何も起きていないため）。

    戻り値: 実際に新規行を作ったら True、`run_id` 重複でスキップしたら False。
    """
    _ensure()
    if condition not in QUALITY_RUN_CONDITIONS:
        raise ValueError(f"condition は {'/'.join(QUALITY_RUN_CONDITIONS)} のいずれかで指定してください")
    exec_from = _parse_period_bound(executed_from, "executed_from")
    exec_to = _parse_period_bound(executed_to, "executed_to")
    if exec_from >= exec_to:
        raise UsagePeriodError("executed_from は executed_to より前の日時で指定してください")
    if exec_to - exec_from > timedelta(days=_USAGE_PERIOD_MAX_DAYS):
        # 照会期間の上限（365日）を超える実行期間は、どの照会からも包含条件を満たせず永久に
        # 集計へ現れない＝登録させない。
        raise UsagePeriodError(f"実行期間は最大 {_USAGE_PERIOD_MAX_DAYS} 日です")
    rounds = int(rounds)
    if rounds < 0:
        raise ValueError("rounds は0以上で指定してください")
    counts = counts if isinstance(counts, dict) else {}
    vals = {}
    for f in _QUALITY_COUNT_FIELDS:
        v = counts.get(f, 0)
        vals[f] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0
    cost_usd = (float(cost_usd)
                if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool)
                and math.isfinite(cost_usd) and cost_usd >= 0 else None)
    cols = ["rounds", "run_id", "condition", "executed_from", "executed_to",
            "correct", "wrong_assertion", "missing", "regressed", "unrated", "cost_usd"]
    params = [rounds, run_id, condition, exec_from, exec_to,
              vals["correct"], vals["wrong_assertion"], vals["missing"],
              vals["regressed"], vals["unrated"], cost_usd]
    if ts is not None:
        cols.insert(0, "ts")
        params.insert(0, ts)
    placeholders = ",".join(["%s"] * len(cols))
    from sherpa import store as _facade   # settings.py と同じ実行時解決（monkeypatch シーム維持）
    with _connect() as c:
        row = c.execute(
            f"INSERT INTO depth_quality_runs ({','.join(cols)}) VALUES ({placeholders}) "
            "ON CONFLICT (run_id) WHERE run_id IS NOT NULL DO NOTHING RETURNING id",
            params,
        ).fetchone()
        inserted = row is not None
        if inserted and audit_actor is not None:
            _facade._audit_insert(
                c, audit_actor, "admin.usage_quality_run_recorded", "usage", None,
                detail={"rounds": rounds, "condition": condition}, outcome="success", severity="info")
        return inserted


def depth_quality_stats(days: int = 180, *, time_from: str | None = None,
                        time_to: str | None = None) -> dict:
    """`record_depth_quality_run` が積んだ集計済みカウントを条件×巡数別に合算する
    （既定180日＝他の利用統計より広め・正解付き比較は実施頻度が低い運用のため）。

    母集団は「**実行期間**（`executed_from`/`executed_to`）が照会期間に完全に含まれる採点ラン」
    ＝`from <= executed_from AND executed_to <= to`（実行期間・照会期間とも半開区間なので、
    実行の終端が照会の上限ちょうど（`executed_to == to`）のランは含む）——登録時刻（`ts`）では
    絞らない。採点は実行より後に登録されるのが普通で、登録時刻で絞ると
    「先週流した質問セットの結果」を先週の期間で読めなくなる。実行期間を持たない行（入口を
    通らずに入った過去データ）は母集団に入らない。
    """
    _ensure()
    start_ts, end_exclusive_ts, period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        rows = c.execute(
            "SELECT condition, rounds, COUNT(*) AS runs, "
            + ", ".join(f"SUM({f}) AS {f}" for f in _QUALITY_COUNT_FIELDS) + ", "
            "  SUM(cost_usd) AS cost_usd_total "
            "FROM depth_quality_runs "
            "WHERE executed_from >= %s AND executed_to <= %s "
            "GROUP BY condition, rounds ORDER BY condition, rounds",
            (start_ts, end_exclusive_ts),
        ).fetchall()
    by_rounds = [
        {"condition": r["condition"], "rounds": r["rounds"], "runs": r["runs"] or 0,
         **{f: int(r[f] or 0) for f in _QUALITY_COUNT_FIELDS},
         "cost_usd_total": float(r["cost_usd_total"]) if r["cost_usd_total"] is not None else None}
        for r in rows
    ]
    return {"period": period, "by_rounds": by_rounds}


# ===================================================================================
# docs/proposals/2026-09-12-利用統計の拡充2.md §3b: 利用統計チャットの調査ツールが
# 使う、絞り込み付きの集計関数。不変条件は本モジュール冒頭と同じ（本文・会話タイトルは一切
# SELECT しない）ことに加え、display_name も返さない（ツールの戻り値は件数・時刻・種別・トークン・
# 所要時間・会話 id・uid・world のみという契約——`usage_stats()` 自体の戻り値は画面表示用のため
# display_name を含めたまま変えない）。
#
# 引数の妥当性判定（不正な metric/sort・負の days 等を error 辞書にする）は呼び出し元
# （`agentic_search.run_tool`）の責務——ここでは常に何かしらの値を返せるよう防御的にクランプする
# （他の将来の呼び出し元にも安全な既定を提供する二重防御）。
# ===================================================================================

_TOOL_LIMIT_UPPER = 50   # 裁定（2026-09-12）: 返却上限は件数系の全ツールで50件。


def _clamp_days(days) -> int:
    """1〜365 日にクランプ（型不正・欠落は既定30日）。"""
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 30
    return max(1, min(d, 365))


def _clamp_limit(limit, default: int = 20, upper: int = _TOOL_LIMIT_UPPER) -> int:
    """1〜upper 件にクランプ（型不正・欠落は既定 `default` 件）。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, upper))


def _norm_str(value) -> str | None:
    """絞り込み引数（uid/kind/provider）の正規化: 空文字/空白のみ/None は「絞り込みなし」。"""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _tool_json_projection(value):
    """usage 系調査ツールの戻り値を JSON ネイティブ型だけに畳む射影（`json.dumps` に `default` を
    渡さずに直列化できることを保証する）。dict は再帰しつつ `display_name` キーを落とし、list は
    要素ごとに再帰、`datetime`/`date` は isoformat 文字列へ、`Decimal` は float へ変換し、それ以外は
    そのまま返す。`usage_stats()` 自体（画面表示用）は display_name を含めたまま変えず、
    ツール専用のこの射影に一本化する（`usage_overview` に限らず本モジュールの全ツール関数が使う）。
    """
    if isinstance(value, dict):
        return {k: _tool_json_projection(v) for k, v in value.items() if k != "display_name"}
    if isinstance(value, list):
        return [_tool_json_projection(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def usage_overview(days: int = 30, *, time_from: str | None = None,
                   time_to: str | None = None) -> dict:
    """`usage_stats(days)` の質問応答向け射影（`usage_chat._stats_projection` と概ね同じ形だが、
    ツールの戻り値契約により display_name は含めない）。内訳リストは上位 `_TOOL_LIMIT_UPPER` 件。

    期間指定（`days` か `time_from`/`time_to`）はそのまま `usage_stats` へ委譲する。
    """
    if time_from is None and time_to is None:
        days = _clamp_days(days)
    stats = usage_stats(days, time_from=time_from, time_to=time_to)
    tokens = stats.get("tokens") or {}
    limit = _TOOL_LIMIT_UPPER
    return _tool_json_projection({
        "period": stats.get("period"), "totals": stats.get("totals"), "zero_hit": stats.get("zero_hit"),
        "quality_runs": stats.get("quality_runs"),
        "worlds": stats.get("worlds"), "providers": stats.get("providers"),
        "retention": stats.get("retention"), "downloads": stats.get("downloads"),
        "daily": stats.get("daily"), "stop_kinds": stats.get("stop_kinds"),
        "stopped_turns": stats.get("stopped_turns"), "conversation_turns": stats.get("conversation_turns"),
        "resume_rate": stats.get("resume_rate"), "response_time": stats.get("response_time"),
        "users": (stats.get("users") or [])[:limit],
        "tokens": {
            "totals": tokens.get("totals"), "daily": tokens.get("daily"), "by_kind": tokens.get("by_kind"),
            "by_model": (tokens.get("by_model") or [])[:limit],
            "by_user": (tokens.get("by_user") or [])[:limit],
            "by_user_kind": sorted(tokens.get("by_user_kind") or [],
                                  key=lambda r: -((r.get("input") or 0) + (r.get("output") or 0)))[:limit],
        },
        "conversations_top": [
            {"conversation_id": conv.get("conversation_id"), "uid": conv.get("uid"),
             "world": conv.get("world"), "user_turns": conv.get("user_turns"),
             "response_time_avg_ms": conv.get("response_time_avg_ms"),
             "kinds": [{"kind": k.get("kind"), "calls": k.get("calls"),
                       "input": k.get("input"), "output": k.get("output")}
                      for k in (conv.get("kinds") or [])]}
            for conv in (stats.get("conversations_top") or [])[:limit]
        ],
    })


def usage_by_user(days: int = 7, uid: str | None = None, kind: str | None = None, *,
                  time_from: str | None = None, time_to: str | None = None) -> dict:
    """ユーザー別 × 用途別（kind）の calls/tokens/所要時間（期間・uid・kind 絞り込み付き）。
    `tokens.by_user_kind`（U1）と同じ材料（chat は `messages.answer->'usage'`・他は
    `usage_events`）を、期間・利用者・用途で絞り込んで返す。display_name は含めない。

    期間は `days` か `time_from`/`time_to`（`_usage_period` の規則・`usage_stats` と同じ）。
    """
    _ensure()
    if time_from is None and time_to is None:
        days = _clamp_days(days)
    uid = _norm_str(uid)
    kind = _norm_str(kind)
    start_ts, end_exclusive_ts, period = _usage_period(days, time_from=time_from, time_to=time_to)
    rows: list[dict] = []
    with _connect() as c:
        if kind is None or kind == "chat":
            chat_sql = (
                _USAGE_TURN_CTE + " SELECT user_id AS uid, " + _usage_token_sum_cols() +
                " FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s" + _USAGE_TOKEN_WHERE
            )
            chat_params = [start_ts, end_exclusive_ts, start_ts, end_exclusive_ts]
            if uid:
                chat_sql += " AND user_id = %s"
                chat_params.append(uid)
            chat_sql += " GROUP BY user_id"
            for r in c.execute(chat_sql, chat_params).fetchall():
                rows.append({"uid": r["uid"], "kind": "chat", "calls": r["turns"] or 0,
                            "input": int(r["input"] or 0), "cached_input": int(r["cached_input"] or 0),
                            "output": int(r["output"] or 0),
                            "reasoning_output": int(r["reasoning_output"] or 0),
                            "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0})
        if kind != "chat":
            # `chat-round`（巡別記録＝表示用）は正本と二重に足さないため除く（`usage_stats` と同じ）。
            ev_sql = (
                "SELECT user_id AS uid, kind, SUM(calls) AS calls, "
                "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
                "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output, "
                "  SUM(elapsed_ms) AS elapsed_ms_total, AVG(elapsed_ms) AS elapsed_ms_avg, "
                "  COUNT(elapsed_ms) AS elapsed_n "
                "FROM usage_events WHERE ts >= %s AND ts < %s AND user_id IS NOT NULL "
                "  AND kind <> 'chat-round'"
            )
            ev_params = [start_ts, end_exclusive_ts]
            if uid:
                ev_sql += " AND user_id = %s"
                ev_params.append(uid)
            if kind:
                ev_sql += " AND kind = %s"
                ev_params.append(kind)
            ev_sql += " GROUP BY user_id, kind"
            for r in c.execute(ev_sql, ev_params).fetchall():
                rows.append({
                    "uid": r["uid"], "kind": r["kind"], "calls": int(r["calls"] or 0),
                    "input": int(r["input"]) if r["input"] is not None else None,
                    "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
                    "output": int(r["output"]) if r["output"] is not None else None,
                    "reasoning_output": (int(r["reasoning_output"]) if r["reasoning_output"] is not None
                                        else None),
                    "elapsed_ms_total": (int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None
                                        else None),
                    "elapsed_ms_avg": (float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None
                                      else None),
                    "elapsed_n": int(r["elapsed_n"] or 0),
                })
    # 利用量（input+output）の多い順＝予算内への先頭切り（agentic_search の間引き）で重い利用者が残る
    rows.sort(key=lambda r: (-((r.get("input") or 0) + (r.get("output") or 0)), r["uid"] or "", r["kind"]))
    return _tool_json_projection({
        "period": period,
        "uid": uid, "kind": kind, "rows": rows})


def usage_conversations(days: int = 30, uid: str | None = None, limit: int = 20,
                        sort: str = "tokens", *, time_from: str | None = None,
                        time_to: str | None = None) -> dict:
    """会話別の上位表（`conversations_top`＝U2 と同じ材料）を期間・uid で絞り込み、
    並び順（tokens/turns/elapsed）と件数上限を選べる形にしたもの。タイトル・本文は含めない。

    期間は `days` か `time_from`/`time_to`（`_usage_period` の規則・`usage_stats` と同じ）。
    """
    _ensure()
    if time_from is None and time_to is None:
        days = _clamp_days(days)
    limit = _clamp_limit(limit, default=20)
    uid = _norm_str(uid)
    sort = sort if sort in ("tokens", "turns", "elapsed") else "tokens"
    start_ts, end_exclusive_ts, period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        conv_sql = (
            _USAGE_TURN_CTE + " "
            "SELECT conversation_id AS cid, user_id AS uid, version AS world, "
            "  COUNT(*) AS user_turns, "
            "  COUNT(*) FILTER (WHERE jsonb_typeof(answer->'usage')='object') AS chat_calls, "
            f"  SUM({_usage_tok('input_tokens')}) AS chat_input, "
            f"  SUM({_usage_tok('cached_input_tokens')}) AS chat_cached_input, "
            f"  SUM({_usage_tok('output_tokens')}) AS chat_output, "
            f"  SUM({_usage_tok('reasoning_output_tokens')}) AS chat_reasoning_output, "
            "  AVG(CASE WHEN lens IS DISTINCT FROM 'clarify' AND answer->>'duration_ms' ~ '^[0-9]+$' "
            "    THEN (answer->>'duration_ms')::bigint END) AS avg_response_time_ms "
            "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s"
        )
        conv_params = [start_ts, end_exclusive_ts, start_ts, end_exclusive_ts]
        if uid:
            conv_sql += " AND user_id = %s"
            conv_params.append(uid)
        conv_sql += " GROUP BY conversation_id, user_id, version"
        conv_turn_rows = c.execute(conv_sql, conv_params).fetchall()
        _cids = [r["cid"] for r in conv_turn_rows]
        conv_kind_rows = (
            c.execute(
                "SELECT conversation_id AS cid, kind, SUM(calls) AS calls, "
                "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
                "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output, "
                "  SUM(elapsed_ms) AS elapsed_ms_total, AVG(elapsed_ms) AS elapsed_ms_avg, "
                "  COUNT(elapsed_ms) AS elapsed_n "
                "FROM usage_events WHERE ts >= %s AND ts < %s AND conversation_id = ANY(%s) "
                "  AND kind <> 'chat-round' "
                "GROUP BY conversation_id, kind",
                (start_ts, end_exclusive_ts, _cids),
            ).fetchall()
            if _cids else []
        )
    conv_map: dict[int, dict] = {}
    sort_key: dict[int, dict] = {}
    for r in conv_turn_rows:
        cid = r["cid"]
        chat_input = int(r["chat_input"] or 0)
        chat_output = int(r["chat_output"] or 0)
        kinds: list[dict] = []
        if (r["chat_calls"] or 0) > 0:
            kinds.append({"kind": "chat", "calls": int(r["chat_calls"] or 0), "input": chat_input,
                         "cached_input": int(r["chat_cached_input"] or 0), "output": chat_output,
                         "reasoning_output": int(r["chat_reasoning_output"] or 0),
                         "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0})
        conv_map[cid] = {
            "conversation_id": cid, "uid": r["uid"], "world": r["world"],
            "user_turns": r["user_turns"] or 0, "kinds": kinds,
            "response_time_avg_ms": (float(r["avg_response_time_ms"])
                                    if r["avg_response_time_ms"] is not None else None),
        }
        sort_key[cid] = {"tokens": chat_input + chat_output, "turns": r["user_turns"] or 0, "elapsed": 0}
    for r in conv_kind_rows:
        entry = conv_map.get(r["cid"])
        if entry is None:
            continue   # conv_sql に無い会話 id（安全側・ANY(%s) 済みで実際には起こらない）
        entry["kinds"].append({
            "kind": r["kind"], "calls": int(r["calls"] or 0),
            "input": int(r["input"]) if r["input"] is not None else None,
            "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
            "output": int(r["output"]) if r["output"] is not None else None,
            "reasoning_output": int(r["reasoning_output"]) if r["reasoning_output"] is not None else None,
            "elapsed_ms_total": int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None else None,
            "elapsed_ms_avg": float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None else None,
            "elapsed_n": int(r["elapsed_n"] or 0),
        })
        sk = sort_key[r["cid"]]
        sk["tokens"] += (r["input"] or 0) + (r["output"] or 0)
        sk["elapsed"] += int(r["elapsed_ms_total"] or 0)
    for entry in conv_map.values():
        entry["kinds"].sort(key=_kind_sort_key)
    ordered = sorted(conv_map.values(),
                     key=lambda e: (-sort_key[e["conversation_id"]][sort], e["conversation_id"]))[:limit]
    return _tool_json_projection({
        "period": period,
        "uid": uid, "sort": sort, "conversations": ordered})


# 会話1件の内訳（`usage_conversation_detail`）専用の turn 対応付け CTE。`_USAGE_TURN_CTE` と同じ
# 「各 user ターンの直後に来る最初の assistant 応答」だけをペアリングする規則だが、1会話に scope する
# ため「期間内に触れた会話」への絞り込み（`touched`）は不要。
_CONV_DETAIL_TURN_CTE = (
    "WITH numbered AS ("
    "  SELECT m.id, m.role, m.lens, m.answer, "
    "    SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) "
    "      OVER (ORDER BY m.id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS turn_no "
    "  FROM messages m WHERE m.conversation_id = %s"
    "), assistant_replies AS ("
    "  SELECT DISTINCT ON (turn_no) turn_no, lens, answer "
    "  FROM numbered WHERE role='assistant' AND turn_no > 0 "
    "  ORDER BY turn_no, id"
    "), turns AS ("
    "  SELECT n.turn_no, ar.lens, ar.answer "
    "  FROM numbered n LEFT JOIN assistant_replies ar ON ar.turn_no = n.turn_no "
    "  WHERE n.role='user'"
    ")"
)


def usage_conversation_detail(conversation_id) -> dict:
    """1会話の内訳: user ターン数・用途別（kind）の calls/tokens・回答時間の系列（ターン番号・
    duration_ms・provider のみ）。本文・タイトルは一切含めない。存在しない/対象外（削除済み・
    共有受領）の会話 id は本文/タイトルのキーを一切持たない error 辞書を返す。
    """
    _ensure()
    try:
        cid = int(conversation_id)
    except (TypeError, ValueError):
        return {"error": "conversation_id は整数で指定してください"}
    with _connect() as c:
        conv_row = c.execute(
            "SELECT id FROM conversations WHERE id = %s AND deleted_at IS NULL AND origin='own'",
            (cid,),
        ).fetchone()
        if not conv_row:
            return {"error": "指定した会話が見つかりません"}
        summary_row = c.execute(
            _CONV_DETAIL_TURN_CTE + " "
            "SELECT COUNT(*) AS user_turns, "
            "  COUNT(*) FILTER (WHERE jsonb_typeof(answer->'usage')='object') AS chat_calls, "
            f"  SUM({_usage_tok('input_tokens')}) AS chat_input, "
            f"  SUM({_usage_tok('cached_input_tokens')}) AS chat_cached_input, "
            f"  SUM({_usage_tok('output_tokens')}) AS chat_output, "
            f"  SUM({_usage_tok('reasoning_output_tokens')}) AS chat_reasoning_output "
            "FROM turns",
            (cid,),
        ).fetchone()
        response_rows = c.execute(
            _CONV_DETAIL_TURN_CTE + " "
            "SELECT turn_no, (answer->>'duration_ms')::bigint AS duration_ms, "
            "  answer->'usage'->>'provider' AS provider "
            "FROM turns WHERE lens IS DISTINCT FROM 'clarify' "
            "  AND answer->>'duration_ms' ~ '^[0-9]+$' ORDER BY turn_no",
            (cid,),
        ).fetchall()
        # `chat-round`（巡別記録＝表示用）は正本と二重に足さないため除く（`usage_stats` と同じ）。
        kind_rows = c.execute(
            "SELECT kind, SUM(calls) AS calls, SUM(input_tokens) AS input, "
            "  SUM(cached_input_tokens) AS cached_input, SUM(output_tokens) AS output, "
            "  SUM(reasoning_output_tokens) AS reasoning_output, SUM(elapsed_ms) AS elapsed_ms_total, "
            "  AVG(elapsed_ms) AS elapsed_ms_avg, COUNT(elapsed_ms) AS elapsed_n "
            "FROM usage_events WHERE conversation_id = %s AND kind <> 'chat-round' "
            "GROUP BY kind ORDER BY kind",
            (cid,),
        ).fetchall()
    kinds: list[dict] = []
    chat_calls = summary_row["chat_calls"] or 0
    if chat_calls > 0:
        kinds.append({"kind": "chat", "calls": int(chat_calls),
                      "input": int(summary_row["chat_input"] or 0),
                      "cached_input": int(summary_row["chat_cached_input"] or 0),
                      "output": int(summary_row["chat_output"] or 0),
                      "reasoning_output": int(summary_row["chat_reasoning_output"] or 0),
                      "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0})
    for r in kind_rows:
        kinds.append({
            "kind": r["kind"], "calls": int(r["calls"] or 0),
            "input": int(r["input"]) if r["input"] is not None else None,
            "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
            "output": int(r["output"]) if r["output"] is not None else None,
            "reasoning_output": int(r["reasoning_output"]) if r["reasoning_output"] is not None else None,
            "elapsed_ms_total": int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None else None,
            "elapsed_ms_avg": float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None else None,
            "elapsed_n": int(r["elapsed_n"] or 0),
        })
    kinds.sort(key=_kind_sort_key)
    response_time_series = [{"turn": r["turn_no"], "duration_ms": int(r["duration_ms"]),
                             "provider": r["provider"] or "unknown"} for r in response_rows]
    return _tool_json_projection({"conversation_id": cid, "user_turns": summary_row["user_turns"] or 0,
                                  "kinds": kinds, "response_time_series": response_time_series})


def usage_response_time(days: int = 30, provider: str | None = None) -> dict:
    """回答時間（duration_ms）の分布（全体、または指定 provider に絞った avg/median/p90/max・件数）。
    期間・provider 絞り込み付き（`response_time`＝U1 と同じ材料）。
    """
    _ensure()
    days = _clamp_days(days)
    provider = _norm_str(provider)
    start_ts, start_date, end_date, end_exclusive_ts = _usage_period_bounds(days)
    with _connect() as c:
        sql = (
            "SELECT (answer->>'duration_ms')::bigint AS duration_ms "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.created_at >= %s AND m.created_at < %s AND m.role='assistant' "
            "  AND c.deleted_at IS NULL AND c.origin='own' "
            "  AND m.lens IS DISTINCT FROM 'clarify' "
            "  AND answer->>'duration_ms' ~ '^[0-9]+$'"
        )
        params = [start_ts, end_exclusive_ts]
        if provider:
            sql += " AND coalesce(answer->'usage'->>'provider', 'unknown') = %s"
            params.append(provider)
        rows = c.execute(sql, params).fetchall()
    durations = [int(r["duration_ms"]) for r in rows]
    stats = _compute_response_time_stats(durations)
    stats["provider"] = provider
    return _tool_json_projection(
        {"period": {"start": start_date.isoformat(), "end": end_date.isoformat(), "days": days}, **stats})


def usage_daily(days: int = 30, metric: str = "turns") -> dict:
    """日別の系列（turns/tokens/response_time のいずれか）。"""
    _ensure()
    days = _clamp_days(days)
    metric = metric if metric in ("turns", "tokens", "response_time") else "turns"
    start_ts, start_date, end_date, end_exclusive_ts = _usage_period_bounds(days)
    with _connect() as c:
        if metric == "turns":
            rows = c.execute(
                "SELECT (m.created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, COUNT(*) AS n "
                "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE m.created_at >= %s AND m.created_at < %s AND c.deleted_at IS NULL "
                "  AND m.role='user' AND c.origin='own' "
                "GROUP BY date ORDER BY date",
                (start_ts, end_exclusive_ts),
            ).fetchall()
            series = [{"date": str(r["date"]), "value": r["n"] or 0} for r in rows]
        elif metric == "tokens":
            rows = c.execute(
                _USAGE_TURN_CTE + " "
                "SELECT (turn_created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, "
                f"SUM({_usage_tok('input_tokens')}) AS input, SUM({_usage_tok('output_tokens')}) AS output "
                "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s" + _USAGE_TOKEN_WHERE +
                "GROUP BY date ORDER BY date",
                (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
            ).fetchall()
            series = [{"date": str(r["date"]), "input": int(r["input"] or 0), "output": int(r["output"] or 0)}
                     for r in rows]
        else:   # response_time
            rows = c.execute(
                "SELECT (m.created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, "
                "  AVG((answer->>'duration_ms')::bigint) AS avg_ms, COUNT(*) AS n "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.created_at >= %s AND m.created_at < %s AND m.role='assistant' "
                "  AND c.deleted_at IS NULL AND c.origin='own' "
                "  AND m.lens IS DISTINCT FROM 'clarify' AND answer->>'duration_ms' ~ '^[0-9]+$' "
                "GROUP BY date ORDER BY date",
                (start_ts, end_exclusive_ts),
            ).fetchall()
            series = [{"date": str(r["date"]),
                      "avg_ms": float(r["avg_ms"]) if r["avg_ms"] is not None else None,
                      "n": r["n"] or 0} for r in rows]
    return _tool_json_projection({
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat(), "days": days},
        "metric": metric, "series": series})


def usage_stop_kinds(days: int = 30, uid: str | None = None, *, time_from: str | None = None,
                     time_to: str | None = None) -> dict:
    """終了理由の分布＋利用者停止件数（期間・uid 絞り込み付き・`stop_kinds`＝U1 と同じ材料）。

    期間は `days` か `time_from`/`time_to`（`_usage_period` の規則・`usage_stats` と同じ）。
    """
    _ensure()
    if time_from is None and time_to is None:
        days = _clamp_days(days)
    uid = _norm_str(uid)
    start_ts, end_exclusive_ts, period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        sql = (
            _USAGE_TURN_CTE + " "
            "SELECT CASE WHEN answer->>'stop_kind' = ANY(%s) THEN answer->>'stop_kind' "
            "  ELSE 'unknown' END AS stop_kind, COUNT(*) AS n "
            "FROM turns "
            "WHERE turn_created_at >= %s AND turn_created_at < %s "
            "  AND answer IS NOT NULL AND lens IS DISTINCT FROM 'clarify' "
            "  AND answer->>'stop_kind' IS DISTINCT FROM 'stopped_by_user'"
        )
        params = [start_ts, end_exclusive_ts, list(stop_kind.STOP_KINDS), start_ts, end_exclusive_ts]
        if uid:
            sql += " AND user_id = %s"
            params.append(uid)
        sql += " GROUP BY stop_kind ORDER BY n DESC"
        rows = c.execute(sql, params).fetchall()
        stopped_sql = _stopped_turns_sql()
        stopped_params = [start_ts, end_exclusive_ts]
        if uid:
            stopped_sql += " AND c.user_id = %s"
            stopped_params.append(uid)
        stopped_row = c.execute(stopped_sql, stopped_params).fetchone()
    return _tool_json_projection({
        "period": period,
        "uid": uid, "stop_kinds": [{"stop_kind": r["stop_kind"], "turns": r["n"] or 0} for r in rows],
        "stopped_turns": (stopped_row["n"] or 0) if stopped_row else 0})


def usage_depth_rounds(days: int = 30, *, time_from: str | None = None,
                       time_to: str | None = None) -> dict:
    """深さ×経路別の巡数分布・不明理由コードの分布・品質採点の条件別件数
    （`usage_stats()` の `rounds`/`quality_runs` と同じ材料。本関数での品質採点のキーは
    `quality`＝`quality.by_rounds`）。

    期間は `days`（既定30日）または `time_from`/`time_to`（ISO 8601・オフセット必須・`[from, to)`）
    ——`usage_stats` と同じ規則・同じ集計なので、同じ期間を指定すれば両者の値は一致する。
    境界の基準は指標ごとに違う（`usage_stats` の docstring 参照）: 巡集計は**所属する user 発言の
    `created_at`**、品質採点は**実行期間の完全包含**。

    本文（質問/回答/資料名）は一切含まない——他の usage_* ツールと同じ不変条件
    （`round_distribution`/`reason_codes`/`quality_runs` はいずれも件数・ラベルのみ）。
    """
    _ensure()
    if time_from is None and time_to is None:
        days = _clamp_days(days)
    start_ts, end_exclusive_ts, period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        round_rows = _round_rows_query(c, start_ts, end_exclusive_ts)
        final_claims_rows = _final_claims_rows_query(c, start_ts, end_exclusive_ts)
    rounds_stats = _compute_round_stats(round_rows)
    return _tool_json_projection({
        "period": period,
        "round_distribution": rounds_stats["round_distribution"],
        "unmatched_rounds": rounds_stats["unmatched_rounds"],
        "reason_codes": _round_reason_codes(rounds_stats, final_claims_rows),
        "quality": depth_quality_stats(days, time_from=time_from, time_to=time_to),
    })
