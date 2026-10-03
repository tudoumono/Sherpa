"""改善ログ: 実運用ログから精度の改善点を見つけるための集計。

所要時間（`messages.answer.duration_ms`）以外は、既存の Execution Event（`messages.trace`）と `messages.answer`（出典・Evidence Packet・
usage）を集計するだけ。管理者向けエクスポート（`routers/improvement_log.py`）が集計関数を使う。
個人 workspace 参照ターン・sanitized share の複製・論理削除済み会話は `fetch_export_rows` の時点で除外済み。
"""
from __future__ import annotations

# 無条件に honest failure（答えられなかった／根拠不足で断った）とみなす stop_reason の2値。それ以外は evidence_selected/investigation_status の条件で判定する
HONEST_FAILURE_STOP_REASONS = ("evidence_verification_failed", "evaluation_blocked")
# investigation_status のクローズド語彙は sufficient/insufficient/conflicting/blocked（「完了」は sufficient のみ）
_INVESTIGATION_STATUS_COMPLETED = "sufficient"
# 「未完了」（途中終了・回答を控えた・終了理由が不明）と判定する stop_reason。honest_failure には含めず、
# evidence_selected==0 のフォールバック判定（`is_honest_failure`）からも除外する
INCOMPLETE_STOP_REASONS = ("truncated", "content_filtered", "unknown", "refusal", "tools_per_turn_exceeded")
# 検索を伴うレンズだけを honest_failure 判定の対象にする
_KNOWLEDGE_LENSES = ("qa", "impact", "troubleshoot")
# 現在コードが生成しうる stop_reason の閉じた語彙。`_resolve_stop_reason` は語彙に無い値・非文字列を `"unknown"` へ正規化する
_KNOWN_STOP_REASONS = frozenset(HONEST_FAILURE_STOP_REASONS) | frozenset(INCOMPLETE_STOP_REASONS) | {
    "no_tool_calls", "evaluation_sufficient", "turns_exhausted", "budget_exceeded",
}

# trace 内 kind="tool" ノードのうち、ツール呼び出しとして数えるラベルの閉じた集合。
# プロバイダごとにラベル文言が異なるため両方を含め、保存済みの履歴データを読むため旧ラベルも残す
_TOOL_CALL_LABELS = frozenset({
    "資料の一覧を確認", "資料を検索（語句そのまま）", "資料を検索（全文/日本語）", "資料を検索（全文）",
    "資料を検索（grep）",
    "ファイル名で検索", "フォルダ構成を確認",
    "該当箇所を精読", "文書を通読", "見出し構造を確認", "関係グラフをたどる", "世代間の差分を比較",
    # 原本読取ツールのラベル（`agentic_search._ORIGINAL_READ_LABELS`/`providers/codex/provider.py` の tlabel 辞書と同じ文言）。
    # 「原本のシート一覧を確認」は本文を読まないため `_FILES_READ_LABEL` には含めない
    "原本のシート一覧を確認",
    "原本を読む（Excel）", "原本を読む（Word）", "原本を読む（PowerPoint）", "原本を読む（PDF）",
    "原本を読む（先頭）",
    "調査台帳に項目を登録", "調査台帳の項目を更新", "調査台帳の状態を確認",
    "回答前に中間の見直しを記録",
    "ユーザに確認",
})
# 本文を実際に読んだとみなすラベル集合（見出し構造・シート一覧の確認は対象外）
_FILES_READ_LABEL = frozenset({
    "該当箇所を精読", "文書を通読",
    "原本を読む（Excel）", "原本を読む（Word）", "原本を読む（PowerPoint）", "原本を読む（PDF）",
    "原本を読む（先頭）",
})
# v1 trace の要約ノードの id（保存済みの過去メッセージを読むために残す）
_V1_TRACE_OMITTED_NODE_ID = "trace-omitted"

# エクスポート1行のフィールド順（0件時も列を固定する）。質問/回答は先頭 N 字のみで、省略したかは `question_truncated`/`answer_truncated` で明示する
EXPORT_FIELDS = (
    "conversation_id", "message_id", "created_at",
    "question_head", "question_truncated", "answer_head", "answer_truncated",
    "sources", "sources_verified", "tool_calls", "files_read", "trace_truncated",
    "candidates_seen", "candidates_inspected", "evidence_selected", "investigation_status",
    "stop_reason", "duration_ms", "provider", "model", "tokens", "lane_breakdown",
    "honest_failure", "feedback",
)

# 質問/回答は先頭 N 字のみエクスポートする（本文を丸ごと複製しない）
_TEXT_EXPORT_MAX_LEN = 500


def is_honest_failure(*, lens: str | None, stop_reason: str | None,
                      evidence_selected: int | None, investigation_status: str | None) -> bool:
    """『答えられなかった／根拠不足で断った』ターンかどうかの判定（表示専用の派生値）。

    検索レンズ（qa/impact/troubleshoot）以外は False。`INCOMPLETE_STOP_REASONS` は含めない。それ以外は `stop_reason` が
    `HONEST_FAILURE_STOP_REASONS` のいずれか、または `evidence_selected == 0` かつ `investigation_status` が sufficient 以外で True。
    """
    if lens not in _KNOWLEDGE_LENSES:
        return False
    if stop_reason in INCOMPLETE_STOP_REASONS:
        return False
    if stop_reason in HONEST_FAILURE_STOP_REASONS:
        return True
    return evidence_selected == 0 and investigation_status != _INVESTIGATION_STATUS_COMPLETED


def trace_tool_stats(trace) -> tuple[int, int, bool]:
    """`(tool_calls, files_read, trace_truncated)` を `messages.trace`（JSONB リスト）から数える。

    trace の保存上限で畳まれたターンは件数が実際より少なくなり得るため、`trace_truncated=True` のときは下限値として扱う。
    `kind="tool"` の集約ノードは `metrics.omitted_count` を `tool_calls` に加算する（`files_read` には加算しない）。
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
    """行の回答側・質問側いずれかが個人情報由来か（`store.conversations.is_personal_tainted` を両側に適用する）。"""
    from sherpa.store.conversations import is_personal_tainted
    if is_personal_tainted({"personal": row.get("personal"), "answer": row.get("answer")}):
        return True
    return is_personal_tainted({"personal": row.get("question_personal"),
                                "answer": row.get("question_answer")})


def _lane_breakdown(answer: dict) -> list:
    """複数プロファイル並用時のレーン別 usage（`usage_subs` があればそれ、無ければ `usage_sub` を1件配列にする、どちらも無ければ空）。"""
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


# plan 集約経路（`providers/base.py`）が `f"{profile_id}:{stop_reason}"` を `+` で連結した複合値、または固定文言 `"plan_completed"` を保存することがある
_PLAN_COMPLETED_TOKEN = "plan_completed"


# 複合 stop_reason の代表値は出現順に依存しない固定の優先順位で決める: 未完了側 > honest側 > 上限系 > 全て自然完了。
# 上限系は `is_honest_failure` の条件付き判定に委ねるため、丸めず実値を代表にする
_INCOMPLETE_PRIORITY = ("content_filtered", "truncated", "refusal", "tools_per_turn_exceeded", "unknown")
_HONEST_PRIORITY = ("evidence_verification_failed", "evaluation_blocked")
_LIMIT_PRIORITY = ("budget_exceeded", "turns_exhausted")


def _decompose_composite_stop_reason(raw: str) -> str | None:
    """複合 stop_reason（`name:reason(+name:reason)*`・`plan_completed`）を単一の代表値へ集約する。

    分解できない、またはいずれかの reason が既知語彙に無ければ `None`（呼び出し元が `"unknown"` にする）。代表値は
    `_INCOMPLETE_PRIORITY`→`_HONEST_PRIORITY`→`_LIMIT_PRIORITY` の順にカテゴリ内固定順位で選び、どれにも該当しなければ `"evaluation_sufficient"`。
    """
    if raw == _PLAN_COMPLETED_TOKEN:
        return "evaluation_sufficient"
    reasons = []
    for step in raw.split("+"):
        name, sep, reason = step.partition(":")
        if not sep:
            return None
        reasons.append(reason)
    if not reasons or not all(r in _KNOWN_STOP_REASONS for r in reasons):
        return None
    reason_set = set(reasons)
    for candidate in _INCOMPLETE_PRIORITY + _HONEST_PRIORITY + _LIMIT_PRIORITY:
        if candidate in reason_set:
            return candidate
    return "evaluation_sufficient"


def _resolve_stop_reason(answer: dict, packet: dict, *, packet_present: bool) -> str | None:
    """ターンの stop_reason を閉じた語彙で返す。Evidence Packet が無い（非エージェント・plain 会話）ターンは対象外として `None`。

    `packet_present` は `data.get("evidence_packet")` が dict だったか（空 dict も「ある」）。語彙に無い値は複合値として分解を試み、
    失敗（欠落・非文字列・分解不能）なら `"unknown"` にする。`answer.route.reason` は使わない。
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
    """1件の assistant メッセージ（`store.list_export_messages` の1行＋join 済み feedback）→ 改善ログエクスポートの1行。`msg` に無いキーは安全にフォールバックする。"""
    answer = msg.get("answer") or {}
    data = answer.get("data") or {}
    # `evidence_packet` キーが無い（非 agentic）／空 dict／dict 以外を区別する。「Packet があったか」は `packet_present` で持ち回る
    _evidence_packet = data.get("evidence_packet")
    packet_present = isinstance(_evidence_packet, dict)
    packet = _evidence_packet if packet_present else {}
    usage = answer.get("usage") or {}
    # トップレベル lens が欠落している行は `answer.route.lens` で補う（検索レンズの gate が誤って弾かないため）
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


# エクスポート/要約が1回に取得する最大件数。到達したら呼び出し側が truncated を明示する
EXPORT_MAX_ROWS = 50_000
_EXPORT_PAGE = 500


def _has_more_clean_rows(time_from, cursor_id) -> bool:
    """`cursor_id` より古い側に、個人情報除外を通過する行が1件でも残っているか（`fetch_export_rows` の上限到達時の probe 用）。"""
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

    `store.list_export_messages` をキーセット方式でページングし、個人情報由来の行を除外する。`output_cap` に達したら、
    除外後も後続候補が残っているかを `_has_more_clean_rows` で確認して打ち切り、`(rows, truncated)` の `truncated` で明示する。
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


