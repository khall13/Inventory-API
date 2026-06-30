"""Auth + page-serving tests for web/app.py.

No DB, no network — only auth flows and the static console page.
Env vars are set before the app module is imported so the middleware
picks up the right session key.
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
# Prevent db.connect() from being called by pointing to an unusable DSN
# (the tested routes never call it — they bail out at auth time or serve static)
os.environ.setdefault("DATABASE_URL", "postgresql://no-db-in-tests/dummy")

# ── path: web/ and scripts/ are siblings ─────────────────────────────────────
WEB_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = WEB_DIR.parent / "scripts"
sys.path.insert(0, str(WEB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from starlette.testclient import TestClient  # noqa: E402 (after path setup)

import app as _app_module  # noqa: E402


client = TestClient(_app_module.app, raise_server_exceptions=True, follow_redirects=False)

PASSWORD = os.environ["ADMIN_PASSWORD"]


# ── TC-AUTH-001 ───────────────────────────────────────────────────────────────
def test_login_page_returns_200():
    """GET /login is publicly accessible and returns 200."""
    r = client.get("/login")
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    assert "Password" in r.text, "login form field label not found"


# ── TC-AUTH-002 ───────────────────────────────────────────────────────────────
def test_api_connections_unauthenticated_returns_401():
    """GET /api/connections without a session returns 401 JSON."""
    r = client.get("/api/connections")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    body = r.json()
    assert body.get("detail") == "unauthorized", f"unexpected body: {body}"


# ── TC-AUTH-003 ───────────────────────────────────────────────────────────────
def test_login_wrong_token_returns_401():
    """POST /login with a wrong password returns 401 and re-renders the login page."""
    r = client.post("/login", data={"password": "wrong-password"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    assert "Invalid password" in r.text, "error message not found in response"


# ── TC-AUTH-004 ───────────────────────────────────────────────────────────────
def test_login_correct_token_redirects_and_sets_cookie():
    """POST /login with the correct token → 303 redirect to / with session cookie."""
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303, f"expected 303 redirect, got {r.status_code}"
    assert r.headers.get("location") in ("/", "http://testserver/"), (
        f"unexpected redirect target: {r.headers.get('location')}"
    )
    # A session cookie must have been set
    cookie_names = list(r.cookies.keys())
    assert cookie_names, "no cookies set after login"


# ── TC-AUTH-005 ───────────────────────────────────────────────────────────────
def test_console_page_served_after_login():
    """After login, GET / returns 200 and contains the console HTML."""
    # Use a client that follows redirects and persists cookies across requests
    with TestClient(_app_module.app, raise_server_exceptions=True, follow_redirects=True) as c:
        login_r = c.post("/login", data={"password": PASSWORD})
        # After following redirects the final response should be the console page
        assert login_r.status_code == 200, (
            f"expected 200 after login+redirect, got {login_r.status_code}"
        )
        # Confirm the console HTML was served (not the login page)
        assert "Enter your password to continue" not in login_r.text, (
            "still showing login page after authentication"
        )
        # The console HTML should have some structural marker
        assert "Inventory API" in login_r.text or "<html" in login_r.text.lower(), (
            "console HTML not found in response"
        )


# ── runner ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_login_page_returns_200,
        test_api_connections_unauthenticated_returns_401,
        test_login_wrong_token_returns_401,
        test_login_correct_token_redirects_and_sets_cookie,
        test_console_page_served_after_login,
    ]
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
    print("done")
