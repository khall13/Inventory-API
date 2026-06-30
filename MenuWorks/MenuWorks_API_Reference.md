# MenuWorks (Webtrition) Web Services — API Reference

Digest of the vendor spec, captured for the `menuworks` connector (2026-06-25).

> **Feed version note:** The granted Production feed is **Webtrition Web Services 3.0** delivered
> via the Compass IBM API gateway — **not** 3.1.3. The 3.0 spec doc (when added to this folder)
> is the source of truth. The endpoint catalog below was drafted against the `MenuWorks Webservices
> 3.1.3 API Spec … .docx` and is **provisional** until confirmed against a live
> `--pages 1 --dump` sample on the 3.0 Production feed.

## Overview
RESTful service over the MenuWorks menu platform. Data is requested by **Business Unit (Team)**
and **Location (Venue)**; responses aggregate all menus visible to that team/location that are
marked published. JSON in/out (RFC 8259; double-quoted keys + string values only).

## Base URLs
| Env | URL |
|---|---|
| Production (granted) | `https://api.compass-usa.com/stg/v3/` |
| QA | `https://apiqas.compass-usa.com/stg/qas/v3/` |

Connector uses `MW_ENV` (`prod`\|`qa`) to pick. Because the base URL already ends in `/v3`, the
connector stores it **without** the trailing `/v3` (prod → `https://api.compass-usa.com/stg`,
qa → `https://apiqas.compass-usa.com/stg/qas`) so that the `/v3/…` paths in `endpoints.yaml`
do not produce a doubled segment.

## Authentication
Two required request headers — no OAuth:

| Header | Source env var | Notes |
|---|---|---|
| `X-IBM-Client-Id` | `MW_IBM_CLIENT_ID` | IBM API-gateway client id — value differs between qa and prod environments |
| `WT-Client-Id` | `MW_CLIENT_ID` | Webtrition client id — same across environments |

Connector: `auth.type: headers` with both headers mapped. Only **Production** access has been
granted so far; QA creds have not been issued.

## Response codes
`200` success · `401` unauthorized/invalid `wt-client-id` · `4000` invalid URL/param ·
`4001` business unit unavailable for this client id · `4002` location missing · `4003` required
param missing · `500/5000/5001/5002` server error. (Connector `error_parser: generic`; 4xx
fail fast, 5xx retry with backoff.)

## The `options` query parameter
Many endpoints take an `options` param: a JSON object with `filter` (refine dataset),
`include` (what data comes back), and `page`. Example:
```
?options={"filter":{"startDate":"2024-04-05","days":1},
          "include":{"allergens":true,"icons":true,"ingredients":false}}
```

## Pagination — `options_json` style
Page is nested inside `options`:
```json
{"page": {"offset": "{int: page number}", "limit": "{int: items per page}"}}
```
Each paged response carries:
```json
"paginationDetails": { "offset": <current page>,
  "count": { "recordsReturned": <int>, "totalRecordsAvailable": <int> } }
```
There is **no `hasNext` boolean** — the client stops on a short page (`recordsReturned < limit`)
or when cumulative records reach `totalRecordsAvailable`. Connector: `paging.style: options_json`,
`offset_start: 1`. Records are nested under a `data.*` envelope (dotted `records_key`).

## Business Unit `-1` (mass sync)
Endpoints scoped to `{unitId}` accept **`-1`** to return data across all business units the
account can see — used for full synchronization. Each endpoint in the spec notes whether it
accepts `-1`; confirm per endpoint before relying on it.

## Dates
All dates/datetimes are **EST**, ISO-8601: `YYYY-MM-DD` / `YYYY-MM-DDThh:mm:ss[.nnnnnnn]`.

## Endpoint catalog
Record arrays live under `data.<key>`. ✓ in *Bulk* = pageable list suitable for sync.

| Endpoint | `records_key` | Natural key | Bulk | Notes |
|---|---|---|---|---|
| `/v3/units_of_measure` | `data.unitsOfMeasure` | `id` | ✓ (unpaged) | small reference list; `paged:false` |
| `/v3/business_units` | `data.businessUnits` | `id` | ✓ | team master |
| `/v3/business_units/{unitId}` | — | `id` | — | single team |
| `/v3/business_units/{unitId}/tags` | `data.tags` | `id` | ✓ | |
| `/v3/business_units/{unitId}/locations` | `data.locations` | `id` | ✓ | venues |
| `/v3/business_units/{unitId}/products` | `data.products` | `mrn` | ✓ | accepts `-1` (confirmed) |
| `/v3/business_units/{unitId}/menu_items` | `data.menuItems` | `id` (also `mrn`) | ✓ | confirm `-1` |
| `/v3/business_units/{unitId}/ingredients` | `data.ingredients` | `mrn` | ✓ | full detail (nutrients/allergens) — `include` flags default false |
| `/v3/business_units/{unitId}/product_portions` | `data.product_portions` | `id` (per `mrns`) | ✓ | **snake_case key** (inconsistent with the others) |
| `/v3/business_units/{unitId}/menu_board` | `data.*` | confirm | ✓ | |
| `/v3/business_units/{unitId}/recipes/{mrn}` | `data.recipe` | `mrn` | — | per-recipe fetch (not a bulk list) → synced via the two-stage handler, [ADR 0002](../docs/adr/0002-menuworks-recipe-two-stage.md) |
| `/v3/business_units/{unitId}/locations/{locationId}/menu_templates` | `data.*` | confirm | ✓ | |

### Incremental — `*_deltas` endpoints
MenuWorks does **not** use a `minutesSinceUpdate`-style param; instead it exposes dedicated
delta endpoints driven by a `filter.startDate` in `options`:
- `/v3/business_units/{unitId}/menu_item_deltas`
- `/v3/business_units/{unitId}/recipe_deltas/{mrn}`
- `/v3/business_units/{unitId}/menu_template_deltas`

To sync incrementally, add these as their own `endpoints.yaml` rows with the date filter in
`extra_params` (merged into `options` by the `options_json` pager).

## How it maps into this project
- Connector block: `scripts/connectors.yaml` → `menuworks`.
- Configured domains (`scripts/endpoints.yaml`):
  `mw_units_of_measure`, `mw_products`, `mw_ingredients`, `mw_menu_items` (generic pager) and
  `mw_recipes` (two-stage `handler: sync_menuworks_recipes`, [ADR 0002](../docs/adr/0002-menuworks-recipe-two-stage.md)).
  Confirm each `records_key`/`-1`/key against a live sample via
  `sync.py --domain <name> --pages 1 --dump` once creds land.
- Secrets: `MW_ENV`, `MW_CLIENT_ID`, `MW_IBM_CLIENT_ID` in `.env`.
- Live connection details (credentials structure, BU 4990, path notes): [`MenuWorks_Connection.md`](MenuWorks_Connection.md).
- Execution plan for bringing the connection live: [`MenuWorks_Live_Connection_Plan.md`](MenuWorks_Live_Connection_Plan.md).
- Architecture rationale: [ADR 0001](../docs/adr/0001-connector-based-elt.md). Onboarding more
  endpoints: see [`../docs/ADD_AN_API.md`](../docs/ADD_AN_API.md).

## ✅ Confirmed from a live 3.0 sample (2026-06-30, Compass Production)

A small read-only pull (`samples/mw_*.json`, gitignored) validated the real contract. **Corrections
to the catalog above:**

| Endpoint | Live result | Action for `endpoints.yaml` |
|---|---|---|
| `/v3/units_of_measure` | ✅ 88 recs, `data.unitsOfMeasure`, keys `id, name, shortName` | config correct |
| `/v3/business_units` | ✅ 1 visible, `data.businessUnits` (`id, name, sectorName`) | config correct |
| `/v3/business_units/-1/products` | ✅ **`-1` works**, `data.products`, `mrn` key, **31,118 total** | keep `-1`; use as master index |
| `/v3/business_units/4990/products` | ⚠️ 504 timeout | prefer `-1`, not BU-scoped |
| `/v3/business_units/{u}/ingredients` | ⚠️ **400 "Filter is a required parameter"** | NOT a bulk pager — needs `options.filter.mrns`; fetch in batches |
| `/v3/business_units/{u}/menu_items` | ⚠️ **400 "Filter is a required parameter"** | same — requires a `filter` |

**The master-index model (the real 3.0 sync shape):**
- `products` (`-1`, paged) is the catalog of every `mrn` with a **`productType`** (`Recipe` | `Ingredient`)
  and a `url` to its detail endpoint, e.g.
  `…/recipes/10001` and `…/ingredients?options={"filter":{"mrns":["100079"]}}`.
- **Recipes** → detail via `/recipes/{mrn}` (existing two-stage handler, [ADR 0002](../docs/adr/0002-menuworks-recipe-two-stage.md)).
- **Ingredients** → detail via `/ingredients` with `options.filter.mrns=[…]` in batches — needs a
  filter-driven handler (mirror the recipe two-stage pattern), driven by the `Ingredient`-type mrns
  from `products`. The current `mw_ingredients` plain-pager row will 400 against 3.0.
- **menu_items** likewise requires a `filter`.

Sample product record:
```json
{ "mrn": "10001", "productType": "Recipe", "name": "On the Go: Caprese Parfait",
  "url": "/BusinessUnit/-1/recipes/10001", "updatedDate": "2023-07-17T13:08:44", "cost": 22.85 }
```

### Live landing results + the `/ingredients` 504 (2026-06-30)
First real sync into `raw.*`: **products 500**, **units_of_measure 88**, **recipes 96** (handler
fetched 96/100; 4 recipes returned no `data.recipe`, skipped → sweep suppressed). All clean.

**`/ingredients` is currently broken upstream — NOT our code.** A diagnostic tried the filter call
three ways — 1 mrn no-include, 1 mrn allergens-only, 2 mrns no-include — and **all returned `504
Endpoint request timed out`** (after 5 retries each). The `filter.mrns` shape is *accepted* (no more
`400`); the endpoint itself times out at the IBM gateway for even a single minimal record. Since the
**recipes** detail endpoint (`/recipes/{mrn}`) works fine, this is specific to `/ingredients`, not
detail endpoints generally. The ingredient handler fails safe (retry → skip → suppress delete-sweep).
**Action:** flag to `_DL_US_WebtritionSupport@compass-usa.com` (include an `X-B3-TraceId`); retry
later in case it's transient load. Lightening `include` flags does NOT help (1 mrn no-include 504s).
