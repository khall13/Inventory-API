# MenuWorks Connector — Working Instructions

Scope: all work on the **MenuWorks (Webtrition Web Services 3.0 / Compass)** integration.
These instructions govern this directory and the MenuWorks-specific paths in the shared
`scripts/` engine. They sit **on top of** the repo-root standards in
[`/Users/kenny/Desktop/Claude/CLAUDE.md`](../../../../CLAUDE.md) and the PreciTaste rules in
`PreciTaste/.claude/rules/` — when anything conflicts, the more specific doc wins.

## 1. MenuWorks is a SEPARATE workstream from Crunchtime

They share one connector engine (`scripts/` + `sql/`), but they are **operationally independent**.
Never bundle them in a plan, a run, a deploy, or a commit.

| Concern | MenuWorks | Crunchtime |
|---|---|---|
| Connector block | `menuworks` | `crunchtime` |
| Env vars | `MW_ENV`, `MW_CLIENT_ID`, `MW_IBM_CLIENT_ID` | `CT_ENV`, `CT_AUTH_TOKEN`, `CT_SITENAME`, `CT_USER_ID`, `CT_PASSWORD` |
| Auth | **two** headers (`X-IBM-Client-Id` + `WT-Client-Id`) | four headers (Spring) |
| Base URL | `api.compass-usa.com/stg` (IBM gateway) | `*.net-chef.com` |
| Domains | `mw_*` (`mw_products`, `mw_ingredients`, `mw_menu_items`, `mw_recipes`, …) | `recipes`, Tier-1… |
| Smoke / run / deploy | own `--domain mw_*` runs, own Railway service if deployed | own runs |
| PreciTaste transform | `transform_mw_precitaste.py` + `mw_precitaste_mapping.yaml` | `transform_precitaste.py` |

**Rule:** a change for one connector must not alter the other's behavior. Any shared-engine edit
(`rest_client.py`, `sync.py`, `db.py`) requires the Crunchtime regression check (TEST_PLAN TC-019)
before it's considered done.

## 2. Source of truth for this integration

Read these before touching MenuWorks code:
- **Connection / credentials:** [`MenuWorks_Connection.md`](MenuWorks_Connection.md) — live envs,
  dual-header auth, BU 4990, the `/v3` path-duplication note.
- **API reference:** [`MenuWorks_API_Reference.md`](MenuWorks_API_Reference.md) — endpoint catalog,
  `options_json` paging, `data.*` envelopes. ⚠️ drafted against the **3.1.3** spec; the granted feed
  is **3.0** — reconcile against the attached 3.0 spec doc and a live `--pages 1 --dump` sample.
- **Two-stage recipes:** [`../docs/adr/0002-menuworks-recipe-two-stage.md`](../docs/adr/0002-menuworks-recipe-two-stage.md).
- **Test plan:** [`../TEST_PLAN.md`](../TEST_PLAN.md) — MenuWorks cases are TC-011 → TC-019.

## 3. Use the feature skill for feature work

Run the **`new-feature`** skill at the start of any non-trivial MenuWorks change. It produces the
checklist, branch name (`khallwachs/feature/<TICKET>_…`), TEST_PLAN cases, and conventional-commit
message. Don't write feature code without first going through it. Match the repo-root workflow:
**plan first** (`tasks/todo.md`), check in before implementing, capture lessons in `tasks/lessons.md`.

## 4. Hold the highest senior-engineer standard

Every change is judged by one bar: **would a staff engineer approve this?** Concretely:

- **Root cause, not patch.** No temporary hacks, no "good enough." If a fix feels hacky, stop and
  implement the elegant version. Simplicity first — touch the minimum code that does the job right.
- **Type annotations + docstrings** on every function; **specific exception types** (never bare
  `except`) logged at WARNING+; named arguments for 3+ params; no generic module names
  (`utils.py`/`helpers.py`); imports ordered built-in → third-party → internal.
- **Never mark done without proof.** Run the tests, show the output, diff behavior. A passing
  TEST_PLAN run is the gate — no "should work."
- **Secrets discipline.** Live `X-IBM-Client-Id` / `WT-Client-Id` values live **only** in
  `scripts/.env` (never committed). No hardcoded ids, tokens, or magic strings in code or docs
  beyond the connection reference.
- **Read-only contract.** The MenuWorks API is consumed read-only — **zero** write/`save` calls,
  even against Production (Production is the only env we were granted).
- **Honor what's there.** Reuse the connector engine's patterns (config-driven domains, `raw.*`
  landing, change-aware upsert). Add a domain = add an `endpoints.yaml` row, not a bespoke script.
- **Guardrails are hard rules.** Any PreciTaste upload output must satisfy F1–F17 — the
  `upload-recipes` validators block violations. The transform must stay guardrail-aware
  (`yields = 1/qty`, `order` set, dependency order Stock → Inventory → Service → Menu).

## 5. Definition of done for the live-connection feature

1. `connectors.yaml` carries both headers + the no-trailing-`/v3` Compass base URLs.
2. `sync.py --smoke` returns `200` against Production (auth proven).
3. `--domain mw_products --pages 1 --dump` confirms `records_key`/natural keys vs the live 3.0 JSON
   (and resolves `-1` vs BU `4990`).
4. TEST_PLAN TC-011 → TC-019 pass, including the Crunchtime regression (TC-019).
5. `MenuWorks_API_Reference.md` reconciled to the 3.0 spec; lessons captured.
