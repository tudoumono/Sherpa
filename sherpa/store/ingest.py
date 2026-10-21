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


def downgrade_orphaned_extracting_runs(world=None, *, legacy_only=False, claimed_by=None) -> list:
    """居ないプロセスの孤児 `extracting` run を `failed`（中断）へ格下げし、格下げした run の id 一覧を返す。
    `world=None` は全 world 一括。`world` 指定はその world だけ。
    `legacy_only=True` はワーカーに拾われていない行（`claimed_by IS NULL`＝0.18.x の Web アプリがプロセス内で動かしていたもの）だけ、
    `claimed_by` 指定はそのワーカーが拾った行だけを対象にする。どちらも無ければ全ての `extracting`。
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
    if legacy_only:
        q += " AND claimed_by IS NULL"
    if claimed_by is not None:
        q += " AND claimed_by=%s"
        params.append(claimed_by)
    q += " RETURNING id"
    with _connect() as c:
        return [r["id"] for r in c.execute(q, params).fetchall()]


def note_ingest_run_op(run_id, op: str) -> None:
    """実行中の run の操作名（register/refresh/rerun 等）を `extraction_snapshot.op` へ足す。強制終了で `interrupted` へ格下げされた後も残り、起動時の自動再開の可否判定に使う。"""
    _ensure()
    with _connect() as c:
        c.execute("UPDATE ingest_runs SET extraction_snapshot=COALESCE(extraction_snapshot, '{}'::jsonb) "
                  "  || jsonb_build_object('op', %s::text) "
                  "WHERE kb_id=%s AND id=%s AND status='extracting'", (op, _KB_ID, run_id))


def list_worlds_with_interrupted_latest_run() -> list:
    """最新の run が「中断（`extraction_snapshot.interrupted`）」で終わっている登録済み world を `{world_id, run_id, op, stop_reason}` で返す。
    最新の run だけを見る（その後に別の run が走っていれば再開済み）。world 行が無い（削除・登録解除済み）ものは含まない。`op` は記録が無ければ None。
    """
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT r.version AS world_id, r.id AS run_id, r.extraction_snapshot->>'op' AS op, "
            "r.extraction_snapshot->>'stop_reason' AS stop_reason "
            "FROM (SELECT DISTINCT ON (version) id, version, status, extraction_snapshot "
            "      FROM ingest_runs WHERE kb_id=%s AND version IS NOT NULL ORDER BY version, id DESC) r "
            "JOIN worlds w ON w.kb_id=%s AND w.world_id=r.version "
            "WHERE r.status='failed' AND r.extraction_snapshot->>'interrupted'='true' "
            "ORDER BY r.version", (_KB_ID, _KB_ID)).fetchall()
    return list(rows)


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


_FLAG_REASONS_ALWAYS_SHOWN = ("plugin_failed", "rebind_failed_rolled_back", "rebind_rollback_failed")


def get_latest_run_status(world, flags_limit: int) -> dict | None:
    """最新 run（成否問わず）1件を、状態画面に要る値だけに絞って引く。`extraction_snapshot.flags`（文書ごとの注意で数万件になりうる）は Python へ持ち込まず、
    DB 側で数える・先頭だけ取る。返す値: `id`/`status`/`progress`、`extraction_snapshot`（`flags` を除いた残り）、`flags_total`、`flags_head`（先頭 `flags_limit` 件）、
    `flag_counts`（`always_shown`＝先頭の外でも画面に出す reason、`unparsed_syntax`/`unparsed_oversize`/`mention_unreadable`/`md_failed_many`＝全件からの集計）。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "WITH r AS (SELECT id, status, progress, extraction_snapshot AS es FROM ingest_runs "
            "           WHERE kb_id=%s AND version=%s ORDER BY id DESC LIMIT 1), "
            "fl AS (SELECT f, o FROM r, jsonb_array_elements(CASE WHEN jsonb_typeof(r.es->'flags')='array' "
            "                 THEN r.es->'flags' ELSE '[]'::jsonb END) WITH ORDINALITY AS t(f, o)) "
            "SELECT r.id, r.status, r.progress, "
            "  CASE WHEN jsonb_typeof(r.es)='object' THEN r.es - 'flags' END AS extraction_snapshot, "
            "  (SELECT count(*) FROM fl) AS flags_total, "
            "  (SELECT COALESCE(jsonb_agg(f ORDER BY o), '[]'::jsonb) FROM fl WHERE o <= %s) AS flags_head, "
            "  (SELECT COALESCE(array_agg(DISTINCT f->>'reason'), '{}') FROM fl "
            "    WHERE f->>'reason' = ANY(%s)) AS always_shown, "
            "  (SELECT count(DISTINCT COALESCE(f->'from', 'null'::jsonb)) FROM fl "
            "    WHERE f->>'reason' = 'source_not_registered') AS unparsed_syntax, "
            "  (SELECT count(DISTINCT COALESCE(f->'from', 'null'::jsonb)) FROM fl "
            "    WHERE f->>'reason' = 'dropped_syntax' AND f->>'why' = 'size_exceeded') AS unparsed_oversize, "
            "  (SELECT count(*) FROM fl WHERE f->>'reason' = 'unreadable_mention_doc') AS mention_unreadable, "
            "  (SELECT COALESCE(sum(CASE WHEN jsonb_typeof(f->'count')='number' THEN (f->>'count')::numeric "
            "                            ELSE 0 END), 0) FROM fl "
            "    WHERE f->'many' = 'true'::jsonb AND starts_with(f->>'reason', 'office_md:derived_partial')) "
            "    AS md_failed_many "
            "FROM r", (_KB_ID, world, flags_limit, list(_FLAG_REASONS_ALWAYS_SHOWN))).fetchone()
    if row is None:
        return None
    md_many = row["md_failed_many"]
    return {"id": row["id"], "status": row["status"], "progress": row["progress"],
            "extraction_snapshot": row["extraction_snapshot"],
            "flags_total": row["flags_total"], "flags_head": row["flags_head"],
            "flag_counts": {"always_shown": list(row["always_shown"]),
                            "unparsed_syntax": row["unparsed_syntax"],
                            "unparsed_oversize": row["unparsed_oversize"],
                            "mention_unreadable": row["mention_unreadable"],
                            "md_failed_many": int(md_many) if md_many == int(md_many) else float(md_many)}}


def get_latest_published_run_summary(world) -> dict | None:
    """最新の反映済み（`published_at IS NOT NULL`）run 1件の `published_snapshot` の `nodes`/`edges` と `created_at`。graph（Neo4j）件数専用で、ES は `get_latest_es_run_summary` を使う。
    直近の run が失敗していても Neo4j は直前の成功時点のままなので、これが現在の Neo4j の内容に最も近い。ES 段に触れていない run も拾いうるため ES 件数には流用しない。
    JSONB は丸ごと取らず必要なキーだけ DB 側で取り出す。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT published_snapshot->'nodes' AS nodes, published_snapshot->'edges' AS edges, created_at "
            "FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND published_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()
    if row is None:
        return None
    return {"published_snapshot": {k: row[k] for k in ("nodes", "edges") if row[k] is not None},
            "created_at": row["created_at"]}


def get_latest_es_run_summary(world) -> dict | None:
    """ES 反映を実際に試みた（`published_at IS NOT NULL` かつ `extraction_snapshot` に `es` キーがある）最新 run 1件の `extraction_snapshot.es` と `created_at`（`extraction_snapshot` は `es` だけを持つ）。
    `get_latest_published_run_summary`（Neo4j の完了境界）とは別の境界で、ES 段まで到達した run だけに絞る。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT extraction_snapshot->'es' AS es, created_at FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND published_at IS NOT NULL AND extraction_snapshot ? 'es' "
            "ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()
    if row is None:
        return None
    return {"extraction_snapshot": {"es": row["es"]}, "created_at": row["created_at"]}


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


# ---- 取り込みの依頼（queued）の受付・取得 -------------------------------------------------------------

_ENQUEUE_LOCK_KEY = 0x5348_4552_5041_0001   # 依頼の受付（合流／衝突の判定→INSERT）を直列化する advisory lock の鍵
ACTIVE_STATUSES = ("queued", "extracting")


def enqueue_ingest_run(world, op, payload, fingerprint, app_version, *, keys=(), created_by="admin",
                       progress=None) -> tuple[str, dict]:
    """取り込みの依頼を `status='queued'` の 1 行として積む（受付は O(1)・処理はワーカーが拾ってから始まる）。
    `keys`＝同時に 1 つしか動かさない単位（資料フォルダ ID と、必要なら新規登録の枠など）。同じ単位に queued/extracting の行が
    あるとき、`op`/`fingerprint` が一致すれば積まずに `("joined", 既存行)`、違えば `("conflict", 既存行)`。無ければ `("created", 新しい行)`。
    判定と INSERT は同じトランザクションの advisory lock で直列化する（同時の受付が 2 本とも通らない）。
    """
    _ensure()
    keys = list(dict.fromkeys([world, *keys]))
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENQUEUE_LOCK_KEY,))
        existing = c.execute(
            "SELECT id, version, status, COALESCE(op, extraction_snapshot->>'op') AS op, fingerprint "
            "FROM ingest_runs WHERE kb_id=%s AND status IN ('queued','extracting') "
            "  AND (version = ANY(%s) OR arb_keys && %s) ORDER BY id DESC LIMIT 1",
            (_KB_ID, keys, keys)).fetchone()
        if existing is not None:
            same = existing["op"] == op and existing["fingerprint"] == fingerprint
            return ("joined" if same else "conflict"), existing
        row = c.execute(
            "INSERT INTO ingest_runs (kb_id, version, layer, status, progress, created_by, op, payload, "
            "  fingerprint, app_version, arb_keys) "
            "VALUES (%s,%s,'version','queued',%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id, version, status, op, fingerprint",
            (_KB_ID, world, Json(progress) if progress is not None else None, created_by, op,
             Json(payload or {}), fingerprint, app_version, keys)).fetchone()
        return "created", row


def claim_queued_run(worker_id, app_version) -> dict | None:
    """自分と同じ版の依頼を古い順に 1 件 `extracting` へ進めて返す（`FOR UPDATE SKIP LOCKED`・無ければ None）。
    操作名は `extraction_snapshot.op` にも残す（中断した run を起動時に再開してよいかの判定が読む）。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "UPDATE ingest_runs SET status='extracting', claimed_by=%s, "
            "  extraction_snapshot=COALESCE(extraction_snapshot, '{}'::jsonb) || jsonb_build_object('op', op) "
            "WHERE id = (SELECT id FROM ingest_runs WHERE kb_id=%s AND status='queued' "
            "              AND app_version IS NOT DISTINCT FROM %s "
            "            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id, version, op, payload, created_by",
            (worker_id, _KB_ID, app_version)).fetchone()


def restamp_queued_runs(app_version) -> list:
    """拾われていない依頼（queued）の版を `app_version` に付け替え、付け替えた run の id 一覧を返す。Web アプリの起動時に呼ぶ（前の版の Web アプリが積んだ依頼を、今の版のワーカーが拾えるようにする）。"""
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "UPDATE ingest_runs SET app_version=%s WHERE kb_id=%s AND status='queued' "
            "  AND app_version IS DISTINCT FROM %s RETURNING id", (app_version, _KB_ID, app_version)).fetchall()
    return [r["id"] for r in rows]


def count_queued_runs(app_version) -> tuple[int, int]:
    """待ちの依頼の数 `(自分と同じ版, 違う版)`。違う版だけが残っているワーカーは `waiting_version` で待つ。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT count(*) FILTER (WHERE app_version IS NOT DISTINCT FROM %s) AS same, "
            "       count(*) FILTER (WHERE app_version IS DISTINCT FROM %s) AS other "
            "FROM ingest_runs WHERE kb_id=%s AND status='queued'",
            (app_version, app_version, _KB_ID)).fetchone()
    return int(row["same"]), int(row["other"])


def list_claimants_of_extracting_runs() -> list[str]:
    """`extracting` の行を拾っているワーカーの ID 一覧（生死の判定は呼び出し側）。"""
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT DISTINCT claimed_by FROM ingest_runs "
            "WHERE kb_id=%s AND status='extracting' AND claimed_by IS NOT NULL", (_KB_ID,)).fetchall()
    return [r["claimed_by"] for r in rows]
