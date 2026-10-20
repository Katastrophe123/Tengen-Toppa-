"""Apply flow for one approved job.

assist mode / LinkedIn / Naukri / Workday -> open page + resume, you submit.
known ATS (Greenhouse, Lever, Ashby)      -> Playwright adapter fills the form deterministically.
other sites                               -> Claude Code + Playwright MCP fills it (never submits).
Then: missing required answers -> asked on WhatsApp; CAPTCHA/login -> handed to you;
filled -> screenshot to WhatsApp with [Submit now], or auto-submit if apply.auto_submit = true.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page
from sqlmodel import Session, select

from tengen_toppa.analysis import JobAnalysis
from tengen_toppa.apply import browser as br
from tengen_toppa.apply.adapters import ADAPTERS, ASSIST_ONLY, GENERIC, Adapter, detect_ats
from tengen_toppa.apply.agent import run_agent
from tengen_toppa.apply.fields import FormField, normalize_question
from tengen_toppa.apply.form import captcha_visible, collect_fields, file_role, fill_form
from tengen_toppa.config import settings
from tengen_toppa.llm.claude_client import ClaudeError, ClaudeQuotaExhausted
from tengen_toppa.paths import get_paths
from tengen_toppa.pipeline import questions, steps
from tengen_toppa.profile import MasterProfile
from tengen_toppa.qa import resolve
from tengen_toppa.store.db import set_status
from tengen_toppa.store.models import ApplyAttempt, Job, JobStatus, QAItem, ResumeVersion
from tengen_toppa.tailoring.cover_letter import render_cover_letter, write_cover_letter
from tengen_toppa.whatsapp import notify
from tengen_toppa.whatsapp.client import Button

log = logging.getLogger(__name__)

_COVER_TEXT_RE = re.compile(r"\bcover letter\b|\bletter of motivation\b|\bmotivation letter\b")


class ApplyError(RuntimeError):
    pass


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _record(s: Session, job: Job, method: str, outcome: str, report: dict, shot: Path | None) -> ApplyAttempt:
    att = ApplyAttempt(
        job_id=job.id,
        method=method,
        outcome=outcome,
        report=report,  # type: ignore[arg-type]
        screenshot=str(shot) if shot else None,
    )
    s.add(att)
    s.commit()
    return att


def last_attempt(s: Session, job_id: int) -> ApplyAttempt | None:
    q = select(ApplyAttempt).where(ApplyAttempt.job_id == job_id).order_by(ApplyAttempt.id.desc())  # type: ignore[union-attr]
    return s.exec(q).first()


def _screenshot(page: Page, job_id: int, name: str) -> Path:
    out = get_paths().screenshots / str(job_id)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def _resume_pdf(rv: ResumeVersion | None) -> Path:
    if rv is None or not rv.pdf_path or not Path(rv.pdf_path).exists():
        raise ApplyError("no rendered resume for this job")
    return Path(rv.pdf_path)


def ensure_cover_letter(s: Session, job: Job, rv: ResumeVersion, prof: MasterProfile) -> ResumeVersion:
    if rv.cover_letter_pdf and Path(rv.cover_letter_pdf).exists():
        return rv
    cfg = settings()
    analysis = JobAnalysis.model_validate(job.analysis)
    letter, problems = write_cover_letter(
        prof,
        analysis,
        company=job.company,
        description=job.description,
        model=cfg.llm.tailor_model,
        timeout=cfg.llm.timeout_seconds,
    )
    steps.record_llm(
        s, "cover_letter", cfg.llm.tailor_model, job.id, ok=letter is not None, error="; ".join(problems) or None
    )
    if letter is None:
        log.warning("cover letter rejected for job %s: %s", job.id, problems)
        return rv
    out_dir = Path(rv.pdf_path).parent if rv.pdf_path else get_paths().resume_dir(job.id)  # type: ignore[arg-type]
    rv.cover_letter_pdf = str(render_cover_letter(letter, prof, job.company, out_dir))
    rv.cover_letter_text = letter.text + f"\n{prof.basics.name}"
    s.add(rv)
    s.commit()
    return rv


def _needs_cover_letter(fields: list[FormField]) -> bool:
    mode = settings().apply.cover_letter
    if mode == "never":
        return False
    if mode == "always":
        return True
    return any(
        (f.kind == "file" and file_role(f) == "cover_letter")
        or (f.kind == "textarea" and _COVER_TEXT_RE.search(normalize_question(f.label)))
        for f in fields
    )


def apply_job(s: Session, job_id: int) -> str:
    """Returns the outcome: assisted | filled | submitted | needs_answers | captcha | failed."""
    cfg = settings()
    prof = steps.profile()
    job = steps.get_job(s, job_id)
    rv = steps.latest_resume(s, job_id)
    resume = _resume_pdf(rv)
    assert rv is not None
    ats = detect_ats(job.apply_url)

    if cfg.apply.mode == "assist" or ats in ASSIST_ONLY:
        steps.assist_open(job, rv)
        _record(s, job, "assist", "assisted", {"ats": ats}, None)
        set_status(s, job, JobStatus.APPLYING, f"opened for you ({ats})")
        notify.send_text(
            s,
            f"#{job.id} {job.company}: {ats} applications are submitted by you. Page + resume are open "
            f"on your Mac (path on clipboard). Reply 'done {job.id}' after submitting.",
            job_id=job.id,
        )
        return "assisted"

    br.start_configured()
    set_status(s, job, JobStatus.APPLYING, f"filling via {ats}")

    adapter = ADAPTERS.get(ats)
    if adapter is None and cfg.apply.claude_fallback:
        return _apply_with_agent(s, job, rv, prof, resume)

    with br.attached(cfg.apply.cdp_port) as browser:
        br.close_pages(browser, _origin(job.apply_url))  # stale tabs from earlier attempts
        page = br.open_page(browser, job.apply_url)
        return fill_with_adapter(s, job, rv, prof, resume, page, adapter or GENERIC)


def fill_with_adapter(
    s: Session, job: Job, rv: ResumeVersion, prof: MasterProfile, resume: Path, page: Page, adapter: Adapter
) -> str:
    cfg = settings()
    adapter.prepare(page)
    if captcha_visible(page):
        return _handoff(s, job, page, adapter.name, "captcha", "A CAPTCHA/login blocks the form.")

    fields = collect_fields(page)
    if not fields:
        return _handoff(s, job, page, adapter.name, "failed", "Couldn't find the application form.")

    cover_pdf: Path | None = None
    if _needs_cover_letter(fields) and job.analysis:
        rv = ensure_cover_letter(s, job, rv, prof)
        cover_pdf = Path(rv.cover_letter_pdf) if rv.cover_letter_pdf else None

    resolution = resolve(
        s,
        fields,
        profile=prof,
        applicant=cfg.applicant,
        job_context=job.description,
        model=cfg.llm.analyze_model,
        timeout=cfg.llm.timeout_seconds,
    )
    if rv.cover_letter_text:
        for f in fields:
            if f.kind == "textarea" and _COVER_TEXT_RE.search(normalize_question(f.label)):
                resolution.answers[f.key] = rv.cover_letter_text
                resolution.sources[f.key] = "cover_letter"
                resolution.missing_required = [m for m in resolution.missing_required if m.key != f.key]

    report = fill_form(page, fields, resolution.answers, resume=resume, cover_letter=cover_pdf)
    shot = _screenshot(page, job.id, "filled")  # type: ignore[arg-type]
    summary = {
        "ats": adapter.name,
        "page_url": page.url,
        "filled": report.filled,
        "uploaded": report.uploaded,
        "sources": {f.label: resolution.sources.get(f.key) for f in fields if f.key in resolution.sources},
        "missing_required": [f.label for f in resolution.missing_required],
        "errors": report.errors,
    }

    if resolution.missing_required:
        _record(s, job, adapter.name, "needs_answers", summary, shot)
        set_status(s, job, JobStatus.NEEDS_HUMAN, f"{len(resolution.missing_required)} questions need you")
        questions.ask_questions(s, job.id, resolution.missing_required)  # type: ignore[arg-type]
        return "needs_answers"
    if captcha_visible(page):
        return _handoff(s, job, page, adapter.name, "captcha", "A CAPTCHA appeared on the form.", summary)

    if cfg.apply.auto_submit and not report.errors:
        _record(s, job, adapter.name, "filled", summary, shot)
        return submit_on_page(s, job, page, adapter)

    _record(s, job, adapter.name, "filled", summary, shot)
    set_status(s, job, JobStatus.APPLYING, "form filled, waiting for your submit")
    caption = (
        f"#{job.id} {job.company}: form filled ({len(report.filled)} fields, "
        f"{', '.join(report.uploaded.values()) or 'no upload'})."
    )
    if report.errors:
        caption += f"\nCheck: {'; '.join(report.errors[:3])}"
    caption += f"\nReview it on your Mac, or tap Submit now. Reply 'done {job.id}' if you submit yourself."
    notify.send_screenshot(
        s,
        shot,
        caption,
        job_id=job.id,  # type: ignore[arg-type]
        buttons=[Button(f"submit:{job.id}", "Submit now"), Button(f"skip:{job.id}", "Skip")],
    )
    return "filled"


def _handoff(s: Session, job: Job, page: Page, method: str, outcome: str, why: str, summary: dict | None = None) -> str:
    shot = _screenshot(page, job.id, outcome)  # type: ignore[arg-type]
    _record(s, job, method, outcome, summary or {"page_url": page.url}, shot)
    set_status(s, job, JobStatus.NEEDS_HUMAN, why)
    notify.send_screenshot(
        s,
        shot,
        f"#{job.id} {job.company}: {why} The tab is open on your Mac. "
        f"Reply 'done {job.id}' once you submit, or 'skip {job.id}'.",
        job_id=job.id,
    )  # type: ignore[arg-type]
    return outcome


def _apply_with_agent(s: Session, job: Job, rv: ResumeVersion, prof: MasterProfile, resume: Path) -> str:
    cfg = settings()
    bank = {q.question: q.answer for q in s.exec(select(QAItem))}
    cover = None
    if settings().apply.cover_letter == "always" and job.analysis:
        rv = ensure_cover_letter(s, job, rv, prof)
        cover = Path(rv.cover_letter_pdf) if rv.cover_letter_pdf else None
    with br.attached(cfg.apply.cdp_port) as browser:
        br.close_pages(browser, _origin(job.apply_url))  # stale tabs from earlier attempts
    try:
        res = run_agent(
            job_id=job.id,
            url=job.apply_url,
            profile=prof,
            applicant=cfg.applicant,
            qa_bank=bank,  # type: ignore[arg-type]
            resume=resume,
            cover_letter=cover,
            cdp_port=cfg.apply.cdp_port,
            model=cfg.llm.tailor_model,
            max_turns=cfg.apply.fallback_max_turns,
        )
    except ClaudeQuotaExhausted:
        steps.record_llm(s, "apply_agent", cfg.llm.tailor_model, job.id, ok=False, error="quota")
        raise
    except ClaudeError as e:
        steps.record_llm(s, "apply_agent", cfg.llm.tailor_model, job.id, ok=False, error=str(e)[:500])
        set_status(s, job, JobStatus.NEEDS_HUMAN, "browser agent failed")
        steps.assist_open(job, rv)
        notify.send_text(
            s, f"#{job.id}: automatic filling failed ({str(e)[:120]}). Opened the page for you.", job_id=job.id
        )
        return "failed"
    steps.record_llm(
        s, "apply_agent", cfg.llm.tailor_model, job.id, ok=True, duration_ms=res.duration_ms, cost=res.est_cost_usd
    )
    rep = res.value
    with br.attached(cfg.apply.cdp_port) as browser:
        page = (br.find_page(browser, rep.form_url) if rep.form_url else None) or br.find_page(
            browser, _origin(job.apply_url)
        )
        shot = _screenshot(page, job.id, "filled") if page else None  # type: ignore[arg-type]
        summary = {"ats": "claude_agent", "page_url": page.url if page else job.apply_url, **rep.model_dump()}
        if rep.captcha_or_login or not rep.reached_form:
            _record(s, job, "claude_agent", "captcha", summary, shot)
            set_status(s, job, JobStatus.NEEDS_HUMAN, "login/CAPTCHA or form not reachable")
            notify.send_text(
                s,
                f"#{job.id} {job.company}: needs you (login/CAPTCHA). Tab is open on your Mac. {rep.notes[:200]}",
                job_id=job.id,
                urgent=True,
            )
            return "captcha"
        if rep.unanswered_required:
            _record(s, job, "claude_agent", "needs_answers", summary, shot)
            set_status(s, job, JobStatus.NEEDS_HUMAN, f"{len(rep.unanswered_required)} questions need you")
            fields = [
                FormField(
                    key=q.question,
                    label=q.question,
                    kind="select" if q.options else "text",
                    required=True,
                    options=q.options,
                )
                for q in rep.unanswered_required
            ]
            questions.ask_questions(s, job.id, fields)  # type: ignore[arg-type]
            return "needs_answers"
        _record(s, job, "claude_agent", "filled", summary, shot)
        set_status(s, job, JobStatus.APPLYING, "form filled by agent, waiting for your submit")
        caption = (
            f"#{job.id} {job.company}: form filled by the browser agent ({len(rep.filled_fields)} fields). "
            f"Please review on your Mac and submit, then reply 'done {job.id}'."
        )
        if shot:
            notify.send_screenshot(
                s,
                shot,
                caption,
                job_id=job.id,  # type: ignore[arg-type]
                buttons=[Button(f"submit:{job.id}", "Submit now"), Button(f"skip:{job.id}", "Skip")],
            )
        else:
            notify.send_text(s, caption, job_id=job.id)
        return "filled"


def submit_job(s: Session, job_id: int) -> str:
    """Click submit on the tab filled earlier (after you tapped Submit now)."""
    job = steps.get_job(s, job_id)
    att = last_attempt(s, job_id)
    if att is None or att.outcome != "filled":
        notify.send_text(s, f"#{job_id} has no filled form waiting. Tap Apply first.", job_id=job_id)
        return "nothing_to_submit"
    cfg = settings()
    page_url = att.report.get("page_url") or job.apply_url
    adapter = ADAPTERS.get(att.report.get("ats", ""), GENERIC)
    with br.attached(cfg.apply.cdp_port) as browser:
        page = br.find_page(browser, page_url.split("?")[0])
        if page is None:
            notify.send_text(s, f"#{job_id}: the filled tab was closed. Tap Apply again to refill.", job_id=job_id)
            return "tab_closed"
        return submit_on_page(s, job, page, adapter)


def submit_on_page(s: Session, job: Job, page: Page, adapter: Adapter) -> str:
    before = page.url
    if not adapter.submit(page):
        return _handoff(s, job, page, adapter.name, "failed", "Couldn't find the submit button.")
    if captcha_visible(page):
        return _handoff(s, job, page, adapter.name, "captcha", "A CAPTCHA appeared after submit.")
    shot = _screenshot(page, job.id, "submitted")  # type: ignore[arg-type]
    if adapter.confirmed(page, before):
        _record(s, job, adapter.name, "submitted", {"page_url": page.url}, shot)
        set_status(s, job, JobStatus.APPLIED, f"submitted via {adapter.name}")
        notify.send_screenshot(s, shot, f"#{job.id} {job.company}: application submitted.", job_id=job.id)  # type: ignore[arg-type]
        return "submitted"
    return _handoff(
        s,
        job,
        page,
        adapter.name,
        "failed",
        "Clicked submit but couldn't confirm success (maybe a validation error).",
        {"page_url": page.url},
    )
