"""Read one LinkedIn job page (no clicks): description, years required, Easy Apply / applied / closed."""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Locator, Page

from tengen_toppa.experience import years_required
from tengen_toppa.linkedin.search import check_blocked

EASY_APPLY_NAME = re.compile(r"^\s*Easy Apply", re.I)
_APPLIED_RE = re.compile(
    r"\bApplied\s+\d+\s*(?:s|m|h|d|w|mo|yr)s?\b|\bApplied\s+(?:just now|today)|"
    r"Application submitted|\bView resume\b|See application",
    re.I,
)
_CLOSED_RE = re.compile(r"No longer accepting applications", re.I)
_END_MARKERS = ("Set alert for similar jobs", "About the company", "More jobs", "Show more\n", "Meet the hiring team")


@dataclass(frozen=True)
class JobPageInfo:
    job_id: str
    title: str
    company: str
    description: str
    years_required: float | None
    easy_apply: bool
    applied: bool
    closed: bool


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def easy_apply_button(page: Page) -> Locator | None:
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=EASY_APPLY_NAME)
        if loc.count():
            return loc.first
    return None


def extract_description(body: str) -> str:
    i = body.find("About the job")
    if i < 0:
        return ""
    chunk = body[i + len("About the job") :]
    ends = [chunk.find(m) for m in _END_MARKERS if chunk.find(m) > 0]
    return chunk[: min(ends)].strip() if ends else chunk[:12000].strip()


def read_job_page(page: Page, job_id: str, *, settle_ms: int = 3500) -> JobPageInfo:
    page.goto(job_url(job_id), wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(settle_ms)
    check_blocked(page)
    return inspect_job_page(page, job_id)


def inspect_job_page(page: Page, job_id: str) -> JobPageInfo:
    body = page.inner_text("body")
    desc = extract_description(body)
    parts = [p.strip() for p in page.title().split("|")]
    has_button = easy_apply_button(page) is not None
    head = body[: body.find("About the job")] if "About the job" in body else body[:3000]
    return JobPageInfo(
        job_id=job_id,
        title=parts[0] if parts else "",
        company=parts[1] if len(parts) > 2 else "",
        description=desc,
        years_required=years_required(desc) if desc else None,
        easy_apply=has_button,
        applied=(not has_button) and bool(_APPLIED_RE.search(head)),
        closed=bool(_CLOSED_RE.search(head)),
    )
