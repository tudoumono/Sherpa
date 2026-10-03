"""store の基盤: DSN・接続・advisory lock・スキーマ初期化。
`_KB_ID`・`_SCHEMA`（DDL）・`_inited`・`_dsn`・`_connect`・`init_schema`・`_ensure`・`world_lock`・`workspace_file_lock` を持つ。`_SCHEMA` の内容は `tests/unit/test_store_surface.py` の golden ハッシュで固定されている。
`init_schema` は `pg_advisory_lock` で直列化され、記録専用の `schema_version` 表と readiness 判定用の `schema_ready()` を持つ。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
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

# `system_extras.py`/`ext_api.py` 等と共有するロガー。
_log = logging.getLogger("sherpa")

# 単一 KB 前提。SQL バインドはこの定数に固定する。DB カラム `kb_id`（DEFAULT 'global'）は既存データのため残す。
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
    # ピン止め。既存テーブルにも冪等で列追加する。
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT false",
    # 文書台帳。世代内パス scope_path と層 layer を持つ。
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
    # `GET /documents` の台帳高速経路が実走査せずに重要度を返せるよう、ingest 時（`ingest/worker.py::_ledger_rows`）に 1 回だけ解決して持つ列（無ければ 3 列とも NULL）。
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS importance TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS importance_reason TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS importance_source TEXT",
    # world レジストリ（world_id → 参照元 root_path の 1:1 バインド）。参照先変更（rebind）はこの行を更新し、その world の派生物を全削除して再ミラーする（worlds.rebind）。
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
    # 変更検知用の署名（フォルダ内容のハッシュ）と最終同期時刻。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_sig TEXT",
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ",
    # 最後に取り込んだ時点のファイル明細（rel→[mtime_ns,ctime_ns,size]）。差分チェックの基準。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_manifest JSONB",
    # 成功確定した取り込みが最後に数えた doctype 対応原本の件数（`worker._run_locked` の成功パスのみ更新）。`/ext/v1/capabilities` が走査せず返すための事前集計値。NULL＝未確定。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_doc_count INTEGER",
    # 取り込み集計（`corpus_docs.scan_report()`）のキャッシュ。`GET /worlds/{wid}/status` がフォルダを歩かないための置き場で、`last_doc_count` とは別列にして、直近の run が失敗でも直前に成功した集計を表示し続ける。NULL＝未集計（一度も成功同期/再集計（`POST /worlds/{wid}/recount`）をしていない）。
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_scan_report JSONB",
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS last_scan_report_at TIMESTAMPTZ",
    # 取り込み・抽出の run 記録（ingest_runs）。グラフ反映境界を run 単位で持つ。version は documents.version と同じ自由文字列。共通 run は version=NULL＋layer=common。
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
    # `store.ingest.get_latest_run_summary`/`get_latest_published_run_summary` が `ORDER BY id DESC LIMIT 1` で最新 1 件を引くための索引。反映済み run 用は部分索引（`published_at IS NOT NULL`）で小さく保つ。
    "CREATE INDEX IF NOT EXISTS run_ver_id ON ingest_runs(kb_id, version, id DESC)",
    "CREATE INDEX IF NOT EXISTS run_ver_published ON ingest_runs(kb_id, version, id DESC) "
    "WHERE published_at IS NOT NULL",
    # 実行中 run の逐次進捗（段＋done/total/更新時刻）。`status='extracting'` の間だけ意味を持ち、完了時（`finish_ingest_run`）に NULL へ戻す。
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS progress JSONB",
    # `store.ingest.get_latest_es_run_summary` 専用の部分索引（`extraction_snapshot` に es キーがある run だけ）。
    "CREATE INDEX IF NOT EXISTS run_ver_published_es ON ingest_runs(kb_id, version, id DESC) "
    "WHERE published_at IS NOT NULL AND extraction_snapshot ? 'es'",
    # 取り込み元の登録（ingest_sources）。大量取り込みのスキャン元で、常時 watch しない。
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
    # 既存の古い documents 表にも列を冪等追加する。
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
        openai_api_key TEXT,
        ollama_url TEXT NOT NULL DEFAULT 'http://localhost:11434',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS gemini_api_key TEXT",
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS bedrock_api_key TEXT",
    # Codex CLI がどのモデル提供元へ接続するか（openai / ollama）。Codex 構成のときだけ意味を持つ。''＝openai として扱う。
    "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS codex_model_provider TEXT NOT NULL DEFAULT ''",
    # 新規利用者の既定を `heuristic` ではなく既定構成にする（`CREATE TABLE IF NOT EXISTS` は既存 DB の DEFAULT を変えないため明示的に直す）。既存行は書き換えない（「AIなし」を選んだ利用者を AI 回答へ移さないため）。
    "ALTER TABLE user_settings ALTER COLUMN agent SET DEFAULT 'codex'",
    # Ollama 接続先の列 DEFAULT を空文字（未設定＝中央既定に従う）へ変更する。既存行は書き換えない。
    "ALTER TABLE user_settings ALTER COLUMN ollama_url SET DEFAULT ''",
    # 画面に出ない個人設定の列（実行経路が読まないもの）は起動時に撤去する。
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS codex_reasoning",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS codex_model",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS codex_web_search",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS openai_model",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS ollama_model",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS extract_provider",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS intent_model",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS graph_provider",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS intent_provider",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS embed_provider",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS system_prompt",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS gemini_model",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS bedrock_region",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS bedrock_model",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS sub_profile",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS sub_planner",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS search_helper",
    "ALTER TABLE user_settings DROP COLUMN IF EXISTS search_helper_model",
    # OCR（任意の視覚観測）: 取り込みとは独立したジョブキュー。`canonical_generation_id` には World署名（worlds.last_sig）を入れる（原本内容が変わればキーも変わる）。
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
    "ALTER TABLE ocr_jobs ADD COLUMN IF NOT EXISTS cache_input_fingerprint TEXT",
    "CREATE INDEX IF NOT EXISTS ocr_jobs_leaseable ON ocr_jobs(priority DESC, available_at, id) "
    "WHERE status IN ('queued','leased')",
    "CREATE INDEX IF NOT EXISTS ocr_jobs_world_generation ON ocr_jobs(world, canonical_generation_id, status)",
    # observation generation の公開は fetchall せず、この順序の keyset cursor で stream する。
    "CREATE INDEX IF NOT EXISTS ocr_jobs_publish_order "
    "ON ocr_jobs(world, canonical_generation_id, source_rel_path, result_observation_set_hash, id) "
    "WHERE status='succeeded' AND result_payload IS NOT NULL AND result_observation_set_hash IS NOT NULL",
    # 取り込み側は「この World 署名の OCR を作り直す」1行を enqueue するだけで、隔離 worker が派生物を決定順に stream して ocr_jobs へ展開する。cursor_rel_path は再起動時の再開位置で、manifest 本文や原本 path は複製しない。
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
    # 同じ World 内の同一前処理画像＋engine profile は先勝ち cache を共有する。payload は engine の生 OCR 行だけで、Evidence ID を持つ Observation Set は job ごとに再構築する。
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

    # 認証・ユーザー管理・会話共有。users がアプリの正本で、uid（文字列キー）が conversations/user_settings.user_id と接続する。
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
    # 既存 seed の users にも冪等で列追加する。
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS uid TEXT UNIQUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    # email は任意（uid がキー）なので nullable に緩める（冪等）。
    "ALTER TABLE users ALTER COLUMN email DROP NOT NULL",
    # seed admin を uid='admin' に接続する（既存 email があれば埋める）。
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
    # conversations を「所有」と「受領共有」の両対応にする（受領はメッセージをコピーせず元会話を参照）。
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
    # 同じユーザー・同じ share は履歴に 1 行だけ（受領ラッパーの冪等キー）。
    "CREATE UNIQUE INDEX IF NOT EXISTS conv_received_share_once ON conversations(user_id, share_id) "
    "WHERE origin='received_share' AND share_id IS NOT NULL AND deleted_at IS NULL",
    # 受領共有ラッパーを自分の会話として複製した出所（フォーク元）。`forked_from_share_id` は共有が後で消えても残るよう SET NULL にする。
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS forked_from_share_id BIGINT "
    "REFERENCES conversation_shares(id) ON DELETE SET NULL",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS forked_from_user_id TEXT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS forked_at TIMESTAMPTZ",
    # サニタイズ共有の再共有（スナップショット更新）で最後に取り直した時刻。NULL＝一度も refresh していない。
    "ALTER TABLE conversation_shares ADD COLUMN IF NOT EXISTS refreshed_at TIMESTAMPTZ",
    # uid スラッグ形式を DB でも強制する（多層防御）。NULL uid も拒否する（CHECK は NULL を通すため IS NOT NULL を含める）。古い制約があれば先に DROP して正しい定義で再追加する（冪等）。
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
    # VALIDATE（既存行が全て適合なら NOT VALID→VALID に昇格・何度呼んでも安全）。
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
    # audit_log 強化（冪等 ALTER）。既存行は DEFAULT で埋まる。
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
    # 改ざん検知の hash-chain（entry_hash = SHA256(prev_hash || canonical_json(row)))。
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS prev_hash TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS entry_hash TEXT",
    # chain head アンカー（単一行）。末尾行の truncation/欠落を検出するための last_id/last_hash/cnt で、audit() が同一 tx で更新し、verify() が末尾行と照合する。
    """CREATE TABLE IF NOT EXISTS audit_chain_head (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        last_id BIGINT,
        last_hash TEXT,
        cnt BIGINT NOT NULL DEFAULT 0
    )""",
    # chain_start_id: 最初の hashed 行 id（一度だけ set）。chain 開始後の NULL-hash 偽行注入を検出する基準。
    "ALTER TABLE audit_chain_head ADD COLUMN IF NOT EXISTS chain_start_id BIGINT",
    # 列追加前から chain がある既存 head に、最初の hashed 行 id を冪等に埋める。
    "UPDATE audit_chain_head SET chain_start_id="
    "  (SELECT MIN(id) FROM audit_log WHERE entry_hash IS NOT NULL) "
    "  WHERE chain_start_id IS NULL AND cnt > 0",
    # 個人 workspace 台帳。grep 専用で、ES/Neo4j の共有インデックス取り込み対象に含めない（es_index.py や world_graph.py はこのテーブルを参照しない）。
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
    # 運営掲示板（トップ画面のお知らせ）。
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
    # 掲示板の公開/削除タイマー: publish_at=NULL は即時公開、expire_at=NULL は無期限掲載。
    "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS publish_at TIMESTAMPTZ",
    "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS expire_at TIMESTAMPTZ",
    # publish_at > expire_at を DB でも禁止する（アプリ層の検証に加えた多層防御・冪等 DO $$ + NOT VALID→VALIDATE の 2 段）。
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
    # 会話共有の無期限オプション: NULL = 無期限（既存行は非NULLのまま）。
    "ALTER TABLE conversation_shares ALTER COLUMN expires_at DROP NOT NULL",
    # conversations.source_conversation_id の FK を CASCADE → SET NULL へ冪等移行する（元会話の削除で受領ラッパー/snapshot が消えないようにする）。制約名は conversations_source_conversation_id_fkey。`confdeltype='n'`（既に SET NULL）ならスキップする。
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
    # 全体設定（system_settings・admin 書込のみ・監査つき）。全ユーザーに効く汎用 KV（value=JSONB）。優先順は system_settings > env > コード既定。
    """CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value JSONB,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by TEXT
    )""",
    # 外部連携 API キー。ハッシュのみ保存（プレーンキーは発行レスポンスで 1 度だけ返す）。失効は soft（revoked_at）で行削除はしない。
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
    # キーの world スコープ（オプトイン）。NULL=全 world 許可・空配列=どの world も許可しない。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_worlds TEXT[]",
    # 有効期限・日次クォータ（NULL=無期限・無制限）と所有者（自己発行キーの uid・NULL=admin 発行）。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS daily_quota INTEGER",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_uid TEXT",
    # 発行 UI が生成する相関トークン（オプトイン・秘密ではない・UUID）。POST 応答が失われたとき、回復エンドポイント（`ext_key_recover`/`ext_self_key_recover`・`revoke_unconfirmed_key_by_client_op_id`）がこの値を認証主体・所有条件と同一 SQL で照合して失効する。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS client_op_id TEXT",
    # キー 1 本につき Webhook 宛先 1 本（NULL=無効）。`webhook_secret` は HMAC-SHA256 の署名生成に平文が要るため平文保管し、応答/一覧には出さない（発行応答でのみ 1 度返す・`system_extras.py::_key_created_out`）。
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS webhook_url TEXT",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS webhook_secret TEXT",
    # `client_op_id` は非NULLに限り一意（衝突は二重発行として 409 に変換する・`api_keys.insert_api_key` が制約名で判定する）。大小文字違いは同一 UUID として `lower()` の関数インデックスで一意性を強制する。この索引は `_migrate_client_op_id_unique_index()`（`init_schema()` から呼ぶ）が作る。
    # `daily_quota` の範囲を DB でも制約する（pydantic 上限と揃える・冪等 DO $$ + NOT VALID→VALIDATE の 2 段）。VALIDATE が失敗したときは、違反行を次の SQL で特定してから復旧する（黙って値を丸めない）:
    #   検出: SELECT id, owner_uid, daily_quota FROM api_keys
    #         WHERE NOT (daily_quota IS NULL OR (daily_quota > 0 AND daily_quota <= 1000000));
    #   復旧（行ごとに選ぶ）:
    #     - 上限へ丸める:   UPDATE api_keys SET daily_quota = 1000000 WHERE id = <id> AND daily_quota > 1000000;
    #     - 無制限へ戻す:   UPDATE api_keys SET daily_quota = NULL WHERE id = <id>;
    # 復旧後、次回起動で本 DDL が再実行され VALIDATE が通る。
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
    # schema バージョンの記録専用スタンプ。DDL 適用の可否判断には使わない（DDL は毎起動・全文冪等実行）。読み手は運用者のみ。
    """CREATE TABLE IF NOT EXISTS schema_version (
        id BIGSERIAL PRIMARY KEY,
        schema_hash TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # チャット以外の LLM 呼び出しの計測。チャット本回答の usage は `messages.answer->'usage'` に残る（二重書き込みなし）。記録は常時（`sherpa/metering.py`）で、suppress() 中だけ記録しない。トークン列は NULLABLE（NULL＝プロバイダが usage を返さなかった「報告不能」・0＝ゼロと報告）。
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
    # LLM 呼び出しの所要時間。NULL＝計測スコープ（`metering.acc_begin`/`acc_end`）の外で記録された行。
    "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS elapsed_ms BIGINT",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events (ts)",
    # 会話ごとの補助 AI 使用量の集計キー。NULL＝会話 id が未確定の経路または過去データ。
    "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS conversation_id BIGINT",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_conversation_id ON usage_events (conversation_id)",
    # 表示・分析用の付帯内訳（kind='chat-round'＝査読の巡別記録: 巡番号・evaluator の判定と不足の軸・引用件数の増分・巡内の limits 増分・主張の区分内訳・役割別のトークン）。課金集計（`store/usage.py`）は読まない。NULL＝付帯内訳を持たない行。
    "ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS meta JSONB",
    # 回答ごとの利用者フィードバック（👍/👎＋定型タグ＋任意の一言）。1 利用者×1 メッセージにつき最新 1 件（再送は上書き）。本文は複製せず message_id で messages を参照する（会話削除に CASCADE で追従）。
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
    # 外部で採点した品質採点（1 巡 vs 3 巡等の正解付き比較）の集計済みカウント。1 行＝1 採点ラン。質問文・回答本文は列を持たない。`rounds` は比較した巡数（0＝見直しを回さない条件）、`cost_usd` は任意。
    """CREATE TABLE IF NOT EXISTS depth_quality_runs (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        rounds INTEGER NOT NULL,
        correct INTEGER NOT NULL DEFAULT 0,
        wrong_assertion INTEGER NOT NULL DEFAULT 0,
        missing INTEGER NOT NULL DEFAULT 0,
        regressed INTEGER NOT NULL DEFAULT 0,
        unrated INTEGER NOT NULL DEFAULT 0,
        cost_usd NUMERIC
    )""",
    # 冪等キー（NULL 可）。`run_id IS NOT NULL` の部分ユニーク索引により、同じ run_id の再送が 2 行目を作らない（`record_depth_quality_run` の ON CONFLICT (run_id) が使う）。
    "ALTER TABLE depth_quality_runs ADD COLUMN IF NOT EXISTS run_id TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_depth_quality_runs_run_id "
    "ON depth_quality_runs (run_id) WHERE run_id IS NOT NULL",
    # 採点した条件（`store/usage.py::QUALITY_RUN_CONDITIONS` の閉集合）と、質問セットを実行した期間。集計（`depth_quality_stats`）は登録時刻 `ts` ではなく実行期間で照会する。列は NULL 許容で、実行期間を持たない行は集計の母集団に入らない。
    "ALTER TABLE depth_quality_runs ADD COLUMN IF NOT EXISTS condition TEXT",
    "ALTER TABLE depth_quality_runs ADD COLUMN IF NOT EXISTS executed_from TIMESTAMPTZ",
    "ALTER TABLE depth_quality_runs ADD COLUMN IF NOT EXISTS executed_to TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS idx_depth_quality_runs_executed "
    "ON depth_quality_runs (executed_from, executed_to)",
    # 集計専用の細い写像表（`turn_metrics`）。1 assistant message = 1 行。正本は `messages.answer` で、本表はそこから再生成できる派生物（`store/turn_metrics.py::metrics_from_answer` が写像元）。`personal` は回答自体が個人の資料を使ったか（`chat_service._used_personal`）で、対応する user 発言は user_message_id で引く。`activity_json` は検証済みの activity（本文・資料名・ツール引数を含まない）を保持する。
    """CREATE TABLE IF NOT EXISTS turn_metrics (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        user_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        user_id TEXT NOT NULL,
        world TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        lens TEXT,
        personal BOOLEAN NOT NULL DEFAULT false,
        provider TEXT,
        model TEXT,
        depth_profile TEXT,
        reasoning TEXT,
        app_version TEXT,
        stop_kind TEXT,
        codex_error_code TEXT,
        duration_ms BIGINT,
        phase_prepare_ms BIGINT,
        phase_agent_ms BIGINT,
        phase_post_ms BIGINT,
        input_tokens BIGINT,
        cached_input_tokens BIGINT,
        output_tokens BIGINT,
        reasoning_output_tokens BIGINT,
        parent_input_tokens BIGINT,
        parent_cached_input_tokens BIGINT,
        parent_output_tokens BIGINT,
        parent_reasoning_output_tokens BIGINT,
        child_input_tokens BIGINT,
        child_cached_input_tokens BIGINT,
        child_output_tokens BIGINT,
        child_reasoning_output_tokens BIGINT,
        children_detected INTEGER,
        children_usage_found INTEGER,
        children_usage_missing INTEGER,
        tool_calls_total INTEGER,
        tool_result_bytes_total BIGINT,
        api_rounds_total INTEGER,
        compactions_total INTEGER,
        tool_result_clipped INTEGER,
        context_compactions INTEGER,
        search_truncated INTEGER,
        auto_continues INTEGER,
        duplicate_tool_call INTEGER,
        total_budget_hit BOOLEAN,
        synthesis_truncated BOOLEAN,
        depth_escalated BOOLEAN,
        backend_unavailable_fulltext BOOLEAN,
        backend_unavailable_graph BOOLEAN,
        graph_reingest_required BOOLEAN,
        tool_calls_exhausted BOOLEAN,
        sources_count INTEGER NOT NULL DEFAULT 0,
        investigation_complete BOOLEAN,
        investigation_continuations INTEGER,
        investigation_counts JSONB,
        claims_confirmed INTEGER,
        claims_inferred INTEGER,
        claims_unknown INTEGER,
        claims_unknown_reasons JSONB,
        gate_missing_codes JSONB,
        activity_json JSONB,
        mapping_source TEXT NOT NULL,
        mapping_version INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS turn_metrics_created_at ON turn_metrics(created_at)",
    "CREATE INDEX IF NOT EXISTS turn_metrics_user_created ON turn_metrics(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS turn_metrics_conversation ON turn_metrics(conversation_id)",
    "CREATE INDEX IF NOT EXISTS turn_metrics_user_message ON turn_metrics(user_message_id)",
    # 1 ターン×エージェント番号（activity.agents の配列添字）×ツール 1 行。activity の無いターンは行を持たない。
    """CREATE TABLE IF NOT EXISTS turn_tool_stats (
        id BIGSERIAL PRIMARY KEY,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        agent_index INTEGER NOT NULL,
        role TEXT,
        tool TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        bytes_total BIGINT,
        max_bytes BIGINT,
        clipped INTEGER,
        truncated INTEGER,
        errors INTEGER,
        ms BIGINT,
        UNIQUE (message_id, agent_index, tool)
    )""",
    # Codex ジョブ（外部 API の非同期 Codex 実行）。再起動後も追える永続ジョブ（`sherpa/store/codex_jobs.py` が唯一の読み書き窓口）。`status` は queued/running/completed/failed/cancelled/expired の 6 値（CHECK 制約は持たない）。結果（質問文・回答・出典・未確認項目）は `finished_at` から 7 日（`expires_at`）を過ぎたら `status='expired'` にして NULL へ消す（`codex_jobs.py::expire_due_jobs`/照会時の遅延判定）。
    """CREATE TABLE IF NOT EXISTS codex_jobs (
        id TEXT PRIMARY KEY,
        key_id BIGINT NOT NULL REFERENCES api_keys(id),
        world TEXT NOT NULL,
        query TEXT,
        scope_paths TEXT[] NOT NULL DEFAULT '{}',
        depth TEXT NOT NULL DEFAULT 'standard',
        status TEXT NOT NULL DEFAULT 'queued',
        error_code TEXT,
        answer TEXT,
        sources JSONB,
        unconfirmed_items JSONB,
        elapsed_ms INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ
    )""",
    # 待ちジョブの取り出し（古い順）・鍵単位の一覧・期限切れ掃除がそれぞれ使う索引。
    "CREATE INDEX IF NOT EXISTS codex_jobs_queued ON codex_jobs(created_at) WHERE status='queued'",
    "CREATE INDEX IF NOT EXISTS codex_jobs_key_id ON codex_jobs(key_id)",
    "CREATE INDEX IF NOT EXISTS codex_jobs_expires_at ON codex_jobs(expires_at) "
    "WHERE status IN ('completed','failed','cancelled')",
    # ジョブ結果に載せる調査台帳の記録。本体（`answer` 等）と同じ 7 日保存で、`expire_due_jobs`/`expire_job_if_due` が一緒に NULL へ消す。
    "ALTER TABLE codex_jobs ADD COLUMN IF NOT EXISTS investigation JSONB",
    # 終了通知を受付時に希望したか（通知先は送信時に鍵から読む）。
    "ALTER TABLE codex_jobs ADD COLUMN IF NOT EXISTS webhook BOOLEAN NOT NULL DEFAULT false",
    # 調査台帳の記録。1 assistant message = 0〜1 行（台帳ゲートが実際に走ったターンだけ・`sherpa/providers/codex/provider.py::_investigation_record_payload`/`sherpa/chat_service.py` が assistant message 保存の直後に書く）。manifest/items/coverage は投稿時点の調査台帳の正規形をそのまま写したもので、本文・資料名は運ばない。`messages.trace` には入れない（JSONB の肥大を避けて分離する）。
    """CREATE TABLE IF NOT EXISTS investigation_records (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        complete BOOLEAN NOT NULL,
        truncated BOOLEAN NOT NULL DEFAULT false,
        manifest JSONB,
        items JSONB NOT NULL DEFAULT '{}',
        coverage JSONB NOT NULL DEFAULT '{}',
        reviews JSONB NOT NULL DEFAULT '[]'
    )""",
    # 中間の見直し（`investigation_ledger.load_reviews` の正規形の配列）の列。既存の investigation_records へ後付けする（CREATE TABLE IF NOT EXISTS は新規作成しか通らない）。
    "ALTER TABLE investigation_records ADD COLUMN IF NOT EXISTS reviews JSONB NOT NULL DEFAULT '[]'",
    "CREATE INDEX IF NOT EXISTS investigation_records_conversation "
    "ON investigation_records(conversation_id)",
]
# usage_stats の期間絞り（`_usage_period_bounds`）が messages.created_at の索引を使えるようにする索引。`_USAGE_TURN_CTE` の `touched`（期間の候補メッセージを絞るサブクエリ）が使う。
# `_SCHEMA` には含めない（既存の大規模 messages では素の CREATE INDEX が起動の単一トランザクション内で長時間かかり、readiness 未達のまま強制終了→再起動を繰り返しうるため）。`ensure_messages_created_at_index()` が別 autocommit 接続で `CREATE INDEX CONCURRENTLY` を実行し、`init_schema()` はその完了を待たない（background daemon thread）。
_MESSAGES_CREATED_AT_INDEX = "idx_messages_created_at"
# pg_get_indexdef() の出力に含まれるはずの断片で、想定通りの単純索引（複合索引や別列の索引ではない）かを判定する簡易マーカー。
_MESSAGES_CREATED_AT_INDEXDEF_FRAGMENT = "(created_at)"

# 索引構築の直列化用鍵。まず schema 初期化（init_schema の advisory lock）と同じ鍵を取得→即解放して「他プロセスの schema DDL 実行中は索引構築を始めない」バリアにし、続けて索引構築専用の鍵で `pg_try_advisory_lock` する（取れなければ何もせず return）。
_SCHEMA_LOCK_KEY = int.from_bytes(hashlib.sha1(f"schema:{_KB_ID}".encode("utf-8")).digest()[:8],
                                  "big", signed=True)
# 索引構築専用の鍵は pg_advisory_lock の 2 引数形（class_id, key）を使う。1 引数形は world_lock 等の `sha1` 由来の鍵と同じ 64bit 空間を共有して衝突しうるため、別名前空間の固定の整数ペアにする（`tests/integration/test_schema_init_r5.py` の衝突なしテストで固定）。
_CREATED_AT_INDEX_LOCK_CLASSID = 0x50455246  # "PERF" の ASCII 値由来の固定クラス ID。
_CREATED_AT_INDEX_LOCK_KEY = 1


def ensure_messages_created_at_index() -> None:
    """`idx_messages_created_at` を CONCURRENTLY で作成する（別 autocommit 接続・非ブロッキング）。単一 worker 前提。
    次の 2 段の advisory lock を掛ける:
    ① `_SCHEMA_LOCK_KEY`（schema 初期化と同じ鍵）を取得→即解放して、他プロセスの `init_schema()` の DDL 完了まで待つ。
    ② `_CREATED_AT_INDEX_LOCK_CLASSID`/`_CREATED_AT_INDEX_LOCK_KEY`（2 引数形）を `pg_try_advisory_lock` で取得できたときだけ検査・修復・作成を行う。取れなければ他プロセスが処理中とみなして警告ログを残して return する（`indisvalid=false` は構築中の正常な中間状態でもあり、ロック無しで DROP すると構築中の索引を壊しうる）。
    CONCURRENTLY は構築失敗時に INVALID な索引が残りうるため、ロック保持下で `pg_index.indisvalid` を確認し、INVALID なら `DROP INDEX CONCURRENTLY` してから作り直す（冪等）。DB 不達等の例外はログのみで握る（索引が無くても集計結果は変わらず性能にだけ影響する）。
    """
    conn = None
    try:
        conn = psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row,
                               connect_timeout=_INIT_CONNECT_TIMEOUT)
        conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_KEY,))
        try:
            pass  # バリアのみ: 他プロセスの schema DDL 完了を待つ（本体はロック解放後）。
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
            # 他プロセスが処理中（正常な CONCURRENTLY 構築中を含む）。何もせず次回の init_schema() での再試行に委ねる。
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
                # 有効な同名索引が既にある。定義が想定（messages(created_at)）と異なる場合は、別索引を壊さないよう DROP/CREATE せず警告ログのみ残す。
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
    """`ensure_messages_created_at_index()` を daemon スレッドで一度だけ起動する（プロセス生存期間で 1 回）。"""
    global _created_at_index_thread_started
    if _created_at_index_thread_started:
        return
    _created_at_index_thread_started = True
    threading.Thread(target=ensure_messages_created_at_index, daemon=True,
                     name="sherpa-idx-messages-created-at").start()


# コード側スキーマの内容ハッシュ（`schema_version` スタンプ用・記録専用）。`tests/unit/test_store_surface.py` の golden 算出式（sha256("\n".join(_SCHEMA))）と同一。
_SCHEMA_HASH = hashlib.sha256("\n".join(_SCHEMA).encode("utf-8")).hexdigest()


_inited = False

# init_schema の接続確立タイムアウト（秒）。lifespan 起動時と /healthz の readiness リトライの両方から呼ばれるため、PG 不達で無期限にブロックしない上限を置く。
_INIT_CONNECT_TIMEOUT = 5


def _dsn() -> str:
    dsn = os.environ.get("SHERPA_PG_DSN")
    if dsn:
        return dsn
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url
    # フォールバックの password は PGPASSWORD ＞ POSTGRES_PASSWORD ＞ 既定（docker-compose.yml と同じ変数でそろえる）。
    return "host={h} port={p} dbname={d} user={u} password={pw}".format(
        h=os.environ.get("PGHOST", "localhost"), p=os.environ.get("PGPORT", "5432"),
        d=os.environ.get("PGDATABASE", "sherpa"), u=os.environ.get("PGUSER", "sherpa"),
        pw=os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD") or "sherpa_dev")


# PG コネクションプール。引数無しの `_connect()`（advisory/session-level lock を持たない通常の CRUD）だけをプールにする。
# `connect_timeout=`/`options=` 等の kwargs 付き呼び出し（`init_schema`/`_read_system_settings_fresh`/`worlds.py`/`ingest.py`/`usage_events.py`/`api_keys.py` の集計）は、接続ごとに値を変えられないため従来どおり ad-hoc な `psycopg.connect()` を使う。
# advisory lock を保持する `world_lock`/`world_lock_shared`/`world_registry_lock`/`workspace_file_lock` は `_connect()` を経由せず、専用の `psycopg.connect(..., autocommit=True)` を使う（session-level lock をプールの接続に残さない）。
_PG_POOL_LOCK = threading.Lock()
_PG_POOL = None  # 型: psycopg_pool.ConnectionPool（遅延 import・未生成時は None）。


def _make_pg_pool():
    from psycopg_pool import ConnectionPool

    min_size = max(0, int(os.environ.get("SHERPA_PG_POOL_MIN", "2")))
    max_size = max(min_size, int(os.environ.get("SHERPA_PG_POOL_MAX", "10")))
    # 取得タイムアウト（枯渇時は `psycopg_pool.PoolTimeout` を送出して打ち切る・env で調整可）。
    acquire_timeout = float(os.environ.get("SHERPA_PG_POOL_TIMEOUT", "10"))
    pool = ConnectionPool(
        _dsn(),
        min_size=min_size,
        max_size=max_size,
        open=True,
        timeout=acquire_timeout,
        kwargs={"row_factory": dict_row},  # 既存 `_connect()` と同じ row_factory。
        check=ConnectionPool.check_connection,  # 払い出す前に腐った接続を検知して作り直す。
    )
    atexit.register(_close_pg_pool_best_effort, pool)
    return pool


def _close_pg_pool_best_effort(pool) -> None:
    """プロセス終了時のクローズ（フォールバック）。lifespan（`sherpa.lifespan`）の shutdown が通常の終了を担い、lifespan を経由しない経路（テスト/CLI/OCR worker）のために `atexit` にも登録する（二重に呼ばれても `ConnectionPool.close()` は冪等）。"""
    try:
        pool.close(timeout=5.0)
    except Exception:
        pass


def _get_pg_pool():
    """プロセス内シングルトンの PG プール（遅延生成・スレッドセーフ）。
    `_dsn()` は生成時に一度だけ読む。プロセス起動後に `SHERPA_PG_DSN` を書き換えて接続先を切り替える経路は無く、足す場合は明示的なプール再生成が要る。
    """
    global _PG_POOL
    if _PG_POOL is not None:
        return _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is None:
            _PG_POOL = _make_pg_pool()
    return _PG_POOL


def close_pg_pool(timeout: float = 5.0) -> None:
    """PG プールを閉じる（`sherpa.lifespan` の shutdown から呼ぶ）。未生成なら何もしない。多重呼び出し・`atexit` 経由の二重クローズは安全で、呼び出し後に `_connect()` が呼ばれれば新しいプールを遅延生成する。"""
    global _PG_POOL
    with _PG_POOL_LOCK:
        pool, _PG_POOL = _PG_POOL, None
    if pool is not None:
        try:
            pool.close(timeout=timeout)
        except Exception:
            _log.warning("PG プールのクローズに失敗しました（プロセス終了時のベストエフォート）", exc_info=True)


class PooledConnectionReleasedError(RuntimeError):
    """既にプールへ返却済みの `_PooledConnection` を使おうとした（呼び出し側のバグ）。返却後は物理接続への参照を切るため、属性アクセスは必ずこの例外になる（別リクエストへ貸し出された接続へ SQL を流さないため）。"""


class _PooledConnection:
    """`_connect()`（引数無し）の返り値ラッパー。
    `with _connect() as c:` は `c` を実接続（`psycopg.Connection`）に束縛し、commit/rollback は従来と同一で、ブロックを抜けるとソケットを閉じずプールへ返却する。
    `with` を使わず素の返り値へ `.execute()`/`.commit()`/`.close()` を呼ぶ使い方に備え、`__getattr__` で実接続へ委譲しつつ `.close()` だけプールへの返却に置き換える（素の `close()` はプール由来の接続を物理クローズして貸出中のまま失うため）。
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
        conn, self._conn = self._conn, None  # 参照を切る（use-after-release を検出可能にする）。
        self._pool.putconn(conn)

    def __getattr__(self, name):
        if self._conn is None:
            # 返却済みの物理接続は別リクエストへ貸し出されている可能性があるため、属性アクセスを明示的に拒否する。
            raise PooledConnectionReleasedError(
                f"このプール接続は既に返却済みです（close()/with 終了後は再利用不可）: attribute={name!r}")
        return getattr(self._conn, name)


def _connect(**kw):
    if not kw:
        pool = _get_pg_pool()
        return _PooledConnection(pool, pool.getconn())
    # kwargs 付き（`connect_timeout=`/`options=`）はプール非対応。従来どおり ad-hoc 接続。
    return psycopg.connect(_dsn(), row_factory=dict_row, **kw)


def _world_lock_key(world_id) -> int:
    """`world_lock`/`world_lock_shared` 共通のキー導出（同じ world_id は同じ鍵になる）。"""
    return int.from_bytes(hashlib.sha1(f"{_KB_ID}:{world_id}".encode("utf-8")).digest()[:8],
                          "big", signed=True)


@contextlib.contextmanager
def world_lock(world_id, *, timeout_ms: int | None = None):
    """world 単位の Postgres advisory lock（排他・取り込み/削除/rebind を直列化）。複数 worker/プロセスでも有効。
    `world_lock_shared`（共有）と同じ鍵を使うため、共有ロックの全保持者が解放されるまで待ち、保持中は共有ロックも待たされる。
    `timeout_ms`（省略可）: 取得できないまま指定ミリ秒を超えたら `psycopg.errors.LockNotAvailable` を送出する。省略時は無制限に待つ。対話的な HTTP 操作（手動 `/extract` 等）が 409/503 を即座に返すために使う。
    """
    _ensure()
    key = _world_lock_key(world_id)
    conn = psycopg.connect(_dsn(), autocommit=True)  # session-level lock（tx に縛らない）。
    try:
        if timeout_ms is not None:
            conn.execute(f"SET lock_timeout = '{max(1, int(timeout_ms))}ms'")
        conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        yield
    finally:
        # unlock は best-effort。session-level advisory lock は接続 close で必ず解放されるため、unlock の失敗で `with` 本体の元例外を隠さない。
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            pass
        finally:
            conn.close()


@contextlib.contextmanager
def world_lock_shared(world_id, *, timeout_ms: int | None = None, connect_timeout: float | None = None):
    """world 単位の Postgres advisory lock（共有・読み取り専用処理向け）。
    `world_lock`（排他）と同じ鍵（`_world_lock_key`）を `pg_advisory_lock_shared` で取得する。共有ロックの保持者同士は待たず、`world_lock`（rebind/削除/取り込み）とは相互排他になる（読み取り中の rebind による原本 root と派生物/索引の世代の食い違いを防ぐ）。
    `timeout_ms`（省略可）: 取得できないまま指定ミリ秒を超えたら `psycopg.errors.LockNotAvailable` を送出する。省略時は無制限に待つ。接続確立に要した時間を差し引いた残りだけを、接続確立後の `SET lock_timeout` に渡す（残りが 1ms 未満でも 0 にはせず最小 1ms へクランプ＝0 は無制限待ちになるため）。
    `connect_timeout`（省略可・秒）: `psycopg.connect(connect_timeout=...)` へ渡す。整数秒へ切り上げ・最小 1 秒でクランプする（psycopg 3.3.4 は整数秒しか扱わず、1 秒未満は 0＝無制限に丸まるため）。
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
            # SET はバインドパラメータを受け付けないため、内部計算済みの int を直接埋め込む。
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


# `world_registry_lock` 専用の鍵は 2 引数形（classid+key）にして、`_world_lock_key`（1 引数形）とは別の名前空間にする。world_id="world-registry" の `world_lock` と同じ鍵になる自己衝突を避ける。
_WORLD_REGISTRY_LOCK_CLASSID = 0x52454757  # "REGW" の ASCII 値由来の固定クラス ID。
_WORLD_REGISTRY_LOCK_KEY = 1


@contextlib.contextmanager
def world_registry_lock():
    """World 新規登録を全体で直列化する固定 Postgres advisory lock。登録件数の確認から行作成までを保護し、標準 MVP の「登録元フォルダは全体で 1 本」をプロセスをまたいで守る。
    lock 順序は必ず ``world_registry_lock -> world_lock``（逆順の経路は作らない）。
    """
    _ensure()
    conn = psycopg.connect(_dsn(), autocommit=True)  # session-level lock（world_lock と同じ方式）。
    try:
        conn.execute("SELECT pg_advisory_lock(%s, %s)",
                     (_WORLD_REGISTRY_LOCK_CLASSID, _WORLD_REGISTRY_LOCK_KEY))
        yield
    finally:
        # unlock は best-effort（session-level lock は接続 close で必ず解放される）。
        try:
            conn.execute("SELECT pg_advisory_unlock(%s, %s)",
                         (_WORLD_REGISTRY_LOCK_CLASSID, _WORLD_REGISTRY_LOCK_KEY))
        except Exception:
            pass
        finally:
            conn.close()


@contextlib.contextmanager
def workspace_file_lock(uid: str, rel_path: str):
    """(uid, rel_path) 単位の Postgres advisory lock（upload と sweep が同じ物理パスを同時に操作しないよう直列化する）。world_lock と同じ方式（session-level・autocommit）。"""
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
        try:  # unlock は best-effort。
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            pass
        finally:
            conn.close()


class ClientOpIdIndexUnexpectedDefinitionError(Exception):
    """`api_keys_client_op_id_unique` が既知のどの定義（現行=`lower()`／旧来=大小文字を区別）とも構造的に一致しない（非UNIQUE・invalid・複合キー・想定外の式・predicate 不一致 等）。
    fail-closed: DROP せず例外を送出して起動を止める（運用者が加えた別の制約を壊さないため）。
    """


def _resolve_api_keys_schema(conn) -> str:
    """`api_keys` 実表が属するスキーマ名を、`'api_keys'::regclass` と同じ search_path 解決規則で求める共通ヘルパー（`_classify_client_op_id_index`・`_migrate_client_op_id_unique_index` が使う）。"""
    row = conn.execute(
        "SELECT n.nspname AS nspname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = 'api_keys'::regclass"
    ).fetchone()
    return row["nspname"]


def _classify_client_op_id_index(conn) -> str:
    """`api_keys_client_op_id_unique` の現在の定義を `pg_index` の構造的な属性（`indisunique`/`indisvalid`・`indrelid`・`indnkeyatts`/`indnatts`・キー式・predicate）で分類する（`indexdef` 文字列の部分一致には頼らない）。
    索引の検索は `_resolve_api_keys_schema()` のスキーマに限定して名前だけで行い、`indrelid` の比較は Python 側で行う。
    (a) スキーマ限定が要る: 索引名は DB 全体では一意でなく、別スキーマの同名索引を拾うと誤って `"unknown"` と判定しうる。
    (b) `indrelid` を WHERE に混ぜない: 同じスキーマの別表が同名の索引を持つ場合に `"absent"` と区別できず、新規作成が生のエラーで落ちる。この状態は `"unknown"` として止める。
    `ns.nspname = current_schema()` の固定スキーマ名フィルタは使わない。`indnatts == indnkeyatts == 1` を要求して INCLUDE 列付きの索引を `"unknown"` に倒す。
    戻り値:
      - `"absent"`: `api_keys` のスキーマ内にこの名前の索引が無い（初回起動）。
      - `"current"`: 現行定義（`api_keys` 自身の索引・非NULL部分・UNIQUE・valid・単一キー列・INCLUDE 列無し・キー式が `lower(client_op_id)`）。
      - `"legacy"`: 既知の旧定義（上と同じで、キーが `client_op_id` そのまま＝大小文字を区別する）。
      - `"unknown"`: 上記のいずれでもない（別表の同名索引を含む）。
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
        return "unknown"  # 別表の同名索引（api_keys 自身の索引は無い状態）。
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
    毎起動の DROP→再作成はせず、`_classify_client_op_id_index()` の分類に従って次のいずれかだけ行う:
    ① `"absent"`: 新定義（`lower()`）で作成する。
    ② `"current"`: 何もしない。
    ③ `"legacy"`: 一度きりの移行。`GROUP BY lower(client_op_id) HAVING count(*) > 1` で大小文字違いの重複を検査し、新しい方（id が大きい方）の `client_op_id` に `-dup-<id>` を付けて退避してから索引を張り替える（古い方は残す・退避した行のキーは失効させない）。
    ④ `"unknown"`: `ClientOpIdIndexUnexpectedDefinitionError` を送出する（DROP しない）。
    `"legacy"` の `DROP INDEX` は `_resolve_api_keys_schema()` のスキーマで修飾する（別スキーマの同名索引を誤って DROP しないため）。
    戻り値: 退避を行った場合のログメッセージ一覧（空リスト＝退避なし）。ここではログを出さず、呼び出し側（`init_schema()`）がトランザクションの commit 後にだけログする。
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
    """会話/メッセージ表などのスキーマを冪等作成する。
    DDL 全文実行を `pg_advisory_lock`（固定キー・別 autocommit 接続・unlock は best-effort）で直列化する。`_SCHEMA` の DDL は毎起動・全文冪等実行する（適用済みスキップはしない）。例外は `_migrate_client_op_id_unique_index()` で、既に正しい定義ならスキップする。
    ロック内で DDL を実行した後、`schema_version` に記録専用のスタンプ（コード側スキーマのハッシュ＋適用時刻）を打つ（直近行と同じハッシュなら INSERT しない）。
    `connect_timeout`（省略可・既定 None＝`_INIT_CONNECT_TIMEOUT`＝固定 5 秒）: `_ensure()` 経由で呼び出し元が残り時間ベースの接続タイムアウトを渡す場合に使う。bound するのは接続確立の待ちだけで、DDL 実行自体（`statement_timeout`）は無期限のまま。整数秒へ切り上げ・最小 1 秒でクランプする。
    本関数は 2 回接続する（lock 用・DDL 用）。`connect_timeout` 指定時は 1 つの絶対期限を共有し、DDL 用接続には lock 用接続の確立＋`pg_advisory_lock` 待ちで経過した分を差し引いた残りを渡す。残りが 0 以下なら DDL 用接続を開始せず `TimeoutError` を送出する（advisory lock の unlock/close は `finally` で行う）。
    """
    global _inited
    absolute_deadline = (time.monotonic() + connect_timeout) if connect_timeout is not None else None
    ct = max(1, math.ceil(connect_timeout)) if connect_timeout is not None else _INIT_CONNECT_TIMEOUT
    key = int.from_bytes(hashlib.sha1(f"schema:{_KB_ID}".encode("utf-8")).digest()[:8],
                         "big", signed=True)
    # PG に到達できないとき接続待ちが無期限になるのを避けるため、接続確立にだけ上限を付ける（未認証の /healthz からも呼ばれる）。`statement_timeout` は付けない（DDL 全文実行は遅いディスクで時間がかかりうる）。
    lock_conn = psycopg.connect(_dsn(), autocommit=True,  # session-level lock（tx に縛らない）。
                                connect_timeout=ct)
    dup_messages: list[str] = []
    try:
        lock_conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        if absolute_deadline is not None:
            remaining = absolute_deadline - time.monotonic()
            if remaining <= 0:
                # lock 用接続の確立＋`pg_advisory_lock` 待ちで予算を使い切った場合は DDL 用の新規接続を試みない（`finally` で lock_conn の unlock/close は行う）。
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
        # `with` を正常に抜けた（commit 成功）後にだけログする（rollback 時に成功ログを残さないため）。
        for msg in dup_messages:
            _log.error(msg)
    finally:
        # unlock は best-effort。
        try:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            pass
        finally:
            lock_conn.close()
    _inited = True
    # readiness をブロックしない別経路で idx_messages_created_at を構築する（DB 不達等は関数内で握る）。
    _ensure_messages_created_at_index_background()


def schema_ready() -> bool:
    """スキーマ適用が完了しているか（`_inited` を返す）。`/healthz` の readiness 判定に使う。"""
    return _inited


def _ensure(connect_timeout: float | None = None) -> None:
    """未初期化（`_inited=False`）なら `init_schema()` を実行する。`connect_timeout`（省略可）は未初期化時にだけ `init_schema()` へ転送する。"""
    if not _inited:
        init_schema(connect_timeout=connect_timeout)

