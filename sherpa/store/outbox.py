"""配送待ち（`delivery_outbox`）の永続化。Webhook 通知と外部 API 監査の「送る・書く」を、先にここへ保存してから行う。読み書きの唯一の窓口。
状態は `pending`/`done`/`gave_up` の3値。`pending` を `claim_due()` が貸出（`locked_until`）つきで取り出すので、同じ行を同時に2つの係が扱わない。
完了（`done`）と諦め（`gave_up`・理由は `last_error`）の行は `RETENTION_DAYS` 日で `purge_finished()` が消す。
表の作成はこのモジュールが持つ（`_SCHEMA` には含めず、最初の利用時に 1 回だけ冪等に作る）。
設計: docs/design/external-api.md「配送待ち」
"""
from __future__ import annotations

import threading

from psycopg.types.json import Json

from .db import _connect, _dsn, _ensure

KINDS = ("webhook", "ext_audit")
MAX_PENDING_PER_KIND = 1000
LEASE_SECONDS = 120
RETENTION_DAYS = 7

_DDL = (
    """CREATE TABLE IF NOT EXISTS delivery_outbox (
        id BIGSERIAL PRIMARY KEY,
        kind TEXT NOT NULL,
        key_id BIGINT,
        payload JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        locked_until TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS delivery_outbox_due ON delivery_outbox(next_attempt_at) WHERE status='pending'",
    "CREATE INDEX IF NOT EXISTS delivery_outbox_finished ON delivery_outbox(finished_at) WHERE status<>'pending'",
)

_ready_dsn: str | None = None
_ready_lock = threading.Lock()
_ADD_LOCK = 0x4F425859  # 件数の確認と INSERT を直列化する固定 advisory lock key（"OBXY"）


class OutboxFull(Exception):
    """この種類の `pending` が上限（`MAX_PENDING_PER_KIND`）に達していて保存できない。"""


def ensure_table() -> None:
    """接続先ごとに 1 回だけ表を作る（`_SCHEMA` の初期化が済んでから）。"""
    global _ready_dsn
    dsn = _dsn()
    if _ready_dsn == dsn:
        return
    with _ready_lock:
        if _ready_dsn == dsn:
            return
        _ensure()
        with _connect() as c:
            for stmt in _DDL:
                c.execute(stmt)
        _ready_dsn = dsn


def add_in(c, kind: str, payload: dict, *, key_id: int | None = None) -> int:
    """（表は `ensure_table()` で先に作っておく）呼び出し側のトランザクション（接続 `c`）の中で1件を `pending` で保存して id を返す。`kind` の `pending` が上限なら `OutboxFull`。"""
    if kind not in KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    c.execute("SELECT pg_advisory_xact_lock(%s)", (_ADD_LOCK,))
    n = c.execute("SELECT count(*) AS n FROM delivery_outbox WHERE kind=%s AND status='pending'",
                  (kind,)).fetchone()["n"]
    if n >= MAX_PENDING_PER_KIND:
        raise OutboxFull(kind)
    return c.execute(
        "INSERT INTO delivery_outbox (kind, key_id, payload) VALUES (%s,%s,%s) RETURNING id",
        (kind, key_id, Json(payload))).fetchone()["id"]


def add(kind: str, payload: dict, *, key_id: int | None = None) -> int:
    """`add_in` を自前のトランザクションで行う。"""
    ensure_table()
    with _connect() as c:
        return add_in(c, kind, payload, key_id=key_id)


def claim_due(kinds: tuple[str, ...], limit: int) -> list[dict]:
    """送る時刻が来て貸出中でもない `pending` を古い順に最大 `limit` 件取り出す。`attempts` を1つ進め、`LEASE_SECONDS` 秒の貸出を付ける（`FOR UPDATE SKIP LOCKED`）。"""
    ensure_table()
    with _connect() as c:
        return c.execute(
            "UPDATE delivery_outbox SET attempts=attempts+1, "
            "  locked_until=now() + make_interval(secs => %s) "
            "WHERE id IN (SELECT id FROM delivery_outbox WHERE status='pending' AND kind = ANY(%s) "
            "  AND next_attempt_at <= now() AND locked_until <= now() "
            "  ORDER BY next_attempt_at, id FOR UPDATE SKIP LOCKED LIMIT %s) "
            "RETURNING id, kind, key_id, payload, attempts, locked_until",
            (LEASE_SECONDS, list(kinds), limit)).fetchall()


_HELD = "id=%s AND status='pending' AND attempts=%s AND locked_until=%s"


def _held(row: dict) -> tuple:
    return (row["id"], row["attempts"], row["locked_until"])


def release(rows: list[dict]) -> None:
    """取り出したが送らなかった行の貸出を解き、試行に数えず、すぐ次の送り手が取れる状態に戻す。`claim_due()` が返した貸出がまだ自分のものである行だけを解く。"""
    if not rows:
        return
    ensure_table()
    with _connect() as c:
        for row in rows:
            c.execute("UPDATE delivery_outbox SET locked_until=now(), attempts=GREATEST(attempts-1, 0) "
                      f"WHERE {_HELD}", _held(row))


def holds(c, row: dict) -> bool:
    """`claim_due()` が返した貸出がまだ自分のものか。接続 `c` の中で行を `FOR UPDATE` で押さえ、確かめた後は同じトランザクションで書ける。"""
    return c.execute(f"SELECT 1 FROM delivery_outbox WHERE {_HELD} FOR UPDATE", _held(row)).fetchone() is not None


def mark_done(row: dict) -> None:
    """貸出を失った送り手（期限切れで別の送り手が借り直した）は行を書き換えない（以下 `mark_retry`・`mark_gave_up` も同じ）。"""
    ensure_table()
    with _connect() as c:
        c.execute(f"UPDATE delivery_outbox SET status='done', finished_at=now() WHERE {_HELD}", _held(row))


def mark_retry(row: dict, *, delay_seconds: float, error: str) -> None:
    """貸出を外して `delay_seconds` 秒後に再び送る対象にする。"""
    ensure_table()
    with _connect() as c:
        c.execute("UPDATE delivery_outbox SET next_attempt_at=now() + make_interval(secs => %s), "
                  f"  locked_until=now(), last_error=%s WHERE {_HELD}",
                  (delay_seconds, error[:500], *_held(row)))


def mark_gave_up(row: dict, *, reason: str) -> bool:
    """諦めを記録できたか（貸出を失っていたら False）。"""
    ensure_table()
    with _connect() as c:
        return c.execute("UPDATE delivery_outbox SET status='gave_up', last_error=%s, finished_at=now() "
                         f"WHERE {_HELD} RETURNING id", (reason[:500], *_held(row))).fetchone() is not None


def pending_count(kinds: tuple[str, ...] = KINDS) -> int:
    ensure_table()
    with _connect() as c:
        return c.execute("SELECT count(*) AS n FROM delivery_outbox WHERE status='pending' "
                         "AND kind = ANY(%s)", (list(kinds),)).fetchone()["n"]


def purge_finished() -> int:
    """`done`/`gave_up` のうち `RETENTION_DAYS` 日を過ぎた行を消し、消した件数を返す。"""
    ensure_table()
    with _connect() as c:
        return len(c.execute(
            "DELETE FROM delivery_outbox WHERE status<>'pending' "
            "AND finished_at < now() - make_interval(days => %s) RETURNING id",
            (RETENTION_DAYS,)).fetchall())
