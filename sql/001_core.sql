-- Crunchtime → Postgres: core ELT scaffolding
-- Layer 1 (raw.*) lands API JSON as-is; Layer 2 (ct.*) is the typed warehouse.
-- Idempotent: safe to run on every deploy.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ct;

-- Drives incremental windows and per-run drift detection (plan §4/§5).
CREATE TABLE IF NOT EXISTS sync_state (
    domain         TEXT PRIMARY KEY,
    last_run_at    TIMESTAMPTZ,
    last_status    TEXT,                  -- 'ok' | 'error'
    rows_ingested  INTEGER,
    error          TEXT
);

-- Per-run record of what changed; the daily email reads this.
CREATE TABLE IF NOT EXISTS change_log (
    id           BIGSERIAL PRIMARY KEY,
    domain       TEXT NOT NULL,
    natural_key  TEXT NOT NULL,
    change_type  TEXT NOT NULL,           -- 'new' | 'changed' | 'deleted'
    label        TEXT,                    -- human-friendly name for the email
    run_at       TIMESTAMPTZ NOT NULL,
    detail       JSONB
);
CREATE INDEX IF NOT EXISTS ix_change_log_run ON change_log(run_at);

-- Generic raw landing table. One physical table per domain (raw.recipes, raw.products, …)
-- all share this shape; created on demand by db.ensure_raw_table() using this template.
-- Kept here as documentation of the contract:
--   natural_key     TEXT PRIMARY KEY   -- extracted via endpoints.yaml natural_key_path
--   payload         JSONB NOT NULL     -- the full API record, untouched
--   source_endpoint TEXT NOT NULL
--   page            INTEGER
--   ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
--   seen_run_at     TIMESTAMPTZ        -- set every run; powers the "not seen → deleted" sweep
