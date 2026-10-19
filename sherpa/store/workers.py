"""Web アプリと取り込みワーカーの心拍（`ingest_workers`）。取り込み画面の版の照合と、停止待ちの状況表示が読む。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

import hashlib

import psycopg

from .db import _connect, _dsn, _ensure

STALE_SEC = 45   # 心拍（10 秒ごと）がこの秒数書かれていない行は「動いていない」として扱う


def beat(worker_id, *, role, app_version, state="idle", run_id=None, current_file=None, pid=None) -> None:
    """自分の行を書く（無ければ作る）。best-effort で呼ぶこと（書けなくても本体の処理は止めない）。"""
    _ensure()
    with _connect() as c:
        c.execute(
            "INSERT INTO ingest_workers (worker_id, role, app_version, state, run_id, current_file, pid) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (worker_id) DO UPDATE SET app_version=EXCLUDED.app_version, state=EXCLUDED.state, "
            "  run_id=EXCLUDED.run_id, current_file=EXCLUDED.current_file, pid=EXCLUDED.pid, beat_at=now()",
            (worker_id, role, app_version, state, run_id, current_file, pid))


def purge_old(hours: int = 24) -> None:
    """何日も前に止まった行を消す（心拍が止まった行は表示から外れるだけで、消さないと溜まる）。"""
    _ensure()
    with _connect() as c:
        c.execute("DELETE FROM ingest_workers WHERE beat_at < now() - make_interval(hours => %s)", (hours,))


def list_live(role=None, *, stale_sec: int = STALE_SEC) -> list[dict]:
    """心拍が `stale_sec` 秒以内の行（新しい順）。`role` で web／ingest に絞る。"""
    _ensure()
    q = ("SELECT worker_id, role, app_version, state, run_id, current_file, pid, started_at, beat_at, "
         "  EXTRACT(EPOCH FROM (now() - beat_at))::int AS beat_age_sec "
         "FROM ingest_workers WHERE beat_at > now() - make_interval(secs => %s)")
    params: list = [stale_sec]
    if role is not None:
        q += " AND role=%s"
        params.append(role)
    q += " ORDER BY beat_at DESC"
    with _connect() as c:
        return c.execute(q, params).fetchall()


def _lock_key(worker_id: str) -> int:
    return int.from_bytes(hashlib.sha1(f"ingest-worker:{worker_id}".encode("utf-8")).digest()[:8], "big", signed=True)


def hold_liveness_lock(worker_id: str):
    """ワーカーが生きている間だけ持つ advisory lock（プロセスが死ねば DB が自動で外す）。戻り値の接続を閉じない限り保持する。"""
    _ensure()
    conn = psycopg.connect(_dsn(), autocommit=True)
    conn.execute("SELECT pg_advisory_lock(%s)", (_lock_key(worker_id),))
    return conn


def is_dead(worker_id: str) -> bool:
    """そのワーカーの lock を取れたら（＝持ち主が居ない）True。取れたらすぐ手放す。"""
    _ensure()
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_lock_key(worker_id),)).fetchone()[0]
        if got:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(worker_id),))
        return bool(got)
    finally:
        conn.close()
