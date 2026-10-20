"""Turns your WhatsApp messages and button taps into pipeline actions. Returns the reply text."""

from __future__ import annotations

import logging
import re

from sqlmodel import Session, select

from tengen_toppa.intake import find_url
from tengen_toppa.pipeline import questions, steps, tasks
from tengen_toppa.store.db import kv_get, kv_set
from tengen_toppa.store.models import JobStatus, WAMessage
from tengen_toppa.whatsapp import notify
from tengen_toppa.whatsapp.inbound import Inbound, normalize_number

log = logging.getLogger(__name__)

_CMD_RE = re.compile(r"^(apply|skip|done|job|edit|submit)\s+#?(\d+)\s*(.*)$", re.I | re.S)
_COMMANDS = {"help", "?", "commands", "status", "more", "start", "next", "pause", "resume", "question", "questions"}


def handle(s: Session, msg: Inbound, my_number: str) -> str | None:
    """Process one inbound message. Returns the reply (also sent), or None if ignored."""
    if normalize_number(msg.sender) != normalize_number(my_number):
        log.warning("ignored message from unknown sender %s", msg.sender)
        return None
    if msg.wa_id and s.exec(select(WAMessage.id).where(WAMessage.wa_id == msg.wa_id)).first() is not None:
        return None  # Meta retry / duplicate delivery

    s.add(WAMessage(wa_id=msg.wa_id or None, direction="in", kind=msg.kind, body=msg.text))
    s.commit()
    notify.mark_inbound(s)

    try:
        reply = _dispatch(s, msg)
    except steps.JobNotFound as e:
        reply = str(e)
    if reply:
        notify.send_text(s, reply)
    return reply


def _dispatch(s: Session, msg: Inbound) -> str | None:
    if msg.kind == "button":
        action, _, raw_id = msg.text.partition(":")
        if not raw_id.isdigit():
            return "Unknown button."
        return _job_action(s, action, int(raw_id), "")

    text = msg.text.strip()
    low = text.lower()

    pending = kv_get(s, "pending_input")
    if pending and pending.get("kind") == "qa" and low not in _COMMANDS and not _CMD_RE.match(text):
        reply, resume_job = questions.answer_current(s, text)
        if resume_job is not None:
            tasks.apply_task(resume_job)
        return reply

    if low in ("question", "questions"):
        if not questions.queue(s):
            return "No application questions are waiting."
        questions.send_next(s)
        return None
    if url := find_url(text):
        tasks.intake_task(url)
        return "Got it. Reading the job, then I'll send a tailored resume card."
    if low in ("help", "?", "commands"):
        return steps.HELP_TEXT
    if low == "status":
        return steps.status_text(s)
    if low in ("more", "start", "next"):
        waiting = len(steps.jobs_by_status(s, JobStatus.TAILORED))
        if not waiting:
            return "Nothing waiting right now. I'll ping you when new tailored jobs are ready."
        tasks.flush_task()
        return None
    if low == "pause":
        kv_set(s, "paused", True)
        return "Paused. Nothing new will be discovered or tailored. Send 'resume' to continue."
    if low == "resume":
        kv_set(s, "paused", False)
        kv_set(s, "llm_paused_until", None)
        tasks.drain_task()
        return "Resumed."
    if m := re.fullmatch(r"limit\s+(\d+)", low):
        kv_set(s, "daily_cap", int(m.group(1)))
        return f"Daily tailoring cap set to {m.group(1)}."
    if m := _CMD_RE.match(text):
        return _job_action(s, m.group(1).lower(), int(m.group(2)), m.group(3).strip())

    if pending and pending.get("kind") == "edit":
        kv_set(s, "pending_input", None)
        return _retailor(s, int(pending["job_id"]), text)

    return "Didn't get that. Send 'help' for commands."


def _job_action(s: Session, action: str, job_id: int, arg: str) -> str | None:
    job = steps.get_job(s, job_id)  # raises JobNotFound
    match action:
        case "apply":
            if job.status in (JobStatus.APPROVED, JobStatus.APPLIED):
                return f"#{job_id} is already {job.status}."
            tasks.approve_task(job_id)
            return f"Applying to #{job_id} ({job.company}). Filling the form on your Mac..."
        case "skip":
            steps.skip(s, job_id, arg or "skipped by you")
            return f"Skipped #{job_id} ({job.company})."
        case "submit":
            tasks.submit_task(job_id)
            return f"Submitting #{job_id} ({job.company})..."
        case "done":
            steps.mark_applied(s, job_id)
            return f"Marked #{job_id} ({job.company}) as applied."
        case "job":
            tasks.send_card_task(job_id)
            return None
        case "edit":
            if arg:
                return _retailor(s, job_id, arg)
            kv_set(s, "pending_input", {"kind": "edit", "job_id": job_id})
            return f"What should change in the resume for #{job_id} ({job.company})? Reply in one message."
        case _:
            return "Unknown action."


def _retailor(s: Session, job_id: int, feedback: str) -> str:
    job = steps.get_job(s, job_id)
    if not job.analysis:
        return f"#{job_id} hasn't been analyzed yet."
    tasks.tailor_job_task(job_id, feedback)
    return f"Re-tailoring #{job_id} with your note. New PDF coming shortly."
