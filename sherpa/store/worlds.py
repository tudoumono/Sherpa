"""world レジストリ（world_id → 参照元 root_path）の読み書き。
設計: docs/design/scope.md「資料フォルダの登録・解決・付け替え（registry）」
"""
from __future__ import annotations

import math
import time

from psycopg.types.json import Json

from .db import _KB_ID, _connect, _ensure


def upsert_world(world_id, root_path, label=None, storage_mode="external_reference") -> dict:
    """world の参照バインドを登録/更新する。`label=None` の更新は既存 label を保持する（COALESCE）。
    同じ root を別 world に登録すると `worlds_root` UNIQUE 違反になる（呼び出し側が事前に 409 で防ぐ）。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO worlds (kb_id, world_id, root_path, label, storage_mode) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (kb_id, world_id) DO UPDATE SET root_path=EXCLUDED.root_path, "
            "  label=COALESCE(EXCLUDED.label, worlds.label), storage_mode=EXCLUDED.storage_mode, updated_at=now() "
            "RETURNING world_id, root_path, label, storage_mode, created_at, updated_at",
            (_KB_ID, world_id, root_path, label, storage_mode)).fetchone()


def rebind_bind_invalidate_sig(world_id, root_path, label=None,
                               storage_mode="external_reference") -> dict:
    """rebind の新 root へのバインド更新と、`last_sig`/`last_doc_count`/`last_scan_report(+at)` の無効化を同一 tx で確定する。
    `last_sig=''`（pre-invalidate と同じ番兵）・`last_doc_count`・`last_scan_report` を NULL にして、旧 root の件数・集計が新 root の値として見えないようにする。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO worlds (kb_id, world_id, root_path, label, storage_mode) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (kb_id, world_id) DO UPDATE SET root_path=EXCLUDED.root_path, "
            "  label=COALESCE(EXCLUDED.label, worlds.label), storage_mode=EXCLUDED.storage_mode, "
            "  last_sig='', last_doc_count=NULL, last_scan_report=NULL, last_scan_report_at=NULL, "
            "  updated_at=now() "
            "RETURNING world_id, root_path, label, storage_mode, created_at, updated_at",
            (_KB_ID, world_id, root_path, label, storage_mode)).fetchone()


def restore_bind_invalidate_sig(world_id, root_path, label=None,
                                storage_mode="external_reference") -> None:
    """rebind 失敗時のロールバック用: バインドを旧 root へ戻すことと last_sig の無効化を同一 tx で確定する（次回 sync が必ず再構築する）。"""
    _ensure()
    with _connect() as c:
        c.execute(
            "INSERT INTO worlds (kb_id, world_id, root_path, label, storage_mode) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (kb_id, world_id) DO UPDATE SET root_path=EXCLUDED.root_path, "
            "  label=COALESCE(EXCLUDED.label, worlds.label), storage_mode=EXCLUDED.storage_mode, "
            "  last_sig='', updated_at=now()",
            (_KB_ID, world_id, root_path, label, storage_mode))


def get_world(world_id, *, connect_timeout: float | None = None,
             statement_timeout_ms: int | None = None) -> dict | None:
    """world 登録行を1件引く。
    `connect_timeout`/`statement_timeout_ms`（省略可・None＝無期限）は残り時間ベースで渡す。接続確立に要した時間を差し引いた残りを、接続確立後に `SET LOCAL statement_timeout` で発行する（0 以下は最小 1ms へクランプ・`connect_timeout` は整数秒へ切り上げて最小 1 秒）。
    未初期化時の `_ensure()` の消費分も予算から差し引くため、計測は `_ensure()` の前から始める。残りが 0 以下なら接続せず `TimeoutError` を送出する。
    """
    budget_started = time.monotonic()
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            # `_ensure()` で予算を使い切った場合は接続を開始せず、呼び出し元（`worlds.resolve_external_world`）が拾える例外にする。
            raise TimeoutError(f"get_world({world_id!r}): budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL にして、返却後の接続へ statement_timeout を残さない。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(
            "SELECT world_id, root_path, label, storage_mode, last_sig, last_synced_at, "
            "last_manifest, last_doc_count, last_scan_report, last_scan_report_at, resolve_settings, resolve_applied_sig, "
            "created_at, updated_at "
            "FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world_id)).fetchone()


def set_resolve_settings(world_id, settings) -> bool:
    """解決範囲の設定（正規化済みの dict。空は NULL）を保存する。資料フォルダが無ければ False。署名は `worker._sig` が設定のハッシュを材料にするため、次回の更新で全件の取り込みになる。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "UPDATE worlds SET resolve_settings=%s, updated_at=now() WHERE kb_id=%s AND world_id=%s RETURNING 1",
            (Json(settings) if settings else None, _KB_ID, world_id)).fetchone()
    return row is not None


def get_world_status_row(world_id, *, connect_timeout: float | None = None,
                         statement_timeout_ms: int | None = None) -> dict | None:
    """`GET /worlds/{wid}/status` 専用の狭い SELECT（`last_manifest` を含めない＝読取量が world の大きさに比例しない）。
    `last_sig` はグラフ表示のキャッシュ判定にも使う。`connect_timeout`/`statement_timeout_ms` は `get_world()` と同じ方式で、有限の timeout を渡せる。
    """
    budget_started = time.monotonic()
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            raise TimeoutError(f"get_world_status_row({world_id!r}): budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL にして、返却後の接続へ statement_timeout を残さない。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(
            "SELECT world_id, root_path, label, last_sig, last_synced_at, last_scan_report, "
            "last_scan_report_at, resolve_settings, resolve_applied_sig "
            "FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world_id)).fetchone()


def world_by_root(root_path) -> dict | None:
    """その root_path にバインド済みの world（1:1 検証用・無ければ None）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT world_id, root_path, label, storage_mode FROM worlds WHERE kb_id=%s AND root_path=%s",
            (_KB_ID, root_path)).fetchone()


def set_world_sig(world_id, sig, manifest=None, doc_count=None, scan_report=None) -> None:
    """world の内容署名・ファイル明細・最終同期時刻を記録する（変更検知/差分の基準）。
    用途は 4 つ: ① pre-invalidate（`sig=''`・取り込み/削除の開始時の番兵）、② wipe（削除時の無効化）、③ 確定（取り込み成功後の署名＋明細）、④ manifest バックフィル（`sync` の unchanged パス）。
    `manifest`＝rel→[mtime_ns,ctime_ns,size] の dict（None なら明細は更新しない）。`doc_count`（`/ext/v1/capabilities` 用）と `scan_report`（`GET /worlds/{wid}/status` が読むキャッシュ）は③でのみ渡し、sig 確定と同一 UPDATE で書く。`doc_count` は走査が失敗・世代混在のとき None（前回値を保持）。いずれも None なら該当列は更新しない。
    world_lock を保持したまま呼ぶこと（ロック外の書き込みは、他プロセスの pre-invalidate や削除時の無効化を古い署名で復活させる）。
    """
    _ensure()
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
    params += [_KB_ID, world_id]
    with _connect() as c:
        c.execute(f"UPDATE worlds SET {', '.join(sets)} WHERE kb_id=%s AND world_id=%s", params)


def set_scan_report(world_id, report) -> None:
    """`corpus_docs.scan_report()` の結果を単独でキャッシュする（`set_world_sig` の sig 確定と同時に書けない箇所専用）。
    書き手は `ingest.worker.sync()` の unchanged 経路のバックフィル。`POST /worlds/{wid}/recount` は `set_scan_report_if_unchanged` を使う。`last_sig`/`last_doc_count` とは独立の列で、`ingest_runs` には紐付けない。
    """
    _ensure()
    with _connect() as c:
        c.execute(
            "UPDATE worlds SET last_scan_report=%s, last_scan_report_at=now() "
            "WHERE kb_id=%s AND world_id=%s",
            (Json(report), _KB_ID, world_id))


def set_scan_report_if_unchanged(world_id, report, *, expected_root_path, expected_sig,
                                 expected_created_at, expected_updated_at,
                                 expected_last_synced_at, expected_last_scan_report_at) -> bool:
    """`POST /worlds/{wid}/recount` 専用: 走査は world_lock の外で行うため、書き戻し時に binding（`root_path`）と世代マーカーが読み取り時点から変わっていない場合だけ UPDATE する。
    `last_sig` に加えて `updated_at`・`last_synced_at`・`last_scan_report_at`・`created_at` も比較する（ABA を見逃さないため）。比較は全列 `IS NOT DISTINCT FROM`（NULL がありうる）。
    戻り値: 更新したら True、不一致なら False（呼び出し元は 409 で終了する）。
    """
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE worlds SET last_scan_report=%s, last_scan_report_at=now() "
            "WHERE kb_id=%s AND world_id=%s AND root_path=%s AND last_sig IS NOT DISTINCT FROM %s "
            "AND created_at IS NOT DISTINCT FROM %s AND updated_at IS NOT DISTINCT FROM %s "
            "AND last_synced_at IS NOT DISTINCT FROM %s AND last_scan_report_at IS NOT DISTINCT FROM %s",
            (Json(report), _KB_ID, world_id, expected_root_path, expected_sig,
             expected_created_at, expected_updated_at,
             expected_last_synced_at, expected_last_scan_report_at)).rowcount
    return n > 0


def backfill_doc_count(world_id, doc_count, expected_sig) -> bool:
    """`last_doc_count` が NULL のままの既存 world へ、保存済み manifest から算出した件数だけを補完する。
    `last_synced_at` は変更しない。`expected_sig`（呼び出し元が world_lock 内で再確認した sig）が現在の `last_sig` と一致する場合のみ更新する。
    戻り値: 更新したら True（不一致なら False＝no-op）。
    """
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE worlds SET last_doc_count=%s WHERE kb_id=%s AND world_id=%s AND last_sig=%s",
            (doc_count, _KB_ID, world_id, expected_sig)).rowcount
    return n > 0


def backfill_manifest_and_doc_count(world_id, manifest, doc_count, expected_sig) -> bool:
    """`last_manifest` と `last_doc_count` が両方 NULL のままの既存 world へ、再スキャンした manifest とその件数を 1 回の UPDATE で補完する（`last_synced_at` は触れない）。
    `expected_sig` が現在の `last_sig` と一致する場合のみ更新する（呼び出し元は world_lock 内で再確認した sig を渡す）。
    戻り値: 更新したら True（不一致なら False＝no-op）。
    """
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE worlds SET last_manifest=%s, last_doc_count=%s "
            "WHERE kb_id=%s AND world_id=%s AND last_sig=%s",
            (Json(manifest), doc_count, _KB_ID, world_id, expected_sig)).rowcount
    return n > 0


def list_worlds_db() -> list:
    """登録 world の一括取得（`last_sig`/`last_synced_at`/`last_doc_count` も含む）。`{world_id: row}` を組み立てて、world ごとの `get_world()` を避けるために使う。外部への公開フィールドは `world_admin_service.public_world()` が絞る。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT world_id, root_path, label, storage_mode, last_sig, last_synced_at, "
            "  last_doc_count, created_at, updated_at "
            "FROM worlds WHERE kb_id=%s ORDER BY world_id", (_KB_ID,)).fetchall()


def delete_world_row(world_id) -> bool:
    """world レジストリ行を削除する（派生物の wipe は呼び出し側＝worlds.delete が先に行う）。"""
    _ensure()
    with _connect() as c:
        n = c.execute("DELETE FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world_id)).rowcount
    return n > 0
