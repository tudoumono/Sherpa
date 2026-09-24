"""取り込み run（P1・DATA-MODEL ingest_runs）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S4）。`list_ingest_runs` は以後
`connect_timeout`/`statement_timeout_ms`（`corpus_docs.last_run_flags` の打切り契約）を追加済み
（`store.worlds.get_world` 等と同じテンプレート）——それ以外の crud ロジックは移動時のまま。
"""
from __future__ import annotations

import math
import time

from psycopg.types.json import Json

from .db import _KB_ID, _connect, _ensure


def add_ingest_run(world, layer="version", status="auto_published", source_doc_ids=None,
                   extraction_snapshot=None, published_snapshot=None, ingest_source_id=None,
                   scan_root=None, scope_mapping_overrides=None, created_by="admin") -> dict:
    """完了した取り込み run を1行記録（同期ワーカー＝最終状態を記録）。**実際に Neo4j 反映した時のみ** published_at。

    列名 `version`・layer 値 `'version'` は歴史的（DB 不変・語彙統一のスコープ外）。引数は world 用語。
    """
    _ensure()
    published = published_snapshot is not None      # 反映実体がある時だけ（reflect=False/failed は NULL・RV Low）
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
    """取り込み run を開始時点で1行 INSERT する（`status='extracting'`・ING-3 中断リカバリー）。

    従来は完了時に `add_ingest_run` で1回だけ INSERT していたため、プロセスが強制終了（OOM/kill）
    すると実行の痕跡が一切残らなかった（画面には前回成功の内容が残り続け、今回の実行が
    「起きたことにならない」）。開始時にこの関数で行を確保し、完了時は `finish_ingest_run`
    （同じ行の UPDATE）で締める2段構成にすることで、行の存在自体が「実行が始まった」事実になる
    （起動時 lifespan の孤児 `extracting` 検知・格下げの前提）。

    `progress`（ING-3）＝INSERT と同時に確定する初期進捗（例:
    `{"stage": "accepted", "stage_label": "受け付けました", ...}`）。呼び出し元（router の
    受付処理）が O(1) でこの行を作った瞬間から `GET /worlds/{wid}/status` が実行中である
    ことを表示できるようにする——省略時（従来呼び出し）は `progress` 列は NULL のまま。

    戻り値は `id` を含む（呼び出し元はこれを run_id として背景実行の受付応答・進捗更新に使う）。
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
    """`status='extracting'` のままの行だけを `failed` へ CAS で落とす（ING-3）。

    背景実行の最外周（`sherpa.ingest.background`）のセーフティネット専用: 個々の操作
    （register/refresh/extract/delete/rebind/rerun/concepts confirm・disable）は自分の run を
    自分で `finish_ingest_run`／`finish_ingest_run_and_confirm_world` により理由付きで
    terminal 化する契約だが、想定していない例外（バグ・想定外の早期 return 等）でその契約が
    果たされなかった時だけここが拾う。`WHERE status='extracting'` の条件付き UPDATE（CAS）に
    することで、操作自身が既に詳細な理由付きで terminal 化済みの行を汎用の理由で
    上書きしない（二重確定・詳細の消失を防ぐ）。

    戻り値: 実際に更新したか（True＝このセーフティネットが発火した＝呼び出し元の操作が
    自分で terminal 化しなかった証拠・監視/テストで使う）。
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
                                        scan_report=None) -> dict:
    """run の完了確定と world 側の署名/manifest/doc_count/scan_report 確定を**同一トランザクション**
    で行う（ING-3）。

    旧実装は `finish_ingest_run`（run 完了）→（呼び出し元のフレームで）`set_world_sig`（world 確定）
    の2つの独立した UPDATE 文だったため、run が「成功」を確定した直後・world 側の確定前にプロセスが
    落ちると、「run は成功だが world の last_sig/manifest/scan_report は pre-invalidate のまま」という
    中間状態が観測されうる窓があった（次回 sync の自己修復で収束はするが、その間の `status` 表示が
    不整合）。ここでは両方の UPDATE を同じ `with _connect()` 区間（同一トランザクション・同一コミット）
    に収め、run 完了と world 確定が原子的に成立する。

    `sig` が None なら world 側の UPDATE は行わない（reflect=False・失敗パス等・単体の
    `finish_ingest_run` と同じ意味）。scan_report 等の**計算**は呼び出し元がこの関数を呼ぶ**前**に
    済ませておくこと（この関数自体は軽量な UPDATE 2本のみで、重い処理をトランザクション内に残さない）。
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
            params += [_KB_ID, world]
            c.execute(f"UPDATE worlds SET {', '.join(sets)} WHERE kb_id=%s AND world_id=%s", params)
        return rec


def finish_ingest_run_and_delete_world(run_id, world, *, status, extraction_snapshot=None) -> tuple:
    """run の完了確定と world レジストリ行の削除を**同一トランザクション**で行う
    （ING-3・`worlds.delete` 専用）。

    `worlds.delete`（`_wipe_locked` による派生物 wipe が成功した後）だけが呼ぶ——グラフ削除等の
    失敗時はこの関数を呼ばない（fail-closed・world 行を残す契約は不変）。「world 行は消えたのに
    run はまだ `status='extracting'` のまま」という中間状態を作らないための組。

    戻り値: `(run の更新後の行, world 行が実際に削除されたか)`。
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
    """実行中 run の逐次進捗を上書きする（`status='extracting'` の間だけ意味を持つ・best-effort＝
    呼び出し元〔`ingest.worker`〕は書込失敗を取り込み自体の失敗にしない）。"""
    _ensure()
    with _connect() as c:
        c.execute("UPDATE ingest_runs SET progress=%s WHERE kb_id=%s AND id=%s",
                  (Json(progress), _KB_ID, run_id))


def finish_ingest_run(run_id, *, status, extraction_snapshot=None, published_snapshot=None,
                      source_doc_ids=None) -> dict:
    """`start_ingest_run` が確保した行を完了状態へ更新する（INSERT ではなく UPDATE・ING-3）。

    `add_ingest_run` と同じ「実際に Neo4j 反映した時のみ `published_at`」契約（`published_snapshot`
    が None でなければ確定）。`progress` は完了時に NULL へ戻す——`status` 自体で実行中かどうかを
    判別できるため、古い進捗値を完了後の行に残さない（`GET /worlds/{wid}/status` が誤って
    「実行中」の進捗を出し続けることを防ぐ）。
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
    """居ないプロセスの孤児 `extracting` run を `failed`（中断）へ格下げする（ING-3 中断リカバリー）。

    `world=None`（既定）＝全 world 一括——起動時（lifespan・まだどの world も実行中でない時点）に
    呼ばれる契約のため、その時点で見つかる `extracting` 行は例外なく孤児である
    （単一 worker 前提・稼働中に他プロセスが同じ行へ触れることは無い）。`world` 指定＝その world
    だけへ絞る——`ingest.worker._run_locked` が新しい run を開始する直前（同じ world の
    `world_lock` を保持中）に呼び、起動直後の一瞬（lifespan 完了前にリクエストが割り込む等）に
    取りこぼした孤児をその world の次回実行開始時に拾う。world 指定時は「今から作る新しい行」を
    誤って拾わないよう、呼び出し元は必ず新しい行の INSERT（`start_ingest_run`）より**前**に呼ぶこと
    （でなければ作ったばかりの行を自分で格下げしてしまう）。

    戻り値: 格下げした run の id 一覧（ログ/起動メッセージ用）。
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
    """取り込み run 一覧（新しい順）。world 指定で絞る。列名/行キー `version` は DB 不変。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限＝既存呼び出し元は
    無変更）: `corpus_docs.last_run_flags`（`agentic_search.run_tool` の list_docs ツール打切り
    契約）が残り時間ベースで渡す（`store.worlds.get_world`/`store.settings.get_system_settings`
    と同じ理由・同じ方式——接続確立**後**に `SET LOCAL statement_timeout` を発行し、接続に要した時間
    ぶんを差し引く）。未初期化時の `_ensure()` 消費分も同じ予算から差し引く。差し引いた残りが
    0以下なら、最低1秒へクランプして新規接続を試みず `TimeoutError` を送出する。
    """
    budget_started = time.monotonic()   # `_ensure()` の消費分も差し引くため、その呼び出し前から計測する
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
            # SET LOCAL（session-level ではなく）: プール導入後（性能台帳#17 QW2）、この
            # with ブロック＝単一トランザクションの間だけ有効にし、返却後の接続に
            # statement_timeout が残らないようにする（GUC 汚染防止・commit/rollback で自動消滅）。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(q, params).fetchall()


def get_latest_run_summary(world, *, connect_timeout: float | None = None,
                           statement_timeout_ms: int | None = None) -> dict | None:
    """最新 run（成否問わず）1件の軽量列（`id`/`status`/`extraction_snapshot`/`progress`/
    `created_at`）だけを引く。

    `list_ingest_runs(world, limit=1)` は `source_doc_ids`（world の全文書名を持つ JSONB 配列・
    大規模 world では際限なく大きい）まで毎回読む——`GET /worlds/{wid}/status` の要約表示は
    `source_doc_ids` を一切使わないため、この列を持たない狭い SELECT を別に用意する
    （status の定数時間契約）。`last_run_status`/`last_run_warnings`/
    `failed_files`/`stage_summary` はここから読む（run の成否を問わない＝最新の試行を正直に示す）。
    `id`/`progress`（ING-3）＝`status='extracting'` の間だけ意味を持つ実行中進捗——status が
    それ以外なら `progress` は常に NULL（`finish_ingest_run` が完了時にクリアする契約）。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限＝既存呼び出し元は
    無変更）は `list_ingest_runs`/`get_world_status_row` と**同じ方式**（`_ensure()` の消費分も
    予算から差し引く・接続確立**後**に残り時間で `SET LOCAL statement_timeout` を発行）。RV2是正#a3:
    `corpus_docs.last_run_flags` が `list_ingest_runs`（`source_doc_ids` 込みの重い SELECT）の
    代わりにこの狭い SELECT を使うために追加した——`world_lock_shared` 保持中に呼ばれるため、
    O(N)（文書総数比例）の転送・deserialize を共有ロック区間へ持ち込まない。
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
            # SET LOCAL（session-level ではなく）: プール導入後（性能台帳#17 QW2）、この
            # with ブロック＝単一トランザクションの間だけ有効にし、返却後の接続に
            # statement_timeout が残らないようにする（GUC 汚染防止・commit/rollback で自動消滅）。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(
            "SELECT id, status, extraction_snapshot, progress, created_at FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()


def get_latest_published_run_summary(world) -> dict | None:
    """最新の**反映済み**（`published_at IS NOT NULL`）run 1件の軽量列（`published_snapshot`/
    `extraction_snapshot`/`created_at`）。**graph（Neo4j）件数専用**——ES は
    `get_latest_es_run_summary` を使う（同じ「反映済み」でも完了境界が異なる・下記参照）。

    graph 件数はこの run の値を使う——直近の run が失敗していても、Neo4j は直前の成功時点の
    まま変わっていない（`_run_locked` は Neo4j load を tx で失敗時ロールバックする契約）ため、
    これが「今実際に Neo4j にある内容」の最良近似になる（`GET /worlds/{wid}/status` はこれ以上
    Neo4j へ問い合わせない）。`source_doc_ids` 等の重い列は含めない。

    台帳（PG）replace 失敗の run にも「Neo4j へは実際に反映済み」の `published_snapshot` を
    記録する（`ingest.worker._run_locked` 参照）——この run は `published_at` を持つが、
    ES インデックス化は台帳 replace 成功**後**の処理のため `extraction_snapshot` に `es` キーを
    持たない。ES の件数取得にこの関数を流用すると、この run が「最新反映」を名乗ってしまい、
    実際に ES へ触れた直近の run（より古いが有効な `es.chunks`）を隠してしまう。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT published_snapshot, extraction_snapshot, created_at FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND published_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()


def get_latest_es_run_summary(world) -> dict | None:
    """最新の**ES 反映を実際に試みた**（`published_at IS NOT NULL` かつ `extraction_snapshot` に
    `es` キーがある）run 1件の軽量列（`extraction_snapshot`/`created_at`）。

    `get_latest_published_run_summary`（Neo4j 反映の完了境界）とは別の完了境界を追跡する——
    ES インデックス化は台帳 replace 成功後にのみ実行されるため、台帳 replace で止まった run
    （Neo4j へは反映済みなので `published_snapshot`/`published_at` は立つ）は ES に一切触れて
    いない。`extraction_snapshot ? 'es'`（JSONB キー存在演算子）で、実際に ES 段まで到達した
    run だけへ絞り込む。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT extraction_snapshot, created_at FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND published_at IS NOT NULL AND extraction_snapshot ? 'es' "
            "ORDER BY id DESC LIMIT 1",
            (_KB_ID, world)).fetchone()
