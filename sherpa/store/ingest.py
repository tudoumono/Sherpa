"""取り込み run（`ingest_runs`）の記録・参照。
設計: docs/design/data.md「KB（取り込み・範囲・同一性）」
"""
from __future__ import annotations

import math
import time

from psycopg.types.json import Json

from .db import _KB_ID, _connect, _ensure
from .failed_docs import replace_failed_docs_in


def add_ingest_run(world, layer="version", status="auto_published", source_doc_ids=None,
                   extraction_snapshot=None, published_snapshot=None, ingest_source_id=None,
                   scan_root=None, scope_mapping_overrides=None, created_by="admin") -> dict:
    """完了した取り込み run を1行記録する。実際に Neo4j へ反映したときだけ `published_at` を入れる。
    列名 `version`・layer 値 `'version'` は DB 上の既存名で、引数は world 用語。
    """
    _ensure()
    published = published_snapshot is not None  # 反映実体があるときだけ入れる（reflect=False/failed は NULL）。
    with _connect() as c:
        return c.execute(
            "INSERT INTO ingest_runs (kb_id, version, layer, ingest_source_id, scan_root, scope_mapping_overrides, "
            "  source_doc_ids, status, extraction_snapshot, published_snapshot, created_by, published_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() ELSE NULL END) "
            "RETURNING id, version, layer, status, source_doc_ids, extraction_snapshot, created_at, published_at",
            (_KB_ID, world, layer, ingest_source_id, scan_root,
             Json(scope_mapping_overrides) if scope_mapping_overrides else None,
             Json(source_doc_ids or []), status, Json(extraction_snapshot or {}),
             Json(published_snapshot) if published_snapshot is not None else None,
             created_by, published)).fetchone()


def start_ingest_run(world, layer="version", scan_root=None, scope_mapping_overrides=None,
                     ingest_source_id=None, created_by="admin", progress=None) -> dict:
    """取り込み run を開始時点で1行 INSERT する（`status='extracting'`）。完了時は `finish_ingest_run`（同じ行の UPDATE）で締める。行の存在が「実行が始まった」事実になり、起動時の孤児 `extracting` 検知の前提になる。
    `progress` は INSERT と同時に確定する初期進捗（省略時 NULL）。戻り値は `id` を含み、呼び出し元が run_id として使う。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO ingest_runs (kb_id, version, layer, ingest_source_id, scan_root, "
            "  scope_mapping_overrides, status, progress, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,'extracting',%s,%s) "
            "RETURNING id, version, layer, status, progress, created_at",
            (_KB_ID, world, layer, ingest_source_id, scan_root,
             Json(scope_mapping_overrides) if scope_mapping_overrides else None,
             Json(progress) if progress is not None else None,
             created_by)).fetchone()


def fail_close_if_extracting(run_id, *, reason: str) -> bool:
    """`status='extracting'` のままの行だけを `failed` へ CAS で落とす。
    背景実行の最外周（`sherpa.ingest.background`）のセーフティネット専用: 各操作は自分の run を自分で terminal 化する契約で、果たされなかったときだけここが拾う。条件付き UPDATE にして、理由付きで確定済みの行を上書きしない。
    戻り値は実際に更新したか。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "UPDATE ingest_runs SET status='failed', progress=NULL, "
            "  extraction_snapshot=jsonb_set("
            "    COALESCE(extraction_snapshot, '{}'::jsonb), '{flags}', "
            "    COALESCE(extraction_snapshot->'flags', '[]'::jsonb) || "
            "      jsonb_build_array(jsonb_build_object('doc', NULL, 'action', 'blocked', 'reason', %s::text)), "
            "    true) "
            "WHERE kb_id=%s AND id=%s AND status='extracting' "
            "RETURNING id", (reason, _KB_ID, run_id)).fetchone()
        return row is not None


def finish_ingest_run_and_confirm_world(run_id, world, *, status, extraction_snapshot=None,
                                        published_snapshot=None, source_doc_ids=None,
                                        sig=None, manifest=None, doc_count=None,
                                        scan_report=None, resolve_sig=None, failed_docs=None) -> dict:
    """run の完了確定と world 側の署名/manifest/doc_count/scan_report 確定を同一トランザクションで行う。
    `resolve_sig`＝このグラフが使った解決範囲の設定のハッシュ（`worlds.resolve_applied_sig` へ書く・None なら更新しない）。
    `failed_docs`（`{rel: 理由}`）を渡すと、失敗の一覧（`world_failed_docs`）も同じトランザクションで置き換える。
    `sig` が None なら world 側の UPDATE は行わない。scan_report 等の計算は呼び出し前に済ませておくこと（トランザクション内は軽量な UPDATE 2本のみ）。
    """
    _ensure()
    published = published_snapshot is not None
    with _connect() as c:
        rec = c.execute(
            "UPDATE ingest_runs SET status=%s, extraction_snapshot=%s, published_snapshot=%s, "
            "  source_doc_ids=%s, progress=NULL, "
            "  published_at=CASE WHEN %s THEN now() ELSE published_at END "
            "WHERE kb_id=%s AND id=%s "
            "RETURNING id, version, layer, status, source_doc_ids, extraction_snapshot, created_at, published_at",
            (status, Json(extraction_snapshot or {}),
             Json(published_snapshot) if published_snapshot is not None else None,
             Json(source_doc_ids or []), published, _KB_ID, run_id)).fetchone()
        if sig is not None:
            sets = ["last_sig=%s", "last_synced_at=now()"]
            params: list = [sig]
            if manifest is not None:
                sets.append("last_manifest=%s")
                params.append(Json(manifest))
            if doc_count is not None:
                sets.append("last_doc_count=%s")
                params.append(doc_count)
            if scan_report is not None:
                sets.append("last_scan_report=%s")
                sets.append("last_scan_report_at=now()")
                params.append(Json(scan_report))
            if resolve_sig is not None:
                sets.append("resolve_applied_sig=%s")
                params.append(resolve_sig)
            params += [_KB_ID, world]
            c.execute(f"UPDATE worlds SET {', '.join(sets)} WHERE kb_id=%s AND world_id=%s", params)
        if failed_docs is not None:
            replace_failed_docs_in(c, world, failed_docs)
        return rec


def finish_ingest_run_and_delete_world(run_id, world, *, status, extraction_snapshot=None) -> tuple:
    """run の完了確定と world レジストリ行の削除を同一トランザクションで行う（`worlds.delete` 専用）。
    派生物の wipe が成功した後だけ呼ぶ（失敗時は呼ばず world 行を残す）。戻り値は `(run の更新後の行, world 行が実際に削除されたか)`。
    """
    _ensure()
    with _connect() as c:
        rec = c.execute(
            "UPDATE ingest_runs SET status=%s, extraction_snapshot=%s, progress=NULL "
            "WHERE kb_id=%s AND id=%s "
            "RETURNING id, version, layer, status, source_doc_ids, extraction_snapshot, created_at, published_at",
            (status, Json(extraction_snapshot or {}), _KB_ID, run_id)).fetchone()
        n = c.execute("DELETE FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world)).rowcount
        return rec, n > 0


def update_ingest_run_progress(run_id, progress: dict) -> None:
    """実行中 run の逐次進捗を上書きする（`status='extracting'` の間だけ意味を持つ・best-effort で、失敗しても取り込み自体は失敗にしない）。"""
    _ensure()
    with _connect() as c:
        c.execute("UPDATE ingest_runs SET progress=%s WHERE kb_id=%s AND id=%s",
                  (Json(progress), _KB_ID, run_id))


def finish_ingest_run(run_id, *, status, extraction_snapshot=None, published_snapshot=None,
                      source_doc_ids=None) -> dict:
    """`start_ingest_run` が確保した行を完了状態へ更新する（UPDATE）。
    `published_snapshot` が None でないときだけ `published_at` を確定する。`progress` は完了時に NULL へ戻す。
    """
    _ensure()
    published = published_snapshot is not None
    with _connect() as c:
        return c.execute(
            "UPDATE ingest_runs SET status=%s, extraction_snapshot=%s, published_snapshot=%s, "
            "  source_doc_ids=%s, progress=NULL, "
            "  published_at=CASE WHEN %s THEN now() ELSE published_at END "
            "WHERE kb_id=%s AND id=%s "
            "RETURNING id, version, layer, status, source_doc_ids, extraction_snapshot, created_at, published_at",
            (status, Json(extraction_snapshot or {}),
             Json(published_snapshot) if published_snapshot is not None else None,
             Json(source_doc_ids or []), published, _KB_ID, run_id)).fetchone()


def downgrade_orphaned_extracting_runs(world=None) -> list:
    """居ないプロセスの孤児 `extracting` run を `failed`（中断）へ格下げし、格下げした run の id 一覧を返す。
    `world=None` は全 world 一括（起動時に呼ぶ）。`world` 指定はその world だけ（`ingest.worker._run_locked` が world_lock を持ったまま呼ぶ）。world 指定時は新しい行の INSERT（`start_ingest_run`）より前に呼ぶこと。
    """
    _ensure()
    q = ("UPDATE ingest_runs SET status='failed', progress=NULL, "
        "  extraction_snapshot=COALESCE(extraction_snapshot, '{}'::jsonb) "
        "    || '{\"interrupted\": true}'::jsonb "
        "WHERE kb_id=%s AND status='extracting'")
    params = [_KB_ID]
    if world is not None:
        q += " AND version=%s"
        params.append(world)
    q += " RETURNING id"
    with _connect() as c:
        return [r["id"] for r in c.execute(q, params).fetchall()]


def list_ingest_runs(world=None, limit=50, *, connect_timeout: float | None = None,
                     statement_timeout_ms: int | None = None) -> list:
    """取り込み run 一覧（新しい順）。world 指定で絞る。行キー `version` は DB 上の既存名。
    `connect_timeout`/`statement_timeout_ms`（省略可・None＝無期限）は接続確立後に `SET LOCAL statement_timeout` を残り時間で発行する。`_ensure()` の消費分も差し引き、残りが 0 以下なら接続せず `TimeoutError` を送出する。
    """
    budget_started = time.monotonic()
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            raise TimeoutError(f"list_ingest_runs({world!r}): budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    q = ("SELECT id, version, layer, status, source_doc_ids, extraction_snapshot, "
         "created_at, published_at FROM ingest_runs WHERE kb_id=%s")
    params = [_KB_ID]
    if world is not None:
        q += " AND version=%s"
        params.append(world)
    q += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL にして、返却後の接続へ statement_timeout を残さない。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(q, params).fetchall()


def get_latest_run_summary(world, *, connect_timeout: float | None = None,
                           statement_timeout_ms: int | None = None) -> dict | None:
    """最新 run（成否問わず）1件の軽量列（`id`/`status`/`extraction_snapshot`/`progress`/`created_at`）だけを引く（`source_doc_ids` を読まない）。
    `progress` は `status='extracting'` の間だけ値を持つ。
    `connect_timeout`/`statement_timeout_ms` は `list_ingest_runs` と同じ方式。
    """
    budget_started = time.monotonic()
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            raise TimeoutError(f"get_latest_run_summary({world!r}): budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL にして、返却後の接続へ statement_timeout を残さない。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(
            "SELECT id, status, extraction_snapshot, progress, created_at FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()


def get_latest_published_run_summary(world) -> dict | None:
    """最新の反映済み（`published_at IS NOT NULL`）run 1件の軽量列（`published_snapshot`/`extraction_snapshot`/`created_at`）。graph（Neo4j）件数専用で、ES は `get_latest_es_run_summary` を使う。
    直近の run が失敗していても Neo4j は直前の成功時点のままなので、これが現在の Neo4j の内容に最も近い。ES 段に触れていない run も拾いうるため ES 件数には流用しない。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT published_snapshot, extraction_snapshot, created_at FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND published_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()


def get_latest_es_run_summary(world) -> dict | None:
    """ES 反映を実際に試みた（`published_at IS NOT NULL` かつ `extraction_snapshot` に `es` キーがある）最新 run 1件の軽量列（`extraction_snapshot`/`created_at`）。
    `get_latest_published_run_summary`（Neo4j の完了境界）とは別の境界で、ES 段まで到達した run だけに絞る。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT extraction_snapshot, created_at FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND published_at IS NOT NULL AND extraction_snapshot ? 'es' "
            "ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()


def get_recent_es_attempts(world, limit: int = 200) -> list[dict]:
    """ES 段まで到達した直近の run（`extraction_snapshot` に `es` キーがある・反映済みかは問わない）の
    `es` 記録を新しい順に返す。**画面の全文検索の状態判定専用**——資料が変わらない経路で再索引に
    失敗した run は `published_at` を持たないが、直近の失敗としてここには含める
    （件数表示は `get_latest_es_run_summary` を使う）。"""
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT extraction_snapshot->'es' AS es FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND extraction_snapshot ? 'es' "
            "ORDER BY id DESC LIMIT %s", (_KB_ID, world, limit)).fetchall()
    return [r["es"] for r in rows if isinstance(r.get("es"), dict)]
