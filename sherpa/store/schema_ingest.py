"""取り込み（`ingest_runs`・`ingest_workers`）と、入れ替えの行（`ingest_replacements`）・取り込みの番号の DDL。
`db._SCHEMA` の `documents`・`worlds` の後ろへ展開される（`name_ledger`・`name_ledger_built` もここで作る）。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

INGEST_SCHEMA = [
    # 取り込み・抽出の run 記録（ingest_runs）。グラフ反映境界を run 単位で持つ。version は documents.version と同じ自由文字列。共通 run は version=NULL＋layer=common。
    """CREATE TABLE IF NOT EXISTS ingest_runs (
        id SERIAL PRIMARY KEY,
        kb_id TEXT NOT NULL DEFAULT 'global',
        version TEXT,                              -- 単一版（共通 run は NULL）
        layer TEXT NOT NULL DEFAULT 'version',     -- version / common
        source_doc_ids JSONB,                     -- 対象 documents.name[]
        status TEXT NOT NULL DEFAULT 'extracting'  -- queued/extracting/auto_published/auto_published_with_flags/failed
            CHECK (status IN ('queued','extracting','auto_published','auto_published_with_flags','failed')),
        extraction_snapshot JSONB,                -- 抽出スナップショット（件数・検証フラグ）
        published_snapshot JSONB,                 -- Neo4j 反映内容（差分・再反映の基準）
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_at TIMESTAMPTZ,
        progress JSONB                            -- 実行中 run の逐次進捗（段＋done/total/更新時刻）。`status` が queued/extracting の間だけ意味を持ち、完了時（`finish_ingest_run`）に NULL へ戻す
    )""",
    # 使わなくなった列は起動時に撤去する。
    "ALTER TABLE ingest_runs DROP COLUMN IF EXISTS ingest_source_id",
    "ALTER TABLE ingest_runs DROP COLUMN IF EXISTS scan_root",
    "ALTER TABLE ingest_runs DROP COLUMN IF EXISTS scope_mapping_overrides",
    "ALTER TABLE ingest_runs DROP COLUMN IF EXISTS republished_at",
    # 取り込みの依頼（Web アプリが queued で積み、取り込みワーカーが拾う）。op/payload＝実行する操作と引数、fingerprint＝合流判定、
    # app_version＝依頼を受けた Web アプリの版（同じ版のワーカーだけが拾う）、arb_keys＝同時に 1 つしか動かさない単位（資料フォルダ・新規登録の枠）、
    # claimed_by＝拾ったワーカー。
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS op TEXT",
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS payload JSONB",
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS fingerprint TEXT",
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS app_version TEXT",
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS arb_keys TEXT[]",
    "ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS claimed_by TEXT",
    # 既存 DB の status 制約へ queued を足す（CREATE TABLE の制約は既存表に効かない）。足りているときは何もしない。
    """DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ingest_runs_status_check' AND conrelid = 'ingest_runs'::regclass
          AND pg_get_constraintdef(oid) NOT LIKE '%queued%'
      ) THEN
        ALTER TABLE ingest_runs DROP CONSTRAINT ingest_runs_status_check;
        ALTER TABLE ingest_runs ADD CONSTRAINT ingest_runs_status_check
          CHECK (status IN ('queued','extracting','auto_published','auto_published_with_flags','failed'));
      END IF;
    END $$""",
    "CREATE INDEX IF NOT EXISTS run_queued ON ingest_runs(kb_id, id) WHERE status = 'queued'",
    # 取り込み画面の版の照合・停止待ちの表示のための、Web アプリと取り込みワーカーの心拍（一定時間書かれない行は無視する）。
    """CREATE TABLE IF NOT EXISTS ingest_workers (
        worker_id TEXT PRIMARY KEY,
        role TEXT NOT NULL CHECK (role IN ('web','ingest')),
        app_version TEXT,
        state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle','running','waiting_version','stopping')),
        run_id INTEGER,
        current_file TEXT,
        pid INTEGER,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        beat_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS run_ver ON ingest_runs(kb_id, version)",
    # `store.ingest.get_latest_run_summary`/`get_latest_published_run_summary` が `ORDER BY id DESC LIMIT 1` で最新 1 件を引くための索引。反映済み run 用は部分索引（`published_at IS NOT NULL`）で小さく保つ。
    "CREATE INDEX IF NOT EXISTS run_ver_id ON ingest_runs(kb_id, version, id DESC)",
    "CREATE INDEX IF NOT EXISTS run_ver_published ON ingest_runs(kb_id, version, id DESC) "
    "WHERE published_at IS NOT NULL",
    # `store.ingest.get_latest_es_run_summary` 専用の部分索引（`extraction_snapshot` に es キーがある run だけ）。
    "CREATE INDEX IF NOT EXISTS run_ver_published_es ON ingest_runs(kb_id, version, id DESC) "
    "WHERE published_at IS NOT NULL AND extraction_snapshot ? 'es'",
    # 退役した取り込み元の登録表。行が 1 件も無いときだけ撤去する（残っている登録は消さない）。
    """DO $$ BEGIN
      IF to_regclass('ingest_sources') IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM ingest_sources) THEN
          DROP TABLE ingest_sources;
        END IF;
      END IF;
    END $$""",
    # 名前の台帳。資料フォルダごとに「名前・種類・出どころ・相対パス」を持つ（`store/name_ledger.py`）。
    """CREATE TABLE IF NOT EXISTS name_ledger (
        world TEXT NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL,
        rel_path TEXT NOT NULL
    )""",
    # 資料フォルダごとに「作った」記録（名前が 1 つも無い資料フォルダでも、作り終えたことを行の数と分けて持つ）
    """CREATE TABLE IF NOT EXISTS name_ledger_built (
        world TEXT PRIMARY KEY,
        built_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # 取り込みの番号。作ったもの（目録・名前の台帳・グラフ・ES・派生）に書き、どの取り込みで作ったかを揃えて比べる。0＝番号が付く前に作ったもの。
    "CREATE SEQUENCE IF NOT EXISTS ingest_no_seq",
    "ALTER TABLE worlds ADD COLUMN IF NOT EXISTS shown_ingest_no BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS ingest_no BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_ingest_no BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE name_ledger ADD COLUMN IF NOT EXISTS ingest_no BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE name_ledger_built ADD COLUMN IF NOT EXISTS ingest_no BIGINT NOT NULL DEFAULT 0",
    # 番号の違う行を並べておけるようにする（新しい番号の行を先に入れ、切り替えた後に古い番号の行を消す）。
    "CREATE UNIQUE INDEX IF NOT EXISTS doc_ver_no_name ON documents(kb_id, version, ingest_no, name)",
    # 場所（フォルダ）の前の部分で絞る読み取り（`name` の前方一致）と、名前順の続きの読み取りに使う。
    "CREATE INDEX IF NOT EXISTS doc_ver_no_name_prefix ON documents(kb_id, version, ingest_no, name text_pattern_ops)",
    "ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_kb_id_version_name_key",
    "CREATE INDEX IF NOT EXISTS name_ledger_world_no_name ON name_ledger(world, ingest_no, name)",
    "DROP INDEX IF EXISTS name_ledger_world_name",
    """DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'name_ledger_built_pkey' AND conrelid = 'name_ledger_built'::regclass AND cardinality(conkey) = 1
      ) THEN
        ALTER TABLE name_ledger_built DROP CONSTRAINT name_ledger_built_pkey;
        ALTER TABLE name_ledger_built ADD PRIMARY KEY (world, ingest_no);
      END IF;
    END $$""",
    # 入れ替えが終わるまで残る 1 行（資料フォルダ全体は doc=''）。kind: build 作る／delete 消す／cleanup 登録の無い孤児を消す。
    # lock_ns・lock_id は動いている間だけ持つ DB の lock の鍵。items は「この回が入れ替えるもの」だけを持つ。
    """CREATE TABLE IF NOT EXISTS ingest_replacements (
        id SERIAL PRIMARY KEY,
        kb_id TEXT NOT NULL DEFAULT 'global',
        world TEXT NOT NULL,
        doc TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL CHECK (kind IN ('build','delete','cleanup')),
        ingest_no BIGINT NOT NULL,
        lock_ns INTEGER,
        lock_id INTEGER,
        source_state TEXT NOT NULL DEFAULT 'collecting'
            CHECK (source_state IN ('collecting','fixed','not_used')),
        source_sig TEXT,
        source_list JSONB,
        settings JSONB NOT NULL DEFAULT '{}',
        items JSONB NOT NULL DEFAULT '{}',
        old_things JSONB NOT NULL DEFAULT '{}',
        rebuild_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (kb_id, world, doc)
    )""",
]
