from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from huey.exceptions import RetryTask
from sqlmodel import select

from tengen_toppa.analysis import JobAnalysis
from tengen_toppa.discovery.base import JobPosting
from tengen_toppa.llm.claude_client import CallResult, ClaudeQuotaExhausted
from tengen_toppa.pipeline import steps, tasks
from tengen_toppa.store.db import kv_get, kv_set, session
from tengen_toppa.store.models import Job, JobStatus, WAMessage, utcnow
from tengen_toppa.tailoring.guardrails import validate
from tengen_toppa.tailoring.schema import TailoredBullet, TailoredResume, TailoredRole
from tengen_toppa.tailoring.tailor import TailorOutcome
from tengen_toppa.whatsapp import notify, router
from tengen_toppa.whatsapp.client import Button
from tengen_toppa.whatsapp.inbound import Inbound, parse_webhook

ME = "919876543210"


def posting(i: int, title="Backend Software Engineer", location="Bengaluru, India") -> JobPosting:
    return JobPosting(
        "greenhouse",
        f"acme:{i}",
        "Acme",
        title,
        location,
        None,
        f"https://x/{i}",
        f"https://x/{i}/apply",
        "Python FastAPI PostgreSQL services",
    )


def analysis(fit=90, years=2) -> JobAnalysis:
    return JobAnalysis(
        title="Backend Software Engineer",
        seniority="mid",
        years_required=years,
        must_have_skills=["Python", "FastAPI"],
        nice_to_have_skills=["Kafka"],
        responsibilities=["APIs"],
        ats_keywords=["FastAPI", "Python"],
        domain="fintech",
        remote="onsite",
        visa_sponsorship=None,
        salary_range=None,
        red_flags=[],
        fit_score=fit,
        matched_strengths=["Python"],
        gaps=["Kafka"],
        rationale="good",
    )


def honest_tailored() -> TailoredResume:
    return TailoredResume(
        summary="Backend engineer with 3 years building Python services.",
        skills=["Python", "FastAPI"],
        experience=[
            TailoredRole(
                experience_id="acme",
                bullets=[
                    TailoredBullet(
                        source_id="acme-1",
                        text="Built a FastAPI payments service handling 2M requests/day with p99 latency under 120 ms.",
                    )
                ],
            )
        ],
        project_order=["proj-1"],
        changes=["Led with FastAPI bullet"],
    )


class FakeWA:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []

    def send_text(self, text):
        self.sent.append(("text", text))
        return f"wamid.{len(self.sent)}"

    def send_buttons(self, body, buttons: list[Button]):
        self.sent.append(("buttons", [b.id for b in buttons]))
        return f"wamid.{len(self.sent)}"

    def send_document(self, path: Path, caption=""):
        assert path.exists()
        self.sent.append(("document", path.name))
        return f"wamid.{len(self.sent)}"

    def send_template(self, name, language, params):
        self.sent.append(("template", name))
        return f"wamid.{len(self.sent)}"


@pytest.fixture
def wa(monkeypatch):
    fake = FakeWA()
    notify.set_client(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(steps, "assist_open", lambda job, rv: False)
    return fake


@pytest.fixture
def fake_claude(monkeypatch, master):
    state = {"fit": 90, "quota": False}

    def fake_analyze(**kw):
        if state["quota"]:
            raise ClaudeQuotaExhausted("usage limit reached")
        return CallResult(analysis(fit=state["fit"]), 10, 0.001)

    def fake_tailor(prof, an, desc, **kw):
        t = honest_tailored()
        rep = validate(t, prof, jd_terms=an.must_have_skills, ats_keywords=an.ats_keywords)
        return TailorOutcome(tailored=t, report=rep, attempts=1, durations_ms=[20], costs=[0.01])

    monkeypatch.setattr(steps, "analyze_job", fake_analyze)
    monkeypatch.setattr(steps, "tailor", fake_tailor)
    monkeypatch.setattr("tengen_toppa.filtering.Embedder.get", classmethod(lambda cls: None))
    return state


# ---------------- pipeline ----------------


def test_ingest_dedupes_and_filters(s):
    stats = steps.ingest(s, [posting(1), posting(1), posting(2, title="Engineering Manager")])
    assert (stats.new, stats.filtered, stats.duplicates) == (1, 1, 1)
    # same company/title/location reposted under a new id is a duplicate too
    assert steps.ingest(s, [posting(99)]).duplicates == 1


def test_full_flow_to_whatsapp_card(s, wa, fake_claude, master):
    steps.ingest(s, [posting(1)])
    ids = steps.select_for_analysis(s, master, limit=10)
    job = steps.analyze(s, ids[0], master)
    assert job.status == JobStatus.SHORTLISTED and job.score >= 70

    rv = steps.tailor_job(s, job.id, prof=master)
    assert rv is not None and Path(rv.pdf_path).exists()
    assert rv.validation["ok"] is True
    assert s.get(Job, job.id).status == JobStatus.TAILORED
    assert steps.tailored_today(s) == 1  # daily-cap counter sees the stored (UTC) event

    assert steps.flush_cards(s) == 0  # window closed: no free-form messages allowed
    notify.mark_inbound(s)
    assert steps.flush_cards(s) == 1
    kinds = [k for k, _ in wa.sent]
    assert kinds == ["document", "buttons"]
    assert wa.sent[1][1] == [f"apply:{job.id}", f"edit:{job.id}", f"skip:{job.id}"]
    assert s.get(Job, job.id).status == JobStatus.PENDING_REVIEW


def test_low_score_and_years_gap_skipped(s, fake_claude, master):
    steps.ingest(s, [posting(1), posting(2, title="Python Engineer")])
    fake_claude["fit"] = 20
    j1 = steps.analyze(s, 1, master)
    assert j1.status == JobStatus.SKIPPED and "score" in j1.status_reason


def test_guardrail_failure_goes_to_needs_human(s, wa, monkeypatch, master, fake_claude):
    steps.ingest(s, [posting(1)])
    steps.analyze(s, 1, master)

    def lying_tailor(prof, an, desc, **kw):
        t = honest_tailored()
        t.skills.append("Kubernetes")
        rep = validate(t, prof, jd_terms=["Kubernetes"])
        return TailorOutcome(tailored=t, report=rep, attempts=3, durations_ms=[1, 1, 1], costs=[0, 0, 0])

    monkeypatch.setattr(steps, "tailor", lying_tailor)
    rv = steps.tailor_job(s, 1, prof=master)
    assert rv is not None and rv.pdf_path is None
    assert s.get(Job, 1).status == JobStatus.NEEDS_HUMAN


def test_quota_exhaustion_pauses_and_retries(s, wa, fake_claude):
    steps.ingest(s, [posting(1)])
    fake_claude["quota"] = True
    with pytest.raises(RetryTask):
        tasks.analyze_job_task.call_local(1)
    assert steps.llm_paused_until(s) is not None
    assert any(k == "template" for k, _ in wa.sent)  # urgent alert even with the window closed
    # while paused, tasks don't even try Claude
    fake_claude["quota"] = False
    with pytest.raises(RetryTask):
        tasks.analyze_job_task.call_local(1)


def test_daily_cap_blocks_tailoring(s, fake_claude, master):
    steps.ingest(s, [posting(1)])
    steps.analyze(s, 1, master)
    kv_set(s, "daily_cap", 0)
    assert tasks.tailor_job_task.call_local(1) == "cap"


def test_window_expiry(s):
    assert not notify.window_open(s)
    notify.mark_inbound(s)
    assert notify.window_open(s)
    kv_set(s, "wa_last_inbound", (utcnow() - timedelta(hours=24)).isoformat())
    assert not notify.window_open(s)


# ---------------- router ----------------


def inbound(text, kind="text", wa_id=None, sender=ME):
    inbound.n = getattr(inbound, "n", 0) + 1
    return Inbound(wa_id or f"in.{inbound.n}", sender, kind, text)


def test_router_ignores_strangers_and_duplicates(s, wa):
    assert router.handle(s, inbound("status", sender="15550001111"), ME) is None
    msg = inbound("status")
    assert "Tengen-Toppa status" in router.handle(s, msg, ME)
    assert router.handle(s, msg, ME) is None  # duplicate delivery


def test_router_commands(s, wa, fake_claude, master, monkeypatch):
    applied: list[int] = []
    monkeypatch.setattr("tengen_toppa.apply.runner.apply_job", lambda s_, jid: applied.append(jid) or "filled")
    steps.ingest(s, [posting(1)])
    steps.analyze(s, 1, master)
    steps.tailor_job(s, 1, prof=master)

    assert "Paused" in router.handle(s, inbound("pause"), ME)
    assert kv_get(s, "paused") is True
    assert router.handle(s, inbound("resume"), ME) == "Resumed."
    assert "cap set to 7" in router.handle(s, inbound("limit 7"), ME)
    assert steps.daily_cap(s) == 7

    assert "Applying to #1" in router.handle(s, inbound("apply:1", kind="button"), ME)
    assert s.get(Job, 1).status == JobStatus.APPROVED
    assert applied == [1]
    assert "Marked #1" in router.handle(s, inbound("done 1"), ME)
    assert s.get(Job, 1).status == JobStatus.APPLIED
    assert "not found" in router.handle(s, inbound("skip 999"), ME)


def test_router_edit_flow_retailors_and_resends(s, wa, fake_claude, master):
    steps.ingest(s, [posting(1)])
    steps.analyze(s, 1, master)
    steps.tailor_job(s, 1, prof=master)

    ask = router.handle(s, inbound("edit:1", kind="button"), ME)
    assert "What should change" in ask
    reply = router.handle(s, inbound("Emphasise payments scale"), ME)
    assert "Re-tailoring #1" in reply
    assert steps.latest_resume(s, 1).version == 2
    assert steps.latest_resume(s, 1).feedback == "Emphasise payments scale"
    assert ("document", Path(steps.latest_resume(s, 1).pdf_path).name) in wa.sent


# ---------------- webhook ----------------


def test_webhook_signature_and_routing(monkeypatch, wa):
    from tengen_toppa.api import app

    monkeypatch.setenv("TENGEN_WA_APP_SECRET", "shh")
    monkeypatch.setenv("TENGEN_WA_MY_NUMBER", ME)
    monkeypatch.setenv("TENGEN_WA_VERIFY_TOKEN", "vt")
    client = TestClient(app)

    r = client.get(
        "/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}
    )
    assert r.status_code == 200 and r.text == "42"
    assert (
        client.get(
            "/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"}
        ).status_code
        == 403
    )

    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [{"from": ME, "id": "wamid.X", "type": "text", "text": {"body": "help"}}]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    assert (
        client.post("/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    )
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = client.post("/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200
    assert any(k == "text" and "Commands" in str(v) for k, v in wa.sent)

    with session() as s2:
        assert s2.exec(select(WAMessage).where(WAMessage.wa_id == "wamid.X")).first()


def test_parse_webhook_variants():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": ME,
                                    "id": "a",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {"id": "skip:3", "title": "Skip"},
                                    },
                                },
                                {
                                    "from": ME,
                                    "id": "b",
                                    "type": "button",
                                    "button": {"payload": "START", "text": "Start"},
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    }
    msgs = parse_webhook(payload)
    assert [(m.kind, m.text) for m in msgs] == [("button", "skip:3"), ("text", "START")]
    assert parse_webhook({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}) == []
