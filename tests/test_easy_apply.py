"""Easy Apply engine against a local copy of LinkedIn's flow, in real headless Chrome."""

from __future__ import annotations

from pathlib import Path

import pytest

from tengen_toppa.config import ApplicantSettings
from tengen_toppa.linkedin.easy_apply import Clicker, EngineConfig, SubmitNotAllowed, run_easy_apply, submit_ready
from tengen_toppa.qa import resolve, save_answer

pytest.importorskip("playwright.sync_api")

FIXTURE = (Path(__file__).parent / "fixtures" / "linkedin_easy_apply.html").resolve().as_uri()


@pytest.fixture(scope="module")
def chrome():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(chrome):
    pg = chrome.new_page()
    yield pg
    pg.close()


@pytest.fixture
def resume(tmp_path) -> Path:
    p = tmp_path / "Kanishk_Singh_Parihar_Resume.pdf"
    p.write_bytes(b"%PDF-1.4\n%fixture\n")
    return p


def engine_cfg(resume: Path) -> EngineConfig:
    return EngineConfig(resume=resume, display_name=resume.name, step_wait_ms=150, leave_url="about:blank")


def resolver(s, master):
    def fn(fields):
        r = resolve(s, fields, profile=master, applicant=ApplicantSettings(), use_llm=False)
        return r.answers, r.missing_required

    return fn


def test_guard_refuses_to_open_or_submit_by_default(page, s, master, resume, tmp_path):
    page.goto(FIXTURE)
    with pytest.raises(SubmitNotAllowed):
        run_easy_apply(
            page,
            "1",
            engine_cfg(resume),
            resolver(s, master),
            Clicker(),
            stop_before_submit=True,
            screenshot_dir=tmp_path,
        )
    assert page.evaluate("window.__clicks") == []
    assert page.evaluate("window.__submitted") is False


def test_review_mode_fills_every_step_and_stops_before_submit(page, s, master, resume, tmp_path):
    save_answer(s, "Are you comfortable working from our Gurugram office?", "Yes")
    page.goto(FIXTURE)
    clicker = Clicker(allow_open=True, allow_submit=False)
    res = run_easy_apply(
        page, "1", engine_cfg(resume), resolver(s, master), clicker, stop_before_submit=True, screenshot_dir=tmp_path
    )
    assert res.outcome == "ready", res.detail
    assert page.evaluate("window.__submitted") is False
    assert "submit" not in page.evaluate("window.__clicks")
    assert res.answers["How many years of experience do you have with Python?"] == "1"
    assert res.answers["Do you have 5+ years of experience in machine learning?"] == "No"
    assert res.screenshot and res.screenshot.exists()
    assert not page.is_checked("#follow")  # follow_company = false

    # your "Submit now" on WhatsApp
    with pytest.raises(SubmitNotAllowed):
        submit_ready(page, "1", Clicker(), tmp_path)
    done = submit_ready(page, "1", Clicker(allow_submit=True), tmp_path)
    assert done.outcome == "applied"
    assert page.evaluate("window.__submitted") is True
    assert page.evaluate("window.__follow") is False
    assert page.evaluate("window.__py") == "1"


def test_auto_mode_uploads_this_exact_resume_and_submits(page, s, master, resume, tmp_path):
    save_answer(s, "Are you comfortable working from our Gurugram office?", "Yes")
    page.goto(FIXTURE)
    res = run_easy_apply(
        page,
        "1",
        engine_cfg(resume),
        resolver(s, master),
        Clicker(allow_open=True, allow_submit=True),
        stop_before_submit=False,
        screenshot_dir=tmp_path,
    )
    assert res.outcome == "applied", res.detail
    assert page.evaluate("window.__clicks") == ["open", "next0", "next1", "next2", "submit"]


def test_unknown_question_aborts_without_clicking_anything_risky(page, s, master, resume, tmp_path):
    save_answer(s, "Are you comfortable working from our Gurugram office?", "Yes")
    page.goto(FIXTURE + "?unknown=1")
    res = run_easy_apply(
        page,
        "1",
        engine_cfg(resume),
        resolver(s, master),
        Clicker(allow_open=True, allow_submit=True),
        stop_before_submit=False,
        screenshot_dir=tmp_path,
    )
    assert res.outcome == "needs_answers"
    assert [f.label for f in res.missing] == ["How many years of experience do you have with Kubernetes?"]
    assert page.url == "about:blank"  # left by navigating away, no Dismiss/Discard clicks


def test_one_click_easy_apply_is_detected_as_applied(page, s, master, resume, tmp_path):
    page.goto(FIXTURE + "?oneclick=1")
    res = run_easy_apply(
        page,
        "1",
        engine_cfg(resume),
        resolver(s, master),
        Clicker(allow_open=True, allow_submit=False),
        stop_before_submit=True,
        screenshot_dir=tmp_path,
    )
    assert res.outcome == "applied"  # the controller counted it against the cap before allowing the open


def test_clicker_never_clicks_submit_as_next(page):
    page.set_content('<button aria-label="Submit application">Submit application</button>')
    with pytest.raises(SubmitNotAllowed):
        Clicker(allow_open=True).click(page.locator("button"), purpose="next")
