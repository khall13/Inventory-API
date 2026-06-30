"""MenuWorks ingredient sync — two-stage list→filter handler (mirrors ADR 0002).

In Webtrition 3.0 the `/ingredients` endpoint is NOT a bulk pager — it rejects an
unfiltered call with `400 "Filter is a required parameter"` and instead takes
`options.filter.mrns=[...]` (confirmed from a live sample, 2026-06-30). So we:

  Stage 1 — page /business_units/{unit}/products filtered to productType=Ingredient → ingredient mrns.
  Stage 2 — GET /business_units/{unit}/ingredients with options.filter.mrns=<chunk> (BATCHED:
            many mrns per call, unlike recipes which are one-per-mrn), extract data.ingredients,
            and yield in batches.

Exposes `iter_records(client, dom, max_pages=None)` → Iterator[(batch_no, [ingredient_dict])], the
record-source contract sync.py expects. Raw landing, change detection, and sync_state are handled
by sync.py exactly as for a paged domain — ingredients land in raw.mw_ingredients.

Config (endpoints.yaml row): natural_key_path: mrn · label_path: name · raw_table: raw.mw_ingredients
Optional row keys: unit_id (default -1), products_path, ingredients_path, detail_include,
mrn_batch_size (mrns per filter call, default 100).
"""
from __future__ import annotations

import json
import logging
from typing import Iterator, Protocol

from rest_client import ApiError

log = logging.getLogger("handler.mw_ingredients")

# Auth failures are not record-level — they should abort the whole run, not be skipped.
AUTH_STATUS = {401, 403}

# Detail opt-ins for each ingredients call. All default false at the API, so we must opt in.
DEFAULT_INCLUDE = {
    "allergens": True,
    "nutrientTypes": ["Standard"],
}


class _Client(Protocol):
    """The subset of RestClient this handler depends on."""
    def get(self, path: str, params: dict | None = None): ...
    def paginate(self, path: str, *, records_key: str, page_size: int,
                 params: dict | None = None, max_pages: int | None = None): ...


def _chunks(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def iter_records(client: _Client, dom: dict, max_pages: int | None = None) -> Iterator[tuple[int, list[dict]]]:
    unit = dom.get("unit_id", -1)
    # Pessimistic until a fully clean pass: a skipped chunk means the raw set is incomplete,
    # so sync.py must NOT run the full-snapshot delete-sweep this run (would emit false "removed").
    dom["_sweep_safe"] = False
    products_path = dom.get("products_path", f"/v3/business_units/{unit}/products")
    ingredients_path = dom.get("ingredients_path", f"/v3/business_units/{unit}/ingredients")
    include = dom.get("detail_include", DEFAULT_INCLUDE)
    page_size = dom.get("page_size", 100)
    mrn_batch_size = dom.get("mrn_batch_size", 100)

    # ── Stage 1: collect Ingredient mrns from the products catalog ────────────
    mrns: list[str] = []
    seen: set[str] = set()
    for _, products in client.paginate(
        products_path,
        records_key=dom.get("products_records_key", "data.products"),
        page_size=page_size,
        params={"filter": {"producttypes": ["Ingredient"]}},
        max_pages=max_pages,
    ):
        for p in products:
            # Defensive: don't trust the server filter alone — keep only Ingredient products
            # (productType absent ⇒ keep, since we can't tell).
            if str(p.get("productType", "Ingredient")).lower() != "ingredient":
                continue
            mrn = p.get("mrn")
            if mrn is None:
                continue
            mrn = str(mrn)
            if mrn not in seen:
                seen.add(mrn)
                mrns.append(mrn)

    # --pages N sampling: cap fan-out proportional to the page sample.
    if max_pages is not None:
        mrns = mrns[: max_pages * page_size]
    log.info("stage 1: %d Ingredient mrns to fetch", len(mrns))

    # ── Stage 2: fetch ingredient detail in batched filter calls ──────────────
    batch_no = 0
    skipped_chunks = 0
    for chunk in _chunks(mrns, mrn_batch_size):
        options = json.dumps({"filter": {"mrns": chunk}, "include": include})
        try:
            body = client.get(ingredients_path, {"options": options})
        except ApiError as e:
            # Auth failures aren't record-level — abort the whole run rather than skip silently.
            if e.status in AUTH_STATUS:
                raise
            log.warning("ingredients chunk of %d: fetch failed (%s) — skipped", len(chunk), e)
            skipped_chunks += 1
            continue
        records = (body.get("data") or {}).get("ingredients") if isinstance(body, dict) else None
        if not records:
            log.warning("ingredients chunk of %d: no data.ingredients in response — skipped", len(chunk))
            skipped_chunks += 1
            continue
        for ing in records:
            if ing.get("mrn") is not None:
                ing["mrn"] = str(ing["mrn"])   # ensure the natural key is a stable string
        batch_no += 1
        log.info("stage 2: batch %d (%d ingredients)", batch_no, len(records))
        yield batch_no, records

    # Only a complete pass (every chunk landed) is safe to delete-sweep against.
    if skipped_chunks == 0 and max_pages is None:
        dom["_sweep_safe"] = True
    elif skipped_chunks:
        log.warning("%d ingredient chunk(s) skipped this run — suppressing delete-sweep to avoid false 'removed'",
                    skipped_chunks)
