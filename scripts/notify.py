"""SMTP email notifier for the daily sync (Gmail / O365 / any SMTP server).

Env (see .env.example):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
  EMAIL_FROM (default = SMTP_USER), EMAIL_TO (comma-separated)
  SMTP_STARTTLS (default true), SMTP_SSL (default false; use for port 465)

Gmail: enable 2FA and use a 16-char App Password as SMTP_PASSWORD.
O365:  SMTP_HOST=smtp.office365.com, port 587, STARTTLS.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("notify")


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def email_enabled() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("EMAIL_TO"))


def build_summary(changes: list[dict], run_at, *, errors: list[str] | None = None) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) from change_log rows."""
    errors = errors or []
    by_domain: dict[str, dict[str, list[str]]] = {}
    for c in changes:
        d = by_domain.setdefault(c["domain"], {"new": [], "changed": [], "deleted": []})
        d[c["change_type"]].append(c.get("label") or c["natural_key"])

    totals = {"new": 0, "changed": 0, "deleted": 0}
    for d in by_domain.values():
        for k in totals:
            totals[k] += len(d[k])

    stamp = run_at.strftime("%Y-%m-%d") if hasattr(run_at, "strftime") else str(run_at)
    flag = " ⚠" if errors else ""
    subject = (f"Crunchtime sync {stamp}: "
               f"{totals['new']} new, {totals['changed']} changed, {totals['deleted']} removed{flag}")

    # ── plain text ──
    lines = [subject, ""]
    if errors:
        lines += ["ERRORS:", *[f"  - {e}" for e in errors], ""]
    if not changes:
        lines.append("No changes since the last run.")
    for dom, d in sorted(by_domain.items()):
        lines.append(f"== {dom} ==")
        for kind, items in (("new", d["new"]), ("changed", d["changed"]), ("deleted", d["deleted"])):
            if items:
                lines.append(f"  {kind} ({len(items)}):")
                lines += [f"    - {x}" for x in items[:50]]
                if len(items) > 50:
                    lines.append(f"    … and {len(items) - 50} more")
        lines.append("")
    text = "\n".join(lines)

    # ── html ──
    def ul(items):
        shown = "".join(f"<li>{_esc(x)}</li>" for x in items[:50])
        if len(items) > 50:
            shown += f"<li><em>… and {len(items) - 50} more</em></li>"
        return f"<ul>{shown}</ul>"

    html = [f"<h2>{_esc(subject)}</h2>"]
    if errors:
        html.append("<p style='color:#b00'><strong>Errors:</strong></p>"
                     + "<ul>" + "".join(f"<li>{_esc(e)}</li>" for e in errors) + "</ul>")
    if not changes:
        html.append("<p>No changes since the last run.</p>")
    for dom, d in sorted(by_domain.items()):
        html.append(f"<h3>{_esc(dom)}</h3>")
        for kind, items in (("New", d["new"]), ("Changed", d["changed"]), ("Removed", d["deleted"])):
            if items:
                html.append(f"<p><strong>{kind} ({len(items)})</strong></p>{ul(items)}")
    return subject, text, "".join(html)


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def send(subject: str, text_body: str, html_body: str | None = None) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("EMAIL_FROM") or user
    recipients = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if not recipients:
        raise ValueError("EMAIL_TO is empty")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(f"<html><body>{html_body}</body></html>", subtype="html")

    if _env_bool("SMTP_SSL", False):
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if _env_bool("SMTP_STARTTLS", True):
                s.starttls()
            if user:
                s.login(user, password)
            s.send_message(msg)
    log.info("emailed %d recipient(s): %s", len(recipients), subject)


def notify_changes(changes: list[dict], run_at, *, errors: list[str] | None = None) -> bool:
    """Build + send the summary if SMTP is configured. Returns True if sent."""
    if not email_enabled():
        log.info("email not configured (SMTP_HOST/EMAIL_TO) — skipping notification")
        return False
    subject, text, html = build_summary(changes, run_at, errors=errors)
    send(subject, text, html)
    return True
