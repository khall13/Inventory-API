"""Format API tests for /api/format/preview.

No real DB — the unusable DATABASE_URL causes db.connect() to fail, which the
route catches and returns the graceful empty payload (status 200, never 500).
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

# Use raise_server_exceptions=False so that a 500 surfaces as a response
# (not an exception) — the test asserts we return 200, never 500.
client = TestClient(_app_module.app, raise_server_exceptions=False, follow_redirects=True)


def _login():
    """POST /login and return the session cookie dict."""
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code in (200, 303), f"login failed: {r.status_code}"


# ── TC-FORMAT-001: unauthenticated → 401 ─────────────────────────────────────
def test_format_preview_requires_auth():
    fresh = TestClient(_app_module.app, raise_server_exceptions=False, follow_redirects=False)
    r = fresh.get("/api/format/preview")
    assert r.status_code == 401
    body = r.json()
    assert "detail" in body


# ── TC-FORMAT-002: authenticated + unusable DB → 200 graceful empty ──────────
def test_format_preview_returns_200_on_db_error():
    _login()
    r = client.get("/api/format/preview")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
    body = r.json()
    # Top-level keys must always be present
    assert "items" in body, "missing 'items' key"
    assert "report" in body, "missing 'report' key"
    items = body["items"]
    for key in ("stock", "inventory", "service", "menu"):
        assert key in items, f"missing items['{key}']"
    # With an unusable DB the route either sets 'error' or returns empty counts
    counts_empty = all(len(items[k]) == 0 for k in ("stock", "inventory", "service", "menu"))
    has_error = "error" in body
    assert counts_empty or has_error, (
        "expected empty counts or 'error' field when DB is unreachable"
    )


if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))
