"""Unit tests for the MenuWorks two-stage recipe handler (TEST_PLAN TC-003..008).

Pure-logic — no network, no DB. Run: python -m pytest scripts/tests/  (or run directly).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_menuworks_recipes as h
from rest_client import ApiError


class FakeClient:
    """Stand-in for RestClient: Stage-1 products page + per-mrn detail with scripted outcomes."""

    def __init__(self, products, detail):
        self._products = products
        self._detail = detail        # mrn -> body | ApiError instance | None-data
        self.detail_calls: list[str] = []

    def paginate(self, path, *, records_key, page_size, params, max_pages=None):
        assert params == {"filter": {"producttypes": ["Recipe"]}}
        yield 1, self._products

    def get(self, path, params=None):
        mrn = path.rsplit("/", 1)[-1]
        self.detail_calls.append(mrn)
        outcome = self._detail.get(mrn)
        if isinstance(outcome, ApiError):
            raise outcome
        return outcome


def _products():
    return [
        {"mrn": "100", "productType": "Recipe", "name": "Marinara"},
        {"mrn": "200", "productType": "Ingredient", "name": "Salt"},     # excluded
        {"mrn": "100", "productType": "Recipe", "name": "dup"},          # deduped
        {"mrn": "300", "productType": "Recipe", "name": "Alfredo"},
    ]


def test_filter_dedup_and_default_unit():  # TC-003 / TC-007
    c = FakeClient(_products(), {"100": {"data": {"recipe": {"mrn": "100", "name": "M"}}},
                                 "300": {"data": {"recipe": {"mrn": "300", "name": "A"}}}})
    dom = {"name": "mw_recipes", "page_size": 100}
    list(h.iter_records(c, dom))
    assert c.detail_calls == ["100", "300"], c.detail_calls            # Ingredient + dup excluded
    assert dom["_sweep_safe"] is True                                  # clean pass


def test_include_flags_sent():  # TC-004
    c = FakeClient([_products()[0]], {"100": {"data": {"recipe": {"mrn": "100"}}}})
    captured = {}
    real_get = c.get
    def spy(path, params=None):
        captured["options"] = params["options"]; return real_get(path, params)
    c.get = spy
    list(h.iter_records(c, {"name": "x", "page_size": 100}))
    assert "preparationSteps" in captured["options"] and "ingredients" in captured["options"]


def test_missing_detail_skipped_suppresses_sweep():  # TC-006 / Must-Fix #2
    c = FakeClient([_products()[0], _products()[3]],
                   {"100": {"data": {"recipe": {"mrn": "100"}}}, "300": {"data": {"recipe": None}}})
    dom = {"name": "mw_recipes", "page_size": 100}
    recs = [r for _, b in h.iter_records(c, dom) for r in b]
    assert [r["mrn"] for r in recs] == ["100"]
    assert dom["_sweep_safe"] is False                                 # 300 skipped → no sweep


def test_record_level_error_skipped_not_fatal():  # TC-008 / Must-Fix #1
    c = FakeClient([_products()[0], _products()[3]],
                   {"100": {"data": {"recipe": {"mrn": "100"}}},
                    "300": ApiError(500, ["boom"])})                    # transient, post-retry
    dom = {"name": "mw_recipes", "page_size": 100}
    recs = [r for _, b in h.iter_records(c, dom) for r in b]
    assert [r["mrn"] for r in recs] == ["100"]                         # 300 skipped, run continued
    assert dom["_sweep_safe"] is False


def test_recurses_into_subrecipes():  # recursive closure — sub-recipes get landed, not stocked
    prods = [{"mrn": "100", "productType": "Recipe", "name": "Parent"}]
    detail = {
        "100": {"data": {"recipe": {"mrn": "100", "name": "Parent", "ingredients": [
            {"mrn": "500", "type": "Recipe", "name": "Sub", "quantity": 1},
            {"mrn": "9", "type": "Ingredient", "name": "Salt", "quantity": 1}]}}},
        # 500 is only reachable by recursing into 100's ingredients (not in the products page)
        "500": {"data": {"recipe": {"mrn": "500", "name": "Sub", "ingredients": [
            {"mrn": "8", "type": "Ingredient", "name": "Water", "quantity": 1}]}}},
    }
    c = FakeClient(prods, detail)
    dom = {"name": "mw_recipes", "page_size": 100}
    recs = [r for _, b in h.iter_records(c, dom) for r in b]
    assert sorted(r["mrn"] for r in recs) == ["100", "500"], [r["mrn"] for r in recs]
    assert "500" in c.detail_calls                                    # fetched via recursion


def test_recursion_can_be_disabled():
    prods = [{"mrn": "100", "productType": "Recipe", "name": "Parent"}]
    detail = {"100": {"data": {"recipe": {"mrn": "100", "ingredients": [
        {"mrn": "500", "type": "Recipe", "name": "Sub", "quantity": 1}]}}},
        "500": {"data": {"recipe": {"mrn": "500"}}}}
    c = FakeClient(prods, detail)
    recs = [r for _, b in h.iter_records(c, {"name": "x", "page_size": 100, "recurse_subrecipes": False}) for r in b]
    assert [r["mrn"] for r in recs] == ["100"] and "500" not in c.detail_calls


def test_auth_error_aborts():  # Must-Fix #1 — 401 must propagate
    c = FakeClient([_products()[0]], {"100": ApiError(401, ["unauthorized"])})
    try:
        list(h.iter_records(c, {"name": "x", "page_size": 100}))
        assert False, "expected ApiError to propagate on 401"
    except ApiError as e:
        assert e.status == 401


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("all handler tests passed")
