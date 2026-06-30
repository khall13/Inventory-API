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


def make_client(name: str, **overrides) -> RestClient:
    cfg = get_config(name)

    base_urls = cfg["base_urls"]
    env = os.environ.get(cfg.get("env_select", ""), "test") if cfg.get("env_select") else None
    base_url = base_urls.get(env) if env else next(iter(base_urls.values()))
    if not base_url:
        raise ValueError(f"connector {name}: no base_url for env {env!r}; have {list(base_urls)}")

    auth = cfg.get("auth", {})
    headers, query_auth, basic_auth = {}, {}, None
    atype = auth.get("type", "headers")
    if atype == "headers":
        headers = {h: _require_env(var) for h, var in (auth.get("headers") or {}).items()}
    elif atype == "bearer":
        headers = {"Authorization": f"Bearer {_require_env(auth['token_env'])}"}
    elif atype == "basic":
        basic_auth = (_require_env(auth["user_env"]), _require_env(auth["pass_env"]))
    elif atype == "query_key":
        query_auth = {p: _require_env(var) for p, var in (auth.get("params") or {}).items()}
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
