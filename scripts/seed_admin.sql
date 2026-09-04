-- Sherpa MVP — M0 最小スキーマ：利用者テーブル＋ seed admin 1名
-- （07-データモデル.md の users を縮約。full スキーマは M1 以降のマイグレーションで）
-- Postgres の docker-entrypoint-initdb.d から初回起動時に1度だけ実行される。

CREATE TABLE IF NOT EXISTS users (
  id          BIGSERIAL PRIMARY KEY,
  email       TEXT UNIQUE NOT NULL,
  role        TEXT NOT NULL DEFAULT 'user'      -- 'user' | 'admin'
              CHECK (role IN ('user','admin')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- MVP は seed admin 1名で運用（10-Codexブリッジ.md §10 / MVP-DETAIL §6）
INSERT INTO users (email, role)
VALUES ('admin@sherpa.local', 'admin')
ON CONFLICT (email) DO NOTHING;
