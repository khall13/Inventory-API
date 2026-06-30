# TEST PLAN — Inventory Systems API extractor

- **Feature:** Complete the MenuWorks connector (ingredients + menu_items bulk domains, two-stage recipe handler)
- **Ticket:** TBD
- **Date:** 2026-06-25
- **Stack:** Python 3.11 (`scripts/`), PostgreSQL, Railway cron

## Prerequisites
- `pip install -r scripts/requirements.txt`
- For live tests: `.env` with `MW_ENV`, `MW_CLIENT_ID`, `DATABASE_URL` (blocked until creds land).
- Logic tests below run with **no creds** (fake client / synthetic payloads).

## Test scenarios
| ID | Priority | Type | Scenario | Expected |
|---|---|---|---|---|
| TC-001 | P0 | happy | `mw_ingredients` row pages via `options_json` | records land under `data.ingredients`; PK `mrn`; idempotent re-run |
| TC-002 | P0 | happy | `mw_menu_items` row | records under `data.menuItems`; PK `id`; label = `name` |
| TC-003 | P0 | happy | Recipe handler Stage 1 filters products to `producttypes=["Recipe"]` | only Recipe mrns collected; Ingredient mrns excluded; deduped |
| TC-004 | P0 | happy | Recipe handler Stage 2 fetches `/recipes/{mrn}` with `include` flags | one detail call per mrn; `data.recipe` extracted; yielded in batches of 50 |
| TC-005 | P1 | edge | `--pages 1` caps Stage-1 product pages | detail fan-out limited to that sample; no full N+1 |
| TC-006 | P1 | edge | Recipe detail missing/`data.recipe` null | logged WARNING, that mrn skipped, run continues |
| TC-007 | P1 | edge | `unit_id` default | handler targets `/business_units/-1/...` (mass sync) when unset |
| TC-008 | P0 | failure | Stage-2 GET 401/403 (auth) | `ApiError` propagates → run aborts (✅ `test_auth_error_aborts`) |
| TC-008b | P0 | failure | Stage-2 GET 404/500 (record-level) | logged WARNING, mrn skipped, run continues, `_sweep_safe=False` (✅ `test_record_level_error_skipped_not_fatal`) |
| TC-009 | P1 | edge | `product_portions` snake_case `records_key` | dotted key `data.product_portions` resolves (regression for casing) |
| TC-010 | P0 | regression | Existing Crunchtime `recipes` + generic pager unchanged | flat paging still works; handler branch only triggers when `handler:` set |

## Logic verification (no creds) — run
```bash
cd scripts
python -m py_compile *.py tests/*.py
python tests/test_mw_recipes.py     # TC-003/004/006/007/008/008b — all pass
```
Committed: `scripts/tests/test_mw_recipes.py` (fake client; covers filter/dedup, include flags,
missing-detail skip, record-level error skip + sweep suppression, and auth-error abort).

## Regression placeholder
- TC-010: re-run `sync.py --smoke --connector crunchtime` and the existing connector-build /
  options_json pager unit checks from session 2; confirm no behavior change.

---

## Feature: MenuWorks live connection (Webtrition 3.0 / Compass IBM gateway) — TBD

- **Date:** 2026-06-30
- **Why:** real Production credentials landed (Compass `api.compass-usa.com/stg`, dual headers
  `X-IBM-Client-Id` + `WT-Client-Id`, BU 4990). Reconcile the connector (built against
  webtrition.com / single-header / 3.1.3) and run the live smoke + recipe pull.
- **Logic tests (TC-011, TC-012) run with no DB.** Live tests (TC-013+) need `.env` + `DATABASE_URL`.

| ID | Priority | Type | Scenario | Expected |
|---|---|---|---|---|
| TC-011 | P0 | happy | Connector builds `menuworks` with both `X-IBM-Client-Id` and `WT-Client-Id` headers from env | request headers contain both ids; base URL has no trailing `/v3` |
| TC-012 | P0 | happy | Base URL + `/v3/…` path join produces no `/v3/v3` duplication | `prod` base `…/stg` + path `/v3/business_units` → `…/stg/v3/business_units` |
| TC-013 | P0 | happy | `sync.py --smoke` against Production proves auth | `200` on `/v3/business_units` (or `units_of_measure`); no `401` |
| TC-014 | P0 | happy | `--domain mw_products --pages 1 --dump` against BU `-1` | records land under `data.products`; confirm `records_key`/natural key vs live JSON |
| TC-015 | P1 | edge | `-1` mass-sync rejected for this client (`4001`) | fall back to BU `4990`; endpoints.yaml `{unitId}` scoped to 4990; pull succeeds |
| TC-016 | P1 | edge | `X-IBM-Client-Id` wrong/missing | API `401`; connector fails fast (no retry on 4xx) with clear message |
| TC-017 | P1 | edge | 3.0 spec endpoint shape differs from 3.1.3 reference | `--pages 1 --dump` reveals mismatch; `records_key`/keys corrected per live sample |
| TC-018 | P0 | failure | Only Production granted; QA `X-IBM-Client-Id` used against prod | rejected; documents that the per-env IBM id must match the env |
| TC-019 | regression | — | Crunchtime connector + existing `options_json` pager unaffected by the dual-header change | `sync.py --smoke --connector crunchtime` unchanged; TC-001…010 still pass |

---

## Feature: Setup Console (web service — login + connections + SMTP) — TBD

- **Date:** 2026-06-30 · **Stack:** FastAPI + Jinja2 + Postgres (`web/`).
- Auth/page tests run with no DB (`web/tests/test_app.py`). DB + live tests need `DATABASE_URL` + creds.

| ID | Pri | Type | Scenario | Expected | Status |
|---|---|---|---|---|---|
| TC-020 | P0 | happy | Create a MenuWorks connection via `POST /api/connections` | row persisted; `secrets` Fernet-encrypted; response masked | ✅ live |
| TC-021 | P0 | happy | `POST /api/connections/{id}/test` with the live Compass creds | `200` / `status=ok`; matches CLI smoke | ✅ live (detail=`…/stg`) |
| TC-022 | P0 | failure | Test with a bad IBM id | `fail`; detail surfaced; never 500 | ✅ live (`fail`, "HTTP 403") |
| TC-023 | P1 | edge | `GET /api/connections` never returns plaintext secrets | values masked; plaintext absent | ✅ live |
| TC-024 | P0 | happy | `POST /api/smtp/test` | graceful on no-config; sends `ok` with creds | ✅ graceful ("SMTP not configured"); full send pending creds |
| TC-025 | P0 | failure | `/api/*` without a session | `401 unauthorized` | ✅ live + unit |
| TC-026 | P0 | happy | **password** login → session cookie → console served | wrong pw `401`; correct → `303` + cookie; `/` serves wired console | ✅ live + unit |
| TC-027 | regression | — | `make_client()` default path (no `env`/`secret_values`) unchanged | TC-011/012 + crunchtime smoke still pass | ✅ (menuworks + mw_recipes green) |

---

## Feature: MenuWorks ingredient two-stage handler + Data browser — TBD

- **Date:** 2026-06-30 · `sync_menuworks_ingredients.py`, `web/` Data page.

| ID | Pri | Type | Scenario | Expected | Status |
|---|---|---|---|---|---|
| TC-028 | P0 | happy | Stage1 filters products to Ingredient + dedups; Stage2 one batched `filter.mrns` call | only Ingredient mrns; `data.ingredients` extracted | ✅ unit |
| TC-029 | P1 | happy | `include` flags sent on the ingredients call | `allergens` + `nutrientTypes:[Standard]` in options | ✅ unit |
| TC-030 | P1 | edge | `mrn_batch_size` splits mrns into multiple filter calls | N calls, each with its mrn chunk | ✅ unit |
| TC-031 | P1 | edge | chunk returns no `data.ingredients` | skipped; `_sweep_safe=False` | ✅ unit |
| TC-032 | P0 | failure | transient `500` on a chunk | skipped, run continues; sweep suppressed | ✅ unit |
| TC-033 | P0 | failure | `401/403` on a chunk | `ApiError` propagates (abort) | ✅ unit |
| TC-034 | — | live | live `/ingredients` filter call against Prod | **filter accepted (no 400)**; endpoint currently **504s** (upstream timeout) — handler skips gracefully; data pull deferred | ⚠️ API 504 |
| TC-035 | P0 | failure | `GET /api/data/datasets` without session | `401` | ✅ unit |
| TC-036 | P0 | failure | unknown dataset name in `/api/data/{ds}/...` | `404` (whitelist rejects) | ✅ unit |
| TC-037 | P0 | edge | `export.csv` with DB down/empty table | no `500`; empty/headers-only CSV | ✅ unit |
| TC-038 | P0 | happy | console serves Data panel + Download-CSV wired to `/api/data/*` | panel + CSV link present | ✅ unit + serve check |
