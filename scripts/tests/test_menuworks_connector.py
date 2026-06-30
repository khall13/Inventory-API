"""Connector-level tests for the menuworks block (TEST_PLAN TC-011..012).

Pure-logic — no network, no DB, no real creds.
Run: python scripts/tests/test_menuworks_connector.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import connectors  # get_config, make_client
from rest_client import RestClient


# ── TC-011 ────────────────────────────────────────────────────────────────────
def test_menuworks_dual_auth_headers():  # TC-011
    """make_client('menuworks') injects BOTH X-IBM-Client-Id and WT-Client-Id."""
    os.environ["MW_ENV"] = "prod"
    os.environ["MW_IBM_CLIENT_ID"] = "dummy-ibm-id"
    os.environ["MW_CLIENT_ID"] = "dummy-wt-id"

    client = connectors.make_client("menuworks")

    assert isinstance(client, RestClient)
    session_headers = dict(client._session.headers)

    assert session_headers.get("X-IBM-Client-Id") == "dummy-ibm-id", (
        f"X-IBM-Client-Id missing or wrong: {session_headers}"
    )
    assert session_headers.get("WT-Client-Id") == "dummy-wt-id", (
        f"WT-Client-Id missing or wrong: {session_headers}"
    )


# ── TC-012 ────────────────────────────────────────────────────────────────────
def test_menuworks_prod_url_no_double_v3():  # TC-012
    """Prod base URL + /v3/business_units path produces the right URL with no /v3/v3 duplication."""
    os.environ["MW_ENV"] = "prod"
    os.environ["MW_IBM_CLIENT_ID"] = "dummy-ibm-id"
    os.environ["MW_CLIENT_ID"] = "dummy-wt-id"

    client = connectors.make_client("menuworks")

    # RestClient stores the stripped base_url; joining a path is base_url + path
    expected = "https://api.compass-usa.com/stg/v3/business_units"
    actual = client.base_url + "/v3/business_units"

    assert actual == expected, (
        f"URL mismatch — got {actual!r}, want {expected!r}"
    )
    assert "/v3/v3" not in actual, f"Double /v3 detected in {actual!r}"


# ── runner ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("all menuworks connector tests passed")
