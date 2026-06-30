"""Crunchtime ct.* → PreciTaste (TastEOS) upload arrays.

Produces `output/precitaste_items.json` with {stock, inventory, service, menu} arrays in the
shape the `upload-recipes` skill consumes (BLOCK 3 of upload-template.md), so the data is
paste-ready for console upload. Rule-based classification + unit mapping live in
precitaste_mapping.yaml; this script ALSO emits a decision/coverage report so you can tune
the rules against real data.

It deliberately does NOT POST anything: the proven `upload-recipes` pipeline owns the actual
upload (it enforces the F1–F17 guardrails + self-test). This step prepares the input.

Guardrail-aware choices made here:
  • contains[].yields = 1 / quantity_used         (F1 — never divide by batch/servings)
  • contains[].order  = component sequence         (F4)
  • dependency order is implicit in the arrays      (F8: Stock→Inventory→Service→Menu)
  • menu items get a dish/each unit                 (item-hierarchy rule)
  • service items carry production_time seconds     (F10)

Run:  python transform_precitaste.py [--out output/precitaste_items.json]
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml

log = logging.getLogger("transform.precitaste")
ROOT = Path(__file__).resolve().parent
MAPPING_FILE = ROOT / "precitaste_mapping.yaml"
DEFAULT_OUT = ROOT.parent / "output" / "precitaste_items.json"


def _load_mapping() -> dict:
    return yaml.safe_load(MAPPING_FILE.read_text())


def _num(v):
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _truthy(v) -> bool:
    return v is not None and str(v).strip().lower() in ("y", "yes", "true", "1")


def _present(v) -> bool:
    return v is not None and str(v).strip() != ""


PREDICATES = {
    "has_plu_number": lambda r: _present(r.get("plu_number")),
    "has_pos_decrement": lambda r: _truthy(r.get("recipe_pos_decrement")),
    "has_prep_station": lambda r: _present(r.get("prep_station_name")),
}


def run(conn, out_path: str | Path = DEFAULT_OUT) -> int:
    m = _load_mapping()
    unit_map = m.get("unit_map", {})
    dts = m.get("duration_type_seconds", {})
    menu_when = m["classification"]["menu_when_any"]
    service_when = m["classification"]["service_when_any"]
    defaults = m.get("defaults", {})

    report = {"unmapped_units": set(), "zero_qty": [], "counts": {}, "ambiguous": []}

    def to_unit(pkg):
        if pkg is None:
            return None
        key = str(pkg).strip().upper()
        if key in unit_map:
            return unit_map[key]
        report["unmapped_units"].add(str(pkg))
        return str(pkg)   # pass through; flagged in report

    def duration_seconds(amount, type_):
        a = _num(amount)
        if a is None or not type_:
            return None
        return int(a * dts.get(str(type_).strip().upper(), 0))

    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM ct.recipes")
        recipes = {r["number"]: dict(r) for r in cur.fetchall()}
        cur.execute("SELECT * FROM ct.recipe_components ORDER BY recipe_number, sequence")
        comps_by_recipe: dict[str, list] = {}
        for c in cur.fetchall():
            comps_by_recipe.setdefault(c["recipe_number"], []).append(dict(c))

    recipe_numbers = set(recipes)

    def build_contains(recipe_number):
        out = []
        for c in comps_by_recipe.get(recipe_number, []):
            qty = _num(c.get("quantity"))
            if not qty:
                report["zero_qty"].append(f"{recipe_number}:{c.get('name')}")
                yields = 1.0
            else:
                yields = 1.0 / qty
            out.append({
                "ingredient": c.get("name"),
                "unit": to_unit(c.get("recipe_package")),
                "yields": round(yields, 6),
                "order": int(c["sequence"]) if c.get("sequence") is not None else len(out) + 1,
            })
        return out

    stock, inventory, service, menu = {}, [], [], []

    # Stock = leaf components (referenced number is not itself a recipe), deduped by name.
    for comps in comps_by_recipe.values():
        for c in comps:
            if c.get("number") in recipe_numbers:
                continue
            name = c.get("name")
            if name and name not in stock:
                stock[name] = {"display_name": name, "unit": to_unit(c.get("recipe_package")),
                               "shelf_life": None}

    # Recipes → menu / service / inventory.
    for num, r in recipes.items():
        is_menu = any(PREDICATES[p](r) for p in menu_when if p in PREDICATES)
        is_service = (not is_menu) and any(PREDICATES[p](r) for p in service_when if p in PREDICATES)
        contains = build_contains(num)
        name = r.get("name") or num

        if is_menu:
            menu.append({"display_name": name, "unit": defaults.get("menu_unit", "Each"),
                         "contains": contains})
        elif is_service:
            ptime = duration_seconds(r.get("prep_time_duration"), r.get("prep_time_type")) \
                or defaults.get("service_production_seconds", 300)
            service.append({"display_name": name, "unit": "Each",
                            "production_time": ptime,
                            "shelf_life": duration_seconds(r.get("shelf_life_duration"), r.get("shelf_life_type")),
                            "contains": contains})
        else:
            inventory.append({
                "display_name": name, "unit": "Recipe", "on_hand_unit": "Each",
                "unit_conversion_factor": _num(r.get("batch_quantity")) or 1,
                "shelf_life": duration_seconds(r.get("shelf_life_duration"), r.get("shelf_life_type")),
                "increment_step": 0.25, "increment_by_one": False,
                "contains": contains,
            })

    result = {"stock": list(stock.values()), "inventory": inventory,
              "service": service, "menu": menu}
    report["counts"] = {k: len(v) for k, v in result.items()}
    report["unmapped_units"] = sorted(report["unmapped_units"])

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"items": result, "report": report}, indent=2))

    log.info("PreciTaste items: %s", report["counts"])
    if report["unmapped_units"]:
        log.warning("UNMAPPED units (add to precitaste_mapping.yaml + create via unit-management): %s",
                    ", ".join(report["unmapped_units"]))
    if report["zero_qty"]:
        log.warning("%d component(s) had qty=0 → yield defaulted to 1.0 (review): %s",
                    len(report["zero_qty"]), ", ".join(report["zero_qty"][:10]))
    log.info("wrote %s", out_path)
    return sum(report["counts"].values())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Crunchtime ct.* → PreciTaste upload arrays")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    import db
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    conn = db.connect()
    try:
        run(conn, args.out)
    finally:
        conn.close()
