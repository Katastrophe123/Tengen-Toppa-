"""High-level, 24h-window-aware WhatsApp messaging. Every message is logged to SQLite."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from tengen_toppa.config import settings
from tengen_toppa.secrets import Secret, get_secret
from tengen_toppa.store.db import kv_get, kv_set
from tengen_toppa.store.models import Job, ResumeVersion, WAMessage, utcnow
from tengen_toppa.whatsapp.client import Button, WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=23, minutes=30)  # a little under Meta's 24h, to be safe
_client: WhatsAppClient | None = None


def get_client() -> WhatsAppClient | None:
    """None when WhatsApp isn't configured yet: the pipeline still runs, messages go to the log."""
    global _client
    if _client is None:
        token = get_secret(Secret.WA_ACCESS_TOKEN)
        phone_id = get_secret(Secret.WA_PHONE_NUMBER_ID)
        me = get_secret(Secret.WA_MY_NUMBER)
        if not (token and phone_id and me):
            return None
        _client = WhatsAppClient(token, phone_id, me, settings().whatsapp.graph_version)
    return _client


def set_client(client: WhatsAppClient | None) -> None:
    """For tests."""
    global _client
    _client = client


def mark_inbound(s: Session) -> None:
    kv_set(s, "wa_last_inbound", utcnow().isoformat())


def window_open(s: Session) -> bool:
    last = kv_get(s, "wa_last_inbound")
    return bool(last) and utcnow() - datetime.fromisoformat(last) < WINDOW


def _log(s: Session, wa_id: str | None, kind: str, body: str, job_id: int | None = None) -> None:
    s.add(WAMessage(wa_id=wa_id, direction="out", kind=kind, body=body, job_id=job_id))
    s.commit()


def send_text(s: Session, text: str, *, job_id: int | None = None, urgent: bool = False) -> bool:
    """Free-form text inside the window. Outside it, urgent messages go via the alert template."""
    client = get_client()
    if client is None:
        log.info("[whatsapp not configured] %s", text)
        return False
    try:
        if window_open(s):
            _log(s, client.send_text(text), "text", text, job_id)
            return True
        if urgent:
            wa = settings().whatsapp
            _log(
                s,
                client.send_template(wa.alert_template, wa.template_language, [text]),
                "template",
                text,
                job_id,
            )
            return True
        log.info("[window closed, dropped non-urgent] %s", text)
        return False
    except WhatsAppError as e:
        log.error("whatsapp send failed: %s", e)
        return False


def send_digest_template(s: Session, waiting: int) -> bool:
    client = get_client()
    if client is None:
        return False
    wa = settings().whatsapp
    try:
        _log(
            s,
            client.send_template(wa.digest_template, wa.template_language, [str(waiting)]),
            "template",
            f"digest:{waiting}",
        )
        return True
    except WhatsAppError as e:
        log.error("digest template failed: %s", e)
        return False


def send_screenshot(s: Session, path: Path, caption: str, *, job_id: int, buttons: list[Button] | None = None) -> bool:
    """Form screenshot + optional action buttons. Falls back to a text alert outside the 24h window."""
    client = get_client()
    if client is None:
        log.info("[whatsapp not configured] %s (%s)", caption, path)
        return False
    if not window_open(s):
        return send_text(s, caption, job_id=job_id, urgent=True)
    try:
        _log(s, client.send_image(path, caption=caption), "image", caption, job_id)
        if buttons:
            _log(s, client.send_buttons(caption.split("\n")[0], buttons), "interactive", caption, job_id)
        return True
    except WhatsAppError as e:
        log.error("screenshot send failed for job %s: %s", job_id, e)
        return False


def format_card(job: Job, rv: ResumeVersion) -> str:
    a = job.analysis or {}
    report = rv.validation or {}
    lines = [
        f"#{job.id}  Score {job.score:.0f}/100" if job.score is not None else f"#{job.id}",
        f"{job.title} - {job.company}",
        " | ".join(
            x
            for x in [
                job.location or None,
                "Remote" if job.remote else None,
                f"{a['years_required']}+ yrs" if a.get("years_required") else None,
                job.source,
            ]
            if x
        ),
    ]
    if a.get("matched_strengths"):
        lines.append("Matches: " + ", ".join(a["matched_strengths"][:4]))
    if a.get("gaps"):
        lines.append("Gaps (not added): " + ", ".join(a["gaps"][:4]))
    if report.get("keyword_coverage") is not None:
        lines.append(f"ATS keyword coverage: {report['keyword_coverage'] * 100:.0f}%")
    changes = (rv.tailored or {}).get("changes") or []
    if changes:
        lines.append("Changes:")
        lines += [f"- {c}" for c in changes[:4]]
    lines.append("Projects: unchanged")
    return "\n".join(lines)


def send_job_card(s: Session, job: Job, rv: ResumeVersion) -> bool:
    client = get_client()
    if client is None:
        log.info("[whatsapp not configured] card for job %s\n%s", job.id, format_card(job, rv))
        return False
    if not window_open(s):
        return False
    try:
        if rv.pdf_path:
            pdf = Path(rv.pdf_path)
            _log(s, client.send_document(pdf, caption=f"#{job.id} {job.company}"), "document", pdf.name, job.id)
        body = format_card(job, rv)
        wa_id = client.send_buttons(
            body,
            [
                Button(f"apply:{job.id}", "Apply"),
                Button(f"edit:{job.id}", "Request edit"),
                Button(f"skip:{job.id}", "Skip"),
            ],
        )
        _log(s, wa_id, "interactive", body, job.id)
        return True
    except WhatsAppError as e:
        log.error("card send failed for job %s: %s", job.id, e)
        return False
