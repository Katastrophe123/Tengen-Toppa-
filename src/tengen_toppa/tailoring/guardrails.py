"""Code-enforced honesty. The prompt asks Claude not to fabricate; this module makes sure of it.

A tailored resume is rejected if it:
- lists a skill not in skills_verified
- drops, adds, duplicates, or reorders roles
- uses a bullet that doesn't trace to a master bullet of the same role
- introduces a number (metric, %, count) not present in the source bullet
- introduces a technology term not present in the source bullet
- drops a locked project, references an unknown project, or reorders projects when not allowed
- puts unverified tech or invented numbers in the summary
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from tengen_toppa.profile import MasterProfile
from tengen_toppa.tailoring.schema import TailoredResume

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


class ValidationReport(BaseModel):
    ok: bool
    violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    keyword_coverage: float | None = None
    missing_keywords: list[str] = Field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM_RE.findall(text)}


def has_term(text: str, term: str) -> bool:
    term = term.strip()
    if not term:
        return False
    if len(term) <= 2:  # "Go", "R", "C": case-sensitive, else "go live" would match Go
        return re.search(rf"(?<![\w+#.-]){re.escape(term)}(?![\w+#-])", text) is not None
    pattern = rf"(?<![\w+#]){re.escape(term.lower())}(?![\w+#])"
    return re.search(pattern, text.lower()) is not None


def terms_in(text: str, vocab: Iterable[str]) -> set[str]:
    return {t for t in vocab if has_term(text, t)}


def validate(
    tailored: TailoredResume,
    master: MasterProfile,
    *,
    jd_terms: Iterable[str] = (),
    ats_keywords: Iterable[str] = (),
    allow_project_reorder: bool = False,
    max_bullets_per_role: int = 5,
) -> ValidationReport:
    v: list[str] = []
    w: list[str] = []
    verified = {_norm(s): s for s in master.skills_verified}

    # Tech vocabulary: anything that looks like a skill claim
    vocab = set(master.skills_verified) | {t for p in master.projects for t in p.tech} | set(jd_terms)
    vocab = {t for t in vocab if t.strip()}

    # 1. skills
    seen: set[str] = set()
    for s in tailored.skills:
        n = _norm(s)
        if n not in verified:
            v.append(f"skill '{s}' is not in skills_verified")
        if n in seen:
            w.append(f"duplicate skill '{s}'")
        seen.add(n)

    # 2. roles: exact same set and order as master
    master_ids = [e.id for e in master.experience]
    got_ids = [r.experience_id for r in tailored.experience]
    if got_ids != master_ids:
        v.append(f"experience must contain every role in master order {master_ids}, got {got_ids}")

    # 3. bullets
    for role in tailored.experience:
        exp = master.experience_by_id(role.experience_id)
        if exp is None:
            continue
        sources = {b.id: b for b in exp.bullets}
        if not role.bullets:
            v.append(f"role '{exp.id}' has no bullets")
        if len(role.bullets) > max_bullets_per_role:
            v.append(f"role '{exp.id}' has {len(role.bullets)} bullets (max {max_bullets_per_role})")
        used: set[str] = set()
        for b in role.bullets:
            src = sources.get(b.source_id)
            if src is None:
                v.append(f"bullet source_id '{b.source_id}' does not belong to role '{exp.id}'")
                continue
            if b.source_id in used:
                v.append(f"bullet '{b.source_id}' used twice")
            used.add(b.source_id)
            if extra := numbers(b.text) - numbers(src.text):
                v.append(f"bullet '{b.source_id}' introduces numbers {sorted(extra)} not in the source")
            if new_terms := terms_in(b.text, vocab) - terms_in(src.text, vocab):
                v.append(f"bullet '{b.source_id}' introduces tech {sorted(new_terms)} not in the source bullet")
            if len(b.text) > len(src.text) * 1.6 + 40:
                v.append(f"bullet '{b.source_id}' is padded (much longer than the source)")

    # 4. projects
    proj_ids = [p.id for p in master.projects]
    unknown = [p for p in tailored.project_order if p not in proj_ids]
    if unknown:
        v.append(f"unknown project ids {unknown}")
    if len(set(tailored.project_order)) != len(tailored.project_order):
        v.append("duplicate project ids in project_order")
    missing_locked = [p.id for p in master.projects if p.locked and p.id not in tailored.project_order]
    if missing_locked:
        v.append(f"locked projects must be included: {missing_locked}")
    if not allow_project_reorder:
        expected = [p for p in proj_ids if p in tailored.project_order]
        if [p for p in tailored.project_order if p in proj_ids] != expected:
            v.append("projects were reordered but allow_project_reorder is false")

    # 5. summary
    master_text = master.model_dump_json()
    master_nums = numbers(master_text) | numbers(str(master.basics.years_experience))
    if extra := numbers(tailored.summary) - master_nums:
        v.append(f"summary introduces numbers {sorted(extra)} not found in the master profile")
    unverified_in_summary = {t for t in terms_in(tailored.summary, vocab) if _norm(t) not in verified}
    unverified_in_summary = {t for t in unverified_in_summary if not has_term(master.summary, t)}
    if unverified_in_summary:
        v.append(f"summary mentions unverified tech {sorted(unverified_in_summary)}")
    if not tailored.summary.strip():
        w.append("empty summary")

    # ATS keyword coverage (informational)
    kws = [k for k in ats_keywords if k.strip()]
    coverage, missing = None, []
    if kws:
        full = " ".join(
            [master.basics.headline, tailored.summary, " ".join(tailored.skills)]
            + [b.text for r in tailored.experience for b in r.bullets]
            + [" ".join(p.bullets + p.tech) for p in master.projects if p.id in tailored.project_order]
        )
        missing = [k for k in kws if not has_term(full, k)]
        coverage = round(1 - len(missing) / len(kws), 2)

    return ValidationReport(ok=not v, violations=v, warnings=w, keyword_coverage=coverage, missing_keywords=missing)
