"""SQLite tables (SQLModel)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    FILTERED_OUT = "filtered_out"  # failed free hard filters / prefilter
    ANALYZED = "analyzed"
    SKIPPED = "skipped"  # low score or post-analysis filter, or user tapped Skip
    SHORTLISTED = "shortlisted"
    TAILORED = "tailored"
    PENDING_REVIEW = "pending_review"  # card sent (or queued) to WhatsApp
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"


class Job(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    external_id: str
    company: str = Field(index=True)
    title: str
    location: str = ""
    remote: bool | None = None
    url: str
    apply_url: str
    description: str = Field(default="", sa_column=Column(Text))
    fingerprint: str = Field(index=True)
    status: JobStatus = Field(default=JobStatus.DISCOVERED, index=True)
    status_reason: str | None = None
    prefilter_score: float | None = None
    score: float | None = None
    analysis: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ResumeVersion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True, foreign_key="job.id")
    version: int = 1
    feedback: str | None = None
    tailored: dict[str, Any] = Field(sa_column=Column(JSON))
    validation: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    pdf_path: str | None = None
    cover_letter_text: str | None = Field(default=None, sa_column=Column(Text))
    cover_letter_pdf: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ApplyAttempt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(index=True, foreign_key="job.id")
    method: str  # adapter name | claude_agent | assist
    outcome: str  # filled | submitted | needs_answers | captcha | failed | assisted
    report: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    screenshot: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ProcessedEmail(SQLModel, table=True):
    message_id: str = Field(primary_key=True)
    job_id: int | None = Field(default=None, index=True)
    category: str
    subject: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class LLMCall(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    step: str = Field(index=True)
    model: str
    ok: bool
    duration_ms: int
    est_cost_usd: float | None = None
    error: str | None = None
    job_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class WAMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    wa_id: str | None = Field(default=None, unique=True)
    direction: str  # "in" | "out"
    kind: str  # text | interactive | document | template | button
    body: str = Field(default="", sa_column=Column(Text))
    job_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class QAItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    question: str = Field(sa_column=Column(Text))
    answer: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class KV(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: Any = Field(default=None, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, index=True)
    type: str = Field(index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
