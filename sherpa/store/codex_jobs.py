"""Codex ジョブ（外部 API の非同期 Codex 実行）の永続化。読み書きの唯一の窓口。
状態は `queued`/`running`/`completed`/`failed`/`cancelled`/`expired` の6値。`finished_at` から7日（`expires_at`）を過ぎた終端ジョブは `expire_due_jobs()` が `expired` にして質問文・回答・出典・未確認項目を NULL へ消す。
設計: docs/design/external-api.md「Codex ジョブ（非同期）」
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from .db import _connect, _ensure

_log = logging.getLogger(__name__)

RETENTION_DAYS = 7

# 状態の閉じた語彙（`expired` は遷移元に含めない）。
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _notify_terminal(rows: list[dict], status: str) -> None:
    """終端化した行のうち `webhook=true` のものだけ終了通知を積む。終端化の UPDATE が実際に行を更新したときだけ呼ぶ（遷移1回につき通知は高々1回・失敗してもジョブの終端化は戻さない）。"""
    for r in rows:
        if not r.get("webhook"):
            continue
        try:
            from .. import webhooks
            webhooks.notify_codex_job_terminal(r["id"], r["key_id"], status, r["finished_at"])
        except Exception:
            _log.warning("Codex ジョブの終了通知に失敗しました", exc_info=True)


def new_job_id() -> str:
    return uuid.uuid4().hex


def insert_job(*, job_id: str, key_id: int, world: str, query: str,
               scope_paths: list[str], depth: str, webhook: bool = False) -> dict:
    """新規ジョブを `queued` で登録し、ジョブ行（`get_job` と同じ形）を返す。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO codex_jobs (id, key_id, world, query, scope_paths, depth, status, webhook) "
            "VALUES (%s,%s,%s,%s,%s,%s,'queued',%s) "
            "RETURNING id, key_id, world, query, scope_paths, depth, status, error_code, "
            "  answer, sources, unconfirmed_items, investigation, elapsed_ms, created_at, "
            "  started_at, finished_at, expires_at",
            (job_id, key_id, world, query, scope_paths, depth, webhook),
        ).fetchone()


def get_job(job_id: str) -> dict | None:
    """ジョブ1件（無ければ None）。`key_id`/`allowed_worlds` の確認は呼び出し側が行う（ここではスコープ判定しない）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, key_id, world, query, scope_paths, depth, status, error_code, "
            "  answer, sources, unconfirmed_items, investigation, elapsed_ms, created_at, "
            "  started_at, finished_at, expires_at "
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
            "  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT %s) "
            "RETURNING id, key_id, world, query, scope_paths, depth, status, created_at, "
            "  started_at",
            (limit,),
        ).fetchall()


def mark_completed(job_id: str, *, answer: str, sources: list, unconfirmed_items: list,
                   elapsed_ms: int, investigation: dict | None = None) -> None:
    """`investigation`（省略可）: 調査の記録（`trim_to_budget` 済みの `{complete, truncated, manifest, items, coverage}`）。None＝台帳ゲートが走らなかったジョブ。"""
    _ensure()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _connect() as c:
        rows = c.execute(
            "UPDATE codex_jobs SET status='completed', answer=%s, sources=%s, "
            "  unconfirmed_items=%s, investigation=%s, elapsed_ms=%s, finished_at=now(), "
            "  expires_at=%s "
            "WHERE id=%s AND status='running' RETURNING id, key_id, webhook, finished_at",
            (answer, Json(sources), Json(unconfirmed_items),
             Json(investigation) if investigation is not None else None,
             elapsed_ms, expires_at, job_id)).fetchall()
    _notify_terminal(rows, "completed")


def mark_failed(job_id: str, *, error_code: str, from_status: str = "running") -> None:
    """`error_code` は閉じた語彙（`interrupted`/`timeout`/`codex_failed`）。`from_status`（既定 `running`）を条件にし、終端化済みのジョブは書き換えない。"""
    _ensure()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _connect() as c:
        rows = c.execute(
            "UPDATE codex_jobs SET status='failed', error_code=%s, finished_at=now(), "
            "  expires_at=%s WHERE id=%s AND status=%s RETURNING id, key_id, webhook, finished_at",
            (error_code, expires_at, job_id, from_status)).fetchall()
    _notify_terminal(rows, "failed")


def mark_cancelled(job_id: str, *, from_status: str = "running") -> bool:
    """`from_status`（既定 `running`）の行だけを原子的に `cancelled` へ確定し、実際に遷移させたかを返す。
    False は既に別状態へ進んでいたことを表し、呼び出し側は取消が勝ったかの判定に使う。
    """
    _ensure()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _connect() as c:
        row = c.execute(
            "UPDATE codex_jobs SET status='cancelled', finished_at=now(), expires_at=%s "
            "WHERE id=%s AND status=%s RETURNING id, key_id, webhook, finished_at",
            (expires_at, job_id, from_status),
        ).fetchone()
    _notify_terminal([row] if row else [], "cancelled")
    return row is not None


def cancel_if_queued(job_id: str) -> bool:
    """`queued` のジョブだけを即座に `cancelled` にし、実際に遷移させたかを返す（`running` は呼び出し側がプロセスを止めてから `mark_cancelled`）。"""
    _ensure()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _connect() as c:
        row = c.execute(
            "UPDATE codex_jobs SET status='cancelled', finished_at=now(), expires_at=%s "
            "WHERE id=%s AND status='queued' RETURNING id, key_id, webhook, finished_at",
            (expires_at, job_id),
        ).fetchone()
    _notify_terminal([row] if row else [], "cancelled")
    return row is not None


def recover_interrupted_on_startup() -> list[str]:
    """起動時、前回プロセスが `running` のまま残したジョブを `failed`（`error_code='interrupted'`）にし、格下げした job_id の一覧を返す（`queued` は対象外）。"""
    _ensure()
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _connect() as c:
        rows = c.execute(
            "UPDATE codex_jobs SET status='failed', error_code='interrupted', "
            "  finished_at=now(), expires_at=%s WHERE status='running' "
            "RETURNING id, key_id, webhook, finished_at",
            (expires_at,),
        ).fetchall()
    _notify_terminal(rows, "failed")
    return [r["id"] for r in rows]


def expire_due_jobs(limit: int = 500) -> int:
    """保存期間（7日）を過ぎた終端ジョブ（completed/failed/cancelled）を `expired` にし、質問文・回答・出典・未確認項目・調査の記録を消す。処理件数を返す。"""
    _ensure()
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
                    "  elapsed_ms, created_at, started_at, finished_at, expires_at",
                    (job_id,),
                ).fetchone()
            return fresh if fresh is not None else row
    return row
