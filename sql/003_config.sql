-- Setup Console: connection credentials and SMTP config.
-- Idempotent: safe to run on every deploy (all DDL uses IF NOT EXISTS).
-- Loaded automatically by db.init_schema() which globs sql/*.sql sorted.

CREATE SCHEMA IF NOT EXISTS config;

-- ── config.connections ────────────────────────────────────────────────────────
-- One row per (customer, connector, environment) triple.
-- `secrets` holds a Fernet-encrypted JSON blob of the credential set so that
-- plaintext credentials never touch the database unencrypted.

CREATE TABLE IF NOT EXISTS config.connections (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_name    text        NOT NULL,
    connector        text        NOT NULL CHECK (connector IN ('crunchtime', 'menuworks')),
    environment      text        NOT NULL,
    enabled          boolean     NOT NULL DEFAULT true,
    business_unit    text,
    secrets          text        NOT NULL,          -- Fernet-encrypted JSON blob
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    last_test_at     timestamptz,
    last_test_status text        NOT NULL DEFAULT 'untested'
                                 CHECK (last_test_status IN ('ok', 'fail', 'untested')),
    last_test_detail text
);

-- Enforce one active config per (customer, connector, environment).
CREATE UNIQUE INDEX IF NOT EXISTS connections_customer_connector_env
    ON config.connections (customer_name, connector, environment);

-- ── config.smtp ───────────────────────────────────────────────────────────────
-- Single-row table (enforced by CHECK id = 1) for the outbound SMTP settings
-- used by the daily email summary.  `password` is Fernet-encrypted.

CREATE TABLE IF NOT EXISTS config.smtp (
    id               int         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    host             text,
    port             int,
    security         text,                         -- 'starttls' | 'ssl' | 'none'
    username         text,
    password         text,                         -- Fernet-encrypted
    from_addr        text,
    recipients       text[],
    enabled          boolean     NOT NULL DEFAULT false,
    last_test_at     timestamptz,
    last_test_status text        NOT NULL DEFAULT 'untested',
    updated_at       timestamptz NOT NULL DEFAULT now()
);
