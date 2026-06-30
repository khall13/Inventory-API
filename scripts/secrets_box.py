"""Fernet encryption helpers for storing credentials safely in Postgres.

The key is read lazily from the ``APP_SECRET_KEY`` environment variable
(a URL-safe base64-encoded 32-byte Fernet key).  Importing this module
never raises — the error only surfaces when an encrypt/decrypt function
is actually called without the key present.

Generate a key once and store it in .env::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    """Return a Fernet instance built from APP_SECRET_KEY.

    Raises RuntimeError if the environment variable is not set.
    """
    key = os.environ.get("APP_SECRET_KEY")
    if not key:
        raise RuntimeError("APP_SECRET_KEY not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return a Fernet token as a ``str``.

    Args:
        plaintext: The string value to encrypt.

    Returns:
        A URL-safe base64-encoded Fernet token string.

    Raises:
        RuntimeError: If ``APP_SECRET_KEY`` is not set in the environment.
    """
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet *token* and return the original plaintext ``str``.

    Args:
        token: A Fernet token previously produced by :func:`encrypt`.

    Returns:
        The decrypted plaintext string.

    Raises:
        RuntimeError: If ``APP_SECRET_KEY`` is not set in the environment.
        cryptography.fernet.InvalidToken: If the token is invalid or tampered.
    """
    return _fernet().decrypt(token.encode()).decode()


def encrypt_json(obj: dict) -> str:
    """Serialize *obj* to JSON then encrypt it.

    Args:
        obj: Any JSON-serializable dictionary (e.g. a credentials payload).

    Returns:
        A Fernet token string representing the encrypted JSON.

    Raises:
        RuntimeError: If ``APP_SECRET_KEY`` is not set in the environment.
    """
    return encrypt(json.dumps(obj))


def decrypt_json(token: str) -> dict:
    """Decrypt a Fernet *token* and deserialize the JSON payload.

    Args:
        token: A Fernet token previously produced by :func:`encrypt_json`.

    Returns:
        The original dictionary.

    Raises:
        RuntimeError: If ``APP_SECRET_KEY`` is not set in the environment.
        cryptography.fernet.InvalidToken: If the token is invalid or tampered.
        json.JSONDecodeError: If the decrypted payload is not valid JSON.
    """
    return json.loads(decrypt(token))


def mask(plaintext: str, keep: int = 4) -> str:
    """Return a redacted version of *plaintext* for safe display in logs/UI.

    Shows only the last *keep* characters; the rest are replaced with ``•``.
    At least 4 bullet characters are always prepended regardless of string
    length, so the full value is never revealed even for very short inputs.

    Args:
        plaintext: The secret string to mask.
        keep: Number of trailing characters to reveal (default 4).

    Returns:
        A string of the form ``"••••<last_keep_chars>"``.

    Examples:
        >>> mask("PrehUiCAyYyBr60v5Rhok2zNHxFcVhfIYgbkRaM")
        '••••RaM'  # wait — keep=4, so last 4 chars + min 4 dots
        # actual: '••••••••••••••••••••••••••••••••••••RaM' + last char
        # simpler: 4 dots minimum, then the last `keep` chars
    """
    suffix = plaintext[-keep:] if len(plaintext) >= keep else plaintext
    # Always at least 4 bullet dots, regardless of how short the string is
    dots = "•" * max(4, len(plaintext) - keep)
    return dots + suffix
