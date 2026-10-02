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
_USAGE_KNOWN_PROVIDERS = ("heuristic", "codex", "openai", "gemini", "bedrock", "ollama", "simple")


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


# ---- 旧ターン対応付け CTE（`messages.answer` JSON を読む方式）----
# 本番の集計は `turn_metrics` を読む `_build_usage_turns` に移行済み。下の `_USAGE_TURN_CTE`・`_usage_tok`・
# `_usage_token_sum_cols`・`_USAGE_TOKEN_WHERE` は、移行前後の同値性を確かめるテスト
# （tests/api/test_usage_stats.py・test_usage_turn_metrics.py）が基準として使うためだけに残す
# （本番コードからは呼ばない）。
#
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
#
# `message_id`/`message_created_at`（assistant 側の messages.id/created_at）は、既存の集計
# クエリはどれも SELECT リストに明示していないため無害な追加列——利用明細エクスポート
# （`usage_export_turns`）が回答番号として使う。
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


# SQL集計後、経路別定義で計測項目と未計測項目を分けてAPIへ返す。
# イベントが一度も書かれない項目も、計測対象なら集計値0を保つ。
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
    # API 集計対象の provider 名を明示する。未知の provider は計測対象にしない。
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


# ---- turn_metrics 版の集計部品（`usage_stats` 専用） ----
#
# 1 ターン＝期間内の user 発言 1 件＋その最初の assistant 返答の `turn_metrics` 行。ターンは
# リクエストごとに 1 回だけ一時表 `usage_turns` へ組み立て、各集計はこの表だけを読む。
# `messages.answer`（JSONB・TOAST）には触れない: ターンの組み立ては `messages` の細い列
# （id/会話/時刻/personal）と `turn_metrics` の結合だけで済み、回答 JSON を述語に使う旧方式
# （ターン数×assistant 行数の入れ子ループで展開が走る）を避ける。
#
# 対応付けは `turn_metrics.user_message_id`（保存時に「直近の先行 user 発言」で決まる）で引き、
# 1 ターンに複数返答があれば `message_id` が最小のもの（旧 CTE の「最初の assistant」と同じ）。
# `personal_turns` は user 発言の `messages.personal`（`turn_metrics.personal` は回答側の別物）。
# トークンは `turn_metrics` の値（activity 優先）＝失敗ターンの実消費も数える。
_TURN_LIMIT_FIELDS = _USAGE_LIMIT_INT_FIELDS + _USAGE_LIMIT_BOOL_FIELDS
_TURN_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


# `usage_turns` から終了理由（`stop_kind.STOP_KINDS` の閉じた語彙）の分布を引く SQL。返答が存在するターン
# （`message_id IS NOT NULL`）のうち確認カード（`lens='clarify'`）と利用者停止（`stopped_by_user`＝
# `stopped_turns` 側で別に数える）を除き、語彙外・NULL は 'unknown' へ畳み込む。
_STOP_KINDS_SQL = (
    "SELECT CASE WHEN stop_kind = ANY(%s) THEN stop_kind ELSE 'unknown' END AS stop_kind, COUNT(*) AS n "
    "FROM usage_turns "
    "WHERE message_id IS NOT NULL "
    "  AND lens IS DISTINCT FROM 'clarify' "
    "  AND stop_kind IS DISTINCT FROM 'stopped_by_user' "
    "GROUP BY 1 ORDER BY n DESC"
)


def _usage_read_tuning(c) -> None:
    """この集計トランザクションの間だけ（`SET LOCAL`）ソート・ハッシュ集計の作業メモリを広げる——
    一時表への束ねが既定値（4MB）を超えてディスクへ溢れるのを避ける。管理者の集計画面専用の短い読み取り。"""
    c.execute("SET LOCAL work_mem = '64MB'")


def _build_usage_turns(c, start_ts, end_exclusive_ts, *, uid: str | None = None,
                       with_next: bool = False) -> None:
    """期間 `[start_ts, end_exclusive_ts)`（user 発言の created_at 基準）のターンを一時表
    `usage_turns` へ作る（呼び出しトランザクションの終了で消える）。`uid` を渡すとその利用者のターン
    だけ。`with_next` は同会話の次の user 発言の時刻（`next_user_created_at`・期間の上限を越えた発言も
    見る）も持たせる＝巡（`_build_usage_rounds`）を所属ターンへ結ぶための区間の終端。`turn_jst` は発言時刻の
    JST 壁時計（日付・曜日・時・週の集計が各自で時刻帯変換しないよう 1 回だけ計算する）。"""
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_turns")
    cols = ", ".join(
        f"tm.{f}" for f in ("message_id", "lens", "provider", "model", "depth_profile", "stop_kind",
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


def _compute_retention(week_user_rows) -> dict:
    # 参照実装（本番は SQL＝`usage_stats` の retention_rows）。テストが基準として使う。
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


# ---- 分布・巡集計の参照実装（Python） ----
# 本番の集計は SQL（`percentile_cont`/`percentile_disc`・`_round_stats_from_sql`・`_final_claims_from_sql`）が
# 担う。次の `_percentile`・`_compute_conversation_turn_stats`・`_compute_response_time_stats`・
# `_compute_round_stats`・`_compute_final_*` は、SQL 集計の定義（最近傍順位の p90・偶数件の中央値・
# 巡の型規則）を表す参照実装で、SQL との一致を確かめるテストが基準として使う（本番コードからは呼ばない）。
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
    conversation_turns に session_eligible（分母）と session_recorded（分子）を含める。
    セッションIDは現在の保存状態であり、再開を試行・成功した記録ではない。
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
    """最終回答の主張のうち不明（`status='unknown'`）の理由コードを、深さ×経路×理由コードで
    合算する（主張単位）。行の `unknown_reasons` は `turn_metrics.claims_unknown_reasons`
    （理由コード→件数）。"""
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


# `usage_stats()`/`usage_depth_rounds()` が共有する `chat-round` の集計。
#
# 期間境界は「巡が属する user ターン」の `created_at`（他集計と同じ `usage_turns` の境界）を使う——
# 巡イベント自身の `ts` で絞ると、同じユーザーターンの巡別記録と最終回答が異なる期間境界に割れ、
# 両者を突き合わせる集計（reason_codes の final/rounds 比較）の母集団がずれる。所属ターンは
# 「その巡の ts 以前で最も新しい同会話の user 発言」＝`usage_turns` の区間
# `[turn_created_at, next_user_created_at)`（`_build_usage_turns(with_next=True)`）に ts が入るターン。
# 区間は同会話内で重ならない。期間内に所属ターンを持たない巡（期間外のターン・user 発言の無い会話・
# 削除済み/内部成果物の会話）は数えない。
#
# 所属ターンの返答（`turn_message_id`）は、そのターンの最初の返答（`usage_turns.message_id`）。
# 返答が保存されていないターン（停止等）の巡は `unmatched_rounds` に計上するだけで分布には数えない。
def _build_usage_rounds(c, start_ts) -> None:
    """`usage_turns`（`with_next=True` で構築済み）を前提に、期間内の `chat-round` を 1 回の走査で 2 通りに
    束ねて一時表 `usage_rounds` へ作る（呼び出しトランザクションの終了で消える）。
    `per_turn=false` の行＝（深さ, 経路, 集計に使う meta の欄）が同じ巡の束（巡数 `w`・所要時間・トークンの
    合計）。束ねる欄は meta から**集計に使う欄だけ**を生の JSONB のまま取り出したもの（巡ごとに値の
    違う欄＝`roles`・`lens` 等は束ねる単位に入れない＝同じ値の巡が束ねられる）。`per_turn=true` の行＝
    所属ターン（深さは 1 ターンで一定・経路は
    照合順序に依らない最小値）ごとの到達巡数（`max_round`）。型の判定・合算は束ねた後の行（値の種類の数だけ）に対して
    行う＝コストは巡の数でなく値の種類に比例する。
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


# 束ねた巡（`per_turn=false`）の欄を型付きにする。型が合わない値（数でない・オブジェクトでない等）は
# NULL＝数えない。
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
    """SQL の合計（numeric）を JSON 向けの数へ: 整数値なら int・小数なら float。"""
    if v is None:
        return 0
    return int(v) if v == v.to_integral_value() else float(v)


def _round_stats_from_sql(c) -> dict:
    """一時表 `usage_rounds`（`_build_usage_rounds`）を SQL で集計し、`_compute_round_stats` と同じ形の
    dict を返す。巡の行は Python へ引かない: 深さ×経路×巡番号ごとの合計（巡数・引用増分・所要時間・
    トークン・主張の区分）・理由コード/limits/verdict/stop/不足軸の分類別合計・到達巡数の分布を
    すべて SQL が返し、Python は返った行を応答の形へ並べるだけ。型の規則は `_accumulate_round` と
    同じ（主張件数・理由コード件数は数値のみ・limits の bool は真の巡数・数値は合計・
    verdict/stop/不足軸は空でない文字列のみ）。保存済みの `chat-round` は `meta` が書込側の型どおり
    であることを前提にする（欠落・NULL は数えない）。
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

    # 深さ×経路の合計は巡番号別の群を足し上げる（群の数だけの小さな合算）。
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


# 最終回答の不明理由コード（`turn_metrics.claims_unknown_reasons`）と最終ゲートの不足軸
# （`gate_missing_codes`）を、組み立て済みの一時表 `usage_turns`（`_build_usage_turns`）から
# 取る（`usage_stats()`/`usage_depth_rounds()` が共有）。主張を持つターン（`claims_unknown_reasons`
# が NULL でない＝回答に `data.claims` 配列がある）だけが対象。`gate_missing_codes`（Codex 経路の
# 最終ゲート）は Codex が `chat-round` を発生させないため、巡別記録（`_build_usage_rounds`）には
# 不足軸が載らず、ここが唯一の取得点になる（API 経路は NULL のまま＝`_round_stats_from_sql` 側の
# `missing_codes` 集計と二重計上にならない）。ターンの行は Python へ引かず、深さ×経路×コードの
# 合計だけを SQL が返す（規則は `_compute_final_reason_codes`/`_compute_final_missing_codes` と同一）。
def _final_claims_from_sql(c) -> tuple[list[dict], dict[tuple, dict[str, int]]]:
    """`(最終回答の不明理由コード分布, 最終ゲートの不足軸の深さ×経路別合算)`。"""
    # 同じ値のターンを先に 1 回の走査で束ね（ターン数 `w`）、束ねた行だけを展開して `w` 倍で足す＝
    # コストはターン数でなく値の種類に比例する。
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
    # 空のオブジェクト（最終ゲートはあるが不足軸が無い）も「その深さ×経路のバケットが在る」印として返す
    # （k.key が NULL の行）。
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


def _compute_final_missing_codes(final_claims_rows) -> dict[tuple, dict[str, int]]:
    """S1b: 最終回答の `evidence_gate.missing_codes`（Codex 経路の最終ゲート・巡を発生させない
    ため `chat-round` には載らない）を深さ×経路で合算する。行の `gate_missing_codes` は
    `turn_metrics.gate_missing_codes`（コード→件数）。NULL（API 経路・v1・旧データ）は静かに
    スキップする。
    """
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
    """S1b: 最終回答由来の不足軸（`_compute_final_missing_codes`）を、既存の巡別集計
    （`rounds_stats["by_depth_provider"]`・表示は `web/usage.js::reviewCounts(r.missing_codes)`
    のまま＝新しい表は作らない）へ深さ×経路で合流させる。Codex は `chat-round` を発生させない
    ため、既存集計に対応するバケットが無い（深さ, "codex"）組は新規バケットとして追加する
    （`rounds` 等の巡別指標は0のまま＝Codex 側にその意味の値が無いことを表す）。
    """
    _merge_final_missing_agg(rounds_stats, _compute_final_missing_codes(final_claims_rows))


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


# ---- チャット以外の LLM 呼び出し（`usage_events`）の期間内集計 ----

# `usage_ev` を（用途 kind・経路・モデル・利用者・会話）で束ねた行から、`keys` 単位の合計を取る列。
# 全 NULL の合計（報告不能マーカーのみのグループ）は None のまま保つ（0 に丸めない）。平均所要時間は
# 計測のあった呼び出し（`elapsed_n`）あたり（`AVG(elapsed_ms)` と同じ numeric 除算）。
_EV_KIND_COLS = (
    "{keys}, SUM(calls) AS calls, SUM(input) AS input, SUM(cached_input) AS cached_input, "
    "SUM(output) AS output, SUM(reasoning_output) AS reasoning_output, "
    "SUM(elapsed_ms_total) AS elapsed_ms_total, "
    "SUM(elapsed_ms_total) / NULLIF(SUM(elapsed_n), 0) AS elapsed_ms_avg, "
    "COALESCE(SUM(elapsed_n), 0) AS elapsed_n"
)


def _build_usage_events(c, start_ts, end_exclusive_ts, *, conv_only: bool = False) -> None:
    """期間内の `usage_events`（`chat-round`＝査読の巡別記録は正本と二重に足さないため除く）を、
    用途 kind・経路・モデル・利用者・会話ごとに束ねて一時表 `usage_ev` へ作る（`usage_events` の走査は
    ここで 1 回だけ・kind 別／利用者×kind 別／会話別の集計はすべてこの表から取る）。`conv_only` は
    `usage_conv`（`_build_usage_conversations`）の会話に属する行だけ（会話別の表のための絞り込み）。"""
    conv_filter = " AND conversation_id IN (SELECT cid FROM usage_conv)" if conv_only else ""
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_ev")
    c.execute(
        "CREATE TEMP TABLE usage_ev ON COMMIT DROP AS "
        "SELECT user_id, conversation_id, kind, provider, model, SUM(calls) AS calls, "
        "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
        "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output, "
        "  SUM(elapsed_ms) AS elapsed_ms_total, COUNT(elapsed_ms) AS elapsed_n "
        "FROM usage_events WHERE ts >= %s AND ts < %s AND kind <> 'chat-round'" + conv_filter + " "
        "GROUP BY user_id, conversation_id, kind, provider, model",
        (start_ts, end_exclusive_ts),
    )


# ---- 回答時間・会話単位の集計（SQL 側で完結・Python は返った行を応答の形へ並べるだけ） ----

# 回答時間（ミリ秒）の分布を SQL で求める列。平均は合計/件数の倍精度除算、中央値は線形補間
# （偶数件は中央 2 値の平均）、p90 は最近傍順位（`ceil(0.9 * n)` 番目の値・補間なし）。
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


# 回答時間の母集団: 期間内（回答の created_at）の assistant 返答のうち、他の集計と同じ会話
# （origin='own'・deleted_at IS NULL）で、確認カード（回答前の一時停止＝回答時間ではない）でなく、
# 所要時間が記録された行。
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
    """`usage_turns` から 1 会話 1 行の一時表 `usage_conv`（会話・利用者・資料フォルダ・期間内の user ターン数・
    chat のトークン合計・回答時間の平均）を作る。"""
    c.execute("DROP TABLE IF EXISTS pg_temp.usage_conv")
    c.execute(
        "CREATE TEMP TABLE usage_conv ON COMMIT DROP AS "
        "SELECT conversation_id AS cid, user_id AS uid, version AS world, codex_session_id, "
        "  COUNT(*) AS user_turns, "
        "  COUNT(*) FILTER (WHERE input_tokens IS NOT NULL) AS chat_calls, "
        "  SUM(input_tokens) AS chat_input, SUM(cached_input_tokens) AS chat_cached_input, "
        "  SUM(output_tokens) AS chat_output, SUM(reasoning_output_tokens) AS chat_reasoning_output, "
        "  AVG(duration_ms) FILTER (WHERE lens IS DISTINCT FROM 'clarify') AS avg_response_time_ms "
        "FROM usage_turns GROUP BY conversation_id, user_id, version, codex_session_id"
    )


def _conversation_turn_stats_from_sql(c) -> tuple[dict, float | None]:
    """会話あたりの user ターン数分布（avg/median/max/p90）と resume_rate・session_eligible/recorded
    （`usage_stats().conversation_turns`/`resume_rate`）。対象は `usage_conv`＝期間内に user ターンが
    1 件以上ある会話。user ターン数 2 以上の会話のうち `codex_session_id` が設定された割合が
    resume_rate（分母 0 なら None）。"""
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
    """`usage_events` の kind 別集計行（calls/tokens/elapsed）を応答の形へ。全 NULL の合計は 0 に丸めず
    None のまま（報告不能と 0 を区別する）。"""
    return {
        "kind": r["kind"], "calls": int(r["calls"] or 0),
        "input": int(r["input"]) if r["input"] is not None else None,
        "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
        "output": int(r["output"]) if r["output"] is not None else None,
        "reasoning_output": int(r["reasoning_output"]) if r["reasoning_output"] is not None else None,
        "elapsed_ms_total": int(r["elapsed_ms_total"]) if r["elapsed_ms_total"] is not None else None,
        "elapsed_ms_avg": float(r["elapsed_ms_avg"]) if r["elapsed_ms_avg"] is not None else None,
        "elapsed_n": int(r["elapsed_n"] or 0),
    }


def _conversations_top_from_sql(c, *, sort: str = "tokens", limit: int = 20) -> list[dict]:
    """会話ごとの補助 AI 使用量の上位（`usage_stats().conversations_top`・`usage_conversations`）。
    `usage_conv`（`_build_usage_conversations`）を土台に、chat 以外の kind（`usage_ev`＝期間内の
    `usage_events`・`chat-round` を除く）を会話単位で合流し、並び（tokens＝chat と kind の input+output の合算・報告不能
    は 0 扱い／turns＝期間内の user ターン数／elapsed＝kind の所要時間合計）の上位 `limit` 件を SQL で
    選ぶ。同値は会話 id の昇順。`usage_events.conversation_id` が NULL の行はどの会話にも合流しない。
    返す各会話は `kinds`（chat＋kind 別・input+output の降順）を持つ。"""
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

    `time_from`/`time_to`（ISO 8601・オフセット必須）を渡すと `days` の代わりに `[from, to)` を
    期間に使う（`_usage_period` 参照）。規則違反は `UsagePeriodError`。

    users: ターン数（role='user' メッセージ数）降順。totals: 期間合計。
    daily: 日別ターン数＋日別アクティブユーザー数。
    period: 集計対象の JST 暦日範囲（start/end・フロントの日別チャートはこの範囲でゼロ埋め描画する）。

    lens 内訳・personal 利用ターン数は「各 user ターンに対応する最初の assistant 返答」だけを数える
    （`_build_usage_turns` 参照・assistant 単独行の混入防止）。

    active_days・daily の日付境界は **user メッセージのみ**を **JST（Asia/Tokyo）**で区切り、
    **`_usage_period_bounds` で計算した固定の JST 暦日下限**を users/daily/audit すべてに使う
    （表とグラフの合計を一致させる）。

    ターン由来の集計の読み元: user 発言（`messages`）と、その最初の返答の `turn_metrics` 行を
    リクエストごとに 1 回だけ一時表（`_build_usage_turns`）へ組み、各集計はそれだけを読む（回答 JSON
    は読まない）。トークンは `turn_metrics`（activity 優先）の値＝失敗ターンの実消費も数える。
    `turn_metrics` の無い返答（旧版で保存・書込失敗）は起動時の補完（`turn_metrics.backfill_missing`）
    が埋めるまで、そのターンの返答由来の項目（lens・トークン等）に現れない。

    集計の前提:
      - `c.origin='own'` に限定（sanitized_snapshot は本文コピー済みの内部成果物で、同じ owner の
        別 conversation として messages が二重に存在するため、含めると owner の turns/daily/active_days
        が水増しされる。received_share は自分名義の messages を持たないため実害は無いが明示的に除外）。
      - `conversations`/`active_days`/`last_active` は role='user' 基準に統一し、
        `HAVING` で「期間内に user turn が 0 件」の行（assistant のみ該当した見せかけの活動）を除外する。

    「利用の傾向」指標（既存の境界/origin/turn 規約を再利用・N+1 は
    避けるが単一クエリ主義ではない＝固定本数の追加クエリ）:
      - `zero_hit`（全体）／各 user 行の `knowledge_turns`/`zero_hit_turns`/`zero_hit_rate`:
        ナレッジ参照オンのターン（lens != 'chat'）のうち返答の根拠（`turn_metrics.sources_count`）が
        0 件の割合（既存の user_rows 集計に FILTER 列を追加するだけ＝新規クエリ無し）。
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

    `docs/archive/2026-09-12-利用統計の拡充2.md` §2/§3:
      - `tokens.by_user_kind`: ユーザー別 × 用途別（kind）の calls/tokens/elapsed_ms（`tokens.by_kind`
        と同じ材料・同じ扱い）。chat 行（`turn_metrics` 由来）は `token_by_user`
        （`user_rows` と同じ `usage_turns` 集計）から `kind='chat'` として合流し、それ以外の kind は
        `usage_events`（`user_id IS NOT NULL` のみ＝集計できない匿名呼び出しは含めない）を
        `user_id, kind` で集計する。`by_kind` と同じ内訳を利用者ごとに分けた形だが、user_id が
        NULL の行（取り込み時の埋め込み・画像読み取り・rag_render 等の利用者に紐付かない呼び出し）は
        含まれないため、同一 kind の合計は `by_kind` の当該行**以下**になりうる。並びは `(uid, kind)`。
      - `response_time`: 期間内の assistant 行（`c.origin='own'・deleted_at IS NULL`＝他の集計と
        同じ母集団）の `turn_metrics.duration_ms`（1ターンの壁時計所要時間）から、全体（`overall`）と
        経路別（`by_provider`＝`turn_metrics.provider`・取れなければ `'unknown'`）の
        avg/median/p90/max/件数を SQL で計算する（`_RESPONSE_TIME_COLS`・平均は合計/件数・
        中央値は偶数件で中央 2 値の平均・p90 は最近傍順位法＝`_percentile`/`_compute_response_time_stats`
        と同じ定義）。利用者の明示停止・実行中のターンは assistant を保存しないため対象に含まれず、
        所要時間が無い行・確認カードは除く（0件なら avg/median/p90/max=None・n=0）。
      - `conversations_top`: 期間内に user ターンが1件以上ある会話（`conversation_turns`/`resume_rate`
        と同じ母集団）について、会話 id・uid・world・user ターン数・用途別（kind）内訳
        （`kinds`＝chat は `usage_turns` の `turn_metrics` 合計・他は `usage_events` を
        `conversation_id` で集計・null の意味は `tokens.by_kind` と同じ）・回答時間の平均
        （`duration_ms` が無い行は除外）を、トークン合計（`kinds` 内の input+output の合算・
        報告不能＝None は合算時のみ0扱い）の降順で上位20件。`usage_events.conversation_id` が
        NULL の行（列追加前の過去データ・遡及なし）はどの会話にも合流しない
        （`usage_conv` の対象会話に結合できないため）。上位の選択は SQL（`_conversations_top_from_sql`）。
        タイトル・本文は含まない。

    ターンの終了理由:
      - `stop_kinds`: `turn_metrics.stop_kind`（`sherpa/stop_kind.py` の閉じた8値・
        `chat_service._finalize` が保存した回答から写す）の分布。assistant 返答が存在するターン
        （`message_id IS NOT NULL`）のみを対象にする。利用者の明示停止は `stopped_turns` 側だけで数える（二重計上しない）——
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
        （`_build_usage_rounds`）——巡イベント自身の `ts` が `to` を越えていても、所属する user 発言が
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
        # 終了理由（`usage_turns.stop_kind`・`stop_kind.py` の8値）の分布。`usage_turns`
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
        stop_kind_rows = c.execute(_STOP_KINDS_SQL, (list(stop_kind.STOP_KINDS),)).fetchall()
        # limits（「打ち切りの内訳」・経路別）: `stop_kind_rows` から `stopped_by_user` の除外だけを
        # 外した population（turn_created_at 境界・answer IS NOT NULL・clarify 除外——巡ループの
        # 停止終端が保存した行も含む＝停止ターンで当たった制限も内訳に残す）に
        # `answer->'usage'->>'provider'` 別の集計を足す
        # （`token_by_model` と同じ provider 抽出キー・専用フィールドは持たない＝重複させない）。
        limits_rows = c.execute(
            "SELECT COALESCE(provider, 'unknown') AS provider, "
            "  COUNT(*) AS turns, " + _turn_limits_select_cols() + " "
            "FROM usage_turns "
            "WHERE message_id IS NOT NULL "
            "  AND lens IS DISTINCT FROM 'clarify' "
            "GROUP BY 1 ORDER BY turns DESC, provider",
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
            "SELECT EXTRACT(DOW FROM turn_jst)::int AS weekday, "
            "  EXTRACT(HOUR FROM turn_jst)::int AS hour, "
            "  COUNT(*) AS n "
            "FROM usage_turns GROUP BY weekday, hour",
        ).fetchall()
        # 定着指標: 週ごとのアクティブ人数と、翌週（7 日後）も続けた人数を SQL が返す（週の行だけ）。
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
        # トークン使用量（answer->'usage'）を provider/model 別・上位ユーザー別・日別で集計。
        #   入力/出力トークン数のみ集計する（金額換算はしない）。
        #   usage を持たないターン（heuristic・停止・旧データ）は自然に除外。
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
        # チャット以外の LLM 呼び出し（intent 分類・
        # グラフ抽出・概念候補提案・埋め込み・admin グラフ質問・VLM）を kind 別に集計。usage_events は
        # kind='chat' を含まない（chat は token_model_rows 由来で別途合成する・二重計上なし）。
        # kind='chat-round'（査読の巡別記録）も除く——巡の消費は既に
        # `chat-sub`（worker）・`chat-review`（evaluator/orchestrator）・`answer.usage`（清書）
        # として正本に載っており、巡別記録は表示・分析用の別イベントで二重に足さない。
        # elapsed_ms は計測スコープ外の行（NULL）を
        # 自然に除いて集計する（SUM/AVG は NULL を無視・COUNT(列) は非 NULL 行数＝`elapsed_n`）。
        _build_usage_events(c, start_ts, end_exclusive_ts)
        usage_event_rows = c.execute(
            "SELECT " + _EV_KIND_COLS.format(keys="kind, provider, model") + " "
            "FROM usage_ev GROUP BY kind, provider, model ORDER BY kind, input DESC NULLS LAST",
        ).fetchall()
        # ユーザー別 × 用途別（kind）内訳（usage_events 側）。`user_id IS NOT NULL` で
        # 絞る——匿名呼び出し（ext:等ユーザー本人以外・世界単位のバックグラウンド処理）は
        # どの利用者にも属さないため by_user_kind には出せない（by_kind 側では引き続き集計対象）。
        usage_event_user_kind_rows = c.execute(
            "SELECT " + _EV_KIND_COLS.format(keys="user_id AS uid, kind") + " "
            "FROM usage_ev WHERE user_id IS NOT NULL GROUP BY user_id, kind ORDER BY user_id, kind",
        ).fetchall()
        # 回答時間（`duration_ms`）の分布・会話あたりの user ターン数分布・resume_rate・会話ごとの
        # 補助 AI 使用量の上位。いずれも SQL が集計し、返るのは分布の値・上位件数の行だけ。
        response_time = _response_time_from_sql(c, start_ts, end_exclusive_ts)
        _build_usage_conversations(c)
        conversation_turns, resume_rate = _conversation_turn_stats_from_sql(c)
        conversations_top = _conversations_top_from_sql(c, limit=20)
        # 巡別記録（`chat-round`）の表示専用集計——深さ（`answer->'usage'->>'depth_profile'`）×
        # 経路（provider）別の巡数分布・活動量（引用増分・所要時間・トークン）・主張の区分/理由
        # コード内訳。期間境界・assistant 対応付けの規約は `_build_usage_rounds` 参照（他集計と同じ
        # 「所属する user ターン」基準の境界に揃える）。
        _build_usage_rounds(c, start_ts)
        rounds_stats = _round_stats_from_sql(c)
        # 最終回答の主張のうち不明（`status='unknown'`）の理由コード分布（主張単位）。深さ・経路
        # （`answer->'usage'`）別に数える——巡別（chat-round）の集計とは別軸（こちらは全巡を経た
        # 最終回答時点の判定・巡ごとの是正で覆った分は数えない）。
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

    for conv in conversations_top:
        conv["display_name"] = display_names.get(conv["uid"]) or conv["uid"]

    # 巡別記録（表示専用）の深さ×経路別集計＋理由コードの主張単位分布（巡別＝chat-round の
    # meta 由来／最終＝最終回答の data.claims 由来。二軸とも課金集計（tokens.*）とは独立＝
    # 正本に触れない）。
    rounds_stats["reason_codes"] = _round_reason_codes(rounds_stats, final_reasons)
    _merge_final_missing_agg(rounds_stats, final_missing)

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


def usage_export_turns(days: int = 30, *, time_from: str | None = None, time_to: str | None = None) -> list[dict]:
    """管理者の利用明細エクスポート（ZIP）用: 期間内の回答（assistant 返答）1件=1行の生データ。

    母集団・期間境界は `usage_stats()` と同じ（`usage_turns`＝`c.origin='own'`・`deleted_at IS NULL`・
    境界は質問（user 発言）の `turn_created_at`・1 ターンの最初の返答）。応答の無いユーザー発言
    （実行中・利用者停止等）は返答が無いため除く。数字（トークン・所要時間・打ち切り等）は画面の集計と
    同じ `turn_metrics` から読む（活動記録があればその値＝失敗ターンの実消費も数える）。

    `answer` は数字と閉じた語彙の欄だけを `turn_metrics` から組み直した JSON（本文・出典・主張は
    DB から読まない＝`scripts/turn_activity.py::_rows` と同じ規律）。
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
    """管理者の利用明細エクスポート（ZIP）用: 期間内の `usage_events`（チャット以外の LLM 呼び出し・
    査読の巡別記録を含む）1件=1行の生データ。

    `usage_stats()` の会話単位集計とは異なり会話所属で絞らない（`usage_events` は
    `conversation_id IS NULL` の行を持ちうる＝取り込み時の埋め込み等・遡及なし契約）——境界は
    イベント自身の `ts` のみ。`world` 列は意図的に選ばない（取り込みフォルダの名前になり得る）。
    """
    _ensure()
    start_ts, end_exclusive_ts, _period = _usage_period(days, time_from=time_from, time_to=time_to)
    with _connect() as c:
        rows = c.execute(
            "SELECT ts, kind, provider, model, input_tokens, cached_input_tokens, output_tokens, "
            "  reasoning_output_tokens, calls, elapsed_ms, user_id AS uid, conversation_id "
            "FROM usage_events WHERE ts >= %s AND ts < %s ORDER BY ts",
            (start_ts, end_exclusive_ts),
        ).fetchall()
    return [dict(r) for r in rows]


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
# docs/archive/2026-09-12-利用統計の拡充2.md §3b: 利用統計チャットの調査ツールが
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
    `tokens.by_user_kind`（U1）と同じ材料（chat は `turn_metrics`・他は
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
            _build_usage_turns(c, start_ts, end_exclusive_ts, uid=uid)
            for r in c.execute(
                "SELECT user_id AS uid, " + _turn_token_sum_cols() + " FROM usage_turns" + _TURN_TOKEN_WHERE +
                "GROUP BY user_id"
            ).fetchall():
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
        _usage_read_tuning(c)
        _build_usage_turns(c, start_ts, end_exclusive_ts, uid=uid)
        _build_usage_conversations(c)
        _build_usage_events(c, start_ts, end_exclusive_ts, conv_only=True)
        ordered = _conversations_top_from_sql(c, sort=sort, limit=limit)
    return _tool_json_projection({
        "period": period,
        "uid": uid, "sort": sort, "conversations": ordered})


# 会話1件の内訳（`usage_conversation_detail`）専用の turn 対応付け。各 user ターン（会話内の user 発言を
# id 順に数えた番号 `turn_no`）の最初の返答の `turn_metrics` 行を引く。
_CONV_DETAIL_TURNS = (
    "WITH turns AS ("
    "  SELECT ROW_NUMBER() OVER (ORDER BY u.id) AS turn_no, tm.lens, tm.provider, tm.duration_ms, "
    "    tm.input_tokens, tm.cached_input_tokens, tm.output_tokens, tm.reasoning_output_tokens "
    "  FROM messages u LEFT JOIN LATERAL ("
    "    SELECT t.* FROM turn_metrics t WHERE t.user_message_id = u.id ORDER BY t.message_id LIMIT 1) tm ON true "
    "  WHERE u.conversation_id = %s AND u.role = 'user'"
    ") "
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
            _CONV_DETAIL_TURNS +
            "SELECT COUNT(*) AS user_turns, "
            "  COUNT(*) FILTER (WHERE input_tokens IS NOT NULL) AS chat_calls, "
            "  SUM(input_tokens) AS chat_input, SUM(cached_input_tokens) AS chat_cached_input, "
            "  SUM(output_tokens) AS chat_output, SUM(reasoning_output_tokens) AS chat_reasoning_output "
            "FROM turns",
            (cid,),
        ).fetchone()
        response_rows = c.execute(
            _CONV_DETAIL_TURNS +
            "SELECT turn_no, duration_ms, provider FROM turns "
            "WHERE lens IS DISTINCT FROM 'clarify' AND duration_ms IS NOT NULL ORDER BY turn_no",
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
    sql = "SELECT " + _RESPONSE_TIME_COLS + " FROM (SELECT tm.duration_ms AS d " + _RESPONSE_TIME_FROM
    params = [start_ts, end_exclusive_ts]
    if provider:
        sql += " AND COALESCE(NULLIF(tm.provider, ''), 'unknown') = %s"
        params.append(provider)
    with _connect() as c:
        row = c.execute(sql + ") t", params).fetchone()
    stats = _response_time_stats(row)
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
            _build_usage_turns(c, start_ts, end_exclusive_ts)
            rows = c.execute(
                "SELECT turn_jst::date AS date, "
                "SUM(input_tokens) AS input, SUM(output_tokens) AS output "
                "FROM usage_turns" + _TURN_TOKEN_WHERE + "GROUP BY date ORDER BY date",
            ).fetchall()
            series = [{"date": str(r["date"]), "input": int(r["input"] or 0), "output": int(r["output"] or 0)}
                     for r in rows]
        else:   # response_time
            rows = c.execute(
                "SELECT (m.created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, "
                "  AVG(tm.duration_ms) AS avg_ms, COUNT(*) AS n " + _RESPONSE_TIME_FROM +
                " GROUP BY date ORDER BY date",
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
        _build_usage_turns(c, start_ts, end_exclusive_ts, uid=uid)
        rows = c.execute(_STOP_KINDS_SQL, (list(stop_kind.STOP_KINDS),)).fetchall()
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
        _usage_read_tuning(c)
        _build_usage_turns(c, start_ts, end_exclusive_ts, with_next=True)
        _build_usage_rounds(c, start_ts)
        rounds_stats = _round_stats_from_sql(c)
        final_reasons, _final_missing = _final_claims_from_sql(c)
    return _tool_json_projection({
        "period": period,
        "round_distribution": rounds_stats["round_distribution"],
        "unmatched_rounds": rounds_stats["unmatched_rounds"],
        "reason_codes": _round_reason_codes(rounds_stats, final_reasons),
        "quality": depth_quality_stats(days, time_from=time_from, time_to=time_to),
    })
