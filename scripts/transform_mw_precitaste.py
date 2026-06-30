"""MenuWorks raw.* → PreciTaste (TastEOS) upload arrays.

Reads the landed MenuWorks JSON (raw.mw_recipes, raw.mw_ingredients, raw.mw_units_of_measure,
raw.mw_menu_items) and writes output/mw_precitaste_items.json = {stock, inventory, service, menu}
in the shape the `upload-recipes` skill consumes. Sibling of transform_precitaste.py (Crunchtime).

Guardrail-aware: contains[].yields = 1/qty (F1), order set (F4), arrays in dependency order (F8),
menu unit resolves to dish/each. The actual upload runs through the `upload-recipes` pipeline,
which enforces F1–F17 + self-test. Classification + unit mapping live in mw_precitaste_mapping.yaml.

Like the Crunchtime transform, this prints a decision/coverage report (unmapped units, zero-qty,
unresolved ingredient mrns) so the mapping can be tuned against real data. Run:
  python transform_mw_precitaste.py [--out output/mw_precitaste_items.json]
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml

log = logging.getLogger("transform.mw_precitaste")
ROOT = Path(__file__).resolve().parent
MAPPING_FILE = ROOT / "mw_precitaste_mapping.yaml"
DEFAULT_OUT = ROOT.parent / "output" / "mw_precitaste_items.json"


def _num(v):
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _raw_payloads(conn, table: str) -> list[dict]:
    """Return payloads from a raw.* table, or [] if it doesn't exist yet."""
    import psycopg2.errors
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT payload FROM {table}")
            return [r[0] for r in cur.fetchall()]
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            log.warning("%s not found — skipping (run the sync for it first)", table)
            return []


def build(conn) -> dict:
    """Pure transform: read raw.* tables via *conn*, return ``{"items": {...}, "report": {...}}``.

    Sets in *report* are already converted to sorted lists so the result is JSON-serialisable.
    """
    m = yaml.safe_load(MAPPING_FILE.read_text())
    unit_map = {k.upper(): v for k, v in m.get("unit_map", {}).items()}
    defaults = m.get("defaults", {})
    report = {"unmapped_units": set(), "zero_qty": [], "unresolved_mrn": set(), "counts": {}}

    def to_unit(name):
        if not name:
            return defaults.get("stock_unit", "Each")
        if name.upper() in unit_map:
            return unit_map[name.upper()]
        report["unmapped_units"].add(name)
        return name

    # Reference data
    uom_by_id = {}
    for u in _raw_payloads(conn, "raw.mw_units_of_measure"):
        if u.get("id") is not None:
            uom_by_id[str(u["id"])] = u.get("name")
    menu_mrns = {str(mi.get("mrn")) for mi in _raw_payloads(conn, "raw.mw_menu_items")
                 if mi.get("mrn") is not None}
    recipes = {str(r["mrn"]): r for r in _raw_payloads(conn, "raw.mw_recipes") if r.get("mrn")}

    # Stock = ingredient mrns referenced by recipes that are NOT themselves recipes, deduped.
    stock: dict[str, dict] = {}

    def contains_for(recipe: dict) -> list[dict]:
        out = []
        for i, ing in enumerate(recipe.get("ingredients") or [], 1):
            mrn = str(ing.get("mrn")) if ing.get("mrn") is not None else None
            name = ing.get("name")
            qty = _num(ing.get("quantity"))
            if not qty:
                report["zero_qty"].append(f"{recipe.get('mrn')}:{name}")
                yields = 1.0
            else:
                yields = round(1.0 / qty, 6)
            unit_name = uom_by_id.get(str(ing.get("unitId"))) if ing.get("unitId") is not None else None
            out.append({"ingredient": name, "unit": to_unit(unit_name),
                        "yields": yields, "order": i})
            # Register a Stock item for leaf ingredients (mrn not a recipe).
            if mrn and mrn not in recipes and name and name not in stock:
                stock[name] = {"display_name": name, "unit": to_unit(unit_name), "shelf_life": None}
            elif mrn and mrn not in recipes and not name:
                report["unresolved_mrn"].add(mrn)
        return out

    inventory, menu = [], []
    for mrn, r in recipes.items():
        name = r.get("name") or mrn
        contains = contains_for(r)
        if mrn in menu_mrns:
            menu.append({"display_name": name, "unit": defaults.get("menu_unit", "Each"),
                         "contains": contains})
        else:
            portion_unit = to_unit(r.get("standardPortionUnitName"))
            inventory.append({
                "display_name": name,
                "unit": defaults.get("inventory_production_unit", "Recipe"),
                "on_hand_unit": portion_unit,
                "unit_conversion_factor": _num(r.get("yield")) or 1,
                "shelf_life": None,            # MenuWorks recipe carries no shelf life → set on review
                "increment_step": 0.25, "increment_by_one": False,
                "contains": contains,
            })

    result = {"stock": list(stock.values()), "inventory": inventory, "service": [], "menu": menu}
    report["counts"] = {k: len(v) for k, v in result.items()}
    report["unmapped_units"] = sorted(report["unmapped_units"])
    report["unresolved_mrn"] = sorted(report["unresolved_mrn"])

    if not recipes:
        log.warning("no raw.mw_recipes rows — run `sync.py --domain mw_recipes` first")

    return {"items": result, "report": report}


def run(conn, out_path: str | Path = DEFAULT_OUT) -> int:
    """Build the transform and write the result to *out_path*. Returns total item count."""
    payload = build(conn)
    report = payload["report"]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))

    log.info("MenuWorks → PreciTaste: %s", report["counts"])
    if report["unmapped_units"]:
        log.warning("UNMAPPED units (add to mw_precitaste_mapping.yaml): %s", ", ".join(report["unmapped_units"]))
    if report["zero_qty"]:
        log.warning("%d ingredient(s) had qty=0 → yield defaulted to 1.0: %s",
                    len(report["zero_qty"]), ", ".join(report["zero_qty"][:10]))
    log.info("wrote %s", out_path)
    return sum(report["counts"].values())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="MenuWorks raw.* → PreciTaste upload arrays")
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
