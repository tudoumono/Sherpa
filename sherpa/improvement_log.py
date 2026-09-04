"""改善ログ: 実運用ログから精度の改善点を見つけるための集計。

新規計測は「所要時間」（`chat_service.py` が `messages.answer.duration_ms` へ埋め込む）のみで、
それ以外は既存の Execution Event（`messages.trace`）と `messages.answer`（出典・Evidence Packet・
usage）を集計するだけ（新しい記録を増やさない）。管理者向けエクスポート
（`sherpa/routers/improvement_log.py`）と管理者チャット（`sherpa/usage_chat.py`）の両方が
このモジュールの集計関数を再利用する。個人 workspace 参照ターン・sanitized share の複製・
論理削除済み会話は `fetch_export_rows`（`store.list_export_messages` のページングと
`is_export_row_personal_tainted` による除外を担う）の時点で除外済み。
"""
from __future__ import annotations

# 「答えられなかった／根拠不足で断った」ターンと判定する stop_reason（無条件に honest failure と
# みなす2値のみ・それ以外は evidence_selected/investigation_status の条件で判定する）。
HONEST_FAILURE_STOP_REASONS = ("evidence_verification_failed", "evaluation_blocked")
# investigation_status のクローズド語彙は sufficient/insufficient/conflicting/blocked（「完了」に
# 相当する語は "sufficient" のみ）。
_INVESTIGATION_STATUS_COMPLETED = "sufficient"
# 「未完了」（生成が完走しなかった・AI が明示的に回答を控えた・終了理由を確認できない）と判定する
# stop_reason。honest_failure には含めない（根拠不足で断ったのではなく別カテゴリのため）。
# evidence_selected==0 のフォールバック判定（`is_honest_failure` 参照）からも明示的に除外する
# ——除外しないと出典 0 件のまま未完了/理由不明のターンが honest_failure に誤って混入する。
# `refusal`（安全上の理由で回答を控えた）・`tools_per_turn_exceeded`（ツール呼び出し数の上限）は
# 一般には正常終了だが、本モジュールの集計区分では「答えられた」とも「答えられなかった（honest
# failure）」とも数えたくないためこちら側に含める（docs/proposals/2026-08-28-改善ログ.md §0.2）。
# `truncated`/`content_filtered` を生成する経路は現時点のコードに無いため、これらに起因する
# is_incomplete は実データ上は常に0のまま。
INCOMPLETE_STOP_REASONS = ("truncated", "content_filtered", "unknown", "refusal", "tools_per_turn_exceeded")
# 検索を伴うレンズだけを honest_failure 判定の対象にする（雑談等、検索を試みていないターンを
# 「答えられなかった」に含めない）。
_KNOWLEDGE_LENSES = ("qa", "impact", "troubleshoot")
# 現在コードが実際に生成しうる stop_reason の閉じた語彙（`agentic_search.py`/`providers/base.py`
# 参照・正典 docs/proposals/2026-08-28-改善ログ.md §0.2 の表と一致させる）。`_resolve_stop_reason`
# はこの語彙に無い値・非文字列を `"unknown"` へ正規化する——未知語をそのまま通すと
# `stop_reason_counts` 等の下流集計が無制限に増殖し、honest_failure/incomplete の判定も
# 「知らない値」を暗黙に自然完了扱いしてしまう。
_KNOWN_STOP_REASONS = frozenset(HONEST_FAILURE_STOP_REASONS) | frozenset(INCOMPLETE_STOP_REASONS) | {
    "no_tool_calls", "evaluation_sufficient", "turns_exhausted", "budget_exceeded",
}

# trace 内 kind="tool" ノードのうち、実際のツール呼び出しを数える対象ラベルの閉じた集合。
# プロバイダごとにラベル文言が異なる（`agentic_search._tool_node`/`_SUB_TOOL_FIXED_WORDING` と
# `providers/codex/provider.py` の tlabel 辞書）ため両方を含める。ラベル文字列に依存するため、
# いずれかのプロバイダ側でラベルが変わればここも追随させる必要がある。
_TOOL_CALL_LABELS = frozenset({
    "資料の一覧を確認", "資料を検索（語句そのまま）", "資料を検索（全文/日本語）", "資料を検索（全文）",
    "ファイル名で検索",
    "該当箇所を精読", "文書を通読", "見出し構造を確認", "関係グラフをたどる", "ユーザに確認",
})
# 本文を実際に読んだ（精読/通読した）とみなすラベル集合。「見出し構造を確認」（doc_outline）は
# 構造を見るだけで本文を読んだことにはならないため対象外——`files_read` に含めるのは
# `read_around`/`read_doc` 相当の2ラベルのみ。
_FILES_READ_LABEL = frozenset({"該当箇所を精読", "文書を通読"})
# v1 trace 上限（旧 `chat_service._cap_trace`・撤去済み・TOGGLE-RM 2026-09-03）到達時に先頭へ置かれた
# 要約ノードの id。生成コード自体は撤去済みだが、v1 形式で既に保存済みの過去メッセージ（`messages.trace`）
# を読む本関数はこの id を読み続ける必要がある（履歴データの後方互換読み取り）。
_V1_TRACE_OMITTED_NODE_ID = "trace-omitted"

# エクスポート1行のフィールド順（CSV ヘッダ・0件時も列を固定するため定数化）。質問/回答は
# 先頭 N 字のみ（`question_head`/`answer_head`）で丸ごと再掲はしない。省略が起きたかは
# `question_truncated`/`answer_truncated` で明示する（無言で切り詰めない）。
EXPORT_FIELDS = (
    "conversation_id", "message_id", "created_at",
    "question_head", "question_truncated", "answer_head", "answer_truncated",
    "sources", "sources_verified", "tool_calls", "files_read", "trace_truncated",
    "candidates_seen", "candidates_inspected", "evidence_selected", "investigation_status",
    "stop_reason", "duration_ms", "provider", "model", "tokens", "lane_breakdown",
    "honest_failure", "feedback",
)

# 質問/回答は先頭 N 字のみエクスポートする（本文を丸ごと複製しない・監査エクスポートの
# headline 表示と同じ発想）。
_TEXT_EXPORT_MAX_LEN = 500


def is_honest_failure(*, lens: str | None, stop_reason: str | None,
                      evidence_selected: int | None, investigation_status: str | None) -> bool:
    """『答えられなかった／根拠不足で断った』ターンかどうかの判定（表示専用の派生値・DB には
    保存しない）。検索レンズ（qa/impact/troubleshoot）以外は常に False。`stop_reason` が
    `INCOMPLETE_STOP_REASONS`（未完了）の場合は、出典 0 件でも honest_failure に含めない。
    それ以外は `stop_reason` が `HONEST_FAILURE_STOP_REASONS` のいずれか、または
    `evidence_selected == 0` かつ `investigation_status` が完了（sufficient）以外の場合に True。
    """
    if lens not in _KNOWLEDGE_LENSES:
        return False
    if stop_reason in INCOMPLETE_STOP_REASONS:
        return False
    if stop_reason in HONEST_FAILURE_STOP_REASONS:
        return True
    return evidence_selected == 0 and investigation_status != _INVESTIGATION_STATUS_COMPLETED


def is_incomplete(stop_reason: str | None) -> bool:
    """`stop_reason` が `INCOMPLETE_STOP_REASONS`（出力上限/内容フィルタで途中終了・AI が明示的に
    回答を控えた・ツール呼び出し数の上限・終了理由を確認できない、のいずれか）か
    （honest_failure とは別カウント）。"""
    return stop_reason in INCOMPLETE_STOP_REASONS


def trace_tool_stats(trace) -> tuple[int, int, bool]:
    """`(tool_calls, files_read, trace_truncated)` を `messages.trace`（JSONB リスト）から数える。

    trace 保存の上限（既定: 120 ノードを超えると古い方を要約1件へ畳む・v2 のノード集約＋予算超過
    マーカーは常時適用）に達したターンは、畳まれたノードの種別内訳が失われる
    ため `tool_calls`/`files_read` が実際より少なくなり得る。`trace_truncated=True` はその目印
    （呼び出し側はこの値を「実際はこれ以上ある可能性がある下限値」として扱うこと）。v2 の集約
    ノードは畳んだ元ノード群の `kind` を保つ（`chat_service.py::_aggregate_node`）ため、
    `kind="tool"` の集約は `metrics.omitted_count` を `tool_calls` に加算する（畳まれた中の
    grep/精読の内訳までは残らないため `files_read` へは加算しない）。
    """
    if not isinstance(trace, list):
        return 0, 0, False
    tool_calls = files_read = 0
    truncated = False
    for node in trace:
        if not isinstance(node, dict):
            continue
        if node.get("id") == _V1_TRACE_OMITTED_NODE_ID:
            truncated = True
            continue
        metrics = node.get("metrics")
        if isinstance(metrics, dict) and metrics.get("omitted_count"):
            truncated = True
            if node.get("kind") == "tool":
                tool_calls += int(metrics["omitted_count"])
            continue
        if node.get("kind") != "tool":
            continue
        label = node.get("label")
        if label not in _TOOL_CALL_LABELS:
            continue
        tool_calls += 1
        if label in _FILES_READ_LABEL:
            files_read += 1
    return tool_calls, files_read, truncated


def is_export_row_personal_tainted(row: dict) -> bool:
    """行（`store.list_export_messages` の1行）の回答側・質問側いずれかが個人情報由来か
    （`store.conversations.is_personal_tainted` を両側に適用する共通ヘルパ）。"""
    from sherpa.store.conversations import is_personal_tainted
    if is_personal_tainted({"personal": row.get("personal"), "answer": row.get("answer")}):
        return True
    return is_personal_tainted({"personal": row.get("question_personal"),
                                "answer": row.get("question_answer")})


def _lane_breakdown(answer: dict) -> list:
    """複数プロファイル並用時のレーン別 usage（`usage_subs` があればそちら・無ければ `usage_sub`
    を1件配列にする・どちらも無ければ空）。"""
    if answer.get("usage_subs"):
        return list(answer["usage_subs"])
    if answer.get("usage_sub"):
        return [answer["usage_sub"]]
    return []


def _clip_with_flag(text, limit: int = _TEXT_EXPORT_MAX_LEN) -> tuple[str | None, bool]:
    """`(先頭 limit 字, 切り詰めが起きたか)`。文字列でなければ `(None, False)`。"""
    if not isinstance(text, str):
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _clip(text, limit: int = _TEXT_EXPORT_MAX_LEN) -> str | None:
    head, _truncated = _clip_with_flag(text, limit)
    return head


# 複数プロファイル並用（S4・既定 OFF）時、plan 集約経路（`providers/base.py`）が
# `f"{profile_id}:{stop_reason}"` を `+` で連結した複合値、または集約対象が無い場合の固定文言
# `"plan_completed"` を stop_reason に保存することがある（生成側の語彙整理は別スライスの対象・
# ここでは複合値を正典語彙へ集約する側で吸収する）。
_PLAN_COMPLETED_TOKEN = "plan_completed"


# 複合 stop_reason の代表値選定は、ステップの並び順に依存しない**固定の優先順位**で決める
# （出現順で選ぶと `a:x+b:y` と `a:y+b:x` で代表値が変わってしまう）。カテゴリの優先順位は
# 未完了側 > honest側 > 上限系（budget_exceeded/turns_exhausted）> 全て自然完了。
# 上限系はどちらも無条件 honest_failure ではなく `is_honest_failure` の条件付き判定
# （evidence_selected/investigation_status）に委ねる必要があるため、"evaluation_sufficient"
# へ丸めず実値をそのまま代表にする。
_INCOMPLETE_PRIORITY = ("content_filtered", "truncated", "refusal", "tools_per_turn_exceeded", "unknown")
_HONEST_PRIORITY = ("evidence_verification_failed", "evaluation_blocked")
_LIMIT_PRIORITY = ("budget_exceeded", "turns_exhausted")


def _decompose_composite_stop_reason(raw: str) -> str | None:
    """複合 stop_reason（`name:reason(+name:reason)*`・`plan_completed`）を単一の代表値へ
    集約する。分解できない、またはいずれかのステップの reason が既知語彙に無い場合は `None`
    を返す（呼び出し元が `"unknown"` にフォールバックする）。代表値は `_INCOMPLETE_PRIORITY`→
    `_HONEST_PRIORITY`→`_LIMIT_PRIORITY` の順にカテゴリ内固定順位で選ぶ（ステップの出現順には
    依存しない）。いずれのカテゴリにも該当しなければ全ステップが自然完了とみなし
    `"evaluation_sufficient"` に正規化する（個々のステップの自然完了語彙が揃っていなくても
    代表値は1つに固定する）。
    """
    if raw == _PLAN_COMPLETED_TOKEN:
        return "evaluation_sufficient"
    reasons = []
    for step in raw.split("+"):
        name, sep, reason = step.partition(":")
        if not sep:
            return None   # "name:reason" 形式でないステップがあれば分解不能
        reasons.append(reason)
    if not reasons or not all(r in _KNOWN_STOP_REASONS for r in reasons):
        return None
    reason_set = set(reasons)
    for candidate in _INCOMPLETE_PRIORITY + _HONEST_PRIORITY + _LIMIT_PRIORITY:
        if candidate in reason_set:
            return candidate
    return "evaluation_sufficient"


def _resolve_stop_reason(answer: dict, packet: dict, *, packet_present: bool) -> str | None:
    """Evidence Packet 自体が無い（非エージェント・plain 会話）ターンは対象外として `None` を
    返す。Evidence Packet があれば（**空 dict `{}` も「ある」に含む**——`packet_present` は
    `data.get("evidence_packet")` が dict だったかどうかで呼び出し元が判定する。空 dict を
    `not packet` で判定すると「Packet 自体が無い」と区別が付かなくなる）`stop_reason` を採用する。
    閉じた語彙（`_KNOWN_STOP_REASONS`）に無い値は `_decompose_composite_stop_reason` で複合値
    （plan 集約経路）としての分解を試み、それも失敗すれば（欠落・非文字列・分解不能な文字列）
    `"unknown"`（＝incomplete 側）へ正規化する——エージェント経路のはずなのに理由が記録されて
    いない/未知トークンの状態を「対象外（None）」とも「既知の理由」とも区別する。
    `answer.route.reason` はルーティング選択（質問の意図判定）の理由でありエージェントの
    停止理由ではないため使わない。
    """
    if not packet_present:
        return None
    stop_reason = packet.get("stop_reason")
    if isinstance(stop_reason, str):
        if stop_reason in _KNOWN_STOP_REASONS:
            return stop_reason
        decomposed = _decompose_composite_stop_reason(stop_reason)
        if decomposed is not None:
            return decomposed
    return "unknown"


def build_export_row(msg: dict, *, feedback: dict | None) -> dict:
    """1件の assistant メッセージ（`store.list_export_messages` の1行＋join 済み feedback）→
    改善ログエクスポートの1行。`msg` に無いキーは全て安全にフォールバックする（非 agentic・
    plain 会話・clarify ターンでも例外にならない）。
    """
    answer = msg.get("answer") or {}
    data = answer.get("data") or {}
    # `evidence_packet` キーが無い（非 agentic）／値が空 dict `{}`（agentic だが stop_reason 等が
    # 空のまま）／値が dict 以外（型不正）を区別する。`packet` 自体は以降 `.get()` で安全に読める
    # よう常に dict にするが、「Packet があったかどうか」は別途 `packet_present` で持ち回る
    # （`_resolve_stop_reason` 参照）。
    _evidence_packet = data.get("evidence_packet")
    packet_present = isinstance(_evidence_packet, dict)
    packet = _evidence_packet if packet_present else {}
    usage = answer.get("usage") or {}
    # トップレベル lens が欠落している行（route だけが lens を記録している経路がある）は
    # answer.route.lens で補う——補わないと honest_failure 判定の検索レンズ gate（qa/impact/
    # troubleshoot）が誤って弾いてしまう。
    lens = answer.get("lens") or (answer.get("route") or {}).get("lens")
    sources = answer.get("sources") or []
    tool_calls, files_read, trace_truncated = trace_tool_stats(msg.get("trace"))
    stop_reason = _resolve_stop_reason(answer, packet, packet_present=packet_present)
    evidence_selected = packet.get("evidence_selected")
    investigation_status = packet.get("investigation_status")
    question_head, question_truncated = _clip_with_flag(msg.get("question"))
    answer_head, answer_truncated = _clip_with_flag(msg.get("content"))
    return {
        "conversation_id": msg.get("conversation_id"),
        "message_id": msg.get("id"),
        "created_at": msg.get("created_at"),
        "question_head": question_head,
        "question_truncated": question_truncated,
        "answer_head": answer_head,
        "answer_truncated": answer_truncated,
        "sources": sources,
        "sources_verified": answer.get("sources_verified") or [],
        "tool_calls": tool_calls,
        "files_read": files_read,
        "trace_truncated": trace_truncated,
        "candidates_seen": packet.get("candidates_seen"),
        "candidates_inspected": packet.get("candidates_inspected"),
        "evidence_selected": evidence_selected,
        "investigation_status": investigation_status,
        "stop_reason": stop_reason,
        "duration_ms": answer.get("duration_ms"),
        "provider": usage.get("provider"),
        "model": usage.get("model"),
        "tokens": {k: usage.get(k) for k in
                  ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")},
        "lane_breakdown": _lane_breakdown(answer),
        "honest_failure": is_honest_failure(lens=lens, stop_reason=stop_reason,
                                            evidence_selected=evidence_selected,
                                            investigation_status=investigation_status),
        "feedback": ({"rating": feedback.get("rating"), "tags": feedback.get("tags"),
                      "comment": feedback.get("comment")} if feedback else None),
    }


def _duration_distribution(values: list[int]) -> dict:
    if not values:
        return {"count": 0}
    s = sorted(values)
    n = len(s)
    return {"count": n, "min": s[0], "max": s[-1], "avg": round(sum(s) / n), "p50": s[n // 2]}


# 改善ログエクスポート/要約が1回に取得する最大件数。到達した場合は呼び出し側が truncated を
# 明示する（無通知に打ち切らない・`fetch_export_rows` の戻り値 `truncated` 参照）。
EXPORT_MAX_ROWS = 50_000
_SUMMARY_MAX_ROWS = 20_000
_EXPORT_PAGE = 500


def _has_more_clean_rows(time_from, cursor_id) -> bool:
    """`cursor_id` より古い側に、個人情報除外を通過する行が1件でも残っているか
    （`fetch_export_rows` の上限到達時の probe 専用・taint 判定込みで確認する）。"""
    from sherpa import store

    while True:
        batch = store.list_export_messages(time_from=time_from, cursor_id=cursor_id, limit=_EXPORT_PAGE)
        if not batch:
            return False
        if any(not is_export_row_personal_tainted(r) for r in batch):
            return True
        cursor_id = batch[-1]["id"]
        if len(batch) < _EXPORT_PAGE:
            return False


def fetch_export_rows(*, time_from, output_cap: int) -> tuple[list[dict], bool]:
    """`time_from` 以降の改善ログ対象メッセージを新しい順（id 降順）に取得する。

    `store.list_export_messages` をキーセット方式でページングしながら、個人情報由来の行
    （`is_export_row_personal_tainted`）を除外する（`store.list_export_messages` 自身が
    論理削除済み会話・sanitized snapshot の複製・監査で対応付けられない行を既に除外している）。
    `output_cap` に達したら、除外後もまだ後続候補が残っているかを `_has_more_clean_rows` で
    確認したうえで打ち切る——`(rows, truncated)` の `truncated` で呼び出し側（エクスポート API の
    レスポンス/監査・要約の文脈）に明示する（上限到達を無通知にしない・残りが個人情報の行だけ
    なら truncated は立てない）。
    """
    from sherpa import store

    rows: list[dict] = []
    cursor_id = None
    while True:
        batch = store.list_export_messages(time_from=time_from, cursor_id=cursor_id,
                                           limit=_EXPORT_PAGE)
        if not batch:
            return rows, False
        for r in batch:
            if is_export_row_personal_tainted(r):
                continue
            rows.append(r)
            if len(rows) >= output_cap:
                return rows[:output_cap], _has_more_clean_rows(time_from, r["id"])
        cursor_id = batch[-1]["id"]
        if len(batch) < _EXPORT_PAGE:
            return rows, False


# 管理者チャットへ渡す「👎が付いた質問/一言」サンプル数の上限。
_FLAGGED_QUESTIONS_MAX = 20
_FLAGGED_COMMENTS_MAX = 20
# 質問/一言サンプルは本文全体でなく先頭 N 字のみ。
_FLAGGED_QUESTION_CLIP_LEN = 100


def compact_summary(*, days: int) -> dict:
    """改善ログの管理者チャット向け要約（件数・タグ分布・stop_reason 分布・honest_failure 率・
    所要時間の分布・👎 が付いた質問/一言の先頭100字のみ最大20件）。質問・回答の全文はここに
    含めない。個人情報の除外・sanitized_snapshot 除外は `fetch_export_rows` を経由済み。
    """
    from datetime import datetime, timedelta, timezone

    from sherpa import store

    time_from = datetime.now(timezone.utc) - timedelta(days=days)
    rows, truncated = fetch_export_rows(time_from=time_from, output_cap=_SUMMARY_MAX_ROWS)

    feedback_map = store.get_feedback_by_message_ids([r["id"] for r in rows])
    up = down = honest_failures = incomplete = 0
    tag_counts: dict[str, int] = {}
    stop_reason_counts: dict[str, int] = {}
    flagged_questions: list[str] = []
    flagged_comments: list[str] = []
    durations: list[int] = []
    for r in rows:
        built = build_export_row(r, feedback=feedback_map.get(r["id"]))
        if built["honest_failure"]:
            honest_failures += 1
        # 未完了ターン（出力上限/内容フィルタで途中終了・AI が明示的に回答を控えた・ツール
        # 呼び出し数の上限・終了理由を確認できない、のいずれか＝`INCOMPLETE_STOP_REASONS`）。
        # honest_failure とは別カウント（`is_honest_failure` が既に honest_failure から除外済み）。
        if is_incomplete(built["stop_reason"]):
            incomplete += 1
        if isinstance(built["duration_ms"], (int, float)):
            durations.append(built["duration_ms"])
        sr_key = built["stop_reason"] or "none"
        stop_reason_counts[sr_key] = stop_reason_counts.get(sr_key, 0) + 1
        fb = feedback_map.get(r["id"])
        if not fb:
            continue
        if fb.get("rating") == "up":
            up += 1
        elif fb.get("rating") == "down":
            down += 1
            for tag in (fb.get("tags") or []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            if len(flagged_questions) < _FLAGGED_QUESTIONS_MAX:
                q = _clip(r.get("question"), _FLAGGED_QUESTION_CLIP_LEN)
                if q:
                    flagged_questions.append(q)
            if len(flagged_comments) < _FLAGGED_COMMENTS_MAX:
                c = _clip(fb.get("comment"), _FLAGGED_QUESTION_CLIP_LEN)
                if c:
                    flagged_comments.append(c)

    total = len(rows)
    return {
        "period_days": days,
        "turns_total": total,
        "truncated": truncated,
        "feedback_up": up,
        "feedback_down": down,
        "feedback_tag_counts": tag_counts,
        "stop_reason_counts": stop_reason_counts,
        "honest_failure_count": honest_failures,
        "honest_failure_rate": round(honest_failures / total, 4) if total else None,
        "incomplete_count": incomplete,
        "incomplete_rate": round(incomplete / total, 4) if total else None,
        "duration_ms": _duration_distribution(durations),
        "flagged_questions_sample": flagged_questions,
        "flagged_comments_sample": flagged_comments,
    }
