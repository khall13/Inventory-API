"""Transform raw.recipes (JSONB) → typed ct.* tables (plan §5).

Re-runnable from raw at any time (ELT). Idempotent: upserts the header, replaces
each recipe's child rows.

⚠ FIELD NAMES ARE BEST-EFFORT until a live sample lands (plan Phase 0).
The Crunchtime JSON keys aren't fully documented, so the maps below are derived from
the plan's column list. They are CENTRALIZED here on purpose: run

    python sync.py --domain recipes --pages 1 --dump samples/recipes.json --no-transform
    python transform_recipes.py --coverage   # prints which mapped keys were found/missing

then correct any MISSING keys in ONE place. `run()` also logs a coverage summary so the
first real transform tells you exactly what to fix.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

log = logging.getLogger("crunchtime.transform.recipes")

# Container keys inside each recipeEnhancedDetails[] element.
HEADER_KEY = ["recipeEnhancedHeaderDetails", "recipeHeaderDetails", "header"]
COMPONENTS_KEY = ["recipeEnhancedComponentDetails", "recipeComponentDetails", "components"]
SUBSTITUTES_KEY = ["recipeEnhancedComponentSubstituteDetails", "substituteDetails", "substitutes"]
UPCS_KEY = ["recipeEnhancedUpcDetails", "recipeUpcDetails", "upcs"]
SHIPLOC_KEY = ["recipeEnhancedShipLocationDetails", "recipeShipLocationDetails", "shipLocations"]
ALLERGEN_KEY = ["recipeEnhancedAllergenDetails", "recipeAllergenDetails", "allergens"]
CONCEPT_KEY = ["recipeEnhancedConceptDetails", "recipeConceptDetails", "concepts"]

# ct.recipes column → candidate JSON keys in the header object.
HEADER_MAP: dict[str, list[str]] = {
    "number": ["number", "recipeNumber", "productNumber"],
    "name": ["name", "recipeName"],
    "active_flag": ["activeFlag", "active"],
    "category_name": ["categoryName", "category"],
    "subcategory_name": ["subcategoryName", "subCategoryName", "subCategory"],
    "microcategory_name": ["microcategoryName", "microCategoryName", "microCategory"],
    "plu_number": ["pluNumber"],
    "universal_product_code": ["universalProductCode", "upc"],
    "price": ["price"],
    "batch_quantity": ["batchQuantity"],
    "batch_package_type": ["batchPackageType"],
    "portion_amount": ["portionAmount"],
    "portion_yield": ["portionYield"],
    "inventory_unit_package_type": ["inventoryUnitPackageType"],
    "prep_station_name": ["prepStationName"],
    "effective_date": ["effectiveDate"],
    "expiration_date": ["expirationDate"],
    "shelf_life_duration": ["shelfLifeDuration"],
    "shelf_life_type": ["shelfLifeType"],
    "prep_time_duration": ["prepTimeDuration"],
    "prep_time_type": ["prepTimeType"],
    "recipe_class": ["recipeClass"],
    "production_type": ["productionType"],
    "recipe_status": ["recipeStatus"],
    "recipe_pos_decrement": ["recipePosDecrement"],
    "minimum_batch": ["minimumBatch"],
    "maximum_batch": ["maximumBatch"],
    "nutrition_id": ["nutritionId"],
    "preparation_notes": ["preparationNotes"],
    "plating_instructions": ["platingInstructions"],
    "last_touch_date": ["lastTouchDate"],
}
# Keys carried whole into ct.recipes.units (the unit/yield pairs) until promoted to columns.
UNIT_KEY_PREFIXES = ("recipeUnit", "issueUnit")

COMPONENT_MAP: dict[str, list[str]] = {
    "sequence": ["sequence"],
    "number": ["number", "componentNumber", "productNumber"],
    "name": ["name", "componentName"],
    "quantity": ["quantity"],
    "recipe_package": ["recipePackage", "package"],
    "scaling_factor": ["scalingFactor"],
    "major_ingredient": ["majorIngredient"],
    "pre_production": ["preProduction"],
    "special_instruction1": ["specialInstruction1"],
    "special_instruction2": ["specialInstruction2"],
}
SUBSTITUTE_MAP = {
    "name": ["name"], "number": ["number", "productNumber"], "quantity": ["quantity"],
    "recipe_package": ["recipePackage", "package"], "scaling_factor": ["scalingFactor"],
}

_seen_keys: set[str] = set()  # coverage tracker (populated as we pick())


def first(d: dict, candidates: list[str]):
    if not isinstance(d, dict):
        return None
    for k in candidates:
        if k in d:
            _seen_keys.add(k)
            return d[k]
    return None


def pick(d: dict, col_map: dict[str, list[str]]) -> dict:
    return {col: first(d, cands) for col, cands in col_map.items()}


def get_container(rec: dict, candidates: list[str]):
    for k in candidates:
        if k in rec:
            _seen_keys.add(k)
            return rec[k]
    return None


def as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _units_blob(header: dict) -> str | None:
    blob = {k: v for k, v in header.items() if k.startswith(UNIT_KEY_PREFIXES)} if isinstance(header, dict) else {}
    return json.dumps(blob) if blob else None


def run(conn) -> int:
    """Read every raw.recipes payload and (re)populate ct.* tables. Returns recipe count."""
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM raw.recipes")
        payloads = [row[0] for row in cur.fetchall()]

    count = 0
    with conn.cursor() as cur:
        for rec in payloads:
            header = get_container(rec, HEADER_KEY) or {}
            cols = pick(header, HEADER_MAP)
            num = cols.get("number")
            if num is None:
                log.warning("payload missing recipe number — skipped")
                continue
            num = str(num)
            cols["number"] = num
            cols["units"] = _units_blob(header)

            _upsert(cur, "ct.recipes", cols, conflict="number")

            # Replace this recipe's children (idempotent).
            for tbl in ("recipe_components", "recipe_component_substitutes", "recipe_upcs",
                        "recipe_ship_locations", "recipe_allergens", "recipe_concepts"):
                cur.execute(f"DELETE FROM ct.{tbl} WHERE recipe_number = %s", (num,))

            for comp in as_list(get_container(rec, COMPONENTS_KEY)):
                crow = pick(comp, COMPONENT_MAP)
                crow["recipe_number"] = num
                _insert(cur, "ct.recipe_components", crow)
                for sub in as_list(get_container(comp, SUBSTITUTES_KEY)):
                    srow = pick(sub, SUBSTITUTE_MAP)
                    srow["recipe_number"] = num
                    srow["component_number"] = crow.get("number")
                    _insert(cur, "ct.recipe_component_substitutes", srow)

            _insert_simple(cur, "ct.recipe_upcs", num, get_container(rec, UPCS_KEY),
                           {"universal_product_code": ["universalProductCode", "upc"], "active_flag": ["activeFlag"]})
            _insert_simple(cur, "ct.recipe_ship_locations", num, get_container(rec, SHIPLOC_KEY),
                           {"location_number": ["locationNumber", "number"], "active_flag": ["activeFlag"]})
            _insert_simple(cur, "ct.recipe_allergens", num, get_container(rec, ALLERGEN_KEY),
                           {"nutrition_class": ["nutritionClass", "class"], "nutrition_value": ["nutritionValue", "value"], "active_flag": ["activeFlag"]})
            _insert_simple(cur, "ct.recipe_concepts", num, get_container(rec, CONCEPT_KEY),
                           {"concept_name": ["conceptName", "name"], "active_flag": ["activeFlag"]})
            count += 1

    _log_coverage(payloads)
    return count


def _insert_simple(cur, table, recipe_number, container, col_map):
    for item in as_list(container):
        row = pick(item, col_map)
        row["recipe_number"] = recipe_number
        _insert(cur, table, row)


def _upsert(cur, table, row: dict, *, conflict: str):
    cols = list(row)
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != conflict)
    cur.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def _insert(cur, table, row: dict):
    cols = list(row)
    placeholders = ", ".join(["%s"] * len(cols))
    cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols])


def _log_coverage(payloads: list[dict]) -> None:
    all_mapped = set()
    for m in (HEADER_MAP, COMPONENT_MAP, SUBSTITUTE_MAP):
        for cands in m.values():
            all_mapped.update(cands)
    missing = sorted(all_mapped - _seen_keys)
    log.info("transform key coverage: %d/%d mapped keys seen across %d recipes",
             len(_seen_keys & all_mapped), len(all_mapped), len(payloads))
    if missing:
        log.warning("mapped keys NEVER seen (likely wrong name — verify against a sample): %s",
                    ", ".join(missing))


def _coverage_from_file(path: str) -> None:
    """Offline coverage check against a dumped sample — no DB needed."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    data = json.loads(Path(path).read_text())
    recs = data if isinstance(data, list) else data.get("recipeEnhancedDetails", [])
    for rec in recs:
        header = get_container(rec, HEADER_KEY) or {}
        pick(header, HEADER_MAP)
        for comp in as_list(get_container(rec, COMPONENTS_KEY)):
            pick(comp, COMPONENT_MAP)
            for sub in as_list(get_container(comp, SUBSTITUTES_KEY)):
                pick(sub, SUBSTITUTE_MAP)
    _log_coverage(recs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Recipes transform / offline coverage check")
    ap.add_argument("--coverage", metavar="SAMPLE_JSON",
                    nargs="?", const="samples/recipes.json",
                    help="report mapped-key coverage against a dumped sample (default samples/recipes.json)")
    args = ap.parse_args()
    if args.coverage:
        _coverage_from_file(args.coverage)
    else:
        ap.error("run via sync.py, or pass --coverage <sample.json> for an offline key check")
