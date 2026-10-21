"""Run controller with a fake browser: caps, crash safety, years filter, stop, review flow, questions."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from tengen_toppa.apply.fields import FormField
from tengen_toppa.config import settings
from tengen_toppa.linkedin import controller
from tengen_toppa.linkedin.easy_apply import ApplyResult
from tengen_toppa.linkedin.harvest import HarvestStats
from tengen_toppa.linkedin.jobpage import JobPageInfo
from tengen_toppa.linkedin.pool import applied_today, fingerprint
from tengen_toppa.linkedin.search import LinkedInBlocked
from tengen_toppa.pipeline import questions
from tengen_toppa.resume_file import ResumeInfo
from tengen_toppa.store.models import LiApplication, LiJob, LiRun, LiState, utcnow
from tengen_toppa.whatsapp import dashboard, notify


class FakeOps:
    def __init__(self, s, jobs: dict[str, dict] | None = None):
        self.s = s
        self.pages = jobs or {}
        self.apply_calls: list[dict] = []
        self.results: dict[str, ApplyResult | Exception] = {}
        self.submitted: list[str] = []

    def harvest(self, s, cfg):
        return HarvestStats(pages=2, seen=10, new=3, eligible_new=3, pool=3)

    def check_job(self, job_id):
        p = self.pages.get(job_id, {})
        return JobPageInfo(
            job_id,
            "t",
            "c",
            p.get("desc", "We need 0-1 years of experience"),
            p.get("years", 0),
            p.get("easy", True),
            p.get("applied", False),
            p.get("closed", False),
        )

    def apply(self, job_id, *, allow_open, allow_submit, stop_before_submit, resolve_fields, screenshot_dir):
        # C3: the 'submitting' row must already be committed when the risky click happens
        row = self.s.exec(
            select(LiApplication).where(LiApplication.job_id == job_id, LiApplication.outcome == "submitting")
        ).first()
        assert row is not None, "apply called before the submitting record was written"
        self.apply_calls.append(
            {"job": job_id, "allow_open": allow_open, "allow_submit": allow_submit, "stop": stop_before_submit}
        )
        r = self.results.get(job_id, ApplyResult("ready" if stop_before_submit else "applied", "ok", 3))
        if isinstance(r, Exception):
            raise r
        return r

    def submit_ready(self, job_id, screenshot_dir):
        self.submitted.append(job_id)
        return ApplyResult("applied", "submitted")

    def canary(self):
        return []


class WA:
    def __init__(self):
        self.sent: list[str] = []

    def send_text(self, t):
        self.sent.append(t)

    def send_buttons(self, body, buttons):
        self.sent.append("BTN " + ",".join(b.id for b in buttons))

    def send_image(self, p, caption=""):
        self.sent.append("IMG " + caption)

    def send_template(self, *a):
        self.sent.append("TPL")


@pytest.fixture
def env(s, monkeypatch, tmp_path):
    wa = WA()
    notify.set_client(wa)  # type: ignore[arg-type]
    notify.mark_inbound(s)
    pdf = tmp_path / "Kanishk_Singh_Parihar_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(controller, "current", lambda s_: ResumeInfo(pdf, "abc", pdf.name, 2))
    monkeypatch.setattr("tengen_toppa.resume_file.current", lambda s_: ResumeInfo(pdf, "abc", pdf.name, 2))
    monkeypatch.setattr("tengen_toppa.resume_file.profile_mismatches", lambda *a: [])
    monkeypatch.setattr("tengen_toppa.whatsapp.health.commands_online", lambda s_, **k: True)
    monkeypatch.setattr("tengen_toppa.whatsapp.health.alert_template_status", lambda s_: "APPROVED")
    cfg = settings()
    cfg.linkedin.mode = "auto"
    cfg.linkedin.max_applications_per_day = 30
    cfg.linkedin.max_per_hour = 10
    return wa


def add_jobs(s, n: int, prefix: str = "j") -> list[str]:
    ids = []
    for i in range(n):
        jid = f"{prefix}{i}"
        s.add(
            LiJob(
                job_id=jid,
                title=f"AI Engineer {i}",
                company=f"Co{i}",
                fingerprint=fingerprint(f"Co{i}", "AI"),
                score=1 - i / 100,
            )
        )
        ids.append(jid)
    s.commit()
    return ids


def start(s, ops, **kw) -> LiRun:
    run, problems = controller.start_run(s, ops, **kw)
    assert run is not None, problems
    res = controller.harvest_step(s, run.id, ops)
    assert res.delay is not None
    return run


def drain(s, run_id, ops, max_steps=50):
    notes = []
    for _ in range(max_steps):
        r = controller.apply_step(s, run_id, ops)
        notes.append(r.note)
        if r.delay is None or r.note in ("hourly cap",):
            break
    return notes


def test_auto_run_applies_and_stops_exactly_at_the_daily_cap(s, env):
    settings().linkedin.max_applications_per_day = 2
    add_jobs(s, 4)
    ops = FakeOps(s)
    run = start(s, ops)
    drain(s, run.id, ops)
    assert len(ops.apply_calls) == 2 and applied_today(s) == 2
    assert all(c["allow_open"] and c["allow_submit"] and not c["stop"] for c in ops.apply_calls)
    run = s.get(LiRun, run.id)
    assert run.state == "done" and "daily cap" in run.stop_reason
    assert any("🏁" in m for m in env.sent)


def test_daily_cap_counts_previous_runs_and_cap_override(s, env):
    settings().linkedin.max_per_hour = 100  # the 29 earlier rows are timestamped "now"
    add_jobs(s, 5)
    for _i in range(29):
        s.add(LiApplication(job_id="j0", outcome="applied", submitted_at=utcnow()))
    s.commit()
    ops = FakeOps(s)
    run = start(s, ops)
    assert run.target == 1
    drain(s, run.id, ops)
    assert applied_today(s) == 30 and len(ops.apply_calls) == 1

    # WhatsApp `cap N` can lower the cap but never raise it above settings
    from tengen_toppa.linkedin.pool import daily_cap
    from tengen_toppa.store.db import kv_set

    kv_set(s, "li_daily_cap", 50)
    assert daily_cap(s) == 30
    kv_set(s, "li_daily_cap", 10)
    assert daily_cap(s) == 10


def test_jobs_needing_more_than_one_year_are_never_opened(s, env):
    add_jobs(s, 3)
    ops = FakeOps(s, {"j0": {"years": 3, "desc": "3+ years of experience"}, "j1": {"years": 1}, "j2": {"years": None}})
    run = start(s, ops)
    drain(s, run.id, ops)
    assert [c["job"] for c in ops.apply_calls] == ["j1", "j2"]
    j0 = s.get(LiJob, "j0")
    assert j0.state == LiState.INELIGIBLE and "needs 3+ years" in j0.skip_reason


def test_closed_and_already_applied_are_skipped_without_clicks(s, env):
    add_jobs(s, 2)
    ops = FakeOps(s, {"j0": {"closed": True}, "j1": {"applied": True}})
    run = start(s, ops)
    drain(s, run.id, ops)
    assert ops.apply_calls == []
    assert s.get(LiJob, "j0").state == LiState.CLOSED and s.get(LiJob, "j1").state == LiState.APPLIED


def test_crash_mid_apply_is_never_retried_blindly(s, env):
    add_jobs(s, 1)
    ops = FakeOps(s)
    ops.results["j0"] = RuntimeError("browser died")
    run = start(s, ops)
    controller.apply_step(s, run.id, ops)
    app = s.exec(select(LiApplication)).one()
    assert app.outcome == "submitting"  # unknown: kept, counts towards the cap
    assert applied_today(s) == 1

    # restart later: LinkedIn says it DID go through -> recorded as applied, not re-applied
    app.created_at = utcnow() - timedelta(minutes=10)
    s.add(app)
    s.commit()
    ops2 = FakeOps(s, {"j0": {"applied": True}})
    assert controller.recover_submitting(s, ops2) == 1
    assert s.exec(select(LiApplication)).one().outcome == "applied"
    assert s.get(LiJob, "j0").state == LiState.APPLIED
    assert ops2.apply_calls == []


def test_crash_recovery_requeues_when_nothing_was_sent(s, env):
    add_jobs(s, 1)
    s.add(LiApplication(job_id="j0", outcome="submitting", created_at=utcnow() - timedelta(minutes=10)))
    job = s.get(LiJob, "j0")
    job.state, job.attempts = LiState.SUBMITTING, 1
    s.add(job)
    s.commit()
    controller.recover_submitting(s, FakeOps(s, {"j0": {"applied": False}}))
    assert s.exec(select(LiApplication)).one().outcome == "failed"
    assert s.get(LiJob, "j0").state == LiState.POOLED


def test_stop_file_and_stop_command(s, env):
    add_jobs(s, 3)
    ops = FakeOps(s)
    run = start(s, ops)
    controller.request_stop(s)
    controller.apply_step(s, run.id, ops)
    assert s.get(LiRun, run.id).state == "stopped" and ops.apply_calls == []

    controller.stop_file().write_text("")
    try:
        _, problems = controller.start_run(s, ops)
        assert any("STOP file" in p for p in problems)
    finally:
        controller.stop_file().unlink()


def test_linkedin_block_stops_the_run_and_alerts(s, env):
    add_jobs(s, 2)
    ops = FakeOps(s)
    ops.results["j0"] = ApplyResult("blocked", "LinkedIn security check / restriction")
    run = start(s, ops)
    drain(s, run.id, ops)
    assert s.get(LiRun, run.id).state == "stopped"
    assert len(ops.apply_calls) == 1
    assert s.exec(select(LiApplication)).one().outcome == "submitting"  # verified later, never assumed


def test_harvest_block_stops_before_any_apply(s, env):
    class Blocked(FakeOps):
        def harvest(self, s, cfg):
            raise LinkedInBlocked("login or checkpoint page")

    ops = Blocked(s)
    run, _ = controller.start_run(s, ops)
    controller.harvest_step(s, run.id, ops)
    assert s.get(LiRun, run.id).state == "stopped"


def test_hourly_cap_waits(s, env):
    settings().linkedin.max_per_hour = 2
    add_jobs(s, 4)
    ops = FakeOps(s)
    run = start(s, ops)
    notes = drain(s, run.id, ops)
    assert notes[-1] == "hourly cap" and len(ops.apply_calls) == 2


def test_consecutive_failures_stop_the_run(s, env):
    settings().linkedin.stop_after_consecutive_failures = 2
    add_jobs(s, 4)
    ops = FakeOps(s)
    for j in ("j0", "j1", "j2", "j3"):
        ops.results[j] = ApplyResult("failed", "stuck on a step")
    run = start(s, ops)
    drain(s, run.id, ops)
    assert s.get(LiRun, run.id).state == "stopped" and len(ops.apply_calls) == 2


def test_unknown_questions_are_asked_once_for_many_jobs(s, env):
    add_jobs(s, 3)
    ops = FakeOps(s)
    q = FormField("k", "How many years of experience do you have with Kubernetes?", "text", True)
    for j in ("j0", "j1"):
        ops.results[j] = ApplyResult("needs_answers", "1 question(s) need you", 2, missing=[q])
    run = start(s, ops)
    drain(s, run.id, ops)
    queue = questions.queue(s)
    assert len(queue) == 1 and queue[0]["li_job_ids"] == ["j0", "j1"]
    assert sum("2 LinkedIn job(s)" in m or "1 LinkedIn job(s)" in m for m in env.sent) >= 1
    assert applied_today(s) == 1  # needs_answers rows don't count towards the cap

    res = questions.answer(s, "0")
    assert res.li_ready == ["j0", "j1"]
    assert controller.requeue_answered(s, res.li_ready) == 2
    assert s.get(LiJob, "j0").state == LiState.POOLED


def test_review_mode_needs_your_approval_and_your_submit(s, env):
    settings().linkedin.mode = "review"
    add_jobs(s, 2)
    ops = FakeOps(s)
    run = start(s, ops)
    r = controller.apply_step(s, run.id, ops)
    assert r.note == "awaiting approval" and ops.apply_calls == []
    assert any(m.startswith("BTN liapply:j0") for m in env.sent)

    r = controller.approve(s, "j0", ops)
    assert ops.apply_calls[-1] == {"job": "j0", "allow_open": True, "allow_submit": False, "stop": True}
    assert s.get(LiRun, run.id).state == "awaiting_you"

    r = controller.submit(s, "j0", ops)
    assert ops.submitted == ["j0"] and s.get(LiJob, "j0").state == LiState.APPLIED
    assert r.delay is not None  # next job follows


def test_dashboard_sends_only_when_due_and_changed(s, env):
    add_jobs(s, 2)
    ops = FakeOps(s)
    run = start(s, ops)
    assert dashboard.tick(s) == "sent"
    text = [m for m in env.sent if m.startswith("📊")][-1]
    assert f"Run #{run.id}" in text and "Today 0/30" in text and "Reply: stop" in text
    assert dashboard.tick(s) == "not due"
    from tengen_toppa.store.db import kv_set

    kv_set(s, "dash_last_sent", (utcnow() - timedelta(minutes=5)).isoformat())
    assert dashboard.tick(s) == "unchanged"
    controller.apply_step(s, run.id, ops)
    kv_set(s, "dash_last_sent", (utcnow() - timedelta(minutes=5)).isoformat())
    assert dashboard.tick(s) == "sent"
    assert "✅ Applied 1/" in [m for m in env.sent if m.startswith("📊")][-1]


def test_dashboard_pauses_run_when_commands_are_offline(s, env, monkeypatch):
    add_jobs(s, 2)
    ops = FakeOps(s)
    run = start(s, ops)
    monkeypatch.setattr("tengen_toppa.whatsapp.health.commands_online", lambda s_, **k: False)
    monkeypatch.setattr("tengen_toppa.whatsapp.health.offline_minutes", lambda s_: 12.0)
    dashboard.tick(s)
    assert s.get(LiRun, run.id).state == "paused"
    assert controller.apply_step(s, run.id, ops).note == "paused" and ops.apply_calls == []


def test_preflight_requires_approved_alert_template_for_auto(s, env, monkeypatch):
    monkeypatch.setattr("tengen_toppa.whatsapp.health.alert_template_status", lambda s_: "PENDING")
    _, problems = controller.start_run(s, FakeOps(s))
    assert any("template" in p for p in problems)
    settings().linkedin.mode = "review"
    run, problems = controller.start_run(s, FakeOps(s))
    assert run is not None, problems


def test_dry_run_never_applies(s, env):
    add_jobs(s, 3)
    ops = FakeOps(s)
    run, _ = controller.start_run(s, ops, kind="dry")
    res = controller.harvest_step(s, run.id, ops)
    assert res.delay is None and ops.apply_calls == [] and s.get(LiRun, run.id).state == "done"


def test_screens_dir_is_per_job(tmp_path):
    assert isinstance(controller._screens("123"), Path) and controller._screens("123").name == "li-123"
