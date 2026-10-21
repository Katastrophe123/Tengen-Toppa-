"""Huey tasks for LinkedIn runs. Each task does one bounded step and schedules the next, so the
dashboard and your WhatsApp commands always get a turn between applications (C2)."""

from __future__ import annotations

import logging
from datetime import datetime

from huey import crontab

from tengen_toppa.config import settings
from tengen_toppa.linkedin import controller
from tengen_toppa.linkedin.controller import StepResult
from tengen_toppa.pipeline.tasks import huey
from tengen_toppa.store.db import kv_get, kv_set, session
from tengen_toppa.whatsapp import dashboard, notify

log = logging.getLogger(__name__)


def ops():
    from tengen_toppa.linkedin.live import LiveOps

    return LiveOps()


def _schedule_next(run_id: int, res: StepResult) -> None:
    if res.delay is not None:
        li_apply_step.schedule((run_id,), delay=max(1, int(res.delay)))


@huey.task()
def li_start(kind: str = "full", trigger: str = "manual", target: int | None = None) -> str:
    with session() as s:
        try:
            run, problems = controller.start_run(s, ops(), kind=kind, trigger=trigger, target=target)
        except Exception as e:  # browser not reachable etc.
            log.exception("li_start failed")
            notify.send_text(s, f"Couldn't start the LinkedIn run: {e}", urgent=True)
            return "error"
        if run is None:
            notify.send_text(s, "Can't start the LinkedIn run:\n- " + "\n- ".join(problems), urgent=True)
            return "refused"
        li = settings().linkedin
        notify.send_text(
            s,
            f"🚀 LinkedIn run #{run.id} started ({kind}, {li.mode} mode). Target {run.target} "
            f"applications · max {li.max_per_hour}/hour · dashboard every "
            f"{settings().dashboard.interval_minutes} min.",
        )
        run_id = run.id
    li_harvest_step(run_id)
    return "started"


@huey.task()
def li_harvest_step(run_id: int) -> str:
    with session() as s:
        res = controller.harvest_step(s, run_id, ops())
    _schedule_next(run_id, res)
    return res.note


@huey.task()
def li_apply_step(run_id: int) -> str:
    with session() as s:
        res = controller.apply_step(s, run_id, ops())
    _schedule_next(run_id, res)
    return res.note


@huey.task()
def li_approve(job_id: str) -> str:
    with session() as s:
        res = controller.approve(s, job_id, ops())
        run = controller.active_run(s)
    if run and res.delay is not None:
        _schedule_next(run.id, res)  # type: ignore[arg-type]
    return res.note


@huey.task()
def li_submit(job_id: str) -> str:
    with session() as s:
        res = controller.submit(s, job_id, ops())
        if res.delay is None and res.note:
            notify.send_text(s, f"Submit: {res.note}")
        run = controller.active_run(s)
    if run and res.delay is not None:
        _schedule_next(run.id, res)  # type: ignore[arg-type]
    return res.note


@huey.task()
def li_resume_run() -> str:
    with session() as s:
        run = controller.active_run(s)
        if run is None:
            return "no run"
        if run.state in ("paused", "awaiting_you"):
            run.state = "applying"
            s.add(run)
            s.commit()
        run_id = run.id
    li_apply_step(run_id)
    return "resumed"


@huey.task()
def li_sync_status() -> int:
    with session() as s:
        try:
            n = ops().sync_status(s)
        except Exception as e:
            log.warning("status sync failed: %s", e)
            return 0
        if n:
            notify.send_text(s, f"🔄 {n} LinkedIn application status update(s). Send 'report' for the summary.")
        return n


# ---------- periodic ----------


@huey.periodic_task(crontab(minute="*"))
def li_dashboard_tick() -> str:
    with session() as s:
        return dashboard.tick(s)


@huey.periodic_task(crontab(minute="*/5"))
def li_window_tick() -> str:
    """Auto-schedule: start a run of `per_window` at each window (only if auto_schedule = true)."""
    li = settings().linkedin
    if not (li.enabled and li.auto_schedule):
        return "off"
    now = datetime.now()
    for w in li.windows:
        hh, mm = (int(x) for x in w.split(":"))
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if 0 <= (now - start).total_seconds() < 600:
            key = f"li_window:{now:%Y-%m-%d}:{w}"
            with session() as s:
                if kv_get(s, key):
                    return "already ran"
                kv_set(s, key, True)
            li_start("full", f"window {w}", li.per_window)
            return f"started window {w}"
    return "no window"


@huey.periodic_task(crontab(minute="15"))
def li_status_periodic() -> str:
    if datetime.now().hour in settings().linkedin.status_sync_hours:
        li_sync_status()
        return "sync"
    return "skip"
