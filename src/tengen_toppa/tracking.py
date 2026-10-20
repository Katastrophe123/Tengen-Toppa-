"""Reply tracking over IMAP (Gmail app password). Only emails mentioning a company you applied to are
sent to Claude Haiku for classification, so normal inbox traffic costs nothing."""

from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Literal

from pydantic import BaseModel
from sqlmodel import Session, select

from tengen_toppa.config import settings
from tengen_toppa.discovery.base import html_to_text
from tengen_toppa.llm.claude_client import ask
from tengen_toppa.pipeline import steps
from tengen_toppa.secrets import Secret, require_secret
from tengen_toppa.store.db import set_status
from tengen_toppa.store.models import Job, JobStatus, ProcessedEmail
from tengen_toppa.whatsapp import notify

log = logging.getLogger(__name__)

TRACKED = (JobStatus.APPLIED, JobStatus.APPLYING, JobStatus.INTERVIEW, JobStatus.APPROVED)


@dataclass(frozen=True)
class Mail:
    message_id: str
    sender: str
    subject: str
    body: str


class EmailVerdict(BaseModel):
    about_job_application: bool
    company: str
    category: Literal["interview", "assessment", "rejection", "offer", "acknowledgement", "other"]
    summary: str
    action_needed: str


def _body(msg: EmailMessage) -> str:
    if msg.is_multipart():
        plain = html = ""
        for part in msg.walk():
            if not isinstance(part, EmailMessage) or part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and not plain:
                plain = part.get_content()
            elif ctype == "text/html" and not html:
                html = part.get_content()
        return plain or html_to_text(html)
    content = msg.get_content()
    return html_to_text(content) if msg.get_content_type() == "text/html" else content


def parse_mail(raw: bytes) -> Mail:
    msg = email.message_from_bytes(raw, policy=default_policy)
    assert isinstance(msg, EmailMessage)
    return Mail(
        message_id=str(msg.get("Message-ID", "")).strip() or hashlib.sha1(raw).hexdigest(),
        sender=str(msg.get("From", "")),
        subject=str(msg.get("Subject", "")),
        body=_body(msg)[:8000],
    )


def fetch_recent(host: str, address: str, password: str, days: int) -> list[Mail]:
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    out: list[Mail] = []
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(address, password)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, f'(SINCE "{since}")')
        for num in (data[0] or b"").split()[-200:]:
            _, parts = imap.fetch(num.decode(), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append(parse_mail(raw))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def match_job(mail: Mail, jobs: list[Job]) -> Job | None:
    hay = _norm(f"{mail.sender} {mail.subject} {mail.body[:3000]}")
    hits = [j for j in jobs if len(_norm(j.company)) >= 3 and _norm(j.company) in hay]
    if not hits:
        return None
    titled = [j for j in hits if _norm(j.title)[:20] in hay]
    return max(titled or hits, key=lambda j: j.updated_at)


def classify(mail: Mail, company: str) -> EmailVerdict:
    cfg = settings()
    payload = (
        f"<company>{company}</company>\n<from>{mail.sender}</from>\n<subject>{mail.subject}</subject>\n"
        f"<body>\n{mail.body[:6000]}\n</body>"
    )
    return ask(
        "Classify this email about a job application. Input on stdin.",
        payload,
        EmailVerdict,
        system_prompt="classify_email.md",
        model=cfg.llm.analyze_model,
        timeout=cfg.llm.timeout_seconds,
    ).value


STATUS_FOR = {
    "interview": JobStatus.INTERVIEW,
    "assessment": JobStatus.INTERVIEW,
    "rejection": JobStatus.REJECTED,
    "offer": JobStatus.OFFER,
}


def process_mails(s: Session, mails: list[Mail]) -> int:
    jobs = list(s.exec(select(Job).where(Job.status.in_((*TRACKED, JobStatus.REJECTED)))))  # type: ignore[attr-defined]
    handled = 0
    for mail in mails:
        if s.get(ProcessedEmail, mail.message_id) is not None:
            continue
        job = match_job(mail, jobs)
        if job is None:
            continue  # unrelated mail: no Claude call
        verdict = classify(mail, job.company)
        steps.record_llm(s, "email", settings().llm.analyze_model, job.id, ok=True)
        s.add(
            ProcessedEmail(
                message_id=mail.message_id, job_id=job.id, category=verdict.category, subject=mail.subject[:300]
            )
        )
        s.commit()
        if not verdict.about_job_application or verdict.category in ("other",):
            continue
        handled += 1
        new_status = STATUS_FOR.get(verdict.category)
        if new_status and job.status != new_status:
            set_status(s, job, new_status, f"email: {verdict.summary[:150]}")
        urgent = verdict.category in ("interview", "assessment", "offer")
        notify.send_text(
            s,
            f"#{job.id} {job.company} - {verdict.category.upper()}\n{verdict.summary}"
            + (f"\nTo do: {verdict.action_needed}" if verdict.action_needed else "")
            + f"\nSubject: {mail.subject}",
            job_id=job.id,
            urgent=urgent,
        )
    return handled


def poll(s: Session) -> int:
    cfg = settings().email
    if not cfg.enabled or not cfg.address:
        return 0
    mails = fetch_recent(cfg.imap_host, cfg.address, require_secret(Secret.EMAIL_APP_PASSWORD), cfg.lookback_days)
    return process_mails(s, mails)
