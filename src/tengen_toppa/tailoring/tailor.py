"""Tailor loop: Claude (Sonnet) proposes, guardrails dispose, violations are fed back for a retry."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from tengen_toppa.analysis import JobAnalysis
from tengen_toppa.llm.claude_client import ask
from tengen_toppa.profile import MasterProfile
from tengen_toppa.tailoring.guardrails import ValidationReport, validate
from tengen_toppa.tailoring.schema import TailoredResume

log = logging.getLogger(__name__)


@dataclass
class TailorOutcome:
    tailored: TailoredResume | None
    report: ValidationReport | None
    attempts: int
    durations_ms: list[int] = field(default_factory=list)
    costs: list[float | None] = field(default_factory=list)


def build_payload(
    master: MasterProfile,
    analysis: JobAnalysis,
    description: str,
    *,
    feedback: str | None,
    allow_project_reorder: bool,
    max_bullets_per_role: int,
    previous: TailoredResume | None = None,
    violations: list[str] | None = None,
) -> str:
    parts = [
        "<master_profile>\n" + master.model_dump_json(indent=1) + "\n</master_profile>",
        "<job_analysis>\n" + analysis.model_dump_json(indent=1) + "\n</job_analysis>",
        "<job_description>\n" + description[:12000] + "\n</job_description>",
        "<constraints>\n"
        + json.dumps(
            {
                "allow_project_reorder": allow_project_reorder,
                "max_bullets_per_role": max_bullets_per_role,
                "role_order": [e.id for e in master.experience],
            }
        )
        + "\n</constraints>",
    ]
    if feedback:
        parts.append(f"<user_feedback>\n{feedback}\n</user_feedback>")
    if previous is not None and violations:
        parts.append("<previous_attempt>\n" + previous.model_dump_json(indent=1) + "\n</previous_attempt>")
        parts.append(
            "<rejected_because>\n" + "\n".join(f"- {x}" for x in violations) + "\n</rejected_because>\n"
            "Fix every listed problem. Do not introduce new ones."
        )
    return "\n\n".join(parts)


def tailor(
    master: MasterProfile,
    analysis: JobAnalysis,
    description: str,
    *,
    model: str,
    timeout: int,
    max_attempts: int = 3,
    feedback: str | None = None,
    allow_project_reorder: bool = False,
    max_bullets_per_role: int = 5,
) -> TailorOutcome:
    jd_terms = analysis.must_have_skills + analysis.nice_to_have_skills
    outcome = TailorOutcome(tailored=None, report=None, attempts=0)
    previous: TailoredResume | None = None
    violations: list[str] | None = None

    for attempt in range(1, max_attempts + 1):
        outcome.attempts = attempt
        payload = build_payload(
            master,
            analysis,
            description,
            feedback=feedback,
            allow_project_reorder=allow_project_reorder,
            max_bullets_per_role=max_bullets_per_role,
            previous=previous,
            violations=violations,
        )
        res = ask(
            "Tailor the resume for this job following the system rules. Input is on stdin.",
            payload,
            TailoredResume,
            system_prompt="tailor_resume.md",
            model=model,
            timeout=timeout,
        )
        outcome.durations_ms.append(res.duration_ms)
        outcome.costs.append(res.est_cost_usd)
        report = validate(
            res.value,
            master,
            jd_terms=jd_terms,
            ats_keywords=analysis.ats_keywords,
            allow_project_reorder=allow_project_reorder,
            max_bullets_per_role=max_bullets_per_role,
        )
        outcome.tailored, outcome.report = res.value, report
        if report.ok:
            return outcome
        log.info("tailor attempt %d rejected: %s", attempt, report.violations)
        previous, violations = res.value, report.violations
    return outcome
