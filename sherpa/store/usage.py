"""利用統計（admin 専用）の集計。メッセージ本文・会話タイトルは SELECT しない（件数・日時・種別のみ）。
設計: docs/design/usage.md「管理画面が読む `GET /admin/usage/stats`」
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .. import answer_shape, stop_kind
from .db import _connect, _ensure

_USAGE_AUDIT_ACTIONS = ("auth.login", "document.downloaded", "workspace.file_uploaded", "share.created")
_JST = timezone(timedelta(hours=9))

# chat.turn 監査の detail.provider を集計側でも allowlist で畳み込む。store は他の sherpa.* を import しないため `sherpa.agents.AGENT_PROVIDERS` の値をここに複製している（変更時は合わせて確認する）。
_USAGE_KNOWN_PROVIDERS = ("heuristic", "codex", "openai", "gemini", "bedrock", "ollama", "simple")


def _usage_period_bounds(days: int):
    """JST の「(今日 − (days−1)) の日初」〜「明日の日初（排他的上限）」を期間として返す。
    daily・users・totals・audit 由来の全集計が同じ境界を使い、期間は半開区間 `[start_ts, end_exclusive_ts)` に固定する。アプリサーバの UTC 時刻から計算するため DB セッションの timezone に依存しない。
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


# `from`/`to` で指定できる期間の上限（日数・API の `days` と同じ365日）。
_USAGE_PERIOD_MAX_DAYS = 365


def _parse_period_bound(value, field: str) -> datetime:
    """ISO 8601 の日時文字列を解釈する。UTC オフセット必須（`+09:00`・`Z` 等・オフセット無しは JST か UTC か決められない）。"""
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
    既定は `days`（JST 暦日・`_usage_period_bounds` と同じ境界）。`time_from`/`time_to`（ISO 8601・オフセット必須）を渡すと `[from, to)` をそのまま境界に使う。
    - `from`/`to` は両方必須（片方だけはエラー）。
    - 区間は半開 `[from, to)` で `from` < `to`。
    - 期間の長さは最大 `_USAGE_PERIOD_MAX_DAYS` 日。
    `days` との排他は呼び出し側が判定する。`period` は JST 暦日の `start`/`end`/`days`（`end` は含む終了日）を保ち、実際に使った半開区間（`from`/`to`・ISO 8601・オフセット付き）を必ず加える。
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
    # `to` は排他的上限のため、暦日表示の終端は区間に含まれる最後の瞬間の JST 暦日。
    end_date = (end_exclusive_ts - timedelta(microseconds=1)).astimezone(_JST).date()
    return start_ts, end_exclusive_ts, {
        "start": start_date.isoformat(), "end": end_date.isoformat(),
        "days": (end_date - start_date).days + 1,
        "from": start_ts.isoformat(), "to": end_exclusive_ts.isoformat()}


# `messages.answer` JSON から直接ターンを対応付ける CTE。本番の集計は `turn_metrics` を読む `_build_usage_turns` を使い、`_USAGE_TURN_CTE`・`_usage_tok`・`_usage_token_sum_cols`・`_USAGE_TOKEN_WHERE` はテストが集計結果を照合する基準としてだけ使う。
# 対応付け: conversation 内で各 user メッセージの直後に来る最初の assistant メッセージだけをその user ターンの返答として数える。turn_no は user メッセージの累積カウントで、user 行と直後の assistant 行が同じ turn_no を持つことでペアリングする。`answer`（JSONB）もペアリングして持ち回る。
# `numbered` の基点スキャンは、期間内にメッセージを1件でも持つ会話 `touched`（DISTINCT conversation_id）への明示 JOIN に絞る（呼び出し側は `(start_ts, end_exclusive_ts)` をこの CTE 用にも渡す）。
# 絞りは会話単位で行う（行単位で `created_at` で間引くと id 順と created_at が逆転する並びで turn_no がずれ、別ターンの assistant が誤結合する）。
# `message_id`/`message_created_at`（assistant 側の messages.id/created_at）は利用明細エクスポート（`usage_export_turns`）が回答番号として使う。
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
    "  SELECT DISTINCT ON (conversation_id, turn_no) conversation_id, turn_no, lens, answer, "
    "    id AS message_id, created_at AS message_created_at "
    "  FROM numbered WHERE role='assistant' AND turn_no > 0 "
    "  ORDER BY conversation_id, turn_no, id "
    "), turns AS ("
    "  SELECT n.user_id, n.version, n.conversation_id, n.created_at AS turn_created_at, "
    "    n.personal AS user_personal, ar.lens, ar.answer, ar.message_id, ar.message_created_at "
    "  FROM numbered n LEFT JOIN assistant_replies ar "
    "    ON ar.conversation_id = n.conversation_id AND ar.turn_no = n.turn_no "
    "  WHERE n.role='user' "
    ")"
)


# messages.answer->'usage' からトークン使用量を集計する SQL 断片。answer->'usage' は `{provider, model, input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}`。想定外データ（非数値・欠落）は 0 に畳む。フィールド名はコード内リテラルのため f-string 埋め込みは安全。
def _usage_tok(field: str) -> str:
    return (f"CASE WHEN (answer->'usage'->>'{field}') ~ '^[0-9]+$' "
            f"THEN (answer->'usage'->>'{field}')::bigint ELSE 0 END")


# 会話の `kinds`（用途別内訳）の並び順: input+output（報告不能=None は 0）の降順・同値は kind 名。バイト予算内へ間引く側は先頭から残すため、重い用途が落ちないようにこの順にする。
def _kind_sort_key(k: dict) -> tuple:
    return (-((k.get("input") or 0) + (k.get("output") or 0)), k.get("kind") or "")


# 利用者停止（`chat.turn` 監査の `detail.stopped=true`）を数える SQL。`detail.message_id_user` で `messages` に結合し、その `created_at` を `turns`/`stop_kinds` と同じ `turn_created_at` 境界として使う。`message_id_user` が無い過去行は結合が成立せず `a.created_at`（監査時刻）にフォールバックする。呼び出し側は境界2引数の後に `AND c.user_id = %s` 等を追加できる。
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


# SQL集計後、経路別定義で計測項目と未計測項目を分けて返す。イベントが一度も書かれない項目も、計測対象なら集計値0を保つ。
_API_USAGE_LIMIT_FIELDS = {
    "tool_result_clipped": "count",
    "total_budget_hit": "flag",
    "context_compactions": "count",
    "synthesis_truncated": "flag",
    "depth_escalated": "flag",
    "search_truncated": "count",
    "auto_continues": "count",
    "backend_unavailable_fulltext": "flag",
    "backend_unavailable_graph": "flag",
    "graph_reingest_required": "flag",
}
_USAGE_LIMIT_FIELDS_BY_ROUTE = {
    "codex": {
        "tool_result_clipped": "count",
        "total_budget_hit": "flag",
        "search_truncated": "count",
        "auto_continues": "count",
        "duplicate_tool_call": "count",
        "tool_calls_exhausted": "flag",
        "backend_unavailable_fulltext": "flag",
        "backend_unavailable_graph": "flag",
        "graph_reingest_required": "flag",
    },
    # API 集計対象の provider 名。未知の provider は計測対象にしない。
    "openai": _API_USAGE_LIMIT_FIELDS,
    "ollama": _API_USAGE_LIMIT_FIELDS,
    "gemini": _API_USAGE_LIMIT_FIELDS,
    "bedrock": _API_USAGE_LIMIT_FIELDS,
}
_USAGE_LIMIT_INT_FIELDS = tuple(dict.fromkeys(
    field for route_fields in _USAGE_LIMIT_FIELDS_BY_ROUTE.values()
    for field, kind in route_fields.items() if kind == "count"))
_USAGE_LIMIT_BOOL_FIELDS = tuple(dict.fromkeys(
    field for route_fields in _USAGE_LIMIT_FIELDS_BY_ROUTE.values()
    for field, kind in route_fields.items() if kind == "flag"))
_USAGE_LIMIT_FIELD_KINDS = _USAGE_LIMIT_FIELDS_BY_ROUTE["codex"] | _API_USAGE_LIMIT_FIELDS


def _usage_limits_provider_row(row: dict) -> dict:
    """provider別に計測対象だけを値として返し、未計測項目は null にする。"""
    measured = _USAGE_LIMIT_FIELDS_BY_ROUTE.get(row["provider"])
    result = {"provider": row["provider"], "turns": row["turns"] or 0}
    for field, kind in _USAGE_LIMIT_FIELD_KINDS.items():
        value = row[f"{field}_turns"] or 0
        result[f"{field}_turns"] = value if measured is not None and field in measured else None
        if kind == "count":
            total = row[f"{field}_total"]
            result[f"{field}_total"] = (
                int(total or 0) if measured is not None and field in measured else None)
    return result


def _usage_token_sum_cols() -> str:
    return ("COUNT(*) AS turns, "
            f"SUM({_usage_tok('input_tokens')}) AS input, "
            f"SUM({_usage_tok('cached_input_tokens')}) AS cached_input, "
            f"SUM({_usage_tok('output_tokens')}) AS output, "
            f"SUM({_usage_tok('reasoning_output_tokens')}) AS reasoning_output")


# usage を持つ user ターン（対応 assistant 返答に answer.usage がある）だけを集計対象にする WHERE 追加句。
_USAGE_TOKEN_WHERE = " AND jsonb_typeof(answer->'usage')='object' "


# turn_metrics 版の集計部品（`usage_stats` 専用）。
# 1 ターン＝期間内の user 発言 1 件＋その最初の assistant 返答の `turn_metrics` 行。ターンはリクエストごとに 1 回だけ一時表 `usage_turns` へ組み立て、各集計はこの表だけを読む（回答 JSON には触れない）。
# 対応付けは `turn_metrics.user_message_id` で引き、1 ターンに複数返答があれば `message_id` が最小のもの。`personal_turns` は user 発言の `messages.personal`（`turn_metrics.personal` は回答側の別物）。トークンは `turn_metrics` の値（activity 優先）で、失敗ターンの実消費も数える。
_TURN_LIMIT_FIELDS = _USAGE_LIMIT_INT_FIELDS + _USAGE_LIMIT_BOOL_FIELDS
_TURN_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
                      "cache_write_tokens")


# キャッシュ書き込み量の集計は、報告のあった行だけの合計（`cache_write`・全行が不明なら None）と、不明だった行（ターン／呼び出し）の数（`cache_write_unknown`）を並べて返す。0 に丸めず、不明が混ざる集計は `cache_write_unknown > 0` で分かる。
def _cache_write_fields(r) -> dict:
    return {"cache_write": int(r["cache_write"]) if r["cache_write"] is not None else None,
            "cache_write_unknown": int(r["cache_write_unknown"] or 0)}


# `usage_turns` から終了理由（`stop_kind.STOP_KINDS` の閉じた語彙）の分布を引く SQL。返答が存在するターン（`message_id IS NOT NULL`）のうち、確認カード（`lens='clarify'`）と利用者停止（`stopped_turns` 側で数える）を除き、語彙外・NULL は 'unknown' へ畳み込む。
_STOP_KINDS_SQL = (
    "SELECT CASE WHEN stop_kind = ANY(%s) THEN stop_kind ELSE 'unknown' END AS stop_kind, COUNT(*) AS n "
    "FROM usage_turns "
    "WHERE message_id IS NOT NULL "
    "  AND lens IS DISTINCT FROM 'clarify' "
    "  AND stop_kind IS DISTINCT FROM 'stopped_by_user' "
    "GROUP BY 1 ORDER BY n DESC"
)


# `usage_turns` から回答の完了状態（`answer_shape.COMPLETIONS`）の分布を引く SQL。母集団は `_STOP_KINDS_SQL` と同じ（返答があり確認カードでないターン）だが、利用者停止も `stopped` として数える。語彙外・NULL（旧形式の行）は 'unknown'。
_COMPLETIONS_SQL = (
    "SELECT CASE WHEN completion = ANY(%s) THEN completion ELSE 'unknown' END AS completion, COUNT(*) AS n "
    "FROM usage_turns "
    "WHERE message_id IS NOT NULL "
    "  AND lens IS DISTINCT FROM 'clarify' "
    "GROUP BY 1 ORDER BY n DESC"
)


def _usage_read_tuning(c) -> None:
    """この集計トランザクションの間だけ（`SET LOCAL`）ソート・ハッシュ集計の作業メモリを広げる（管理者の集計画面専用の短い読み取り）。"""
    c.execute("SET LOCAL work_mem = '64MB'")


def _build_usage_turns(c, start_ts, end_exclusive_ts, *, uid: str | None = None,
                       with_next: bool = False) -> None:
    """期間 `[start_ts, end_exclusive_ts)`（user 発言の created_at 基準）のターンを一時表 `usage_turns` へ作る（呼び出しトランザクションの終了で消える）。`uid` を渡すとその利用者のターンだけ。
    `with_next` は同会話の次の user 発言の時刻（`next_user_created_at`）も持たせ、巡（`_build_usage_rounds`）を所属ターンへ結ぶ区間の終端にする。`turn_jst` は発言時刻の JST 壁時計（日付・曜日・時・週の集計用に 1 回だけ計算する）。
    """
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_turns")
    cols = ", ".join(
        f"tm.{f}" for f in ("message_id", "lens", "provider", "model", "depth_profile", "stop_kind", "completion",
                            "duration_ms", "sources_count", "claims_unknown_reasons",
                            "gate_missing_codes") + _TURN_TOKEN_FIELDS + _TURN_LIMIT_FIELDS)
    if with_next:
        users = ("(SELECT id, conversation_id, created_at, personal, "
                 "    LEAD(created_at) OVER (PARTITION BY conversation_id ORDER BY created_at, id) "
                 "      AS next_user_created_at "
                 "  FROM messages WHERE role = 'user' AND created_at >= %s) u")
        next_col = ", u.next_user_created_at"
        params: list = [start_ts, end_exclusive_ts]
        period_sql = "u.created_at < %s"
    else:
        users = "messages u"
        next_col = ""
        params = [start_ts, end_exclusive_ts]
        period_sql = "u.role = 'user' AND u.created_at >= %s AND u.created_at < %s"
    where_uid = ""
    if uid is not None:
        where_uid = " AND c.user_id = %s"
        params.append(uid)
    c.execute(
        "CREATE TEMP TABLE usage_turns ON COMMIT DROP AS "
        "SELECT DISTINCT ON (u.id) u.id AS user_message_id, u.conversation_id, "
        "  u.created_at AS turn_created_at, (u.created_at AT TIME ZONE 'Asia/Tokyo') AS turn_jst, "
        "  u.personal AS user_personal" + next_col + ", "
        "  c.user_id, c.version, c.codex_session_id, " + cols + " "
        "FROM " + users + " JOIN conversations c ON c.id = u.conversation_id "
        "LEFT JOIN turn_metrics tm ON tm.user_message_id = u.id "
        "WHERE " + period_sql + " AND c.deleted_at IS NULL AND c.origin = 'own'" + where_uid + " "
        "ORDER BY u.id, tm.message_id",
        params,
    )


def _turn_token_sum_cols() -> str:
    return ("COUNT(*) AS turns, "
            "SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
            "SUM(cache_write_tokens) AS cache_write, "
            "COUNT(*) FILTER (WHERE cache_write_tokens IS NULL) AS cache_write_unknown, "
            "SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output")


# トークンを持つターン（`turn_metrics` に usage か activity がある返答）だけを集計対象にする WHERE 句。
_TURN_TOKEN_WHERE = " WHERE input_tokens IS NOT NULL "


def _turn_limits_select_cols() -> str:
    cols = []
    for f in _USAGE_LIMIT_INT_FIELDS:
        cols.append(f"COUNT(*) FILTER (WHERE {f} > 0) AS {f}_turns")
        cols.append(f"COALESCE(SUM({f}), 0) AS {f}_total")
    for f in _USAGE_LIMIT_BOOL_FIELDS:
        cols.append(f"COUNT(*) FILTER (WHERE {f}) AS {f}_turns")
    return ", ".join(cols)


# 主張の区分キー（`investigation_state.Claim.status`）。SQL 集計はこの3値だけを合算する（usage.py は investigation_state を import しないため値を直接持つ）。
_CLAIM_STATUS_KEYS = ("confirmed", "inferred", "unknown")


# 巡内 limits 増分のキー集合。int 系は合算、bool 系は True の巡数として合算する。
_ROUND_LIMIT_KEYS = _USAGE_LIMIT_INT_FIELDS + _USAGE_LIMIT_BOOL_FIELDS


def _new_round_bucket(depth: str, provider: str) -> dict:
    return {
        "depth_profile": depth, "provider": provider, "rounds": 0,
        "citations_delta_total": 0, "elapsed_ms_total": 0, "elapsed_n": 0,
        "input_tokens": 0, "output_tokens": 0, "tokens_n": 0,
        "claims": {k: 0 for k in _CLAIM_STATUS_KEYS}, "reason_codes": {},
        "limits": {}, "verdicts": {}, "stops": {}, "missing_codes": {},
    }


# `usage_stats()`/`usage_depth_rounds()` が共有する `chat-round` の集計。
# 期間境界は「巡が属する user ターン」の `created_at`（`usage_turns` の境界）を使う。所属ターンは `usage_turns` の区間 `[turn_created_at, next_user_created_at)`（`_build_usage_turns(with_next=True)`）に ts が入るターン。期間内に所属ターンを持たない巡は数えない。
# 所属ターンの返答（`turn_message_id`）はそのターンの最初の返答（`usage_turns.message_id`）。返答が保存されていないターンの巡は `unmatched_rounds` に計上するだけ。
def _build_usage_rounds(c, start_ts) -> None:
    """`usage_turns`（`with_next=True` で構築済み）を前提に、期間内の `chat-round` を 1 回の走査で 2 通りに束ねて一時表 `usage_rounds` へ作る（呼び出しトランザクションの終了で消える）。
    `per_turn=false` の行＝（深さ, 経路, 集計に使う meta の欄）が同じ巡の束（巡数 `w`・所要時間・トークンの合計）。束ねる欄は集計に使う欄だけを生の JSONB のまま取り出す（巡ごとに値の違う欄は束ねる単位に入れない）。
    `per_turn=true` の行＝所属ターン（深さは 1 ターンで一定・経路は最小値）ごとの到達巡数（`max_round`）。型の判定・合算は束ねた後の行に対して行う。
    """
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_rounds")
    c.execute(
        "CREATE TEMP TABLE usage_rounds ON COMMIT DROP AS "
        "SELECT GROUPING(turn_message_id) = 0 AS per_turn, depth, provider, r, cit, cl, lim, ver, stp, mis, "
        "  turn_message_id, "
        "  COUNT(*) AS w, COALESCE(SUM(elapsed_ms), 0) AS elapsed_total, COUNT(elapsed_ms) AS elapsed_n, "
        "  COALESCE(SUM(COALESCE(input_tokens, 0)) FILTER (WHERE input_tokens IS NOT NULL "
        "    OR output_tokens IS NOT NULL), 0) AS input_total, "
        "  COALESCE(SUM(COALESCE(output_tokens, 0)) FILTER (WHERE input_tokens IS NOT NULL "
        "    OR output_tokens IS NOT NULL), 0) AS output_total, "
        "  COUNT(*) FILTER (WHERE input_tokens IS NOT NULL OR output_tokens IS NOT NULL) AS tokens_n, "
        "  MIN(provider COLLATE \"C\") AS turn_provider, "
        "  MAX(CASE WHEN jsonb_typeof(r) = 'number' THEN r::numeric END) AS max_round "
        "FROM ("
        "  SELECT ut.message_id AS turn_message_id, "
        "    COALESCE(NULLIF(ut.depth_profile, ''), 'unknown') AS depth, "
        "    COALESCE(NULLIF(e.provider, ''), 'unknown') AS provider, "
        "    e.input_tokens, e.output_tokens, e.elapsed_ms, "
        "    e.meta->'round' AS r, e.meta->'citations_delta' AS cit, e.meta->'claims' AS cl, "
        "    e.meta->'limits' AS lim, e.meta->'verdict' AS ver, e.meta->'stop' AS stp, "
        "    e.meta->'missing_codes' AS mis "
        "  FROM usage_events e JOIN usage_turns ut ON ut.conversation_id = e.conversation_id "
        "    AND ut.turn_created_at <= e.ts "
        "    AND (ut.next_user_created_at IS NULL OR e.ts < ut.next_user_created_at) "
        "  WHERE e.kind = 'chat-round' AND e.ts >= %s AND e.conversation_id IS NOT NULL"
        ") j GROUP BY GROUPING SETS ((depth, provider, r, cit, cl, lim, ver, stp, mis), "
        "(turn_message_id, depth))",
        (start_ts,),
    )


# 束ねた巡（`per_turn=false`）の欄を型付きにする。型が合わない値は NULL（数えない）。
_ROUND_GROUPS_CTE = (
    "WITH g AS ("
    "  SELECT depth, provider, w, elapsed_total, elapsed_n, input_total, output_total, tokens_n, "
    "    CASE WHEN jsonb_typeof(r) = 'number' THEN r::numeric END AS rnd, "
    "    CASE WHEN jsonb_typeof(cit) = 'number' THEN cit::numeric END AS cit, "
    "    CASE WHEN jsonb_typeof(cl->'confirmed') = 'number' THEN (cl->'confirmed')::numeric END AS cl_confirmed, "
    "    CASE WHEN jsonb_typeof(cl->'inferred') = 'number' THEN (cl->'inferred')::numeric END AS cl_inferred, "
    "    CASE WHEN jsonb_typeof(cl->'unknown') = 'number' THEN (cl->'unknown')::numeric END AS cl_unknown, "
    "    CASE WHEN jsonb_typeof(cl->'reason_codes') = 'object' THEN cl->'reason_codes' END AS reason_codes, "
    "    CASE WHEN jsonb_typeof(lim) = 'object' THEN lim END AS limits, "
    "    CASE WHEN jsonb_typeof(ver) = 'string' THEN ver #>> '{}' END AS verdict, "
    "    CASE WHEN jsonb_typeof(stp) = 'string' THEN stp #>> '{}' END AS stop, "
    "    CASE WHEN jsonb_typeof(mis) = 'array' THEN mis END AS missing "
    "  FROM usage_rounds WHERE NOT per_turn"
    ") "
)


def _dec(v):
    """SQL の合計（numeric）を JSON 向けの数へ（整数値なら int・小数なら float）。"""
    if v is None:
        return 0
    return int(v) if v == v.to_integral_value() else float(v)


def _round_stats_from_sql(c) -> dict:
    """一時表 `usage_rounds`（`_build_usage_rounds`）を SQL で集計し、`_compute_round_stats` と同じ形の dict を返す。
    深さ×経路×巡番号ごとの合計・理由コード/limits/verdict/stop/不足軸の分類別合計・到達巡数の分布をすべて SQL が返し、Python は応答の形へ並べるだけ。型の規則は `_accumulate_round` と同じ。保存済みの `chat-round` の `meta` は書込側の型どおりであることを前提にする（欠落・NULL は数えない）。
    """
    scalars = c.execute(
        _ROUND_GROUPS_CTE +
        "SELECT depth, provider, rnd, SUM(w) AS rounds, SUM(w * cit) AS cit, SUM(elapsed_total) AS elapsed_total, "
        "  SUM(elapsed_n) AS elapsed_n, SUM(input_total) AS input_total, SUM(output_total) AS output_total, "
        "  SUM(tokens_n) AS tokens_n, SUM(w * cl_confirmed) AS cl_confirmed, "
        "  SUM(w * cl_inferred) AS cl_inferred, SUM(w * cl_unknown) AS cl_unknown "
        "FROM g GROUP BY depth, provider, rnd"
    ).fetchall()
    reasons = c.execute(
        _ROUND_GROUPS_CTE +
        "SELECT g.depth, g.provider, g.rnd, k.key AS code, SUM(g.w * (k.value)::numeric) AS n "
        "FROM g CROSS JOIN LATERAL jsonb_each(g.reason_codes) k "
        "WHERE jsonb_typeof(k.value) = 'number' GROUP BY g.depth, g.provider, g.rnd, k.key"
    ).fetchall()
    limits = c.execute(
        _ROUND_GROUPS_CTE +
        "SELECT g.depth, g.provider, g.rnd, k.key AS lim, "
        "  SUM(g.w * CASE WHEN jsonb_typeof(k.value) = 'boolean' THEN 1 ELSE (k.value)::numeric END) AS n "
        "FROM g CROSS JOIN LATERAL jsonb_each(g.limits) k "
        "WHERE k.key = ANY(%s) AND ((jsonb_typeof(k.value) = 'boolean' AND k.value = 'true'::jsonb) "
        "  OR jsonb_typeof(k.value) = 'number') "
        "GROUP BY g.depth, g.provider, g.rnd, k.key",
        (list(_ROUND_LIMIT_KEYS),),
    ).fetchall()
    labels = c.execute(
        _ROUND_GROUPS_CTE +
        "SELECT depth, provider, rnd, 'verdicts' AS f, verdict AS v, SUM(w) AS n FROM g "
        "WHERE verdict <> '' GROUP BY depth, provider, rnd, verdict "
        "UNION ALL "
        "SELECT depth, provider, rnd, 'stops', stop, SUM(w) FROM g "
        "WHERE stop <> '' GROUP BY depth, provider, rnd, stop "
        "UNION ALL "
        "SELECT g.depth, g.provider, g.rnd, 'missing_codes', e.code #>> '{}', SUM(g.w) "
        "FROM g CROSS JOIN LATERAL jsonb_array_elements(g.missing) e(code) "
        "WHERE jsonb_typeof(e.code) = 'string' AND (e.code #>> '{}') <> '' "
        "GROUP BY g.depth, g.provider, g.rnd, e.code #>> '{}'"
    ).fetchall()
    dist = c.execute(
        "SELECT depth, turn_provider AS provider, "
        "  CASE WHEN COALESCE(max_round, 0) > 0 THEN max_round ELSE 1 END AS rounds_reached, "
        "  COUNT(*) AS turns "
        "FROM usage_rounds WHERE per_turn AND turn_message_id IS NOT NULL "
        "GROUP BY 1, 2, 3"
    ).fetchall()
    unmatched = c.execute(
        "SELECT COALESCE(SUM(w), 0) AS n FROM usage_rounds WHERE per_turn AND turn_message_id IS NULL"
    ).fetchone()["n"]

    by_fine: dict[tuple, dict] = {}

    def _bucket(r) -> dict:
        rd = r["rnd"]
        round_no = int(rd) if rd is not None else None
        key = (r["depth"], r["provider"], round_no)
        agg = by_fine.get(key)
        if agg is None:
            agg = by_fine[key] = _new_round_bucket(r["depth"], r["provider"])
            agg["round_no"] = round_no
        return agg

    for r in scalars:
        agg = _bucket(r)
        agg["rounds"] += int(r["rounds"])
        agg["citations_delta_total"] += _dec(r["cit"])
        agg["elapsed_ms_total"] += int(r["elapsed_total"])
        agg["elapsed_n"] += int(r["elapsed_n"])
        agg["input_tokens"] += int(r["input_total"])
        agg["output_tokens"] += int(r["output_total"])
        agg["tokens_n"] += int(r["tokens_n"])
        for status in _CLAIM_STATUS_KEYS:
            agg["claims"][status] += int(r[f"cl_{status}"] or 0)
    for r in reasons:
        agg = _bucket(r)
        agg["reason_codes"][r["code"]] = agg["reason_codes"].get(r["code"], 0) + int(r["n"])
    for r in limits:
        agg = _bucket(r)
        agg["limits"][r["lim"]] = agg["limits"].get(r["lim"], 0) + _dec(r["n"])
    for r in labels:
        agg = _bucket(r)
        agg[r["f"]][r["v"]] = agg[r["f"]].get(r["v"], 0) + int(r["n"])

    # 深さ×経路の合計は巡番号別の群を足し上げる。
    by_dp: dict[tuple, dict] = {}
    for (depth, provider, _rn), a in by_fine.items():
        t = by_dp.setdefault((depth, provider), _new_round_bucket(depth, provider))
        for f in ("rounds", "citations_delta_total", "elapsed_ms_total", "elapsed_n",
                  "input_tokens", "output_tokens", "tokens_n"):
            t[f] += a[f]
        for status in _CLAIM_STATUS_KEYS:
            t["claims"][status] += a["claims"][status]
        for f in ("reason_codes", "limits", "verdicts", "stops", "missing_codes"):
            for k, n in a[f].items():
                t[f][k] = t[f].get(k, 0) + n

    def _finalize(agg: dict) -> dict:
        return {**agg,
                "elapsed_ms_avg": (agg["elapsed_ms_total"] / agg["elapsed_n"]) if agg["elapsed_n"] else None,
                "citations_delta_avg": (agg["citations_delta_total"] / agg["rounds"]) if agg["rounds"] else None}

    return {
        "by_depth_provider": [
            _finalize(a) for a in sorted(by_dp.values(), key=lambda a: (a["depth_profile"], a["provider"]))],
        "by_round": [
            _finalize(a) for a in sorted(
                by_fine.values(),
                key=lambda a: (a["depth_profile"], a["provider"], a["round_no"] is None, a["round_no"] or 0))],
        "round_distribution": [
            {"depth_profile": r["depth"], "provider": r["provider"],
             "rounds_reached": int(r["rounds_reached"]), "turns": r["turns"]}
            for r in sorted(dist, key=lambda r: (r["depth"], r["provider"], int(r["rounds_reached"])))],
        "unmatched_rounds": int(unmatched),
    }


# 最終回答の不明理由コード（`turn_metrics.claims_unknown_reasons`）と最終ゲートの不足軸（`gate_missing_codes`）を、組み立て済みの一時表 `usage_turns` から取る（`usage_stats()`/`usage_depth_rounds()` が共有）。主張を持つターン（`claims_unknown_reasons` が NULL でない）だけが対象。
# `gate_missing_codes`（Codex 経路の最終ゲート）は `chat-round` を発生させないため、ここが唯一の取得点（API 経路は NULL のまま＝二重計上にならない）。深さ×経路×コードの合計だけを SQL が返す（規則は `_compute_final_reason_codes`/`_compute_final_missing_codes` と同一）。
def _final_claims_from_sql(c) -> tuple[list[dict], dict[tuple, dict[str, int]]]:
    """`(最終回答の不明理由コード分布, 最終ゲートの不足軸の深さ×経路別合算)`。"""
    # 同じ値のターンを先に 1 回の走査で束ね（ターン数 `w`）、束ねた行だけを展開して `w` 倍で足す。
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_claims")
    c.execute(
        "CREATE TEMP TABLE usage_claims ON COMMIT DROP AS "
        "SELECT COALESCE(NULLIF(depth_profile, ''), 'unknown') AS depth, "
        "  COALESCE(NULLIF(provider, ''), 'unknown') AS provider, "
        "  claims_unknown_reasons AS reasons, gate_missing_codes AS gate, COUNT(*) AS w "
        "FROM usage_turns WHERE claims_unknown_reasons IS NOT NULL GROUP BY 1, 2, 3, 4"
    )
    reason_rows = c.execute(
        "SELECT g.depth, g.provider, k.key AS code, SUM(g.w * (k.value)::numeric) AS n "
        "FROM usage_claims g CROSS JOIN LATERAL jsonb_each(g.reasons) k "
        "WHERE jsonb_typeof(g.reasons) = 'object' AND jsonb_typeof(k.value) = 'number' "
        "GROUP BY 1, 2, 3"
    ).fetchall()
    # 空のオブジェクト（最終ゲートはあるが不足軸が無い）も「その深さ×経路のバケットが在る」印として返す（k.key が NULL の行）。
    gate_rows = c.execute(
        "SELECT g.depth, g.provider, k.key AS code, "
        "  SUM(CASE WHEN jsonb_typeof(k.value) = 'number' THEN g.w * (k.value)::numeric ELSE 0 END) AS n "
        "FROM usage_claims g LEFT JOIN LATERAL jsonb_each(g.gate) k ON true "
        "WHERE jsonb_typeof(g.gate) = 'object' GROUP BY 1, 2, 3"
    ).fetchall()
    reasons: dict[tuple, dict[str, int]] = {}
    for r in reason_rows:
        reasons.setdefault((r["depth"], r["provider"]), {})[r["code"]] = int(r["n"])
    missing_agg: dict[tuple, dict[str, int]] = {}
    for r in gate_rows:
        bucket = missing_agg.setdefault((r["depth"], r["provider"]), {})
        if r["code"] is not None:
            bucket[r["code"]] = bucket.get(r["code"], 0) + int(r["n"])
    final_reasons = [
        {"depth_profile": dp, "provider": pv, "reason_code": code, "claims": n}
        for (dp, pv), bucket in sorted(reasons.items())
        for code, n in sorted(bucket.items())
    ]
    return final_reasons, missing_agg


def _merge_final_missing_agg(rounds_stats: dict, agg: dict[tuple, dict[str, int]]) -> None:
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


def _round_reason_codes(rounds_stats: dict, final_reasons: list[dict]) -> dict:
    """理由コード分布の2軸（`final`＝最終回答の主張／`rounds`＝巡別記録の主張内訳の合算）。"""
    return {
        "final": final_reasons,
        "rounds": [
            {"depth_profile": a["depth_profile"], "provider": a["provider"],
             "reason_code": code, "claims": n}
            for a in rounds_stats["by_depth_provider"]
            for code, n in sorted(a["reason_codes"].items())
        ],
    }


# チャット以外の LLM 呼び出し（`usage_events`）の期間内集計。

# `usage_ev` を（用途 kind・経路・モデル・利用者・会話）で束ねた行から、`keys` 単位の合計を取る列。全 NULL の合計（報告不能のみのグループ）は None のまま保つ（0 に丸めない）。平均所要時間は計測のあった呼び出し（`elapsed_n`）あたり。
_EV_KIND_COLS = (
    "{keys}, SUM(calls) AS calls, SUM(input) AS input, SUM(cached_input) AS cached_input, "
    "SUM(cache_write) AS cache_write, SUM(cache_write_unknown) AS cache_write_unknown, "
    "SUM(output) AS output, SUM(reasoning_output) AS reasoning_output, "
    "SUM(elapsed_ms_total) AS elapsed_ms_total, "
    "SUM(elapsed_ms_total) / NULLIF(SUM(elapsed_n), 0) AS elapsed_ms_avg, "
    "COALESCE(SUM(elapsed_n), 0) AS elapsed_n"
)


def _build_usage_events(c, start_ts, end_exclusive_ts, *, conv_only: bool = False) -> None:
    """期間内の `usage_events`（`chat-round` は正本と二重に足さないため除く）を、用途 kind・経路・モデル・利用者・会話ごとに束ねて一時表 `usage_ev` へ作る（`usage_events` の走査はここで 1 回だけ）。`conv_only` は `usage_conv`（`_build_usage_conversations`）の会話に属する行だけに絞る。"""
    conv_filter = " AND conversation_id IN (SELECT cid FROM usage_conv)" if conv_only else ""
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_ev")
    c.execute(
        "CREATE TEMP TABLE usage_ev ON COMMIT DROP AS "
        "SELECT user_id, conversation_id, kind, provider, model, SUM(calls) AS calls, "
        "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
        "  SUM(cache_write_tokens) AS cache_write, "
        "  COALESCE(SUM(calls) FILTER (WHERE cache_write_tokens IS NULL), 0) AS cache_write_unknown, "
        "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output, "
        "  SUM(elapsed_ms) AS elapsed_ms_total, COUNT(elapsed_ms) AS elapsed_n "
        "FROM usage_events WHERE ts >= %s AND ts < %s AND kind <> 'chat-round'" + conv_filter + " "
        "GROUP BY user_id, conversation_id, kind, provider, model",
        (start_ts, end_exclusive_ts),
    )


# 回答時間・会話単位の集計（SQL 側で完結し、Python は応答の形へ並べるだけ）。

# 回答時間（ミリ秒）の分布を SQL で求める列。平均は合計/件数、中央値は線形補間（偶数件は中央 2 値の平均）、p90 は最近傍順位（`ceil(0.9 * n)` 番目・補間なし）。
_RESPONSE_TIME_COLS = (
    "COUNT(*) AS n, SUM(d)::float8 / NULLIF(COUNT(*), 0) AS avg, MAX(d) AS max, "
    "percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS median, "
    "percentile_disc(0.9) WITHIN GROUP (ORDER BY d) AS p90"
)


def _response_time_stats(row) -> dict:
    """`_RESPONSE_TIME_COLS` の行を応答の形へ（0 件なら全て None・n=0）。"""
    n = row["n"] if row else 0
    if not n:
        return {"avg": None, "median": None, "max": None, "p90": None, "n": 0}
    return {"avg": float(row["avg"]), "median": float(row["median"]), "max": int(row["max"]),
            "p90": float(row["p90"]), "n": int(n)}


# 回答時間の母集団: 期間内（回答の created_at）の assistant 返答のうち、他の集計と同じ会話（origin='own'・deleted_at IS NULL）で、確認カードでなく、所要時間が記録された行。
_RESPONSE_TIME_FROM = (
    "FROM turn_metrics tm JOIN messages m ON m.id = tm.message_id "
    "JOIN conversations c ON c.id = tm.conversation_id "
    "WHERE m.created_at >= %s AND m.created_at < %s "
    "  AND c.deleted_at IS NULL AND c.origin = 'own' "
    "  AND tm.lens IS DISTINCT FROM 'clarify' AND tm.duration_ms IS NOT NULL"
)


def _response_time_from_sql(c, start_ts, end_exclusive_ts) -> dict:
    """`usage_stats().response_time`（全体＋経路別）。"""
    rows = c.execute(
        "SELECT p AS provider, GROUPING(p) AS g, " + _RESPONSE_TIME_COLS + " FROM ("
        "  SELECT COALESCE(NULLIF(tm.provider, ''), 'unknown') AS p, tm.duration_ms AS d "
        + _RESPONSE_TIME_FROM + ") t GROUP BY GROUPING SETS ((), (p))",
        (start_ts, end_exclusive_ts),
    ).fetchall()
    overall = _response_time_stats(next((r for r in rows if r["g"] == 1), None))
    overall["provider"] = None
    by_provider = []
    for r in sorted((r for r in rows if r["g"] == 0), key=lambda r: (-r["n"], r["provider"])):
        row = _response_time_stats(r)
        row["provider"] = r["provider"]
        by_provider.append(row)
    return {"overall": overall, "by_provider": by_provider}


def _build_usage_conversations(c) -> None:
    """`usage_turns` から 1 会話 1 行の一時表 `usage_conv`（会話・利用者・資料フォルダ・期間内の user ターン数・chat のトークン合計・回答時間の平均）を作る。"""
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_conv")
    c.execute(
        "CREATE TEMP TABLE usage_conv ON COMMIT DROP AS "
        "SELECT conversation_id AS cid, user_id AS uid, version AS world, codex_session_id, "
        "  COUNT(*) AS user_turns, "
        "  COUNT(*) FILTER (WHERE input_tokens IS NOT NULL) AS chat_calls, "
        "  SUM(input_tokens) AS chat_input, SUM(cached_input_tokens) AS chat_cached_input, "
        "  SUM(cache_write_tokens) AS chat_cache_write, "
        "  COUNT(*) FILTER (WHERE input_tokens IS NOT NULL AND cache_write_tokens IS NULL) "
        "    AS chat_cache_write_unknown, "
        "  SUM(output_tokens) AS chat_output, SUM(reasoning_output_tokens) AS chat_reasoning_output, "
        "  AVG(duration_ms) FILTER (WHERE lens IS DISTINCT FROM 'clarify') AS avg_response_time_ms "
        "FROM usage_turns GROUP BY conversation_id, user_id, version, codex_session_id"
    )


def _conversation_turn_stats_from_sql(c) -> tuple[dict, float | None]:
    """会話あたりの user ターン数分布（avg/median/max/p90）と resume_rate・session_eligible/recorded（`usage_stats().conversation_turns`/`resume_rate`）。対象は `usage_conv`（期間内に user ターンが 1 件以上ある会話）。user ターン数 2 以上の会話のうち `codex_session_id` が設定された割合が resume_rate（分母 0 なら None）。"""
    r = c.execute(
        "SELECT COUNT(*) AS n, SUM(user_turns)::float8 / NULLIF(COUNT(*), 0) AS avg, MAX(user_turns) AS max, "
        "  percentile_cont(0.5) WITHIN GROUP (ORDER BY user_turns) AS median, "
        "  percentile_disc(0.9) WITHIN GROUP (ORDER BY user_turns) AS p90, "
        "  COUNT(*) FILTER (WHERE user_turns >= 2) AS eligible, "
        "  COUNT(*) FILTER (WHERE user_turns >= 2 AND codex_session_id IS NOT NULL) AS recorded "
        "FROM usage_conv"
    ).fetchone()
    if r["n"]:
        stats = {"avg": float(r["avg"]), "median": float(r["median"]), "max": int(r["max"]),
                 "p90": float(r["p90"])}
    else:
        stats = {"avg": None, "median": None, "max": None, "p90": None}
    eligible, recorded = int(r["eligible"]), int(r["recorded"])
    stats.update(session_eligible=eligible, session_recorded=recorded)
    return stats, ((recorded / eligible) if eligible > 0 else None)


_CONV_TOP_SORT = {
    "tokens": "tok_total", "turns": "cv.user_turns", "elapsed": "el_total"}


def _kind_row(r) -> dict:
    """`usage_events` の kind 別集計行（calls/tokens/elapsed）を応答の形へ。全 NULL の合計は 0 に丸めず None のまま（報告不能と 0 を区別する）。"""
    return {
        "kind": r["kind"], "calls": int(r["calls"] or 0),
        "input": int(r["input"]) if r["input"] is not None else None,
        "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
        **_cache_write_fields(r),
        "output": int(r["output"]) if r["output"] is not None else None,
        "reasoning_output": int(r["reasoning_output"]) if r["reasoning_output"] is not None else None,
        "elapsed_ms_total": int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None else None,
        "elapsed_ms_avg": float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None else None,
        "elapsed_n": int(r["elapsed_n"] or 0),
    }


def _conversations_top_from_sql(c, *, sort: str = "tokens", limit: int = 20) -> list[dict]:
    """会話ごとの補助 AI 使用量の上位（`usage_stats().conversations_top`・`usage_conversations`）。
    `usage_conv` を土台に、chat 以外の kind（`usage_ev`）を会話単位で合流し、並び（tokens＝chat と kind の input+output の合算・報告不能は 0 扱い／turns／elapsed）の上位 `limit` 件を SQL で選ぶ（同値は会話 id の昇順）。`usage_events.conversation_id` が NULL の行はどの会話にも合流しない。各会話は `kinds`（chat＋kind 別・input+output の降順）を持つ。
    """
    top = c.execute(
        "SELECT cv.*, COALESCE(cv.chat_input, 0) + COALESCE(cv.chat_output, 0) + COALESCE(ev.tok, 0) AS tok_total, "
        "  COALESCE(ev.el, 0) AS el_total "
        "FROM usage_conv cv LEFT JOIN ("
        "  SELECT conversation_id AS cid, SUM(COALESCE(input, 0) + COALESCE(output, 0)) AS tok, "
        "    SUM(COALESCE(elapsed_ms_total, 0)) AS el FROM usage_ev WHERE conversation_id IS NOT NULL "
        "  GROUP BY conversation_id) ev ON ev.cid = cv.cid "
        "ORDER BY " + _CONV_TOP_SORT[sort] + " DESC, cv.cid LIMIT %s",
        (limit,),
    ).fetchall()
    kind_rows = c.execute(
        "SELECT " + _EV_KIND_COLS.format(keys="conversation_id AS cid, kind") + " "
        "FROM usage_ev WHERE conversation_id = ANY(%s) GROUP BY conversation_id, kind",
        ([r["cid"] for r in top],),
    ).fetchall() if top else []
    kinds_by_cid: dict[int, list[dict]] = {}
    for r in kind_rows:
        kinds_by_cid.setdefault(r["cid"], []).append(_kind_row(r))
    out = []
    for r in top:
        kinds: list[dict] = []
        if (r["chat_calls"] or 0) > 0:
            kinds.append({
                "kind": "chat", "calls": int(r["chat_calls"]),
                "input": int(r["chat_input"] or 0), "cached_input": int(r["chat_cached_input"] or 0),
                **_cache_write_fields({"cache_write": r["chat_cache_write"],
                                       "cache_write_unknown": r["chat_cache_write_unknown"]}),
                "output": int(r["chat_output"] or 0), "reasoning_output": int(r["chat_reasoning_output"] or 0),
                "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0,
            })
        kinds += kinds_by_cid.get(r["cid"], [])
        kinds.sort(key=_kind_sort_key)
        out.append({
            "conversation_id": r["cid"], "uid": r["uid"], "world": r["world"],
            "user_turns": r["user_turns"] or 0, "kinds": kinds,
            "response_time_avg_ms": (float(r["avg_response_time_ms"])
                                     if r["avg_response_time_ms"] is not None else None),
        })
    return out


def usage_stats(days: int = 30, *, time_from: str | None = None, time_to: str | None = None) -> dict:
    """期間内の利用統計を集計する（本文/タイトルは含めない）。
    `time_from`/`time_to`（ISO 8601・オフセット必須）を渡すと `days` の代わりに `[from, to)` を期間に使う（`_usage_period` 参照・規則違反は `UsagePeriodError`）。全クエリは `_usage_period_bounds` の半開区間 `[start_ts, end_exclusive_ts)` で絞る。
    主な項目:
    - users: ターン数（role='user' メッセージ数）降順。totals: 期間合計。daily: 日別ターン数＋日別アクティブユーザー数。period: 集計対象の JST 暦日範囲（日別チャートはこの範囲でゼロ埋め描画する）。
    - lens 内訳・personal 利用ターン数は各 user ターンの最初の assistant 返答だけを数える（`_build_usage_turns`）。
    - ターン由来の集計は、user 発言（`messages`）とその最初の返答の `turn_metrics` 行を一時表（`_build_usage_turns`）へ組み、各集計はそれだけを読む（トークンは activity 優先で失敗ターンの実消費も数える）。`turn_metrics` の無い返答は起動時の補完（`turn_metrics.backfill_missing`）が埋めるまで返答由来の項目に現れない。
    - `c.origin='own'` に限定する（sanitized_snapshot の二重計上を避ける）。`conversations`/`active_days`/`last_active` は role='user' 基準で、`HAVING` により期間内に user turn が 0 件の行を除く。
    - zero_hit／各 user 行の `knowledge_turns`/`zero_hit_turns`/`zero_hit_rate`: lens != 'chat' のターンのうち `turn_metrics.sources_count` が 0 件の割合。
    - heatmap: user メッセージ数を JST 曜日(0=日〜6=土)×時間帯(0-23)で集計（0 件のセルは返さない）。
    - worlds: `conversations.version` 別ターン数。providers: `chat.turn` 監査の `detail->>'provider'` 別ターン数（`_USAGE_KNOWN_PROVIDERS` の allowlist で畳む・stopped ターンも含む）。
    - retention: JST 週（月曜始まり）ごとのアクティブユーザー数の推移と、連続する週ペアをプールした再訪率（週ペアが無ければ `revisit_rate=None`）。
    - downloads: `document.downloaded` 監査の期間合計＋日別内訳（原本DL数）。
    - conversation_turns: 会話あたりの user ターン数の avg/median/max/p90。resume_rate: user ターン数2以上の会話のうち `codex_session_id` が設定された割合（対象会話が無ければ None）。
    - tokens.by_user_kind: ユーザー別×用途別（kind）の calls/tokens/elapsed_ms。chat 行は `token_by_user` から合流し、それ以外は `usage_events`（`user_id IS NOT NULL` のみ）を集計する。user_id が NULL の呼び出しは含まれないため、同一 kind の合計は `by_kind` 以下になりうる。
    - response_time: 全体（`overall`）と経路別（`by_provider`）の `turn_metrics.duration_ms` の avg/median/p90/max/件数（0件なら None・n=0）。確認カードと所要時間が無い行（実行中のターン・停止の終端が無いまま終わったターン）は除く。停止の終端を保存したターンは含む。
    - conversations_top: 期間内に user ターンがある会話について、会話 id・uid・world・user ターン数・用途別内訳（`kinds`）・回答時間の平均を、トークン合計の降順で上位20件（選択は SQL の `_conversations_top_from_sql`・タイトル・本文は含まない）。
    - stop_kinds: `turn_metrics.stop_kind`（`sherpa/stop_kind.py` の閉じた8値）の分布。返答が存在するターン（`message_id IS NOT NULL`）のみで、利用者の明示停止と確認カードは除き、語彙外・NULL は 'unknown' に畳む。
    - completions: `turn_metrics.completion`（complete/partial/stopped/failed）の分布。母集団は stop_kinds と同じ（返答があり確認カードでないターン）で、利用者停止も `stopped` に入る。旧形式の行・語彙外は 'unknown'。
    - stopped_turns: 利用者の明示停止（`chat.turn` 監査の `detail.stopped=true`）の件数。境界は `turns`/`stop_kinds` と同じ `turn_created_at`（`_stopped_turns_sql`）。
    期間の基準時刻は指標ごとに違う:
    - ターン由来の集計: user 発言の `turn_created_at`。
    - `usage_events` 由来: イベント自身の `ts`。
    - 巡集計（kind='chat-round'）: その巡が属する user 発言の `created_at`（`_build_usage_rounds`）。
    - 品質採点（`quality_runs`）: 実行期間（`executed_from`/`executed_to`）の完全包含（`depth_quality_stats`）。
    """
    _ensure()
    start_ts, end_exclusive_ts, period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        _usage_read_tuning(c)
        _build_usage_turns(c, start_ts, end_exclusive_ts, with_next=True)
        user_rows = c.execute(
            "SELECT user_id AS uid, "
            "  COUNT(*) AS turns, "
            "  COUNT(DISTINCT conversation_id) AS conversations, "
            "  COUNT(DISTINCT turn_jst::date) AS active_days, "
            "  MAX(turn_created_at) AS last_active, "
            "  COUNT(*) FILTER (WHERE lens='impact') AS lens_impact, "
            "  COUNT(*) FILTER (WHERE lens='qa') AS lens_qa, "
            "  COUNT(*) FILTER (WHERE lens='troubleshoot') AS lens_troubleshoot, "
            "  COUNT(*) FILTER (WHERE lens='chat') AS lens_chat, "
            "  COUNT(*) FILTER (WHERE user_personal) AS personal_turns, "
            "  ARRAY_REMOVE(ARRAY_AGG(DISTINCT version COLLATE \"C\"), NULL) AS worlds, "
            "  COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens != 'chat') AS knowledge_turns, "
            "  COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens != 'chat' AND "
            "    sources_count = 0) AS zero_hit_turns "
            "FROM usage_turns "
            "GROUP BY user_id "
            "ORDER BY turns DESC, user_id",
        ).fetchall()
        daily_rows = c.execute(
            "SELECT turn_jst::date AS date, "
            "  COUNT(*) AS turns, COUNT(DISTINCT user_id) AS active_users "
            "FROM usage_turns GROUP BY 1 ORDER BY 1",
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
            "SELECT version AS world, COUNT(*) AS turns FROM usage_turns "
            "WHERE version IS NOT NULL "
            "GROUP BY version ORDER BY turns DESC, version",
        ).fetchall()
        provider_rows = c.execute(
            "SELECT CASE WHEN detail->>'provider' = ANY(%s) THEN detail->>'provider' ELSE 'unknown' END AS provider, "
            "  COUNT(*) AS n FROM audit_log "
            "WHERE created_at >= %s AND created_at < %s AND action='chat.turn' "
            "GROUP BY provider ORDER BY n DESC",
            (list(_USAGE_KNOWN_PROVIDERS), start_ts, end_exclusive_ts),
        ).fetchall()
        # 終了理由（`usage_turns.stop_kind`・`stop_kind.py` の8値）の分布。`usage_turns` 経由にして `turns`/`stopped_turns` と同じ `turn_created_at` 境界を使う。`answer IS NOT NULL` で assistant 返答が存在するターンだけに絞り（停止・実行中のターンが 'unknown' に混入して `stopped_turns` と二重計上になるのを避ける）、`stopped_by_user` は除く。allowlist（`stop_kind.STOP_KINDS`）外の値は 'unknown' へ畳み込み、確認カード（lens='clarify'）は母数から外す。
        stop_kind_rows = c.execute(_STOP_KINDS_SQL, (list(stop_kind.STOP_KINDS),)).fetchall()
        completion_rows = c.execute(_COMPLETIONS_SQL, (list(answer_shape.COMPLETIONS),)).fetchall()
        # limits（「打ち切りの内訳」・経路別）: `stop_kind_rows` から `stopped_by_user` の除外だけを外した母集団（停止ターンで当たった制限も残す）に、`answer->'usage'->>'provider'` 別の集計を足す。
        limits_rows = c.execute(
            "SELECT COALESCE(provider, 'unknown') AS provider, "
            "  COUNT(*) AS turns, " + _turn_limits_select_cols() + " "
            "FROM usage_turns "
            "WHERE message_id IS NOT NULL "
            "  AND lens IS DISTINCT FROM 'clarify' "
            "GROUP BY 1 ORDER BY turns DESC, provider",
        ).fetchall()
        # 利用者停止（`stopped_by_user`）は上の分布から除き、監査 `chat.turn`（`detail.stopped=true`）から別途数える（`_stopped_turns_sql`・`turns`/`stop_kinds` と同じ `turn_created_at` 境界）。`audit_log` は削除伝播の対象外のため、`conversations` と JOIN して `turns`/`stop_kinds` と同じ母集団（`deleted_at IS NULL AND origin='own'`）に絞る。`resource_id` は `chat.turn` 監査の書込側が常に `f"conv:{conversation_id}"` 形式で書く。
        stopped_turns_row = c.execute(
            _stopped_turns_sql(), (start_ts, end_exclusive_ts)
        ).fetchone()
        heatmap_rows = c.execute(
            "SELECT EXTRACT(DOW FROM turn_jst)::int AS weekday, "
            "  EXTRACT(HOUR FROM turn_jst)::int AS hour, "
            "  COUNT(*) AS n "
            "FROM usage_turns GROUP BY weekday, hour",
        ).fetchall()
        # 定着指標: 週ごとのアクティブ人数と、翌週（7 日後）も続けた人数を SQL が返す。
        retention_rows = c.execute(
            "WITH w AS (SELECT DISTINCT user_id, date_trunc('week', turn_jst)::date AS wk FROM usage_turns) "
            "SELECT a.wk AS week_start, COUNT(*) AS active_users, "
            "  EXISTS (SELECT 1 FROM w x WHERE x.wk = a.wk + 7) AS has_next, "
            "  COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM w b WHERE b.user_id = a.user_id "
            "    AND b.wk = a.wk + 7)) AS revisited "
            "FROM w a GROUP BY a.wk ORDER BY a.wk",
        ).fetchall()
        download_daily_rows = c.execute(
            "SELECT (created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, COUNT(*) AS n FROM audit_log "
            "WHERE created_at >= %s AND created_at < %s AND action='document.downloaded' "
            "GROUP BY date ORDER BY date",
            (start_ts, end_exclusive_ts),
        ).fetchall()
        # トークン使用量（answer->'usage'）を provider/model 別・上位ユーザー別・日別で集計する（入力/出力トークン数のみ・金額換算はしない・usage を持たないターンは除外）。
        token_model_rows = c.execute(
            "SELECT provider, model, " + _turn_token_sum_cols() + " FROM usage_turns" + _TURN_TOKEN_WHERE +
            "GROUP BY provider, model ORDER BY input DESC, output DESC",
        ).fetchall()
        token_user_rows = c.execute(
            "SELECT user_id AS uid, " + _turn_token_sum_cols() + " FROM usage_turns" + _TURN_TOKEN_WHERE +
            "GROUP BY user_id ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC, user_id",
        ).fetchall()
        token_daily_rows = c.execute(
            "SELECT turn_jst::date AS date, "
            "SUM(input_tokens) AS input, SUM(output_tokens) AS output "
            "FROM usage_turns" + _TURN_TOKEN_WHERE +
            "GROUP BY date ORDER BY date",
        ).fetchall()
        # チャット以外の LLM 呼び出し（intent 分類・グラフ抽出・概念候補提案・埋め込み・admin グラフ質問・VLM）を kind 別に集計する。usage_events は kind='chat' を含まず（chat は token_model_rows から合成）、kind='chat-round' も除く（巡の消費は正本に載っているため二重に足さない）。elapsed_ms が NULL の行は集計から除く（COUNT(列)＝`elapsed_n`）。
        _build_usage_events(c, start_ts, end_exclusive_ts)
        usage_event_rows = c.execute(
            "SELECT " + _EV_KIND_COLS.format(keys="kind, provider, model") + " "
            "FROM usage_ev GROUP BY kind, provider, model ORDER BY kind, input DESC NULLS LAST",
        ).fetchall()
        # ユーザー別×用途別（kind）内訳（usage_events 側）。`user_id IS NOT NULL` で絞る（匿名呼び出しは by_kind 側でのみ集計）。
        usage_event_user_kind_rows = c.execute(
            "SELECT " + _EV_KIND_COLS.format(keys="user_id AS uid, kind") + " "
            "FROM usage_ev WHERE user_id IS NOT NULL GROUP BY user_id, kind ORDER BY user_id, kind",
        ).fetchall()
        # 回答時間（`duration_ms`）の分布・会話あたりの user ターン数分布・resume_rate・会話ごとの補助 AI 使用量の上位。いずれも SQL が集計する。
        response_time = _response_time_from_sql(c, start_ts, end_exclusive_ts)
        _build_usage_conversations(c)
        conversation_turns, resume_rate = _conversation_turn_stats_from_sql(c)
        conversations_top = _conversations_top_from_sql(c, limit=20)
        # 巡別記録（`chat-round`）の表示専用集計: 深さ（`answer->'usage'->>'depth_profile'`）×経路（provider）別の巡数分布・活動量・主張の区分/理由コード内訳。期間境界は `_build_usage_rounds` の「所属する user ターン」基準。
        _build_usage_rounds(c, start_ts)
        rounds_stats = _round_stats_from_sql(c)
        # 最終回答の主張のうち不明（`status='unknown'`）の理由コード分布（主張単位）。深さ・経路別に数え、巡別（chat-round）の集計とは別軸。
        final_reasons, final_missing = _final_claims_from_sql(c)

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
    # フロントの日別チャートはこの範囲でゼロ埋め描画する。`period` は `_usage_period` が組み立て済み（`end` は含む終了日・実際に使った半開区間は `from`/`to`）。

    zero_hit = {
        "knowledge_turns": total_knowledge_turns,
        "zero_hit_turns": total_zero_hit_turns,
        "rate": (total_zero_hit_turns / total_knowledge_turns) if total_knowledge_turns > 0 else None,
    }
    worlds_usage = [{"world": r["world"], "turns": r["turns"] or 0} for r in world_rows]
    # allowlist 外/NULL の畳み込みは SQL 側（CASE式・GROUP BY）で完結する。
    providers_usage = [{"provider": r["provider"], "turns": r["n"] or 0} for r in provider_rows]
    heatmap = [{"weekday": r["weekday"], "hour": r["hour"], "count": r["n"] or 0} for r in heatmap_rows]
    # 終了理由の分布＋利用者停止の件数。
    stop_kinds = [{"stop_kind": r["stop_kind"], "turns": r["n"] or 0} for r in stop_kind_rows]
    stopped_turns = (stopped_turns_row["n"] or 0) if stopped_turns_row else 0
    completions = [{"completion": r["completion"], "turns": r["n"] or 0} for r in completion_rows]

    # limits（「打ち切りの内訳」・経路別・行=provider の list）。`*_turns`＝回数系は1回以上・bool系は真だったターン数、`*_total`＝回数系の合計回数。
    by_provider_limits = [_usage_limits_provider_row(r) for r in limits_rows]
    limits_stats = {"by_provider": by_provider_limits}

    # 定着指標: JST 週（月曜始まり）ごとのアクティブユーザー集合→週次人数の推移＋連続週ペアの再訪率。
    den = sum(r["active_users"] for r in retention_rows if r["has_next"])
    num = sum(r["revisited"] for r in retention_rows if r["has_next"])
    retention = {"weekly": [{"week_start": r["week_start"].isoformat(), "active_users": r["active_users"]}
                            for r in retention_rows],
                 "revisit_rate": (num / den) if den > 0 else None}

    download_daily = [{"date": str(r["date"]), "count": r["n"] or 0} for r in download_daily_rows]
    downloads = {"total": sum(r["count"] for r in download_daily), "daily": download_daily}

    # トークン使用量（provider/model 別・上位ユーザー別・日別）。金額換算はしない。
    token_by_model = [{"provider": r["provider"] or "unknown", "model": r["model"] or "",
                       "turns": r["turns"] or 0, "input": int(r["input"] or 0),
                       "cached_input": int(r["cached_input"] or 0), **_cache_write_fields(r),
                       "output": int(r["output"] or 0),
                       "reasoning_output": int(r["reasoning_output"] or 0)}
                      for r in token_model_rows]
    token_by_user = [{"uid": r["uid"], "display_name": display_names.get(r["uid"]) or r["uid"],
                      "turns": r["turns"] or 0, "input": int(r["input"] or 0),
                      "cached_input": int(r["cached_input"] or 0), **_cache_write_fields(r),
                      "output": int(r["output"] or 0),
                      "reasoning_output": int(r["reasoning_output"] or 0)}
                     for r in token_user_rows]
    token_daily = [{"date": str(r["date"]), "input": int(r["input"] or 0), "output": int(r["output"] or 0)}
                   for r in token_daily_rows]
    # 用途別（kind）内訳。chat 行は token_by_model から合成し、usage_events 由来の行と結合する。usage_events 側の全 NULL 合計はそのまま None（0 に丸めない）。chat 行は elapsed_ms を持たないため elapsed_n=0・total/avg は None。
    token_by_kind = [{"kind": "chat", "provider": m["provider"], "model": m["model"], "calls": m["turns"],
                      "input": m["input"], "cached_input": m["cached_input"],
                      "cache_write": m["cache_write"], "cache_write_unknown": m["cache_write_unknown"],
                      "output": m["output"], "reasoning_output": m["reasoning_output"],
                      "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0}
                     for m in token_by_model]
    token_by_kind += [{"kind": r["kind"], "provider": r["provider"] or "unknown", "model": r["model"] or "",
                       "calls": int(r["calls"] or 0),
                       "input": int(r["input"]) if r["input"] is not None else None,
                       "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
                       **_cache_write_fields(r),
                       "output": int(r["output"]) if r["output"] is not None else None,
                       "reasoning_output": (int(r["reasoning_output"]) if r["reasoning_output"] is not None
                                            else None),
                       "elapsed_ms_total": (int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None
                                            else None),
                       "elapsed_ms_avg": (float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None
                                          else None),
                       "elapsed_n": int(r["elapsed_n"] or 0)}
                      for r in usage_event_rows]
    # ユーザー別×用途別（kind）内訳。chat 行は token_by_user と同じ材料から kind='chat' として合流し、それ以外は usage_event_user_kind_rows 由来（user_id が NULL の行は含まない）。
    token_by_user_kind = [{"uid": r["uid"], "display_name": display_names.get(r["uid"]) or r["uid"],
                          "kind": "chat", "calls": r["turns"] or 0, "input": int(r["input"] or 0),
                          "cached_input": int(r["cached_input"] or 0), **_cache_write_fields(r),
                          "output": int(r["output"] or 0),
                          "reasoning_output": int(r["reasoning_output"] or 0),
                          "elapsed_ms_total": None, "elapsed_ms_avg": None, "elapsed_n": 0}
                         for r in token_user_rows]
    token_by_user_kind += [{"uid": r["uid"], "display_name": display_names.get(r["uid"]) or r["uid"],
                           "kind": r["kind"], "calls": int(r["calls"] or 0),
                           "input": int(r["input"]) if r["input"] is not None else None,
                           "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
                           **_cache_write_fields(r),
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
            "cache_write": (sum(r["cache_write"] or 0 for r in token_by_model)
                            if any(r["cache_write"] is not None for r in token_by_model) else None),
            "cache_write_unknown": sum(r["cache_write_unknown"] for r in token_by_model),
            "output": sum(r["output"] for r in token_by_model),
            "reasoning_output": sum(r["reasoning_output"] for r in token_by_model),
        },
        "by_model": token_by_model, "by_user": token_by_user, "daily": token_daily,
        "by_kind": token_by_kind, "by_user_kind": token_by_user_kind,
    }

    for conv in conversations_top:
        conv["display_name"] = display_names.get(conv["uid"]) or conv["uid"]

    # 巡別記録（表示専用）の深さ×経路別集計＋理由コードの主張単位分布（巡別＝chat-round の meta 由来／最終＝最終回答の data.claims 由来）。課金集計（tokens.*）とは独立。
    rounds_stats["reason_codes"] = _round_reason_codes(rounds_stats, final_reasons)
    _merge_final_missing_agg(rounds_stats, final_missing)

    return {
        "users": users, "totals": totals, "daily": daily, "period": period,
        "zero_hit": zero_hit, "worlds": worlds_usage, "providers": providers_usage,
        "heatmap": heatmap, "retention": retention, "downloads": downloads, "tokens": tokens,
        "conversation_turns": conversation_turns, "resume_rate": resume_rate,
        "stop_kinds": stop_kinds, "stopped_turns": stopped_turns, "completions": completions,
        "response_time": response_time, "limits": limits_stats,
        "conversations_top": conversations_top,
        "rounds": rounds_stats,
        "quality_runs": depth_quality_stats(days, time_from=time_from, time_to=time_to),
    }


def usage_export_turns(days: int = 30, *, time_from: str | None = None, time_to: str | None = None) -> list[dict]:
    """管理者の利用明細エクスポート（ZIP）用: 期間内の回答（assistant 返答）1件=1行の生データ。
    母集団・期間境界は `usage_stats()` と同じ（`usage_turns`＝`c.origin='own'`・`deleted_at IS NULL`・境界は質問の `turn_created_at`・1 ターンの最初の返答）。返答の無い user 発言は除く。数字は画面の集計と同じ `turn_metrics` から読む。
    `answer` は数字と閉じた語彙の欄だけを `turn_metrics` から組み直した JSON（本文・出典・主張は DB から読まない）。
    """
    _ensure()
    start_ts, end_exclusive_ts, _period = _usage_period(days, time_from=time_from, time_to=time_to)
    limits_obj = ", ".join(f"'{f}', tm.{f}" for f in _TURN_LIMIT_FIELDS)
    usage_obj = ", ".join(
        f"'{f}', tm.{f}" for f in ("provider", "model") + _TURN_TOKEN_FIELDS)
    with _connect() as c:
        _build_usage_turns(c, start_ts, end_exclusive_ts)
        rows = c.execute(
            "SELECT ut.conversation_id, ut.message_id, am.created_at AS message_created_at, "
            "  ut.user_id AS uid, ut.lens, "
            "  jsonb_build_object("
            "    'usage', jsonb_build_object(" + usage_obj + "), "
            "    'limits', jsonb_build_object(" + limits_obj + "), "
            "    'activity', tm.activity_json, 'stop_kind', tm.stop_kind, "
            "    'codex_error_code', tm.codex_error_code, 'duration_ms', tm.duration_ms, "
            "    'investigation', jsonb_build_object("
            "      'complete', tm.investigation_complete, "
            "      'continuations', tm.investigation_continuations, "
            "      'counts', tm.investigation_counts)) AS answer "
            "FROM usage_turns ut JOIN turn_metrics tm ON tm.message_id = ut.message_id "
            "JOIN messages am ON am.id = ut.message_id "
            "ORDER BY ut.conversation_id, ut.message_id",
        ).fetchall()
    return [dict(r) for r in rows]


def usage_export_aux_calls(days: int = 30, *, time_from: str | None = None,
                           time_to: str | None = None) -> list[dict]:
    """管理者の利用明細エクスポート（ZIP）用: 期間内の `usage_events`（査読の巡別記録を含む）1件=1行の生データ。会話所属では絞らず、境界はイベント自身の `ts` のみ。`world` 列は取り込みフォルダの名前になり得るため選ばない。"""
    _ensure()
    start_ts, end_exclusive_ts, _period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        rows = c.execute(
            "SELECT ts, kind, provider, model, input_tokens, cached_input_tokens, cache_write_tokens, "
            "  output_tokens, reasoning_output_tokens, calls, elapsed_ms, user_id AS uid, conversation_id "
            "FROM usage_events WHERE ts >= %s AND ts < %s ORDER BY ts",
            (start_ts, end_exclusive_ts),
        ).fetchall()
    return [dict(r) for r in rows]


# 品質採点の入口。ここは採点結果の集計済みカウントだけを受け取って積む/読む（質問文・回答本文は持たない）。
_QUALITY_COUNT_FIELDS = ("correct", "wrong_assertion", "missing", "regressed", "unrated")

# 採点した条件の閉集合。`main`＝見直しの無い AP、`depth2-*`＝見直しを持つ AP の深さ別（`depth2-quick` が見直し 0 巡・`depth2-standard` は見直し 2 巡）。
QUALITY_RUN_CONDITIONS = ("main", "depth2-quick", "depth2-standard", "depth2-deep", "depth2-max")


def record_depth_quality_run(rounds, counts: dict | None, *, condition: str,
                             executed_from: str, executed_to: str,
                             cost_usd: float | None = None,
                             ts=None, run_id: str | None = None,
                             audit_actor: str | None = None) -> bool:
    """1採点ラン分の集計済みカウントを1行 INSERT する。
    `rounds`: 比較した巡数（0以上）。`counts`: `_QUALITY_COUNT_FIELDS` の一部/全部（欠落キーは0・非負整数以外は0に丸める）。`condition`: `QUALITY_RUN_CONDITIONS` のいずれか（閉集合外は `ValueError`）。`executed_from`/`executed_to`: 質問セットを実行した期間（ISO 8601・オフセット必須・`[from, to)`・集計はこの期間で照会する）。`cost_usd`: 任意（inf/nan・負値・非数値は None へ丸める）。`ts` はテスト用（省略時は DB の `now()`）。
    `run_id`（省略可）: 冪等キー。同じ `run_id` の再送は 2 行目を作らない（None は毎回新規行）。
    `audit_actor`（省略可）: 指定すると、この INSERT と同一トランザクションで監査ログ（`admin.usage_quality_run_recorded`）も書く（監査が失敗すれば INSERT も rollback）。`run_id` 重複でスキップした場合は監査も書かない。
    戻り値: 新規行を作ったら True、`run_id` 重複でスキップしたら False。
    """
    _ensure()
    if condition not in QUALITY_RUN_CONDITIONS:
        raise ValueError(f"condition は {'/'.join(QUALITY_RUN_CONDITIONS)} のいずれかで指定してください")
    exec_from = _parse_period_bound(executed_from, "executed_from")
    exec_to = _parse_period_bound(executed_to, "executed_to")
    if exec_from >= exec_to:
        raise UsagePeriodError("executed_from は executed_to より前の日時で指定してください")
    if exec_to - exec_from > timedelta(days=_USAGE_PERIOD_MAX_DAYS):
        # 照会期間の上限（365日）を超える実行期間は集計に現れないため、登録させない。
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
    from sherpa import store as _facade
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
    """`record_depth_quality_run` が積んだ集計済みカウントを条件×巡数別に合算する（既定180日）。
    母集団は実行期間（`executed_from`/`executed_to`）が照会期間に完全に含まれる採点ラン（`from <= executed_from AND executed_to <= to`・登録時刻 `ts` では絞らない）。実行期間を持たない行は入らない。
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
