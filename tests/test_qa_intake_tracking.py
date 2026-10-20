from __future__ import annotations

import sqlite3

import httpx
import pytest
import respx

from tengen_toppa.analysis import JobAnalysis
from tengen_toppa.apply.fields import FormField, pick_option
from tengen_toppa.config import ApplicantSettings
from tengen_toppa.discovery.base import JobPosting
from tengen_toppa.intake import add_job_from_url, find_url
from tengen_toppa.pipeline import steps
from tengen_toppa.qa import bank_answer, rule_answer, save_answer
from tengen_toppa.store.models import Job, JobStatus, ProcessedEmail
from tengen_toppa.tailoring.cover_letter import CoverLetter, check_cover_letter, render_cover_letter
from tengen_toppa.tracking import EmailVerdict, parse_mail, process_mails

# ---------------- Q&A ----------------


@pytest.mark.parametrize(
    "label, expected",
    [
        ("First Name", "Your"),
        ("Last name *", "Name"),
        ("Email address", "you@example.com"),
        ("LinkedIn Profile URL", "https://linkedin.com/in/you"),
        ("Will you require visa sponsorship?", "No"),
        ("Are you legally authorized to work in India?", "Yes"),
        ("Notice period", "30 days"),
        ("How did you hear about us?", "Company careers page"),
        ("Total years of experience", "3"),
        ("Current company", "Acme Corp"),
        ("Do you have experience with Kubernetes?", None),
    ],
)
def test_rule_answers(master, label, expected):
    assert rule_answer(label, master, ApplicantSettings()) == expected


def test_pick_option():
    assert pick_option(["Select...", "Yes, I am", "No, I am not"], "Yes") == "Yes, I am"
    assert (
        pick_option(["Male", "Female", "I don't wish to answer"], "Decline to self-identify")
        == "I don't wish to answer"
    )
    assert pick_option(["India", "USA"], "Germany") is None


def test_bank_fuzzy_and_update(s):
    save_answer(s, "Are you willing to work from our Bangalore office 3 days a week?", "Yes")
    assert bank_answer(s, "Are you willing to work from our Bangalore office 3 days/week?") == "Yes"
    assert bank_answer(s, "What is your favourite colour?") is None
    save_answer(s, "Are you willing to work from our Bangalore office 3 days a week?", "No")
    assert bank_answer(s, "Are you willing to work from our Bangalore office 3 days a week?") == "No"


def test_llm_answers_are_constrained(monkeypatch, master):
    from tengen_toppa import qa
    from tengen_toppa.llm.claude_client import CallResult

    reply = qa.LLMAnswers(
        answers=[
            qa.LLMAnswer(key="a", answer="Yes", source="profile"),
            qa.LLMAnswer(key="b", answer="I love APIs", source="grounded_writing"),  # not a textarea -> dropped
            qa.LLMAnswer(key="c", answer="Maybe", source="profile"),  # not an option -> dropped
            qa.LLMAnswer(key="d", answer="", source="unknown"),
            qa.LLMAnswer(key="e", answer="I enjoy building payment systems.", source="grounded_writing"),
        ]
    )
    monkeypatch.setattr(qa, "ask", lambda *a, **k: CallResult(reply, 1, 0.0))
    fields = [
        FormField("a", "Do you know Python?", "radio", True, ["Yes", "No"]),
        FormField("b", "Why us?", "text", True),
        FormField("c", "Relocate?", "select", True, ["Yes", "No"]),
        FormField("d", "SAP experience?", "text", True),
        FormField("e", "Why do you want to join?", "textarea", True),
    ]
    out = qa.llm_answers(
        fields, profile=master, applicant=ApplicantSettings(), bank=[], job_context="", model="haiku", timeout=10
    )
    assert out == {"a": "Yes", "e": "I enjoy building payment systems."}


# ---------------- intake ----------------


def test_find_url():
    assert find_url("check this https://jobs.lever.co/acme/abc-123). thanks") == "https://jobs.lever.co/acme/abc-123"
    assert find_url("status") is None


@respx.mock
def test_add_greenhouse_url_uses_api(s):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Backend Engineer",
                "company_name": "Acme",
                "location": {"name": "Remote - India"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                "content": "&lt;p&gt;Python&lt;/p&gt;",
            },
        )
    )
    job = add_job_from_url(s, "https://boards.greenhouse.io/acme/jobs/42")
    assert (job.company, job.title, job.remote, job.description) == ("Acme", "Backend Engineer", True, "Python")
    assert job.status == JobStatus.DISCOVERED
    assert add_job_from_url(s, "https://boards.greenhouse.io/acme/jobs/42").id == job.id  # deduped


# ---------------- tracking ----------------

RAW = (
    b"Message-ID: <m1@acme.com>\r\nFrom: Acme Recruiting <talent@acme.com>\r\n"
    b"Subject: Interview invitation - Backend Engineer\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Hi, we'd like to schedule a technical interview for the Backend Engineer role at Acme.\r\n"
)
UNRELATED = (
    b"Message-ID: <n1@shop.com>\r\nFrom: Shop <deals@shop.com>\r\nSubject: 50% off\r\n"
    b"Content-Type: text/plain\r\n\r\nBuy now\r\n"
)


def test_process_mails_updates_status_and_skips_unrelated(s, monkeypatch):
    steps.ingest(
        s, [JobPosting("greenhouse", "a:1", "Acme", "Backend Engineer", "Bengaluru, India", None, "u", "u", "d")]
    )
    job = s.get(Job, 1)
    job.status = JobStatus.APPLIED
    s.add(job)
    s.commit()
    calls: list[str] = []

    def fake_classify(mail, company):
        calls.append(mail.subject)
        return EmailVerdict(
            about_job_application=True,
            company=company,
            category="interview",
            summary="They want to schedule a technical interview.",
            action_needed="Reply with slots",
        )

    monkeypatch.setattr("tengen_toppa.tracking.classify", fake_classify)
    mails = [parse_mail(RAW), parse_mail(UNRELATED)]
    assert process_mails(s, mails) == 1
    assert calls == ["Interview invitation - Backend Engineer"]  # unrelated mail never reaches Claude
    assert s.get(Job, 1).status == JobStatus.INTERVIEW
    assert s.get(ProcessedEmail, "<m1@acme.com>") is not None
    assert process_mails(s, mails) == 0  # idempotent


# ---------------- cover letter ----------------


def test_cover_letter_checks_and_render(master, tmp_path):
    good = CoverLetter(
        greeting="Dear Hiring Team,",
        paragraphs=[
            "At Acme Corp I built a FastAPI payments service handling 2M requests/day.",
            "I would bring the same focus on reliable Python services to your team.",
        ],
        closing="Sincerely,",
    )
    assert check_cover_letter(good, master, ["Kubernetes"]) == []
    bad = CoverLetter(greeting="Hi", paragraphs=["I scaled Kubernetes clusters to 900 nodes."], closing="Thanks")
    problems = check_cover_letter(bad, master, ["Kubernetes"])
    assert any("900" in p for p in problems) and any("Kubernetes" in p for p in problems)
    pdf = render_cover_letter(good, master, "Stripe", tmp_path)
    assert pdf.exists() and pdf.stat().st_size > 1000
    assert "Dear Hiring Team" in (tmp_path / "cover_letter.txt").read_text()


def test_analysis_schema_roundtrip():
    JobAnalysis.model_json_schema()  # schema used by --json-schema must build


# ---------------- migration ----------------


def test_additive_migration(tmp_path):
    from tengen_toppa.store.db import make_engine

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE resumeversion (id INTEGER PRIMARY KEY, job_id INTEGER, version INTEGER, "
        "feedback TEXT, tailored JSON, validation JSON, pdf_path VARCHAR, created_at DATETIME)"
    )
    con.commit()
    con.close()
    make_engine(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(resumeversion)")}
    assert {"cover_letter_text", "cover_letter_pdf"} <= cols
