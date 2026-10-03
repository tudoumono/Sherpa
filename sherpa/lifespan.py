"""FastAPI lifespan（起動処理の呼び出し順の集約）。

起動時の順序（各処理の実体は `sherpa.api` 側）:
① ログ設定（`log_setup.configure_logging()`・request_id filter の再適用） ② スキーマ初期化 ③ 取り込み中断 run の格下げ
④ 監査 writer・Codex ジョブワーカーの起動 ⑤ env の初回シード ⑥ 起動検査（`_warn_*`・`CHANGE_ME` と既定 admin パスワードの検査は
auth bootstrap より前に置く） ⑦ auth bootstrap・孤児 reconcile・TTL sweep（起動時 1 回＋定期）・`turn_metrics` 補完。
shutdown では新規受付を止め、writer・ワーカー・PG プール・Neo4j driver をクローズする。
循環 import を避けるため `sherpa.api` の参照は lifespan 実行時の遅延 import にする。
設計: docs/design/operations.md「起動・停止・状態」
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from sherpa import codex_jobs_worker, ext_api, log_setup, store

_log = logging.getLogger("sherpa")


@asynccontextmanager
async def lifespan(app):
    _metafile_render_stop = threading.Event()
    _maintenance_threads: list = []  # 起動時掃除・定期掃除のスレッド（終了時に PG プールを閉じる前に join する）
    # `ext_api._attach_request_id_filter()` が作成済み logger の handlers を毎回スキャンするため、必ずそれより前に呼ぶ（失敗は起動を止めない）
    try:
        log_setup.configure_logging()
    except Exception as e:
        _log.warning("サブシステム別ログの設定に失敗しました（起動は続行します）: %s", e)
    # ログ設定が完了しているはずの起動処理の先頭で request_id filter を再度付け直す（冪等・失敗は起動を止めない）
    try:
        ext_api._attach_request_id_filter()
    except Exception as e:
        _log.warning("request_id ログ filter の再適用に失敗しました（起動は続行します）: %s", e)
    # schema 初期化（advisory lock で直列化）。DB 不達でも起動は止めない
    try:
        store.init_schema()
    except Exception as e:
        _log.warning("起動時のスキーマ初期化に失敗しました（DB 不達の可能性・lazy 初期化にフォールバックします）: %s", e)
    # 起動直後に残る `status='extracting'` の run はプロセス強制死の孤児（単一 worker 前提）。failed へ一括格下げする
    try:
        downgraded = store.downgrade_orphaned_extracting_runs()
        if downgraded:
            _log.warning("起動時に中断された取り込み run を検知し failed へ格下げしました: ids=%s", downgraded)
    except Exception as e:
        _log.warning("起動時の孤児 run 格下げに失敗しました（DB 不達の可能性）: %s", e)
    # /ext/v1 監査書込み専用 writer スレッド。起動に失敗しても起動処理は止めず ERROR ログを残す（writer は自己回復する）
    if not ext_api._audit_writer.start():
        _log.error("ext_api audit writer の起動に失敗しました（旧世代のスレッドがまだ停止して"
                  "いない可能性）。旧世代の終了後、次回の submit()/start() で自己回復します。")
    # Codex ジョブの背景ディスパッチャ（前回 `running` のジョブは failed(interrupted) へ回収してから開始）。失敗しても起動は止めない
    try:
        codex_jobs_worker.start()
    except Exception as e:
        _log.warning("codex jobs ワーカーの起動に失敗しました（起動は続行します）: %s", e)
    try:
        # yield 復帰後（shutdown 中）に例外が起きても writer の stop() は必ず実行する（try/finally）
        from sherpa import api, model_catalog
        api._seed_settings_from_env()
        api._seed_ollama_url_from_env()  # OLLAMA_URL は独立マーカーで一度だけシード
        api._warn_central_ollama_not_allowed()
        api._seed_openai_endpoint_from_env()
        api._seed_depth_profile_from_env()
        api._seed_screen_settings_from_env()
        api._seed_user_agent_from_env()
        api._seed_vlm_ollama_url_from_env()  # _seed_ollama_url_from_env の後
        model_catalog.seed_catalog_once()
        api._purge_personal_keys_if_disabled_on_startup()
        api._warn_change_me_placeholders()
        api._warn_default_admin_password()
        api._warn_auth_disabled_in_production()
        api._auth_bootstrap_on_startup()
        api._warn_fixtures()
        api._warn_test_db_isolated()
        api._warn_codex_sandbox_disabled()
        api._warn_multi_worker_chat_turns()
        api._warn_browse_roots_missing()
        api._reconcile_orphans()
        _maintenance_threads.append(api._sweep_expired_on_startup(_metafile_render_stop))
        _maintenance_threads.append(api._start_workspace_maintenance_loop(_metafile_render_stop))  # 期限切れ掃除などを定期実行
        api._backfill_turn_metrics_on_startup()
        from sherpa.ingest import metafile_render
        metafile_render.start_loop(_metafile_render_stop)  # WMF/EMF の図全体の描画待ちを 1 件ずつ進める
        yield
    finally:
        _metafile_render_stop.set()
        # shutdown 時は取り込みの背景実行も新規受付を止め、実行中の run に短い猶予を与える（best-effort）
        try:
            from sherpa.ingest import background
            background.stop_accepting()
            background.drain(timeout=5.0)
        except Exception as e:
            _log.warning("背景実行の shutdown drain に失敗しました（best-effort）: %s", e)
        ext_api._audit_writer.stop()  # 新規受付を止め、既存 queue を回収してから終了する
        try:
            codex_jobs_worker.stop()  # 新規 claim を止める（実行中ジョブは daemon のまま）
        except Exception as e:
            _log.warning("codex jobs ワーカーの shutdown に失敗しました（best-effort）: %s", e)
        # 掃除スレッドの DB 利用が収まるのを待つ（停止イベントは上で立て済み・best-effort）
        for _t in _maintenance_threads:
            if _t is not None:
                _t.join(timeout=10.0)
        # PG プールのクローズ（背景処理の DB 利用が収まった後に閉じる）
        try:
            from sherpa.store import db as store_db
            store_db.close_pg_pool()
        except Exception as e:
            _log.warning("PG プールの shutdown クローズに失敗しました（best-effort）: %s", e)
        # Neo4j driver シングルトンのクローズ
        try:
            from sherpa import deps
            deps.close_neo4j_driver()
        except Exception as e:
            _log.warning("Neo4j driver の shutdown クローズに失敗しました（best-effort）: %s", e)
