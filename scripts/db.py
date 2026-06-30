"""Postgres layer: schema init, raw-landing upserts with change detection, and sync_state.

Raw landing is idempotent (upsert on natural_key). Every upsert compares a content hash
against what's stored to classify each record as new / changed / unchanged, and writes a
`change_log` row for new/changed/deleted. The daily email reads `change_log` for the run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import psycopg2
import psycopg2.extras

log = logging.getLogger("db")
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def connect():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ValueError("DATABASE_URL is required but empty — fill in .env")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


def init_schema(conn) -> None:
    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no .sql files in {SQL_DIR}")
    with conn.cursor() as cur:
        for f in files:
            log.info("applying %s", f.name)
            cur.execute(f.read_text())
    conn.commit()


def ensure_raw_table(conn, raw_table: str) -> None:
    schema, _, name = raw_table.partition(".")
    if not name:
        raise ValueError(f"raw_table must be schema-qualified, got {raw_table!r}")
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {raw_table} (
                natural_key      TEXT PRIMARY KEY,
                payload          JSONB NOT NULL,
                content_hash     TEXT NOT NULL,
                label            TEXT,
                source_endpoint  TEXT NOT NULL,
                page             INTEGER,
                ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                seen_run_at      TIMESTAMPTZ
            )
            """
        )
    conn.commit()


def _hash(payload) -> str:
    return hashlib.md5(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def upsert_raw(conn, domain: str, raw_table: str, rows: list[tuple], run_at) -> dict:
    """rows: list of (natural_key, payload, source_endpoint, page, label).

    Returns {'new': n, 'changed': n, 'unchanged': n} and writes change_log rows.
    """
    stats = {"new": 0, "changed": 0, "unchanged": 0}
    if not rows:
        return stats

    keys = [str(r[0]) for r in rows]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT natural_key, content_hash FROM {raw_table} WHERE natural_key = ANY(%s)",
            (keys,),
        )
        existing = dict(cur.fetchall())

    values, changes = [], []
    for nk, payload, endpoint, page, label in rows:
        nk = str(nk)
        h = _hash(payload)
        prev = existing.get(nk)
        if prev is None:
            stats["new"] += 1
            changes.append((domain, nk, "new", label, run_at))
        elif prev != h:
            stats["changed"] += 1
            changes.append((domain, nk, "changed", label, run_at))
        else:
            stats["unchanged"] += 1
        values.append((nk, json.dumps(payload, default=str), h, label, endpoint, page, run_at))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"""
            INSERT INTO {raw_table}
                (natural_key, payload, content_hash, label, source_endpoint, page, seen_run_at)
            VALUES %s
            ON CONFLICT (natural_key) DO UPDATE SET
                payload         = EXCLUDED.payload,
                content_hash    = EXCLUDED.content_hash,
                label           = EXCLUDED.label,
                source_endpoint = EXCLUDED.source_endpoint,
                page            = EXCLUDED.page,
                ingested_at     = now(),
                seen_run_at     = EXCLUDED.seen_run_at
            """,
            values,
        )
        if changes:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO change_log (domain, natural_key, change_type, label, run_at) VALUES %s",
                changes,
            )
    return stats


def sweep_deleted(conn, domain: str, raw_table: str, run_at) -> int:
    """Full-snapshot deletes: rows not seen this run no longer exist upstream (plan §4A).
    Logs each as a 'deleted' change. Call ONLY after a successful FULL sync."""
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {raw_table} WHERE seen_run_at IS DISTINCT FROM %s "
            f"RETURNING natural_key, label",
            (run_at,),
        )
        deleted = cur.fetchall()
        if deleted:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO change_log (domain, natural_key, change_type, label, run_at) VALUES %s",
                [(domain, nk, "deleted", label, run_at) for nk, label in deleted],
            )
    return len(deleted)


def get_changes(conn, run_at) -> list[dict]:
    """All change_log rows for a run, for the email summary."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT domain, change_type, natural_key, label FROM change_log "
            "WHERE run_at = %s ORDER BY domain, change_type, label",
            (run_at,),
        )
        return [dict(r) for r in cur.fetchall()]


def set_sync_state(conn, domain: str, run_at, status: str, rows: int, error: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (domain, last_run_at, last_status, rows_ingested, error)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (domain) DO UPDATE SET
                last_run_at   = EXCLUDED.last_run_at,
                last_status   = EXCLUDED.last_status,
                rows_ingested = EXCLUDED.rows_ingested,
                error         = EXCLUDED.error
            """,
            (domain, run_at, status, rows, error),
        )
    conn.commit()
