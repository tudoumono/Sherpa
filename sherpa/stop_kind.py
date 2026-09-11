"""ターンの終了理由（`stop_kind`）の正規化——STAT-3 T3（正典
`docs/proposals/2026-09-11-利用統計の拡充.md`）。

`messages.answer.stop_kind` に保存する8値の閉じた語彙と、複数の判定源（API 経路の
`evidence_packet.stop_reason`・Codex 経路の `codex_stopped_early`/`codex_silent_failure`・
`provider.run()` が例外で落ちて honest failure 本文になる経路の例外型）からの導出を
1箇所に集約する（唯一の真実源）。`stopped_by_user` は監査 `chat.turn`（`detail.stopped`）から
別途集計するだけで、この列挙自体が `messages.answer.stop_kind` に保存されることはない
（利用者の明示停止は assistant メッセージを保存しない契約・`chat_service.py::stream_message`/
`handle_message` の stopped 分岐参照）。

`sherpa.chat_service`（`_finalize` が `resolve()` を呼ぶ）・`sherpa.providers.codex.provider`
（silent failure 分岐が env に印を立てる・値の解釈はしない）・`sherpa.routers.chat`
（`_persist_turn_crash` が `from_exception()` を直接使う）・`sherpa.providers.base`
（`_plain_run`／単発フォールバックがストリーム例外に `from_exception()` を直接使い
`agentic_failure` に載せる・honest failure の envelope に印を立てる）が本モジュールを参照する。
同じ印（`agentic_failure`・値の解釈はしない）を立てるだけの参照元は他に `sherpa.providers`
（未接続/無効 AI）・`sherpa.agentic_search`（`tools_blocked_env`）・`sherpa.chat_service`
（`_fixed_lens_result`）。
`sherpa` 内の他モジュールに依存しない（import 循環を避けるため）。
"""
from __future__ import annotations

import http.client
import urllib.error

STOP_KINDS = frozenset({
    "completed", "stopped_by_user", "budget", "no_evidence",
    "transport_error", "timeout", "codex_silent", "codex_partial",
})

# API 経路（`agentic_search.STOP_REASONS` のうち予算到達・根拠不足に対応する部分集合）。
_BUDGET_STOP_REASONS = frozenset({"turns_exhausted", "budget_exceeded", "tools_per_turn_exceeded"})
_NO_EVIDENCE_STOP_REASONS = frozenset({"evaluation_blocked", "evidence_verification_failed"})
# 残り（no_tool_calls/evaluation_sufficient/truncated/content_filtered/refusal/unknown）は
# すべて completed のまま別扱いにしない——既存の `stop_reason` 自体は `answer` に残るため、
# 必要ならそちらの分布で見分けられる。


def is_timeout_exc(exc: BaseException) -> bool:
    """`TimeoutError` 直接、または `URLError` が timeout を reason に包んだ形か
    （`agentic_search._is_timeout_error` と同じ判定。import 循環を避けるためここで独立に持つ）。"""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def is_transport_error_exc(exc: BaseException) -> bool:
    """`OSError`／`URLError`／`http.client` 系の通信例外か（timeout は対象外・`is_timeout_exc` を
    先に確認すること＝両方 True にはならない）。HTTP 応答が返っている失敗（`HTTPError`＝401/429/5xx）は
    通信エラーではないため対象外（型だけでは鍵誤設定/レート制限/サーバ障害を区別できない＝
    `from_exception` は None を返し NULL→unknown に落とす）。"""
    if is_timeout_exc(exc) or isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (OSError, urllib.error.URLError, http.client.HTTPException))


def from_exception(exc: BaseException) -> str | None:
    """例外の型だけから `stop_kind` を導く（`timeout`／`transport_error` のいずれにも
    当てはまらなければ `None`＝呼び出し元は `stop_kind` を立てず NULL のままにする）。
    メッセージ本文は見ない・返さない（型名だけを扱う契約）。"""
    if is_timeout_exc(exc):
        return "timeout"
    if is_transport_error_exc(exc):
        return "transport_error"
    return None


def resolve(env: dict) -> str | None:
    """`_result` の env（`codex_stopped_early`／`codex_silent_failure`／`agentic_failure`／`busy`／
    `data.evidence_packet.stop_reason` を含み得る）から `stop_kind` を1つ導く
    （`chat_service._finalize` の唯一の呼び出し元）。

    `None` を返すのは「このターンは完了として数えない・型も特定できない」場合＝呼び出し元は
    `stop_kind` を立てず NULL（集計側の `unknown`）に落とす: (a) `busy`（Codex 直列化で実行して
    いないターン）・(b) `agentic_failure="error"`（honest failure の定型文で終わったターン＝
    `chat_service._no_genuine_results` が列挙する経路: 未接続/無効 AI・下調べ役の失敗/設定不正・
    Codex の範囲限定不可/直読準備失敗・必須ツール不達・固定文言の縮退・素の会話の定型文。
    例外型は `_result` まで運ばれないため通信/その他を区別できない）。`agentic_failure="insufficient"`（メイン査読が根拠不足と判定した honest failure）
    は `no_evidence`、`"timeout"`／`"transport_error"`（素の会話でストリーム例外の型から
    `from_exception` が導けた場合）はその値。

    優先順位: busy → Codex 経路の印（silent→partial の順） → honest failure の印（`agentic_failure`）→
    `evidence_packet.stop_reason` → 既定 `completed`。`"timeout"`／`"transport_error"` は通信例外の
    型（`from_exception`）だけから導く値で、TIMEOUT-1 以降 Codex/チャット共通処理に独自の時間打ち切りは
    無い。経路は 2 つ: provider.run() が例外で落ちて `_result` 自体を yield できない経路
    （`routers/chat.py::_persist_turn_crash`・`_finalize` を経由しない独立した envelope）と、
    素の会話がストリーム例外を定型文へ倒した経路（`_plain_run` が `agentic_failure` に載せる）。
    """
    if env.get("busy"):
        return None
    if env.get("codex_silent_failure"):
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
    stop_reason = packet.get("stop_reason")
    if stop_reason in _BUDGET_STOP_REASONS:
        return "budget"
    if stop_reason in _NO_EVIDENCE_STOP_REASONS:
        return "no_evidence"
    return "completed"
