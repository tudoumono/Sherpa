"""LLM 利用量イベント（S1・2026-07-15-LLMオーケストレーション実装計画.md §3）。

チャット以外の LLM 呼び出し（intent 分類・グラフ抽出・概念候補提案・埋め込み・admin グラフ質問・
VLM 視覚読み取り）の利用量を記録する。チャット本回答の usage は引き続き `messages.answer->'usage'`
に残る（`kind='chat'` はここには書かない・`store/usage.py::usage_stats` が集計時に合成する）。

書き手は `sherpa/metering.py`（`record()`）限定。本モジュールは kind のバリデーションをしない
（内部限定の単純 INSERT）。`sherpa.*` は import しない（`usage.py` と同じ規約）。
"""
from __future__ import annotations

import math
import time

from .db import _connect, _ensure


def add_usage_event(*, kind, provider, model=None, input_tokens=None, cached_input_tokens=None,
                    output_tokens=None, reasoning_output_tokens=None, calls=1,
                    user_id=None, world=None, ts=None,
                    connect_timeout: float | None = None,
                    statement_timeout_ms: int | None = None) -> None:
    """1行 INSERT。トークン列は NULLABLE（NULL＝プロバイダが usage を返さなかった「報告不能」マーカー）。

    `ts` はテスト用（期間外の行を仕込むため）。None なら DB 側の既定（`now()`）を使う。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限＝既存呼び出し元は
    無変更）: `sherpa.metering.record()` がそのまま転送する（PART-4 は「記録は失敗しても構わない
    が無期限にはブロックしない」ための短い固定予算を渡す）。`store.worlds.get_world` と同じ方式
    （`connect_timeout` は整数秒へ切り上げ・最小1秒でクランプ、`statement_timeout` は接続確立
    **後**に `SET` で発行し、接続に要した時間ぶんを差し引く）。

    未初期化（`_inited=False`）時は `_ensure()` が内部で `init_schema()` を起動しうる——この分の
    経過時間も同じ予算から差し引く（`store.worlds.get_world`/`store.db.init_schema` と同型の
    是正・差し引かないと本関数自身の接続へ満額が再付与され実時間が最大約2倍まで伸びうる）。
    差し引いた残りが0以下なら、最低1秒へクランプして接続を開始することはせず、
    `TimeoutError` を送出して接続自体を開始しない（`metering.record()` は自身でも同じ確認を
    行うが、本関数を直接呼ぶ他の経路のための多層防御）。
    """
    budget_started = time.monotonic()   # `_ensure()` の消費分も差し引くため、その呼び出し前から計測する
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            raise TimeoutError("add_usage_event: budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL（session-level ではなく）: プール導入後（性能台帳#17 QW2）、この
            # with ブロック＝単一トランザクションの間だけ有効にし、返却後の接続に
            # statement_timeout が残らないようにする（GUC 汚染防止・commit/rollback で自動消滅）。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        if ts is not None:
            c.execute(
                "INSERT INTO usage_events (ts, kind, provider, model, input_tokens, cached_input_tokens, "
                "  output_tokens, reasoning_output_tokens, calls, user_id, world) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ts, kind, provider, model, input_tokens, cached_input_tokens,
                 output_tokens, reasoning_output_tokens, calls, user_id, world))
        else:
            c.execute(
                "INSERT INTO usage_events (kind, provider, model, input_tokens, cached_input_tokens, "
                "  output_tokens, reasoning_output_tokens, calls, user_id, world) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (kind, provider, model, input_tokens, cached_input_tokens,
                 output_tokens, reasoning_output_tokens, calls, user_id, world))


def list_recent_events(kind: str, *, limit: int = 200) -> list:
    """`kind` 種別のイベントを新しい順で返す（NOTIFY-1・world 単位の直近完了を拾うための軽量読み出し）。

    `world` が NULL の行（背景処理に利用者コンテキストが無い等）は world 単位の通知に使えないため除く。
    既読管理は無し（毎回全件読み直す）——`kind` の呼び出し元限定の内部語彙は `add_usage_event` と同じく
    ここではバリデーションしない。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT ts, world, calls FROM usage_events WHERE kind=%s AND world IS NOT NULL "
            "ORDER BY ts DESC LIMIT %s", (kind, limit)).fetchall()
