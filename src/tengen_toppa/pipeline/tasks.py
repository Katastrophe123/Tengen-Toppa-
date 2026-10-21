"""Huey tasks (SQLite-backed queue). Run the worker with: tengen worker  (single worker => 1 Claude call at a time)."""

from __future__ import annotations

import logging

from huey import SqliteHuey, crontab
from huey.exceptions import RetryTask

from tengen_toppa.config import settings
from tengen_toppa.llm.claude_client import ClaudeQuotaExhausted
from tengen_toppa.paths import get_paths
from tengen_toppa.pipeline import steps
from tengen_toppa.store.db import backup_db, session, set_status
from tengen_toppa.store.models import Job, JobStatus
from tengen_toppa.whatsapp import notify

log = logging.getLogger(__name__)

_paths = get_paths()
_paths.data.mkdir(parents=True, exist_ok=True)
huey = SqliteHuey("tengen-toppa", filename=str(_paths.queue_db))

BACKOFF_SECONDS = int(steps.QUOTA_BACKOFF.total_seconds())
_hours = max(1, settings().pipeline.discover_every_hours)


def _llm_gate(s) -> None:
    """Raise RetryTask if Claude is in quota backoff, so the task is retried later instead of burning calls."""
    if steps.llm_paused_until(s) is not None:
        raise RetryTask(delay=BACKOFF_SECONDS)


# ---------- discovery & analysis ----------


@huey.periodic_task(crontab(minute="7", hour=f"*/{_hours}"))
def discover_periodic() -> None:
    discover_task()


@huey.task()
def discover_task() -> dict:
    with session() as s:
        if steps.is_paused(s):
            return {"skipped": "paused"}
        stats, errors = steps.discover(s)
    log.info("discovery: %s errors=%s", stats, errors)
    analyze_batch_task()
    return {"new": stats.new, "filtered": stats.filtered, "duplicates": stats.duplicates, "errors": errors}


@huey.task()
def analyze_batch_task() -> list[int]:
    with session() as s:
        if steps.is_paused(s):
            return []
        ids = steps.select_for_analysis(s, steps.profile(), settings().pipeline.analyze_per_run)
    for job_id in ids:
        analyze_job_task(job_id)
    drain_task()
    return ids


@huey.task(retries=2, retry_delay=120)
def analyze_job_task(job_id: int) -> str:
    with session() as s:
        job = s.get(Job, job_id)
        if job is None or job.status != JobStatus.DISCOVERED:
            return "noop"
        _llm_gate(s)
        try:
            job = steps.analyze(s, job_id)
        except ClaudeQuotaExhausted:
            steps.pause_llm(s)
            raise RetryTask(delay=BACKOFF_SECONDS) from None
        return str(job.status)


# ---------- tailoring & review ----------


@huey.periodic_task(crontab(minute="*/20"))
def drain_periodic() -> None:
    drain_task()


@huey.task()
def drain_task() -> int:
    """Queue tailoring for the best shortlisted jobs within today's cap, then push cards if possible."""
    with session() as s:
        if steps.is_paused(s) or steps.llm_paused_until(s):
            return 0
        room = steps.daily_cap(s) - steps.tailored_today(s)
        queued = 0
        if room > 0:
            shortlisted = sorted(
                steps.jobs_by_status(s, JobStatus.SHORTLISTED), key=lambda j: j.score or 0, reverse=True
            )
            for job in shortlisted[:room]:
                tailor_job_task(job.id)
                queued += 1
    flush_task()
    return queued


@huey.task(retries=1, retry_delay=120)
def tailor_job_task(job_id: int, feedback: str | None = None) -> str:
    with session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return "missing"
        if feedback is None:
            if job.status != JobStatus.SHORTLISTED:
                return "noop"
            if steps.tailored_today(s) >= steps.daily_cap(s):
                return "cap"
        _llm_gate(s)
        try:
            rv = steps.tailor_job(s, job_id, feedback=feedback)
        except ClaudeQuotaExhausted:
            steps.pause_llm(s)
            raise RetryTask(delay=BACKOFF_SECONDS) from None
        if rv is not None and rv.pdf_path:
            if feedback is not None:
                steps.send_card(s, job_id)
            else:
                steps.flush_cards(s, limit=1)
        return str(job.status)


@huey.task()
def flush_task(limit: int = 5) -> int:
    with session() as s:
        return steps.flush_cards(s, limit=limit)


@huey.task()
def send_card_task(job_id: int) -> bool:
    with session() as s:
        ok = steps.send_card(s, job_id)
        if not ok:
            notify.send_text(s, f"#{job_id} has no rendered resume yet.")
        return ok


@huey.task()
def approve_task(job_id: int) -> None:
    with session() as s:
        steps.mark_approved(s, job_id)
    apply_task(job_id)


@huey.task(retries=1, retry_delay=60)
def apply_task(job_id: int) -> str:
    from tengen_toppa.apply.runner import apply_job

    with session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return "missing"
        try:
            return apply_job(s, job_id)
        except ClaudeQuotaExhausted:
            steps.pause_llm(s)
            raise RetryTask(delay=BACKOFF_SECONDS) from None
        except Exception as e:
            log.exception("apply failed for job %s", job_id)
            set_status(s, job, JobStatus.NEEDS_HUMAN, f"apply error: {str(e)[:150]}")
            steps.assist_open(job, steps.latest_resume(s, job_id))
            notify.send_text(
                s,
                f"#{job_id} {job.company}: automatic apply hit an error ({str(e)[:120]}). "
                f"Opened the page + resume for you. Reply 'done {job_id}' after submitting.",
                job_id=job_id,
                urgent=True,
            )
            return "error"


@huey.task()
def submit_task(job_id: int) -> str:
    from tengen_toppa.apply.runner import submit_job

    with session() as s:
        try:
            return submit_job(s, job_id)
        except Exception as e:
            log.exception("submit failed for job %s", job_id)
            notify.send_text(
                s, f"#{job_id}: submit failed ({str(e)[:150]}). Please submit on your Mac.", job_id=job_id, urgent=True
            )
            return "error"


@huey.task()
def intake_task(url: str) -> str:
    """A job you sent: always analyzed + tailored (your choice beats score thresholds and the daily cap)."""
    from tengen_toppa.intake import add_job_from_url

    with session() as s:
        try:
            job = add_job_from_url(s, url)
        except Exception as e:
            log.exception("intake failed for %s", url)
            notify.send_text(s, f"Couldn't read that job: {str(e)[:200]}")
            return "error"
        notify.send_text(s, f"#{job.id} {job.title} @ {job.company}: analyzing + tailoring now.", job_id=job.id)
        _llm_gate(s)
        try:
            if not job.analysis:
                job.status = JobStatus.DISCOVERED
                job = steps.analyze(s, job.id)  # type: ignore[arg-type]
            if job.analysis:
                skip_reason = job.status_reason if job.status == JobStatus.SKIPPED else None
                rv = steps.tailor_job(s, job.id, feedback=None)  # type: ignore[arg-type]
                if rv and rv.pdf_path:
                    steps.send_card(s, job.id)  # type: ignore[arg-type]
                    if skip_reason:
                        notify.send_text(
                            s,
                            f"Note: this would normally be skipped ({skip_reason}). "
                            "Tailored anyway because you sent it.",
                            job_id=job.id,
                        )
        except ClaudeQuotaExhausted:
            steps.pause_llm(s)
            raise RetryTask(delay=BACKOFF_SECONDS) from None
        return str(job.status)


@huey.periodic_task(crontab(minute=f"*/{max(5, settings().email.poll_minutes)}"))
def email_periodic() -> int:
    if not settings().email.enabled:
        return 0
    from tengen_toppa.tracking import poll

    with session() as s:
        if steps.llm_paused_until(s):
            return 0
        try:
            return poll(s)
        except ClaudeQuotaExhausted:
            steps.pause_llm(s)
            return 0


# ---------- daily housekeeping ----------


@huey.periodic_task(crontab(minute="0", hour=str(settings().whatsapp.digest_hour)))
def digest_periodic() -> None:
    with session() as s:
        waiting = len(steps.jobs_by_status(s, JobStatus.TAILORED))
        if waiting == 0:
            return
        if notify.window_open(s):
            notify.send_text(s, f"Good morning. {waiting} tailored jobs are waiting for review.")
            steps.flush_cards(s)
        else:
            notify.send_digest_template(s, waiting)


@huey.periodic_task(crontab(minute="30", hour="3"))
def backup_periodic() -> None:
    dest = backup_db(get_paths().backups)
    log.info("backup written to %s", dest)
    backups = sorted(get_paths().backups.glob("tengen-*.db"))
    for old in backups[:-14]:  # keep two weeks
        old.unlink(missing_ok=True)


# LinkedIn tasks register on the same queue (imported last: they import `huey` from this module).
from tengen_toppa.linkedin import tasks as _linkedin_tasks  # noqa: E402, F401
