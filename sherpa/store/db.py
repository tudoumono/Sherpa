"""store の基盤: DSN / 接続 / advisory lock / スキーマ初期化（フェーズ4 S1・純移動）。

`sherpa/store.py` から `_KB_ID`・`_SCHEMA`（DDL・当時97文）・`_inited`・`_dsn`・`_connect`・
`init_schema`・`_ensure`・`world_lock`・`workspace_file_lock` を純移動したもの。ロジックは
一切変更していない（`tests/unit/test_store_surface.py` の `_SCHEMA` 内容ハッシュ golden で
純移動であることを担保）。`_inited` の global 管理はこのモジュール内で完結する。

R5（2026-07-13-横断レビュー対応.md §3）で `init_schema` を `pg_advisory_lock` で直列化し、
記録専用の `schema_version` 表＋`schema_ready()`（readiness 判定用）を追加した。
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import logging
import math
import os
import re
import threading
import time

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

# `system_extras.py`/`ext_api.py` 等と同じ共有ロガー（新しいロガーを増やさない・単一の真実源）。
_log = logging.getLogger("sherpa")

# 単一 KB 前提（多 KB 概念は死んでおり非 global 値は存在しない）。SQL バインドはこの定数に固定する。
# DB カラム `kb_id`（DEFAULT 'global'）は過去データ・監査の保全のため温存（rename/drop はスコープ外）。
_KB_ID = "global"

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'admin',
        version TEXT NOT NULL DEFAULT 'v1',
        title TEXT,
        codex_session_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        lens TEXT,
        route JSONB,
        trace JSONB,
        answer JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS msg_conv ON messages(conversation_id)",
    # per-turn 個人利用フラグ（sanitized share 用・そのターンが個人ファイル/Codex書込を使ったか）。
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS personal BOOLEAN NOT NULL DEFAULT false",
    # ピン止め（お気に入り・#8）。既存テーブルにも冪等で列追加。
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT false",
    # 文書台帳（P3・DATA-MODEL documents の MVP 部分集合）。版内パス scope_path と層 layer を持つ。
    # MVP はコーパス(md/src＋structure.json)から seed する（P1 の取り込みワーカーが将来ここを書く）。
    """CREATE TABLE IF NOT EXISTS documents (
        id SERIAL PRIMARY KEY,
        kb_id TEXT NOT NULL DEFAULT 'global',
        version TEXT NOT NULL,
        name TEXT NOT NULL,                       -- doc_id（MD=stem／ソース=basename・grep/グラフと一致）
        layer TEXT NOT NULL DEFAULT 'version',    -- common / version / personal
        scope_path TEXT,                          -- 版内のフォルダパス（版プレフィックス無し・NULL=版直下/共通）
        doctype TEXT,
        branch TEXT,                              -- source / office
        original_path TEXT,                       -- 原本（DL保証）URI
        md_path TEXT,                             -- MD化版（Office枝のみ）
        status TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (kb_id, version, name)
    )""",
    "CREATE INDEX IF NOT EXISTS doc_ver ON documents(kb_id, version)",
    # RV1是正#2（2026-09-01）: `GET /documents` の台帳高速経路（`doc_ledger.public_documents_page`）が
    # 実走査せずに重要度を返せるよう、ingest 時（`ingest/worker.py::_ledger_rows`）に1回だけ解決して
    # materialize する列（`_重要度.txt` 由来・§2 truth table＝無ければ3列とも NULL）。
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS importance TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS importance_reason TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS importance_source TEXT",
    # 鏡モデル: world レジストリ（world_id → 参照元 root_path の 1:1 バインド）。版ライフサイクルは撤去。
    # 参照先変更（rebind）＝この行を更新し、その world の派生物を全削除して再ミラー（worlds.rebind）。
    """CREATE TABLE IF NOT EXISTS worlds (
        kb_id TEXT NOT NULL DEFAULT 'global',
        world_id TEXT NOT NULL,                    -- 取込ディレクトリ識別子（例 4期システム / XXX開発）
        root_path TEXT NOT NULL,                   -- 参照元のルート（WSL パス・external_reference の鏡元）
        label TEXT,                                -- 表示名
        storage_mode TEXT NOT NULL DEFAULT 'external_reference'
            CHECK (storage_mode IN ('external_reference','managed_copy')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (kb_id, world_id)
    )""",
    # 1 world = 1 参照元（双方向 1:1）。同じ root を別 world に二重登録させない。
    "CREATE UNIQUE INDEX IF NOT EXISTS worlds_root ON worlds(kb_id, root_path)",
    # 変更検知用の署名（フォルダ内容のハッシュ）と最終同期時刻＝ポーリング/手動更新が「変わった時だけ」再取り込み。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_sig TEXT",
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ",
    # 最後に取り込んだ時点のファイル明細（rel→[mtime_ns,ctime_ns,size]）。差分チェック（追加/削除/変更）の基準。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_manifest JSONB",
    # 成功確定した取り込みが最後に数えた doctype 対応原本の件数（`worker._run_locked` の成功パスのみ
    # 更新・pre-invalidate/failed では更新しない）。外部公開 discovery（/ext/v1/capabilities）が
    # world ごとにファイルツリーを走査せず定数時間で返すための事前集計値。NULL＝未確定（一度も
    # 成功同期していない）。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_doc_count INTEGER",
    # 取り込み集計（`corpus_docs.scan_report()`）のキャッシュ（ING-2）。`GET /worlds/{wid}/status` が
    # フォルダを歩かないための事前集計置き場——`last_doc_count` と同じ「成功パスのみ更新」の考え方だが、
    # 別列に分ける: `ingest_runs` の最新1件（`last_run_status` 等の由来）が失敗でも、直前に成功していた
    # 集計をそのまま表示し続けたい（scan_report は「最後に分かった内訳」であり「直近の run 結果」とは
    # 別物）。NULL＝未集計（一度も成功同期／明示の再集計（`POST /worlds/{wid}/recount`）を行っていない）。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_scan_report JSONB",
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_scan_report_at TIMESTAMPTZ",
    # 取り込み・抽出のステージング（P1・DATA-MODEL ingest_runs）＝グラフ反映境界を run 単位で記録。
    # version は documents.version と同じ自由文字列（FK 厳密化は後段）。共通 run は version=NULL＋layer=common。
    """CREATE TABLE IF NOT EXISTS ingest_runs (
        id SERIAL PRIMARY KEY,
        kb_id TEXT NOT NULL DEFAULT 'global',
        version TEXT,                              -- 単一版（共通 run は NULL）
        layer TEXT NOT NULL DEFAULT 'version',     -- version / common
        ingest_source_id INTEGER,                 -- 取込元（P1b・今は NULL）
        scan_root TEXT,                            -- スキャンルート（P1b）
        scope_mapping_overrides JSONB,            -- 階層自動判定のUI上書き（P1b）
        source_doc_ids JSONB,                     -- 対象 documents.name[]
        status TEXT NOT NULL DEFAULT 'extracting'  -- extracting/auto_published/auto_published_with_flags/failed
            CHECK (status IN ('extracting','auto_published','auto_published_with_flags','failed')),
        extraction_snapshot JSONB,                -- 抽出スナップショット（件数・検証フラグ）
        published_snapshot JSONB,                 -- Neo4j 反映内容（差分・再反映の基準）
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_at TIMESTAMPTZ,
        republished_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS run_ver ON ingest_runs(kb_id, version)",
    # `store.ingest.get_latest_run_summary`/`get_latest_published_run_summary`（status の O(1) 契約）
    # は `ORDER BY id DESC LIMIT 1` で最新1件を引く——`run_ver`（kb_id, version のみ）だけでは
    # 候補行の id ソートを都度行うため、run 履歴が積み上がった world ほど遅くなる。id を含めた索引で
    # ソート済みの順で先頭1件を返せるようにする。反映済み run 用の絞り込み（`published_at IS NOT NULL`）
    # は世代のごく一部（成功して Neo4j へ反映できた run だけ）なので、部分索引で対象を絞り小さく保つ。
    "CREATE INDEX IF NOT EXISTS run_ver_id ON ingest_runs(kb_id, version, id DESC)",
    "CREATE INDEX IF NOT EXISTS run_ver_published ON ingest_runs(kb_id, version, id DESC) "
    "WHERE published_at IS NOT NULL",
    # ING-3（取り込みの背景実行化）: 実行中 run の逐次進捗（段＋done/total/更新時刻）。`status='extracting'`
    # の間だけ意味を持ち、完了時（`finish_ingest_run`）に NULL へ戻す——status 自体で「実行中か」を
    # 判別できるため、progress の有無を別途の真偽値として持たない。
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS progress JSONB",
    # `store.ingest.get_latest_es_run_summary` 専用: ES 反映は台帳 replace 成功後にのみ実行されるため
    # 「反映済み（published_at）」だけでは足りず「実際に ES 段まで到達した（extraction_snapshot に
    # es キーがある）」run へさらに絞り込む——`run_ver_published` と対象がずれる分、別の部分索引にする。
    "CREATE INDEX IF NOT EXISTS run_ver_published_es ON ingest_runs(kb_id, version, id DESC) "
    "WHERE published_at IS NOT NULL AND extraction_snapshot ? 'es'",
    # 取り込み元の登録（P1b-2・DATA-MODEL ingest_sources）＝大量取り込みのスキャン元（常時 watch しない）。
    """CREATE TABLE IF NOT EXISTS ingest_sources (
        id SERIAL PRIMARY KEY,
        kb_id TEXT NOT NULL DEFAULT 'global',
        label TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('browser_upload','windows_path')),
        source_uri TEXT,                          -- Windows/NAS 元パス（browser_upload は NULL）
        wsl_path TEXT,                            -- /mnt に解決したスキャンルート
        storage_mode TEXT NOT NULL DEFAULT 'managed_copy'
            CHECK (storage_mode IN ('managed_copy','external_reference')),
        status TEXT NOT NULL DEFAULT 'registered'
            CHECK (status IN ('registered','scanning','scanned','failed')),
        version TEXT NOT NULL,                    -- 取り込み先の版（documents.version と同じ自由文字列）
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_checked TIMESTAMPTZ
    )""",
    # 既存の古い documents 表にも P3 列を冪等追加（migration）。
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS layer TEXT NOT NULL DEFAULT 'version'",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS scope_path TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doctype TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS branch TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS original_path TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS md_path TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS status TEXT",
    """CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT PRIMARY KEY,
        agent TEXT NOT NULL DEFAULT 'codex',
        codex_reasoning TEXT NOT NULL DEFAULT 'low',
        openai_api_key TEXT,
        openai_model TEXT NOT NULL DEFAULT 'gpt-5.5',
        ollama_url TEXT NOT NULL DEFAULT 'http://localhost:11434',
        ollama_model TEXT NOT NULL DEFAULT 'qwen2.5',
        system_prompt TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # システムプロンプト（#2）。既存テーブルにも冪等で列追加。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS system_prompt TEXT",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS gemini_api_key TEXT",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS gemini_model TEXT NOT NULL DEFAULT 'gemini-2.5-flash'",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS codex_model TEXT NOT NULL DEFAULT 'gpt-5.5'",
    # Codex 強化計画 Phase0・§5-1 で追加。WEB-1 以降、web_search はチャットごとの
    # 希望（`ChatReq.web_search`）のみを実行に使う——この列は互換のため残すが実行経路では読まない
    # （GET/PUT /settings の入出力のみ・`agents._web_search_disabled_value` 参照）。既定 false。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS codex_web_search BOOLEAN NOT NULL DEFAULT false",
    # ナレッジ抽出（グラフ生成）に使うプロバイダ＝チャットの頭脳(agent)とは独立（設定画面で選ぶ）。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS extract_provider TEXT NOT NULL DEFAULT 'auto'",
    # AWS Bedrock（Claude）: region / モデル / API キー（Bearer・書込専用）。api_key 未設定は SDK の env/SigV4 チェーンに委譲。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS bedrock_region TEXT",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS bedrock_model TEXT NOT NULL "
    "DEFAULT 'jp.anthropic.claude-haiku-4-5-20251001-v1:0'",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS bedrock_api_key TEXT",
    # S0（2026-07-15 設定分離）: intent 分類（intent_llm）に使うモデル名。NULL＝未設定＝各プロバイダの既定モデル。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS intent_model TEXT",
    # S2（extract_provider 分割・2026-07-15）: 機能別プロバイダ。空文字＝未設定＝extract_provider に従う。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS graph_provider TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS intent_provider TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS embed_provider TEXT NOT NULL DEFAULT ''",
    # S3（プロファイル型サブエージェント・2026-07-15-LLMオーケストレーション実装計画.md §5.0）: 資料の
    # 検索・下調べを任せるサブエージェント・プロファイル id。''＝OFF（既定・現行と byte-identical）。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS sub_profile TEXT NOT NULL DEFAULT ''",
    # S4-c（複数プロファイル並用＋自動選択・§6.3）: フラグシップが enabled プロファイルの中から
    # 1〜3個を自動で選び直列実行する「計画」ステップの ON/OFF。''＝OFF（既定・現行と byte-identical）・
    # 許可値は 'auto' のみ（routers/system.py::settings_put が検証）。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS sub_planner TEXT NOT NULL DEFAULT ''",
    # 4構成（2026-08-15・`sherpa/agent_constructs.py`）: Codex CLI がどのモデル提供元へ接続するか
    # （openai / ollama）。Codex 構成のときだけ意味を持つ。''＝未設定＝openai として扱う。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS codex_model_provider TEXT NOT NULL DEFAULT ''",
    # 検索アシスタント（2026-08-15・`sherpa/search_helper.py`）: 資料の検索・精読だけを安いモデルへ
    # 任せる利用者ごとの設定。''＝使わない（メインのAIが検索する）／'ollama'／'openai'。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS search_helper TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS search_helper_model TEXT NOT NULL DEFAULT ''",
    # 2026-08-16: 新規利用者の既定を `heuristic`（簡易・AIなし）から既定構成へ変える。
    # `CREATE TABLE IF NOT EXISTS` は既存 DB の DEFAULT を書き換えないため、ここで明示的に直す。
    # **既存行は書き換えない**: `SHERPA_EXTRA_AGENTS=heuristic` で「AIなし」を意図的に選んだ利用者を、
    # 黙って AI が答える状態へ移してしまうため（選び直しは画面のAI選択から1クリックでできる）。
    "ALTER TABLE user_settings ALTER COLUMN agent SET DEFAULT 'codex'",
    # モデル名/Ollama接続先の列 DEFAULT を空文字へ変更する。空文字＝「未設定＝モデルカタログの
    # 既定／中央既定に従う」という現行契約（`sherpa/model_catalog.py`）に列 DEFAULT を揃える。
    # 新規行（新規利用者）が二度と「自由入力時代のハードコード値」を個人 override として持たない
    # ようにする。**既存行は書き換えない**（上の `agent` 列と同じ理由・保存済みの値に provenance
    # が無く、能動的な選択と旧既定の値が偶然一致した行を区別できないため。既存利用者が中央既定へ
    # 戻したい場合は、個人設定の「管理者の既定を使う」選択肢が既にある）。
    "ALTER TABLE user_settings ALTER COLUMN openai_model SET DEFAULT ''",
    "ALTER TABLE user_settings ALTER COLUMN gemini_model SET DEFAULT ''",
    "ALTER TABLE user_settings ALTER COLUMN ollama_model SET DEFAULT ''",
    "ALTER TABLE user_settings ALTER COLUMN ollama_url SET DEFAULT ''",
    "ALTER TABLE user_settings ALTER COLUMN codex_model SET DEFAULT ''",
    # OCR（任意の視覚観測・2026-08-16 移植）: 取り込みとは独立したジョブキュー。
    # `canonical_generation_id` は上流（フル世代管理）の列名をそのまま使い、この branch では
    # **World署名（worlds.last_sig）**を入れる（原本内容が変わればキーも変わる＝キャッシュの意味は同じ）。
    # 将来フル世代管理へ寄せるときは、入れる値を世代IDへ替えるだけで済む。
    """CREATE TABLE IF NOT EXISTS ocr_jobs (
        id BIGSERIAL PRIMARY KEY,
        world TEXT NOT NULL,
        source_rel_path TEXT NOT NULL,
        canonical_generation_id TEXT NOT NULL,
        source_content_hash TEXT NOT NULL,
        route_manifest_hash TEXT NOT NULL,
        route_input_id TEXT NOT NULL,
        route_input JSONB NOT NULL,
        engine_profile_hash TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','leased','succeeded','failed','stale','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        result_observation_set_hash TEXT,
        result_payload JSONB,
        cache_hit BOOLEAN NOT NULL DEFAULT false,
        observation_count INTEGER CHECK (observation_count IS NULL OR observation_count >= 0),
        artifact_published BOOLEAN NOT NULL DEFAULT false,
        error_code TEXT,
        error_detail TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        UNIQUE (world, canonical_generation_id, route_input_id, engine_profile_hash)
    )""",
    "ALTER TABLE ocr_jobs ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE ocr_jobs ADD COLUMN IF NOT EXISTS observation_count INTEGER",
    "ALTER TABLE ocr_jobs ADD COLUMN IF NOT EXISTS artifact_published BOOLEAN NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS ocr_jobs_leaseable ON ocr_jobs(priority DESC, available_at, id) "
    "WHERE status IN ('queued','leased')",
    "CREATE INDEX IF NOT EXISTS ocr_jobs_world_generation ON ocr_jobs(world, canonical_generation_id, status)",
    # observation generationの公開は2TB級Worldでもfetchallせず、この順序のkeyset cursorでstreamする。
    "CREATE INDEX IF NOT EXISTS ocr_jobs_publish_order "
    "ON ocr_jobs(world, canonical_generation_id, source_rel_path, result_observation_set_hash, id) "
    "WHERE status='succeeded' AND result_payload IS NOT NULL AND result_observation_set_hash IS NOT NULL",
    # 取り込み側は「この World 署名の OCR を作り直す」1行を enqueue するだけとし、隔離 worker が
    # 派生物内を決定順に stream して ocr_jobs へ展開する。cursor_rel_path は再起動時の再開位置で、
    # manifest 本文や原本 path は DB へ複製しない。
    """CREATE TABLE IF NOT EXISTS ocr_refresh_runs (
        id BIGSERIAL PRIMARY KEY,
        world TEXT NOT NULL,
        canonical_generation_id TEXT NOT NULL,
        engine_profile_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','leased','completed','failed','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
        cursor_rel_path TEXT,
        manifests_processed BIGINT NOT NULL DEFAULT 0 CHECK (manifests_processed >= 0),
        selected_count BIGINT NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
        excluded_count BIGINT NOT NULL DEFAULT 0 CHECK (excluded_count >= 0),
        failed_binding_count BIGINT NOT NULL DEFAULT 0 CHECK (failed_binding_count >= 0),
        jobs_enqueued BIGINT NOT NULL DEFAULT 0 CHECK (jobs_enqueued >= 0),
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        error_code TEXT,
        error_detail TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        UNIQUE (world, canonical_generation_id, engine_profile_hash)
    )""",
    "CREATE INDEX IF NOT EXISTS ocr_refresh_runs_leaseable ON ocr_refresh_runs(updated_at, id) "
    "WHERE status IN ('queued','leased')",
    "CREATE INDEX IF NOT EXISTS ocr_refresh_runs_world_generation "
    "ON ocr_refresh_runs(world, canonical_generation_id, status)",
    # 同じ World 内の同一前処理画像＋engine profile は先勝ち cache を共有する。payload は engine の
    # 生 OCR 行だけで、文書固有 Evidence ID を持つ Observation Set は job ごとに再構築する。
    """CREATE TABLE IF NOT EXISTS ocr_result_cache (
        world TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        engine_profile_hash TEXT NOT NULL,
        result_hash TEXT NOT NULL,
        result_payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (world, input_fingerprint, engine_profile_hash)
    )""",
    # FastAPI 本体は Paddle 依存を持たないため、availability は隔離 worker 自身の heartbeat を権威にする。
    """CREATE TABLE IF NOT EXISTS ocr_worker_heartbeats (
        worker_id TEXT PRIMARY KEY,
        engine_profile_hash TEXT NOT NULL,
        available BOOLEAN NOT NULL,
        unavailable_reason TEXT,
        model_hashes_valid BOOLEAN NOT NULL,
        status TEXT NOT NULL DEFAULT 'starting'
            CHECK (status IN ('starting','idle','processing','unavailable','stopping')),
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ocr_worker_heartbeats_profile_seen "
    "ON ocr_worker_heartbeats(engine_profile_hash, last_seen_at DESC)",

    # RV MED（2026-07-16 Codex RV 1巡目 F1/F4/F6 是正）: 保存できる bedrock_model を「実在確認済みID」に
    # 限定するための正本（sherpa/routers/system.py::_bedrock_model_id_valid 参照）。形が正しいだけの
    # 架空IDが保存できてしまう穴（BEDROCK_MODEL_ID_RE.fullmatch だけの許可）を塞ぐため、verify 成功時／
    # 列挙成功時に実際に確認できた ID をここへ記録する。当初は `user_settings.bedrock_verified_models`
    # 列だったが、以下3点の実害が見つかったため**専用テーブルへ分離**した（`user_settings` には一切
    # 触れない）:
    #   F1: 行が無いユーザーの `SELECT ... FOR UPDATE` は何もロックできない＝初回の verify/列挙が
    #       2並行すると両方 `[]` を読み、後勝ちで先の記録が消える。
    #   F4: 行なしユーザーへの記録が `user_settings` に行を実体化させ、`agent` 列既定 'heuristic' が
    #       入る＝`SHERPA_AGENT=bedrock` 環境で「列挙しただけでエージェントが変わる」regression。
    #   F6: 列が `_SETTINGS_FIELDS` にある限り `update_settings(uid, bedrock_verified_models=[...])`
    #       が行なし時に素通りしうる＝「記録は verify/列挙成功のみ」という不変条件と矛盾。
    # `add_bedrock_verified_models`（sherpa/store/settings.py）はこのテーブルに対して
    # 「INSERT ... ON CONFLICT DO NOTHING で行確保 → SELECT ... FOR UPDATE → UPDATE」の3文で完全直列化する。
    """CREATE TABLE IF NOT EXISTS bedrock_verified_models (
        user_id TEXT PRIMARY KEY,
        ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # 旧モデル名（提供終了）を現行へ寄せる（gpt-4o→gpt-5.5／gemini-2.0-flash→gemini-2.5-flash）。
    "UPDATE user_settings SET openai_model='gpt-5.5' WHERE openai_model='gpt-4o'",
    "UPDATE user_settings SET gemini_model='gemini-2.5-flash' WHERE gemini_model IN ('gemini-2.0-flash','gemini-1.5-flash')",
    # Bedrock 旧 Mantle 短縮 ID → JP 推論プロファイルへ移行（runtime 切替 2026-07-03・旧 ID は runtime で 400＝全ユーザーで無効）。
    # **既知の旧3値のみ**対象（NOT IN 方式にすると将来 BEDROCK_MODEL_CHOICES へ追加した新 ID を起動時に潰すため不可）。
    "UPDATE user_settings SET bedrock_model='jp.anthropic.claude-haiku-4-5-20251001-v1:0' "
    "WHERE bedrock_model IN ('anthropic.claude-opus-4-8','anthropic.claude-sonnet-5','anthropic.claude-haiku-4-5')",
    # ---- 認証・ユーザー管理・会話共有（docs/proposals/2026-07-01-認証と共有の提案.md MVP）----
    # users＝アプリの正本。uid（文字列キー）が conversations/user_settings.user_id と接続する。
    """CREATE TABLE IF NOT EXISTS users (
        id BIGSERIAL PRIMARY KEY,
        uid TEXT UNIQUE,
        email TEXT UNIQUE,
        display_name TEXT,
        password_hash TEXT,
        role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','pending')),
        must_change_password BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login_at TIMESTAMPTZ
    )""",
    # 既存 seed（seed_admin.sql の users）にも冪等で列追加（型が無い環境でも揃える）。
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS uid TEXT UNIQUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    # seed_admin.sql の email は NOT NULL だったが MVP は email 任意（uid がキー）→ nullable に緩める（冪等）。
    "ALTER TABLE users ALTER COLUMN email DROP NOT NULL",
    # seed admin を uid='admin' に接続（既存 email があれば埋める・無ければ upsert は seed 側/初回ログイン整備で）。
    "UPDATE users SET uid='admin' WHERE email='admin@sherpa.local' AND uid IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_uid_key ON users(uid)",
    # セッション（cookie の opaque token は hash で保存）。
    """CREATE TABLE IF NOT EXISTS auth_sessions (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        token_hash TEXT UNIQUE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id, expires_at)",
    # conversations を「所有」と「受領共有」の両対応へ（受領はメッセージをコピーせず元会話を参照）。
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'own'",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS source_conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS shared_by_user_id TEXT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS read_only BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS contains_personal_workspace BOOLEAN NOT NULL DEFAULT false",
    # 共有リンク（token hash・期限・取消・招待）。
    """CREATE TABLE IF NOT EXISTS conversation_shares (
        id BIGSERIAL PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        owner_user_id TEXT NOT NULL,
        token_hash TEXT UNIQUE NOT NULL,
        scope TEXT NOT NULL DEFAULT 'view' CHECK (scope IN ('view')),
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS conversation_shares_conv ON conversation_shares(conversation_id)",
    """CREATE TABLE IF NOT EXISTS conversation_share_invites (
        id BIGSERIAL PRIMARY KEY,
        share_id BIGINT NOT NULL REFERENCES conversation_shares(id) ON DELETE CASCADE,
        invitee_user_id TEXT NOT NULL,
        invited_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        accepted_at TIMESTAMPTZ
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS share_invites_user_unique ON conversation_share_invites(share_id, invitee_user_id)",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS share_id BIGINT REFERENCES conversation_shares(id)",
    # 同じユーザー・同じ share は履歴に1行だけ（受領ラッパーの冪等キー）。
    "CREATE UNIQUE INDEX IF NOT EXISTS conv_received_share_once ON conversations(user_id, share_id) "
    "WHERE origin='received_share' AND share_id IS NOT NULL AND deleted_at IS NULL",
    # W3: uid スラッグ形式を DB レベルで強制（多層防御）。
    # NULL uid も拒否する（Postgres の CHECK は NULL/UNKNOWN を通してしまうため IS NOT NULL を含む）。
    # 冪等化: 古い（NULL を許した）制約が存在すれば先に DROP し、正しい定義で再追加する。
    # seed admin の uid='admin' バックフィル（上の UPDATE）は _SCHEMA の先に実行済みなので
    # VALIDATE 時点で uid=NULL の admin 行は残っていない。
    """DO $$ BEGIN
      -- 旧制約（NULL uid を通す可能性あり）が存在すれば先に DROP。
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'users_uid_format' AND conrelid = 'users'::regclass
      ) THEN
        ALTER TABLE users DROP CONSTRAINT users_uid_format;
      END IF;
      -- 正しい定義で追加（NOT VALID = 既存行を即時スキャンしない）。
      ALTER TABLE users ADD CONSTRAINT users_uid_format
        CHECK (uid IS NOT NULL AND uid ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') NOT VALID;
    END $$""",
    # VALIDATE（既存行が全て適合なら即完了・NOT VALID→VALID に昇格）。
    # seed admin バックフィル後に実行されるので uid=NULL の行は存在しない。
    # 冪等: VALIDATE は何度呼んでも安全。
    """DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'users_uid_format' AND conrelid = 'users'::regclass
      ) THEN
        ALTER TABLE users VALIDATE CONSTRAINT users_uid_format;
      END IF;
    END $$""",
    # 監査（login/share/revoke/denied 等）。
    """CREATE TABLE IF NOT EXISTS audit_log (
        id BIGSERIAL PRIMARY KEY,
        actor_user_id TEXT,
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT,
        detail JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # audit_log 強化（2026-07-01-監査ログ強化.md §3・冪等 ALTER）。既存行は DEFAULT で埋まる。
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'success'",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS reason TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'info'",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS request_id TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS session_id TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS ip_hash TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS user_agent TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS before_state JSONB",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS after_state JSONB",
    "CREATE INDEX IF NOT EXISTS audit_log_time ON audit_log(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS audit_log_actor_time ON audit_log(actor_user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_log_action_time ON audit_log(action, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_log_resource_time ON audit_log(resource_type, resource_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_log_outcome_time ON audit_log(outcome, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS audit_log_request ON audit_log(request_id)",
    # 2026-07-01-監査ログ強化.md §Phase2: 改ざん検知の hash-chain（entry_hash = SHA256(prev_hash || canonical_json(row)))。
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS prev_hash TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS entry_hash TEXT",
    # chain head アンカー（単一行）＝末尾行の truncation/欠落を検出するための last_id/last_hash/cnt。
    # audit() が同一 tx で更新。verify() が末尾行と照合する（RV BLOCKER: 末尾削除検出）。
    """CREATE TABLE IF NOT EXISTS audit_chain_head (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        last_id BIGINT,
        last_hash TEXT,
        cnt BIGINT NOT NULL DEFAULT 0
    )""",
    # chain_start_id: 最初の hashed 行 id（一度だけ set）。chain 開始後の NULL-hash 偽行注入を検出する基準。
    "ALTER TABLE audit_chain_head ADD COLUMN IF NOT EXISTS chain_start_id BIGINT",
    # RV: 列追加前から chain がある既存 head に idempotent backfill（最初の hashed 行 id を埋める）。
    "UPDATE audit_chain_head SET chain_start_id="
    "  (SELECT MIN(id) FROM audit_log WHERE entry_hash IS NOT NULL) "
    "  WHERE chain_start_id IS NULL AND cnt > 0",
    # ---- 個人 workspace 台帳（2026-07-01-認証と共有の提案.md §5）----
    # このテーブルは grep 専用の台帳。ES/Neo4j の共有インデックス取り込み対象に**絶対含めない**。
    # es_index.py や world_graph.py はこのテーブルを参照してはならない（RAG 非索引の不変条件）。
    """CREATE TABLE IF NOT EXISTS personal_workspace_files (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
        rel_path TEXT NOT NULL,
        original_path TEXT NOT NULL,
        size_bytes BIGINT,
        sha256 TEXT,
        status TEXT NOT NULL DEFAULT 'uploaded'
            CHECK (status IN ('uploaded','deleted','expired')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ,
        deleted_at TIMESTAMPTZ,
        UNIQUE (user_id, rel_path)
    )""",
    "CREATE INDEX IF NOT EXISTS pwf_user ON personal_workspace_files(user_id, status)",
    # ---- 運営掲示板（2026-07-02-利用統計とホーム掲示板.md Feature 2・トップ画面のお知らせ）----
    """CREATE TABLE IF NOT EXISTS announcements (
        id SERIAL PRIMARY KEY,
        author_uid TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'notice' CHECK (category IN ('maintenance','case','notice')),
        pinned BOOLEAN NOT NULL DEFAULT false,
        published BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS announcements_pub_order ON announcements(published, pinned DESC, created_at DESC)",
    # 掲示板の公開/削除タイマー（S4・2026-07）: publish_at=NULL は即時公開扱い、expire_at=NULL は無期限掲載。
    "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS publish_at TIMESTAMPTZ",
    "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS expire_at TIMESTAMPTZ",
    # RV1（2026-07・S4 再検証）: publish_at > expire_at を DB レベルでも禁止する（最後の砦）。
    # アプリ層は PATCH を SELECT...FOR UPDATE で直列化して防ぐが、CHECK 制約も併置する
    # （直接 SQL 操作や将来のコードパス漏れに対する多層防御・冪等 DO $$ + NOT VALID→VALIDATE の2段）。
    """DO $$ BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'announcements_publish_before_expire' AND conrelid = 'announcements'::regclass
      ) THEN
        ALTER TABLE announcements ADD CONSTRAINT announcements_publish_before_expire
          CHECK (publish_at IS NULL OR expire_at IS NULL OR publish_at <= expire_at) NOT VALID;
      END IF;
    END $$""",
    """DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'announcements_publish_before_expire' AND conrelid = 'announcements'::regclass
      ) THEN
        ALTER TABLE announcements VALIDATE CONSTRAINT announcements_publish_before_expire;
      END IF;
    END $$""",
    # ---- 会話共有: 無期限オプション＋共有元削除で共有先が消えない仕様
    #      （docs/proposals/2026-07-02-共有の無期限と永続化.md）----
    # NULL = 無期限。既存行（NOT NULL 制約下で作られた）は全て非NULLのまま残るため後方互換。
    "ALTER TABLE conversation_shares ALTER COLUMN expires_at DROP NOT NULL",
    # conversations.source_conversation_id の FK を CASCADE → SET NULL へ冪等移行。
    # CASCADE のままだと「元会話を削除→受領ラッパー/snapshotも巻き添えで消える」バグになる
    # （sanitized snapshot は本文コピー済みで独立に読めるのに CASCADE で消えていた）。
    # 制約名は実機で確認済み（pg_constraint）: conversations_source_conversation_id_fkey。
    # confdeltype='n' なら既に SET NULL 済み＝再実行してもスキップ（冪等）。
    """DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'conversations_source_conversation_id_fkey'
          AND conrelid = 'conversations'::regclass
          AND confdeltype <> 'n'
      ) THEN
        ALTER TABLE conversations DROP CONSTRAINT conversations_source_conversation_id_fkey;
        ALTER TABLE conversations ADD CONSTRAINT conversations_source_conversation_id_fkey
          FOREIGN KEY (source_conversation_id) REFERENCES conversations(id) ON DELETE SET NULL;
      END IF;
    END $$""",
    # ---- 全体設定（system_settings・admin 書込のみ・監査つき）----
    #      docs/proposals/2026-07-08-設定分離とUI整備.md S1。per-user の `user_settings` とは別に、
    #      全ユーザーに効くシステム全体設定を保持する汎用 KV（value=JSONB）。優先順は
    #      system_settings > env > コード既定（per-user settings の既存優先順は変えない）。
    #      任意キーを保存できる（初期キー: arms_enabled・W0 で legacy_backend 追加）。
    """CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value JSONB,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by TEXT
    )""",
    # ---- 外部連携 API キー（docs/proposals/2026-07-07-外部API化とDify.md E1）----
    # ハッシュのみ保存（プレーンキーは発行レスポンスで1度だけ返し、DB には残さない）。
    # 失効は soft（revoked_at）＝監査追跡可能。行削除はしない。
    """CREATE TABLE IF NOT EXISTS api_keys (
        id BIGSERIAL PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        label TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        revoked_at TIMESTAMPTZ,
        revoked_by TEXT,
        last_used_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS api_keys_active ON api_keys(revoked_at) WHERE revoked_at IS NULL",
    # キーの world スコープ（オプトイン）。NULL=全 world 許可（既定・後方互換）・空配列=どの world も許可しない。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_worlds TEXT[]",
    # 有効期限・日次クォータ（いずれもオプトイン・NULL=既存キーと同じ後方互換の挙動＝無期限・
    # 無制限）。所有者（自己発行キーの uid・NULL=admin 発行の従来キー）。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS daily_quota INTEGER",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_uid TEXT",
    # 発行 UI が生成する相関トークン（オプトイン・秘密ではない・UUID）。POST 応答がタイムアウト/
    # 通信断/不正な形で失われた場合に、専用の回復エンドポイント（`ext_key_recover`/
    # `ext_self_key_recover`・store の `revoke_unconfirmed_key_by_client_op_id`）がこの値を
    # 認証主体・所有条件と同一SQLの WHERE 句で照合して自動失効できるようにする。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS client_op_id TEXT",
    # PART-6（2026-09-05-Webhook通知.md W2）: キー1本につき Webhook 宛先1本（オプトイン・NULL=無効）。
    # `webhook_secret` は署名生成（HMAC-SHA256）に平文が必須のためハッシュでなく平文保管する
    # （閉域LAN・DB は管理境界内として受容・W4）。応答/一覧には secret を出さない
    # （発行応答でのみ1度返す・`system_extras.py::_key_created_out`）。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS webhook_url TEXT",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS webhook_secret TEXT",
    # `client_op_id` は非NULLに限り一意（衝突は同じ操作の二重発行とみなし 409 に変換する・
    # `api_keys.insert_api_key` が制約名で判定する）。NULL（旧来の admin 発行等・オプトインしない
    # 発行）同士は何個あっても許される（部分インデックス）。大小文字表記の違いは同一の UUID を
    # 指すため `lower()` の関数インデックスで一意性を強制する。この索引自体は単純な DDL 文字列
    # ではなく `_migrate_client_op_id_unique_index()`（`init_schema()` から呼ぶ・下記参照）が
    # 作る——`_classify_client_op_id_index()` が `pg_index`（indisunique/indisvalid・
    # indrelid・キー列数/総列数・キー式・部分インデックスの predicate）を構造的に判定し、
    # 正しい現行定義（"current"）なら何もせず（毎起動の DROP→再作成をしない）、既知の旧定義
    # （"legacy"・大小文字を区別する）からの移行は一度きり（大小文字違いの重複行を先に確定的に
    # 解消してから張り替える）、判定不能（"unknown"・非UNIQUE・invalid・複合キー・INCLUDE列・
    # 別の式・predicate 不一致 等）は fail-closed（DROP せず例外で起動を止める）。
    # daily_quota の範囲を DB でも最後の砦として制約する（アプリ層の pydantic 上限と揃える・
    # 直接 SQL 操作や将来のコードパス漏れに対する多層防御・冪等 DO $$ + NOT VALID→VALIDATE の2段）。
    # 前提: `daily_quota` へ値を書き込める経路（`insert_api_key`）は常にこの CHECK と同じ
    # `_SCHEMA` 定義（このファイル）の適用後にしか呼ばれない（起動時に毎回全文を冪等実行し、
    # アプリはそれより後にしか接続を受け付けない）ため、この制約より前に範囲外の値が書き込まれた
    # 行が存在することは通常あり得ない＝VALIDATE は通常常に安全に成功する。もし NULL 以外かつ
    # 範囲外の `daily_quota` を持つ行が（直接 SQL 操作など本関数を経由しない想定外の経路で）
    # 生じて VALIDATE が失敗した場合は、次の SQL で違反行を特定してから復旧すること
    # （黙って値を丸めない）:
    #   検出: SELECT id, owner_uid, daily_quota FROM api_keys
    #         WHERE NOT (daily_quota IS NULL OR (daily_quota > 0 AND daily_quota <= 1000000));
    #   復旧（例・行ごとに意図を確認のうえ選択）:
    #     - 上限へ丸める:   UPDATE api_keys SET daily_quota = 1000000 WHERE id = <id> AND daily_quota > 1000000;
    #     - 無制限へ戻す:   UPDATE api_keys SET daily_quota = NULL WHERE id = <id>;
    #   復旧後、次回起動時に本 DDL が再実行され VALIDATE が通る（毎起動・全文冪等実行のため）。
    """DO $$ BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'api_keys_daily_quota_range' AND conrelid = 'api_keys'::regclass
      ) THEN
        ALTER TABLE api_keys ADD CONSTRAINT api_keys_daily_quota_range
          CHECK (daily_quota IS NULL OR (daily_quota > 0 AND daily_quota <= 1000000)) NOT VALID;
      END IF;
    END $$""",
    """DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'api_keys_daily_quota_range' AND conrelid = 'api_keys'::regclass
      ) THEN
        ALTER TABLE api_keys VALIDATE CONSTRAINT api_keys_daily_quota_range;
      END IF;
    END $$""",
    # ---- R5: schema バージョンの記録専用スタンプ（2026-07-13-横断レビュー対応.md §3）----
    # DDL 適用の可否判断には使わない（DDL は毎起動・全文冪等実行を継続＝自己修復性を保つ・§2）。
    # 読み手は運用者のみ（コードのどこからも読まない／分岐しない）。
    """CREATE TABLE IF NOT EXISTS schema_version (
        id BIGSERIAL PRIMARY KEY,
        schema_hash TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # ---- S1（2026-07-15-LLMオーケストレーション実装計画.md）: チャット以外の LLM 呼び出しの計測 ----
    #      チャット本回答の usage は引き続き messages.answer->'usage' に残る（二重書き込みなし）。ここは
    #      intent 分類・グラフ抽出・概念候補提案・埋め込み・admin グラフ質問・VLM 視覚読み取りの計測専用。
    #      記録は常時（TOGGLE-RM・2026-09-03 で system_settings.usage_metering の ON/OFF トグルを撤去済み・
    #      sherpa/metering.py）。suppress() 中（A/B ハーネス等の読み取り専用経路）だけ記録しない。
    #      トークン列は NULLABLE が設計（NULL＝呼び出しはあったがプロバイダが usage を返さなかった
    #      「報告不能」マーカー、0＝プロバイダがゼロと報告）。
    """CREATE TABLE IF NOT EXISTS usage_events (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        kind TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT,
        input_tokens BIGINT,
        cached_input_tokens BIGINT,
        output_tokens BIGINT,
        reasoning_output_tokens BIGINT,
        calls INTEGER NOT NULL DEFAULT 1,
        user_id TEXT,
        world TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events (ts)",
    # 回答ごとの利用者フィードバック（👍/👎＋定型タグ＋任意の一言）。1利用者×1メッセージにつき
    # 最新1件のみ（再送は上書き）。本文（質問/回答）は複製せず message_id で messages を参照する
    # だけ（会話削除に CASCADE で追従）。
    """CREATE TABLE IF NOT EXISTS message_feedback (
        id SERIAL PRIMARY KEY,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        rating TEXT NOT NULL CHECK (rating IN ('up','down')),
        tags TEXT[] NOT NULL DEFAULT '{}',
        comment TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (message_id, user_id)
    )""",
]
# PERF-1（台帳#17）: usage_stats の期間絞り（`_usage_period_bounds`）が messages.created_at で
# 索引を使えるようにする。**契約の範囲**: messages 全体に対する線形の物理読取（Seq/Index Scanで
# テーブル全体を辿ること）自体は残り得る（Postgres がどのプランを選ぶかはデータ分布次第）。
# 本スライスが保証するのは、`_USAGE_TURN_CTE` の turn_no 累積カウント計算（`numbered` の
# WindowAgg・SUM(...) OVER(...) であり ROW_NUMBER() ではない）とその後段6集計
# （users/worlds/週次/token×3）へ**投入される行数**を、全 messages N 行から「期間内に触れた
# 会話」T 行へ削減すること（T は通常 N より小さいが、会話の活動が期間に集中していれば N に
# 近づき得る＝常に T ≪ N が保証されるわけではない）。この索引は `touched`（期間の候補
# メッセージを絞るサブクエリ）が使う（詳細は usage.py の `_USAGE_TURN_CTE` 直前のコメント参照）。
# `_SCHEMA` には含めない＝既存の大規模 messages では素の CREATE INDEX（ACCESS SHARE 超のロックを
# 伴い得る）が起動の単一トランザクション内で長時間かかると、readiness 未達のまま liveness
# タイムアウトでプロセスが強制終了→ロールバック→再起動を繰り返しかねない（DDL は毎起動全文再実行の
# ため索引未完了なら何度でも同じ問題を再現する）。`ensure_messages_created_at_index()` が別
# autocommit 接続で `CREATE INDEX CONCURRENTLY` を実行し、テーブルの読み書きをブロックしない
# （`init_schema()` はこの完了を待たない＝background daemon thread で fire-and-forget）。
_MESSAGES_CREATED_AT_INDEX = "idx_messages_created_at"
# pg_get_indexdef() の出力に含まれるはずの断片で「想定通りの単純索引か」を判定する簡易マーカー。
# 複合索引（例: (conversation_id, created_at)）や別列への索引は含まないため区別できる。
_MESSAGES_CREATED_AT_INDEXDEF_FRAGMENT = "(created_at)"

# 索引構築の直列化用鍵。schema 初期化（init_schema の advisory lock）と同じ鍵をまず
# 取得→即解放することで「他プロセスの schema DDL 実行中は索引構築を始めない」バリアにし、
# 続けて索引構築専用の鍵で `pg_try_advisory_lock` する（取れなければ何もせず return）。
_SCHEMA_LOCK_KEY = int.from_bytes(hashlib.sha1(f"schema:{_KB_ID}".encode("utf-8")).digest()[:8],
                                  "big", signed=True)
# 索引構築専用の鍵は pg_advisory_lock の**2引数形**（class_id, key）を使う。1引数形（bigint 1個）
# は world_lock/world_lock_shared/workspace_file_lock が `sha1(f"{_KB_ID}:...")` 等から
# 導出する鍵と同じ 64bit 空間を共有しており、文字列がたまたま一致すると衝突する
# （例: world_id="idx_messages_created_at" の world_lock と、1引数形で同じ文字列から
# 鍵を作った場合の索引ロックが同一キーになり、互いを不当にブロックしうる）。2引数形は
# Postgres 内部で1引数形と完全に別の名前空間を持つため、固定の小さい整数ペアで衝突なく
# 専用化できる（`tests/integration/test_schema_init_r5.py` の衝突なしテストで固定）。
_CREATED_AT_INDEX_LOCK_CLASSID = 0x50455246   # "PERF" の ASCII 値由来の固定クラス ID（他用途と重複しない専用値）
_CREATED_AT_INDEX_LOCK_KEY = 1


def ensure_messages_created_at_index() -> None:
    """`idx_messages_created_at` を CONCURRENTLY で作成する（別 autocommit 接続・非ブロッキング）。

    本アプリの展開は**単一 worker 前提**（既存の日次上限等と同じ前提）。複数プロセスが同時に
    呼ぶことを主な運用シナリオとして想定しないが、起動タイミングの重なりや手動再実行に備えて
    以下の2段の advisory lock だけは掛ける（厳密な全ケースの直列化までは保証しない＝前提は
    あくまで単一 worker であることの明記が主）:
      1. `_SCHEMA_LOCK_KEY`（schema 初期化と同じ鍵）を取得→即解放するバリア。他プロセスが
         `init_schema()` の DDL トランザクション実行中なら、その完了まで待ってから索引構築へ進む
         （初期化中プロセスと構築の同時進行を防ぐ）。
      2. `_CREATED_AT_INDEX_LOCK_CLASSID`/`_CREATED_AT_INDEX_LOCK_KEY`（2引数形）を
         `pg_try_advisory_lock` で取得できた場合のみ実際の検査・修復・作成を行う。取れなければ
         既に他プロセスが処理中（正常な CONCURRENTLY 構築中を含む）とみなし、警告ログを残して
         何もせず return する（`indisvalid=false` は構築中の正常な中間状態でもあるため、ロック
         無しで DROP すると他プロセスの構築中索引を誤って壊しうる）。次回の `init_schema()`
         呼び出し（プロセス再起動等）で自己修復される。

    CONCURRENTLY はテーブルへの読み書きをブロックしない（他のクエリ・ヘルスチェックを妨げない）が、
    トランザクションを使わないため構築失敗時に INVALID な索引が残り得る。上記ロックを保持した
    状態でのみ `pg_index.indisvalid` を確認し、INVALID なら `DROP INDEX CONCURRENTLY` してから
    作り直す（自己修復・冪等）。DB 不達等の例外は握り潰してログのみ（この索引が無くても
    usage_stats の結果自体は変わらず性能にのみ影響するため、fail-open が正しい）。
    """
    conn = None
    try:
        conn = psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row,
                               connect_timeout=_INIT_CONNECT_TIMEOUT)
        conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_KEY,))
        try:
            pass   # バリアのみ：他プロセスの schema DDL 完了を待つ（本体はロック解放後）。
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_KEY,))
            except Exception:
                pass

        got_lock = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS got",
            (_CREATED_AT_INDEX_LOCK_CLASSID, _CREATED_AT_INDEX_LOCK_KEY),
        ).fetchone()["got"]
        if not got_lock:
            # 他プロセスが処理中（正常な CONCURRENTLY 構築中を含む）。ここでは何もせず、
            # 次回の init_schema() 呼び出し（プロセス再起動等）での再試行に委ねる。
            _log.warning("%s の作成をスキップしました（他プロセスが処理中のため advisory lock を"
                        "取得できず）。次回起動時に再試行します。", _MESSAGES_CREATED_AT_INDEX)
            return
        try:
            row = conn.execute(
                "SELECT i.indisvalid, pg_get_indexdef(c.oid) AS indexdef "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = %s",
                (_MESSAGES_CREATED_AT_INDEX,),
            ).fetchone()
            if row is not None and not row["indisvalid"]:
                conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_MESSAGES_CREATED_AT_INDEX}")
                row = None
            if row is not None:
                # 有効な同名索引が既にある。定義が想定（messages(created_at)）と異なる場合
                # （手動作成・別目的の再利用等）は、誤って別索引を壊さないよう DROP/CREATE を
                # 一切行わず警告ログのみ残す（運用者の判断に委ねる契約）。
                if _MESSAGES_CREATED_AT_INDEXDEF_FRAGMENT not in (row["indexdef"] or ""):
                    _log.warning(
                        "%s という名前の索引が既に存在しますが、想定の定義（%s を含む）と"
                        "異なります（現在の定義: %s）。誤って別目的の索引を壊さないよう "
                        "DROP/CREATE をスキップしました。運用者による確認が必要です。",
                        _MESSAGES_CREATED_AT_INDEX, _MESSAGES_CREATED_AT_INDEXDEF_FRAGMENT,
                        row["indexdef"],
                    )
                return
            conn.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_MESSAGES_CREATED_AT_INDEX} "
                "ON messages (created_at)"
            )
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s, %s)",
                            (_CREATED_AT_INDEX_LOCK_CLASSID, _CREATED_AT_INDEX_LOCK_KEY))
            except Exception:
                pass
    except Exception as e:
        _log.warning("%s の作成に失敗しました（usage_stats の性能にのみ影響・次回起動時に"
                    "再試行します）: %s", _MESSAGES_CREATED_AT_INDEX, e)
    finally:
        if conn is not None:
            conn.close()


_created_at_index_thread_started = False


def _ensure_messages_created_at_index_background() -> None:
    """`ensure_messages_created_at_index()` を daemon スレッドで一度だけ起動する
    （プロセス生存期間で1回・`init_schema()` から毎回呼ばれても多重起動しない）。"""
    global _created_at_index_thread_started
    if _created_at_index_thread_started:
        return
    _created_at_index_thread_started = True
    threading.Thread(target=ensure_messages_created_at_index, daemon=True,
                     name="sherpa-idx-messages-created-at").start()


# R5: コード側スキーマの内容ハッシュ（`schema_version` スタンプに使う・記録専用）。
# tests/unit/test_store_surface.py の golden 算出式（sha256("\n".join(_SCHEMA))）と同一式。
_SCHEMA_HASH = hashlib.sha256("\n".join(_SCHEMA).encode("utf-8")).hexdigest()


_inited = False

# R5 RV HIGH（2026-07-15）: init_schema の接続確立タイムアウト（秒）。lifespan 起動時と
# /healthz の readiness リトライの双方から呼ばれるため、PG 不達で分級ブロックしない上限を置く。
_INIT_CONNECT_TIMEOUT = 5


def _dsn() -> str:
    dsn = os.environ.get("SHERPA_PG_DSN")
    if dsn:
        return dsn
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url
    # フォールバックの password は PGPASSWORD ＞ POSTGRES_PASSWORD ＞ 既定（2026-08-18）。docker-compose.yml が
    # PG のパスワードを POSTGRES_PASSWORD から取るため、同じ 1 変数で compose とアプリが揃う
    # （DATABASE_URL に焼き込まなくてよい＝ポート/パスワードは各 1 か所）。
    return "host={h} port={p} dbname={d} user={u} password={pw}".format(
        h=os.environ.get("PGHOST", "localhost"), p=os.environ.get("PGPORT", "5432"),
        d=os.environ.get("PGDATABASE", "sherpa"), u=os.environ.get("PGUSER", "sherpa"),
        pw=os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD") or "sherpa_dev")


# ---- PG コネクションプール（性能台帳#17 QW2）----
# `_connect()` は従来、呼び出しごとに `psycopg.connect()`（新規 TCP 接続＋認証ハンドシェイク）を
# 行っていた。チャット1ターンで10接続超・ポーリング（`/health/summary` 等）のたびにも1接続、が
# 支配的な固定費だったため、**引数無しの `_connect()`**（advisory/session-level lock を持たない
# 通常の CRUD 呼び出し・呼び出し元の大多数）だけをプールへ差し替える。
#
# `connect_timeout=`/`options=` 等の kwargs 付き呼び出し（残り時間予算ベースの接続確立
# タイムアウトや `statement_timeout` の一時上書きを行う少数の呼び出し元＝
# `init_schema`/`_read_system_settings_fresh`/`worlds.py`/`ingest.py`/`usage_events.py`/
# `api_keys.py` の call-count 集計）は、共有プールでは接続ごとに値を変えられない
# （`options`/`connect_timeout` はプール生成時に固定される psycopg 側の接続パラメータ）ため、
# 従来どおり ad-hoc な `psycopg.connect()` のまま残す——挙動・タイムアウト予算計算は完全不変。
#
# advisory lock を保持する `world_lock`/`world_lock_shared`/`world_registry_lock`/
# `workspace_file_lock`（すべて本ファイル内・上記参照）はそもそも `_connect()` を経由せず
# 専用の `psycopg.connect(..., autocommit=True)` を直接使っている（session-level lock を
# プールの使い回し接続に残すと解放後に別リクエストへ漏れるため、既存実装から変更しない）。
_PG_POOL_LOCK = threading.Lock()
_PG_POOL = None   # 型: psycopg_pool.ConnectionPool（遅延 import・未生成時は None）


def _make_pg_pool():
    from psycopg_pool import ConnectionPool

    min_size = max(0, int(os.environ.get("SHERPA_PG_POOL_MIN", "2")))
    max_size = max(min_size, int(os.environ.get("SHERPA_PG_POOL_MAX", "10")))
    # 取得タイムアウト（枯渇時は `psycopg_pool.PoolTimeout`＝`psycopg.errors.OperationalError` の
    # サブクラスを送出して打ち切る。黙って永久待ちしない・env で調整可）。
    acquire_timeout = float(os.environ.get("SHERPA_PG_POOL_TIMEOUT", "10"))
    pool = ConnectionPool(
        _dsn(),
        min_size=min_size,
        max_size=max_size,
        open=True,
        timeout=acquire_timeout,
        kwargs={"row_factory": dict_row},          # 既存 `_connect()` と同じ row_factory
        check=ConnectionPool.check_connection,      # PG 再起動等で腐った接続を払い出す前に検知し作り直す
    )
    atexit.register(_close_pg_pool_best_effort, pool)
    return pool


def _close_pg_pool_best_effort(pool) -> None:
    """プロセス終了時のクローズ（フォールバック）。

    lifespan（`sherpa.lifespan`）の shutdown 経路が通常のプロセス終了を担うが、テスト/CLI
    スクリプト/OCR worker（`python -m sherpa.ingest.ocr_worker`・別プロセス）等 lifespan を
    経由しない実行経路のための保険として `atexit` にも登録する（二重に呼ばれても
    `ConnectionPool.close()` は冪等）。
    """
    try:
        pool.close(timeout=5.0)
    except Exception:
        pass


def _get_pg_pool():
    """プロセス内シングルトンの PG プール（遅延生成・スレッドセーフ）。

    `_dsn()` は生成時に一度だけ読む。テスト用 DB 分離（`tests/conftest.py::_setup_test_pg_dsn`）は
    「どのテストファイルよりも先＝どの `_connect()` 呼び出しよりも先」に `SHERPA_PG_DSN` を
    書き換えるため、プールの遅延生成（初回の引数無し `_connect()` 呼び出し時）はその後になり
    安全（`sherpa_test`/レーン別 DSN を正しく捕まえる）。**プロセス起動後に `SHERPA_PG_DSN` を
    書き換えて同一プロセス内で接続先を切り替える経路は現状存在しない**——将来そのような経路を
    足す場合、本関数のシングルトンはそれに追随しない（明示的なプール再生成が必要）ことに注意。
    """
    global _PG_POOL
    if _PG_POOL is not None:
        return _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is None:
            _PG_POOL = _make_pg_pool()
    return _PG_POOL


def close_pg_pool(timeout: float = 5.0) -> None:
    """PG プールを閉じる（`sherpa.lifespan` の shutdown から直接 import して呼ぶ想定・
    `from sherpa.store import db as store_db; store_db.close_pg_pool()`）。

    未生成（一度も `_connect()` が呼ばれていない）なら何もしない。多重呼び出し・`atexit` 経由の
    二重クローズ（`_close_pg_pool_best_effort` 参照）は安全（`ConnectionPool.close()` は冪等）。
    呼び出し後に `_connect()` が再度呼ばれれば新しいプールを遅延生成する（テストでの明示的な
    プール作り直しにも使える）。
    """
    global _PG_POOL
    with _PG_POOL_LOCK:
        pool, _PG_POOL = _PG_POOL, None
    if pool is not None:
        try:
            pool.close(timeout=timeout)
        except Exception:
            _log.warning("PG プールのクローズに失敗しました（プロセス終了時のベストエフォート）", exc_info=True)


class PooledConnectionReleasedError(RuntimeError):
    """既にプールへ返却済みの `_PooledConnection` を使おうとした（呼び出し側のバグ）。

    `_release()`（with 終了時／`close()` 呼び出し時）の後は物理接続への参照を切るため、
    それ以降このラッパー経由で属性アクセスすると必ずこの例外になる——その物理接続は既に
    別のリクエスト/スレッドへ貸し出されている可能性があり、エラーにせず黙って通すと
    そちらの接続へ意図せず SQL を流してしまう「静かな成功」になり得るため。
    """


class _PooledConnection:
    """`_connect()`（引数無し）の返り値ラッパー。

    `with _connect() as c:`（大多数の呼び出し元）は `c` を実接続（`psycopg.Connection`）に
    束縛し、commit/rollback は従来と完全に同一（`Connection.__enter__`/`__exit__` そのものを
    委譲する）。違いはブロックを抜けた後——ソケットを閉じる代わりにプールへ返却する。

    一部のテスト（advisory lock の直接検証・例: `test_system_settings.py`/
    `test_bedrock_settings.py` の `holder_conn = store._connect(); ...; holder_conn.close()`）は
    `with` を使わず素の返り値へ直接 `.execute()`/`.commit()`/`.close()` を呼ぶ——`__getattr__` で
    実接続へ委譲しつつ `.close()` だけ横取りしてプールへの返却に置き換える（素の
    `Connection.close()` はプール由来の接続でも物理クローズしてしまい、プール側は
    `putconn()` を経由しない限りその1本を恒久的に「貸出中」のまま失う＝実質的な枯渇を招くため）。
    """

    __slots__ = ("_pool", "_conn", "_released")

    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._released = False

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._release()

    def close(self) -> None:
        self._release()

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        conn, self._conn = self._conn, None   # RV代替 M2: 参照を切る（use-after-release を検出可能にする）
        self._pool.putconn(conn)

    def __getattr__(self, name):
        if self._conn is None:
            # RV代替 M2: 返却済みの物理接続は既に別リクエストへ貸し出されている可能性がある。
            # 黙って `getattr` を通すと、その別リクエストの接続へ意図せず SQL を流してしまう
            # 「静かな成功」になり得るため、ここで明示的に拒否する。
            raise PooledConnectionReleasedError(
                f"このプール接続は既に返却済みです（close()/with 終了後は再利用不可）: attribute={name!r}")
        return getattr(self._conn, name)


def _connect(**kw):
    if not kw:
        pool = _get_pg_pool()
        return _PooledConnection(pool, pool.getconn())
    # kwargs 付き（`connect_timeout=`/`options=`）＝プール非対応の特殊呼び出し。従来どおり ad-hoc 接続。
    return psycopg.connect(_dsn(), row_factory=dict_row, **kw)


def _world_lock_key(world_id) -> int:
    """`world_lock`/`world_lock_shared` 共通のキー導出（同じ world_id は必ず同じ鍵になる＝
    排他ロックと共有ロックが同じ対象を取り合う）。"""
    return int.from_bytes(hashlib.sha1(f"{_KB_ID}:{world_id}".encode("utf-8")).digest()[:8],
                          "big", signed=True)


@contextlib.contextmanager
def world_lock(world_id, *, timeout_ms: int | None = None):
    """world 単位の Postgres advisory lock（**排他**・取り込み/削除/rebind を直列化）。

    ポーリング・手動 refresh・register/rebind/delete が同じ world に同時実行しても、Neo4j の delete+load と
    台帳入替が別々のスナップショットで混ざらないようにする（RV High#1）。複数 worker/プロセスでも有効。
    `world_lock_shared`（読み取り専用処理向け・共有ロック）と同じ鍵を使うため、この排他ロックは
    保持中の共有ロック全てが解放されるまで（＝共有ロックの全保持者も、この排他ロックが解放される
    まで）互いに待ち合う——PostgreSQL の advisory lock は同一キーで排他/共有の両モードを提供する。

    `timeout_ms`（省略可）: 取得できないまま指定ミリ秒を超えたら `psycopg.errors.LockNotAvailable`
    を送出する（`world_lock_shared` と同じ挙動）。省略時（既定）は無制限に待つ——既存の
    呼び出し元（register/rebind/sync/wipe 等・取り込みそのものの直列化）の挙動は変えない。
    HTTP 経由の対話的操作（例: 手動 `/extract`）が長時間ブロックせず 409/503 を即座に返せる
    ようにする用途で使う。
    """
    _ensure()
    key = _world_lock_key(world_id)
    conn = psycopg.connect(_dsn(), autocommit=True)            # session-level lock（tx に縛らない）
    try:
        if timeout_ms is not None:
            conn.execute(f"SET lock_timeout = '{max(1, int(timeout_ms))}ms'")
        conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        yield
    finally:
        # RV HIGH（2026-07-14 R3）: unlock は **best-effort**。PG 断で unlock が例外を投げると、それが
        # `with` 本体の元例外を置換して隠す。session-level advisory lock は**接続 close で必ず解放される**
        # ため、unlock 失敗を握り潰しても lock は残らない（close が確実に効く）。
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            pass
        finally:
            conn.close()


@contextlib.contextmanager
def world_lock_shared(world_id, *, timeout_ms: int | None = None, connect_timeout: float | None = None):
    """world 単位の Postgres advisory lock（**共有**・読み取り専用処理向け）。

    `world_lock`（排他）と同じ鍵（`_world_lock_key`）を使う `pg_advisory_lock_shared` で取得する。
    この共有ロックの保持者同士は互いに待たない（並行実行可）が、`world_lock`（rebind/削除/
    取り込み）とは相互排他になる——PART-4（`sherpa/research_service.py`）が検索実行中に rebind と
    競合して原本 root と派生物/索引の世代が食い違う（TOCTOU）ことを防ぐために使う。

    `timeout_ms`（省略可）: 取得できないまま指定ミリ秒を超えたら
    `psycopg.errors.LockNotAvailable` を送出する。省略時は無制限に待つ（`world_lock` と
    同じ既定挙動）。**接続の確立にかかった時間ぶんを差し引いた残りだけ**をロック待ちへ渡す
    （`SET lock_timeout` を接続確立**後**に発行する＝`connect_timeout` での待ちと `lock_timeout`
    での待ちを合算で 2 回消費しない）。差引後の残りが 1ms 未満になっても 0（Postgres では
    `lock_timeout=0` は「無効化＝無制限待ち」を意味し逆効果）にはせず、最小 1ms へクランプして
    ほぼ即座に `LockNotAvailable` を送出させる。

    `connect_timeout`（省略可・秒）: `lock_timeout` は接続**確立後**にしか効かないため、DB自体が
    重い/疎通不能なときは `psycopg.connect()` 自体が OS の TCP タイムアウト（`lock_timeout` より
    大幅に長いことがある）まで無応答になりうる。呼び出し元（PART-4 の残り時間予算）が
    `connect_timeout` を渡せば `psycopg.connect(connect_timeout=...)` へ渡す（省略時は psycopg の
    既定＝無制限に近い OS 既定値のまま）。psycopg 3.3.4 は `connect_timeout` を整数秒でしか扱わず
    小数部を切り捨てる——`0.5〜0.9` のような1未満の値は 0（libpq は「無制限」＝実測で Linux の
    TCP 再送タイムアウト約130秒相当まで無応答になりうる）に、`1.x` は libpq 側の最小値2秒に
    それぞれ丸まる。呼び出し元が渡した秒数は**整数秒へ切り上げ・最小1秒でクランプ**してから渡す
    （1秒未満に切り上げても、上記の libpq 側の最小2秒への底上げで吸収される範囲）。
    """
    _ensure()
    key = _world_lock_key(world_id)
    connect_kwargs = {"autocommit": True}
    if connect_timeout is not None:
        connect_kwargs["connect_timeout"] = max(1, math.ceil(connect_timeout))
    connect_started = time.monotonic()
    conn = psycopg.connect(_dsn(), **connect_kwargs)
    try:
        if timeout_ms is not None:
            elapsed_ms = (time.monotonic() - connect_started) * 1000
            remaining_ms = max(1, int(timeout_ms - elapsed_ms))
            # SET は utility 文でバインドパラメータを受け付けないため、内部計算済みの int を
            # 直接埋め込む（外部入力ではないため injection の懸念はない）。
            conn.execute(f"SET lock_timeout = '{remaining_ms}ms'")
        conn.execute("SELECT pg_advisory_lock_shared(%s)", (key,))
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock_shared(%s)", (key,))
        except Exception:
            pass
        finally:
            conn.close()


# `world_registry_lock` 専用の鍵は `_world_lock_key`（1引数形・世界IDの sha1 から導出）とは
# 完全に別の名前空間（2引数形＝classid+key）を使う——world_id="world-registry"（入力規則上は
# 有効な識別子）が登録された場合に、1引数形だと `world_lock("world-registry")`/
# `world_lock_shared("world-registry")` と**同じ鍵**になり、個別 world の取り込み/検索が
# グローバルな登録直列化ロックと不当に競合する自己衝突を避けるため。2引数形は Postgres 内部で
# 1引数形と別空間のため、`_CREATED_AT_INDEX_LOCK_CLASSID`（`ensure_messages_created_at_index`）
# と同じ手口で専用化する。
_WORLD_REGISTRY_LOCK_CLASSID = 0x52454757   # "REGW" の ASCII 値由来の固定クラス ID（他用途と重複しない専用値）
_WORLD_REGISTRY_LOCK_KEY = 1


@contextlib.contextmanager
def world_registry_lock():
    """World 新規登録を全体で直列化する固定 Postgres advisory lock。

    `world_lock(world_id)` は異なる world_id 同士では別の鍵になるため、登録件数の確認から行作成までを
    保護できない。この固定鍵を先に取得し、その内側で `world_lock` を取得することで、標準MVPの
    「登録元フォルダは全体で1本」をプロセスをまたいで原子的に守る。

    呼び出し側の lock 順序は必ず ``world_registry_lock -> world_lock`` とする。逆順の経路は作らない。
    """
    _ensure()
    conn = psycopg.connect(_dsn(), autocommit=True)            # session-level lock（world_lock と同じ方式）
    try:
        conn.execute("SELECT pg_advisory_lock(%s, %s)",
                     (_WORLD_REGISTRY_LOCK_CLASSID, _WORLD_REGISTRY_LOCK_KEY))
        yield
    finally:
        # world_lock と同じ理由で unlock は best-effort（session-level lock は接続 close で必ず解放される）。
        try:
            conn.execute("SELECT pg_advisory_unlock(%s, %s)",
                         (_WORLD_REGISTRY_LOCK_CLASSID, _WORLD_REGISTRY_LOCK_KEY))
        except Exception:
            pass
        finally:
            conn.close()


@contextlib.contextmanager
def workspace_file_lock(uid: str, rel_path: str):
    """(uid, rel_path) 単位の Postgres advisory lock（upload と sweep を直列化）。

    upload と sweep が同じ物理パスを同時に操作する TOCTOU を排除する。
    world_lock と同じパターン（session-level・autocommit・複数プロセスで有効）。
    """
    _ensure()
    key = int.from_bytes(
        hashlib.sha1(f"wsfile:{uid}:{rel_path}".encode("utf-8")).digest()[:8],
        "big", signed=True,
    )
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        yield
    finally:
        try:                                                   # unlock は best-effort（world_lock と同じ理由）
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            pass
        finally:
            conn.close()


class ClientOpIdIndexUnexpectedDefinitionError(Exception):
    """`api_keys_client_op_id_unique` が既知のどの定義（現行=`lower()`／旧来=大小文字を区別）
    とも構造的に一致しない（非UNIQUE・invalid・複合キー・想定外の式・predicate 不一致 等）。

    fail-closed: このケースでは DROP を一切行わず、例外を送出して起動を止める。索引名だけを
    手がかりに無条件で作り直すと、運用者が意図して加えた別の制約（複合ユニーク化・別条件の
    部分インデックス 等）を気付かないまま壊しかねないため、機械的な自動解消はしない。
    """


def _resolve_api_keys_schema(conn) -> str:
    """`api_keys` 実表が現在属するスキーマ名を、`'api_keys'::regclass` と同じ search_path
    解決規則で求める。索引の分類（`_classify_client_op_id_index`）・`DROP INDEX` のスキーマ
    限定（`_migrate_client_op_id_unique_index`）の両方から呼ぶ共通ヘルパー（indrelid 経由の
    表比較と別に、索引探索そのものをこのスキーマへ絞り込むため）。
    """
    row = conn.execute(
        "SELECT n.nspname AS nspname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = 'api_keys'::regclass"
    ).fetchone()
    return row["nspname"]


def _classify_client_op_id_index(conn) -> str:
    """`api_keys_client_op_id_unique` の現在の定義を `pg_index` の構造的な属性
    （`indisunique`/`indisvalid`・対象表（`indrelid`）・キー列数（`indnkeyatts`）／総列数
    （`indnatts`）・キー式・部分インデックスの predicate）で分類する。`indexdef` 文字列の
    部分一致には頼らない（表記ゆれ・別の式にたまたま同じ部分文字列が含まれる場合に脆弱なため）。

    索引の検索は、`_resolve_api_keys_schema()` で解決した「`api_keys` 実表が実際に属する
    スキーマ」に**限定**して**名前だけ**で行う（`WHERE` 句に `indrelid` 条件そのものは
    混ぜない）。この2段構え（namespace で候補を絞る・indrelid は Python 側で比較する）には
    それぞれ別の理由がある。

    (a) namespace 限定が要る理由: 索引名はスキーマ内で一意だが、DB 全体では一意ではない
    （別スキーマに同名の索引が複数存在しうる）。`WHERE ic.relname = '...'`（namespace 条件
    無し）のまま `fetchone()` すると、`ORDER BY` が無いため複数該当時にどの行が返るかは
    不定——`search_path` の先頭（`api_keys` を含まない）別スキーマに、たまたま同名の索引が
    別表に存在すると、そちらの行を拾って本来 `"current"`/`"legacy"` であるべきところを
    誤って `"unknown"` と判定し、実際には無害な変更検知で起動を止めかねない。`api_keys`
    自身が属するスキーマへ先に絞ることで、この不定性を排除する。

    (b) それでも `indrelid` の比較を **Python 側**に残す理由: 索引名はスキーマ内で一意だが
    「対象表の名前に索引名が対応している」保証は無い——`api_keys` と同じスキーマに存在する
    **別の表**が、たまたま `api_keys_client_op_id_unique` という名前の索引を持つケースは
    あり得る（この場合は namespace 限定だけでは弾けない）。ここで `WHERE` 句にまとめて
    `indrelid = 'api_keys'::regclass` まで書いてしまうと、その別表の同名索引の行ごと除外され
    `"absent"`（＝新規作成してよい）と区別が付かなくなる——`"absent"` 扱いにして
    `CREATE UNIQUE INDEX api_keys_client_op_id_unique ON api_keys(...)` を発行すると、
    Postgres の「同名オブジェクトが既に存在する」という生のエラーで不可解に落ちる。ここでは
    その状態を明示的に `"unknown"` として検出し、fail-closed の分かりやすいメッセージで
    止める。`ns.nspname = current_schema()` のような固定スキーマ名フィルタも使わない——
    `search_path` の先頭に（`api_keys` を含まない）別スキーマがある構成では
    `current_schema()` が `api_keys` の実際のスキーマと一致しないことがあり、正しい現行定義を
    見落としかねないため（`_resolve_api_keys_schema()` は常に search_path 解決済みの実スキーマ
    を指す）。

    `indnatts == indnkeyatts == 1` を要求する（`indnkeyatts` だけでは INCLUDE 列を見逃す——
    `CREATE UNIQUE INDEX ... (lower(client_op_id)) INCLUDE (id)` は `indnkeyatts=1` だが
    `indnatts=2` になり、ストレージ上は追加の列を持つ別物の索引になる。これは "unknown" に
    倒す）。

    戻り値:
      - `"absent"`: `api_keys` のスキーマ内にこの名前の索引が存在しない（初回起動）。
      - `"current"`: 正しい現行定義（`api_keys` 自身の索引・非NULL部分・UNIQUE・valid・
        単一キー列・INCLUDE 列無し・キー式が `lower(client_op_id)`）。
      - `"legacy"`: 既知の旧定義（`api_keys` 自身の索引・非NULL部分・UNIQUE・valid・
        単一キー列・INCLUDE 列無し・キーが `client_op_id` そのまま＝大小文字を区別する）。
      - `"unknown"`: 上記のいずれでもない——同じスキーマ内の別表に同名の索引がある場合を含む
        （非UNIQUE・invalid・複合キー・INCLUDE 列・別の式・predicate が無い/違う 等）。
    """
    target_schema = _resolve_api_keys_schema(conn)
    row = conn.execute(
        "SELECT ix.indrelid AS table_oid, ix.indisunique AS is_unique, "
        "  ix.indisvalid AS is_valid, ix.indnkeyatts AS nkeyatts, ix.indnatts AS natts, "
        "  pg_get_expr(ix.indpred, ix.indrelid) AS predicate, "
        "  pg_get_indexdef(ix.indexrelid, 1, true) AS key1_def "
        "FROM pg_index ix "
        "JOIN pg_class ic ON ic.oid = ix.indexrelid "
        "JOIN pg_namespace ns ON ns.oid = ic.relnamespace "
        "WHERE ic.relname = 'api_keys_client_op_id_unique' AND ns.nspname = %s",
        (target_schema,),
    ).fetchone()
    if row is None:
        return "absent"
    api_keys_oid = conn.execute("SELECT 'api_keys'::regclass::oid AS oid").fetchone()["oid"]
    if row["table_oid"] != api_keys_oid:
        return "unknown"   # 別表の同名索引（api_keys 自身の索引は存在しない状態）。
    predicate_norm = re.sub(r"\s+", "", (row["predicate"] or "").lower())
    key1_norm = re.sub(r"\s+", "", (row["key1_def"] or "").lower())
    structurally_ok = (
        bool(row["is_unique"]) and bool(row["is_valid"])
        and row["nkeyatts"] == 1 and row["natts"] == 1
        and predicate_norm in ("(client_op_idisnotnull)", "client_op_idisnotnull")
    )
    if not structurally_ok:
        return "unknown"
    if key1_norm == "lower(client_op_id)":
        return "current"
    if key1_norm == "client_op_id":
        return "legacy"
    return "unknown"


def _migrate_client_op_id_unique_index(conn) -> list[str]:
    """`api_keys_client_op_id_unique` を大小文字を区別しない（`lower()`）定義へ移行する。

    `_SCHEMA` の他の DDL 文字列と違い、この索引は**単純な冪等 DDL では表現しない**——
    `_classify_client_op_id_index()`（上記）で現在の実際の定義を構造的に分類し、次のいずれか
    だけを行う（毎起動の DROP→再作成はしない・データを伴う移行を伴うため）:
      1. `"absent"`（初回起動）: 新定義（`lower()`）でそのまま作成する。
      2. `"current"`: 何もしない（再構築コストを避ける）。
      3. `"legacy"`: **一度きり**の移行。先に
         `GROUP BY lower(client_op_id) HAVING count(*) > 1` で大小文字違いの重複
         （旧索引の下では別々の値として登録できてしまっていた行）を検査し、見つかれば
         新しい方（id が大きい方）の行の `client_op_id` に決定的なサフィックス
         （`-dup-<id>`）を付けて退避してから索引を張り替える。古い方の行はそのままにする
         （元のトークン・監査履歴・回復 API からの「予約」を保つ）。退避した行は**自動失効
         しない**——キー自体は引き続き有効なまま、`client_op_id` による曖昧回復の対象から
         外れるだけ。
      4. `"unknown"`: `ClientOpIdIndexUnexpectedDefinitionError` を送出する（fail-closed・
         DROP しない）。

    `"legacy"` の `DROP INDEX` はスキーマ修飾する（`_resolve_api_keys_schema()` で解決した
    `api_keys` 自身のスキーマを明示）。非修飾の `DROP INDEX api_keys_client_op_id_unique` は
    search_path 解決に依存し、`_classify_client_op_id_index()` が正しく `"legacy"` と分類した
    後でも、search_path の先頭に（`api_keys` を含まない）別スキーマがあってそこに同名の索引
    （別表のもの）が存在すると、そちらを誤って DROP してしまう恐れがある——分類で見た索引と
    実際に DROP する索引を必ず同一のものにするため、スキーマを明示する。

    戻り値: 退避を行った場合のログメッセージ一覧（空リスト＝退避なし）。**ここではログを
    出さない**——呼び出し側（`init_schema()`）が、この関数を含むトランザクション全体が実際に
    commit された後にだけログする。この関数の途中（特に末尾の DROP/CREATE INDEX）で例外が
    起きて呼び出し元の `with _connect()` ブロックが rollback された場合、退避 UPDATE 自体も
    取り消される——ここで即座に `_log.error(...)` していると、実際には起きなかった（後で
    取り消された）変更について「退避に成功した」という断定的なログが残ってしまう。
    """
    shape = _classify_client_op_id_index(conn)
    if shape == "current":
        return []
    if shape == "unknown":
        raise ClientOpIdIndexUnexpectedDefinitionError(
            "api_keys_client_op_id_unique の定義が既知のいずれの形（現行=lower()／旧来=大小文字"
            "を区別）とも一致しません。DROP は行わず起動を止めます。実際の定義を確認し（"
            "SELECT indexdef FROM pg_indexes WHERE indexname='api_keys_client_op_id_unique'）、"
            "手動で解消してから再起動してください。")
    if shape == "absent":
        conn.execute(
            "CREATE UNIQUE INDEX api_keys_client_op_id_unique "
            "ON api_keys(lower(client_op_id)) WHERE client_op_id IS NOT NULL")
        return []
    assert shape == "legacy"
    dups = conn.execute(
        "SELECT lower(client_op_id) AS lc, array_agg(id ORDER BY id) AS ids "
        "FROM api_keys WHERE client_op_id IS NOT NULL "
        "GROUP BY lower(client_op_id) HAVING count(*) > 1"
    ).fetchall()
    messages: list[str] = []
    for d in dups:
        ids = d["ids"]
        keep_id = ids[0]
        for dup_id in ids[1:]:
            suffix = f"-dup-{dup_id}"
            conn.execute(
                "UPDATE api_keys SET client_op_id = client_op_id || %s WHERE id=%s",
                (suffix, dup_id))
            messages.append(
                f"api_keys.client_op_id の大小文字違い重複を検出（lower={d['lc']}）: "
                f"id={keep_id} を保持、id={dup_id} の client_op_id へ {suffix!r} を付与して"
                "退避しました（自動失効はしていません・監査履歴とキー自体は無傷・回復 API から"
                "の照合対象からは外れるため、運用者は該当キーの実際の所有者/用途を確認して"
                "ください）。")
    target_schema = _resolve_api_keys_schema(conn)
    conn.execute(
        sql.SQL("DROP INDEX {}.api_keys_client_op_id_unique")
        .format(sql.Identifier(target_schema)))
    conn.execute(
        "CREATE UNIQUE INDEX api_keys_client_op_id_unique "
        "ON api_keys(lower(client_op_id)) WHERE client_op_id IS NOT NULL")
    return messages


def init_schema(*, connect_timeout: float | None = None) -> None:
    """会話/メッセージ表を冪等作成（初回のみ実行）。

    R5（2026-07-13-横断レビュー対応.md §3）: 複数プロセス同時起動時の DDL 競合（deadlock の
    蓋然性源）を防ぐため、DDL 全文実行を `pg_advisory_lock` で直列化する（`world_lock` と同型＝
    固定キー・別 autocommit 接続・unlock は best-effort。session-level advisory lock は接続 close
    で必ず解放されるため unlock 失敗を握り潰しても lock は残らない）。
    **`_SCHEMA` の DDL 自体は毎起動・全文冪等実行を維持する**（適用済みスキップはしない＝
    自己修復性を保つ・§2 の決定）。`_migrate_client_op_id_unique_index()`（下記）だけは例外——
    データの移行（重複の退避）を伴うため、既に正しい定義なら実行をスキップする（毎起動の
    無条件 DROP→再作成はしない）。ロック内で DDL 実行後、`schema_version` に**記録専用**の
    スタンプを打つ（コード側スキーマのハッシュ＋適用時刻。DDL 実行の可否判断には使わない＝
    読み手は運用者のみでコードのどこからも読まない。直近行のハッシュと同じなら INSERT しない＝
    再起動毎に行が増え続けない）。

    `connect_timeout`（省略可・既定 None＝`_INIT_CONNECT_TIMEOUT`＝固定5秒を使う・既存呼び出し元は
    無変更）: `_ensure()` 経由で呼び出し元（PART-4 の `get_world`/`get_system_settings`/
    `add_usage_event` 等・残り時間ベースで渡す）が独自の接続タイムアウトを要求する場合に使う——
    未初期化（`_inited=False`）のまま呼ばれると、本来 bound したい接続確立の前に本関数の
    schema 初期化（advisory lock 待ち・DDL 全文実行）が挟まり、呼び出し元の予算を丸ごと迂回して
    しまうため。DDL 実行自体（`statement_timeout`）は従来どおり無期限のまま（上記の理由＝遅い
    ディスクで正当に時間がかかりうるため・ここは変更しない）——bound するのは接続確立の待ち時間
    だけ。`world_lock_shared` 等と同じ理由で整数秒へ切り上げ・最小1秒でクランプする。

    本関数は**2回**接続する（lock 用・DDL 用）——`connect_timeout` 指定時は1つの絶対期限を共有し、
    DDL 用接続には lock 用接続の確立＋`pg_advisory_lock` 待ちで経過した分を差し引いた**残り**を
    渡す（同じ満額をそのまま2回使うと、lock 取得自体に時間がかかった上で DDL 用接続へも満額
    再付与され、合計の実時間が最大で約2倍まで伸びうる・`metering.record`/`store.db.
    world_lock_shared` 等と同型の是正）。差し引いた残りが0以下なら、最低1秒へクランプして
    DDL 用接続を開始することはせず、`TimeoutError` を送出して接続自体を開始しない
    （advisory lock の unlock/close は `finally` で通常どおり行う）。
    """
    global _inited
    absolute_deadline = (time.monotonic() + connect_timeout) if connect_timeout is not None else None
    ct = max(1, math.ceil(connect_timeout)) if connect_timeout is not None else _INIT_CONNECT_TIMEOUT
    key = int.from_bytes(hashlib.sha1(f"schema:{_KB_ID}".encode("utf-8")).digest()[:8],
                         "big", signed=True)
    # RV HIGH（2026-07-15）: PG がブラックホール（SYN timeout・DNS stall）だと接続待ちが分級になり、
    # lifespan は try/except に入る前にブロック＝「DB 不達でも起動を止めない」が実効を失う。未認証の
    # /healthz からも呼ばれるため接続確立にのみ上限を付ける（health.py の _ping_postgres と同方針。
    # statement_timeout は付けない＝DDL 全文実行は遅いディスクで正当に時間がかかりうる）。
    lock_conn = psycopg.connect(_dsn(), autocommit=True,   # session-level lock（tx に縛らない・world_lock と同じ理由）
                                connect_timeout=ct)
    dup_messages: list[str] = []
    try:
        lock_conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        if absolute_deadline is not None:
            remaining = absolute_deadline - time.monotonic()
            if remaining <= 0:
                # lock 用接続の確立＋`pg_advisory_lock` 待ちだけで予算を使い切った——最低1秒へ
                # クランプして DDL 用の新規接続を試みない（`finally` は通常どおり実行され、
                # lock_conn の unlock/close は行われる）。
                raise TimeoutError("init_schema: budget exhausted before DDL connection")
            ct = max(1, math.ceil(remaining))
        with _connect(connect_timeout=ct) as c:
            for stmt in _SCHEMA:
                c.execute(stmt)
            dup_messages = _migrate_client_op_id_unique_index(c)
            row = c.execute(
                "SELECT schema_hash FROM schema_version ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None or row["schema_hash"] != _SCHEMA_HASH:
                c.execute("INSERT INTO schema_version (schema_hash) VALUES (%s)", (_SCHEMA_HASH,))
        # `with` を正常に抜けた（commit 成功）後にだけログする——`_migrate_client_op_id_unique_index`
        # の docstring 参照（rollback 時に断定的な成功ログを残さないため、ここまで遅延させる）。
        for msg in dup_messages:
            _log.error(msg)
    finally:
        # unlock は best-effort（world_lock と同じ理由・上の docstring 参照）。
        try:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            pass
        finally:
            lock_conn.close()
    _inited = True
    # PERF-1: readiness をブロックしない別経路で idx_messages_created_at を構築する（上の
    # ensure_messages_created_at_index() docstring 参照）。DB 不達等は関数内で握り潰し済み。
    _ensure_messages_created_at_index_background()


def schema_ready() -> bool:
    """スキーマ適用が完了しているか（`_inited` を返すだけ）。

    R5: `/healthz` の readiness 判定に使う（liveness→readiness 化）。
    """
    return _inited


def _ensure(connect_timeout: float | None = None) -> None:
    """未初期化（`_inited=False`）なら `init_schema()` を実行する。

    `connect_timeout`（省略可）: 未初期化時に `init_schema()` へそのまま転送する（`init_schema`
    docstring 参照・PART-4 が残り時間ベースで渡す）。既に初期化済みなら本関数は何もしないため
    無関係（無視される）。
    """
    if not _inited:
        init_schema(connect_timeout=connect_timeout)

