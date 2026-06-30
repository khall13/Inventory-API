"""Scaffold helper for onboarding a new API (see docs/ADD_AN_API.md).

Two modes:
  1. Draft a connectors.yaml block:
       python add_api.py connector --name toast --base-url https://api.toast.com --auth bearer
  2. Draft endpoints.yaml rows from an OpenAPI/Swagger spec (finds paged GET endpoints):
       python add_api.py endpoints --openapi https://api.toast.com/openapi.json --connector toast

It PRINTS YAML for you to review and paste — it never edits config in place. Drafts are
best-effort (records_key / natural_key are guesses); confirm against a real response with
`sync.py --domain <name> --pages 1 --dump`.
"""
from __future__ import annotations

import argparse
import json
import sys

PAGE_HINTS = {"pagenumber", "page", "pageindex", "offset", "skip"}
SIZE_HINTS = {"pagesize", "size", "limit", "perpage", "per_page", "pagelimit"}
KEY_HINTS = ("id", "number", "code", "uuid", "guid", "key")


def _load_spec(src: str) -> dict:
    if src.startswith(("http://", "https://")):
        import requests
        return requests.get(src, timeout=60).json()
    with open(src) as f:
        return json.load(f)


def _records_key_from_response(op: dict, spec: dict) -> str | None:
    """Best-effort: name of the array property in the 2xx response schema."""
    resp = (op.get("responses") or {}).get("200") or (op.get("responses") or {}).get("2XX") or {}
    content = resp.get("content", {})
    schema = (content.get("application/json", {}) or {}).get("schema", {})
    schema = _deref(schema, spec)
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for name, sub in props.items():
        if isinstance(sub, dict) and sub.get("type") == "array":
            return name
    return None


def _deref(schema: dict, spec: dict) -> dict:
    if isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"].lstrip("#/").split("/")
        node = spec
        for p in ref:
            node = node.get(p, {})
        return node
    return schema or {}


def cmd_endpoints(args) -> int:
    spec = _load_spec(args.openapi)
    paths = spec.get("paths", {})
    rows = []
    for path, methods in paths.items():
        op = methods.get("get")
        if not op:
            continue
        params = [p.get("name", "") for p in op.get("parameters", [])]
        lower = {p.lower(): p for p in params}
        page_param = next((lower[h] for h in PAGE_HINTS if h in lower), None)
        if not page_param:
            continue  # not a paged endpoint
        size_param = next((lower[h] for h in SIZE_HINTS if h in lower), "pageSize")
        records_key = _records_key_from_response(op, spec) or "REVIEW_records_key"
        name = (op.get("operationId") or path.strip("/").replace("/", "_")).lower()
        rows.append((name, path, page_param, size_param, records_key))

    if not rows:
        print("# No paged GET endpoints found (looked for page-like params).", file=sys.stderr)
        return 1

    print(f"# Draft endpoints.yaml rows for connector '{args.connector}' "
          f"({len(rows)} paged endpoints). REVIEW natural_key_path + records_key against a sample.\n")
    print("domains:")
    for name, path, pp, sp, rk in rows:
        print(f"""  - name: {name}
    connector: {args.connector}
    path: {path}
    records_key: {rk}
    page_size: 200
    natural_key_path: REVIEW_id_field   # the field uniquely identifying each record
    raw_table: raw.{name}
    supports_incremental: false
    mode: full""")
        if pp != "pageNumber" or sp != "pageSize":
            print(f"    # NOTE: this endpoint pages via {pp}/{sp} — set those in the connector's paging block")
    return 0


def cmd_connector(args) -> int:
    auth_blocks = {
        "headers": "    type: headers\n    headers:\n      X-Header-Name: ENV_VAR_NAME",
        "bearer": f"    type: bearer\n    token_env: {args.name.upper()}_API_TOKEN",
        "basic": f"    type: basic\n    user_env: {args.name.upper()}_USER\n    pass_env: {args.name.upper()}_PASSWORD",
        "query_key": f"    type: query_key\n    params:\n      apikey: {args.name.upper()}_API_KEY",
    }
    print(f"""# Draft connectors.yaml block — review, then paste under `connectors:`
  {args.name}:
    base_urls:
      prod: {args.base_url}
    env_select: {args.name.upper()}_ENV
    auth:
{auth_blocks[args.auth]}
    error_parser: generic
    smoke_path: /REVIEW_a_cheap_GET_path
    paging:
      page_param: pageNumber
      size_param: pageSize
      has_next_field: hasNext
      total_pages_field: totalPages

# Then add the matching env vars to .env, and run:
#   python sync.py --smoke --connector {args.name}""")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scaffold a new API connector / endpoints")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("connector", help="draft a connectors.yaml block")
    c.add_argument("--name", required=True)
    c.add_argument("--base-url", required=True)
    c.add_argument("--auth", choices=["headers", "bearer", "basic", "query_key"], default="bearer")
    c.set_defaults(func=cmd_connector)

    e = sub.add_parser("endpoints", help="draft endpoints.yaml rows from an OpenAPI spec")
    e.add_argument("--openapi", required=True, help="URL or path to an OpenAPI/Swagger JSON spec")
    e.add_argument("--connector", required=True)
    e.set_defaults(func=cmd_endpoints)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
