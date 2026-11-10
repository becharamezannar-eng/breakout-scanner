-- Global Intelligence & Wealth Brief: PostgreSQL schema (applied automatically at start-up; safe to re-run).

CREATE TABLE IF NOT EXISTS briefs (
  id            BIGSERIAL PRIMARY KEY,
  kind          TEXT        NOT NULL,              -- daily | weekly
  brief_date    DATE        NOT NULL,
  generated_at  TEXT,
  mode          TEXT,                              -- ai | headlines
  model         TEXT,
  cost_usd      NUMERIC(10,4),
  data          JSONB       NOT NULL,              -- the complete brief as rendered
  UNIQUE (kind, brief_date)
);

CREATE TABLE IF NOT EXISTS brief_items (
  id          BIGSERIAL PRIMARY KEY,
  brief_id    BIGINT REFERENCES briefs(id) ON DELETE CASCADE,
  brief_date  DATE,
  kind        TEXT,
  section     TEXT,
  position    INT,
  headline    TEXT,
  summary     TEXT,
  why         TEXT,
  body        JSONB
);

CREATE TABLE IF NOT EXISTS articles (                -- every article ever cited in a brief
  aid           TEXT PRIMARY KEY,
  url           TEXT NOT NULL,
  source        TEXT,
  title         TEXT,
  published_at  TEXT,
  check_status  TEXT,                              -- ok | protected
  first_seen    TEXT
);

CREATE TABLE IF NOT EXISTS item_articles (
  item_id  BIGINT REFERENCES brief_items(id) ON DELETE CASCADE,
  aid      TEXT
);

CREATE TABLE IF NOT EXISTS item_themes (
  item_id     BIGINT REFERENCES brief_items(id) ON DELETE CASCADE,
  theme       TEXT,
  brief_date  DATE
);

CREATE TABLE IF NOT EXISTS runs (                    -- one row per run: health, errors, cost
  id      BIGSERIAL PRIMARY KEY,
  kind    TEXT,
  at      TEXT,
  status  TEXT,
  error   TEXT,
  stats   JSONB
);

CREATE INDEX IF NOT EXISTS ix_items_date   ON brief_items (brief_date DESC);
CREATE INDEX IF NOT EXISTS ix_items_fts    ON brief_items USING GIN (
  to_tsvector('english', coalesce(headline,'') || ' ' || coalesce(summary,'') || ' ' || coalesce(why,'')));
CREATE INDEX IF NOT EXISTS ix_themes       ON item_themes (theme, brief_date);
CREATE INDEX IF NOT EXISTS ix_item_articles ON item_articles (aid);
