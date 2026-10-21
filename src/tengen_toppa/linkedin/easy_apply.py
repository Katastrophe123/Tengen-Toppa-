"""LinkedIn Easy Apply engine.

Safety model (learned the hard way: on some jobs clicking "Easy Apply" alone submits):
- Every click goes through `Clicker`. A click that can submit ("Easy Apply" itself, "Submit application")
  raises SubmitNotAllowed unless the caller passed allow_open / allow_submit.
- The caller writes a "submitting" record and checks the daily cap BEFORE allowing either.
- Aborting never clicks anything: it navigates the tab away (LinkedIn keeps a draft, nothing is sent).
- Unknown required answers are never guessed: the run stops for this job and asks you on WhatsApp.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PWTimeout

from tengen_toppa.apply.fields import FormField
from tengen_toppa.apply.form import collect_fields, fill_form
from tengen_toppa.linkedin.jobpage import easy_apply_button, inspect_job_page, job_url
from tengen_toppa.linkedin.search import LinkedInBlocked, check_blocked

log = logging.getLogger(__name__)

ROOT = "[data-tt-modal]"
SUBMIT_RE = re.compile(r"\bsubmit\b|\bsend application\b", re.I)
NEXT_RE = re.compile(r"^\s*(next|continue)\b|continue to next step", re.I)
REVIEW_RE = re.compile(r"^\s*review\b|review your application", re.I)
SUCCESS_RE = re.compile(
    r"application (was )?sent|your application was sent|application submitted|you applied|"
    r"applied to .* successfully|done applying",
    re.I,
)
PLACEHOLDER_RE = re.compile(r"^\s*(select( an option)?|please (select|choose)|choose( one)?|-+)?\s*$", re.I)

Outcome = Literal["applied", "ready", "needs_answers", "skipped", "failed", "blocked"]


class SubmitNotAllowed(RuntimeError):
    pass


@dataclass
class ApplyResult:
    outcome: Outcome
    detail: str = ""
    steps: int = 0
    answers: dict[str, str] = field(default_factory=dict)
    missing: list[FormField] = field(default_factory=list)
    screenshot: Path | None = None


@dataclass
class Clicker:
    allow_open: bool = False  # may click "Easy Apply" (can submit on one-click jobs)
    allow_submit: bool = False  # may click "Submit application"
    clicks: list[str] = field(default_factory=list)

    @staticmethod
    def name_of(loc: Locator) -> str:
        try:
            aria = loc.get_attribute("aria-label") or ""
            text = loc.inner_text(timeout=2000) or ""
        except Exception:
            aria = text = ""
        return f"{aria} {text}".strip()

    def click(self, loc: Locator, *, purpose: Literal["open", "next", "submit", "field"]) -> None:
        name = self.name_of(loc)
        if purpose == "open" and not self.allow_open:
            raise SubmitNotAllowed(f"opening Easy Apply not allowed ({name!r})")
        if purpose == "submit" and not self.allow_submit:
            raise SubmitNotAllowed(f"submit not allowed ({name!r})")
        if purpose in ("next", "field") and SUBMIT_RE.search(name):
            raise SubmitNotAllowed(f"refusing to click a submit-like control as {purpose!r}: {name!r}")
        self.clicks.append(f"{purpose}:{name[:60]}")
        try:
            loc.click(timeout=10000)
        except PWTimeout:
            # LinkedIn re-renders or removes the button mid-click (React). When the element is gone
            # after the timeout, the click itself went through (one-click Easy Apply submits instantly)
            # and the caller's state checks — success banner, modal, Applied — tell the truth.
            if loc.count() > 0:
                raise  # still on screen: the click never landed, a real failure


# ---------- modal discovery ----------

_MARK_JS = r"""
() => {
  document.querySelectorAll('[data-tt-modal]').forEach(e => e.removeAttribute('data-tt-modal'));
  const label = b => ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim();
  const re = /^\s*(next|continue|review|submit)\b|continue to next step|review your application|submit application/i;
  const btn = Array.from(document.querySelectorAll('button')).find(b => re.test(label(b)) && b.offsetParent !== null);
  let root = document.querySelector('[role="dialog"][aria-labelledby*="easy-apply" i], .jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"]');
  if (!root && btn) {
    root = btn;
    while (root && root !== document.body && !root.querySelector('input, select, textarea, [role="radiogroup"]')) root = root.parentElement;
    if (root && root.closest('[role="dialog"], [aria-modal="true"], .artdeco-modal')) root = root.closest('[role="dialog"], [aria-modal="true"], .artdeco-modal');
  }
  if (!root || root === document.body) return false;
  root.setAttribute('data-tt-modal', '1');
  return true;
}
"""


def mark_modal(page: Page) -> bool:
    return bool(page.evaluate(_MARK_JS))


def modal_text(page: Page) -> str:
    loc = page.locator(ROOT)
    return loc.first.inner_text(timeout=3000) if loc.count() else ""


def _button(page: Page, rx: re.Pattern) -> Locator | None:
    buttons = page.locator(f"{ROOT} button")
    for i in range(buttons.count()):
        b = buttons.nth(i)
        try:
            if not b.is_visible():
                continue
        except Exception:
            continue
        if rx.search(Clicker.name_of(b)):
            return b
    return None


def success_visible(page: Page) -> bool:
    try:
        body = page.inner_text("body", timeout=3000)[:20000]
    except Exception:
        return False
    return bool(SUCCESS_RE.search(body))


def inline_errors(page: Page) -> list[str]:
    return page.evaluate(r"""() => {
      const r = document.querySelector('[data-tt-modal]'); if (!r) return [];
      return Array.from(r.querySelectorAll('[role="alert"], .artdeco-inline-feedback--error, [class*="error"]'))
        .map(e => (e.innerText || '').trim()).filter(t => t && t.length < 200);
    }""")


# ---------- resume + follow ----------


def handle_resume(page: Page, resume: Path, display_name: str, clicker: Clicker) -> str | None:
    """Make sure THIS file is the one attached. Returns an error, or None."""
    file_inputs = page.locator(f"{ROOT} input[type=file]")
    if not file_inputs.count():
        return None  # only the resume step has an upload control (the review step just lists the file)
    stem = Path(display_name).stem
    cards = page.locator(ROOT).first.get_by_text(re.compile(re.escape(stem), re.I))
    if cards.count():
        card = cards.first
        checked = card.evaluate(r"""e => { const c = e.closest('[aria-checked], [aria-selected], label, li, div');
          const inp = c && c.querySelector('input[type=radio], input[type=checkbox]');
          return !!(c && (c.getAttribute('aria-checked') === 'true' || c.getAttribute('aria-selected') === 'true' ||
                    (inp && inp.checked) || /selected/i.test(c.className))); }""")
        if not checked:
            clicker.click(card, purpose="field")
        return None
    if file_inputs.count():
        file_inputs.first.set_input_files(str(resume))
        for _ in range(8):  # LinkedIn re-renders the window after an upload
            page.wait_for_timeout(500)
            if mark_modal(page) and stem.lower() in modal_text(page).lower():
                return None
        return f"uploaded {display_name} but LinkedIn does not show it"
    return f"resume step shown but {display_name} is not available to select"


def handle_follow(page: Page, want_follow: bool, clicker: Clicker) -> None:
    boxes = page.locator(f"{ROOT} input[type=checkbox]")
    for i in range(boxes.count()):
        box = boxes.nth(i)
        label = box.evaluate(
            "e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.id || '')"
        )
        if re.search(r"\bfollow\b", label, re.I) and box.is_checked() != want_follow:
            box.evaluate("e => e.click()")


# ---------- the engine ----------


def _clean_prefilled(fields: list[FormField]) -> list[FormField]:
    for f in fields:
        if f.kind in ("select", "combobox") and PLACEHOLDER_RE.match(f.value or ""):
            f.value = ""
    return fields


def _step_hash(page: Page) -> str:
    return hashlib.sha1(modal_text(page).encode()).hexdigest()


@dataclass
class EngineConfig:
    resume: Path
    display_name: str
    follow_company: bool = False
    max_steps: int = 10
    step_wait_ms: int = 1500
    leave_url: str | None = None  # where to go when aborting (default: the job page)


def leave(page: Page, job_id: str, url: str | None = None) -> None:
    """Abort without clicking anything: navigate away. LinkedIn keeps a draft; nothing is sent."""
    try:
        page.goto(url or job_url(job_id), wait_until="domcontentloaded", timeout=30000)
    except Exception:
        page.goto("about:blank")


def run_easy_apply(
    page: Page,
    job_id: str,
    cfg: EngineConfig,
    resolve_fields,  # (fields) -> (answers: dict, missing: list[FormField])
    clicker: Clicker,
    *,
    stop_before_submit: bool,
    screenshot_dir: Path,
) -> ApplyResult:
    """`page` must already show the job page. Opens Easy Apply (needs clicker.allow_open)."""
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    btn = easy_apply_button(page)
    if btn is None:
        info = inspect_job_page(page, job_id)
        return ApplyResult("skipped", "already applied" if info.applied else "no Easy Apply button")
    clicker.click(btn, purpose="open")
    answers: dict[str, str] = {}
    last_hash, repeats = "", 0
    for step in range(1, cfg.max_steps + 1):
        page.wait_for_timeout(cfg.step_wait_ms)
        try:
            check_blocked(page)
        except LinkedInBlocked as e:
            return ApplyResult("blocked", str(e), step)
        if success_visible(page) and not mark_modal(page):
            return ApplyResult("applied", "submitted (LinkedIn confirmed)", step, answers)
        if not mark_modal(page):
            info = inspect_job_page(page, job_id)
            if info.applied:
                return ApplyResult("applied", "one-click Easy Apply submitted", step, answers)
            shot = screenshot_dir / "no_modal.png"
            page.screenshot(path=str(shot))
            return ApplyResult("failed", "Easy Apply window not found", step, answers, screenshot=shot)

        h = _step_hash(page)
        repeats = repeats + 1 if h == last_hash else 0
        last_hash = h
        if repeats >= 2:
            errs = inline_errors(page)
            shot = screenshot_dir / f"stuck_{step}.png"
            page.screenshot(path=str(shot))
            leave(page, job_id, cfg.leave_url)
            return ApplyResult(
                "failed",
                "stuck on a step: " + ("; ".join(errs[:3]) or "no visible error"),
                step,
                answers,
                screenshot=shot,
            )

        if err := handle_resume(page, cfg.resume, cfg.display_name, clicker):
            leave(page, job_id, cfg.leave_url)
            return ApplyResult("failed", err, step, answers)
        handle_follow(page, cfg.follow_company, clicker)

        fields = [f for f in _clean_prefilled(collect_fields(page, ROOT)) if f.kind != "file"]
        todo = [f for f in fields if not (f.value or "").strip()]
        got, missing = resolve_fields(todo)
        if missing:
            leave(page, job_id, cfg.leave_url)
            return ApplyResult("needs_answers", f"{len(missing)} question(s) need you", step, answers, missing)
        report = fill_form(page, todo, got, resume=cfg.resume)
        answers.update({f.label: got[f.key] for f in todo if f.key in got})
        if report.errors:
            log.info("fill errors on %s step %d: %s", job_id, step, report.errors)

        submit = _button(page, re.compile(r"submit application|^\s*submit\b", re.I))
        if submit is not None:
            shot = screenshot_dir / "review.png"
            page.screenshot(path=str(shot), full_page=True)
            if stop_before_submit:
                return ApplyResult("ready", "filled; waiting for your Submit", step, answers, screenshot=shot)
            clicker.click(submit, purpose="submit")
            for _ in range(10):
                page.wait_for_timeout(1000)
                if success_visible(page):
                    break
            info = inspect_job_page(page, job_id) if not success_visible(page) else None
            if info is None or info.applied:
                done = screenshot_dir / "submitted.png"
                page.screenshot(path=str(done))
                return ApplyResult("applied", "submitted", step, answers, screenshot=done)
            return ApplyResult("failed", "clicked Submit but LinkedIn did not confirm", step, answers, screenshot=shot)

        nxt = _button(page, REVIEW_RE) or _button(page, NEXT_RE)
        if nxt is None:
            shot = screenshot_dir / f"no_button_{step}.png"
            page.screenshot(path=str(shot))
            leave(page, job_id, cfg.leave_url)
            return ApplyResult("failed", "no Next/Review/Submit button", step, answers, screenshot=shot)
        clicker.click(nxt, purpose="next")
    leave(page, job_id, cfg.leave_url)
    return ApplyResult("failed", f"more than {cfg.max_steps} steps", cfg.max_steps, answers)


def submit_ready(page: Page, job_id: str, clicker: Clicker, screenshot_dir: Path) -> ApplyResult:
    """Review mode: you tapped Submit now. The filled modal must still be open on this tab."""
    if not mark_modal(page):
        return ApplyResult("failed", "the filled Easy Apply window is no longer open")
    submit = _button(page, re.compile(r"submit application|^\s*submit\b", re.I))
    if submit is None:
        return ApplyResult("failed", "no Submit button on the open window")
    clicker.click(submit, purpose="submit")
    for _ in range(10):
        page.wait_for_timeout(1000)
        if success_visible(page):
            break
    ok = success_visible(page) or inspect_job_page(page, job_id).applied
    shot = screenshot_dir / "submitted.png"
    page.screenshot(path=str(shot))
    return ApplyResult(
        "applied" if ok else "failed", "submitted" if ok else "LinkedIn did not confirm", 0, screenshot=shot
    )
