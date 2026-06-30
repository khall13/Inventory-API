"""Tests for secrets_box.py — Fernet encrypt/decrypt helpers.

Run directly:  python scripts/tests/test_secrets_box.py
Or via pytest: python -m pytest scripts/tests/test_secrets_box.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure scripts/ is on the path so we can import secrets_box
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

# ── Fixture: inject a fresh key before importing secrets_box ──────────────────
_TEST_KEY = Fernet.generate_key().decode()
os.environ["APP_SECRET_KEY"] = _TEST_KEY

import secrets_box  # noqa: E402  (import after env var is set)

_SECRET = "PrehUiCAyYyBr60v5Rhok2zNHxFcVhfIYgbkRaM"


def test_encrypt_decrypt_roundtrip():
    """encrypt then decrypt returns the original plaintext."""
    token = secrets_box.encrypt(_SECRET)
    assert isinstance(token, str), "token should be a str"
    assert token != _SECRET, "token must differ from plaintext"
    result = secrets_box.decrypt(token)
    assert result == _SECRET, f"roundtrip failed: got {result!r}"


def test_encrypt_json_decrypt_json_roundtrip():
    """encrypt_json then decrypt_json round-trips a dict faithfully."""
    payload = {"host": "sql.example.com", "port": 5432, "password": "s3cr3t!"}
    token = secrets_box.encrypt_json(payload)
    assert isinstance(token, str), "token should be a str"
    result = secrets_box.decrypt_json(token)
    assert result == payload, f"json roundtrip failed: got {result!r}"


def test_mask_hides_secret_shows_suffix():
    """mask() ends with the real last 4 chars, contains •, never the full secret."""
    masked = secrets_box.mask(_SECRET)
    last4 = _SECRET[-4:]  # "RaM " — actually last 4 = "RaM\x00"? no, let's be explicit
    # _SECRET = "PrehUiCAyYyBr60v5Rhok2zNHxFcVhfIYgbkRaM"
    # last 4 chars: "RaM" is only 3… count properly
    real_last4 = _SECRET[-4:]  # "kRaM"
    assert masked.endswith(real_last4), (
        f"mask should end with {real_last4!r}, got {masked!r}"
    )
    assert "•" in masked, "mask should contain bullet dots"
    assert _SECRET not in masked, "mask must not contain the full secret"
    # Minimum 4 dots always prepended
    dots = masked[: masked.index(real_last4[-1]) - len(real_last4) + 1]  # everything before suffix
    prefix = masked[: len(masked) - 4]
    assert len(prefix) >= 4, f"fewer than 4 bullet chars: {masked!r}"
    assert all(c == "•" for c in prefix), f"prefix should be all dots: {prefix!r}"


def test_decrypt_without_key_raises_runtime_error():
    """Calling decrypt when APP_SECRET_KEY is unset raises RuntimeError."""
    saved = os.environ.pop("APP_SECRET_KEY", None)
    try:
        # Generate a valid token first (while key is still present — we re-set below)
        # Actually we already popped the key, so we need a pre-made token.
        # Re-set temporarily to make a token, then pop again.
        os.environ["APP_SECRET_KEY"] = _TEST_KEY
        token = secrets_box.encrypt("anything")
        os.environ.pop("APP_SECRET_KEY")

        try:
            secrets_box.decrypt(token)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "APP_SECRET_KEY" in str(e), f"unexpected message: {e}"
    finally:
        # Restore so subsequent tests can run
        if saved is not None:
            os.environ["APP_SECRET_KEY"] = saved
        else:
            os.environ["APP_SECRET_KEY"] = _TEST_KEY


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("all secrets_box tests passed")
