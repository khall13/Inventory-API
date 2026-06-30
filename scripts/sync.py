#!/usr/bin/env python3
"""Multi-connector API → Postgres sync CLI (config-driven, plan §3).

Usage:
  python sync.py --smoke [--connector crunchtime]   # prove auth (200) — no DB needed
  python sync.py --init-db                           # create schemas/tables from sql/*.sql
  python sync.py --domain recipes --mode full        # backfill one domain
  python sync.py --domain recipes --pages 1 --dump samples/recipes.json --no-transform
  python sync.py --domain all --mode incremental --email   # daily Railway run

Connectors (base URL + auth + paging) live in connectors.yaml; domains in endpoints.yaml.
Secrets + DATABASE_URL come from .env (see .env.example).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

import db
from connectors import make_client, get_config
from rest_client import ApiError

ROOT = Path(__file__).resolve().parent
ENDPOINTS_FILE = ROOT / "endpoints.yaml"
log = logging.getLogger("sync")


def load_domains() -> dict[str, dict]:
    cfg = yaml.safe_load(ENDPOINTS_FILE.read_text())
    defaults = cfg.get("defaults", {})
    return {(m := {**defaults, **d})["name"]: m for d in cfg.get("domains", [])}


def dig(record: dict, dotted_path: str | None):
    if not dotted_path:
        return None
    cur = record
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def sync_domain(conn, dom: dict, *, mode, max_pages, dump_path, run_transform, run_at) -> int:
    name = dom["name"]
    client = make_client(dom["connector"])
    params = dict(dom.get("extra_params") or {})
    eff_mode = mode or dom.get("mode", "full")

    if eff_mode == "incremental":
        if dom.get("supports_incremental"):
            params["minutesSinceUpdate"] = 1500   # 24h + 60m overlap, < 43200 cap (plan §4B)
        else:
            log.warning("[%s] no incremental support; using full", name)
            eff_mode = "full"

    db.ensure_raw_table(conn, dom["raw_table"])
    dumped, total = [], 0
    agg = {"new": 0, "changed": 0, "unchanged": 0}

    log.info("[%s] start (connector=%s, mode=%s)", name, dom["connector"], eff_mode)
    # A domain may declare a custom `handler` module (e.g. list→detail fan-out) instead of
    # the generic pager; it yields (batch_no, records) the same way (ADR 0002). NOTE: handler
    # domains build their own request params — `extra_params` and `minutesSinceUpdate` are NOT
    # passed to them; a handler reads what it needs from its `dom` row.
    if dom.get("handler"):
        handler = importlib.import_module(dom["handler"])
        record_source = handler.iter_records(client, dom, max_pages=max_pages)
        source_path = dom.get("path", dom["handler"])
    else:
        record_source = client.paginate(
            dom["path"], records_key=dom["records_key"],
            page_size=dom["page_size"], params=params, max_pages=max_pages,
            paged=dom.get("paged", True),
        )
        source_path = dom["path"]

    for page, records in record_source:
        if dump_path is not None:
            dumped.extend(records)
        rows = []
        for rec in records:
            nk = dig(rec, dom["natural_key_path"])
            if nk is None:
                log.warning("[%s] page %d: record missing key %r — skipped",
                            name, page, dom["natural_key_path"])
                continue
            rows.append((str(nk), rec, source_path, page, dig(rec, dom.get("label_path"))))
        stats = db.upsert_raw(conn, name, dom["raw_table"], rows, run_at)
        for k in agg:
            agg[k] += stats[k]
        total += len(rows)
        conn.commit()
        log.info("[%s] page %d: %d records (new=%d changed=%d)",
                 name, page, len(records), stats["new"], stats["changed"])

    # Full snapshots prune rows not seen this run — but only when the pull was COMPLETE.
    # A handler that skipped records sets dom['_sweep_safe']=False so we don't emit false
    # "removed" changes (ADR 0002 / review Must-Fix #2). Generic pager domains default True.
    if eff_mode == "full" and max_pages is None and dom.get("_sweep_safe", True):
        deleted = db.sweep_deleted(conn, name, dom["raw_table"], run_at)
        conn.commit()
        if deleted:
            log.info("[%s] swept %d rows no longer upstream", name, deleted)

    db.set_sync_state(conn, name, run_at, "ok", total, None)
    log.info("[%s] done: %d rows (new=%d changed=%d unchanged=%d)",
             name, total, agg["new"], agg["changed"], agg["unchanged"])

    if dump_path is not None:
        Path(dump_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dump_path).write_text(json.dumps(dumped, indent=2))
        log.info("[%s] wrote %d records → %s", name, len(dumped), dump_path)

    if run_transform and dom.get("transform"):
        mod = importlib.import_module(dom["transform"])
        if hasattr(mod, "run"):
            n = mod.run(conn)
            conn.commit()
            log.info("[%s] transform %s done (%s rows)", name, dom["transform"], n)
    return total


def main(argv=None) -> int:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(description="API → Postgres sync")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--connector", default="crunchtime", help="connector for --smoke")
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--domain", help="domain name from endpoints.yaml, or 'all'")
    ap.add_argument("--mode", choices=["full", "incremental"])
    ap.add_argument("--pages", type=int, help="cap pages per domain (sampling)")
    ap.add_argument("--dump", help="also write pulled raw records to this JSON file")
    ap.add_argument("--no-transform", action="store_true")
    ap.add_argument("--email", action="store_true", help="email the change summary when done")
    args = ap.parse_args(argv)

    if args.smoke:
        cfg = get_config(args.connector)
        client = make_client(args.connector)
        try:
            client.get(cfg.get("smoke_path", "/"), {client.paging["page_param"]: 1,
                                                    client.paging["size_param"]: 1})
        except ApiError as e:
            log.error("SMOKE FAILED — %s", e)
            return 1
        log.info("SMOKE OK — %s authenticated at %s", args.connector, client.base_url)
        return 0

    if args.init_db:
        conn = db.connect()
        try:
            db.init_schema(conn)
            log.info("schema initialized")
        finally:
            conn.close()
        return 0

    if not args.domain:
        ap.error("one of --smoke, --init-db, or --domain is required")

    domains = load_domains()
    if args.domain == "all":
        targets = list(domains.values())
    elif args.domain in domains:
        targets = [domains[args.domain]]
    else:
        ap.error(f"unknown domain {args.domain!r}; known: {', '.join(domains) or '(none)'}")

    run_at = now_utc()
    conn = db.connect()
    errors = []
    try:
        for dom in targets:
            try:
                sync_domain(conn, dom, mode=args.mode, max_pages=args.pages,
                            dump_path=args.dump, run_transform=not args.no_transform, run_at=run_at)
            except ApiError as e:
                conn.rollback()
                db.set_sync_state(conn, dom["name"], run_at, "error", 0, str(e))
                errors.append(f"{dom['name']}: {e}")
                log.error("[%s] FAILED — %s", dom["name"], e)

        if args.email:
            import notify
            try:
                notify.notify_changes(db.get_changes(conn, run_at), run_at, errors=errors)
            except Exception as e:  # never let email failure mask a successful sync
                log.error("email failed: %s", e)
    finally:
        conn.close()
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
