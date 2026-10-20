"""JD analysis + fit scoring in ONE cheap Claude call (Haiku), then deterministic post-filters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from tengen_toppa.config import SearchSettings
from tengen_toppa.filtering import skill_coverage
from tengen_toppa.llm.claude_client import CallResult, ask
from tengen_toppa.profile import MasterProfile


class JobAnalysis(BaseModel):
    title: str
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "principal", "unknown"]
    years_required: int | None = Field(description="Minimum years of experience required, null if not stated")
    must_have_skills: list[str]
    nice_to_have_skills: list[str]
    responsibilities: list[str] = Field(description="Max 6 short items")
    ats_keywords: list[str] = Field(description="Exact phrases from the JD an ATS would match on, max 20")
    domain: str
    remote: Literal["remote", "hybrid", "onsite", "unknown"]
    visa_sponsorship: bool | None
    salary_range: str | None
    red_flags: list[str]
    fit_score: int = Field(ge=0, le=100, description="How well the CANDIDATE fits this job, 0-100")
    matched_strengths: list[str] = Field(description="Max 4, candidate strengths relevant to this JD")
    gaps: list[str] = Field(description="Max 4, requirements the candidate does not show")
    rationale: str = Field(description="One or two sentences")


def analyze_job(
    *,
    title: str,
    company: str,
    location: str,
    description: str,
    profile: MasterProfile,
    model: str,
    timeout: int,
) -> CallResult[JobAnalysis]:
    payload = (
        f"<candidate>\n{profile.compact_for_llm()}\n</candidate>\n\n"
        f"<job>\nTitle: {title}\nCompany: {company}\nLocation: {location}\n\n{description[:15000]}\n</job>"
    )
    return ask(
        "Analyze the job in <job> and score the fit of the candidate in <candidate>. Input is on stdin.",
        payload,
        JobAnalysis,
        system_prompt="analyze_job.md",
        model=model,
        timeout=timeout,
    )


def final_score(analysis: JobAnalysis, profile: MasterProfile, description: str) -> float:
    """Blend LLM judgement with a deterministic must-have coverage signal."""
    must = analysis.must_have_skills
    verified = {s.lower() for s in profile.skills_verified}
    if must:
        covered = sum(1 for s in must if s.lower() in verified or skill_coverage(s, profile.skills_verified) > 0)
        coverage = covered / len(must)
    else:
        coverage = skill_coverage(description, profile.skills_verified)
    return round(0.65 * analysis.fit_score + 0.35 * coverage * 100, 1)


def post_filter(analysis: JobAnalysis, profile: MasterProfile, cfg: SearchSettings) -> str | None:
    """Return a skip reason, or None if the job passes."""
    yrs = analysis.years_required
    if yrs is not None and yrs > profile.basics.years_experience + cfg.max_years_gap:
        return f"needs {yrs}+ yrs (you have {profile.basics.years_experience})"
    if cfg.needs_visa_sponsorship and analysis.visa_sponsorship is False:
        return "no visa sponsorship"
    if analysis.seniority in ("principal",) and profile.basics.years_experience < 8:
        return "principal level"
    return None
