"""Real headless-Chrome tests of form discovery, answering, filling and submitting on ATS-shaped pages."""

from __future__ import annotations

from pathlib import Path

import pytest

from tengen_toppa.apply.adapters import GENERIC, detect_ats
from tengen_toppa.apply.form import captcha_visible, collect_fields, fill_form
from tengen_toppa.config import ApplicantSettings
from tengen_toppa.qa import resolve, save_answer

playwright = pytest.importorskip("playwright.sync_api")

GREENHOUSE_LIKE = """
<html><body><form id="application_form" onsubmit="event.preventDefault(); document.body.innerHTML='<h1>Thank you for applying!</h1>'">
  <div class="field"><label for="first_name">First Name *</label><input id="first_name" name="job_application[first_name]" required></div>
  <div class="field"><label for="last_name">Last Name *</label><input id="last_name" required></div>
  <div class="field"><label for="email">Email *</label><input id="email" type="email" required></div>
  <div class="field"><label for="phone">Phone</label><input id="phone" type="tel"></div>
  <div class="field"><label for="resume">Resume/CV *</label><input id="resume" type="file" required></div>
  <div class="field"><label for="cover">Cover Letter</label><input id="cover" type="file"></div>
  <div class="field"><label for="li">LinkedIn Profile</label><input id="li"></div>
  <div class="field"><label for="auth">Are you legally authorized to work in India? *</label>
    <select id="auth" required><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
  <fieldset><legend>Will you now or in the future require visa sponsorship? *</legend>
    <label><input type="radio" name="spons" value="1" required> Yes</label>
    <label><input type="radio" name="spons" value="0"> No</label></fieldset>
  <div class="field"><label for="gender">Gender</label>
    <select id="gender"><option>Select...</option><option>Male</option><option>Female</option><option>Decline to self-identify</option></select></div>
  <div class="field"><label for="clearance">Do you hold an active security clearance? *</label><input id="clearance" required></div>
  <div class="field"><label><input type="checkbox" id="consent" required> I agree to the privacy policy</label></div>
  <input type="hidden" name="token" value="x">
  <button type="submit">Submit application</button>
</form></body></html>
"""

LEVER_LIKE = """
<html><body><form>
  <ul><li class="application-question"><div class="application-label">Full name ✱</div>
    <div class="application-field"><input name="name" required></div></li>
  <li class="application-question"><div class="application-label">Email ✱</div>
    <div class="application-field"><input name="email" type="email" required></div></li>
  <li class="application-question"><div class="application-label">Current company</div>
    <div class="application-field"><input name="org"></div></li>
  <li class="application-question"><div class="application-label">GitHub URL</div>
    <div class="application-field"><input name="urls[GitHub]"></div></li>
  <li class="application-question"><div class="application-label">Notice period</div>
    <div class="application-field"><textarea name="cards[q1]"></textarea></div></li></ul>
  <span id="lbl-x">What is your expected CTC?</span><input aria-labelledby="lbl-x" name="ctc">
</form></body></html>
"""


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
def resume_pdf(tmp_path) -> Path:
    p = tmp_path / "Your_Name_Resume.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


def by_label(fields, text):
    return next(f for f in fields if text.lower() in f.label.lower())


def test_collect_fields_greenhouse_like(page):
    page.set_content(GREENHOUSE_LIKE)
    fields = collect_fields(page)
    labels = [f.label for f in fields]
    assert "token" not in " ".join(labels)  # hidden input ignored
    first = by_label(fields, "First Name")
    assert first.required and first.kind == "text" and first.label == "First Name"
    assert by_label(fields, "Resume").kind == "file"
    auth = by_label(fields, "legally authorized")
    assert auth.kind == "select" and "Yes" in auth.options
    spons = by_label(fields, "sponsorship")
    assert spons.kind == "radio" and spons.options == ["Yes", "No"] and spons.required
    consent = by_label(fields, "privacy policy")
    assert consent.kind == "checkbox"


def test_collect_fields_lever_like_and_aria(page):
    page.set_content(LEVER_LIKE)
    fields = collect_fields(page)
    assert by_label(fields, "Full name").kind == "text"
    assert by_label(fields, "Notice period").kind == "textarea"
    assert by_label(fields, "expected CTC").name == "ctc"


def test_resolve_fill_and_submit(page, s, master, resume_pdf):
    page.set_content(GREENHOUSE_LIKE)
    fields = collect_fields(page)
    res = resolve(s, fields, profile=master, applicant=ApplicantSettings(), use_llm=False)

    # unknown required question is NOT guessed
    assert [f.label for f in res.missing_required] == ["Do you hold an active security clearance?"]
    report = fill_form(page, fields, res.answers, resume=resume_pdf)
    assert page.input_value("#first_name") == "Your" and page.input_value("#last_name") == "Name"
    assert page.input_value("#email") == "you@example.com"
    assert page.input_value("#li") == "https://linkedin.com/in/you"
    assert page.eval_on_selector("#auth", "e => e.options[e.selectedIndex].text") == "Yes"
    assert page.is_checked("input[name=spons][value='0']")  # requires_sponsorship = false -> "No"
    assert page.eval_on_selector("#gender", "e => e.options[e.selectedIndex].text") == "Decline to self-identify"
    assert page.is_checked("#consent")
    assert page.eval_on_selector("#resume", "e => e.files[0].name") == resume_pdf.name
    assert page.eval_on_selector("#cover", "e => e.files.length") == 0  # no cover letter supplied
    assert not report.errors

    # once you answer on WhatsApp, the bank fills it next time
    save_answer(s, "Do you hold an active security clearance?", "No")
    res2 = resolve(s, collect_fields(page), profile=master, applicant=ApplicantSettings(), use_llm=False)
    assert not res2.missing_required
    fill_form(page, collect_fields(page), res2.answers, resume=resume_pdf)
    assert page.input_value("#clearance") == "No"

    before = page.url
    assert GENERIC.submit(page)
    assert GENERIC.confirmed(page, before)


def test_captcha_detection(page):
    page.set_content('<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x" width="300" height="80"></iframe>')
    assert captcha_visible(page)
    page.set_content(
        '<div class="grecaptcha-badge"><iframe src="https://www.google.com/recaptcha/api2/anchor?size=invisible" width="256" height="60"></iframe></div>'
    )
    assert not captcha_visible(page)


@pytest.mark.parametrize(
    "url, ats",
    [
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://acme.com/careers?gh_jid=123", "greenhouse"),
        ("https://jobs.lever.co/acme/1b2c", "lever"),
        ("https://jobs.ashbyhq.com/acme/uuid", "ashby"),
        ("https://www.linkedin.com/jobs/view/123", "linkedin"),
        ("https://acme.wd5.myworkdayjobs.com/x", "workday"),
        ("https://acme.com/jobs/1", "unknown"),
    ],
)
def test_detect_ats(url, ats):
    assert detect_ats(url) == ats


def test_runner_needs_answers_then_whatsapp_loop(page, s, master, resume_pdf, monkeypatch):
    """Full adapter flow: fill -> unknown required question -> asked on WhatsApp -> answer -> apply resumes."""
    from tengen_toppa.apply import runner
    from tengen_toppa.discovery.base import JobPosting
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.models import Job, JobStatus, ResumeVersion
    from tengen_toppa.whatsapp import notify, router
    from tengen_toppa.whatsapp.inbound import Inbound

    sent: list[str] = []

    class WA:
        def send_text(self, t):
            sent.append(t)
            return None

        def send_image(self, p, caption=""):
            sent.append("IMG " + caption)
            return None

        def send_buttons(self, b, buttons):
            sent.append("BTN " + ",".join(x.id for x in buttons))
            return None

        def send_template(self, *a):
            sent.append("TPL")
            return None

    notify.set_client(WA())  # type: ignore[arg-type]
    notify.mark_inbound(s)
    monkeypatch.setattr("tengen_toppa.qa.llm_answers", lambda *a, **k: {})

    steps.ingest(
        s,
        [
            JobPosting(
                "greenhouse",
                "a:1",
                "Acme",
                "Backend Engineer",
                "Bengaluru, India",
                None,
                "https://x",
                "https://x",
                "desc",
            )
        ],
    )
    job = s.get(Job, 1)
    s.add(ResumeVersion(job_id=1, version=1, tailored={}, validation={}, pdf_path=str(resume_pdf)))
    s.commit()

    page.set_content(GREENHOUSE_LIKE)
    out = runner.fill_with_adapter(s, job, steps.latest_resume(s, 1), master, resume_pdf, page, GENERIC)
    assert out == "needs_answers"
    assert s.get(Job, 1).status == JobStatus.NEEDS_HUMAN
    assert any("security clearance" in m for m in sent)

    resumed: list[int] = []
    monkeypatch.setattr(runner, "apply_job", lambda s_, jid: resumed.append(jid) or "filled")
    reply = router.handle(s, Inbound("w1", "919876543210", "text", "No"), "919876543210")
    assert "resuming the application" in reply
    assert resumed == [1]

    page.set_content(GREENHOUSE_LIKE)
    out = runner.fill_with_adapter(s, s.get(Job, 1), steps.latest_resume(s, 1), master, resume_pdf, page, GENERIC)
    assert out == "filled"
    assert s.get(Job, 1).status == JobStatus.APPLYING
    assert any(m.startswith("BTN submit:1") for m in sent)
    assert runner.last_attempt(s, 1).outcome == "filled"
