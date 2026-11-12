"""Codex ジョブ（外部 API の非同期 Codex 実行）の永続化。読み書きの唯一の窓口。
状態は `queued`/`running`/`completed`/`failed`/`cancelled`/`expired` の6値。`finished_at` から7日（`expires_at`）を過ぎた終端ジョブは `expire_due_jobs()` が `expired` にして質問文・回答・出典・未確認項目を NULL へ消す。
待ち列には上限がある（全体・鍵ごと）。受付時に待ち期限（`queue_deadline`）を保存し、期限を過ぎた `queued` は `failed(error_code='queue_timeout')` で終わらせる。
設計: docs/design/external-api.md「Codex ジョブ（非同期）」
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from . import outbox
from .db import _connect, _ensure

_log = logging.getLogger(__name__)

RETENTION_DAYS = 7

# 待ち列の上限（受付時に数える）と待ち期限。超える受付は断る
QUEUE_LIMIT_TOTAL = 40
QUEUE_LIMIT_PER_KEY = 10
QUEUE_WAIT_MINUTES = 30
_QUEUE_LOCK = 0x43584A51  # 受付の件数確認と INSERT を直列化する固定 advisory lock key（"CXJQ"）

# 受付前から残る `queued`（期限の列が NULL）は作成時刻から数える
_DEADLINE = "COALESCE(queue_deadline, created_at + make_interval(mins => %d))" % QUEUE_WAIT_MINUTES

# 状態の閉じた語彙（`expired` は遷移元に含めない）。
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class QueueFull(Exception):
    """待ち列が上限に達していて受け付けられない。`scope` は `total`（全体）か `key`（鍵ごと）。"""

    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


def _prepare() -> None:
    """終端化の前に、スキーマと配送待ちの表を作っておく（トランザクションの中で別の接続を取らないため）。"""
    _ensure()
    outbox.ensure_table()


@contextmanager
def _terminal_tx():
    """終端化のトランザクション。`(接続, dropped)` を渡し、確定した後で `dropped`（配送待ちが上限で保存できなかった通知）を監査に残す。"""
    dropped: list[tuple[int, dict]] = []
    with _connect() as c:
        yield c, dropped
    from .. import webhooks
    for key_id, payload in dropped:
        webhooks.record_dropped(key_id, payload)


def _notify_terminal(c, dropped: list, rows: list[dict], status: str) -> None:
    """終端化した行のうち `webhook=true` のものの終了通知を、終端化の UPDATE と同じトランザクション（`c`）で配送待ちへ保存する（通知だけが消えない・遷移1回につき高々1回）。
    配送待ちが上限のときは通知だけ `dropped` に積んで終端化は進める。それ以外の失敗は終端化ごと戻す。
    """
    from .. import webhooks
    for r in rows:
        if not r.get("webhook"):
            continue
        payload = webhooks.codex_job_payload(r["id"], status, r["finished_at"], completion=r.get("completion"),
                                             stop_kind=r.get("stop_kind"), error_code=r.get("error_code"))
        try:
            outbox.add_in(c, "webhook", payload, key_id=r["key_id"])
        except outbox.OutboxFull:
            dropped.append((r["key_id"], payload))


def new_job_id() -> str:
    return uuid.uuid4().hex


def insert_job(*, job_id: str, key_id: int, world: str, query: str,
               scope_paths: list[str], depth: str, webhook: bool = False) -> dict:
    """新規ジョブを `queued` で登録し、ジョブ行（`get_job` と同じ形）を返す。
    待ちの件数の確認と INSERT は同じトランザクション（advisory lock で直列化）で行い、上限を超える受付は `QueueFull`。
    """
    _ensure()
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_QUEUE_LOCK,))
        n = c.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE key_id=%s) AS mine "
            f"FROM codex_jobs WHERE status='queued' AND {_DEADLINE} >= now()",
            (key_id,)).fetchone()
        if n["total"] >= QUEUE_LIMIT_TOTAL:
            raise QueueFull("total")
        if n["mine"] >= QUEUE_LIMIT_PER_KEY:
            raise QueueFull("key")
        return c.execute(
            "INSERT INTO codex_jobs (id, key_id, world, query, scope_paths, depth, status, webhook, "
            "  queue_deadline) "
            "VALUES (%s,%s,%s,%s,%s,%s,'queued',%s, now() + make_interval(mins => %s)) "
            "RETURNING id, key_id, world, query, scope_paths, depth, status, error_code, "
            "  answer, sources, unconfirmed_items, investigation, elapsed_ms, created_at, "
            "  started_at, finished_at, expires_at, completion, stop_kind",
            (job_id, key_id, world, query, scope_paths, depth, webhook, QUEUE_WAIT_MINUTES),
        ).fetchone()


def get_job(job_id: str) -> dict | None:
    """ジョブ1件（無ければ None）。`key_id`/`allowed_worlds` の確認は呼び出し側が行う（ここではスコープ判定しない）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, key_id, world, query, scope_paths, depth, status, error_code, "
            "  answer, sources, unconfirmed_items, investigation, elapsed_ms, created_at, "
            "  started_at, finished_at, expires_at, completion, stop_kind "
            "FROM codex_jobs WHERE id=%s", (job_id,),
        ).fetchone()


def claim_queued_jobs(limit: int) -> list[dict]:
    """待ちジョブを古い順に最大 `limit` 件 `running` へ遷移させて返す（`FOR UPDATE SKIP LOCKED`）。`limit<=0` なら何もしない。"""
    if limit <= 0:
        return []
    _ensure()
    with _connect() as c:
        return c.execute(
            "UPDATE codex_jobs SET status='running', started_at=now() "
            "WHERE id IN (SELECT id FROM codex_jobs WHERE status='queued' "
            f"  AND {_DEADLINE} >= now() "
            "  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT %s) "
            "RETURNING id, key_id, world, query, scope_paths, depth, status, created_at, "
            "  started_at",
            (limit,),
        ).fetchall()


def mark_completed(job_id: str, *, answer: str, sources: list, unconfirmed_items: list,
                   elapsed_ms: int, investigation: dict | None = None,
                   completion: str | None = None, stop_kind: str | None = None) -> None:
    """`investigation`（省略可）: 調査の記録（`trim_to_budget` 済みの `{complete, truncated, manifest, items, coverage}`）。None＝台帳ゲートが走らなかったジョブ。
    `completion`（`complete`/`partial`）と `stop_kind` は回答の完了状態と終了理由。途中までの回答（`partial`）は結果・通知にそのまま出る。
    """
    _prepare()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _terminal_tx() as (c, dropped):
        rows = c.execute(
            "UPDATE codex_jobs SET status='completed', answer=%s, sources=%s, "
            "  unconfirmed_items=%s, investigation=%s, elapsed_ms=%s, finished_at=now(), "
            "  expires_at=%s, completion=%s, stop_kind=%s "
            "WHERE id=%s AND status='running' RETURNING id, key_id, webhook, finished_at, completion, stop_kind",
            (answer, Json(sources), Json(unconfirmed_items),
             Json(investigation) if investigation is not None else None,
             elapsed_ms, expires_at, completion, stop_kind, job_id)).fetchall()
        _notify_terminal(c, dropped, rows, "completed")


def mark_failed(job_id: str, *, error_code: str, from_status: str = "running",
                completion: str | None = None, stop_kind: str | None = None) -> None:
    """`error_code` は閉じた語彙（`interrupted`/`timeout`/`codex_failed`）。`from_status`（既定 `running`）を条件にし、終端化済みのジョブは書き換えない。
    `completion`/`stop_kind` は回答側が報告した完了状態と終了理由（省略可）。
    """
    _prepare()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _terminal_tx() as (c, dropped):
        rows = c.execute(
            "UPDATE codex_jobs SET status='failed', error_code=%s, finished_at=now(), "
            "  expires_at=%s, completion=%s, stop_kind=%s WHERE id=%s AND status=%s "
            "RETURNING id, key_id, webhook, finished_at, error_code, completion, stop_kind",
            (error_code, expires_at, completion, stop_kind, job_id, from_status)).fetchall()
        _notify_terminal(c, dropped, rows, "failed")


def mark_cancelled(job_id: str, *, from_status: str = "running") -> bool:
    """`from_status`（既定 `running`）の行だけを原子的に `cancelled` へ確定し、実際に遷移させたかを返す。
    False は既に別状態へ進んでいたことを表し、呼び出し側は取消が勝ったかの判定に使う。
    """
    _prepare()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _terminal_tx() as (c, dropped):
        row = c.execute(
            "UPDATE codex_jobs SET status='cancelled', finished_at=now(), expires_at=%s "
            "WHERE id=%s AND status=%s RETURNING id, key_id, webhook, finished_at",
            (expires_at, job_id, from_status),
        ).fetchone()
        _notify_terminal(c, dropped, [row] if row else [], "cancelled")
    return row is not None


def cancel_if_queued(job_id: str) -> bool:
    """`queued` のジョブだけを即座に `cancelled` にし、実際に遷移させたかを返す（`running` は呼び出し側がプロセスを止めてから `mark_cancelled`）。"""
    _prepare()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _terminal_tx() as (c, dropped):
        row = c.execute(
            "UPDATE codex_jobs SET status='cancelled', finished_at=now(), expires_at=%s "
            "WHERE id=%s AND status='queued' RETURNING id, key_id, webhook, finished_at",
            (expires_at, job_id),
        ).fetchone()
        _notify_terminal(c, dropped, [row] if row else [], "cancelled")
    return row is not None


def recover_interrupted_on_startup() -> list[str]:
    """起動時、前回プロセスが `running` のまま残したジョブを `failed`（`error_code='interrupted'`）にし、格下げした job_id の一覧を返す（`queued` は対象外）。"""
    _prepare()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _terminal_tx() as (c, dropped):
        rows = c.execute(
            "UPDATE codex_jobs SET status='failed', error_code='interrupted', "
            "  finished_at=now(), expires_at=%s WHERE status='running' "
            "RETURNING id, key_id, webhook, finished_at",
            (expires_at,),
        ).fetchall()
        _notify_terminal(c, dropped, rows, "failed")
    return [r["id"] for r in rows]


def fail_overdue_queued(limit: int = 500) -> int:
    """待ち期限（`queue_deadline`）を過ぎた `queued` を `failed(error_code='queue_timeout')` にし、終了通知を積む。処理件数を返す。"""
    _prepare()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _terminal_tx() as (c, dropped):
        rows = c.execute(
            "UPDATE codex_jobs SET status='failed', error_code='queue_timeout', "
            "  finished_at=now(), expires_at=%s "
            f"WHERE id IN (SELECT id FROM codex_jobs WHERE status='queued' AND {_DEADLINE} < now() "
            "  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT %s) "
            "RETURNING id, key_id, webhook, finished_at",
            (expires_at, limit)).fetchall()
        _notify_terminal(c, dropped, rows, "failed")
    return len(rows)


def fail_if_overdue(job: dict) -> dict:
    """読み取りの時点で待ち期限を過ぎた `queued` なら、原子的に `failed(queue_timeout)` へ移して最新の行を返す。そうでなければ `job` をそのまま返す。"""
    if job["status"] != "queued":
        return job
    _prepare()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _terminal_tx() as (c, dropped):
        rows = c.execute(
            "UPDATE codex_jobs SET status='failed', error_code='queue_timeout', "
            "  finished_at=now(), expires_at=%s "
            f"WHERE id=%s AND status='queued' AND {_DEADLINE} < now() "
            "RETURNING id, key_id, webhook, finished_at",
            (expires_at, job["id"])).fetchall()
        _notify_terminal(c, dropped, rows, "failed")
    return get_job(job["id"]) or job


def expire_due_jobs(limit: int = 500) -> int:
    """待ち期限切れの `queued` を `failed` にしたうえで、保存期間（7日）を過ぎた終端ジョブ（completed/failed/cancelled）を `expired` にし、質問文・回答・出典・未確認項目・調査の記録を消す。`expired` にした件数を返す。"""
    fail_overdue_queued(limit)
    with _connect() as c:
        rows = c.execute(
            "UPDATE codex_jobs SET status='expired', query=NULL, answer=NULL, sources=NULL, "
            "  unconfirmed_items=NULL, investigation=NULL "
            "WHERE id IN (SELECT id FROM codex_jobs "
            "  WHERE status IN ('completed','failed','cancelled') AND expires_at < now() "
            "  LIMIT %s) RETURNING id",
            (limit,),
        ).fetchall()
        return len(rows)


def expire_job_if_due(job_id: str, row: dict) -> dict:
    """1件の読み取り時点での期限切れ判定。`row` は `get_job()` の戻り値で、期限超過なら `row["id"]` 自身だけを消去する。期限切れでなければ `row` をそのまま返す。"""
    if row["status"] in TERMINAL_STATUSES and row.get("expires_at") is not None:
        if datetime.now(timezone.utc) >= row["expires_at"]:
            _ensure()
            with _connect() as c:
                fresh = c.execute(
                    "UPDATE codex_jobs SET status='expired', query=NULL, answer=NULL, "
                    "  sources=NULL, unconfirmed_items=NULL, investigation=NULL "
                    "WHERE id=%s AND status IN ('completed','failed','cancelled') "
                    "  AND expires_at < now() "
                    "RETURNING id, key_id, world, query, scope_paths, depth, status, "
                    "  error_code, answer, sources, unconfirmed_items, investigation, "
                    "  elapsed_ms, created_at, started_at, finished_at, expires_at, completion, stop_kind",
                    (job_id,),
                ).fetchone()
            return fresh if fresh is not None else row
    return row
