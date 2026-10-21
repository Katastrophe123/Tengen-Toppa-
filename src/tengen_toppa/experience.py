"""Experience questions ("How many years with Python?", "Do you have 3+ years of X?") answered from
YOUR stated experience only: [experience] settings + skills_verified. Never from the Q&A bank by
similarity, never from an LLM. A skill we have no stated years for -> None (ask you once)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from tengen_toppa.apply.fields import normalize_question
from tengen_toppa.config import ExperienceSettings
from tengen_toppa.profile import MasterProfile
from tengen_toppa.tailoring.guardrails import has_term

_EXP_RE = re.compile(
    r"\b(years?|yrs?)\b.*\b(experience|worked|working|using|with|in)\b|\bexperience (with|in|using|of)\b"
    r"|\b(hands on|hands-on|familiar(ity)? with|proficien\w* (in|with)|worked with|knowledge of)\b"
    r"|\bhow many years\b|\byears of (professional |relevant |total |work |industry )?experience\b",
    re.I,
)
_THRESHOLD_RE = re.compile(
    r"(?:at least|minimum(?: of)?|min\.?|more than|over)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)"
    r"|(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)|(\d+(?:\.\d+)?)\s*(?:or more|and above)\s*(?:years?|yrs?)?"
    r"|(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:or more|and above|\+)",
    re.I,
)
_TECH_AFTER_RE = re.compile(r"\b(?:with|in|using|on)\s+([a-z0-9+#./ -]{2,40}?)(?:\?|$|,| and | experience)", re.I)
_NOT_TECH_RE = re.compile(r"\b(do|you|have|your|our|this|company|role|team|field|industry|position|years?)\b", re.I)
_GENERIC_TARGETS = {
    "experience",
    "professional experience",
    "work experience",
    "total experience",
    "the industry",
    "industry",
    "this role",
    "this field",
    "a similar role",
    "similar roles",
    "software development",
    "relevant experience",
    "years",
    "the field",
}


@dataclass(frozen=True)
class ExpAnswer:
    applies: bool  # this is an experience question; bank-by-similarity and LLM must not answer it
    answer: str | None  # None + applies => ask the user
    skill: str | None = None


def is_experience_question(label: str) -> bool:
    return bool(_EXP_RE.search(normalize_question(label)) or _EXP_RE.search(label))


def _fmt(years: float) -> str:
    return f"{years:g}"


def skill_years(skill: str, profile: MasterProfile, exp: ExperienceSettings) -> float | None:
    for name, yrs in exp.skills.items():
        if name.lower() == skill.lower():
            return yrs
    if any(s.lower() == skill.lower() for s in profile.skills_verified):
        return exp.verified_skill_years
    if any(t.lower() == skill.lower() for t in exp.ai_terms):
        return exp.ai_skill_years
    return None


def mentioned_skills(label: str, profile: MasterProfile, exp: ExperienceSettings) -> list[str]:
    vocab = set(exp.skills) | set(profile.skills_verified) | set(exp.ai_terms)
    vocab |= {t for p in profile.projects for t in p.tech}
    hits = [t for t in vocab if has_term(label, t)]
    # prefer the most specific term ("LangGraph state machines" over "LangGraph", "Hugging Face Transformers"...)
    hits.sort(key=len, reverse=True)
    out: list[str] = []
    for h in hits:
        if not any(h.lower() in o.lower() for o in out):
            out.append(h)
    return out


def unknown_target(label: str, known: list[str]) -> str | None:
    """A 'with X' target that isn't one of the known skills, e.g. 'Kubernetes'."""
    m = _TECH_AFTER_RE.search(label.strip())
    if not m:
        return None
    target = m.group(1).strip(" .").lower()
    target = re.sub(r"^(the|a|an)\s+", "", target)
    if not target or target in _GENERIC_TARGETS or any(k.lower() in target for k in known):
        return None
    if _NOT_TECH_RE.search(target):
        return None
    if re.fullmatch(r"(professional|relevant|total|work|industry|overall)( experience)?", target):
        return None
    return target


def threshold(label: str) -> float | None:
    m = _THRESHOLD_RE.search(label)
    if not m:
        return None
    return float(next(g for g in m.groups() if g))


def pick_years_option(options: list[str], years: float) -> str | None:
    """Pick '1-3 years' / '0 - 1' / 'Less than 2 years' / '5+' style options containing `years`."""
    best = None
    for o in options:
        n = o.lower().replace("–", "-")
        if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", n):
            lo, hi = float(m.group(1)), float(m.group(2))
            if lo <= years <= hi:
                return o
        elif m := re.search(r"(?:less than|under|<)\s*(\d+(?:\.\d+)?)", n):
            if years < float(m.group(1)):
                best = best or o
        elif m := re.search(r"(\d+(?:\.\d+)?)\s*\+|(\d+(?:\.\d+)?)\s*(?:or more|and above)", n):
            if years >= float(m.group(1) or m.group(2)):
                best = best or o
        elif m := re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\s*", n):
            if float(m.group(1)) == years:
                return o
    return best


def experience_answer(
    label: str, *, kind: str, options: list[str], profile: MasterProfile, exp: ExperienceSettings
) -> ExpAnswer:
    if not is_experience_question(label):
        return ExpAnswer(False, None)
    skills = mentioned_skills(label, profile, exp)
    unknown = unknown_target(label, skills)
    if unknown and not skills:
        return ExpAnswer(True, None, unknown)  # e.g. Kubernetes: you haven't stated years -> ask
    if skills:
        yrs_list = [skill_years(s, profile, exp) for s in skills]
        if any(y is None for y in yrs_list):
            return ExpAnswer(True, None, skills[0])
        years = min(y for y in yrs_list if y is not None)  # "Python and AWS" -> the weaker one
        skill = skills[0]
    else:
        years, skill = exp.total_years, None

    yes_no = {o.strip().lower() for o in options} >= {"yes", "no"} or kind in ("checkbox",)
    need = threshold(label)
    if yes_no or (need is not None and kind in ("radio", "select") and not _numeric_options(options)):
        flag = years >= need if need is not None else years > 0
        value = "Yes" if flag else "No"
        if options:
            value = next((o for o in options if o.strip().lower() == value.lower()), value)
        return ExpAnswer(True, value, skill)
    if options:
        return ExpAnswer(True, pick_years_option(options, years), skill)
    return ExpAnswer(True, _fmt(years), skill)


def _numeric_options(options: list[str]) -> bool:
    return any(re.search(r"\d", o) for o in options)


# ---------- job requirement parsing ----------

_REQ_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*\+?\s*(?:years?|yrs?)"
    r"(?:\s*(?:of|’|')?\s*(?:\w+\s+){0,4}?(?:experience|exp\b))",
    re.I,
)
_REQ_RE2 = re.compile(
    r"(?:experience|exp)\s*(?:of|:|-)?\s*(?:at least|minimum|min\.?)?\s*(\d+(?:\.\d+)?)\s*(?:\+|plus)?\s*"
    r"(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*\+?\s*(?:years?|yrs?)",
    re.I,
)
_FRESHER_RE = re.compile(r"\b(freshers?|0\s*-\s*1\s*(?:years?|yrs?)|entry[- ]level|no experience required)\b", re.I)


def years_required(text: str) -> float | None:
    """Minimum years of experience a JD asks for. Uses the smallest lower bound stated, so
    '0-2 years' -> 0, '1+ years' -> 1, '5+ years of experience' -> 5. None if not stated."""
    lows: list[float] = []
    for rx in (_REQ_RE, _REQ_RE2):
        for m in rx.finditer(text):
            lo = float(m.group(1))
            if 0 <= lo <= 30:
                lows.append(lo)
    if lows:
        return min(lows)
    if _FRESHER_RE.search(text):
        return 0
    return None
