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

import datetime as dt
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
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware

import db as _db
import secrets_box  # encrypt/decrypt/mask helpers

log = logging.getLogger("web.app")

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
