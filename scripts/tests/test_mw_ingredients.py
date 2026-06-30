"""Unit tests for the MenuWorks two-stage ingredient handler (TEST_PLAN TC-028..033).

Pure-logic — no network, no DB. Run: python -m pytest scripts/tests/  (or run directly).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_menuworks_ingredients as h
from rest_client import ApiError


class FakeClient:
    """Stand-in for RestClient: Stage-1 products page + batched Stage-2 filter calls.

    `outcomes` is consumed one per get() call, in chunk order: each is a response body,
    an ApiError to raise, or an empty/missing-data body.
    """

    def __init__(self, products, outcomes):
        self._products = products
        self._outcomes = list(outcomes)
        self.get_calls: list[dict] = []   # captured options dicts, per call

    def paginate(self, path, *, records_key, page_size, params, max_pages=None):
        assert params == {"filter": {"producttypes": ["Ingredient"]}}, params
        yield 1, self._products

    def get(self, path, params=None):
        self.get_calls.append(json.loads(params["options"]))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, ApiError):
            raise outcome
        return outcome


def _products():
    return [
        {"mrn": "100", "productType": "Ingredient", "name": "Salt"},
        {"mrn": "900", "productType": "Recipe", "name": "Marinara"},      # excluded
        {"mrn": "100", "productType": "Ingredient", "name": "dup"},       # deduped
        {"mrn": "300", "productType": "Ingredient", "name": "Pepper"},
    ]


def test_filter_dedup_and_batched_filter_call():  # TC-028 — Stage1 filter+dedup, Stage2 batched
    c = FakeClient(_products(), [{"data": {"ingredients": [{"mrn": "100"}, {"mrn": "300"}]}}])
    dom = {"name": "mw_ingredients", "page_size": 100}
    recs = [r for _, b in h.iter_records(c, dom) for r in b]
    assert [r["mrn"] for r in recs] == ["100", "300"]
    assert len(c.get_calls) == 1                                    # one batched call (Recipe + dup excluded)
    assert c.get_calls[0]["filter"]["mrns"] == ["100", "300"]
    assert dom["_sweep_safe"] is True                              # clean pass


def test_include_flags_sent():  # TC-029
    c = FakeClient([_products()[0]], [{"data": {"ingredients": [{"mrn": "100"}]}}])
    list(h.iter_records(c, {"name": "x", "page_size": 100}))
    opts = c.get_calls[0]
    assert "allergens" in opts["include"] and opts["include"]["nutrientTypes"] == ["Standard"]


def test_mrn_batching():  # TC-030 — mrn_batch_size splits into multiple filter calls
    c = FakeClient(_products(), [{"data": {"ingredients": [{"mrn": "100"}]}},
                                 {"data": {"ingredients": [{"mrn": "300"}]}}])
    dom = {"name": "x", "page_size": 100, "mrn_batch_size": 1}
    batches = list(h.iter_records(c, dom))
    assert len(batches) == 2 and len(c.get_calls) == 2
    assert [c.get_calls[0]["filter"]["mrns"], c.get_calls[1]["filter"]["mrns"]] == [["100"], ["300"]]


def test_missing_data_skipped_suppresses_sweep():  # TC-031
    c = FakeClient([_products()[0]], [{"data": {"ingredients": []}}])   # empty → skip
    dom = {"name": "x", "page_size": 100}
    recs = [r for _, b in h.iter_records(c, dom) for r in b]
    assert recs == []
    assert dom["_sweep_safe"] is False


def test_record_level_error_skipped_not_fatal():  # TC-032 — transient 500 chunk skipped
    c = FakeClient(_products(), [{"data": {"ingredients": [{"mrn": "100"}]}},
                                 ApiError(500, ["boom"])])
    dom = {"name": "x", "page_size": 100, "mrn_batch_size": 1}
    recs = [r for _, b in h.iter_records(c, dom) for r in b]
    assert [r["mrn"] for r in recs] == ["100"]                     # first chunk ok, second skipped
    assert dom["_sweep_safe"] is False


def test_auth_error_aborts():  # TC-033 — 403 must propagate, not be skipped
    c = FakeClient([_products()[0]], [ApiError(403, ["forbidden"])])
    try:
        list(h.iter_records(c, {"name": "x", "page_size": 100}))
        assert False, "expected ApiError to propagate on 403"
    except ApiError as e:
        assert e.status == 403


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("all ingredient handler tests passed")
