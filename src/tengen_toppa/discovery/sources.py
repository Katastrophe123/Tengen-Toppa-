"""Public ATS job-board APIs (no login, no scraping): Greenhouse, Lever, Ashby."""

from __future__ import annotations

import logging

import httpx

from tengen_toppa.discovery.base import JobPosting, html_to_text, looks_remote, parse_dt

log = logging.getLogger(__name__)

UA = {"User-Agent": "Tengen-Toppa/0.1 (personal job search)"}


def fetch_greenhouse(client: httpx.Client, board: str) -> list[JobPosting]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        location = (j.get("location") or {}).get("name", "")
        desc = html_to_text(j.get("content", ""))
        out.append(
            JobPosting(
                source="greenhouse",
                external_id=f"{board}:{j['id']}",
                company=j.get("company_name") or board,
                title=j.get("title", "").strip(),
                location=location,
                remote=looks_remote(location),
                url=j.get("absolute_url", ""),
                apply_url=j.get("absolute_url", ""),
                description=desc,
                posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
            )
        )
    return out


def fetch_lever(client: httpx.Client, company: str) -> list[JobPosting]:
    r = client.get(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        sections = [j.get("descriptionPlain") or html_to_text(j.get("description", ""))]
        for lst in j.get("lists") or []:
            sections.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
        sections.append(j.get("additionalPlain") or "")
        workplace = (j.get("workplaceType") or "").lower()
        out.append(
            JobPosting(
                source="lever",
                external_id=f"{company}:{j['id']}",
                company=company,
                title=j.get("text", "").strip(),
                location=location,
                remote=True if workplace == "remote" else looks_remote(location),
                url=j.get("hostedUrl", ""),
                apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
                description="\n\n".join(s for s in sections if s).strip(),
                posted_at=parse_dt(j.get("createdAt")),
            )
        )
    return out


def fetch_ashby(client: httpx.Client, board: str) -> list[JobPosting]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        location = j.get("location") or ""
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
        out.append(
            JobPosting(
                source="ashby",
                external_id=f"{board}:{j['id']}",
                company=board,
                title=j.get("title", "").strip(),
                location=location,
                remote=True if j.get("isRemote") else looks_remote(location, j.get("workplaceType") or ""),
                url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
                description=desc,
                posted_at=parse_dt(j.get("publishedAt")),
            )
        )
    return out


def fetch_all(greenhouse: list[str], lever: list[str], ashby: list[str]) -> tuple[list[JobPosting], list[str]]:
    """Returns (postings, errors). One failing board never stops the others."""
    postings: list[JobPosting] = []
    errors: list[str] = []
    plan = (
        [(fetch_greenhouse, b) for b in greenhouse]
        + [(fetch_lever, c) for c in lever]
        + [(fetch_ashby, b) for b in ashby]
    )
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as client:
        for fn, slug in plan:
            try:
                got = fn(client, slug)
                log.info("%s/%s: %d jobs", fn.__name__, slug, len(got))
                postings.extend(got)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                errors.append(f"{fn.__name__.removeprefix('fetch_')}/{slug}: {e}")
                log.warning("discovery failed for %s/%s: %s", fn.__name__, slug, e)
    return postings, errors
