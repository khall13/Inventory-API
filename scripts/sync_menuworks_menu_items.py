"""MenuWorks menu-items sync — rolling date-window handler.

The 3.0 /menu_items endpoint requires an options.filter DATE WINDOW (startDate + days).
A static date goes stale, so this handler computes the window from the run date:
    startDate = today - lookback_days   (default 30, to catch recently-published menus)
    days      = window_days             (default 120, forward coverage)
Then it pages the normal options_json way and yields (page_no, records) — the same
record-source contract sync.py expects. Menu items land in raw.mw_menu_items (natural
key = id; the id recurs per date/location, so the batch-dedup upsert collapses to one
row per menu-item definition).

Config (endpoints.yaml row): natural_key_path: id · raw_table: raw.mw_menu_items
Optional row keys: unit_id (default -1), lookback_days, window_days, path, records_key.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator, Protocol

log = logging.getLogger("handler.mw_menu_items")


class _Client(Protocol):
    def paginate(self, path: str, *, records_key: str, page_size: int,
                 params: dict | None = None, max_pages: int | None = None): ...


def _today():
    # Isolated so tests can monkeypatch the run date deterministically.
    return datetime.now(timezone.utc).date()


def iter_records(client: _Client, dom: dict, max_pages: int | None = None) -> Iterator[tuple[int, list[dict]]]:
    unit = dom.get("unit_id", -1)
    path = dom.get("path", f"/v3/business_units/{unit}/menu_items")
    lookback = dom.get("lookback_days", 30)
    window = dom.get("window_days", 120)
    start = (_today() - timedelta(days=lookback)).isoformat()
    params = {"filter": {"startDate": start, "days": window}}
    log.info("menu_items rolling window: startDate=%s days=%d (lookback=%d)", start, window, lookback)

    dom["_sweep_safe"] = False
    for page_no, records in client.paginate(
        path,
        records_key=dom.get("records_key", "data.menuItems"),
        page_size=dom.get("page_size", 100),
        params=params,
        max_pages=max_pages,
    ):
        yield page_no, records
    # A full pass (no page cap) is safe to delete-sweep against — it drops menu items that
    # rolled out of the current window, which is the intended behaviour.
    if max_pages is None:
        dom["_sweep_safe"] = True
