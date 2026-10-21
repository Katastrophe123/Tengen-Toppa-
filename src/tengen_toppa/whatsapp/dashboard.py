"""The live WhatsApp dashboard: every N minutes while a LinkedIn run is active, only when something changed.
Also the 24h-window keep-alive prompt and the auto-pause when your commands can't reach the Mac (C4)."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta

from sqlmodel import Session, func, select

from tengen_toppa.config import settings
from tengen_toppa.linkedin.controller import active_run
from tengen_toppa.linkedin.pool import applied_last_hour, applied_today, daily_cap, pool_size
from tengen_toppa.store.db import kv_get, kv_set
from tengen_toppa.store.models import LiApplication, LiJob, LiRun, LiState, utcnow
from tengen_toppa.whatsapp import health, notify

KEEPALIVE_AFTER = timedelta(hours=23)


def _fmt_dur(start: datetime) -> str:
    start = start if start.tzinfo else start.replace(tzinfo=utcnow().tzinfo)
    mins = int((utcnow() - start).total_seconds() // 60)
    return f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def status_counts(s: Session) -> Counter:
    rows = s.exec(select(LiApplication.li_status).where(LiApplication.outcome == "applied")).all()
    c: Counter = Counter()
    for st in rows:
        low = (st or "").lower()
        if "interview" in low:
            c["interview"] += 1
        elif "not selected" in low or "rejected" in low:
            c["rejected"] += 1
        elif "resume downloaded" in low:
            c["resume downloaded"] += 1
        elif "viewed" in low:
            c["viewed"] += 1
        elif "no longer accepting" in low:
            c["closed"] += 1
        else:
            c["applied"] += 1
    return c


def build(s: Session, run: LiRun | None = None) -> str:
    cfg = settings()
    li = cfg.linkedin
    run = run or active_run(s) or s.exec(select(LiRun).order_by(LiRun.id.desc())).first()  # type: ignore[union-attr]
    online = health.commands_online(s)
    lines = []
    if run:
        lines.append(f"📊 Tengen-Toppa · Run #{run.id} · {run.state} ({_fmt_dur(run.started_at)}) · {li.mode} mode")
        lines.append(f"Pool {pool_size(s)} · harvested {run.harvested} · new eligible {run.new_in_pool}")
        lines.append(
            f"✅ Applied {run.applied}/{run.target} · ⏭ Skipped {run.skipped} · ❌ Failed {run.failed} · "
            f"❓ Waiting {run.waiting}"
        )
    else:
        lines.append(f"📊 Tengen-Toppa · idle · {li.mode} mode · pool {pool_size(s)}")
    lines.append(f"Today {applied_today(s)}/{daily_cap(s)} · last hour {applied_last_hour(s)}/{li.max_per_hour}")
    recent = s.exec(select(LiApplication).order_by(LiApplication.id.desc()).limit(3)).all()  # type: ignore[union-attr]
    if recent:
        icons = {
            "applied": "✅",
            "skipped": "⏭",
            "failed": "❌",
            "needs_answers": "❓",
            "ready": "📝",
            "submitting": "⏳",
        }
        parts = []
        for a in recent:
            job = s.get(LiJob, a.job_id)
            parts.append(
                f"{icons.get(a.outcome, '•')} {(job.title if job else a.job_id)[:28]} @ "
                f"{(job.company if job else '')[:18]}"
            )
        lines.append("Last: " + " · ".join(parts))
    if run and run.last_event:
        lines.append(f"Now: {run.last_event}")
    sc = status_counts(s)
    total = s.exec(select(func.count()).select_from(LiApplication).where(LiApplication.outcome == "applied")).one()
    if total:
        lines.append(f"All time: {total} applied · " + " · ".join(f"{v} {k}" for k, v in sc.items() if k != "applied"))
    if not online:
        lines.append(f"⚠️ commands offline {health.offline_minutes(s):.0f}m (tunnel down) — run `tengen tunnel-sync`")
    lines.append("Reply: stop · pause · resume · dash · failed · skipped · answers · cap N")
    return "\n".join(lines)


def _digest(text: str) -> str:
    stable = "\n".join(ln for ln in text.splitlines() if not ln.startswith("📊"))  # ignore the ticking timer
    return hashlib.sha1(stable.encode()).hexdigest()


def tick(s: Session) -> str:
    """Called every minute by the worker. Returns what it did (for logs/tests)."""
    cfg = settings()
    run = active_run(s)
    recently_ended = s.exec(select(LiRun).where(LiRun.ended_at >= utcnow() - timedelta(minutes=10))).first()
    if run is None and recently_ended is None:
        return "idle"

    online = health.commands_online(s)
    if (
        run
        and run.state == "applying"
        and not online
        and health.offline_minutes(s) >= cfg.dashboard.commands_offline_pause_minutes
    ):
        run.state, run.last_event = "paused", "paused: your WhatsApp commands can't reach the Mac"
        s.add(run)
        s.commit()
        notify.send_text(
            s,
            "⏸ Run paused: WhatsApp commands can't reach your Mac (tunnel down). "
            "Run `tengen tunnel-sync`, then send 'resume'.",
            urgent=True,
        )

    last_in = kv_get(s, "wa_last_inbound")
    if (
        last_in
        and run
        and utcnow() - datetime.fromisoformat(last_in) > KEEPALIVE_AFTER
        and kv_get(s, "wa_keepalive_for") != last_in
    ):
        notify.send_text(s, "⏰ Reply anything to keep the live dashboard running (WhatsApp's 24h window).")
        kv_set(s, "wa_keepalive_for", last_in)

    last = kv_get(s, "dash_last_sent")
    if last and utcnow() - datetime.fromisoformat(last) < timedelta(minutes=cfg.dashboard.interval_minutes):
        return "not due"
    text = build(s, run)
    d = _digest(text)
    if cfg.dashboard.only_when_changed and d == kv_get(s, "dash_last_digest"):
        return "unchanged"
    if not notify.window_open(s):
        return "window closed"
    notify.send_text(s, text)
    kv_set(s, "dash_last_sent", utcnow().isoformat())
    kv_set(s, "dash_last_digest", d)
    return "sent"


def list_recent(s: Session, outcome: str, n: int = 10) -> str:
    if outcome == "skipped":
        jobs = s.exec(
            select(LiJob)
            .where(LiJob.state.in_((LiState.SKIPPED, LiState.INELIGIBLE)))  # type: ignore[attr-defined]
            .order_by(LiJob.updated_at.desc())
            .limit(n)
        ).all()  # type: ignore[attr-defined]
        rows = [f"⏭ {j.title[:35]} @ {j.company[:20]}: {j.skip_reason}" for j in jobs]
    else:
        apps = s.exec(
            select(LiApplication).where(LiApplication.outcome == outcome).order_by(LiApplication.id.desc()).limit(n)
        ).all()  # type: ignore[union-attr]
        rows = []
        for a in apps:
            job = s.get(LiJob, a.job_id)
            rows.append(f"❌ {(job.title if job else a.job_id)[:35]} @ {(job.company if job else '')[:20]}: {a.detail}")
    return "\n".join(rows) or f"No {outcome} jobs."


def report(s: Session) -> str:
    since = utcnow() - timedelta(days=7)
    week = s.exec(
        select(func.count())
        .select_from(LiApplication)
        .where(LiApplication.outcome == "applied", LiApplication.submitted_at >= since)
    ).one()
    sc = status_counts(s)
    total = sum(sc.values())
    viewed = sc["viewed"] + sc["resume downloaded"] + sc["interview"]
    rate = f"{viewed / total * 100:.0f}%" if total else "-"
    return (
        f"📈 LinkedIn report\nToday {applied_today(s)} · last 7 days {week} · all time {total}\n"
        f"Viewed or better: {viewed} ({rate}) · interviews {sc['interview']} · rejected {sc['rejected']}\n"
        f"Pool {pool_size(s)} eligible jobs"
    )
