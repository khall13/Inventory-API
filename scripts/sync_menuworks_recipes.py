"""MenuWorks recipe sync — two-stage list→detail handler (ADR 0002).

MenuWorks has no bulk "recipes-with-details by page" endpoint, so we:
  Stage 1 — page /business_units/{unit}/products filtered to productType=Recipe → recipe mrns.
  Stage 2 — GET /business_units/{unit}/recipes/{mrn} per mrn (with `include` flags), extract
            data.recipe, and yield in batches.

Exposes `iter_records(client, dom, max_pages=None)` → Iterator[(batch_no, [recipe_dict])], the
record-source contract sync.py expects. Raw landing, change detection, and sync_state are handled
by sync.py exactly as for a paged domain — recipes land in raw.mw_recipes.

Config (endpoints.yaml row): natural_key_path: mrn · label_path: name · raw_table: raw.mw_recipes
Optional row keys: unit_id (default -1), products_path, recipe_path_tmpl, detail_include, batch_size.
"""
from __future__ import annotations

import json
import logging
from typing import Iterator, Protocol
from urllib.parse import quote

from rest_client import ApiError

log = logging.getLogger("handler.mw_recipes")

# Auth failures are not record-level — they should abort the whole run, not be skipped.
AUTH_STATUS = {401, 403}


class _Client(Protocol):
    """The subset of RestClient this handler depends on."""
    def get(self, path: str, params: dict | None = None): ...
    def paginate(self, path: str, *, records_key: str, page_size: int,
                 params: dict | None = None, max_pages: int | None = None): ...

# What to pull on each recipe detail call. All default false at the API, so we must opt in.
DEFAULT_INCLUDE = {
    "ingredients": True,
    "conversions": True,
    "preparationSteps": True,
    "allergens": True,
    "tags": True,
    "nutrientTypes": ["Standard"],
}


def iter_records(client: _Client, dom: dict, max_pages: int | None = None) -> Iterator[tuple[int, list[dict]]]:
    unit = dom.get("unit_id", -1)
    # Pessimistic until a fully clean pass: a skipped recipe means the raw set is incomplete,
    # so sync.py must NOT run the full-snapshot delete-sweep this run (would emit false "removed").
    dom["_sweep_safe"] = False
    products_path = dom.get("products_path", f"/v3/business_units/{unit}/products")
    recipe_tmpl = dom.get("recipe_path_tmpl", f"/v3/business_units/{unit}/recipes/{{mrn}}")
    include = dom.get("detail_include", DEFAULT_INCLUDE)
    page_size = dom.get("page_size", 100)
    batch_size = dom.get("batch_size", 50)

    # ── Stage 1: collect recipe mrns from the products catalog ────────────────
    mrns: list[str] = []
    seen: set[str] = set()
    for _, products in client.paginate(
        products_path,
        records_key=dom.get("products_records_key", "data.products"),
        page_size=page_size,
        params={"filter": {"producttypes": ["Recipe"]}},
        max_pages=max_pages,
    ):
        for p in products:
            # Defensive: don't trust the server filter alone — keep only Recipe products
            # (productType absent ⇒ keep, since we can't tell).
            if str(p.get("productType", "Recipe")).lower() != "recipe":
                continue
            mrn = p.get("mrn")
            if mrn is None:
                continue
            mrn = str(mrn)
            if mrn not in seen:
                seen.add(mrn)
                mrns.append(mrn)

    # --pages N sampling: cap detail fan-out proportional to the page sample.
    if max_pages is not None:
        mrns = mrns[: max_pages * page_size]
    log.info("stage 1: %d Recipe mrns to fetch", len(mrns))

    # ── Stage 2: fetch each recipe's detail, yield in batches ─────────────────
    options = json.dumps({"include": include})
    batch: list[dict] = []
    batch_no = 0
    skipped = 0
    for i, mrn in enumerate(mrns, 1):
        try:
            body = client.get(recipe_tmpl.format(mrn=quote(str(mrn), safe="")), {"options": options})
        except ApiError as e:
            # Auth failures aren't record-level — abort the whole run rather than skip silently.
            if e.status in AUTH_STATUS:
                raise
            log.warning("recipe %s: fetch failed (%s) — skipped", mrn, e)
            skipped += 1
            continue
        recipe = (body.get("data") or {}).get("recipe") if isinstance(body, dict) else None
        if not recipe:
            log.warning("recipe %s: no data.recipe in response — skipped", mrn)
            skipped += 1
            continue
        recipe.setdefault("mrn", mrn)   # ensure the natural key is present
        batch.append(recipe)
        if len(batch) >= batch_size:
            batch_no += 1
            log.info("stage 2: batch %d (%d/%d recipes fetched)", batch_no, i, len(mrns))
            yield batch_no, batch
            batch = []
    if batch:
        batch_no += 1
        yield batch_no, batch

    # Only a complete pass (every mrn landed) is safe to delete-sweep against (Must-Fix #2).
    if skipped == 0 and max_pages is None:
        dom["_sweep_safe"] = True
    elif skipped:
        log.warning("%d recipe(s) skipped this run — suppressing delete-sweep to avoid false 'removed'", skipped)
