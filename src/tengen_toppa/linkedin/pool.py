"""The standing pool of eligible Easy Apply jobs: card filters, dedupe, ranking, cap accounting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, func, select

from tengen_toppa.config import Settings
from tengen_toppa.discovery.base import JobPosting
from tengen_toppa.filtering import _contains_any, prefilter_scores
from tengen_toppa.linkedin.search import Card
from tengen_toppa.profile import MasterProfile
from tengen_toppa.store.models import LiApplication, LiJob, LiState, utcnow

SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|manager|director|head|architect|vp|vice president|chief|expert|"
    r"specialist ii|iii|iv)\b",
    re.I,
)
COUNTS_TOWARDS_CAP = ("submitting", "applied")


def fingerprint(company: str, title: str) -> str:
    """Company + title without location or requisition codes: the same role posted twice is one job."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|\b[A-Z]{1,4}/[\w/-]+\b|-\s*\d{3,}\b", " ", title)
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()  # noqa: E731
    return f"{norm(company)}|{norm(t)}"


def card_reason(card: Card, cfg: Settings) -> str | None:
    """Why a card is ineligible, or None. Only free, local checks."""
    if card.applied:  # applied cards lose their "Easy Apply" label, so check this first
        return "already applied"
    if not card.easy_apply:
        return "not Easy Apply"
    if _contains_any(card.company, cfg.search.company_blacklist):
        return f"blacklisted company {card.company}"
    if bad := _contains_any(card.title, cfg.search.title_exclude):
        return f"title excluded by '{bad}'"
    if m := SENIOR_RE.search(card.title):
        return f"senior title ('{m.group(0)}')"
    if not title_relevant(card.title, cfg):
        return "title not relevant"
    if not _location_preferred(card.location, cfg):
        return "outside preferred locations"
    return None


def _location_preferred(location: str, cfg: Settings) -> bool:
    """Search locations act as a filter unless they include 'Worldwide'."""
    specs = [x.strip() for x in cfg.linkedin.locations if x.strip()]
    if not specs or any(x.lower() == "worldwide" for x in specs):
        return True
    hay = location.lower()
    return any(part.strip().lower() in hay for spec in specs for part in spec.split(",") if part.strip())


# Built-in relevance vocabulary, on top of [search] title_include and [linkedin] keywords. Matched both as
# whole words and inside compound words ("GenAI", "AI/ML", "MLOps"), which plain word matching missed.
RELEVANT_WORDS = [
    "ai",
    "ml",
    "llm",
    "llms",
    "nlp",
    "genai",
    "gen ai",
    "generative",
    "machine learning",
    "deep learning",
    "data scientist",
    "data science",
    "computer vision",
    "agentic",
    "agent",
    "agents",
    "prompt",
    "langchain",
    "rag",
    "mlops",
    "llmops",
    "artificial intelligence",
    "applied scientist",
    "python",
]
# Compounds that whole-word matching can't see. "AI-Native" and "AI/ML" already match "ai" as a word.
_COMPOUND_RE = re.compile(r"(?i)gen\s?ai\b|\baiml\b|\bmlops\b|\bllmops\b|\bllms?\b")


def title_relevant(title: str, cfg: Settings) -> bool:
    words = list(cfg.search.title_include) + list(cfg.linkedin.keywords) + RELEVANT_WORDS
    return _contains_any(title, words) is not None or bool(_COMPOUND_RE.search(title))


def refilter(s: Session, cfg: Settings) -> int:
    """Re-check jobs rejected only for their title (after the filter improves). Returns how many re-entered."""
    n = 0
    for job in s.exec(
        select(LiJob).where(LiJob.state == LiState.INELIGIBLE, LiJob.skip_reason == "title not relevant")
    ):
        card = Card(job.job_id, job.title, job.company, job.location, job.workplace, job.easy_apply, False, ())
        if card_reason(card, cfg) is None and duplicate_reason(s, job.fingerprint, cfg.linkedin.dedupe_days) is None:
            job.state, job.skip_reason = LiState.POOLED, None
            s.add(job)
            n += 1
    s.commit()
    return n


@dataclass
class IngestResult:
    seen: int = 0
    new: int = 0
    eligible_new: int = 0


def ingest_cards(s: Session, cards: list[Card], cfg: Settings, *, search: str) -> IngestResult:
    res = IngestResult()
    now = utcnow()
    for c in cards:
        res.seen += 1
        job = s.get(LiJob, c.job_id)
        if job is not None:
            job.last_seen = now
            if c.applied and job.state not in (LiState.APPLIED, LiState.SUBMITTING):
                job.state, job.skip_reason = LiState.APPLIED, "applied outside Tengen"
            s.add(job)
            continue
        res.new += 1
        reason = card_reason(c, cfg)
        fp = fingerprint(c.company, c.title)
        if reason is None:
            reason = duplicate_reason(s, fp, cfg.linkedin.dedupe_days)
        state = LiState.INELIGIBLE if reason else LiState.POOLED
        if c.applied:
            state = LiState.APPLIED
        s.add(
            LiJob(
                job_id=c.job_id,
                title=c.title,
                company=c.company,
                location=c.location,
                workplace=c.workplace,
                url=f"https://www.linkedin.com/jobs/view/{c.job_id}/",
                easy_apply=c.easy_apply,
                fingerprint=fp,
                state=state,
                skip_reason=reason,
                search=search,
            )
        )
        if state == LiState.POOLED:
            res.eligible_new += 1
    s.commit()
    return res


def duplicate_reason(s: Session, fp: str, days: int) -> str | None:
    since = utcnow() - timedelta(days=days)
    q = select(LiJob).where(
        LiJob.fingerprint == fp,
        LiJob.state.in_(
            (
                LiState.POOLED,
                LiState.QUEUED,
                LiState.SUBMITTING,
                LiState.APPLIED,  # type: ignore[attr-defined]
                LiState.WAITING_ANSWER,
            )
        ),
        LiJob.first_seen >= since,
    )
    other = s.exec(q).first()
    return f"duplicate of {other.job_id} ({other.title} @ {other.company})" if other else None


def pool_size(s: Session) -> int:
    return s.exec(select(func.count()).select_from(LiJob).where(LiJob.state == LiState.POOLED)).one()


def rank_pool(s: Session, profile: MasterProfile) -> int:
    jobs = list(s.exec(select(LiJob).where(LiJob.state == LiState.POOLED)))
    if not jobs:
        return 0
    postings = [
        JobPosting(
            "linkedin",
            j.job_id,
            j.company,
            j.title,
            j.location,
            j.workplace == "remote",
            j.url,
            j.url,
            j.description or j.title,
        )
        for j in jobs
    ]
    for j, score in zip(jobs, prefilter_scores(postings, profile), strict=True):
        j.score = round(score, 4)
        s.add(j)
    s.commit()
    return len(jobs)


def next_candidates(s: Session, limit: int = 10) -> list[LiJob]:
    q = (
        select(LiJob)
        .where(LiJob.state == LiState.POOLED)
        .order_by(LiJob.score.desc().nullslast(), LiJob.first_seen.desc())
        .limit(limit)
    )  # type: ignore[union-attr]
    return list(s.exec(q))


def expire_stale(s: Session, days: int = 21) -> int:
    since = utcnow() - timedelta(days=days)
    stale = list(s.exec(select(LiJob).where(LiJob.state == LiState.POOLED, LiJob.last_seen < since)))
    for j in stale:
        j.state, j.skip_reason = LiState.CLOSED, f"not seen in {days} days"
        s.add(j)
    s.commit()
    return len(stale)


# ---------- cap accounting (counted at the moment of submit) ----------


def local_midnight_utc(now: datetime | None = None) -> datetime:
    local = (now or datetime.now(UTC)).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def applied_since(s: Session, since: datetime) -> int:
    q = (
        select(func.count())
        .select_from(LiApplication)
        .where(
            LiApplication.outcome.in_(COUNTS_TOWARDS_CAP),  # type: ignore[attr-defined]
            LiApplication.created_at >= since,
        )
    )
    return s.exec(q).one()


def applied_today(s: Session) -> int:
    return applied_since(s, local_midnight_utc())


def applied_last_hour(s: Session) -> int:
    return applied_since(s, utcnow() - timedelta(hours=1))


def daily_cap(s: Session) -> int:
    """Settings value, lowered (never raised) by WhatsApp `cap N`."""
    from tengen_toppa.config import settings
    from tengen_toppa.store.db import kv_get

    hard = settings().linkedin.max_applications_per_day
    override = kv_get(s, "li_daily_cap")
    return min(hard, int(override)) if override is not None else hard
