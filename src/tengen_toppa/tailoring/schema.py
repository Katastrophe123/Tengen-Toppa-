"""What Claude is allowed to output when tailoring. Note what's ABSENT: company names, titles, dates,
project text. Those are always copied from the master profile, so the model structurally cannot alter them."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TailoredBullet(BaseModel):
    source_id: str = Field(description="id of the master bullet this was rephrased from")
    text: str


class TailoredRole(BaseModel):
    experience_id: str
    bullets: list[TailoredBullet]


class TailoredResume(BaseModel):
    summary: str = Field(description="2-3 sentences, facts only from the master profile")
    skills: list[str] = Field(description="Ordered subset of skills_verified, most relevant first")
    experience: list[TailoredRole] = Field(description="Every role from the master profile, same order")
    project_order: list[str] = Field(description="Project ids to include, in display order")
    changes: list[str] = Field(description="Max 5 short notes on what you changed and why")
