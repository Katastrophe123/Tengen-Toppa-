"""Can your WhatsApp commands reach this Mac? Is the alert template approved?

Outbound messages (dashboard, alerts) go straight to Meta and keep working when the tunnel dies;
only inbound commands (stop, answers, Submit now) need the tunnel. So we check it explicitly."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session

from tengen_toppa.config import settings
from tengen_toppa.secrets import Secret, get_secret
from tengen_toppa.store.db import kv_get, kv_set
from tengen_toppa.store.models import utcnow

log = logging.getLogger(__name__)
CHECK_EVERY = timedelta(seconds=60)


def tunnel_url(s: Session) -> str | None:
    return kv_get(s, "tunnel_url")


def _probe(url: str) -> bool:
    try:
        return httpx.get(f"{url.rstrip('/')}/health", timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


def commands_online(s: Session, *, force: bool = False) -> bool:
    """Cached for a minute. Records when the outage started (kv commands_offline_since)."""
    url = tunnel_url(s)
    if not url:
        return False
    last = kv_get(s, "commands_checked_at")
    if not force and last and utcnow() - datetime.fromisoformat(last) < CHECK_EVERY:
        return bool(kv_get(s, "commands_online"))
    ok = _probe(url)
    kv_set(s, "commands_checked_at", utcnow().isoformat())
    kv_set(s, "commands_online", ok)
    if ok:
        kv_set(s, "commands_offline_since", None)
    elif not kv_get(s, "commands_offline_since"):
        kv_set(s, "commands_offline_since", utcnow().isoformat())
    return ok


def offline_minutes(s: Session) -> float:
    since = kv_get(s, "commands_offline_since")
    return (utcnow() - datetime.fromisoformat(since)).total_seconds() / 60 if since else 0.0


def alert_template_status(s: Session) -> str:
    waba = kv_get(s, "waba_id")
    token = get_secret(Secret.WA_ACCESS_TOKEN)
    if not waba or not token:
        return "unknown (run `tengen wa-subscribe <WABA_ID>` once)"
    wa = settings().whatsapp
    try:
        r = httpx.get(
            f"https://graph.facebook.com/{wa.graph_version}/{waba}/message_templates",
            params={"access_token": token, "name": wa.alert_template, "fields": "name,status"},
            timeout=20,
        )
        rows = [t for t in r.json().get("data", []) if t.get("name") == wa.alert_template]
    except (httpx.HTTPError, ValueError) as e:
        return f"unknown ({e})"
    return rows[0].get("status", "UNKNOWN") if rows else "NOT CREATED"
