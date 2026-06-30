# Crunchtime API → SQL Integration Plan

**Status:** Code built (2026-06-25) — blocked on external access to run
**Date:** 2026-06-23 (plan) · 2026-06-25 (build)
**Goal:** Connect to the Crunchtime (net-chef) REST API and land data in a SQL database with repeatable full + incremental syncs.

> **Now part of the `Features/Inventory Systems API/` umbrella** — the engine is connector-based
> and serves multiple tools (Crunchtime first; MenuWorks next). This plan documents the
> Crunchtime connector specifically.

## Build status (2026-06-25)
**Implemented and committed** under `Features/Inventory Systems API/` (see [README](../README.md)).
Scope expanded beyond the original draft to cover three follow-on requirements:

- **Connector architecture** — generic paged client with pluggable auth (`rest_client.py`)
  + connector registry (`connectors.yaml`/`.py`). Crunchtime is the first connector
  (4-header auth, Spring errors, `hasNext` paging). Adding a tool = config, not code
  (`add_api.py` scaffolder + `docs/ADD_AN_API.md` runbook).
- **Daily Railway deploy** — `Dockerfile` + `railway.json` (cron `0 8 * * *`, run-once); the
  job runs `--init-db && --domain all --mode incremental --email`.
- **"What changed" email** — content-hash change detection → `change_log` table; SMTP
  summary (Gmail/O365) of new/changed/removed per domain (`notify.py`, `db.py`).
- **PreciTaste export** — `transform_precitaste.py` + `precitaste_mapping.yaml` turn `ct.*`
  into `{stock,inventory,service,menu}` upload arrays for console upload (rule-based,
  guardrail-aware: yields=1/qty, order=sequence, dependency order).
- Recipes schema + self-reporting recipes transform (`transform_recipes.py`).

**Verified (no creds):** all modules compile; YAML configs parse; connector builds with correct
base URL + auth headers + Spring parser; CT→PreciTaste classification, unit map, duration math,
F1 yields correct; email summary renders; OpenAPI scaffolder extracts paged endpoints. (psycopg2
DB paths run on Railway's linux image.)

**Cannot run/validate live until external deps land (§8/§9):**
1. A customer's **Crunchtime Test API credentials** → `.env`.
2. The **Railway Postgres `DATABASE_URL`** + **SMTP creds**.

Then: `sync.py --smoke` → `--pages 1 --dump` (finalize field maps) → `--init-db` →
`--domain recipes --mode full` → `transform_precitaste.py`; deploy via Railway for the daily run.

**Scope decisions (2026-06-23):**
- **Primary goal: Recipes** (`getRecipesEnhancedByPageV2` + `getRecipeLocationsByPage`). Build and validate this domain end-to-end first.
- **Pull-all capable:** the extractor is config-driven so every other domain can be turned on by adding a row to `endpoints.yaml` — no code changes. Recipes ships first; the rest follow.
- **Target DB: PostgreSQL on Railway** (instance already provisioned). Exact schema/conn finalized at build time. *Railway MCP not yet authenticated in this session — need a valid `RAILWAY_API_TOKEN` (or DB connection string) before Phase 2.*
- **Customer/test:** see §8 — we need one customer's API credentials (Test site preferred).

**Docs:**
- Using the APIs — https://developer.crunchtime.com/docs/using-apis
- Getting Started — https://developer.crunchtime.com/docs/getting-started-with-apis
- Machine-readable index (all pages + OpenAPI) — https://developer.crunchtime.com/llms.txt
- Example endpoint — https://developer.crunchtime.com/reference/getallapplicationusersv1usingget

---

## 1. What the API looks like (verified from docs)

### Environments
| Env | Base URL |
|---|---|
| Test | `https://webservices-test.net-chef.com` |
| Production | `https://webservices.net-chef.com` |

Build/develop against **Test** first. The API self-validates that a `-test` URL is paired with a `-test` sitename, so prod creds won't run against test endpoints by accident.

### Authentication — header-based (no OAuth flow)
Every request sends **four headers**. Tokens are minted in Enterprise Manager → Security → grant *Application Users > API Tokens*, then per-user on the **API Tokens** tab (one token per service). Use a dedicated **"API User"** account, not a person's login.

| Header | Value |
|---|---|
| `authenticationtoken` | Test or Production API token |
| `sitename` | URL portion before `.net-chef.com` (e.g. `eattacos-test`) |
| `userid` | Application User ID |
| `password` | Application User password |

Optional: `X-B3-TraceId` for request tracing (worth logging for debugging).

### Request / response shape
- REST GET for reads, POST for writes (we are **read-only** — no `save*` calls).
- `application/json` in and out.
- Status codes: `200` OK, `400` validation, `401` auth, **`429` rate limit**, `500` server, `504` timeout.

### Error body contract (Spring backend)
Non-2xx responses return a Spring `BindingResult`-style **array** of error objects (the `X-B3-TraceId` header confirms Spring Cloud Sleuth):
```json
[
  { "arguments": [{}], "code": "string", "codes": ["string"],
    "defaultMessage": "string", "objectName": "string" }
]
```
Extractor handling:
- Parse as a **list** (multiple validation errors possible). Log `defaultMessage` (human) + `code`/`objectName` (machine) + the `X-B3-TraceId`.
- **`400`/`401` → fail fast, do NOT retry** (retrying resends the same bad/unauthorized request). Surface `defaultMessage`.
- **`429`/`500`/`504` → retry with exponential backoff** (transient).

### Two read patterns per domain
- **`getAll*`** — requires targeted query params (date/location filters). Good for narrow pulls.
- **`get*ByPage`** — paginated, **no required filters**, built for bulk. Params: `pageNumber` (default 1) + `pageSize`. **This is our primary backfill mechanism.** The response includes **`hasNext` (boolean)** and **`totalPages` (int)** — loop `pageNumber` while `hasNext == true` (no cursor/token).

### Incremental sync
Endpoints accept **`minutesSinceUpdate`** (int) — returns only records changed in the last N minutes. This is the key to cheap recurring syncs (see §4). **Cap: must be `>0` and `<43200`** (30 days), so the incremental window can't exceed 30 days — a longer outage forces a full backfill.

### Rate limits
Not numerically documented; `429` exists. Treat as: sequential requests, exponential backoff on 429/504, modest concurrency. (Mirrors our TastEOS rule: sequential + backoff.)

---

## 2. Data domains (full catalog from `llms.txt`)

Every domain offers `getAll` + `getByPage` (+ `save` we ignore). Grouped by priority for a kitchen-ops warehouse:

**Tier 1 — core master + transactional (sync first)**
- **Locations** — `getLocationsByPageV1`
- **Products/Items** — `getCompanyProductsEnhancedByPage`
- **Recipes** — `getRecipesEnhancedByPageV2` (+ `getRecipeLocationsByPage`)
- **Vendors** — `getAllVendorsV1`, `getVendorLocationByPage`, `getVendorProductPricingByPage`
- **Purchase Orders** — `getPurchaseOrdersByPage`
- **Invoices / Receipts** — `getConfirmReceiptsStandardByPage`
- **Inventory** — `getCurrentInventoryByPage`, `getInventoryAdjustmentsByPage` (waste), `getPhysicalInventoryStandardsByPage` (counts)
- **Sales** — `getSalesMixByPage`, `getCheckMenuMixRecordsByPage`

**Tier 2 — labor + forecasting**
- Employees (`getEmployeesByPageV1`), Schedules, Time & Labor (`getTimeClockEnhancedByPage`, supplemental wages)
- Forecasts — daily / hourly / 15-minute sales forecast; Projected Consumption
- Daily Prep — `getDailyPrepsByPage`

**Tier 3 — reference / config (low churn, sync occasionally)**
- Categories, Hierarchies (v2), Storage Locations, Package Types, PBIs, Nutrition, Customers, Customer/Commissary Orders, Location Product Pricing, GL, Budget, Currency, Application Users, UDAs

**Alternative bulk path:** Scheduled Exports (`getAllScheduledExportsV1` → `downloadScheduledExport` → `markScheduledExportV1`) deliver pre-built flat files. Worth evaluating for very large historical backfills vs. paging.

---

## 3. Architecture

```
Crunchtime API ──(HTTPS + 4 headers)──▶  Extractor (Python)
                                              │
                  ┌───────────────────────────┴───────────────┐
                  ▼                                            ▼
        raw landing (JSON as-is)                      typed warehouse tables
        schema: raw.*                                 schema: ct.*
        (one row per API record,                      (normalized columns,
         payload in JSONB, ingested_at)                FKs, indexes)
```

**Two-layer (ELT) design — recommended:**
1. **Raw landing (`raw.*`)** — persist each record's full JSON payload + metadata (`source_endpoint`, `page`, `ingested_at`, natural key). Idempotent upsert on natural key. Never lose a field even if the API adds new ones.
2. **Typed warehouse (`ct.*`)** — flatten the JSON we actually query into proper columns/relations via SQL views or a transform step. Re-runnable from raw.

Why two layers: the API enriches responses over time (we've seen this exact pattern with TastEOS GET enrichment, Lesson #76). Landing raw JSON first means a schema change never drops data — we just extend the transform.

### Tech stack (recommended)
- **Language:** Python 3.11 (consistent with the rest of this workspace; `requests` + `pydantic` for validation).
- **DB:** **PostgreSQL** (JSONB landing + relational warehouse in one engine). **SQLite** acceptable for a local POC.
- **Config:** `.env` for the 4 secrets (never commit) + `endpoints.yaml` describing each domain (path, page param names, natural key, incremental flag).
- **Orchestration:** start as a cron-driven CLI (`python sync.py --domain all --mode incremental`); graduate to Airflow/Prefect only if needed.

### Config-driven extractor (one engine, all domains)
A single paging loop parameterized by `endpoints.yaml` avoids 30 bespoke scripts:
```yaml
- name: products
  path: /companyproduct/v1/getCompanyProductsEnhancedByPage
  page_param: pageNumber
  size_param: pageSize
  page_size: 500
  natural_key: [productId]
  supports_incremental: true        # minutesSinceUpdate
  raw_table: raw.products
```

---

## 4. Sync strategy — **once daily** (per requirement)

One scheduled run per day (Railway cron, off-peak e.g. ~03:00 local). Two modes per domain:

**A. Full daily snapshot — default for master data (recipes, products, vendors, locations).**
Page the full `getByPage` set every run (`pageNumber` from 1 while `hasNext == true`) and upsert into `raw.*` on natural key. These sets are small and low-churn, so a full pull each day is simpler, self-healing (no missed-window risk), and naturally captures deletes via a "not seen this run" sweep. **Recipes use this mode.**

**B. Daily incremental — for high-volume transactional domains (sales, menu mix, inventory adjustments, receipts).**
`minutesSinceUpdate ≈ 1500` (24h + 60min overlap buffer; safely under the 43200 cap). Store `last_run_at` per domain in `sync_state`; idempotent upserts dedupe the overlap. If a run is missed and the gap would exceed ~30 days, fall back to a full snapshot (window cap).

**Resilience:** page sequentially; retry with exponential backoff on `429`/`500`/`503`/`504`; **fail fast on `400`/`401`** (surface `defaultMessage`, alert); log `X-B3-TraceId`; record per-run row counts in `sync_state` for drift detection. A daily cadence makes rate limits a non-issue.

---

## 5. SQL schema (first cut)

- `sync_state(domain PK, last_run_at, last_status, rows_ingested, error)` — drives incremental windows.
- `raw.<domain>(natural_key PK, payload JSONB, source_endpoint, page, ingested_at)` — one per domain.
### Recipes — verified schema (primary domain)
`GET /recipe/v2/getRecipesEnhancedByPage`. **Must pass `includeDetails=true&componentDetails=true`** or components (ingredients) come back empty. Response root: `{ hasNext, totalPages, recipeEnhancedDetails[] }`. Each recipe = a **header** + several child arrays → one parent table + child tables (natural key = recipe `number`):

- `ct.recipes` (from `recipeEnhancedHeaderDetails`) — PK `number`; key cols: `name`, `active_flag` (Y/N), `category_name`/`subcategory_name`/`microcategory_name`, `plu_number`, `universal_product_code`, `price`, `batch_quantity`, `batch_package_type`, `portion_amount`, `portion_yield`, `inventory_unit_package_type`, the `recipe_unit_one..five_package_type`/`_yield` and `issue_unit_one/two_*` pairs, `prep_station_name`, `effective_date`, `expiration_date`, `shelf_life_duration`+`shelf_life_type` (D/H/M), `prep_time_duration`+`prep_time_type`, `recipe_class` (S/I), `production_type` (P/O), `recipe_status`, `recipe_pos_decrement`, `minimum_batch`/`maximum_batch`, `nutrition_id`, `preparation_notes`, `plating_instructions`, `last_touch_date`.
- `ct.recipe_components` (ingredients, `recipeEnhancedComponentDetails[]`) — FK `recipe_number`; cols: `number` (ingredient product #), `name`, `quantity`, `recipe_package` (recipe unit), `scaling_factor`, `sequence`, `major_ingredient`, `pre_production`, `special_instruction1/2`.
- `ct.recipe_component_substitutes` (`…SubstituteDetails[]`) — substitute name/number/qty/package/scale per component.
- `ct.recipe_upcs`, `ct.recipe_ship_locations`, `ct.recipe_allergens` (nutrition class/value), `ct.recipe_concepts` — small child tables, each with `recipe_number` FK + `active_flag`.

> Note: components reference ingredients by `number` — to resolve them to products/sub-recipes, also sync **Products** + reuse recipe `number`s (a component whose `number` matches a recipe = a sub-recipe). This is the Crunchtime analog of TastEOS `contains[]`.

### Other Tier-1 domains (columns finalized from live JSON in Phase 1)
- `ct.locations`, `ct.products`, `ct.vendors` + `ct.vendor_product_pricing`
- `ct.purchase_orders` + `_lines`, `ct.receipts` + `_lines`
- `ct.inventory_current`, `ct.inventory_adjustments`, `ct.physical_counts`
- `ct.sales_mix`, `ct.check_menu_mix`

---

## 6. Phased implementation

- [ ] **Phase 0 — Access & smoke test.** Get Test token + API User from CT admin. Python hits `getAllApplicationUsers` with the 4 headers → confirm `200`. Then pull **1 page of recipes** with `includeDetails=true&componentDetails=true&pageSize=5`, save the raw JSON to `Features/Crunchtime/samples/recipes.json`, and confirm it matches the verified schema in §5.
- [ ] **Phase 1 — Recipes end-to-end (primary goal).** Author `sql/` DDL for `ct.recipes` + child tables (already specced in §5), build the pager for the recipe endpoint, backfill all recipes into Railway Postgres via `raw.recipes` → transform. Validate counts vs. CT UI.
- [ ] **Phase 2 — Extractor engine generalized.** Refactor the recipe pager into the config-driven engine (`scripts/sync.py` + `endpoints.yaml`), `.env` secrets, `raw.*` upsert, `sync_state`, `hasNext` loop, error handling per the Spring contract.
- [ ] **Phase 3 — Schedule (once daily).** Schedule a single daily run on Railway cron (full snapshot for recipes; daily-incremental mode available for transactional domains later), add backoff + alerting + `sync_state` drift check.
- [ ] **Phase 4 — Expand to all domains.** Add the remaining Tier-1/2/3 domains via `endpoints.yaml` rows only (Phase 1 sample-then-transform repeated per domain). Evaluate Scheduled Exports for heavy historical backfills.

---

## 8. Access requirement — we need a customer (resolved direction)

**There is no public Crunchtime sandbox.** Auth is per-customer-instance: every call needs a `sitename` + `authenticationtoken`/`userid`/`password` minted inside *that customer's* Enterprise Manager (Security → Application Users > API Tokens). So we **must have one customer lend us API credentials** to call the API at all — including just to pull recipes.

- **Test is per-site, not a separate sandbox:** same customer at `{sitename}-test.net-chef.com` with its own Test token. Docs say develop against Test first; the API self-validates `-test` URL ↔ `-test` sitename.
- **Preferred:** customer's CT admin creates a dedicated **"API User"** + grants API Token perms, gives us the **Test** sitename + Test token. We build there, then flip base URL + token to Production.
- **If no Test site exists:** we can develop read-only against Production safely — we make **zero `save*` calls**, so there's no write risk — but with more care.

**Action:** pick a customer (ideally one already on both Crunchtime + TastEOS) and request a Test API User + token.

## 9. Remaining open questions

| # | Question | Status |
|---|---|---|
| 1 | Which customer/site lends API creds? | **OPEN** — need Test sitename + token (§8) |
| 2 | Target DB | **RESOLVED** — Postgres on Railway (need token/conn string at build) |
| 3 | Domain scope | **RESOLVED** — recipes first, all-capable via config |
| 4 | History depth for initial backfill | OPEN — recipes are master data (full snapshot fine) |
| 5 | Read-only? | **RESOLVED** — yes, no `save*` writes |
| 6 | Scheduler host / cadence | **RESOLVED** — once daily, Railway cron alongside the DB |
