"""Real browser implementation of controller.Ops, on the signed-in Tengen Chrome."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlmodel import Session

from tengen_toppa.apply import browser as br
from tengen_toppa.config import Settings, settings
from tengen_toppa.linkedin.easy_apply import ApplyResult, Clicker, EngineConfig, run_easy_apply, submit_ready
from tengen_toppa.linkedin.harvest import HarvestStats, harvest
from tengen_toppa.linkedin.jobpage import JobPageInfo, job_url, read_job_page
from tengen_toppa.linkedin.search import LinkedInBlocked, read_search_page, search_url
from tengen_toppa.resume_file import current
from tengen_toppa.store.db import session

log = logging.getLogger(__name__)


class LiveOps:
    def _tab(self, browser, url: str = "about:blank"):
        return br.open_page(browser, url)

    def harvest(self, s: Session, cfg: Settings) -> HarvestStats:
        with br.browser_lock(owner="li-harvest"):
            port = br.start_configured()
            with br.attached(port) as b:
                page = self._tab(b)
                try:
                    return harvest(s, page, cfg)
                finally:
                    page.close()

    def check_job(self, job_id: str) -> JobPageInfo:
        with br.browser_lock(owner="li-check"):
            port = br.start_configured()
            with br.attached(port) as b:
                page = self._tab(b)
                try:
                    return read_job_page(page, job_id)
                finally:
                    page.close()

    def apply(
        self,
        job_id: str,
        *,
        allow_open: bool,
        allow_submit: bool,
        stop_before_submit: bool,
        resolve_fields,
        screenshot_dir: Path,
    ) -> ApplyResult:
        cfg = settings().linkedin
        with session() as s:
            resume = current(s)
        eng = EngineConfig(
            resume=resume.path,
            display_name=resume.display_name,
            follow_company=cfg.follow_company,
            max_steps=cfg.max_steps,
        )
        with br.browser_lock(owner="li-apply"):
            port = br.start_configured()
            with br.attached(port) as b:
                br.close_pages(b, job_url(job_id))
                page = self._tab(b, job_url(job_id))
                page.wait_for_timeout(3000)
                res = run_easy_apply(
                    page,
                    job_id,
                    eng,
                    resolve_fields,
                    Clicker(allow_open=allow_open, allow_submit=allow_submit),
                    stop_before_submit=stop_before_submit,
                    screenshot_dir=screenshot_dir,
                )
                if res.outcome != "ready":
                    page.close()  # 'ready' keeps the filled tab open for your Submit
                return res

    def submit_ready(self, job_id: str, screenshot_dir: Path) -> ApplyResult:
        with br.browser_lock(owner="li-submit"):
            port = br.start_configured()
            with br.attached(port) as b:
                page = br.find_page(b, job_url(job_id))
                if page is None:
                    return ApplyResult("failed", "the filled tab was closed; tap Apply again")
                res = submit_ready(page, job_id, Clicker(allow_submit=True), screenshot_dir)
                if res.outcome == "applied":
                    page.close()
                return res

    def sync_status(self, s: Session) -> int:
        from tengen_toppa.linkedin.status_sync import read_tracker, store

        with br.browser_lock(owner="li-status"):
            port = br.start_configured()
            with br.attached(port) as b:
                page = self._tab(b)
                try:
                    return store(s, read_tracker(page))
                finally:
                    page.close()

    def canary(self) -> list[str]:
        """Read-only: signed in, English UI, search page still parses."""
        cfg = settings().linkedin
        problems: list[str] = []
        with br.browser_lock(owner="li-canary"):
            try:
                port = br.start_configured()
            except br.BrowserError as e:
                return [str(e)]
            with br.attached(port) as b:
                ctx = b.contexts[0] if b.contexts else b.new_context()
                if "li_at" not in {c["name"] for c in ctx.cookies("https://www.linkedin.com")}:
                    return ["LinkedIn is not signed in in Tengen Chrome (run `tengen browser` and sign in)"]
                page = self._tab(b)
                try:
                    url = search_url(cfg, cfg.keywords[0], cfg.locations[-1], posted_within_seconds=7 * 86400)
                    cards, _ = read_search_page(page, url)
                    lang = page.evaluate("document.documentElement.lang || ''")
                    if not lang.lower().startswith("en"):
                        problems.append(f"LinkedIn UI language is '{lang}', switch it to English")
                    if len(cards) < 5:
                        problems.append(f"search page parsed only {len(cards)} jobs: LinkedIn layout may have changed")
                except LinkedInBlocked as e:
                    problems.append(f"LinkedIn blocked: {e}")
                finally:
                    page.close()
        return problems
