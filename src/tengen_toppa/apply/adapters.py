"""Per-ATS knowledge: how to reach the form, which button submits, what success looks like."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.sync_api import Page

from tengen_toppa.apply.form import click_first, page_text

SUCCESS_RE = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?(submitted|received)"
    r"|we('ve| have) received your application|successfully submitted",
    re.I,
)


@dataclass(frozen=True)
class Adapter:
    name: str
    prepare: Callable[[Page], None]
    submit_selectors: tuple[str, ...]

    def submit(self, page: Page) -> bool:
        return click_first(page, self.submit_selectors, timeout=8000)

    def confirmed(self, page: Page, before_url: str) -> bool:
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(1500)
        return bool(SUCCESS_RE.search(page_text(page))) or (
            page.url != before_url and re.search(r"confirm|thank|success", page.url, re.I) is not None
        )


def detect_ats(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "greenhouse.io" in host or "gh_jid=" in url:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host or "workday" in host:
        return "workday"
    if "linkedin.com" in host:
        return "linkedin"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "naukri.com" in host:
        return "naukri"
    return "unknown"


def _wait_form(page: Page) -> None:
    with contextlib.suppress(Exception):
        page.wait_for_selector("form input, form textarea, input[type=file]", timeout=15000, state="attached")


def _greenhouse_prepare(page: Page) -> None:
    if not page.locator("form input:visible").count():
        click_first(page, ('a:has-text("Apply")', 'button:has-text("Apply")', "#apply_button"))
    _wait_form(page)


def _lever_prepare(page: Page) -> None:
    path = urlparse(page.url).path.rstrip("/")
    if not path.endswith("/apply"):
        page.goto(page.url.split("?")[0].rstrip("/") + "/apply", wait_until="domcontentloaded")
    _wait_form(page)


def _ashby_prepare(page: Page) -> None:
    if "/application" not in urlparse(page.url).path:
        page.goto(page.url.split("?")[0].rstrip("/") + "/application", wait_until="domcontentloaded")
    _wait_form(page)


def _generic_prepare(page: Page) -> None:
    if not page.locator("form input:visible, form textarea:visible").count():
        click_first(page, ('button:has-text("Apply")', 'a:has-text("Apply now")', 'a:has-text("Apply")'))
    _wait_form(page)


ADAPTERS: dict[str, Adapter] = {
    "greenhouse": Adapter(
        "greenhouse",
        _greenhouse_prepare,
        ('button:has-text("Submit application")', "#submit_app", 'button[type="submit"]', 'input[type="submit"]'),
    ),
    "lever": Adapter(
        "lever",
        _lever_prepare,
        ("#btn-submit", 'button:has-text("Submit application")', 'button[type="submit"]'),
    ),
    "ashby": Adapter(
        "ashby",
        _ashby_prepare,
        ('button:has-text("Submit Application")', 'button:has-text("Submit application")', 'button[type="submit"]'),
    ),
}

GENERIC = Adapter(
    "generic", _generic_prepare, ('button:has-text("Submit")', 'button[type="submit"]', 'input[type="submit"]')
)

# Never automate these: login-walled / ToS-restricted. Assist mode only (open page + resume, you click).
ASSIST_ONLY = {"linkedin", "naukri", "workday"}
