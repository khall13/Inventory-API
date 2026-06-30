"""Connector loader — turns a connectors.yaml block + .env secrets into a RestClient."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from rest_client import RestClient

log = logging.getLogger("connectors")
CONNECTORS_FILE = Path(__file__).resolve().parent / "connectors.yaml"


def _load() -> dict:
    return yaml.safe_load(CONNECTORS_FILE.read_text()).get("connectors", {})


def get_config(name: str) -> dict:
    cfg = _load()
    if name not in cfg:
        raise KeyError(f"unknown connector {name!r}; defined: {', '.join(cfg) or '(none)'}")
    return cfg[name]


def _require_env(var: str) -> str:
    val = os.environ.get(var, "")
    if not val:
        raise ValueError(f"env var {var} is required but empty — fill in .env")
    return val


def make_client(
    name: str,
    *,
    env: str | None = None,
    secret_values: dict[str, str] | None = None,
    **overrides,
) -> RestClient:
    """Build and return a RestClient for the named connector.

    Args:
        name: Connector name (key in connectors.yaml).
        env: Optional override for the ``env_select`` value used to pick the
            base URL.  When *None* (default) the env var named by ``env_select``
            is read from ``os.environ`` — identical to the original behaviour.
            Pass an explicit value (e.g. ``"prod"``) to select the URL without
            touching the process environment.  Enables DB-driven per-customer
            credential selection layered over ``.env``.
        secret_values: Optional dict mapping ENV_VAR names to their values.
            When resolving an auth header's ENV_VAR name the lookup order is:
            ``secret_values`` first, then ``os.environ`` as fallback.  When
            *None* (default) only ``os.environ`` is consulted — identical to
            the original behaviour.  Enables DB-driven per-customer credentials
            layered over ``.env``.
        **overrides: Extra kwargs forwarded verbatim to ``RestClient.__init__``.
    """
    cfg = get_config(name)

    base_urls = cfg["base_urls"]
    # Resolve env: explicit override > env_select env var > "test" default
    if env is not None:
        resolved_env = env
    elif cfg.get("env_select"):
        resolved_env = os.environ.get(cfg["env_select"], "test")
    else:
        resolved_env = None
    base_url = base_urls.get(resolved_env) if resolved_env else next(iter(base_urls.values()))
    if not base_url:
        raise ValueError(f"connector {name}: no base_url for env {resolved_env!r}; have {list(base_urls)}")

    def _resolve(var: str) -> str:
        """Look up an env-var name: secret_values first, then os.environ."""
        val = (secret_values or {}).get(var) or os.environ.get(var, "")
        if not val:
            raise ValueError(f"env var {var} is required but empty — fill in .env or provide secret_values")
        return val

    auth = cfg.get("auth", {})
    headers, query_auth, basic_auth = {}, {}, None
    atype = auth.get("type", "headers")
    if atype == "headers":
        headers = {h: _resolve(var) for h, var in (auth.get("headers") or {}).items()}
    elif atype == "bearer":
        headers = {"Authorization": f"Bearer {_resolve(auth['token_env'])}"}
    elif atype == "basic":
        basic_auth = (_resolve(auth["user_env"]), _resolve(auth["pass_env"]))
    elif atype == "query_key":
        query_auth = {p: _resolve(var) for p, var in (auth.get("params") or {}).items()}
    else:
        raise ValueError(f"connector {name}: unsupported auth.type {atype!r}")

    return RestClient(
        base_url,
        headers=headers or None,
        query_auth=query_auth or None,
        basic_auth=basic_auth,
        error_parser=cfg.get("error_parser", "generic"),
        trace_header=cfg.get("trace_header"),
        paging=cfg.get("paging"),
        **overrides,
    )
