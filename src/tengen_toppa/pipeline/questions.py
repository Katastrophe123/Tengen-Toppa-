"""Ask unanswered application questions on WhatsApp, one at a time; answers go to the Q&A bank.

Two kinds of waiting jobs: ATS jobs (`job_id`, one form) and LinkedIn jobs (`li_job_ids`, many jobs can
wait on the same question, which is asked once).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session

from tengen_toppa.apply.fields import FormField, pick_option
from tengen_toppa.qa import same_question, save_answer
from tengen_toppa.store.db import kv_get, kv_set
from tengen_toppa.store.models import Job, LiJob
from tengen_toppa.whatsapp import notify

QUEUE_KEY = "qa_queue"
_PLACEHOLDERS = ("select...", "select", "please select", "select an option")


@dataclass
class AnswerResult:
    reply: str
    job_id: int | None = None  # ATS job ready to resume
    li_ready: list[str] = field(default_factory=list)  # LinkedIn jobs with no questions left
    li_skipped: list[str] = field(default_factory=list)  # LinkedIn jobs whose required question you skipped


def queue(s: Session) -> list[dict]:
    return list(kv_get(s, QUEUE_KEY, []) or [])


def _opts(item: dict) -> list[str]:
    return [o for o in item.get("options") or [] if o.strip() and o.strip().lower() not in _PLACEHOLDERS]


def _maybe_send(s: Session) -> None:
    pending = kv_get(s, "pending_input")
    if not (pending and pending.get("kind") == "qa"):
        send_next(s)


def ask_questions(s: Session, job_id: int, fields: list[FormField]) -> int:
    q = [item for item in queue(s) if item.get("job_id") != job_id]
    q += [{"job_id": job_id, "question": f.label, "options": f.options} for f in fields]
    kv_set(s, QUEUE_KEY, q)
    _maybe_send(s)
    return len(fields)


def ask_li_questions(s: Session, li_job_id: str, fields: list[FormField]) -> int:
    """Add a LinkedIn job to each question; a question already waiting is not asked again."""
    q = queue(s)
    added = 0
    for f in fields:
        item = next((i for i in q if "li_job_ids" in i and same_question(i["question"], f.label)), None)
        if item is None:
            q.append({"li_job_ids": [li_job_id], "question": f.label, "options": f.options})
            added += 1
        elif li_job_id not in item["li_job_ids"]:
            item["li_job_ids"].append(li_job_id)
    kv_set(s, QUEUE_KEY, q)
    _maybe_send(s)
    return added


def format_question(s: Session, item: dict) -> str:
    if "li_job_ids" in item:
        ids = item["li_job_ids"]
        first = s.get(LiJob, ids[0])
        who = f"{first.title} @ {first.company}" if first else ids[0]
        head = f"❓ {len(ids)} LinkedIn job(s) need this (e.g. {who}):"
    else:
        job = s.get(Job, item["job_id"])
        head = f"#{item['job_id']} {job.company if job else ''} application asks:"
    lines = [head, item["question"]]
    if opts := _opts(item):
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
    notify.send_text(s, format_question(s, q[0]), job_id=q[0].get("job_id"), urgent=True)
    return True


def answer(s: Session, text: str) -> AnswerResult:
    q = queue(s)
    if not q:
        kv_set(s, "pending_input", None)
        return AnswerResult("No question is waiting.")
    item = q.pop(0)
    opts = _opts(item)
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
                return AnswerResult("That doesn't match an option. " + format_question(s, item))
            value = picked
        save_answer(s, item["question"], value)
    kv_set(s, QUEUE_KEY, q)
    if q:
        send_next(s)
    else:
        kv_set(s, "pending_input", None)
    word = "Skipped" if skipped else "Saved"

    if "li_job_ids" in item:
        still_waiting = {j for i in q for j in i.get("li_job_ids", [])}
        ids = item["li_job_ids"]
        if skipped:
            return AnswerResult(f"{word}. {len(ids)} LinkedIn job(s) that require it will be skipped.", li_skipped=ids)
        ready = [j for j in ids if j not in still_waiting]
        return AnswerResult(f"{word}. {len(ready)} LinkedIn job(s) go back in the queue.", li_ready=ready)

    job_id = item["job_id"]
    if any(i.get("job_id") == job_id for i in q):
        return AnswerResult(f"{word}.")
    return AnswerResult(f"{word}. All questions for #{job_id} answered, resuming the application.", job_id=job_id)


def answer_current(s: Session, text: str) -> tuple[str, int | None]:
    """Back-compat wrapper for ATS jobs."""
    r = answer(s, text)
    return r.reply, r.job_id
