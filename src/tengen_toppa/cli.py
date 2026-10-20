"""`tengen` command-line interface."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated

import typer
from sqlmodel import select

from tengen_toppa.config import SAMPLE_SETTINGS_TOML
from tengen_toppa.paths import get_paths
from tengen_toppa.profile import SAMPLE_PROFILE

app = typer.Typer(help="Tengen-Toppa: local-first AI job application autopilot.", no_args_is_help=True)
secrets_app = typer.Typer(help="Manage secrets in the macOS Keychain.", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")
qa_app = typer.Typer(help="Your saved answers for application forms.", no_args_is_help=True)
app.add_typer(qa_app, name="qa")


def _setup_logging(verbose: bool = False) -> None:
    paths = get_paths()
    paths.logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = RotatingFileHandler(paths.logs / "tengen.log", maxBytes=5_000_000, backupCount=3)
    file_h.setFormatter(fmt)
    err_h = logging.StreamHandler(sys.stderr)
    err_h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [file_h, err_h]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    _setup_logging(verbose)


# ---------------- setup ----------------


@app.command()
def init() -> None:
    """Create the runtime folders + config/settings.toml + data/profile/master_profile.json (never overwrites)."""
    paths = get_paths()
    paths.ensure()
    typer.echo(f"home: {paths.home}")
    if not paths.settings_file.exists():
        paths.settings_file.write_text(SAMPLE_SETTINGS_TOML)
        typer.echo(f"wrote {paths.settings_file}")
    if not paths.profile.exists():
        paths.profile.write_text(json.dumps(SAMPLE_PROFILE, indent=2))
        typer.echo(f"wrote {paths.profile}")
    from tengen_toppa.store.db import engine

    engine()
    typer.echo(f"database at {paths.db}")
    typer.echo("\nNext: edit master_profile.json + settings.toml, then run `tengen doctor`.")


@app.command()
def doctor() -> None:
    """Check Claude subscription auth, profile, renderer, secrets."""
    from tengen_toppa.llm.claude_client import ClaudeError, assert_subscription_auth, claude_bin
    from tengen_toppa.profile import load_profile
    from tengen_toppa.secrets import Secret, get_secret

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        typer.echo(f"[{'OK' if passed else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))

    paths = get_paths()
    check("home folder", paths.home.exists(), str(paths.home))
    try:
        check("claude CLI", True, claude_bin())
        status = assert_subscription_auth()
        check(
            "Claude subscription auth",
            True,
            f"{status.get('authMethod')} / {status.get('subscriptionType', '?')} / {status.get('email', '')}",
        )
    except ClaudeError as e:
        check("Claude subscription auth", False, str(e))
    check(
        "ANTHROPIC_API_KEY not used",
        True,
        "set in your shell but stripped for every call" if os.environ.get("ANTHROPIC_API_KEY") else "not set",
    )

    try:
        prof = load_profile(paths.profile)
        check(
            "master profile",
            True,
            f"{len(prof.experience)} roles, {len(prof.projects)} projects, {len(prof.skills_verified)} skills",
        )
        if prof.basics.email == "you@example.com":
            check("master profile filled in", False, "still the sample profile; edit it")
        from tengen_toppa.tailoring.render import ats_parse_check, build_document, render_pdf

        pdf = render_pdf(build_document(prof, None), paths.data / "master_preview")
        problems = ats_parse_check(pdf, prof)
        check("Typst render + ATS parse-back", not problems, "; ".join(problems) or str(pdf))
    except Exception as e:
        check("master profile", False, str(e))

    for sec in (
        Secret.WA_ACCESS_TOKEN,
        Secret.WA_PHONE_NUMBER_ID,
        Secret.WA_APP_SECRET,
        Secret.WA_VERIFY_TOKEN,
        Secret.WA_MY_NUMBER,
    ):
        check(
            f"secret {sec.value}",
            bool(get_secret(sec)),
            "" if get_secret(sec) else "tengen secrets set " + sec.value,
        )
    check("cloudflared installed", shutil.which("cloudflared") is not None, "brew install cloudflared")

    from tengen_toppa.apply.browser import BrowserError, executable
    from tengen_toppa.config import settings

    cfg = settings()
    try:
        check("browser for applying", True, executable(cfg.apply.browser))
    except BrowserError as e:
        check("browser for applying", False, str(e))
    npx = shutil.which("npx")
    check(
        "npx (for Claude browser fallback)",
        npx is not None or not cfg.apply.claude_fallback,
        npx or "brew install node",
    )
    if cfg.email.enabled:
        check(
            "email tracking",
            bool(cfg.email.address and get_secret(Secret.EMAIL_APP_PASSWORD)),
            "set [email].address and `tengen secrets set email_app_password`",
        )
    raise typer.Exit(0 if ok else 1)


@secrets_app.command("set")
def secrets_set(name: str) -> None:
    """Store a secret in the Keychain (value prompted, hidden)."""
    from tengen_toppa.secrets import Secret, set_secret

    try:
        sec = Secret(name)
    except ValueError:
        typer.echo("Valid names: " + ", ".join(s.value for s in Secret))
        raise typer.Exit(2) from None
    value = typer.prompt(f"{name} (input hidden, paste then Enter)", hide_input=True).strip().strip("\"'")
    problem = _secret_problem(sec, value)
    if problem:
        typer.echo(f"Not saved: {problem}")
        raise typer.Exit(1)
    set_secret(sec, value)
    typer.echo(f"stored {name} in Keychain ({len(value)} chars)")


def _secret_problem(sec, value: str) -> str | None:
    import re

    from tengen_toppa.secrets import Secret

    if not value:
        return "empty value"
    if re.match(r"^(uv|tengen|\./|cd )\b", value) or " secrets set " in value:
        return "that looks like a command, not the secret. Paste the secret value itself."
    if sec == Secret.WA_ACCESS_TOKEN and (not value.startswith("EAA") or re.search(r"\s", value) or len(value) < 100):
        return "Meta access tokens start with 'EAA', have no spaces and are 150+ characters. Copy the whole token."
    if sec == Secret.WA_PHONE_NUMBER_ID and not value.isdigit():
        return "the Phone number ID is digits only (shown under the test number in API Setup)."
    if sec == Secret.WA_MY_NUMBER and not (value.isdigit() and 11 <= len(value) <= 15):
        return "digits only WITH country code, e.g. 919876543210"
    if sec == Secret.WA_APP_SECRET and not re.fullmatch(r"[0-9a-f]{32}", value):
        return "the App secret is 32 hex characters (App settings → Basic → Show)."
    return None


@secrets_app.command("list")
def secrets_list() -> None:
    from tengen_toppa.secrets import Secret, get_secret

    for sec in Secret:
        typer.echo(f"{sec.value:22} {'set' if get_secret(sec) else '-'}")


# ---------------- pipeline (manual, no worker needed) ----------------


@app.command()
def discover() -> None:
    """Fetch jobs from configured boards and apply free hard filters."""
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session

    with session() as s:
        stats, errors = steps.discover(s)
    typer.echo(f"new: {stats.new}  filtered: {stats.filtered}  duplicates: {stats.duplicates}")
    for e in errors:
        typer.echo(f"error: {e}")


@app.command()
def analyze(
    job_id: Annotated[int | None, typer.Argument()] = None,
    limit: Annotated[int, typer.Option(help="max jobs to send to Claude")] = 5,
) -> None:
    """Score discovered jobs with Claude (Haiku). Uses your subscription."""
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session
    from tengen_toppa.store.models import JobStatus

    with session() as s:
        prof = steps.profile()
        if job_id is not None:
            job = steps.get_job(s, job_id)
            if job.status != JobStatus.DISCOVERED:
                job.status = JobStatus.DISCOVERED
                s.add(job)
                s.commit()
            ids = [job_id]
        else:
            ids = steps.select_for_analysis(s, prof, limit)
        for i in ids:
            job = steps.analyze(s, i, prof)
            typer.echo(f"#{job.id} {job.score}  {job.status}  {job.title} @ {job.company}  ({job.status_reason})")


@app.command()
def tailor(
    job_id: int,
    feedback: Annotated[str | None, typer.Option("--feedback", "-f")] = None,
    open_pdf: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Tailor + render the resume for one analyzed job (Sonnet)."""
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session

    with session() as s:
        rv = steps.tailor_job(s, job_id, feedback=feedback)
        if rv is None:
            typer.echo("tailoring failed; see `tengen show`")
            raise typer.Exit(1)
        typer.echo(json.dumps(rv.validation, indent=2))
        if rv.pdf_path:
            typer.echo(rv.pdf_path)
            if open_pdf and sys.platform == "darwin":
                subprocess.run(["open", rv.pdf_path], check=False)


@app.command()
def run(tailor_limit: Annotated[int, typer.Option(help="max resumes to tailor this run")] = 3) -> None:
    """One full pass synchronously: discover -> prefilter -> analyze -> tailor -> WhatsApp cards."""
    from tengen_toppa.config import settings
    from tengen_toppa.llm.claude_client import ClaudeQuotaExhausted
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session
    from tengen_toppa.store.models import JobStatus

    with session() as s:
        prof = steps.profile()
        stats, errors = steps.discover(s)
        typer.echo(f"discovered new={stats.new} filtered={stats.filtered} dupes={stats.duplicates} errors={errors}")
        try:
            for jid in steps.select_for_analysis(s, prof, settings().pipeline.analyze_per_run):
                job = steps.analyze(s, jid, prof)
                typer.echo(f"  #{job.id} {job.score} {job.status}: {job.title} @ {job.company}")
            shortlisted = sorted(steps.jobs_by_status(s, JobStatus.SHORTLISTED), key=lambda j: -(j.score or 0))
            for job in shortlisted[:tailor_limit]:
                rv = steps.tailor_job(s, job.id, prof=prof)  # type: ignore[arg-type]
                typer.echo(f"  tailored #{job.id}: {rv.pdf_path if rv else 'failed'}")
        except ClaudeQuotaExhausted as e:
            typer.echo(f"Claude limit reached: {e}")
        sent = steps.flush_cards(s)
        typer.echo(f"WhatsApp cards sent: {sent}")


@app.command()
def jobs(
    status: Annotated[str | None, typer.Option("--status", "-s")] = None,
    limit: Annotated[int, typer.Option()] = 30,
) -> None:
    """List jobs."""
    from tengen_toppa.store.db import session
    from tengen_toppa.store.models import Job

    with session() as s:
        q = select(Job).order_by(Job.id.desc()).limit(limit)  # type: ignore[union-attr]
        if status:
            q = q.where(Job.status == status)
        for j in s.exec(q):
            score = f"{j.score:5.1f}" if j.score is not None else "    -"
            typer.echo(f"#{j.id:<5} {score}  {j.status:<15} {j.title[:45]:<45} {j.company[:20]:<20} {j.location[:25]}")


@app.command()
def show(job_id: int) -> None:
    """Show one job's analysis and latest resume."""
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session

    with session() as s:
        j = steps.get_job(s, job_id)
        typer.echo(f"#{j.id} {j.title} @ {j.company} | {j.location} | {j.status} ({j.status_reason})")
        typer.echo(f"score={j.score} prefilter={j.prefilter_score}\napply: {j.apply_url}")
        if j.analysis:
            typer.echo(json.dumps(j.analysis, indent=2))
        rv = steps.latest_resume(s, job_id)
        if rv:
            typer.echo(f"resume v{rv.version}: {rv.pdf_path}\nvalidation: {json.dumps(rv.validation, indent=2)}")


@app.command()
def status() -> None:
    """Pipeline status (same as WhatsApp 'status')."""
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session

    with session() as s:
        typer.echo(steps.status_text(s))


@app.command("preview-master")
def preview_master() -> None:
    """Render your untailored master resume to check the profile + template."""
    from tengen_toppa.pipeline import steps
    from tengen_toppa.tailoring.render import build_document, render_pdf

    pdf = render_pdf(build_document(steps.profile(), None), get_paths().data / "master_preview")
    typer.echo(pdf)
    if sys.platform == "darwin":
        subprocess.run(["open", str(pdf)], check=False)


@app.command()
def backup() -> None:
    from tengen_toppa.store.db import backup_db

    typer.echo(backup_db(get_paths().backups))


# ---------------- applying ----------------


@app.command()
def add(url: str, tailor_now: Annotated[bool, typer.Option("--tailor/--no-tailor")] = True) -> None:
    """Add a job from any link (LinkedIn, Naukri, Greenhouse, Lever, Ashby, company site)."""
    from tengen_toppa.intake import add_job_from_url
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session
    from tengen_toppa.store.models import JobStatus

    with session() as s:
        job = add_job_from_url(s, url)
        typer.echo(f"#{job.id} {job.title} @ {job.company} ({job.source}) apply: {job.apply_url}")
        if not job.analysis:
            job.status = JobStatus.DISCOVERED
            job = steps.analyze(s, job.id)  # type: ignore[arg-type]
            typer.echo(f"score {job.score} -> {job.status} ({job.status_reason})")
        if tailor_now and job.analysis:
            rv = steps.tailor_job(s, job.id)  # type: ignore[arg-type]
            typer.echo(rv.pdf_path if rv and rv.pdf_path else "tailoring rejected; see `tengen show`")


@app.command()
def apply(job_id: int) -> None:
    """Fill the application for a tailored job in the dedicated Chrome (submits only if auto_submit=true)."""
    from tengen_toppa.apply.runner import apply_job
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session

    with session() as s:
        steps.mark_approved(s, job_id)
        typer.echo(apply_job(s, job_id))
        from tengen_toppa.apply.runner import last_attempt

        att = last_attempt(s, job_id)
        if att:
            typer.echo(json.dumps(att.report, indent=2, default=str))
            if att.screenshot:
                typer.echo(f"screenshot: {att.screenshot}")


@app.command()
def submit(job_id: int) -> None:
    """Submit a form that `apply` already filled."""
    from tengen_toppa.apply.runner import submit_job
    from tengen_toppa.store.db import session

    with session() as s:
        typer.echo(submit_job(s, job_id))


@app.command("cover-letter")
def cover_letter(job_id: int) -> None:
    """Generate (or regenerate) the grounded cover letter PDF for a tailored job."""
    from tengen_toppa.apply.runner import ensure_cover_letter
    from tengen_toppa.pipeline import steps
    from tengen_toppa.store.db import session

    with session() as s:
        job = steps.get_job(s, job_id)
        rv = steps.latest_resume(s, job_id)
        if rv is None:
            typer.echo("tailor the resume first: tengen tailor <id>")
            raise typer.Exit(1)
        rv.cover_letter_pdf = None
        rv = ensure_cover_letter(s, job, rv, steps.profile())
        typer.echo(rv.cover_letter_pdf or "cover letter rejected by checks; see logs")


@app.command()
def browser() -> None:
    """Open the dedicated Tengen Chrome profile (log in to LinkedIn/Naukri here once)."""
    from tengen_toppa.apply.browser import cdp_url, start_configured

    port = start_configured()
    typer.echo(f"Tengen Chrome running at {cdp_url(port)} (profile: {get_paths().browser_profile})")


@app.command("email-check")
def email_check() -> None:
    """Poll the inbox once for interview / rejection / assessment replies."""
    from tengen_toppa.store.db import session
    from tengen_toppa.tracking import poll

    with session() as s:
        typer.echo(f"updates: {poll(s)}")


@qa_app.command("list")
def qa_list() -> None:
    from tengen_toppa.store.db import session
    from tengen_toppa.store.models import QAItem

    with session() as s:
        for item in s.exec(select(QAItem)):
            typer.echo(f"[{item.id}] {item.question}\n     -> {item.answer}")


@qa_app.command("add")
def qa_add(question: str, answer: str) -> None:
    """Save an answer used for future application forms."""
    from tengen_toppa.qa import save_answer
    from tengen_toppa.store.db import session

    with session() as s:
        save_answer(s, question, answer)
    typer.echo("saved")


@qa_app.command("remove")
def qa_remove(item_id: int) -> None:
    from tengen_toppa.store.db import session
    from tengen_toppa.store.models import QAItem

    with session() as s:
        item = s.get(QAItem, item_id)
        if item:
            s.delete(item)
            s.commit()
    typer.echo("removed")


# ---------------- services ----------------


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the WhatsApp webhook server (expose with cloudflared)."""
    import uvicorn

    uvicorn.run("tengen_toppa.api:app", host=host, port=port, log_level="info")


@app.command()
def worker() -> None:
    """Run the background worker (1 thread => 1 Claude call at a time) + periodic schedules."""
    from tengen_toppa.pipeline.tasks import huey

    huey.create_consumer(workers=1, periodic=True).run()


@app.command("wa-verify")
def wa_verify() -> None:
    """Verify Meta setup: token valid + never expires + scopes, phone number ID, app secret, your number."""
    import hashlib
    import hmac

    import httpx

    from tengen_toppa.config import settings
    from tengen_toppa.secrets import Secret, get_secret

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        typer.echo(f"[{'OK' if passed else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))

    token = get_secret(Secret.WA_ACCESS_TOKEN)
    phone_id = get_secret(Secret.WA_PHONE_NUMBER_ID)
    secret = get_secret(Secret.WA_APP_SECRET)
    me = get_secret(Secret.WA_MY_NUMBER)
    missing = [
        s.value
        for s, v in [
            (Secret.WA_ACCESS_TOKEN, token),
            (Secret.WA_PHONE_NUMBER_ID, phone_id),
            (Secret.WA_APP_SECRET, secret),
            (Secret.WA_MY_NUMBER, me),
            (Secret.WA_VERIFY_TOKEN, get_secret(Secret.WA_VERIFY_TOKEN)),
        ]
        if not v
    ]
    if missing:
        typer.echo("Missing secrets: " + ", ".join(missing) + "\nRun: uv run tengen secrets set <name>")
        raise typer.Exit(1)
    assert token and phone_id and secret and me

    base = f"https://graph.facebook.com/{settings().whatsapp.graph_version}"
    proof = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()

    def get(path: str, **params: str) -> tuple[int, dict]:
        r = httpx.get(f"{base}/{path}", params={"access_token": token, **params}, timeout=20)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"error": {"message": r.text[:200]}}

    def err(body: dict) -> str:
        return str((body.get("error") or {}).get("message", body))[:200]

    code, body = get("me", fields="id,name")
    check("access token valid", code == 200, body.get("name", "") if code == 200 else err(body))
    if code != 200:
        raise typer.Exit(1)

    code, body = get("debug_token", input_token=token)
    app_id = (body.get("data") or {}).get("app_id") if code == 200 else None
    if app_id:
        r = httpx.get(f"{base}/{app_id}/subscriptions", params={"access_token": f"{app_id}|{secret}"}, timeout=20)
        subs = [x for x in r.json().get("data", []) if x.get("object") == "whatsapp_business_account"]
        if not subs:
            check("webhook configured", False, "WhatsApp → Configuration → Webhook → Edit")
        else:
            sub = subs[0]
            fields = {f.get("name") for f in sub.get("fields", [])}
            check("webhook configured", bool(sub.get("active")), sub.get("callback_url", ""))
            check(
                "webhook subscribed to 'messages'",
                "messages" in fields,
                "subscribed"
                if "messages" in fields
                else "WhatsApp → Configuration → Webhook fields → messages → Subscribe",
            )
    if code == 200:
        d = body.get("data", {})
        exp = d.get("expires_at", -1)
        check("token never expires", exp == 0, "never" if exp == 0 else f"expires_at={exp} (make a System User token)")
        scopes = set(d.get("scopes", []))
        need = {"whatsapp_business_messaging", "whatsapp_business_management"}
        check("token permissions", need <= scopes, ", ".join(sorted(need - scopes)) or "both present")
        check("token type", d.get("type") == "SYSTEM_USER", str(d.get("type")))
    else:
        typer.echo(f"[SKIP] token expiry/scopes check - {err(body)}")

    code, body = get("me", fields="id", appsecret_proof=proof)
    check("app secret matches this app", code == 200, "" if code == 200 else err(body))

    code, body = get(phone_id, fields="display_phone_number,verified_name,quality_rating")
    check(
        "phone number ID",
        code == 200,
        f"{body.get('display_phone_number')} ({body.get('verified_name')})"
        if code == 200
        else err(body) + " (wrong ID, or WhatsApp account not assigned to the system user)",
    )

    digits = "".join(c for c in me if c.isdigit())
    good = digits == me and 11 <= len(me) <= 15 and not me.startswith("0")
    check(
        "your number format (with country code)",
        good,
        me if good else f"got {me!r}; use digits only WITH country code, e.g. 919876543210",
    )
    raise typer.Exit(0 if ok else 1)


@app.command("tunnel-sync")
def tunnel_sync() -> None:
    """Point Meta's webhook at the running cloudflared quick tunnel (run after every cloudflared restart)."""
    import socket
    import subprocess as sp

    import httpx

    from tengen_toppa.config import settings
    from tengen_toppa.secrets import Secret, require_secret

    pids = sp.run(["pgrep", "-f", "cloudflared tunnel"], capture_output=True, text=True).stdout.split()
    if not pids:
        typer.echo("cloudflared isn't running. Start it: cloudflared tunnel --url http://localhost:8765")
        raise typer.Exit(1)
    host = ""
    for pid in pids:
        out = sp.run(["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
        for line in out.splitlines()[1:]:
            port = line.split()[8].rsplit(":", 1)[-1]
            try:
                ready = httpx.get(f"http://127.0.0.1:{port}/ready", timeout=3).status_code
                qt = httpx.get(f"http://127.0.0.1:{port}/quicktunnel", timeout=3).json()
            except (httpx.HTTPError, ValueError):
                continue
            if qt.get("hostname"):
                host = qt["hostname"]
                if ready != 200:
                    typer.echo(
                        f"cloudflared lost its connection ({host} is dead). Restart it: Ctrl+C in its window, "
                        "then `cloudflared tunnel --url http://localhost:8765`, then run this again."
                    )
                    raise typer.Exit(1)
    if not host:
        typer.echo("Couldn't read the tunnel address from cloudflared.")
        raise typer.Exit(1)
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        typer.echo(f"{host} doesn't resolve yet; wait 10 seconds and run this again.")
        raise typer.Exit(1) from None
    url = f"https://{host}/whatsapp/webhook"
    try:
        health = httpx.get(f"https://{host}/health", timeout=15).status_code
    except httpx.HTTPError:
        health = 0
    if health != 200:
        typer.echo(f"Tunnel is up but the server isn't answering ({health}). Is `uv run tengen serve` running?")
        raise typer.Exit(1)

    token = require_secret(Secret.WA_ACCESS_TOKEN)
    secret = require_secret(Secret.WA_APP_SECRET)
    graph = f"https://graph.facebook.com/{settings().whatsapp.graph_version}"
    app_id = httpx.get(f"{graph}/debug_token", params={"input_token": token, "access_token": token}, timeout=20).json()[
        "data"
    ]["app_id"]
    r = httpx.post(
        f"{graph}/{app_id}/subscriptions",
        timeout=60,
        data={
            "access_token": f"{app_id}|{secret}",
            "object": "whatsapp_business_account",
            "callback_url": url,
            "verify_token": require_secret(Secret.WA_VERIFY_TOKEN),
            "fields": "messages",
        },
    ).json()
    if not r.get("success"):
        typer.echo(f"Meta rejected the new URL: {r.get('error', {}).get('message', r)}")
        raise typer.Exit(1)
    typer.echo(f"Meta webhook now points to {url}\nSend 'help' on WhatsApp to test.")


@app.command("wa-subscribe")
def wa_subscribe(waba_id: str) -> None:
    """Subscribe your WhatsApp Business Account to the app so real messages reach the webhook."""
    import httpx

    from tengen_toppa.config import settings
    from tengen_toppa.secrets import Secret, require_secret

    token = require_secret(Secret.WA_ACCESS_TOKEN)
    pid = require_secret(Secret.WA_PHONE_NUMBER_ID)
    base = f"https://graph.facebook.com/{settings().whatsapp.graph_version}/{waba_id.strip()}"
    auth = {"access_token": token}

    nums = httpx.get(f"{base}/phone_numbers", params={**auth, "fields": "id,display_phone_number"}, timeout=20).json()
    if "error" in nums:
        typer.echo(
            f"Can't read that WhatsApp Business Account: {nums['error'].get('message')}\n"
            "Check the ID (WhatsApp → API Setup) and that the system user has it under Assign assets."
        )
        raise typer.Exit(1)
    if not any(n.get("id") == pid for n in nums.get("data", [])):
        typer.echo(
            "That account doesn't contain your saved Phone number ID. Copy the account ID shown on the same "
            "API Setup page as the test number."
        )
        raise typer.Exit(1)

    r = httpx.post(f"{base}/subscribed_apps", params=auth, timeout=20).json()
    if not r.get("success"):
        typer.echo(f"Subscribe failed: {r.get('error', {}).get('message', r)}")
        raise typer.Exit(1)
    subs = httpx.get(f"{base}/subscribed_apps", params=auth, timeout=20).json()
    names = [a.get("whatsapp_business_api_data", {}).get("name") for a in subs.get("data", [])]
    typer.echo(f"Subscribed. Apps receiving this account's messages: {names}")
    typer.echo("Now send 'help' to the test number on WhatsApp.")


@app.command("wa-test")
def wa_test(
    template: Annotated[bool, typer.Option(help="send Meta's default hello_world template")] = False,
) -> None:
    """Send a test WhatsApp message."""
    from tengen_toppa.store.db import session
    from tengen_toppa.whatsapp import notify

    client = notify.get_client()
    if client is None:
        typer.echo("WhatsApp secrets missing. See `tengen secrets list`.")
        raise typer.Exit(1)
    if template:
        typer.echo(client.send_template("hello_world", "en_US", []))
        return
    with session() as s:
        if not notify.window_open(s):
            typer.echo("24h window closed: message the bot from your phone first, or use --template.")
            raise typer.Exit(1)
        notify.send_text(s, "Tengen-Toppa test message. Send 'help' for commands.")
    typer.echo("sent")


@app.command()
def launchd(
    port: int = 8765,
    tunnel_name: Annotated[str | None, typer.Option(help="cloudflared named tunnel to keep alive")] = None,
) -> None:
    """Write launchd agents (api, worker, optional tunnel) and print install commands."""
    paths = get_paths()
    paths.launchd.mkdir(parents=True, exist_ok=True)
    tengen = shutil.which("tengen") or str(Path(sys.executable).parent / "tengen")
    path_env = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".local/bin")
    agents = {
        "com.tengentoppa.api": [tengen, "serve", "--port", str(port)],
        "com.tengentoppa.worker": ["/usr/bin/caffeinate", "-i", tengen, "worker"],
    }
    if tunnel_name:
        cloudflared = shutil.which("cloudflared") or "/opt/homebrew/bin/cloudflared"
        agents["com.tengentoppa.tunnel"] = [cloudflared, "tunnel", "run", tunnel_name]

    for label, argv in agents.items():
        args_xml = "\n".join(f"    <string>{a}</string>" for a in argv)
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path_env}</string>
    <key>TENGEN_HOME</key><string>{paths.home}</string>
  </dict>
  <key>WorkingDirectory</key><string>{paths.home}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{paths.logs}/{label}.out.log</string>
  <key>StandardErrorPath</key><string>{paths.logs}/{label}.err.log</string>
</dict>
</plist>
"""
        (paths.launchd / f"{label}.plist").write_text(plist)
        typer.echo(f"wrote {paths.launchd / f'{label}.plist'}")
    typer.echo("\nInstall:")
    for label in agents:
        typer.echo(
            f"  cp {paths.launchd}/{label}.plist ~/Library/LaunchAgents/ && "
            f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist"
        )
    typer.echo("Uninstall: launchctl bootout gui/$(id -u)/<label>")


if __name__ == "__main__":
    app()
