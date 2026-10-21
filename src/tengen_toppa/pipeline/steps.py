"""Pipeline steps as plain synchronous functions (easy to test, reused by Huey tasks and the CLI)."""

from __future__ import annotations

import logging
import platform
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from tengen_toppa.analysis import JobAnalysis, analyze_job, final_score, post_filter
from tengen_toppa.config import settings
from tengen_toppa.discovery.base import JobPosting
from tengen_toppa.discovery.sources import fetch_all
from tengen_toppa.filtering import hard_filter, prefilter_scores
from tengen_toppa.llm.claude_client import ClaudeError, ClaudeQuotaExhausted
from tengen_toppa.paths import get_paths
from tengen_toppa.profile import MasterProfile, load_profile
from tengen_toppa.store.db import kv_get, kv_set, set_status
from tengen_toppa.store.models import Event, Job, JobStatus, LLMCall, ResumeVersion, utcnow
from tengen_toppa.tailoring.render import ats_parse_check, build_document, pdf_filename, render_pdf
from tengen_toppa.tailoring.schema import TailoredResume
from tengen_toppa.tailoring.tailor import tailor
from tengen_toppa.whatsapp import notify

log = logging.getLogger(__name__)

QUOTA_BACKOFF = timedelta(minutes=30)


class JobNotFound(LookupError):
    pass


def profile() -> MasterProfile:
    return load_profile(get_paths().profile)


def jobs_by_status(s: Session, status: JobStatus) -> list[Job]:
    return list(s.exec(select(Job).where(Job.status == status)))


def get_job(s: Session, job_id: int) -> Job:
    job = s.get(Job, job_id)
    if job is None:
        raise JobNotFound(f"job #{job_id} not found")
    return job


# ---------- runtime controls (WhatsApp commands override settings.toml) ----------


def is_paused(s: Session) -> bool:
    return bool(kv_get(s, "paused", False))


def llm_paused_until(s: Session) -> datetime | None:
    raw = kv_get(s, "llm_paused_until")
    if not raw:
        return None
    until = datetime.fromisoformat(raw)
    return until if until > utcnow() else None


def pause_llm(s: Session) -> datetime:
    until = utcnow() + QUOTA_BACKOFF
    already = llm_paused_until(s)
    kv_set(s, "llm_paused_until", until.isoformat())
    if already is None:
        notify.send_text(
            s,
            "Claude subscription limit reached. Pipeline paused ~30 min, will resume automatically.",
            urgent=True,
        )
    return until


def daily_cap(s: Session) -> int:
    return int(kv_get(s, "daily_cap", settings().pipeline.daily_tailor_cap))


def local_midnight_utc() -> datetime:
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def tailored_today(s: Session) -> int:
    q = select(Event).where(Event.type == "tailored", Event.created_at >= local_midnight_utc())
    return len(list(s.exec(q)))


def record_llm(
    s: Session,
    step: str,
    model: str,
    job_id: int | None,
    *,
    ok: bool,
    duration_ms: int = 0,
    cost: float | None = None,
    error: str | None = None,
) -> None:
    s.add(
        LLMCall(
            step=step,
            model=model,
            ok=ok,
            duration_ms=duration_ms,
            est_cost_usd=cost,
            error=error,
            job_id=job_id,
        )
    )
    s.commit()


# ---------- discovery ----------


@dataclass
class IngestStats:
    new: int = 0
    filtered: int = 0
    duplicates: int = 0


def ingest(s: Session, postings: list[JobPosting]) -> IngestStats:
    cfg = settings().search
    stats = IngestStats()
    for p in postings:
        exists = s.exec(
            select(Job.id).where(
                ((Job.source == p.source) & (Job.external_id == p.external_id)) | (Job.fingerprint == p.fingerprint)
            )
        ).first()
        if exists is not None:
            stats.duplicates += 1
            continue
        verdict = hard_filter(p, cfg)
        job = Job(
            source=p.source,
            external_id=p.external_id,
            company=p.company,
            title=p.title,
            location=p.location,
            remote=p.remote,
            url=p.url,
            apply_url=p.apply_url,
            description=p.description,
            fingerprint=p.fingerprint,
            posted_at=p.posted_at,
            status=JobStatus.DISCOVERED if verdict.ok else JobStatus.FILTERED_OUT,
            status_reason=verdict.reason,
        )
        s.add(job)
        if verdict.ok:
            stats.new += 1
        else:
            stats.filtered += 1
    s.commit()
    return stats


def discover(s: Session) -> tuple[IngestStats, list[str]]:
    src = settings().sources
    postings, errors = fetch_all(src.greenhouse.boards, src.lever.companies, src.ashby.boards)
    stats = ingest(s, postings)
    s.add(
        Event(
            type="discovered",
            payload={
                "new": stats.new,
                "filtered": stats.filtered,
                "duplicates": stats.duplicates,
                "errors": errors,
            },
        )
    )
    s.commit()
    return stats, errors


def select_for_analysis(s: Session, prof: MasterProfile, limit: int) -> list[int]:
    """Free local ranking; only the top `limit` jobs go to Claude. The rest are parked as filtered_out."""
    jobs = list(s.exec(select(Job).where(Job.status == JobStatus.DISCOVERED)))
    if not jobs:
        return []
    postings = [
        JobPosting(
            j.source,
            j.external_id,
            j.company,
            j.title,
            j.location,
            j.remote,
            j.url,
            j.apply_url,
            j.description,
        )
        for j in jobs
    ]
    scores = prefilter_scores(postings, prof)
    ranked = sorted(zip(jobs, scores, strict=True), key=lambda x: x[1], reverse=True)
    chosen: list[int] = []
    for i, (job, score) in enumerate(ranked):
        job.prefilter_score = round(score, 4)
        if i < limit:
            chosen.append(job.id)  # type: ignore[arg-type]
            s.add(job)
        else:
            job.status = JobStatus.FILTERED_OUT
            job.status_reason = f"prefilter rank {i + 1} > {limit}"
            s.add(job)
    s.commit()
    return chosen


# ---------- analysis ----------


def analyze(s: Session, job_id: int, prof: MasterProfile | None = None) -> Job:
    prof = prof or profile()
    job = get_job(s, job_id)
    cfg = settings()
    model = cfg.llm.analyze_model
    try:
        res = analyze_job(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description,
            profile=prof,
            model=model,
            timeout=cfg.llm.timeout_seconds,
        )
    except ClaudeQuotaExhausted:
        record_llm(s, "analyze", model, job_id, ok=False, error="quota")
        raise
    except ClaudeError as e:
        record_llm(s, "analyze", model, job_id, ok=False, error=str(e)[:500])
        set_status(s, job, JobStatus.FAILED, f"analysis failed: {str(e)[:200]}")
        return job
    record_llm(s, "analyze", model, job_id, ok=True, duration_ms=res.duration_ms, cost=res.est_cost_usd)

    analysis = res.value
    job.analysis = analysis.model_dump()
    job.score = final_score(analysis, prof, job.description)
    s.add(job)
    s.commit()
    reason = post_filter(analysis, prof, cfg.search)
    if reason:
        set_status(s, job, JobStatus.SKIPPED, reason)
    elif analysis.red_flags and job.score < 85:
        set_status(s, job, JobStatus.SKIPPED, "red flags: " + "; ".join(analysis.red_flags)[:200])
    elif job.score < cfg.pipeline.min_score:
        set_status(s, job, JobStatus.SKIPPED, f"score {job.score} < {cfg.pipeline.min_score}")
    else:
        set_status(s, job, JobStatus.SHORTLISTED, f"score {job.score}")
    return job


# ---------- tailoring ----------


def latest_resume(s: Session, job_id: int) -> ResumeVersion | None:
    q = select(ResumeVersion).where(ResumeVersion.job_id == job_id).order_by(ResumeVersion.version.desc())  # type: ignore[attr-defined]
    return s.exec(q).first()


def tailor_job(
    s: Session, job_id: int, *, feedback: str | None = None, prof: MasterProfile | None = None
) -> ResumeVersion | None:
    prof = prof or profile()
    job = get_job(s, job_id)
    if not job.analysis:
        raise ValueError(f"job #{job_id} has no analysis yet")
    cfg = settings()
    model = cfg.llm.tailor_model
    analysis = JobAnalysis.model_validate(job.analysis)
    try:
        out = tailor(
            prof,
            analysis,
            job.description,
            model=model,
            timeout=cfg.llm.timeout_seconds,
            max_attempts=cfg.llm.max_tailor_attempts,
            feedback=feedback,
            allow_project_reorder=cfg.pipeline.allow_project_reorder,
            max_bullets_per_role=cfg.pipeline.max_bullets_per_role,
        )
    except ClaudeQuotaExhausted:
        record_llm(s, "tailor", model, job_id, ok=False, error="quota")
        raise
    except ClaudeError as e:
        record_llm(s, "tailor", model, job_id, ok=False, error=str(e)[:500])
        set_status(s, job, JobStatus.FAILED, f"tailoring failed: {str(e)[:200]}")
        return None
    for d, c in zip(out.durations_ms, out.costs, strict=True):
        record_llm(s, "tailor", model, job_id, ok=True, duration_ms=d, cost=c)

    prev = latest_resume(s, job_id)
    version = (prev.version + 1) if prev else 1
    assert out.report is not None and out.tailored is not None
    rv = ResumeVersion(
        job_id=job_id,
        version=version,
        feedback=feedback,
        tailored=out.tailored.model_dump(),
        validation=out.report.model_dump(),
    )
    if not out.report.ok:
        s.add(rv)
        s.commit()
        set_status(s, job, JobStatus.NEEDS_HUMAN, "guardrails rejected all attempts")
        notify.send_text(
            s,
            f"#{job.id} {job.company}: tailoring rejected by guardrails after {out.attempts} attempts.\n"
            + "\n".join(f"- {v}" for v in out.report.violations[:5])
            + f"\nReply 'skip {job.id}' or 'edit {job.id} <instructions>'.",
            job_id=job.id,
        )
        return rv

    pdf = render_resume(prof, out.tailored, job, version)
    rv.pdf_path = str(pdf)
    ats_problems = ats_parse_check(pdf, prof)
    if ats_problems:
        rv.validation = {**rv.validation, "warnings": rv.validation.get("warnings", []) + ats_problems}
    s.add(rv)
    s.add(Event(job_id=job_id, type="tailored", payload={"version": version, "attempts": out.attempts}))
    s.commit()
    set_status(s, job, JobStatus.TAILORED, f"resume v{version}")
    return rv


def render_resume(prof: MasterProfile, tailored: TailoredResume | None, job: Job, version: int) -> Path:
    out_dir = get_paths().resume_dir(job.id) / f"v{version}"  # type: ignore[arg-type]
    return render_pdf(build_document(prof, tailored), out_dir, pdf_filename(prof.basics.name, job.company, job.title))


# ---------- review / WhatsApp ----------


def flush_cards(s: Session, limit: int = 5) -> int:
    """Send the best tailored jobs as WhatsApp cards, if the 24h window is open."""
    if not notify.window_open(s):
        return 0
    q = select(Job).where(Job.status == JobStatus.TAILORED).order_by(Job.score.desc()).limit(limit)  # type: ignore[union-attr]
    sent = 0
    for job in s.exec(q).all():
        rv = latest_resume(s, job.id)  # type: ignore[arg-type]
        if rv and notify.send_job_card(s, job, rv):
            set_status(s, job, JobStatus.PENDING_REVIEW)
            sent += 1
    remaining = len(list(s.exec(select(Job.id).where(Job.status == JobStatus.TAILORED))))
    if sent and remaining:
        notify.send_text(s, f"{remaining} more waiting. Reply 'more' for the next batch.")
    return sent


def send_card(s: Session, job_id: int) -> bool:
    job = get_job(s, job_id)
    rv = latest_resume(s, job_id)
    if rv is None or not rv.pdf_path:
        return False
    if notify.send_job_card(s, job, rv):
        if job.status == JobStatus.TAILORED:
            set_status(s, job, JobStatus.PENDING_REVIEW)
        return True
    return False


def assist_open(job: Job, rv: ResumeVersion | None) -> bool:
    """Assisted apply: open the application page + reveal the resume in Finder (path copied to clipboard)."""
    if platform.system() != "Darwin":
        return False
    subprocess.run(["open", job.apply_url], check=False)
    if rv and rv.pdf_path:
        subprocess.run(["open", "-R", rv.pdf_path], check=False)
        subprocess.run(["pbcopy"], input=rv.pdf_path, text=True, check=False)
    return True


def mark_approved(s: Session, job_id: int) -> Job:
    job = get_job(s, job_id)
    set_status(s, job, JobStatus.APPROVED, "approved by you")
    return job


def skip(s: Session, job_id: int, reason: str = "skipped by you") -> Job:
    job = get_job(s, job_id)
    set_status(s, job, JobStatus.SKIPPED, reason)
    return job


def mark_applied(s: Session, job_id: int) -> Job:
    job = get_job(s, job_id)
    set_status(s, job, JobStatus.APPLIED, "submitted")
    return job


def counts_by_status(s: Session) -> dict[str, int]:
    out: dict[str, int] = {}
    for st in s.exec(select(Job.status)).all():
        out[str(st)] = out.get(str(st), 0) + 1
    return out


def status_text(s: Session) -> str:
    c = counts_by_status(s)
    since = local_midnight_utc()
    calls = list(s.exec(select(LLMCall).where(LLMCall.created_at >= since)))
    paused_until = llm_paused_until(s)
    lines = [
        "Tengen-Toppa status",
        f"Paused: {'yes' if is_paused(s) else 'no'}"
        + (f" | Claude backoff until {paused_until:%H:%M} UTC" if paused_until else ""),
        f"Tailored today: {tailored_today(s)}/{daily_cap(s)}",
        f"Claude calls today: {len(calls)} ({sum(1 for c in calls if not c.ok)} failed)",
        "Jobs: " + (", ".join(f"{k} {v}" for k, v in sorted(c.items())) or "none yet"),
    ]
    return "\n".join(lines)


HELP_TEXT = """Commands

LinkedIn Easy Apply:
run - start a LinkedIn run (checks, read jobs, apply up to today's cap)
run dry - read and rank jobs only, apply to nothing
stop - stop after the current step
dash - live dashboard now
failed / skipped - last 10 with reasons
answers - repeat the pending form question
cap N - lower today's LinkedIn cap
report - statuses and view rate

Other jobs:
<any job link> - add a job (Greenhouse, Lever, Ashby, company sites)
status - pipeline status
more - next batch of job cards
job N - resend card for job N
apply N / skip N / submit N / done N - job actions
edit N <what to change> - re-tailor resume

pause / resume - stop or start everything
help - this message"""
