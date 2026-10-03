"""ターンの終了理由（`stop_kind`）の正規化。

`messages.answer.stop_kind` に保存する8値の閉じた語彙と、複数の判定源（API 経路の `evidence_packet.stop_reason`・
Codex 経路の `codex_stopped_early`/`codex_silent_failure`・例外型）からの導出を1箇所に集約する。
`stopped_by_user` は巡ループの停止終端で未完了回答を保存したターンだけが持つ（それ以外の停止は監査 `chat.turn` から集計）。
`sherpa` 内の他モジュールに依存しない葉モジュール。
設計: docs/design/usage.md「数えるもの・数えないもの」
"""
from __future__ import annotations

import http.client
import socket
import urllib.error

STOP_KINDS = frozenset({
    "completed", "stopped_by_user", "budget", "no_evidence",
    "transport_error", "timeout", "codex_silent", "codex_partial",
})

# API 経路の予算到達・根拠不足に対応する部分集合
_BUDGET_STOP_REASONS = frozenset({"turns_exhausted", "budget_exceeded", "tools_per_turn_exceeded"})
_NO_EVIDENCE_STOP_REASONS = frozenset({"evaluation_blocked", "evidence_verification_failed"})
# 残りの stop_reason はすべて completed のまま別扱いしない


def is_timeout_exc(exc: BaseException) -> bool:
    """`TimeoutError` 直接、または `URLError` が timeout を包んだ形か。"""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def is_transport_error_exc(exc: BaseException) -> bool:
    """通信境界の例外か（`ConnectionError`/`socket.gaierror`/`URLError`/`HTTPException` に限る）。

    timeout と `HTTPError` は対象外（`is_timeout_exc` を先に確認する）。`OSError` 全体は対象にしない（ローカル I/O 障害を誤記録しないため）。
    """
    if is_timeout_exc(exc) or isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (ConnectionError, socket.gaierror, urllib.error.URLError,
                            http.client.HTTPException))


def from_exception(exc: BaseException) -> str | None:
    """例外の型だけから `stop_kind`（`timeout`/`transport_error`）を導く。当てはまらなければ `None`。

    メッセージ本文は見ない。`__cause__` を 1 段だけ見る。
    """
    for c in (exc, getattr(exc, "__cause__", None)):
        if c is None:
            continue
        if is_timeout_exc(c):
            return "timeout"
        if is_transport_error_exc(c):
            return "transport_error"
    return None


def is_main_task(packet: dict) -> bool:
    """`evidence_packet.task_id` がメインの調査ターンか（`"main"` のみ真。`sub:…`/`plan:…` は偽）。"""
    return packet.get("task_id") == "main"


def resolve(env: dict) -> str | None:
    """`_result` の env から `stop_kind` を1つ導く（`chat_service._finalize` が呼ぶ）。

    `None` は「完了として数えない・型も特定できない」場合（`busy`・`agentic_failure="error"`）で、集計側の `unknown` になる。
    `agentic_failure="insufficient"` は `no_evidence`、`"timeout"`/`"transport_error"` はその値。

    優先順位: busy → Codex 経路の印（silent のうち `limits.total_budget_hit` は budget へ格上げ→それ以外の silent→partial）
    → honest failure の印 → `evidence_packet.stop_reason`（`is_main_task` が真のときのみ）→ 既定 `completed`。
    戻り値が `None` でなければ必ず `STOP_KINDS` の要素。
    """
    kind = _resolve_kind(env)
    if kind is not None and kind not in STOP_KINDS:
        raise ValueError(f"stop_kind.resolve() produced a value outside STOP_KINDS: {kind!r}")
    return kind


def _resolve_kind(env: dict) -> str | None:
    if env.get("busy"):
        return None
    if env.get("stopped_by_user"):
        # 利用者の停止で打ち切った未完了回答は完了として数えない
        return "stopped_by_user"
    if env.get("codex_silent_failure"):
        # Codex の文脈枠超過は打切りの内訳（budget）へ数える。それ以外の無出力失敗は codex_silent
        if (env.get("limits") or {}).get("total_budget_hit"):
            return "budget"
        return "codex_silent"
    if env.get("codex_stopped_early"):
        return "codex_partial"
    failure = env.get("agentic_failure")
    if failure == "insufficient":
        return "no_evidence"
    if failure in ("timeout", "transport_error"):
        return failure
    if failure:
        return None
    packet = (env.get("data") or {}).get("evidence_packet") or {}
    if is_main_task(packet):
        stop_reason = packet.get("stop_reason")
        if stop_reason in _BUDGET_STOP_REASONS:
            return "budget"
        if stop_reason in _NO_EVIDENCE_STOP_REASONS:
            return "no_evidence"
    return "completed"
