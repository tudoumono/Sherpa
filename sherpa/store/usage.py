"""利用統計（2026-07-02-利用統計とホーム掲示板.md Feature 1・admin 専用）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S3）。ロジックは一切変更していない。
不変条件: メッセージ本文・会話タイトルは一切 SELECT しない（件数・日時・種別のみ集計）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import _connect, _ensure

_USAGE_AUDIT_ACTIONS = ("auth.login", "document.downloaded", "workspace.file_uploaded", "share.created")
_JST = timezone(timedelta(hours=9))

# RV バッチ3再検証（2026-07-03）MEDIUM: chat.turn 監査の detail.provider は書込側
# （`chat_service._audit_chat_turn`・`sherpa.agents.AGENT_PROVIDERS`）で allowlist 正規化済みのはずだが、
# 過去の保存済み不正値（env 誤設定等）が残っている可能性があるため、集計（読み出し）側でも
# 同じ allowlist で畳み込む二重防御。store.py は他の sherpa.* を import しない設計
# （循環 import 回避・store は最下層）のため、`sherpa.agents.AGENT_PROVIDERS` の値をここに複製する。
# 値を変更したらそちらも合わせて確認すること。
_USAGE_KNOWN_PROVIDERS = ("heuristic", "codex", "openai", "gemini", "bedrock", "ollama")


def _usage_period_bounds(days: int):
    """JST の「(今日 − (days−1)) の日初」〜「明日の日初（排他的上限）」を期間として返す
    （RV ラウンド3 MEDIUM 対応・RV バッチ3再検証 MEDIUM: 上限も追加して全クエリで統一）。

    daily・users・totals・audit 由来集計の**全てが同じ境界**を使うことで、表（users）とグラフ（daily）の
    合計が常に一致するようにする（以前は `now() - make_interval(days)` のローリング境界で、
    フロントの「JST 暦日で days 個分」描画と食い違い、最古日の部分バケットが暗黙に drop されていた）。
    アプリサーバの UTC 時刻（`datetime.now(timezone.utc)`）から計算するため DB セッション timezone に
    依存しない（RV ラウンド1 MEDIUM と同じ理由）。

    下限のみで上限が無いと、クロックスキューやテスト由来の未来時刻行（`created_at` が「今日」より
    先）が「期間内」に混入してしまう（RV バッチ3再検証 MEDIUM）。`end_exclusive_ts`（「明日」の
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


# lens 内訳の対応付け（RV ラウンド3 MEDIUM 対応）: 「conversation 内で各 user メッセージの直後に来る
# 最初の assistant メッセージ」だけをその user ターンの返答として数える。assistant 単独行（対応する
# user メッセージが無い・または既に他の user メッセージの返答として数えられた2件目以降の assistant 行）は
# lens 内訳に混入させない。turn_no は「その行より前（自分を含む）に何件の user メッセージがあったか」の
# 累積カウントで、user 行と直後の assistant 行が同じ turn_no を持つことを利用してペアリングする。
# バッチ3（2026-07-03）: `answer`（JSONB）もペアリングして持ち回る＝ゼロヒット率（lens != 'chat' の
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


# F3（2026-07-07）: messages.answer->'usage' からトークン使用量を集計する SQL 断片。
# answer->'usage' は `{provider, model, input_tokens, cached_input_tokens, output_tokens,
# reasoning_output_tokens}`（agents._usage_meta）。想定外データ（非数値・欠落）は 0 に畳む
# （`~ '^[0-9]+$'` を先に確認してから ::bigint・zero_hit の非配列ガードと同じ防御思想）。
# フィールド名はコード内リテラル（ユーザー入力ではない）＝f-string 埋め込みは安全。
def _usage_tok(field: str) -> str:
    return (f"CASE WHEN (answer->'usage'->>'{field}') ~ '^[0-9]+$' "
            f"THEN (answer->'usage'->>'{field}')::bigint ELSE 0 END")


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
    （`{"uid", "week_start"}` の行・`week_start` は `date`）から計算する（バッチ3・2026-07-03）。

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


def usage_stats(days: int = 30) -> dict:
    """期間内の利用統計を集計する（本文/タイトルは含めない）。

    users: ターン数（role='user' メッセージ数）降順。totals: 期間合計。
    daily: 日別ターン数＋日別アクティブユーザー数（2026-07-02-利用統計とホーム掲示板.md Part2-A）。
    period: 集計対象の JST 暦日範囲（start/end・フロントの日別チャートはこの範囲でゼロ埋め描画する）。

    lens 内訳・personal 利用ターン数は「各 user ターンに対応する最初の assistant 返答」だけを数える
    （_USAGE_TURN_CTE 参照・RV ラウンド3 MEDIUM: assistant 単独行の混入防止）。

    active_days・daily の日付境界は **user メッセージのみ**を **JST（Asia/Tokyo）**で区切り、
    **`_usage_period_bounds` で計算した固定の JST 暦日下限**を users/daily/audit すべてに使う
    （RV ラウンド3 MEDIUM: 表とグラフの合計を一致させる）。

    RV ラウンド2 対応:
      - `c.origin='own'` に限定（sanitized_snapshot は本文コピー済みの内部成果物で、同じ owner の
        別 conversation として messages が二重に存在するため、含めると owner の turns/daily/active_days
        が水増しされる。received_share は自分名義の messages を持たないため実害は無いが明示的に除外）。
      - `conversations`/`active_days`/`last_active` は role='user' 基準に統一し、
        `HAVING` で「期間内に user turn が 0 件」の行（assistant のみ該当した見せかけの活動）を除外する。

    バッチ3（2026-07-03）で追加した「利用の傾向」指標（既存の境界/origin/turn 規約を再利用・N+1 は
    避けるが単一クエリ主義ではない＝固定本数の追加クエリ）:
      - `zero_hit`（全体）／各 user 行の `knowledge_turns`/`zero_hit_turns`/`zero_hit_rate`:
        ナレッジ参照オンのターン（lens != 'chat'）のうち assistant answer.sources が空の割合
        （_USAGE_TURN_CTE の `answer` を使い、既存の user_rows 集計に FILTER 列を追加するだけ＝新規クエリ無し）。
        `answer->'sources'` が NULL・欠落・非配列（想定外データ）でも 500 にしない
        （`jsonb_typeof(...)='array'` を先に確認してから `jsonb_array_length` を呼ぶ・
        RV バッチ3再検証 MEDIUM: 素朴な `COALESCE(jsonb_array_length(...), 0)` は非配列で例外になる）。
      - `heatmap`: user メッセージ数を JST 曜日(0=日〜6=土)×時間帯(0-23)で集計（sparse・0 件のセルは
        返さない＝フロントでゼロ埋め）。
      - `worlds`: `turns`（conversations.version）別ターン数の内訳（world が1つでも正直に1行返す）。
      - `providers`: `chat.turn` 監査の `detail->>'provider'` 別ターン数。書込側（S5・`AGENT_PROVIDERS`）で
        allowlist 正規化済みのはずだが、集計（読み出し）側でも同じ allowlist で畳み込む二重防御
        （`_USAGE_KNOWN_PROVIDERS`・RV バッチ3再検証 MEDIUM: Python 側の `or "unknown"` は NULL しか
        拾えず、allowlist 外の異なる不正値が別行のまま残ってしまっていた）。stopped ターンも
        `detail.stopped` に関わらず母数に含む（画面側で注記）。
      - `retention`: JST 週（Postgres `date_trunc('week', ...)` ＝月曜始まり）ごとのアクティブユーザー数の
        推移と、**連続する**週ペア（7日差のペアのみ・間が空いた週は「前週」として扱わない）をプールした
        再訪率（前週アクティブの延べ人数のうち、翌週もアクティブだった延べ人数の割合）。週ペアが無ければ
        `revisit_rate=None`。
      - `downloads`: `document.downloaded` 監査の期間合計＋日別内訳（「出典クリック数」計測基盤が無いため
        原本DL数で代替＝新規テレメトリは追加しない）。

    全クエリは `_usage_period_bounds` の `[start_ts, end_exclusive_ts)` という同じ半開区間で絞る
    （RV バッチ3再検証 MEDIUM: 以前は下限のみで、クロックスキュー/テスト由来の未来時刻行が
    「期間内」に混入し得た。DL/provider/heatmap/retention/world/user/daily すべて同じ上下限）。
    """
    _ensure()
    start_ts, start_date, end_date, end_exclusive_ts = _usage_period_bounds(days)
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
        # F3（2026-07-07）: トークン使用量（answer->'usage'）を provider/model 別・上位ユーザー別・日別で集計。
        #   入力/出力トークン数のみ集計する（金額換算は撤去・2026-07-08 フィードバック⑦）。
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
        # S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: チャット以外の LLM 呼び出し（intent 分類・
        # グラフ抽出・概念候補提案・埋め込み・admin グラフ質問・VLM）を kind 別に集計。usage_events は
        # kind='chat' を含まない（chat は token_model_rows 由来で別途合成する・二重計上なし）。
        usage_event_rows = c.execute(
            "SELECT kind, provider, model, SUM(calls) AS calls, "
            "  SUM(input_tokens) AS input, SUM(cached_input_tokens) AS cached_input, "
            "  SUM(output_tokens) AS output, SUM(reasoning_output_tokens) AS reasoning_output "
            "FROM usage_events WHERE ts >= %s AND ts < %s "
            "GROUP BY kind, provider, model ORDER BY kind, input DESC NULLS LAST",
            (start_ts, end_exclusive_ts),
        ).fetchall()

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
    period = {"start": start_date.isoformat(), "end": end_date.isoformat(), "days": days}

    zero_hit = {
        "knowledge_turns": total_knowledge_turns,
        "zero_hit_turns": total_zero_hit_turns,
        "rate": (total_zero_hit_turns / total_knowledge_turns) if total_knowledge_turns > 0 else None,
    }
    worlds_usage = [{"world": r["world"], "turns": r["turns"] or 0} for r in world_rows]
    # RV バッチ3再検証（2026-07-03）MEDIUM: allowlist 外/NULL の畳み込みは SQL 側（CASE式・GROUP BY）で
    # 完結している＝同じ 'unknown' に集約された複数の元値が別行として残ることはない（二重集計の防止）。
    providers_usage = [{"provider": r["provider"], "turns": r["n"] or 0} for r in provider_rows]
    heatmap = [{"weekday": r["weekday"], "hour": r["hour"], "count": r["n"] or 0} for r in heatmap_rows]

    # 定着指標: JST 週（月曜始まり）ごとのアクティブユーザー集合→週次人数の推移＋連続週ペアの再訪率。
    retention = _compute_retention(week_user_rows)

    download_daily = [{"date": str(r["date"]), "count": r["n"] or 0} for r in download_daily_rows]
    downloads = {"total": sum(r["count"] for r in download_daily), "daily": download_daily}

    # F3: トークン使用量（provider/model 別・上位ユーザー別・日別）。金額換算はしない（2026-07-08 撤去）。
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
    token_by_kind = [{"kind": "chat", "provider": m["provider"], "model": m["model"], "calls": m["turns"],
                      "input": m["input"], "cached_input": m["cached_input"], "output": m["output"],
                      "reasoning_output": m["reasoning_output"]}
                     for m in token_by_model]
    token_by_kind += [{"kind": r["kind"], "provider": r["provider"] or "unknown", "model": r["model"] or "",
                       "calls": int(r["calls"] or 0),
                       "input": int(r["input"]) if r["input"] is not None else None,
                       "cached_input": int(r["cached_input"]) if r["cached_input"] is not None else None,
                       "output": int(r["output"]) if r["output"] is not None else None,
                       "reasoning_output": (int(r["reasoning_output"]) if r["reasoning_output"] is not None
                                            else None)}
                      for r in usage_event_rows]
    tokens = {
        "totals": {
            "turns": sum(r["turns"] for r in token_by_model),
            "input": sum(r["input"] for r in token_by_model),
            "cached_input": sum(r["cached_input"] for r in token_by_model),
            "output": sum(r["output"] for r in token_by_model),
            "reasoning_output": sum(r["reasoning_output"] for r in token_by_model),
        },
        "by_model": token_by_model, "by_user": token_by_user, "daily": token_daily,
        "by_kind": token_by_kind,
    }

    return {
        "users": users, "totals": totals, "daily": daily, "period": period,
        "zero_hit": zero_hit, "worlds": worlds_usage, "providers": providers_usage,
        "heatmap": heatmap, "retention": retention, "downloads": downloads, "tokens": tokens,
    }
