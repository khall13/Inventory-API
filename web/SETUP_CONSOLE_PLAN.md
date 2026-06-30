# Setup Console — Plan

**Feature:** A web UI to onboard new customer API connections (Crunchtime / MenuWorks) and configure
the SMTP "what changed" email — replacing hand-editing `.env` + `connectors.yaml`.
**Ticket:** TBD
**Status:** 📋 Plan + HTML sample — for review
**Date:** 2026-06-30
**Stack:** Python (FastAPI) backend + static HTML/JS frontend, PostgreSQL, Railway
**Sample:** [`setup-console.sample.html`](setup-console.sample.html) (rendered as an Artifact)

---

## Why
Today a connection lives in two places edited by hand: secrets in `scripts/.env` and shape in
`scripts/connectors.yaml`. That doesn't scale past one customer per connector and has no UI to
**test** a connection or manage the email. The console makes connections **data-driven** (DB rows),
testable in one click (reusing the `--smoke` path), and lets non-engineers onboard a customer.

## Scope
- **In:** CRUD for connector connections; per-connection "Test" (live auth check); SMTP config +
  "Send test email"; which connections feed the daily summary.
- **Out (separate):** the sync/transform engine itself; the PreciTaste upload pipeline; per-domain
  scheduling UI (Phase 2).

---

## Architecture

```
Browser (static HTML/JS)  ──fetch──▶  FastAPI web service  ──▶  Postgres (config.* tables)
                                            │                        ▲
                                            └── reuses make_client() / smoke ── connectors engine
Railway: existing "inventory-sync" cron service + NEW "inventory-web" service, shared Postgres.
```

- The **sync engine** gains a connection source: read enabled rows from `config.connections`
  instead of (or layered over) `.env`. `make_client()` already takes a connector config dict —
  feed it from a DB row instead of `connectors.yaml` + env. Backward compatible: env still works
  when no DB row exists.

### Data model (`config` schema)
- `config.connections`
  - `id` (uuid), `customer_name`, `connector` (`crunchtime`|`menuworks`), `environment`
    (`test`|`prod`|`qa`), `enabled` (bool), `business_unit` (text, nullable — MenuWorks),
    `secrets` (**encrypted** JSONB: the per-connector credential set), `created_at`, `updated_at`,
    `last_test_at`, `last_test_status` (`ok`|`fail`|`untested`), `last_test_detail`.
  - The credential set is connector-shaped: Crunchtime → `{sitename, authenticationtoken, userid,
    password}`; MenuWorks → `{wt_client_id, ibm_client_id}`.
- `config.smtp` (single active row)
  - `host`, `port`, `security` (`starttls`|`ssl`|`none`), `username`, `password` (**encrypted**),
    `from_addr`, `recipients` (text[]), `enabled`, `last_test_at`, `last_test_status`.

### Secrets at rest
- Encrypt `secrets`/`password` with **Fernet** (`cryptography`); key from `APP_SECRET_KEY` env on
  Railway (never in the repo). API returns secrets **masked** (`••••1a40`) — never the plaintext.
- Web service behind auth (start: a single `ADMIN_TOKEN`/basic auth; HTTPS via Railway domain).

### API (FastAPI)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/connections` | list (secrets masked) |
| POST | `/api/connections` | create |
| PUT | `/api/connections/{id}` | update (only re-encrypt secrets if resent) |
| DELETE | `/api/connections/{id}` | remove |
| POST | `/api/connections/{id}/test` | live auth check → reuses the connector `--smoke` GET; writes `last_test_*` |
| GET / PUT | `/api/smtp` | read / save SMTP config |
| POST | `/api/smtp/test` | send a test email to `recipients` |

### Frontend
Static `web/` (HTML + vanilla JS + the sample's CSS), served by the FastAPI app. Three panels:
**Connections** (table + add/edit form that adapts fields to the connector), **Email** (SMTP form +
test), **Schedule** (read-only view of the cron + which connections are enabled). The
[HTML sample](setup-console.sample.html) is the visual + interaction reference.

---

## Phases
- **Phase 1 — Backend skeleton:** `config` schema + migrations; FastAPI app; Fernet helper;
  connections CRUD with masking; `ADMIN_TOKEN` auth.
- **Phase 2 — Test actions:** `/connections/{id}/test` reusing `make_client()` + smoke GET;
  `/smtp/test` via `notify.py`'s sender.
- **Phase 3 — Engine integration:** sync reads enabled `config.connections` (env fallback kept);
  one customer per connector becomes many.
- **Phase 4 — Frontend wiring:** ship `web/` static assets against the API; the sample becomes live.
- **Phase 5 — Deploy:** new Railway `inventory-web` service (same repo, web start command), public
  domain, `APP_SECRET_KEY` + `ADMIN_TOKEN` set; shared Postgres.

## Test cases (append to TEST_PLAN.md)
| ID | Pri | Scenario | Expected |
|---|---|---|---|
| TC-020 | P0 | Create a MenuWorks connection, secrets stored | row persisted; `secrets` encrypted; GET returns masked |
| TC-021 | P0 | `POST /connections/{id}/test` for the live MenuWorks creds | `200`/`ok`; `last_test_status=ok` (matches CLI smoke) |
| TC-022 | P0 | Test with a bad IBM id | `fail`; detail surfaced; no crash |
| TC-023 | P1 | Update connection without resending secret | existing secret preserved (not nulled) |
| TC-024 | P0 | `POST /smtp/test` with valid creds | test email sent; `ok` |
| TC-025 | P1 | API without `ADMIN_TOKEN` | `401` |
| TC-026 | regression | sync still runs from `.env` when no DB connection rows | unchanged behavior |

## Open questions
1. Auth depth for v1 — single `ADMIN_TOKEN` vs real user accounts? (Recommend token for v1.)
2. One SMTP config globally, or per-customer recipients? (Recommend global + per-connection enable.)
3. Does engine integration (Phase 3) replace `.env` or layer over it? (Recommend layer: DB wins,
   env is fallback — zero-downtime migration.)
