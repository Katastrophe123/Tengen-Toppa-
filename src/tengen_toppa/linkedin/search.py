"""LinkedIn job search: URL building and read-only card parsing (no clicks)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from playwright.sync_api import Page

from tengen_toppa.config import LinkedInSettings

log = logging.getLogger(__name__)

BASE = "https://www.linkedin.com/jobs/search/"
EXPERIENCE_CODES = {"internship": "1", "entry": "2", "associate": "3", "mid_senior": "4"}
WORKPLACE_CODES = {"onsite": "1", "remote": "2", "hybrid": "3"}
PAGE_SIZE = 25


class LinkedInBlocked(RuntimeError):
    """Login wall, security checkpoint, CAPTCHA or rate-limit page. Stop everything."""


@dataclass(frozen=True)
class Card:
    job_id: str
    title: str
    company: str
    location: str
    workplace: str
    easy_apply: bool
    applied: bool
    footer: tuple[str, ...]


def search_url(
    cfg: LinkedInSettings, keyword: str, location: str, *, posted_within_seconds: int, start: int = 0
) -> str:
    params = {
        "keywords": keyword,
        "location": location,
        "f_AL": "true",
        "f_TPR": f"r{posted_within_seconds}",
        "sortBy": "DD",
    }
    if cfg.experience_levels:
        params["f_E"] = ",".join(EXPERIENCE_CODES[e] for e in cfg.experience_levels)
    if cfg.workplace:
        params["f_WT"] = ",".join(WORKPLACE_CODES[w] for w in cfg.workplace)
    if start:
        params["start"] = str(start)
    return f"{BASE}?{urlencode(params)}"


_RENDER_JS = r"""
async () => {
  const items = Array.from(document.querySelectorAll('li[data-occludable-job-id]'));
  for (const li of items) {
    if (!li.querySelector('.job-card-container')) { li.scrollIntoView({block: 'center'}); await new Promise(r => setTimeout(r, 120)); }
  }
  return items.filter(li => li.querySelector('.job-card-container')).length + '/' + items.length;
}
"""

_PARSE_JS = r"""
() => Array.from(document.querySelectorAll('li[data-occludable-job-id]')).map(li => {
  const t = s => { const e = li.querySelector(s); return e ? (e.innerText || '').trim() : ''; };
  const footer = Array.from(li.querySelectorAll('.job-card-container__footer-item, li.job-card-container__footer-item'))
    .map(x => (x.innerText || '').trim()).filter(Boolean);
  return {
    id: li.getAttribute('data-occludable-job-id'),
    rendered: !!li.querySelector('.job-card-container'),
    title: t('.job-card-list__title--link strong') || t('.job-card-list__title--link') || t('a[href*="/jobs/view/"]'),
    company: t('.artdeco-entity-lockup__subtitle'),
    caption: t('.artdeco-entity-lockup__caption'),
    footer, text: (li.innerText || '').replace(/\s+/g, ' ').slice(0, 400),
  };
})
"""

_BLOCK_JS = r"""
() => {
  const u = location.href, t = (document.body && document.body.innerText || '').slice(0, 4000);
  if (/\/checkpoint\/|\/authwall|\/login|\/uas\/login/.test(u)) return 'login or checkpoint page: ' + u.split('?')[0];
  if (/Let's do a quick security check|security verification|unusual activity|You're temporarily restricted|confirm you're not a robot/i.test(t))
    return 'LinkedIn security check / restriction';
  if (/You've reached the Easy Apply application limit|application limit for today/i.test(t)) return 'Easy Apply daily limit reached';
  if (/too many requests/i.test(t)) return 'LinkedIn rate limit';
  return '';
}
"""

_NO_RESULTS_RE = re.compile(r"No matching jobs found|We couldn.t find a match", re.I)
_WORKPLACE_RE = re.compile(r"\((On-site|Remote|Hybrid)\)", re.I)


def check_blocked(page: Page) -> None:
    reason = page.evaluate(_BLOCK_JS)
    if reason:
        raise LinkedInBlocked(reason)


def parse_cards(raw: list[dict]) -> list[Card]:
    cards = []
    for r in raw:
        if not r.get("id") or not r.get("rendered"):
            continue
        title = (r.get("title") or "").split("\n")[0].strip()
        title = re.sub(r"\s+with verification$", "", title)
        caption = (r.get("caption") or "").strip()
        wp = _WORKPLACE_RE.search(caption)
        footer = tuple(f.split("\n")[0].strip() for f in r.get("footer") or [])
        text = r.get("text") or ""
        cards.append(
            Card(
                job_id=str(r["id"]),
                title=title,
                company=(r.get("company") or "").split("\n")[0].strip(),
                location=_WORKPLACE_RE.sub("", caption).strip(),
                workplace=wp.group(1).lower() if wp else "",
                easy_apply=any(f.lower() == "easy apply" for f in footer) or "Easy Apply" in text,
                applied=any(f.lower().startswith("applied") for f in footer),
                footer=footer,
            )
        )
    return cards


def read_search_page(page: Page, url: str, *, settle_ms: int = 3500) -> tuple[list[Card], bool]:
    """Returns (cards, no_more_results). Raises LinkedInBlocked on walls/checkpoints/limits."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(settle_ms)
    check_blocked(page)
    body = page.inner_text("body")[:6000]
    if _NO_RESULTS_RE.search(body):
        return [], True
    for _ in range(3):
        done = page.evaluate(_RENDER_JS)
        rendered, total = (int(x) for x in done.split("/"))
        if rendered >= total:
            break
    cards = parse_cards(page.evaluate(_PARSE_JS))
    return cards, len(cards) < PAGE_SIZE
