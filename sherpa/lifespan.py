"""FastAPI lifespan（起動処理の集約・非推奨の startup イベントハンドラからの移行）。

旧 startup ハンドラ 5 本を **順序を変えずに** 1 つの lifespan コンテキストマネージャへ集約する:
  ①auth bootstrap → ②fixtures fail-closed 検査 → ③folder poller → ④孤児 reconcile → ⑤workspace TTL sweep。
  （2026-07-03 RV 対応 MEDIUM で②の直後に SHERPA_TEST_DB_ISOLATED 検査を追加＝同じ流儀の fail-closed 検査）
  （背景実行チャットターン §7・docs/proposals/2026-07-03-チャット背景実行.md 導入時、②の検査群に
  workers>1 警告を追加＝fail-closed ではなく警告のみ）
  （Linux サーバホスト対応 L1・docs/proposals/2026-07-10-Linuxサーバホスト.md 導入時、②の検査群に
  フォルダ選択ルート（既定 /mnt）不在の警告を追加＝同じく fail-closed ではなく警告のみ）
  （監査台帳 2026-07-10-監査対応台帳.md #3 対応で、①auth bootstrap が既定パスワードで admin を
  DB に刻んでしまう**前**に `_warn_default_admin_password()` を追加＝唯一 ①より前に置く fail-closed 検査）
  （横断レビュー対応 R4・2026-07-13-横断レビュー対応.md 導入時、②の検査群に
  `_warn_codex_sandbox_disabled()` を追加＝production で Codex sandbox 無効なら fail-closed。
  同スライスで `_warn_multi_worker_chat_turns()` も production では警告のみ→fail-closed へ格上げ）
  （横断レビュー対応 R5・2026-07-13-横断レビュー対応.md 導入時、①より前に `store.init_schema()`
  を追加＝schema 直列化初期化（advisory lock）＋readiness（`store.schema_ready()`）連動。
  DB 不達でも起動は止めない＝try/except warning ログのみ・以後は各 store 関数の `_ensure()` に
  よる lazy 初期化の自己修復、または `/healthz` 側の1回リトライに委ねる）
  （schema 初期化の直後に `_seed_settings_from_env()` を実行＝env のクラウド AI 資格情報（openai/
  gemini/bedrock の API キー）を system_settings へ初回シード。以後 `sherpa.keys` は env を読まない・
  `sherpa/keys.py` 参照）
  （続けて `_seed_ollama_url_from_env()` を実行＝`OLLAMA_URL` は資格情報とは独立したマーカー
  （`ollama_url_seed_version`）で初回シードする＝不正な形式の値は資格情報側の確定に影響しない。
  続けて `_confirm_legacy_env_seed_marker()` を実行＝資格情報／Ollama 両方の新マーカーが確定した後にだけ
  旧共有マーカー（`env_seed_version`）も互換のため追いつき確定する＝rollback 時に旧コードが
  正しく再評価できるようにする）
  （同じ並びに `_seed_openai_endpoint_from_env()` を追加＝OpenAI 互換 API の接続先4項目
  （`openai_endpoint_kind`/`openai_base_url`/`openai_auth_header`/`openai_api_version`）も同じ
  「一度だけ」方式で初回シードする。`healthz()` 側の同名呼び出しは DB 一時不達時の再試行専用
  （通常起動の唯一の実行経路はここ）・`sherpa/llm.py` 参照）
  （外部連携 API（docs/proposals/2026-08-24-部品API設計.md）対応で、`ext_api._audit_writer`
  （監査DB書込み専用の単一 writer スレッド）の start/stop をここで管理する＝プロセス起動と共に
  受付を開始し、shutdown 時は新規受付を止めて既存 queue を回収してから終了する）
  （LOG-2（サブシステム別ログ分離）対応で、起動処理の先頭（`_attach_request_id_filter()` より前）に
  `log_setup.configure_logging()` を追加＝順序の理由は `sherpa/log_setup.py` 参照）
  （TOGGLE-RM・2026-09-03: `_warn_default_admin_password()` の直後に `_warn_auth_disabled_in_production()`
  を追加＝本番プロファイルで `SHERPA_AUTH_DISABLED` が残っていたら1回だけ ERROR ログ。判定自体は
  `auth.auth_disabled()` が production では常に無視するため fail-closed 起動拒否は不要＝警告のみ）
  （ENV-ONE・env 例の1本化・2026-09-03: `_warn_default_admin_password()` の**直前**に
  `_warn_change_me_placeholders()` を追加＝配布テンプレのプレースホルダ（`CHANGE_ME`）が env に
  残ったまま admin パスワードが DB に刻まれる前に fail-closed で止める。同スライスで
  `_warn_default_admin_password()` 自体も「開発既定と同値の明示設定は許す」旧扱いを撤回し、
  未設定と同じく拒否するよう強化）

各起動処理の実体は `sherpa.api` 側の関数にそのまま残す（フェーズ1は純移動＝処理内容・順序不変）。
本モジュールは起動時の**呼び出し順**だけを担う。`sherpa.api` は import 時に本モジュールの `lifespan`
を参照するため、循環 import を避けて `sherpa.api` の参照は lifespan 実行時（runtime）の遅延 import にする。
`sherpa.store` は `sherpa.api` を import しない下位モジュールのため循環の懸念が無く、モジュール先頭で
通常 import している。`sherpa.ext_api` も `sherpa.api`/本モジュールを import しない下位モジュールの
ため、同じ理由でモジュール先頭で通常 import している。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from sherpa import ext_api, log_setup, store

_log = logging.getLogger("sherpa")


@asynccontextmanager
async def lifespan(app):
    # LOG-2（2026-09-03）: サブシステム別ログ（専用ファイルハンドラ）を設定する。ここより後段の
    # ext_api._attach_request_id_filter() が "sherpa"/"sherpa.*" の作成済み logger の handlers を
    # 毎回スキャンして request_id フィルタを付け直すため、**必ずそれより前**に呼ぶ契約
    # （configure_logging() が付ける handler にもフィルタが自動で届く）。失敗は起動を止めない
    # （fail-open・ログ設定自体が起動失敗の原因になっては本末転倒）。
    try:
        log_setup.configure_logging()
    except Exception as e:
        _log.warning("サブシステム別ログの設定に失敗しました（起動は続行します）: %s", e)
    # ASGI サーバー（uvicorn 等）はロギング設定を import より後に終えることがあるため、
    # import 時点の _attach_request_id_filter() 呼び出しだけでは root へ後から追加された
    # handler を取りこぼしうる。ロギング設定が完了しているはずの起動処理の先頭で再度呼ぶ
    # （冪等）。ログ設定の副作用（request_id 付与）が起動失敗になっては本末転倒なので、
    # 想定外の例外が起きても起動処理自体は止めない（fail-open）。
    try:
        ext_api._attach_request_id_filter()
    except Exception as e:
        _log.warning("request_id ログ filter の再適用に失敗しました（起動は続行します）: %s", e)
    # R5: schema 初期化（advisory lock で直列化・DDL は毎起動全文冪等実行）。DB 不達でも起動は
    # 止めない（readiness が false のままになるだけ・_warn_default_admin_password より前に置く
    # 理由は無い＝admin bootstrap 自体が schema 依存のため、schema 初期化を先に試みておく）。
    try:
        store.init_schema()
    except Exception as e:
        _log.warning("起動時のスキーマ初期化に失敗しました（DB 不達の可能性・lazy 初期化にフォールバックします）: %s", e)
    # ING-3（取り込みの背景実行化・中断リカバリー）: 起動直後はどの world も実行中でない
    # （単一 worker 前提）ため、この時点で見つかる `status='extracting'` の ingest_runs 行は
    # 例外なくプロセス強制死（OOM/kill）の孤児——以後の poller/リクエスト受付が新しい run を
    # 始める前に failed（中断）へ一括格下げする（fail-open・DB 不達でも起動は止めない）。
    try:
        downgraded = store.downgrade_orphaned_extracting_runs()
        if downgraded:
            _log.warning("起動時に中断された取り込み run を検知し failed へ格下げしました: ids=%s", downgraded)
    except Exception as e:
        _log.warning("起動時の孤児 run 格下げに失敗しました（DB 不達の可能性）: %s", e)
    # /ext/v1 監査書込み専用 writer スレッド。start() は実際に稼働状態を確立できたか（bool）を
    # 返す——False（旧世代がまだ停止しきっていない等）でも起動処理自体は止めない（fail-open）が、
    # ERROR ログは残す。writer 側は自己回復する（旧世代が実際に終わった時点で `_run()` の
    # finally が状態を STOPPED へ確定し、以後の submit()/start() の再試行で正しく再起動できる）
    # ため、ここでリトライループは組まない。
    if not ext_api._audit_writer.start():
        _log.error("ext_api audit writer の起動に失敗しました（旧世代のスレッドがまだ停止して"
                  "いない可能性）。旧世代の終了後、次回の submit()/start() で自己回復します。")
    try:
        # 実行順は旧 startup ハンドラの登録順を維持する（純移動）。起動処理の本体は sherpa.api 側に残す。
        # ここから yield までの間・yield 復帰後（shutdown 中）に例外が起きても、writer の stop() は
        # 必ず実行する（try/finally）——さもないと start() 済みの writer が誰にも stop() されないまま
        # プロセスに取り残される（daemon なので終了は妨げないが、正常な graceful shutdown 契約が崩れる）。
        from sherpa import api, model_catalog
        api._seed_settings_from_env()
        api._seed_ollama_url_from_env()   # OLLAMA_URL は独立マーカー（上記docstring参照）
        api._confirm_legacy_env_seed_marker()   # 旧共有マーカーの互換確定（上記docstring参照）
        api._catchup_ollama_allowlist_for_central_url()   # 同じ「一度だけ」方式の追いつき移行
        api._seed_openai_endpoint_from_env()   # SET-2c: OpenAI 接続先も同じ「一度だけ」方式で初期化する
        api._seed_depth_profile_from_env()   # SC-6c: 調べる深さの基準値7項目も同じ「一度だけ」方式で初期化する
        model_catalog.seed_catalog_once()   # model_catalog も同じ「一度だけ」方式で初期化する
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
        api._start_poller()
        api._reconcile_orphans()
        api._sweep_expired_on_startup()
        yield
    finally:
        # ING-3: shutdown 時は取り込みの背景実行（`sherpa.ingest.background`）も新規受付を
        # 止める——直後に `downgrade_orphaned_extracting_runs()`（次回起動時）に頼らず、今動いて
        # いる run が「中断」ではなく完走できるだけの短い猶予（best-effort）を与える。daemon
        # thread のためこの drain 自体が終了を妨げることはない（timeout で必ず戻る）。
        try:
            from sherpa.ingest import background
            background.stop_accepting()
            background.drain(timeout=5.0)
        except Exception as e:
            _log.warning("背景実行の shutdown drain に失敗しました（best-effort）: %s", e)
        ext_api._audit_writer.stop()   # 新規受付を止め、既存 queue を回収してから終了する
        # QW2（性能台帳#17）: PG プール（`store.db._get_pg_pool()`）のクローズ。上記の drain/stop で
        # 背景処理からの DB 利用が収まった後に閉じる（`atexit` にも保険で登録済み・二重クローズは冪等）。
        try:
            from sherpa.store import db as store_db
            store_db.close_pg_pool()
        except Exception as e:
            _log.warning("PG プールの shutdown クローズに失敗しました（best-effort）: %s", e)
        # QW2: Neo4j driver シングルトン（`deps._driver()`）のクローズ（`atexit` にも保険で登録済み）。
        try:
            from sherpa import deps
            deps.close_neo4j_driver()
        except Exception as e:
            _log.warning("Neo4j driver の shutdown クローズに失敗しました（best-effort）: %s", e)
