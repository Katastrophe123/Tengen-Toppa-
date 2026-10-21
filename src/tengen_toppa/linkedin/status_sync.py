"""Read LinkedIn's job tracker (My Jobs -> Applied) and store each application's status. Read-only."""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Page
from sqlmodel import Session, select

from tengen_toppa.linkedin.search import check_blocked
from tengen_toppa.store.models import LiApplication, utcnow

TRACKER = "https://www.linkedin.com/my-items/saved-jobs/"
_STATUS_RE = re.compile(
    r"^(Applied .*|Application viewed.*|Viewed.*|Resume downloaded.*|Not selected.*|No longer accepting.*|"
    r"\(No longer accepting applications\)|Interview.*|Rejected.*|Application withdrawn.*)$",
    re.I,
)

_ITEMS_JS = r"""
() => Array.from(document.querySelectorAll('main a[href*="/jobs/view/"]')).map(a => {
  const m = a.getAttribute('href').match(/\/jobs\/view\/(\d+)/);
  let c = a; for (let i = 0; i < 6 && c.parentElement; i++) { c = c.parentElement; if ((c.innerText || '').includes('Applied')) break; }
  return { id: m ? m[1] : null, text: (c.innerText || '').slice(0, 600) };
}).filter(x => x.id)
"""


@dataclass(frozen=True)
class TrackerItem:
    job_id: str
    status: str


def parse_items(raw: list[dict]) -> list[TrackerItem]:
    out: dict[str, TrackerItem] = {}
    for r in raw:
        lines = [ln.strip() for ln in r["text"].splitlines() if ln.strip()]
        status = " · ".join(ln.strip("()") for ln in lines if _STATUS_RE.match(ln))
        if status:
            out[r["id"]] = TrackerItem(r["id"], status)
    return list(out.values())


def read_tracker(page: Page, pages: int = 3) -> list[TrackerItem]:
    page.goto(TRACKER, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    check_blocked(page)
    tab = page.get_by_text(re.compile(r"^\s*Applied\s*·?\s*\d*\s*$"))
    if tab.count():
        tab.first.click()  # a filter tab on your own tracker; it cannot apply to anything
        page.wait_for_timeout(3500)
    items: list[TrackerItem] = []
    for _ in range(pages):
        items += parse_items(page.evaluate(_ITEMS_JS))
        nxt = page.get_by_role("button", name=re.compile(r"^Next$", re.I))
        if not nxt.count() or not nxt.first.is_enabled():
            break
        nxt.first.click()
        page.wait_for_timeout(3000)
    return items


def store(s: Session, items: list[TrackerItem]) -> int:
    changed = 0
    for it in items:
        app = s.exec(
            select(LiApplication)
            .where(LiApplication.job_id == it.job_id, LiApplication.outcome == "applied")
            .order_by(LiApplication.id.desc())
        ).first()  # type: ignore[union-attr]
        if app and app.li_status != it.status:
            app.li_status, app.li_status_at = it.status, utcnow()
            s.add(app)
            changed += 1
    s.commit()
    return changed
