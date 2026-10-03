"""LLM 利用量イベント（`usage_events`）の記録・読み出し。
チャット以外の LLM 呼び出しの利用量を記録する。チャット本回答の usage は `messages.answer->'usage'` に残り、`kind='chat'` はここに書かない。
書き手は `sherpa/metering.py`（`record()`）のみ。`sherpa.*` は import しない（`usage.py` と同じ）。
設計: docs/design/usage.md「`usage_events`（チャット以外の LLM 呼び出し）」
"""
from __future__ import annotations

import json
import math
import time

from .db import _connect, _ensure


def add_usage_event(*, kind, provider, model=None, input_tokens=None, cached_input_tokens=None,
                    output_tokens=None, reasoning_output_tokens=None, calls=1,
                    user_id=None, world=None, ts=None, elapsed_ms: int | None = None,
                    conversation_id: int | None = None, meta=None,
                    connect_timeout: float | None = None,
                    statement_timeout_ms: int | None = None) -> None:
    """1行 INSERT。トークン列の NULL はプロバイダが usage を返さなかったことを表す。
    `ts` はテスト用（None なら DB 既定の now()）。`elapsed_ms` は呼び出しの所要時間（None＝未計測）。
    `conversation_id` は会話別集計キー（None＝未確定）。`meta` は表示・分析用の付帯内訳（JSONB・課金集計は読まない）。
    `connect_timeout`/`statement_timeout_ms`（None＝無期限）は接続確立と SET statement_timeout の予算。
    `_ensure()`（初期化）に使った時間も予算から差し引き、残りが 0 以下なら接続せず `TimeoutError` を送出する。
    """
    budget_started = time.monotonic()
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            raise TimeoutError("add_usage_event: budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            budget_elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - budget_elapsed_ms))
            # SET LOCAL にして、返却後の接続へ statement_timeout を残さない。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        meta_json = json.dumps(meta, ensure_ascii=False) if meta is not None else None
        if ts is not None:
            c.execute(
                "INSERT INTO usage_events (ts, kind, provider, model, input_tokens, cached_input_tokens, "
                "  output_tokens, reasoning_output_tokens, calls, user_id, world, elapsed_ms, "
                "  conversation_id, meta) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ts, kind, provider, model, input_tokens, cached_input_tokens,
                 output_tokens, reasoning_output_tokens, calls, user_id, world, elapsed_ms,
                 conversation_id, meta_json))
        else:
            c.execute(
                "INSERT INTO usage_events (kind, provider, model, input_tokens, cached_input_tokens, "
                "  output_tokens, reasoning_output_tokens, calls, user_id, world, elapsed_ms, "
                "  conversation_id, meta) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (kind, provider, model, input_tokens, cached_input_tokens,
                 output_tokens, reasoning_output_tokens, calls, user_id, world, elapsed_ms,
                 conversation_id, meta_json))


def list_recent_events(kind: str, *, limit: int = 200) -> list:
    """`kind` 種別のイベントを新しい順で返す（world 単位の直近完了の通知用）。
    `world` が NULL の行は除く。既読管理は無く、毎回全件を読み直す。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT ts, world, calls FROM usage_events WHERE kind=%s AND world IS NOT NULL "
            "ORDER BY ts DESC LIMIT %s", (kind, limit)).fetchall()
