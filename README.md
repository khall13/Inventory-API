# Crunchtime → Postgres extractor (+ daily email + PreciTaste export)

Connector-based ELT that pulls API data into PostgreSQL, runs daily on Railway, emails you
what changed, and can transform recipes into the PreciTaste/TastEOS upload shape.
**Connectors:** `crunchtime` (4-header auth, flat paging) and `menuworks` (single-header auth,
`options_json` paging, `data.*` envelopes). Adding another tool is config — see
[`docs/ADD_AN_API.md`](docs/ADD_AN_API.md).

> **Shared engine, per-system docs.** One extractor (`scripts/` + `sql/`) serves every
> connector. Each inventory system has its own subfolder holding *its* API docs/reference;
> all the runnable code stays at the root.

## Layout
```
Features/Inventory Systems API/
├── Dockerfile, railway.json        # daily Railway cron deploy
├── Crunchtime/                     # Crunchtime-specific docs
│   └── Crunchtime_API_to_SQL_Plan.md
├── MenuWorks/                      # MenuWorks-specific docs
│   ├── MenuWorks … API Spec.docx   # vendor spec (source of truth)
│   └── MenuWorks_API_Reference.md  # digested reference for the connector
├── sql/                            # 001_core (raw/ct schemas, sync_state, change_log) · 002_recipes
├── docs/ADD_AN_API.md              # runbook: onboard a new tool
├── scripts/
│   ├── sync.py                     # CLI orchestrator (smoke / init-db / domain / email)
│   ├── rest_client.py              # generic paged client: pluggable auth, paging, retry/backoff
│   ├── connectors.py + .yaml       # connector registry (base URL + auth + paging)
│   ├── endpoints.yaml              # domain config (add a domain = add a row)
│   ├── db.py                       # raw upsert w/ change detection, sync_state, change_log
│   ├── notify.py                   # SMTP "what changed" email (Gmail/O365)
│   ├── transform_recipes.py        # raw.recipes JSONB → ct.* (self-reports key coverage)
│   ├── transform_precitaste.py + precitaste_mapping.yaml   # ct.* → TastEOS upload arrays
│   ├── add_api.py                  # scaffold a connector / endpoints from an OpenAPI spec
│   ├── requirements.txt, .env.example
├── samples/   (Phase-0 raw JSON)   output/  (PreciTaste upload JSON)
```
Per-system references: [`Crunchtime/Crunchtime_API_to_SQL_Plan.md`](Crunchtime/Crunchtime_API_to_SQL_Plan.md)
· [`MenuWorks/MenuWorks_API_Reference.md`](MenuWorks/MenuWorks_API_Reference.md)

## What's needed to RUN (external — plan §8/§9)
1. **Crunchtime Test API creds** (a customer's CT admin mints an API User + token) → `.env`.
2. **Railway Postgres `DATABASE_URL`** (on Railway, set `${{Postgres.DATABASE_URL}}`).
3. **SMTP creds** for the email (Gmail App Password or O365).

## Local run order
```bash
cd scripts
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill CT creds + DATABASE_URL + SMTP

python sync.py --smoke                                   # prove auth
python sync.py --domain recipes --pages 1 --dump ../samples/recipes.json --no-transform
python transform_recipes.py --coverage ../samples/recipes.json   # finalize field maps
python sync.py --init-db
python sync.py --domain recipes --mode full              # backfill
python transform_precitaste.py                           # → ../output/precitaste_items.json
```

## Deploy to Railway (daily auto-pull + email)
1. New Railway project → add the **Postgres** plugin.
2. New service from this repo; set the service **root directory** to `Features/Crunchtime`
   (it builds the `Dockerfile`).
3. Service **Variables**: `CT_ENV, CT_SITENAME, CT_AUTH_TOKEN, CT_USER_ID, CT_PASSWORD`,
   `DATABASE_URL=${{Postgres.DATABASE_URL}}`, and the `SMTP_*` / `EMAIL_*` vars.
4. `railway.json` already sets the **cron schedule** (`0 8 * * *` ≈ 03:00 CT) and a
   run-once-then-exit policy. The container runs
   `sync.py --init-db && sync.py --domain all --mode incremental --email` daily.

The email summarizes new / changed / removed records per domain (change detection is a
content-hash comparison written to `change_log` each run).

## PreciTaste upload export
Per-source transforms write `{stock, inventory, service, menu}` arrays to `output/` in the shape
the [`upload-recipes`](../../.claude/skills/upload-recipes/SKILL.md) skill consumes (paste-ready
for console upload):
- **Crunchtime:** `transform_precitaste.py` (reads typed `ct.*`) + `precitaste_mapping.yaml`.
- **MenuWorks:** `transform_mw_precitaste.py` (reads `raw.mw_recipes`/`mw_ingredients`/
  `mw_units_of_measure`/`mw_menu_items`) + `mw_precitaste_mapping.yaml`. Resolves ingredient
  `unitId`→unit, classifies recipes Menu (in `mw_menu_items`) vs Inventory.

Both are rule-based + tunable, are guardrail-aware (yields=1/qty, `order` set, dependency order),
and print a decision/coverage report (unmapped units, zero-qty, unresolved mrns) so you refine
before uploading. The actual upload still runs through the `upload-recipes` pipeline, which
enforces the F1–F17 guardrails + self-test.

## Add another API
`python add_api.py connector --name <tool> --base-url <url> --auth <type>` and
`python add_api.py endpoints --openapi <spec> --connector <tool>` draft the YAML for you.
See [`docs/ADD_AN_API.md`](docs/ADD_AN_API.md).

## Design notes
- **Two-layer ELT:** `raw.*` lands untouched JSON (never loses a field when an API enriches
  responses — cf. TastEOS Lesson #76); `ct.*` is the typed warehouse, rebuildable from raw.
- **Idempotent + change-aware:** raw upserts on natural key + content hash; full-snapshot runs
  sweep rows not seen this run (captures deletes). Re-running is always safe.
- **Connector-agnostic:** auth (headers/bearer/basic/query-key), paging, and error format are
  per-connector config. Error contract: 4xx fail fast, 429/5xx retry with backoff.
