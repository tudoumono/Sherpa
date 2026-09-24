"""ターンの終了理由（`stop_kind`）の正規化（正典
`docs/proposals/2026-09-11-利用統計の拡充.md`）。

`messages.answer.stop_kind` に保存する8値の閉じた語彙と、複数の判定源（API 経路の
`evidence_packet.stop_reason`・Codex 経路の `codex_stopped_early`/`codex_silent_failure`・
`provider.run()` が例外で落ちて honest failure 本文になる経路の例外型）からの導出を
1箇所に集約する（唯一の真実源）。`stopped_by_user` は、巡ループの停止終端（`providers/base.py`
の "stopped"＝コードで組んだ未完了回答を保存する）のターンだけ `messages.answer.stop_kind` に
保存される。それ以外の停止は従来どおり assistant を保存せず（`chat_service.py::stream_message`/
`handle_message` の stopped 分岐）、監査 `chat.turn`（`detail.stopped`）から別途集計する——
利用統計の分布では二重に数えないよう、`stopped_by_user` は常に停止ターン側だけで数える。

`sherpa.chat_service`（`_finalize` が `resolve()` を呼ぶ）・`sherpa.providers.codex.provider`
（silent failure 分岐が env に印を立てる・値の解釈はしない）・`sherpa.routers.chat`
（`_persist_turn_crash` が `from_exception()` を直接使う）・`sherpa.providers.base`
（`_plain_run`／単発フォールバックがストリーム例外に `from_exception()` を直接使い
`agentic_failure` に載せる・honest failure の envelope に印を立てる）が本モジュールを参照する。
同じ印（`agentic_failure`・値の解釈はしない）を立てるだけの参照元は他に `sherpa.providers`
（未接続/無効 AI）・`sherpa.agentic_search`（`tools_blocked_env`）・`sherpa.chat_service`
（`_fixed_lens_result`）。
`sherpa` 内の他モジュールに依存しない葉モジュール（判定の唯一の実装＝
`agentic_search`/`ingest.arms.legacy_convert` はここを import して使う・重複させない）。
"""
from __future__ import annotations

import http.client
import socket
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
    （`agentic_search`／`ingest.arms.legacy_convert` のタイムアウト判定はこの関数を参照する）。"""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def is_transport_error_exc(exc: BaseException) -> bool:
    """通信境界の例外か（timeout は対象外・`is_timeout_exc` を先に確認すること＝両方 True にはならない）。
    対象は `ConnectionError`／`socket.gaierror`／`urllib.error.URLError`／`http.client.HTTPException`
    に限る——`OSError` 全体（`PermissionError`/`FileNotFoundError`/`OSError(ENOSPC)` 等のローカル
    I/O 障害を含む）を対象にすると、ワークスペースの権限/容量エラーが「通信障害」と誤記録される。
    HTTP 応答が返っている失敗（`HTTPError`＝401/429/5xx）は通信エラーではないため対象外
    （型だけでは鍵誤設定/レート制限/サーバ障害を区別できない＝`from_exception` は None を返し
    NULL→unknown に落とす）。"""
    if is_timeout_exc(exc) or isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (ConnectionError, socket.gaierror, urllib.error.URLError,
                            http.client.HTTPException))


def from_exception(exc: BaseException) -> str | None:
    """例外の型だけから `stop_kind` を導く（`timeout`／`transport_error` のいずれにも
    当てはまらなければ `None`＝呼び出し元は `stop_kind` を立てず NULL のままにする）。
    メッセージ本文は見ない・返さない（型名だけを扱う契約）。`raise RuntimeError(...) from e` で
    包まれた通信例外も拾えるよう `__cause__` を 1 段だけ見る（`health.py` の判定と同じ深さ）。"""
    for c in (exc, getattr(exc, "__cause__", None)):
        if c is None:
            continue
        if is_timeout_exc(c):
            return "timeout"
        if is_transport_error_exc(c):
            return "transport_error"
    return None


def is_main_task(packet: dict) -> bool:
    """`evidence_packet.task_id` がメインの調査ターンか（`"main"` のみ真）。
    `"sub:{profile_id}"`（ハイブリッドの下調べ役）・`"plan:..."`（複数プロファイルを束ねる
    プラン経路の複合 `stop_reason`）はいずれも偽——`_BUDGET_STOP_REASONS`／
    `_NO_EVIDENCE_STOP_REASONS` の完全一致判定はメイン調査の実測 `stop_reason` だけを
    対象にする契約のための共有述語（`chat_service._is_budget_exhausted` と同じ判定）。"""
    return packet.get("task_id") == "main"


def resolve(env: dict) -> str | None:
    """`_result` の env（`codex_stopped_early`／`codex_silent_failure`／`agentic_failure`／`busy`／
    `data.evidence_packet.stop_reason` を含み得る）から `stop_kind` を1つ導く
    （`chat_service._finalize` の唯一の呼び出し元）。

    `None` を返すのは「このターンは完了として数えない・型も特定できない」場合＝呼び出し元は
    `stop_kind` を立てず NULL（集計側の `unknown`）に落とす: (a) `busy`（Codex 直列化で実行して
    いないターン）・(b) `agentic_failure="error"`（honest failure の定型文で終わったターン＝
    `chat_service._no_genuine_results` が列挙する経路: 未接続/無効 AI・下調べ役の失敗/設定不正・
    Codex の範囲限定不可/直読準備失敗・必須ツール不達・固定文言の縮退・素の会話の定型文。
    加えて、下調べ役なしの巡で主張構造からの清書が未完了（終端未受信/出力上限）のまま実本文を
    保存した通常終端も同じ印を立てる＝完了として数えない。
    例外型は `_result` まで運ばれないため通信/その他を区別できない）。`agentic_failure="insufficient"`（メイン査読が根拠不足と判定した honest failure）
    は `no_evidence`、`"timeout"`／`"transport_error"`（素の会話でストリーム例外の型から
    `from_exception` が導けた場合）はその値。

    優先順位: busy → Codex 経路の印（silent のうち `limits.total_budget_hit` は budget へ格上げ→
    それ以外の silent→partial の順） → honest failure の印（`agentic_failure`）→
    `evidence_packet.stop_reason`（`is_main_task` が真の場合のみ・`"sub:..."`／`"plan:..."` は
    この判定をスキップして `completed` へ流す） → 既定 `completed`。`"timeout"`／`"transport_error"` は通信例外の
    型（`from_exception`）だけから導く値で、TIMEOUT-1 以降 Codex/チャット共通処理に独自の時間打ち切りは
    無い。経路は 2 つ: provider.run() が例外で落ちて `_result` 自体を yield できない経路
    （`routers/chat.py::_persist_turn_crash`・`_finalize` を経由しない独立した envelope）と、
    素の会話がストリーム例外を定型文へ倒した経路（`_plain_run` が `agentic_failure` に載せる）。

    戻り値が `None` でなければ必ず `STOP_KINDS` の要素——このモジュールの外に語彙が漏れないよう
    ここで検査する（`STOP_KINDS` を増やさずに新しい値を返す変更を入れると、ここで例外になる）。
    """
    kind = _resolve_kind(env)
    if kind is not None and kind not in STOP_KINDS:
        raise ValueError(f"stop_kind.resolve() produced a value outside STOP_KINDS: {kind!r}")
    return kind


def _resolve_kind(env: dict) -> str | None:
    if env.get("busy"):
        return None
    if env.get("stopped_by_user"):
        # DEPTH-2 S5: 利用者の停止で打ち切った未完了回答（`providers/base.py` の "stopped" 終端）。
        # 本文はコードで組んだ未完了回答＝完了として数えない。
        return "stopped_by_user"
    if env.get("codex_silent_failure"):
        # Codex の文脈枠超過（`env["limits"]["total_budget_hit"]`・provider.py の
        # `_CONTEXT_WINDOW_EXCEEDED_CODE` 分岐）は認証/ネットワーク不調と原因が異なる
        # ＝打切りの内訳（budget）へ数える。それ以外の無出力失敗は従来どおり codex_silent。
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
