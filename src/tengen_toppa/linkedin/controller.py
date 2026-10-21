"""LinkedIn run controller. Every function does a small, bounded piece of work so the worker stays
responsive (dashboard, WhatsApp commands) between applications.

Run:   start_run -> harvest_step -> apply_step (one job) -> apply_step ... -> finish
Caps:  checked at the moment of the risky click, from the database (never from in-memory counters)
Safe:  a LiApplication(outcome="submitting") row is committed BEFORE any click that could submit;
       after a crash, recover_submitting() checks LinkedIn before anything is retried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from sqlmodel import Session, select

from tengen_toppa.apply.fields import FormField
from tengen_toppa.config import Settings, settings
from tengen_toppa.linkedin.easy_apply import ApplyResult
from tengen_toppa.linkedin.harvest import HarvestStats
from tengen_toppa.linkedin.jobpage import JobPageInfo
from tengen_toppa.linkedin.pool import (
    applied_last_hour,
    applied_today,
    daily_cap,
    next_candidates,
    pool_size,
    rank_pool,
)
from tengen_toppa.linkedin.search import LinkedInBlocked
from tengen_toppa.paths import get_paths
from tengen_toppa.pipeline import questions
from tengen_toppa.profile import MasterProfile, load_profile
from tengen_toppa.qa import resolve
from tengen_toppa.resume_file import ResumeError, current
from tengen_toppa.store.db import kv_get, kv_set
from tengen_toppa.store.models import LiApplication, LiJob, LiRun, LiState, utcnow
from tengen_toppa.whatsapp import notify
from tengen_toppa.whatsapp.client import Button

log = logging.getLogger(__name__)

ACTIVE = ("starting", "harvesting", "applying", "paused", "awaiting_you")
MAX_CHECKS_PER_STEP = 8
STALE_SUBMITTING = timedelta(minutes=3)


class Ops(Protocol):
    """Everything that touches the browser. LiveOps in live.py; fakes in tests."""

    def harvest(self, s: Session, cfg: Settings) -> HarvestStats: ...
    def check_job(self, job_id: str) -> JobPageInfo: ...
    def apply(
        self,
        job_id: str,
        *,
        allow_open: bool,
        allow_submit: bool,
        stop_before_submit: bool,
        resolve_fields,
        screenshot_dir: Path,
    ) -> ApplyResult: ...
    def submit_ready(self, job_id: str, screenshot_dir: Path) -> ApplyResult: ...
    def canary(self) -> list[str]: ...


@dataclass
class StepResult:
    delay: float | None  # seconds until the next apply_step; None = don't schedule
    note: str = ""


# ---------- state helpers ----------


def active_run(s: Session) -> LiRun | None:
    q = select(LiRun).where(LiRun.state.in_(ACTIVE)).order_by(LiRun.id.desc())  # type: ignore[attr-defined]
    return s.exec(q).first()


def stop_file() -> Path:
    return get_paths().home / "STOP"


def stop_reason(s: Session) -> str | None:
    if stop_file().exists():
        return f"STOP file present ({stop_file()})"
    if kv_get(s, "li_stop"):
        return "stop requested"
    return None


def request_stop(s: Session) -> None:
    kv_set(s, "li_stop", True)


def _event(s: Session, run: LiRun, text: str) -> None:
    run.last_event = text[:200]
    s.add(run)
    s.commit()


def finish(s: Session, run: LiRun, state: str, reason: str, *, urgent: bool = False) -> None:
    run.state, run.stop_reason, run.ended_at = state, reason, utcnow()
    s.add(run)
    s.commit()
    kv_set(s, "li_stop", False)
    notify.send_text(
        s,
        f"🏁 LinkedIn run #{run.id} {state}: {reason}\n"
        f"Applied {run.applied} · Skipped {run.skipped} · Failed {run.failed} · Waiting on you {run.waiting}\n"
        f"Today {applied_today(s)}/{daily_cap(s)} · Pool {pool_size(s)}",
        urgent=urgent,
    )


# ---------- preflight ----------


def preflight(s: Session, ops: Ops | None, *, kind: str, cfg: Settings | None = None) -> list[str]:
    cfg = cfg or settings()
    problems: list[str] = []
    if stop_file().exists():
        problems.append(f"remove the STOP file first ({stop_file()})")
    if kind != "dry":
        try:
            info = current(s)
            from tengen_toppa.resume_file import profile_mismatches

            if mism := profile_mismatches(info.path, load_profile(get_paths().profile)):
                problems.append("resume and profile disagree: " + "; ".join(mism[:3]))
        except ResumeError as e:
            problems.append(str(e))
        from tengen_toppa.whatsapp.health import commands_online

        if not commands_online(s):
            problems.append("WhatsApp commands are offline (tunnel down). Run `tengen tunnel-sync`.")
        if cfg.linkedin.mode == "auto":
            from tengen_toppa.whatsapp.health import alert_template_status

            status = alert_template_status(s)
            if status != "APPROVED":
                problems.append(
                    f"auto mode needs the '{cfg.whatsapp.alert_template}' template approved (status: {status})"
                )
    if ops is not None:
        problems += ops.canary()
    return problems


# ---------- run lifecycle ----------


def start_run(
    s: Session, ops: Ops | None, *, kind: str = "full", trigger: str = "manual", target: int | None = None
) -> tuple[LiRun | None, list[str]]:
    cfg = settings()
    if (run := active_run(s)) is not None:
        return None, [f"run #{run.id} is already {run.state}"]
    kv_set(s, "li_stop", False)
    problems = preflight(s, ops, kind=kind, cfg=cfg)
    if problems:
        return None, problems
    remaining = max(0, cfg.linkedin.max_applications_per_day - applied_today(s))
    run = LiRun(
        kind=kind,
        trigger=trigger,
        target=min(target if target is not None else remaining, remaining),
        state="harvesting",
    )
    s.add(run)
    s.commit()
    return run, []


def harvest_step(s: Session, run_id: int, ops: Ops) -> StepResult:
    cfg = settings()
    run = s.get(LiRun, run_id)
    if run is None or run.state not in ACTIVE:
        return StepResult(None, "run not active")
    _event(s, run, "harvesting jobs")
    try:
        stats = ops.harvest(s, cfg)
    except LinkedInBlocked as e:
        finish(s, run, "stopped", f"LinkedIn blocked harvesting: {e}", urgent=True)
        return StepResult(None, str(e))
    run.harvested, run.new_in_pool, run.pages = stats.seen, stats.eligible_new, stats.pages
    rank_pool(s, load_profile(get_paths().profile))
    if run.kind == "dry" or run.target <= 0:
        reason = "dry run complete" if run.kind == "dry" else "daily cap already reached"
        run.state = "done"
        s.add(run)
        s.commit()
        finish(
            s,
            run,
            "done",
            f"{reason}. Read {stats.pages} pages, {stats.seen} jobs, {stats.eligible_new} new "
            f"eligible. Pool now {stats.pool}.",
        )
        return StepResult(None, reason)
    run.state = "applying"
    s.add(run)
    s.commit()
    _event(s, run, f"pool {stats.pool}, applying (target {run.target})")
    return StepResult(1.0)


def _resolver(s: Session, profile: MasterProfile, cfg: Settings, job: LiJob):
    def fn(fields: list[FormField]):
        r = resolve(
            s,
            fields,
            profile=profile,
            applicant=cfg.applicant,
            job_context=job.description or "",
            model=cfg.llm.analyze_model,
            timeout=cfg.llm.timeout_seconds,
            experience=cfg.experience,
            job_location=job.location or "",
        )
        return r.answers, r.missing_required

    return fn


def _screens(job_id: str) -> Path:
    return get_paths().screenshots / f"li-{job_id}"


def recover_submitting(s: Session, ops: Ops) -> int:
    """C3: a 'submitting' row older than a few minutes means we crashed mid-apply. Ask LinkedIn."""
    cutoff = utcnow() - STALE_SUBMITTING
    stale = list(
        s.exec(select(LiApplication).where(LiApplication.outcome == "submitting", LiApplication.created_at < cutoff))
    )
    for app in stale:
        job = s.get(LiJob, app.job_id)
        try:
            info = ops.check_job(app.job_id)
        except LinkedInBlocked:
            raise
        except Exception as e:
            log.warning("recovery check failed for %s: %s", app.job_id, e)
            continue
        if info.applied:
            app.outcome, app.submitted_at, app.detail = (
                "applied",
                app.submitted_at or utcnow(),
                "confirmed after restart",
            )
            if job:
                job.state = LiState.APPLIED
        else:
            app.outcome, app.detail = "failed", "interrupted before submit (nothing sent)"
            if job:
                job.state = LiState.POOLED if job.attempts < 2 else LiState.FAILED
        s.add(app)
        if job:
            s.add(job)
        s.commit()
    return len(stale)


def _pick_eligible(s: Session, ops: Ops, cfg: Settings, run: LiRun) -> LiJob | None:
    """Read job pages (no clicks) until one passes every check. Bounded per step."""
    for _ in range(MAX_CHECKS_PER_STEP):
        cands = next_candidates(s, 1)
        if not cands:
            return None
        job = cands[0]
        info = ops.check_job(job.job_id)
        job.description = info.description or job.description
        job.years_required = info.years_required
        job.updated_at = utcnow()
        reason = None
        if info.closed:
            job.state, reason = LiState.CLOSED, "no longer accepting applications"
        elif info.applied:
            job.state, reason = LiState.APPLIED, "already applied (outside Tengen)"
        elif not info.easy_apply:
            job.state, reason = LiState.INELIGIBLE, "no Easy Apply button"
        elif info.years_required is not None and info.years_required > cfg.experience.max_years_required:
            job.state = LiState.INELIGIBLE
            reason = f"needs {info.years_required:g}+ years (max {cfg.experience.max_years_required:g})"
        job.skip_reason = reason
        s.add(job)
        s.commit()
        if reason is None:
            return job
        run.skipped += 1
        s.add(run)
        s.commit()
    return None


def apply_step(s: Session, run_id: int, ops: Ops) -> StepResult:
    cfg = settings()
    li = cfg.linkedin
    run = s.get(LiRun, run_id)
    if run is None or run.state not in ACTIVE:
        return StepResult(None, "run not active")
    if reason := stop_reason(s):
        finish(s, run, "stopped", reason)
        return StepResult(None, reason)
    if kv_get(s, "paused") or run.state == "paused":
        if run.state != "paused":
            run.state = "paused"
            s.add(run)
            s.commit()
        return StepResult(None, "paused")
    if run.state == "awaiting_you":
        return StepResult(None, "waiting for your approval")

    try:
        recover_submitting(s, ops)
    except LinkedInBlocked as e:
        finish(s, run, "stopped", f"LinkedIn blocked: {e}", urgent=True)
        return StepResult(None, str(e))

    done_today = applied_today(s)
    if done_today >= daily_cap(s):
        finish(s, run, "done", f"daily cap reached ({done_today}/{daily_cap(s)})")
        return StepResult(None, "daily cap")
    if run.applied >= run.target:
        finish(s, run, "done", f"run target reached ({run.applied})")
        return StepResult(None, "target")
    if applied_last_hour(s) >= li.max_per_hour:
        _event(s, run, f"hourly cap {li.max_per_hour} reached, waiting")
        return StepResult(300.0, "hourly cap")
    if run.consecutive_failures >= li.stop_after_consecutive_failures:
        finish(s, run, "stopped", f"{run.consecutive_failures} failures in a row; check the screenshots", urgent=True)
        return StepResult(None, "failures")

    try:
        job = _pick_eligible(s, ops, cfg, run)
    except LinkedInBlocked as e:
        finish(s, run, "stopped", f"LinkedIn blocked: {e}", urgent=True)
        return StepResult(None, str(e))
    if job is None:
        if next_candidates(s, 1):
            return StepResult(5.0, "skipped ineligible jobs, continuing")
        finish(s, run, "done", "no eligible jobs left in the pool (next run tops it up)")
        return StepResult(None, "pool empty")

    if li.mode == "review":
        job.state = LiState.QUEUED
        s.add(job)
        run.state = "awaiting_you"
        s.add(run)
        s.commit()
        yrs = f"{job.years_required:g}+ yrs" if job.years_required is not None else "years not stated"
        notify.send_text(
            s,
            f"🔎 Next LinkedIn job: {job.title} @ {job.company}\n{job.location} · {yrs} · "
            f"score {job.score or 0:.2f}\n{job.url}",
        )
        _send_buttons(
            s,
            f"Apply to {job.title} @ {job.company}?",
            [Button(f"liapply:{job.job_id}", "Apply"), Button(f"liskip:{job.job_id}", "Skip")],
        )
        return StepResult(None, "awaiting approval")

    return _apply_now(s, run, job, ops, stop_before_submit=li.mode != "auto")


def _send_buttons(s: Session, body: str, buttons: list[Button]) -> None:
    client = notify.get_client()
    if client is None or not notify.window_open(s):
        notify.send_text(s, body + "\nReply: " + " / ".join(b.id for b in buttons), urgent=True)
        return
    try:
        client.send_buttons(body, buttons)
    except Exception as e:
        log.warning("buttons failed: %s", e)
        notify.send_text(s, body)


def _apply_now(s: Session, run: LiRun, job: LiJob, ops: Ops, *, stop_before_submit: bool) -> StepResult:
    cfg = settings()
    li = cfg.linkedin
    # Cap check at the moment of the risky click (the Easy Apply click itself can submit).
    if applied_today(s) >= daily_cap(s):
        finish(s, run, "done", "daily cap reached")
        return StepResult(None, "daily cap")
    try:
        resume = current(s)
    except ResumeError as e:
        finish(s, run, "stopped", str(e), urgent=True)
        return StepResult(None, str(e))
    app = LiApplication(job_id=job.job_id, run_id=run.id, outcome="submitting", resume_sha=resume.sha256)
    job.state, job.attempts = LiState.SUBMITTING, job.attempts + 1
    s.add(app)
    s.add(job)
    s.commit()  # committed BEFORE the click
    _event(s, run, f"applying: {job.title} @ {job.company}")

    try:
        res = ops.apply(
            job.job_id,
            allow_open=True,
            allow_submit=not stop_before_submit,
            stop_before_submit=stop_before_submit,
            resolve_fields=_resolver(s, load_profile(get_paths().profile), cfg, job),
            screenshot_dir=_screens(job.job_id),
        )
    except LinkedInBlocked as e:
        finish(s, run, "stopped", f"LinkedIn blocked: {e}", urgent=True)
        return StepResult(None, str(e))
    except Exception as e:  # leave 'submitting'; recovery asks LinkedIn before any retry
        log.exception("apply crashed for %s", job.job_id)
        run.failed += 1
        run.consecutive_failures += 1
        s.add(run)
        s.commit()
        return StepResult(float(li.apply_interval_seconds), f"crash: {e}")
    return record_result(s, run, job, app, res)


def record_result(s: Session, run: LiRun, job: LiJob, app: LiApplication, res: ApplyResult) -> StepResult:
    li = settings().linkedin
    app.steps, app.answers, app.detail = res.steps, res.answers, res.detail
    app.screenshot = str(res.screenshot) if res.screenshot else None
    delay: float | None = float(li.apply_interval_seconds)
    match res.outcome:
        case "applied":
            app.outcome, app.submitted_at = "applied", utcnow()
            job.state, job.skip_reason = LiState.APPLIED, None
            run.applied += 1
            run.consecutive_failures = 0
            note = f"✅ {job.title} @ {job.company}"
        case "ready":
            app.outcome = "ready"
            job.state = LiState.QUEUED
            run.state = "awaiting_you"
            delay = None
            note = "ready for your Submit"
            if res.screenshot:
                notify.send_screenshot(
                    s,
                    res.screenshot,
                    f"Filled: {job.title} @ {job.company}. Check it and tap Submit now.",
                    job_id=None,
                    buttons=[Button(f"lisubmit:{job.job_id}", "Submit now"), Button(f"liskip:{job.job_id}", "Skip")],
                )  # type: ignore[arg-type]
        case "needs_answers":
            app.outcome = "needs_answers"
            job.state, job.skip_reason = LiState.WAITING_ANSWER, res.detail
            run.waiting += 1
            questions.ask_li_questions(s, job.job_id, res.missing)
            delay = 5.0
            note = f"❓ {job.title}: {res.detail}"
        case "skipped":
            app.outcome = "skipped"
            job.state = LiState.APPLIED if "already applied" in res.detail else LiState.SKIPPED
            job.skip_reason = res.detail
            run.skipped += 1
            delay = 5.0
            note = f"⏭ {job.title}: {res.detail}"
        case "blocked":
            app.outcome = "submitting"  # unknown whether it went through; recovery will check
            s.add(app)
            s.commit()
            finish(s, run, "stopped", f"LinkedIn blocked: {res.detail}", urgent=True)
            return StepResult(None, res.detail)
        case _:
            app.outcome = "failed"
            job.state = LiState.POOLED if job.attempts < 2 else LiState.FAILED
            job.skip_reason = res.detail
            run.failed += 1
            run.consecutive_failures += 1
            note = f"❌ {job.title}: {res.detail}"
    job.updated_at = utcnow()
    for obj in (app, job, run):
        s.add(obj)
    s.commit()
    _event(s, run, note)
    return StepResult(delay, note)


# ---------- review mode: your taps ----------


def approve(s: Session, job_id: str, ops: Ops) -> StepResult:
    run = active_run(s)
    job = s.get(LiJob, job_id)
    if run is None or job is None:
        return StepResult(None, "no active run for that job")
    run.state = "applying"
    s.add(run)
    s.commit()
    return _apply_now(s, run, job, ops, stop_before_submit=True)


def submit(s: Session, job_id: str, ops: Ops) -> StepResult:
    run = active_run(s)
    job = s.get(LiJob, job_id)
    app = s.exec(
        select(LiApplication)
        .where(LiApplication.job_id == job_id, LiApplication.outcome == "ready")
        .order_by(LiApplication.id.desc())
    ).first()  # type: ignore[union-attr]
    if job is None or app is None:
        return StepResult(None, "nothing filled and waiting for that job")
    if applied_today(s) >= daily_cap(s):
        return StepResult(None, "daily cap reached; not submitting")
    app.outcome = "submitting"
    s.add(app)
    s.commit()
    res = ops.submit_ready(job_id, _screens(job_id))
    if run is None:
        run = LiRun(kind="full", state="done", trigger="review")
        s.add(run)
        s.commit()
    if run.state == "awaiting_you":
        run.state = "applying"
    if res.outcome != "applied":
        app.outcome = "ready"  # the filled tab may still be there; you can retry or skip
        s.add(app)
        s.commit()
        return StepResult(None, res.detail)
    return record_result(s, run, job, app, res)


def skip(s: Session, job_id: str) -> StepResult:
    job = s.get(LiJob, job_id)
    if job is None:
        return StepResult(None, "unknown job")
    job.state, job.skip_reason = LiState.SKIPPED, "skipped by you"
    s.add(job)
    for app in s.exec(select(LiApplication).where(LiApplication.job_id == job_id, LiApplication.outcome == "ready")):
        app.outcome = "skipped"
        s.add(app)
    run = active_run(s)
    if run and run.state == "awaiting_you":
        run.state = "applying"
        run.skipped += 1
        s.add(run)
    s.commit()
    return StepResult(2.0 if run else None, "skipped")


def requeue_answered(s: Session, job_ids: list[str]) -> int:
    n = 0
    for jid in job_ids:
        job = s.get(LiJob, jid)
        if job and job.state == LiState.WAITING_ANSWER:
            job.state, job.skip_reason = LiState.POOLED, None
            s.add(job)
            n += 1
    s.commit()
    return n


def skip_unanswerable(s: Session, job_ids: list[str]) -> None:
    for jid in job_ids:
        job = s.get(LiJob, jid)
        if job and job.state == LiState.WAITING_ANSWER:
            job.state, job.skip_reason = LiState.SKIPPED, "required question skipped by you"
            s.add(job)
    s.commit()
