"""Tests for GET /api/customers and the connector-filtered GET /api/data/datasets.

No real DB required — covers auth guards, graceful DB-failure behaviour, and
connector filtering on the datasets endpoint.

Env vars are set BEFORE the app module is imported so middleware picks up the
right session and Fernet keys.
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


def _authed_client(raise_server_exceptions: bool = True):
    """Return a TestClient with a valid session (follows redirects to complete login)."""
    c = TestClient(_app_module.app, raise_server_exceptions=raise_server_exceptions, follow_redirects=True)
    c.post("/login", data={"password": PASSWORD})
    return c


# ── TC-CUST-001 ───────────────────────────────────────────────────────────────
def test_customers_unauthenticated_returns_401():
    """GET /api/customers without a session returns 401 JSON."""
    c = TestClient(_app_module.app, raise_server_exceptions=True, follow_redirects=False)
    r = c.get("/api/customers")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    assert r.json().get("detail") == "unauthorized"


# ── TC-CUST-002 ───────────────────────────────────────────────────────────────
def test_customers_bad_db_returns_empty_list_not_500():
    """GET /api/customers with an unusable DB returns 200 and [] — never 500.

    DATABASE_URL points to a non-existent host, so _db.connect() will fail.
    The route must catch this and return an empty list gracefully.
    """
    c = TestClient(_app_module.app, raise_server_exceptions=False, follow_redirects=True)
    c.post("/login", data={"password": PASSWORD})
    r = c.get("/api/customers")
    assert r.status_code == 200, (
        f"expected 200 on DB failure, got {r.status_code}: {r.text[:200]}"
    )
    body = r.json()
    assert isinstance(body, list), f"expected list, got {type(body)}: {body}"
    # With a bad DB the list will be empty — that is the correct graceful response.
    assert body == [], f"expected empty list on DB failure, got: {body}"


# ── TC-CUST-003 ───────────────────────────────────────────────────────────────
def test_datasets_connector_filter_menuworks():
    """GET /api/data/datasets?connector=menuworks returns only menuworks datasets.

    With a bad DB every dataset's count is 0 — that's fine; what we verify is
    that every returned dataset carries connector == "menuworks".
    """
    whitelist = _app_module._DATASET_WHITELIST
    menuworks_domains = [k for k, v in whitelist.items() if v["connector"] == "menuworks"]
    if not menuworks_domains:
        print("  SKIP  no menuworks domains in whitelist — skipping connector filter test")
        return

    c = TestClient(_app_module.app, raise_server_exceptions=False, follow_redirects=True)
    c.post("/login", data={"password": PASSWORD})
    r = c.get("/api/data/datasets?connector=menuworks")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert isinstance(body, list), f"expected list, got {type(body)}"

    returned_names = [ds["dataset"] for ds in body]
    # Every returned dataset must have connector == "menuworks"
    for ds in body:
        assert ds.get("connector") == "menuworks", (
            f"dataset {ds['dataset']!r} has connector={ds.get('connector')!r}, expected 'menuworks'"
        )
    # No crunchtime domain should slip through
    crunchtime_domains = {k for k, v in whitelist.items() if v["connector"] == "crunchtime"}
    leaked = crunchtime_domains & set(returned_names)
    assert not leaked, f"crunchtime domains leaked into menuworks response: {leaked}"


# ── TC-CUST-004 ───────────────────────────────────────────────────────────────
def test_datasets_no_connector_returns_all():
    """GET /api/data/datasets without connector param returns all whitelisted domains."""
    whitelist = _app_module._DATASET_WHITELIST
    if not whitelist:
        print("  SKIP  whitelist empty — skipping")
        return

    c = TestClient(_app_module.app, raise_server_exceptions=False, follow_redirects=True)
    c.post("/login", data={"password": PASSWORD})
    r = c.get("/api/data/datasets")
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == len(whitelist), (
        f"expected {len(whitelist)} datasets (all), got {len(body)}"
    )
    # Every entry must carry a connector field
    for ds in body:
        assert "connector" in ds, f"dataset {ds.get('dataset')!r} missing 'connector' field"


# ── TC-CUST-005 ───────────────────────────────────────────────────────────────
def test_whitelist_carries_connector():
    """_DATASET_WHITELIST values must be dicts with 'table' and 'connector' keys."""
    whitelist = _app_module._DATASET_WHITELIST
    assert isinstance(whitelist, dict), "whitelist is not a dict"
    assert len(whitelist) > 0, "whitelist is empty — endpoints.yaml may not have loaded"
    for domain, entry in whitelist.items():
        assert isinstance(entry, dict), f"{domain!r}: entry is not a dict: {entry!r}"
        assert "table" in entry, f"{domain!r}: missing 'table' key"
        assert "connector" in entry, f"{domain!r}: missing 'connector' key"
        assert "." in entry["table"], (
            f"{domain!r}: raw_table not schema-qualified: {entry['table']!r}"
        )
        assert entry["connector"], f"{domain!r}: connector is empty"


# ── runner ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_customers_unauthenticated_returns_401,
        test_customers_bad_db_returns_empty_list_not_500,
        test_datasets_connector_filter_menuworks,
        test_datasets_no_connector_returns_all,
        test_whitelist_carries_connector,
    ]
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print("done")
