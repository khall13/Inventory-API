# ADR 0002 — MenuWorks recipe sync via a two-stage list→detail handler

- **Status:** Accepted
- **Date:** 2026-06-25
- **Relates to:** [ADR 0001](0001-connector-based-elt.md)

## Context
The generic engine (ADR 0001) assumes each domain is *"page a flat list of records."* MenuWorks
recipes break that assumption:

- There is **no bulk "recipes-with-details by page"** endpoint. `/products` returns a lightweight
  catalog (`mrn`, `productType ∈ {Ingredient, Recipe}`, `name`, `url`, `updateDate`); full recipe
  data only comes from **`/business_units/{unitId}/recipes/{mrn}`**, one mrn at a time.
- Recipe detail omits the parts we need (`ingredients`, `conversions`, `preparationSteps`,
  `nutrients`) unless `options.include.*` flags are set — all default `false`.

So fetching all recipes is inherently **N+1**: list the products of type `Recipe`, then fan out a
detail call per mrn. The generic pager cannot express a per-parent path template
(`/recipes/{mrn}`) or a two-phase pull.

## Decision
Introduce a **per-domain `handler` hook**. A domain in `endpoints.yaml` may set
`handler: <module>` instead of relying on the generic pager. The module exposes:

```python
def iter_records(client, dom, max_pages=None) -> Iterator[tuple[int, list[dict]]]
```

`sync.py` uses the handler as the record source and keeps everything else identical (raw upsert,
change detection, `sync_state`, delete-sweep, dump, transform). For MenuWorks recipes,
`sync_menuworks_recipes.iter_records`:

1. **Stage 1** — page `/business_units/{unit}/products` with
   `filter.producttypes=["Recipe"]`, collect deduped recipe `mrn`s.
2. **Stage 2** — for each mrn, GET `/recipes/{mrn}` with the `include` flags, extract
   `data.recipe`, and yield in batches (50) so commits stay bounded.

`unit` defaults to `-1` (all business units / mass sync). `--pages N` caps Stage-1 product pages
for sampling. Rate limiting + retries are inherited from the shared client.

## Consequences
**Positive**
- The N+1 reality is contained in one small, testable module; the generic engine stays simple.
- The `handler` hook is reusable for any future "list→detail" or otherwise non-pageable endpoint.
- Reuses raw landing, change detection, and `sync_state` unchanged — recipes land in
  `raw.mw_recipes` like any other domain.

**Negative / costs**
- N+1 means one HTTP call per recipe — slow and rate-limit-sensitive for large catalogs. Mitigate
  with sequential calls + backoff (already in the client) and, later, incremental via
  `recipe_deltas` so daily runs only refetch changed mrns.
- A `handler` domain ignores some generic knobs (`path`/`records_key` are advisory); documented on
  the row.

## Alternatives considered
- **Force it into the generic pager** — not possible (no per-parent path templating, no two-phase).
- **Only sync the `/products` catalog** — rejected: no ingredients/yield/conversions, so useless
  for the PreciTaste transform.
- **Generic "expand" feature in `rest_client`** — rejected as premature: one consumer today;
  revisit if a third vendor needs the same shape (rule of three).
