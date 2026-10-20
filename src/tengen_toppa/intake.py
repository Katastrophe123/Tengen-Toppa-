"""Add a job from any URL (send it on WhatsApp or `tengen add <url>`).

Greenhouse / Lever / Ashby URLs use their public APIs. Anything else (LinkedIn, Naukri, Wellfound,
company sites) is read from your dedicated Chrome, so your own logged-in session is used for viewing
only, then Claude Haiku extracts the job. LinkedIn/Naukri jobs are applied in assist mode.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from tengen_toppa.apply import browser as br
from tengen_toppa.apply.adapters import detect_ats
from tengen_toppa.apply.form import page_text
from tengen_toppa.config import settings
from tengen_toppa.discovery.base import JobPosting, html_to_text, looks_remote, parse_dt
from tengen_toppa.discovery.sources import UA
from tengen_toppa.llm.claude_client import ask
from tengen_toppa.store.models import Job, JobStatus

URL_RE = re.compile(r"https?://[^\s<>\"']+")


class IntakeError(RuntimeError):
    pass


class ExtractedJob(BaseModel):
    is_job_posting: bool
    title: str
    company: str
    location: str = ""
    remote: bool | None = None
    description: str = Field(description="Full job description text: responsibilities, requirements, benefits")
    external_apply_url: str | None = Field(default=None, description="Company/ATS apply link if shown, else null")


def find_url(text: str) -> str | None:
    m = URL_RE.search(text)
    return m.group(0).rstrip(").,") if m else None


def _greenhouse(client: httpx.Client, url: str) -> JobPosting | None:
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url) or re.search(
        r"for=([\w-]+).*?token=(\d+)", url
    )
    if not m:
        return None
    board, jid = m.groups()
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}")
    r.raise_for_status()
    j = r.json()
    loc = (j.get("location") or {}).get("name", "")
    return JobPosting(
        "greenhouse",
        f"{board}:{jid}",
        j.get("company_name") or board,
        j["title"],
        loc,
        looks_remote(loc),
        j.get("absolute_url", url),
        j.get("absolute_url", url),
        html_to_text(j.get("content", "")),
        parse_dt(j.get("first_published") or j.get("updated_at")),
    )


def _lever(client: httpx.Client, url: str) -> JobPosting | None:
    m = re.search(r"jobs\.lever\.co/([\w-]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    company, pid = m.groups()
    r = client.get(f"https://api.lever.co/v0/postings/{company}/{pid}")
    r.raise_for_status()
    j = r.json()
    cats = j.get("categories") or {}
    parts = (
        [j.get("descriptionPlain") or ""]
        + [f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in j.get("lists") or []]
        + [j.get("additionalPlain") or ""]
    )
    return JobPosting(
        "lever",
        f"{company}:{pid}",
        company,
        j["text"],
        cats.get("location", ""),
        True if (j.get("workplaceType") or "") == "remote" else looks_remote(cats.get("location", "")),
        j.get("hostedUrl", url),
        j.get("applyUrl") or url,
        "\n\n".join(p for p in parts if p),
        parse_dt(j.get("createdAt")),
    )


def _ashby(client: httpx.Client, url: str) -> JobPosting | None:
    m = re.search(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    board, jid = m.groups()
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        if j.get("id") == jid:
            loc = j.get("location") or ""
            return JobPosting(
                "ashby",
                f"{board}:{jid}",
                board,
                j["title"],
                loc,
                True if j.get("isRemote") else looks_remote(loc),
                j.get("jobUrl", url),
                j.get("applyUrl") or url,
                j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                parse_dt(j.get("publishedAt")),
            )
    return None


def _from_browser(url: str) -> JobPosting:
    cfg = settings()
    with br.attached(br.start_configured()) as browser:
        page = br.open_page(browser, url)
        page.wait_for_timeout(2500)
        title, text, final_url = page.title(), page_text(page), page.url
        page.close()
    if len(text) < 200:
        raise IntakeError("Page had almost no text (login wall?). Log in once in the Tengen Chrome window.")
    res = ask(
        "Extract the job posting from the page text on stdin.",
        f"<url>{final_url}</url>\n<title>{title}</title>\n<page_text>\n{text[:20000]}\n</page_text>",
        ExtractedJob,
        system_prompt="extract_job.md",
        model=cfg.llm.analyze_model,
        timeout=cfg.llm.timeout_seconds,
    )
    ex = res.value
    if not ex.is_job_posting:
        raise IntakeError("That page doesn't look like a job posting.")
    apply_url = ex.external_apply_url or final_url
    source = detect_ats(final_url)
    return JobPosting(
        source if source != "unknown" else "web",
        hashlib.sha1(final_url.encode()).hexdigest()[:16],
        ex.company,
        ex.title,
        ex.location,
        ex.remote,
        final_url,
        apply_url,
        ex.description,
    )


def fetch_posting(url: str) -> JobPosting:
    host = urlparse(url).netloc.lower()
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as client:
        for match, fn in (("greenhouse.io", _greenhouse), ("lever.co", _lever), ("ashbyhq.com", _ashby)):
            if match in host or (match == "greenhouse.io" and "gh_jid=" in url):
                try:
                    posting = fn(client, url)
                except httpx.HTTPError:
                    posting = None
                if posting:
                    return posting
    return _from_browser(url)


def add_job_from_url(s: Session, url: str) -> Job:
    p = fetch_posting(url)
    existing = s.exec(
        select(Job).where(
            ((Job.source == p.source) & (Job.external_id == p.external_id)) | (Job.fingerprint == p.fingerprint)
        )
    ).first()
    if existing is not None:
        return existing
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
        status=JobStatus.DISCOVERED,
        status_reason="added by you",
    )
    s.add(job)
    s.commit()
    return job
