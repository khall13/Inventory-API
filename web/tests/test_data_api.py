"""Tests for the Data API endpoints in web/app.py.

No real DB required — covers auth guards, whitelist rejection, and
resilience against a down/missing DB on the export endpoint.

Env vars are set BEFORE the app module is imported so middleware
picks up the right session and Fernet keys.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── env must be set BEFORE app is imported ────────────────────────────────────
from cryptography.fernet import Fernet
_key = Fernet.generate_key().decode()
os.environ.setdefault("ADMIN_PASSWORD", "test-secret-password")
os.environ.setdefault("APP_SECRET_KEY", _key)
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://no/db")

# ── path: web/ and scripts/ are siblings ─────────────────────────────────────
WEB_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = WEB_DIR.parent / "scripts"
sys.path.insert(0, str(WEB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from starlette.testclient import TestClient  # noqa: E402

import app as _app_module  # noqa: E402

PASSWORD = os.environ["ADMIN_PASSWORD"]


def _authed_client():
    """Return a TestClient with a valid session (follows redirects to complete login)."""
    c = TestClient(_app_module.app, raise_server_exceptions=True, follow_redirects=True)
    c.post("/login", data={"password": PASSWORD})
    return c


# ── TC-DATA-001 ───────────────────────────────────────────────────────────────
def test_datasets_unauthenticated_returns_401():
    """GET /api/data/datasets without a session returns 401 JSON."""
    c = TestClient(_app_module.app, raise_server_exceptions=True, follow_redirects=False)
    r = c.get("/api/data/datasets")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    assert r.json().get("detail") == "unauthorized"


# ── TC-DATA-002 ───────────────────────────────────────────────────────────────
def test_preview_unknown_dataset_returns_404():
    """GET /api/data/UNKNOWN/preview after login returns 404 (whitelist rejects it)."""
    c = _authed_client()
    r = c.get("/api/data/UNKNOWN_DATASET_THAT_DOES_NOT_EXIST/preview")
    assert r.status_code == 404, f"expected 404, got {r.status_code}"
    body = r.json()
    assert "not found" in body.get("detail", "").lower(), f"unexpected body: {body}"


# ── TC-DATA-003 ───────────────────────────────────────────────────────────────
def test_export_csv_unknown_dataset_returns_404():
    """GET /api/data/UNKNOWN/export.csv after login returns 404 (whitelist rejects it)."""
    c = _authed_client()
    r = c.get("/api/data/UNKNOWN_DATASET_THAT_DOES_NOT_EXIST/export.csv")
    assert r.status_code == 404, f"expected 404, got {r.status_code}"


# ── TC-DATA-004 ───────────────────────────────────────────────────────────────
def test_export_csv_bad_db_does_not_500():
    """GET /api/data/mw_products/export.csv with an unusable DB does not raise a 500.

    Uses raise_server_exceptions=False so a connection failure surfaces as a
    response rather than an exception in test code. We assert the status is
    not 500 — either the whitelist check fires (200/404) or the DB error is
    caught and returns a graceful empty CSV (200).
    """
    # Only run if mw_products is in the whitelist (it should be from endpoints.yaml)
    whitelist = _app_module._DATASET_WHITELIST
    if "mw_products" not in whitelist:
        print("  SKIP  mw_products not in whitelist — skipping DB resilience check")
        return

    c = TestClient(_app_module.app, raise_server_exceptions=False, follow_redirects=True)
    c.post("/login", data={"password": PASSWORD})
    r = c.get("/api/data/mw_products/export.csv")
    assert r.status_code != 500, (
        f"export.csv raised a 500 on DB failure — error not handled gracefully: {r.text[:200]}"
    )


# ── TC-DATA-005 ───────────────────────────────────────────────────────────────
def test_whitelist_loaded():
    """The whitelist must contain at least the known domains from endpoints.yaml."""
    whitelist = _app_module._DATASET_WHITELIST
    assert isinstance(whitelist, dict), "whitelist is not a dict"
    assert len(whitelist) > 0, "whitelist is empty — endpoints.yaml may not have loaded"
    # mw_products is a stable domain in endpoints.yaml
    assert "mw_products" in whitelist, f"mw_products missing from whitelist: {list(whitelist)}"
    # All values should be schema-qualified raw.* table names
    for domain, table in whitelist.items():
        assert "." in table, f"raw_table for {domain!r} is not schema-qualified: {table!r}"


# ── runner ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_datasets_unauthenticated_returns_401,
        test_preview_unknown_dataset_returns_404,
        test_export_csv_unknown_dataset_returns_404,
        test_export_csv_bad_db_does_not_500,
        test_whitelist_loaded,
    ]
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print("done")
