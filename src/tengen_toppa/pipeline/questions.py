"""Ask unanswered application questions on WhatsApp, one at a time; answers go to the Q&A bank."""

from __future__ import annotations

from sqlmodel import Session

from tengen_toppa.apply.fields import FormField, pick_option
from tengen_toppa.qa import save_answer
from tengen_toppa.store.db import kv_get, kv_set
from tengen_toppa.store.models import Job
from tengen_toppa.whatsapp import notify

QUEUE_KEY = "qa_queue"


def queue(s: Session) -> list[dict]:
    return list(kv_get(s, QUEUE_KEY, []) or [])


def ask_questions(s: Session, job_id: int, fields: list[FormField]) -> int:
    q = [item for item in queue(s) if item["job_id"] != job_id]
    q += [{"job_id": job_id, "question": f.label, "options": f.options} for f in fields]
    kv_set(s, QUEUE_KEY, q)
    pending = kv_get(s, "pending_input")
    if not (pending and pending.get("kind") == "qa"):
        send_next(s)
    return len(fields)


def format_question(s: Session, item: dict) -> str:
    job = s.get(Job, item["job_id"])
    head = f"#{item['job_id']} {job.company if job else ''} application asks:"
    lines = [head, item["question"]]
    opts = [
        o for o in item.get("options") or [] if o.strip() and o.lower() not in ("select...", "select", "please select")
    ]
    if opts:
        lines.append("Options:")
        lines += [f"{i}. {o}" for i, o in enumerate(opts, 1)]
        lines.append("Reply with the number or the text.")
    else:
        lines.append("Reply with your answer.")
    lines.append("(Saved for future applications. Send 'skip question' to leave it blank.)")
    return "\n".join(lines)


def send_next(s: Session) -> bool:
    q = queue(s)
    if not q:
        kv_set(s, "pending_input", None)
        return False
    kv_set(s, "pending_input", {"kind": "qa"})
    notify.send_text(s, format_question(s, q[0]), job_id=q[0]["job_id"], urgent=True)
    return True


def answer_current(s: Session, text: str) -> tuple[str, int | None]:
    """Record the reply to the current question. Returns (reply, job_id_to_resume_or_None)."""
    q = queue(s)
    if not q:
        kv_set(s, "pending_input", None)
        return "No question is waiting.", None
    item = q.pop(0)
    opts = [
        o for o in item.get("options") or [] if o.strip() and o.lower() not in ("select...", "select", "please select")
    ]
    value = text.strip()
    skipped = value.lower() == "skip question"
    if not skipped:
        if opts and value.isdigit() and 1 <= int(value) <= len(opts):
            value = opts[int(value) - 1]
        elif opts:
            picked = pick_option(opts, value)
            if picked is None:
                q.insert(0, item)
                kv_set(s, QUEUE_KEY, q)
                return "That doesn't match an option. " + format_question(s, item), None
            value = picked
        save_answer(s, item["question"], value)
    kv_set(s, QUEUE_KEY, q)
    job_id = item["job_id"]
    more_for_job = any(i["job_id"] == job_id for i in q)
    if q:
        send_next(s)
    else:
        kv_set(s, "pending_input", None)
    if more_for_job:
        return ("Skipped." if skipped else "Saved."), None
    return (
        f"{'Skipped' if skipped else 'Saved'}. All questions for #{job_id} answered, resuming the application."
    ), job_id
