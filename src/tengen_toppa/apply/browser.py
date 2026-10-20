"""A dedicated, persistent Chrome profile (data/browser-profile) driven over CDP.

Chrome runs as its own process, so filled forms stay open on your Mac after a task finishes.
Python Playwright and the Claude Playwright-MCP agent attach to the same browser.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, sync_playwright

log = logging.getLogger(__name__)

CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class BrowserError(RuntimeError):
    pass


def cdp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def is_up(port: int) -> bool:
    try:
        return httpx.get(f"{cdp_url(port)}/json/version", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def executable(kind: str) -> str:
    if kind == "chrome" and Path(CHROME_MAC).exists():
        return CHROME_MAC
    with sync_playwright() as p:
        path = p.chromium.executable_path
    if not Path(path).exists():
        raise BrowserError("No browser found. Install Google Chrome or run: uv run playwright install chromium")
    return path


def ensure_browser(
    port: int, profile_dir: Path, kind: str = "chrome", log_file: Path | None = None, headless: bool = False
) -> subprocess.Popen | None:
    """Start the dedicated Chrome if it isn't running. Returns the process if we started it."""
    if is_up(port):
        return None
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        executable(kind),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        *(["--headless=new"] if headless else []),
        "about:blank",
    ]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    out = log_file.open("ab") if log_file else subprocess.DEVNULL
    proc = subprocess.Popen(args, stdout=out, stderr=out, start_new_session=True)
    for _ in range(60):
        if is_up(port):
            return proc
        time.sleep(0.5)
    proc.terminate()
    raise BrowserError(f"Chrome did not open CDP port {port}")


def start_configured() -> int:
    """Start the dedicated Chrome per [apply] settings. Returns the CDP port."""
    from tengen_toppa.config import settings
    from tengen_toppa.paths import get_paths

    cfg = settings().apply
    paths = get_paths()
    ensure_browser(cfg.cdp_port, paths.browser_profile, cfg.browser, paths.logs / "chrome.log", cfg.headless)
    return cfg.cdp_port


@contextmanager
def attached(port: int) -> Iterator[Browser]:
    """Attach to the running Chrome. Leaving the block only disconnects; Chrome and its tabs stay open."""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url(port))
        try:
            yield browser
        finally:
            browser.close()  # for CDP connections this disconnects without closing Chrome


def find_page(browser: Browser, url_prefix: str) -> Page | None:
    """Most recently opened tab whose URL starts with url_prefix."""
    matches = [p for ctx in browser.contexts for p in ctx.pages if p.url.startswith(url_prefix)]
    return matches[-1] if matches else None


def close_pages(browser: Browser, url_prefix: str) -> int:
    pages = [p for ctx in browser.contexts for p in ctx.pages if p.url.startswith(url_prefix)]
    for p in pages:
        p.close()
    return len(pages)


def open_page(browser: Browser, url: str) -> Page:
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        log.debug("networkidle not reached for %s", url)
    page.bring_to_front()
    return page
