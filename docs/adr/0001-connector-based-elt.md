# ADR 0001 — Connector-based ELT for inventory-system integrations

- **Status:** Accepted
- **Date:** 2026-06-25
- **Context owner:** Onboarding / Data
- **Supersedes:** the original single-purpose "Crunchtime → SQL" draft

## Context
We need to pull data from multiple third-party kitchen/menu systems (Crunchtime/net-chef,
MenuWorks/Webtrition, more later) into one Postgres warehouse, run it unattended daily, email
what changed, and reshape recipe data into the PreciTaste/TastEOS upload format. The vendor APIs
differ in auth (multi-header vs single-header), pagination (flat `pageNumber/pageSize` vs a JSON
`options.page`), response envelopes (flat array vs nested `data.*`), and incremental support
(`minutesSinceUpdate` vs date-window `*_deltas`). They also enrich responses over time.

## Decision
Build **one config-driven ELT engine** with these layers:

1. **Generic REST client** (`rest_client.py`) — pluggable auth (`headers`/`bearer`/`basic`/
   `query_key`), pluggable paging styles (`flat`, `options_json`), dotted `records_key`, and a
   uniform error contract (4xx fail-fast, 429/5xx exponential backoff).
2. **Connector registry** (`connectors.yaml`/`.py`) — each tool is a config block (base URLs,
   auth, paging, error parser). Crunchtime and MenuWorks are connectors, not code.
3. **Domain config** (`endpoints.yaml`) — each endpoint is a row (path, `records_key`,
   `natural_key_path`, `raw_table`, mode). Adding an endpoint = adding a row.
4. **Two-layer storage (ELT)** — `raw.*` lands the untouched JSON (idempotent upsert on natural
   key + content hash; full-snapshot delete-sweep); `ct.*` is the typed warehouse, rebuildable
   from raw by re-running a transform.
5. **Orchestration** — `sync.py` CLI; daily Railway cron container; SMTP "what changed" email
   driven by a `change_log` populated from content-hash diffs.
6. **PreciTaste export** — `transform_precitaste.py` turns the typed layer into TastEOS upload
   arrays; the upload itself stays in the proven `upload-recipes` pipeline (F1–F17 guardrails).

## Consequences
**Positive**
- New tool onboarding is mostly config + a runbook (`docs/ADD_AN_API.md`); proven by adding
  MenuWorks after Crunchtime with no client rewrite.
- Raw-first means a vendor adding fields never drops data (cf. TastEOS Lesson #76).
- Idempotent + change-aware: re-runs are safe; deletes are captured; the email is cheap.

**Negative / costs**
- An auth scheme or paging style the client doesn't model needs a new branch in
  `rest_client.py`/`connectors.py` (small, localized — that's how `options_json` was added).
- Endpoints that don't fit "page a flat list" (per-parent detail fan-out) can't be a plain row —
  see [ADR 0002](0002-menuworks-recipe-two-stage.md).
- Field mappings into `ct.*` can't be finalized until a live sample lands (mitigated by
  self-reporting coverage in the transforms).

## Alternatives considered
- **One bespoke script per vendor** — rejected: O(N) duplication, drift, no shared resilience.
- **Off-the-shelf ELT (Airbyte/Fivetran)** — rejected for now: no connectors for these niche
  APIs, and the PreciTaste-specific transform + guardrails need custom code anyway.
- **Direct API→TastEOS (skip the warehouse)** — rejected: loses history, change detection, and
  the ability to re-transform without re-pulling.
