# MenuWorks Live Connection — Execution Plan

**Feature:** Reconcile the `menuworks` connector to the provisioned Production API and run the live
smoke + recipe pull.
**Ticket:** TBD
**Status:** 📋 Plan — awaiting review before execution
**Date:** 2026-06-30
**Stack:** Python 3.11 (`scripts/`), PostgreSQL (Railway)
**Governing doc:** [`CLAUDE.md`](CLAUDE.md) · **Sources of truth:**
[`MenuWorks_Connection.md`](MenuWorks_Connection.md) · [`../TEST_PLAN.md`](../TEST_PLAN.md) (TC-011→019)

> **Separation rule (from CLAUDE.md §1):** MenuWorks is independent of Crunchtime. No edit here may
> change Crunchtime behavior. Any shared-engine change is gated by the Crunchtime regression
> (TC-019) before it counts as done.

---

## Goal & scope

Real **Production** credentials landed (Compass IBM gateway, dual headers, BU 4990). The connector
was drafted against the wrong shape (webtrition.com host, single header, 3.1.3 spec). Reconcile it,
prove auth live, confirm the endpoint contract against a real sample, then hand off to the existing
`raw.* → transform_mw_precitaste.py` pipeline.

**Out of scope (separate efforts):** Crunchtime; the Railway Postgres provisioning (tracked
separately — needed for landing/transform but not for the smoke test); QA-environment support.

---

## What changes (exact edits)

### Edit 1 — `scripts/connectors.yaml` → `menuworks` block
- `base_urls.prod`: `https://services.webtrition.com/serviceapi` → `https://api.compass-usa.com/stg`
- `base_urls.qa`:   `https://servicesqa.webtrition.com/serviceapi` → `https://apiqas.compass-usa.com/stg/qas`
  - **Drop the trailing `/v3`** so `/v3/…` endpoint paths join cleanly (`…/stg` + `/v3/business_units`).
- `auth.headers`: add the IBM gateway header **above** the existing one:
  ```yaml
  auth:
    type: headers
    headers:
      X-IBM-Client-Id: MW_IBM_CLIENT_ID   # NEW — IBM gateway id (per-env value)
      WT-Client-Id:    MW_CLIENT_ID        # Webtrition id (same across envs)
  ```
- `smoke_path`: keep `/v3/units_of_measure` (cheap, unpaged) — or switch to `/v3/business_units`
  to match the email's worked example. **Decision:** use `/v3/business_units` (proven in the email).

### Edit 2 — `scripts/.env` (local, never committed) + `.env.example` (var names only)
```
MW_ENV=prod
MW_CLIENT_ID=<WT-Client-Id — see local MenuWorks_Connection.md / scripts/.env>
MW_IBM_CLIENT_ID=<prod X-IBM-Client-Id — see local scripts/.env>   # per-env value
```
> Real id values live ONLY in the gitignored `scripts/.env` and `MenuWorks_Connection.md`
> (never committed). `.env.example` gets the three keys with placeholder values + a comment that
> `X-IBM-Client-Id` is per-env.

### Edit 3 — `MenuWorks_API_Reference.md`
- Update the Base URLs table to the Compass hosts + the no-trailing-`/v3` note.
- Update the Authentication section: **two** headers.
- Flag spec version **3.0** (reconcile endpoint catalog once the 3.0 spec doc is in the folder).

### Edit 4 — `scripts/endpoints.yaml` (only if the live sample requires it)
- Default stays BU `-1`. If TC-014 returns `4001 business unit unavailable`, scope every
  `/business_units/{unitId}/…` row to `4990` and re-pull.

### No code change expected
The dual header is pure config — `rest_client.py` already iterates `auth.headers` (header→env var).
If verification shows otherwise, that becomes its own task (and triggers TC-019 regression).

---

## Phases & checklist

### Phase A — Branch + config (no creds needed)
- [ ] Branch `khallwachs/feature/TBD_menuworks-compass-live-conn`
- [ ] Edit 1 (`connectors.yaml`) — dual header + Compass base URLs + smoke path
- [ ] Edit 2 (`.env` + `.env.example`)
- [ ] Edit 3 (`MenuWorks_API_Reference.md`)
- [ ] **TC-011/012** (no creds): confirm the built request carries both headers and the base+path
      join produces no `/v3/v3`. Prove with a dry-run/print of the prepared request.

### Phase B — Live auth + contract (needs creds; no DB needed)
- [ ] **TC-013** `python sync.py --smoke --connector menuworks` → expect `200`, no `401`
- [ ] **TC-014** `python sync.py --domain mw_products --pages 1 --dump ../samples/mw_products.json --no-transform`
      → confirm `records_key=data.products`, natural key `mrn` against live JSON
- [ ] **TC-015** if `-1` rejected → Edit 4 (scope to BU `4990`), re-pull
- [ ] **TC-017** diff live 3.0 shape vs the 3.1.3 reference; correct `records_key`/keys per sample
- [ ] Repeat the one-page dump for `mw_ingredients`, `mw_menu_items`, and one `mw_recipes` (two-stage)

### Phase C — Land + transform (needs Railway `DATABASE_URL`)
- [ ] `sync.py --init-db` then `--domain mw_* --mode full` backfill into `raw.mw_*`
- [ ] `python transform_mw_precitaste.py` → `output/` upload arrays; review the coverage report
      (unmapped units, zero-qty, unresolved mrns) — **F1–F17 guardrail-aware**

### Phase D — Close out
- [ ] **TC-019 regression:** `sync.py --smoke --connector crunchtime` unchanged; TC-001…010 pass
- [ ] `/review-code` on changed files
- [ ] Reconcile `MenuWorks_API_Reference.md` to the 3.0 spec; capture lessons in `tasks/lessons.md`
- [ ] Commit `feat(menuworks): wire live Compass/Webtrition 3.0 credentials + dual-header auth`

---

## Risks & open questions

| # | Item | Handling |
|---|---|---|
| 1 | `X-IBM-Client-Id` is per-env but the connector resolves one value per env var | Prod-only access today → set `MW_IBM_CLIENT_ID` to the prod value. Env-keyed header values become a task only if QA access is granted. |
| 2 | `-1` mass sync may be rejected for this client | TC-014/015: fall back to BU `4990`. |
| 3 | 3.0 endpoint shapes may differ from the 3.1.3 reference | Don't trust the reference — confirm every `records_key`/natural key via `--pages 1 --dump`. |
| 4 | Phase C blocked on Railway `DATABASE_URL` | Phases A/B fully unblock auth + contract; only landing/transform waits on the DB. |
| 5 | Read-only contract on Production | Zero `save*`/write calls — enforced by only configuring GET domains. |

## Definition of done
Phases A–D complete; **TC-011 → TC-019 pass** (incl. Crunchtime regression); auth proven live;
endpoint contract confirmed against real 3.0 JSON; reference doc reconciled; lessons captured.

## Execution note
On approval, execute **Phase A** immediately (no creds needed) and **Phase B** as soon as the
client ids are in `.env`. Phase C waits on the Railway `DATABASE_URL`.
