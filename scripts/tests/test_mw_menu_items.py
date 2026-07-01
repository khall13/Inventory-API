"""Unit tests for the MenuWorks rolling-window menu-items handler.

Pure-logic — no network, no DB. Run: python -m pytest scripts/tests/  (or run directly).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_menuworks_menu_items as h


class FakeClient:
    """Captures the paginate params and yields one page of records."""

    def __init__(self, records):
        self._records = records
        self.params = None
        self.records_key = None

    def paginate(self, path, *, records_key, page_size, params, max_pages=None):
        self.params = params
        self.records_key = records_key
        yield 1, self._records


def test_computes_rolling_window(monkeypatch):
    # Freeze "today" so the window is deterministic.
    monkeypatch.setattr(h, "_today", lambda: dt.date(2026, 7, 1))
    c = FakeClient([{"id": 1, "mrn": "100.0", "name": "X"}])
    dom = {"name": "mw_menu_items"}
    recs = [r for _, b in h.iter_records(c, dom) for r in b]
    assert c.params == {"filter": {"startDate": "2026-06-01", "days": 120}}  # 30d lookback, 120d window
    assert c.records_key == "data.menuItems"
    assert [r["id"] for r in recs] == [1]
    assert dom["_sweep_safe"] is True                       # full pass (no page cap)


def test_window_overrides_and_cap(monkeypatch):
    monkeypatch.setattr(h, "_today", lambda: dt.date(2026, 7, 1))
    c = FakeClient([{"id": 9}])
    dom = {"name": "x", "lookback_days": 7, "window_days": 45, "unit_id": 4990}
    list(h.iter_records(c, dom, max_pages=1))
    assert c.params == {"filter": {"startDate": "2026-06-24", "days": 45}}
    assert dom["_sweep_safe"] is False                      # page cap → don't delete-sweep


if __name__ == "__main__":
    # Minimal monkeypatch shim so this runs without pytest too.
    class _MP:
        def setattr(self, obj, name, val): setattr(obj, name, val)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(_MP()); print(f"  ✓ {name}")
    print("all menu-items handler tests passed")
