"""Setup Console — FastAPI backend.

Routes
------
GET  /login                 Login page (redirect to / if already authed)
POST /login                 Verify admin token; set session; redirect to /
GET  /logout                Clear session; redirect to /login
GET  /                      Serve the setup console HTML (auth required)

GET  /api/connections       List connections (secrets masked)
POST /api/connections       Create a connection
PUT  /api/connections/{id}  Update a connection
DEL  /api/connections/{id}  Delete a connection
POST /api/connections/{id}/test  Live smoke-test a connection

GET  /api/smtp              Get SMTP config (password masked)
PUT  /api/smtp              Upsert SMTP config
POST /api/smtp/test         Send a test email; update last_test_*

Config from env
---------------
ADMIN_PASSWORD   — required; the sign-in password (change it in Railway → inventory-web vars)
APP_SECRET_KEY   — Fernet key (also used as SESSION_SECRET fallback)
SESSION_SECRET   — cookie signing key (falls back to APP_SECRET_KEY)
DATABASE_URL     — psycopg2 DSN (used by db.connect())
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import os
import secrets
import smtplib
import sys
from pathlib import Path

# ── path wiring: web/ and scripts/ are siblings ──────────────────────────────
WEB_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = WEB_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import psycopg2
import psycopg2.extras
import psycopg2.sql
import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware

import db as _db
import secrets_box  # encrypt/decrypt/mask helpers

log = logging.getLogger("web.app")

# ── Data whitelist: domain -> {table, connector} (loaded from endpoints.yaml) ─
def _load_dataset_whitelist() -> dict[str, dict]:
    """Return {domain_name: {"table": raw_table, "connector": connector}} for every
    domain that declares raw_table.  The connector falls back to defaults.connector."""
    ep_path = SCRIPTS_DIR / "endpoints.yaml"
    try:
        with ep_path.open() as fh:
            data = yaml.safe_load(fh)
        default_connector: str = (data.get("defaults") or {}).get("connector", "")
        whitelist: dict[str, dict] = {}
        for domain in data.get("domains", []):
            name = domain.get("name")
            raw_table = domain.get("raw_table")
            if name and raw_table:
                connector = domain.get("connector") or default_connector
                whitelist[name] = {"table": raw_table, "connector": connector}
        return whitelist
    except Exception as exc:
        log.warning("Failed to load endpoints.yaml for data whitelist: %s", exc)
        return {}

_DATASET_WHITELIST: dict[str, dict] = _load_dataset_whitelist()

# ── app factory ───────────────────────────────────────────────────────────────
app = FastAPI(title="Inventory API — Setup Console", docs_url=None, redoc_url=None)

_session_key = os.environ.get("SESSION_SECRET") or os.environ.get("APP_SECRET_KEY", "")
if not _session_key:
    log.warning("SESSION_SECRET / APP_SECRET_KEY not set — sessions will not survive restarts")
    _session_key = "dev-insecure-key"

app.add_middleware(SessionMiddleware, secret_key=_session_key)

# ── Jinja2 templates ──────────────────────────────────────────────────────────
_templates = Environment(
    loader=FileSystemLoader(str(WEB_DIR / "templates")),
    autoescape=select_autoescape(["html"]),
)

# ── helpers ───────────────────────────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def _is_authed(request: Request) -> bool:
    return request.session.get("auth") is True


def _require_page_auth(request: Request) -> None | RedirectResponse:
    """For page routes: redirect to /login if not authed."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return None


def _require_api_auth(request: Request) -> None | JSONResponse:
    """For /api/* routes: 401 JSON if not authed."""
    if not _is_authed(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _mask_secrets(raw: dict) -> dict:
    """Return a copy of a secrets dict with each value masked."""
    return {k: secrets_box.mask(v) for k, v in raw.items()}


def _row_to_dict(row) -> dict:
    """Convert a psycopg2 RealDictRow / plain row to a regular dict."""
    return dict(row)


# ── SECRET_ENV_MAP — maps DB secret key → ENV_VAR name per connector ─────────
SECRET_ENV_MAP: dict[str, dict[str, str]] = {
    "menuworks": {
        "wt_client_id": "MW_CLIENT_ID",
        "ibm_client_id": "MW_IBM_CLIENT_ID",
    },
    "crunchtime": {
        "sitename": "CT_SITENAME",
        "authenticationtoken": "CT_AUTH_TOKEN",
        "userid": "CT_USER_ID",
        "password": "CT_PASSWORD",
    },
}

# ── console HTML (served statically behind login) ────────────────────────────
_CONSOLE_HTML: str | None = None


def _console_html() -> str:
    global _CONSOLE_HTML
    if _CONSOLE_HTML is None:
        _CONSOLE_HTML = (WEB_DIR / "console.html").read_text()
    return _CONSOLE_HTML


# ─────────────────────────────────────────────────────────────────────────────
# Auth routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_authed(request):
        return RedirectResponse("/", status_code=303)
    tmpl = _templates.get_template("login.html")
    return HTMLResponse(tmpl.render(error=None))


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if ADMIN_PASSWORD and secrets.compare_digest(password, ADMIN_PASSWORD):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    tmpl = _templates.get_template("login.html")
    return HTMLResponse(tmpl.render(error="Invalid password — try again."), status_code=401)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Console page
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def console(request: Request):
    guard = _require_page_auth(request)
    if guard:
        return guard
    return HTMLResponse(_console_html())


# ─────────────────────────────────────────────────────────────────────────────
# Connections API
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/connections")
async def list_connections(request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard
    conn = _db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, customer_name, connector, environment, enabled, business_unit, "
                "secrets, created_at, updated_at, last_test_at, last_test_status, last_test_detail "
                "FROM config.connections ORDER BY customer_name, connector, environment"
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    result = []
    for row in rows:
        try:
            raw = secrets_box.decrypt_json(row["secrets"])
            row["secrets"] = _mask_secrets(raw)
        except Exception:
            row["secrets"] = {}
        # Convert non-serialisable types for JSON
        for k in ("created_at", "updated_at", "last_test_at"):
            if row.get(k) is not None:
                row[k] = row[k].isoformat()
        row["id"] = str(row["id"])
        result.append(row)
    return JSONResponse(result)


@app.post("/api/connections", status_code=201)
async def create_connection(request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard
    body = await request.json()
    encrypted = secrets_box.encrypt_json(body.get("secrets") or {})
    conn = _db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO config.connections
                    (customer_name, connector, environment, enabled, business_unit, secrets)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, customer_name, connector, environment, enabled, business_unit,
                          secrets, created_at, updated_at, last_test_at, last_test_status, last_test_detail
                """,
                (
                    body.get("customer_name"),
                    body.get("connector"),
                    body.get("environment"),
                    body.get("enabled", True),
                    body.get("business_unit"),
                    encrypted,
                ),
            )
            row = _row_to_dict(cur.fetchone())
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.warning("create_connection failed: %s", exc)
        return JSONResponse({"detail": str(exc)}, status_code=400)
    finally:
        conn.close()

    try:
        raw = secrets_box.decrypt_json(row["secrets"])
        row["secrets"] = _mask_secrets(raw)
    except Exception:
        row["secrets"] = {}
    row["id"] = str(row["id"])
    for k in ("created_at", "updated_at", "last_test_at"):
        if row.get(k) is not None:
            row[k] = row[k].isoformat()
    return JSONResponse(row, status_code=201)


@app.put("/api/connections/{conn_id}")
async def update_connection(conn_id: str, request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard
    body = await request.json()

    db_conn = _db.connect()
    try:
        # Fetch existing row first so we can preserve secrets if not provided
        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT secrets FROM config.connections WHERE id = %s",
                (conn_id,),
            )
            existing = cur.fetchone()
        if existing is None:
            return JSONResponse({"detail": "not found"}, status_code=404)

        # Determine new secrets: preserve existing if body omits or sends empty
        new_secrets_raw = body.get("secrets")
        if new_secrets_raw:
            encrypted = secrets_box.encrypt_json(new_secrets_raw)
        else:
            encrypted = existing["secrets"]  # preserve existing encrypted blob

        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE config.connections SET
                    customer_name = COALESCE(%s, customer_name),
                    connector     = COALESCE(%s, connector),
                    environment   = COALESCE(%s, environment),
                    enabled       = COALESCE(%s, enabled),
                    business_unit = COALESCE(%s, business_unit),
                    secrets       = %s,
                    updated_at    = %s
                WHERE id = %s
                RETURNING id, customer_name, connector, environment, enabled, business_unit,
                          secrets, created_at, updated_at, last_test_at, last_test_status, last_test_detail
                """,
                (
                    body.get("customer_name"),
                    body.get("connector"),
                    body.get("environment"),
                    body.get("enabled"),
                    body.get("business_unit"),
                    encrypted,
                    _now(),
                    conn_id,
                ),
            )
            row = cur.fetchone()
        db_conn.commit()
    except Exception as exc:
        db_conn.rollback()
        log.warning("update_connection %s failed: %s", conn_id, exc)
        return JSONResponse({"detail": str(exc)}, status_code=400)
    finally:
        db_conn.close()

    if row is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    row = _row_to_dict(row)
    try:
        raw = secrets_box.decrypt_json(row["secrets"])
        row["secrets"] = _mask_secrets(raw)
    except Exception:
        row["secrets"] = {}
    row["id"] = str(row["id"])
    for k in ("created_at", "updated_at", "last_test_at"):
        if row.get(k) is not None:
            row[k] = row[k].isoformat()
    return JSONResponse(row)


@app.delete("/api/connections/{conn_id}", status_code=204)
async def delete_connection(conn_id: str, request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard
    db_conn = _db.connect()
    try:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM config.connections WHERE id = %s", (conn_id,))
        db_conn.commit()
    except Exception as exc:
        db_conn.rollback()
        log.warning("delete_connection %s failed: %s", conn_id, exc)
        return JSONResponse({"detail": str(exc)}, status_code=400)
    finally:
        db_conn.close()
    return Response(status_code=204)


@app.post("/api/connections/{conn_id}/test")
async def test_connection(conn_id: str, request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard

    db_conn = _db.connect()
    try:
        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT connector, environment, secrets FROM config.connections WHERE id = %s",
                (conn_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        db_conn.close()
        log.warning("test_connection fetch %s failed: %s", conn_id, exc)
        return JSONResponse({"status": "fail", "detail": str(exc)})

    if row is None:
        db_conn.close()
        return JSONResponse({"detail": "not found"}, status_code=404)

    connector_name = row["connector"]
    environment = row["environment"]
    try:
        secret_plain = secrets_box.decrypt_json(row["secrets"])
    except Exception as exc:
        db_conn.close()
        log.warning("test_connection decrypt %s failed: %s", conn_id, exc)
        return JSONResponse({"status": "fail", "detail": f"decrypt error: {exc}"})

    # Build secret_values: {ENV_VAR: secret_value}
    env_map = SECRET_ENV_MAP.get(connector_name, {})
    secret_values = {
        env_var: secret_plain[db_key]
        for db_key, env_var in env_map.items()
        if db_key in secret_plain
    }

    status, detail = "fail", "unknown error"
    try:
        from connectors import get_config, make_client
        from rest_client import ApiError

        cfg = get_config(connector_name)
        client = make_client(connector_name, env=environment, secret_values=secret_values)

        # Reuse sync.py smoke logic: build page params defensively
        smoke_params: dict = {}
        for key in ("page_param", "size_param"):
            param_name = client.paging.get(key)
            if param_name:
                smoke_params[param_name] = 1

        client.get(cfg.get("smoke_path", "/"), smoke_params)
        status = "ok"
        detail = client.base_url

    except Exception as exc:
        status = "fail"
        detail = str(exc)
        log.warning("test_connection smoke %s (%s) failed: %s", conn_id, connector_name, exc)

    # Persist test result
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE config.connections
                SET last_test_at = %s, last_test_status = %s, last_test_detail = %s
                WHERE id = %s
                """,
                (_now(), status, detail, conn_id),
            )
        db_conn.commit()
    except Exception as exc:
        log.warning("test_connection persist %s failed: %s", conn_id, exc)
        db_conn.rollback()
    finally:
        db_conn.close()

    return JSONResponse({"status": status, "detail": detail})


# ─────────────────────────────────────────────────────────────────────────────
# SMTP API
# ─────────────────────────────────────────────────────────────────────────────

def _smtp_row_safe(row: dict) -> dict:
    """Mask password and serialise timestamps."""
    out = dict(row)
    if out.get("password"):
        try:
            plain = secrets_box.decrypt(out["password"])
            out["password"] = secrets_box.mask(plain)
        except Exception:
            out["password"] = "••••"
    for k in ("last_test_at", "updated_at"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    return out


@app.get("/api/smtp")
async def get_smtp(request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard
    db_conn = _db.connect()
    try:
        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM config.smtp WHERE id = 1")
            row = cur.fetchone()
    finally:
        db_conn.close()

    if row is None:
        return JSONResponse({})
    return JSONResponse(_smtp_row_safe(_row_to_dict(row)))


@app.put("/api/smtp")
async def upsert_smtp(request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard
    body = await request.json()

    db_conn = _db.connect()
    try:
        # Fetch existing to preserve password if omitted
        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT password FROM config.smtp WHERE id = 1")
            existing = cur.fetchone()

        new_pw_plain = body.get("password")
        if new_pw_plain:
            encrypted_pw = secrets_box.encrypt(new_pw_plain)
        elif existing and existing["password"]:
            encrypted_pw = existing["password"]
        else:
            encrypted_pw = None

        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO config.smtp
                    (id, host, port, security, username, password, from_addr, recipients, enabled, updated_at)
                VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    host       = EXCLUDED.host,
                    port       = EXCLUDED.port,
                    security   = EXCLUDED.security,
                    username   = EXCLUDED.username,
                    password   = COALESCE(EXCLUDED.password, config.smtp.password),
                    from_addr  = EXCLUDED.from_addr,
                    recipients = EXCLUDED.recipients,
                    enabled    = EXCLUDED.enabled,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    body.get("host"),
                    body.get("port"),
                    body.get("security"),
                    body.get("username"),
                    encrypted_pw,
                    body.get("from_addr"),
                    body.get("recipients"),
                    body.get("enabled", False),
                    _now(),
                ),
            )
            row = _row_to_dict(cur.fetchone())
        db_conn.commit()
    except Exception as exc:
        db_conn.rollback()
        log.warning("upsert_smtp failed: %s", exc)
        return JSONResponse({"detail": str(exc)}, status_code=400)
    finally:
        db_conn.close()

    return JSONResponse(_smtp_row_safe(row))


@app.post("/api/smtp/test")
async def test_smtp(request: Request):
    guard = _require_api_auth(request)
    if guard:
        return guard

    db_conn = _db.connect()
    try:
        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM config.smtp WHERE id = 1")
            row = cur.fetchone()
    except Exception as exc:
        db_conn.close()
        return JSONResponse({"status": "fail", "detail": str(exc)})

    if row is None:
        db_conn.close()
        return JSONResponse({"status": "fail", "detail": "SMTP not configured"})

    row = _row_to_dict(row)
    status, detail = "fail", "unknown error"
    try:
        host = row.get("host") or ""
        port = int(row.get("port") or 587)
        security = (row.get("security") or "starttls").lower()
        username = row.get("username") or ""
        password_enc = row.get("password") or ""
        password = secrets_box.decrypt(password_enc) if password_enc else ""
        sender = row.get("from_addr") or username
        recipients = row.get("recipients") or []

        if not host:
            raise ValueError("SMTP host not configured")
        if not recipients:
            raise ValueError("No recipients configured")

        # Send directly via smtplib using decrypted DB values (notify.send reads
        # os.environ so we bypass it here to use the DB-stored credentials).
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = "Inventory API — SMTP test"
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.set_content("This is a test message from the Inventory API Setup Console.")

        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                if username:
                    s.login(username, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                if security == "starttls":
                    s.starttls()
                if username:
                    s.login(username, password)
                s.send_message(msg)

        status = "ok"
        detail = f"Test email sent to {', '.join(recipients)}"

    except Exception as exc:
        status = "fail"
        detail = str(exc)
        log.warning("smtp_test failed: %s", exc)

    # Persist result
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE config.smtp
                SET last_test_at = %s, last_test_status = %s
                WHERE id = 1
                """,
                (_now(), status),
            )
        db_conn.commit()
    except Exception as exc:
        log.warning("smtp_test persist failed: %s", exc)
        db_conn.rollback()
    finally:
        db_conn.close()

    return JSONResponse({"status": status, "detail": detail})


# ─────────────────────────────────────────────────────────────────────────────
# Data API  — view synced raw.* tables
# ─────────────────────────────────────────────────────────────────────────────

def _flatten_payload(payload) -> dict:
    """Flatten a JSONB payload dict: scalars pass through, dicts/lists → JSON string."""
    if not isinstance(payload, dict):
        return {}
    out = {}
    for k, v in payload.items():
        if isinstance(v, (dict, list)):
            out[k] = json.dumps(v, default=str)
        else:
            out[k] = v
    return out


def _table_identifier(raw_table: str) -> psycopg2.sql.Composed:
    """Return a safe psycopg2 SQL object for a schema-qualified table name."""
    schema, _, name = raw_table.partition(".")
    return psycopg2.sql.SQL("{}.{}").format(
        psycopg2.sql.Identifier(schema),
        psycopg2.sql.Identifier(name),
    )


@app.get("/api/customers")
async def list_customers(request: Request):
    """Return [{id, customer_name, connector, environment, business_unit, enabled}]
    ordered by customer_name, sourced from config.connections.

    Never returns secrets.  On any DB error returns [] (200) — never 500.

    Note on connector-scoped filtering: raw tables are currently tagged only by
    connector, not by individual connection row.  Filtering the Data and Format
    panels by the selected customer's connector is correct today (one customer per
    connector).  If multiple customers share a connector in future, raw tables would
    need a connection_id column — that is out of scope here.
    """
    guard = _require_api_auth(request)
    if guard:
        return guard
    try:
        db_conn = _db.connect()
        try:
            with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, customer_name, connector, environment, business_unit, enabled "
                    "FROM config.connections ORDER BY customer_name"
                )
                rows = [_row_to_dict(r) for r in cur.fetchall()]
        finally:
            db_conn.close()
        for row in rows:
            row["id"] = str(row["id"])
        return JSONResponse(rows)
    except Exception as exc:
        log.warning("list_customers failed: %s", exc)
        return JSONResponse([])


@app.get("/api/data/datasets")
async def list_datasets(request: Request, connector: str | None = None):
    """Return [{dataset, table, connector, count}] for whitelisted domains.

    When *connector* is provided only domains whose connector matches are returned.
    Without connector all domains are returned (backward-compat; the FE always passes one).
    """
    guard = _require_api_auth(request)
    if guard:
        return guard

    result = []
    for domain_name in sorted(_DATASET_WHITELIST):
        entry = _DATASET_WHITELIST[domain_name]
        raw_table = entry["table"]
        domain_connector = entry["connector"]
        if connector and domain_connector != connector:
            continue
        count = 0
        db_conn = None
        try:
            db_conn = _db.connect()
            tbl = _table_identifier(raw_table)
            with db_conn.cursor() as cur:
                cur.execute(psycopg2.sql.SQL("SELECT count(*) FROM {}").format(tbl))
                row = cur.fetchone()
                count = row[0] if row else 0
        except psycopg2.errors.UndefinedTable:
            if db_conn:
                db_conn.rollback()
            count = 0
        except Exception as exc:
            log.warning("list_datasets count failed for %s: %s", raw_table, exc)
            if db_conn:
                try:
                    db_conn.rollback()
                except Exception:
                    pass
            count = 0
        finally:
            if db_conn:
                db_conn.close()
        result.append({"dataset": domain_name, "table": raw_table, "connector": domain_connector, "count": count})

    return JSONResponse(result)


@app.get("/api/data/{dataset}/preview")
async def preview_dataset(dataset: str, request: Request, limit: int = 50):
    """Return {columns, rows} for the first N rows of a whitelisted dataset."""
    guard = _require_api_auth(request)
    if guard:
        return guard

    _entry = _DATASET_WHITELIST.get(dataset)
    if _entry is None:
        return JSONResponse({"detail": "dataset not found"}, status_code=404)
    raw_table = _entry["table"]

    limit = max(1, min(limit, 500))

    rows_raw = []
    db_conn = None
    try:
        db_conn = _db.connect()
        tbl = _table_identifier(raw_table)
        with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                psycopg2.sql.SQL(
                    "SELECT natural_key, payload, ingested_at FROM {} "
                    "ORDER BY ingested_at DESC NULLS LAST LIMIT %s"
                ).format(tbl),
                (limit,),
            )
            rows_raw = cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        if db_conn:
            db_conn.rollback()
        return JSONResponse({"columns": [], "rows": []})
    except Exception as exc:
        log.warning("preview_dataset %s failed: %s", dataset, exc)
        if db_conn:
            try:
                db_conn.rollback()
            except Exception:
                pass
        return JSONResponse({"columns": [], "rows": []})
    finally:
        if db_conn:
            db_conn.close()

    if not rows_raw:
        return JSONResponse({"columns": [], "rows": []})

    # Collect union of payload keys across all rows
    payload_keys: set[str] = set()
    for r in rows_raw:
        p = r["payload"] if isinstance(r["payload"], dict) else {}
        payload_keys.update(p.keys())

    columns = ["natural_key"] + sorted(payload_keys) + ["ingested_at"]

    out_rows = []
    for r in rows_raw:
        flat = _flatten_payload(r["payload"] if isinstance(r["payload"], dict) else {})
        row_dict: dict = {"natural_key": r["natural_key"]}
        row_dict.update(flat)
        ts = r["ingested_at"]
        row_dict["ingested_at"] = ts.isoformat() if ts is not None else None
        out_rows.append(row_dict)

    return JSONResponse({"columns": columns, "rows": out_rows})


# ─────────────────────────────────────────────────────────────────────────────
# Format API  — preview MenuWorks data in TastEOS item format
# ─────────────────────────────────────────────────────────────────────────────

_EMPTY_FORMAT_PAYLOAD: dict = {
    "items": {"stock": [], "inventory": [], "service": [], "menu": []},
    "report": {"counts": {}, "unmapped_units": [], "zero_qty": [], "unresolved_mrn": []},
}


@app.get("/api/format/preview")
async def format_preview(request: Request, connector: str | None = None):
    """Return landed source data in TastEOS item format (stock/inventory/service/menu).

    *connector* selects which transform to run:
      - "menuworks" (default when omitted) → transform_mw_precitaste.build(conn)
      - "crunchtime" → transform_precitaste.build(conn) if available
      - anything else → graceful empty payload

    Preserving the no-connector default keeps existing tests passing.
    """
    guard = _require_api_auth(request)
    if guard:
        return guard

    # Default to menuworks for backward compat when FE omits the param
    effective_connector = connector or "menuworks"

    db_conn = None
    try:
        if effective_connector == "menuworks":
            import transform_mw_precitaste as _transform  # lazy import
            db_conn = _db.connect()
            payload = _transform.build(db_conn)
            return JSONResponse(payload)
        elif effective_connector == "crunchtime":
            try:
                import transform_precitaste as _ct_transform
                if hasattr(_ct_transform, "build"):
                    db_conn = _db.connect()
                    payload = _ct_transform.build(db_conn)
                    return JSONResponse(payload)
            except ImportError:
                pass
            # No build() available yet — return graceful empty payload
            result = dict(_EMPTY_FORMAT_PAYLOAD)
            result["note"] = "crunchtime transform not yet implemented"
            return JSONResponse(result)
        else:
            result = dict(_EMPTY_FORMAT_PAYLOAD)
            result["note"] = f"no transform available for connector '{effective_connector}'"
            return JSONResponse(result)
    except Exception as exc:
        log.warning("format_preview failed (connector=%s): %s", effective_connector, exc)
        result = dict(_EMPTY_FORMAT_PAYLOAD)
        result["error"] = str(exc)
        return JSONResponse(result)
    finally:
        if db_conn is not None:
            try:
                db_conn.close()
            except Exception:
                pass


@app.get("/api/data/{dataset}/export.csv")
async def export_dataset_csv(dataset: str, request: Request):
    """Stream all rows of a whitelisted dataset as a CSV download."""
    guard = _require_api_auth(request)
    if guard:
        return guard

    _entry = _DATASET_WHITELIST.get(dataset)
    if _entry is None:
        return JSONResponse({"detail": "dataset not found"}, status_code=404)
    raw_table = _entry["table"]

    _empty_csv = StreamingResponse(
        iter([""]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{dataset}.csv"'},
    )

    payload_keys: set[str] = set()
    buffered: list[dict] = []
    db_conn = None
    try:
        db_conn = _db.connect()
        tbl = _table_identifier(raw_table)
        # Use a named server-side cursor for large tables
        with db_conn.cursor(name="csv_export", cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                psycopg2.sql.SQL(
                    "SELECT natural_key, payload, ingested_at FROM {} "
                    "ORDER BY ingested_at DESC NULLS LAST"
                ).format(tbl)
            )
            while True:
                batch = cur.fetchmany(500)
                if not batch:
                    break
                for r in batch:
                    p = r["payload"] if isinstance(r["payload"], dict) else {}
                    payload_keys.update(p.keys())
                    buffered.append(dict(r))
    except psycopg2.errors.UndefinedTable:
        if db_conn:
            try:
                db_conn.rollback()
            except Exception:
                pass
            db_conn.close()
        return _empty_csv
    except Exception as exc:
        log.warning("export_dataset_csv %s failed: %s", dataset, exc)
        if db_conn:
            try:
                db_conn.rollback()
            except Exception:
                pass
            db_conn.close()
        return _empty_csv
    finally:
        if db_conn:
            try:
                db_conn.close()
            except Exception:
                pass

    columns = ["natural_key"] + sorted(payload_keys) + ["ingested_at"]

    def generate():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        yield buf.getvalue()
        for r in buffered:
            flat = _flatten_payload(r["payload"] if isinstance(r["payload"], dict) else {})
            row_dict: dict = {"natural_key": r["natural_key"]}
            row_dict.update(flat)
            ts = r["ingested_at"]
            row_dict["ingested_at"] = ts.isoformat() if ts is not None else None
            buf2 = io.StringIO()
            writer2 = csv.DictWriter(buf2, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
            writer2.writerow(row_dict)
            yield buf2.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{dataset}.csv"'},
    )
