# Runbook: add another API

The extractor is connector-based, so onboarding a new tool (Toast, R365, Square, …) is
mostly config. You need its **docs** (ideally an OpenAPI/Swagger JSON) and **credentials**.

## 1. Read the docs — answer these five questions
| Question | Where it goes |
|---|---|
| Base URL(s) (test vs prod)? | `connectors.yaml` → `base_urls` |
| How does auth work? (bearer token / API key header / basic / multi-header) | `connectors.yaml` → `auth` |
| How does pagination work? (param names + how you know there's a next page) | `connectors.yaml` → `paging` |
| For each endpoint: path, the array field in the response, the per-record unique id | `endpoints.yaml` rows |
| Does it support an "updated since" filter (for cheap incremental)? | `endpoints.yaml` → `supports_incremental` |

## 2. Scaffold the connector
```bash
python add_api.py connector --name toast --base-url https://api.toast.com --auth bearer
```
Paste the printed block under `connectors:` in `connectors.yaml`, fix `smoke_path` + `paging`,
and add the env vars it names to `.env`.

**Supported auth types:** `headers` (map header→env), `bearer` (token env),
`basic` (user/pass envs), `query_key` (query param→env). If the API needs an auth scheme not
in this list, add a branch to `make_client()` in `connectors.py` — that's the only code change.

**Supported paging styles** (`connectors.yaml` → `paging.style`):
- `flat` (default) — flat `pageNumber`/`pageSize` params; ends on `hasNext`/`totalPages`/empty.
- `options_json` — page nested in a JSON `options` query param (`{"page":{"offset":N,"limit":M}}`);
  ends via a `paginationDetails` count or a short page.
A new style needs a `_paginate_*` branch in `rest_client.py` (that's how `options_json` was added).

**`records_key` may be a dotted path** for nested envelopes (e.g. `data.products`).
Set `paged: false` on a domain for small reference endpoints that take no paging params.

### Worked example — MenuWorks (the second connector)
MenuWorks 3.1 (`connectors.yaml` → `menuworks`) exercises all of the above: a single
`WT-Client-Id` header, `options_json` paging, `data.*` dotted record keys, `unitId=-1` for
mass sync, and `paged: false` on `mw_units_of_measure`. See its rows in `endpoints.yaml`.

## 3. Scaffold endpoints (if there's an OpenAPI spec)
```bash
python add_api.py endpoints --openapi https://api.toast.com/openapi.json --connector toast
```
It finds paged GET endpoints and drafts `endpoints.yaml` rows. **Review every row** — the
`records_key` and `natural_key_path` are guesses. No spec? Hand-write rows using the template
comments at the top of `endpoints.yaml`.

## 4. Prove it, then sample
```bash
python sync.py --smoke --connector toast                       # expect "SMOKE OK"
python sync.py --domain <name> --pages 1 --dump samples/<name>.json --no-transform
```
Open the sample, confirm `records_key` and `natural_key_path` are right, fix the row.

## 5. Backfill + schedule
```bash
python sync.py --init-db
python sync.py --domain <name> --mode full
```
The daily Railway job (`--domain all`) now includes it automatically. Want it in the typed
warehouse too? Add a `ct.<name>` table in `sql/` + a `transform_<name>.py` exposing `run(conn)`
and set `transform: transform_<name>` on the domain (optional — raw landing works without it).

## 6. (Optional) PreciTaste upload mapping
If the new API carries recipe/item data destined for TastEOS, extend
`transform_precitaste.py` + `precitaste_mapping.yaml` the same way recipes are handled.
