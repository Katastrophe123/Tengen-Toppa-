"""Fill and top up the standing pool. First fill: past week until the pool reaches pool_target.
Top-up runs: past 24h only, stopping a search as soon as a page brings nothing new."""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.sync_api import Page
from sqlmodel import Session

from tengen_toppa.config import Settings
from tengen_toppa.linkedin.pool import expire_stale, ingest_cards, pool_size
from tengen_toppa.linkedin.search import PAGE_SIZE, read_search_page, search_url

log = logging.getLogger(__name__)
DAY = 86400


@dataclass
class HarvestStats:
    pages: int = 0
    seen: int = 0
    new: int = 0
    eligible_new: int = 0
    searches: int = 0
    first_fill: bool = False
    pool: int = 0
    errors: list[str] = field(default_factory=list)


def harvest(
    s: Session,
    page: Page,
    cfg: Settings,
    *,
    page_budget: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_page: Callable[[HarvestStats], None] | None = None,
) -> HarvestStats:
    li = cfg.linkedin
    stats = HarvestStats(first_fill=pool_size(s) < li.pool_target)
    window = li.first_fill_days * DAY if stats.first_fill else DAY
    budget = page_budget if page_budget is not None else li.max_pages_per_run
    expire_stale(s)
    # Round-robin: page 1 of every search, then page 2 of every search... so the budget covers every
    # keyword/location instead of being spent on the first two searches' deep (and repetitive) pages.
    active = list(itertools.product(li.keywords, li.locations))
    stats.searches = len(active)
    for n in range(li.max_pages_per_search):
        still: list[tuple[str, str]] = []
        for kw, loc in active:
            if stats.pages >= budget or (stats.first_fill and pool_size(s) >= li.pool_target):
                stats.pool = pool_size(s)
                return stats
            url = search_url(li, kw, loc, posted_within_seconds=window, start=n * PAGE_SIZE)
            cards, last = read_search_page(page, url)  # raises LinkedInBlocked
            stats.pages += 1
            res = ingest_cards(s, cards, cfg, search=f"{kw} @ {loc}")
            stats.seen += res.seen
            stats.new += res.new
            stats.eligible_new += res.eligible_new
            if on_page:
                stats.pool = pool_size(s)
                on_page(stats)
            # keep paging a search only while it still brings new jobs
            if not last and res.new > 0:
                still.append((kw, loc))
            sleep(li.page_delay_seconds)
        active = still
        if not active:
            break
    stats.pool = pool_size(s)
    return stats
