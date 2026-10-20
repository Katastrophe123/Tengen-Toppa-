"""Answers to application-form questions. Order of trust:

1. deterministic rules from the master profile + [applicant] settings (name, email, visa, notice...)
2. your Q&A bank (answers you gave before over WhatsApp / CLI)
3. Claude Haiku, which may only answer from (1) and (2) or write a grounded free-text answer for
   open-ended textarea questions ("Why do you want to join?")
Anything still unknown and required is asked on WhatsApp and saved to the bank.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from tengen_toppa.apply.fields import FormField, normalize_question, pick_option
from tengen_toppa.config import ApplicantSettings
from tengen_toppa.llm.claude_client import ask
from tengen_toppa.profile import MasterProfile
from tengen_toppa.store.models import QAItem

YES, NO = "Yes", "No"


def _yn(flag: bool) -> str:
    return YES if flag else NO


def _link(profile: MasterProfile, *names: str) -> str:
    for link in profile.basics.links:
        if any(n in (link.label + " " + link.url).lower() for n in names):
            return link.url
    return ""


def _name_parts(profile: MasterProfile) -> tuple[str, str]:
    parts = profile.basics.name.split()
    return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (profile.basics.name, "")


Rule = tuple[str, Callable[[MasterProfile, ApplicantSettings], str]]

# Order matters: more specific patterns first. Patterns match the normalized label.
RULES: list[Rule] = [
    (r"\bfirst name\b|\bgiven name\b", lambda p, a: _name_parts(p)[0]),
    (r"\blast name\b|\bsurname\b|\bfamily name\b", lambda p, a: _name_parts(p)[1]),
    (r"\bpreferred (first )?name\b", lambda p, a: _name_parts(p)[0]),
    (r"^(full )?name$|^your name$|\blegal name\b", lambda p, a: p.basics.name),
    (r"\bemail\b", lambda p, a: p.basics.email),
    (r"\bphone\b|\bmobile\b|\bcontact number\b", lambda p, a: p.basics.phone),
    (r"\blinkedin\b", lambda p, a: _link(p, "linkedin")),
    (r"\bgithub\b", lambda p, a: _link(p, "github")),
    (
        r"\bportfolio\b|\bpersonal (website|site)\b|^website$|\bother website\b",
        lambda p, a: _link(p, "portfolio", "website"),
    ),
    (
        r"\bcurrent (company|employer|organi[sz]ation)\b|^company$|^org$",
        lambda p, a: p.experience[0].company if p.experience else "",
    ),
    (
        r"\bcurrent (job )?title\b|\bcurrent role\b|\bcurrent designation\b",
        lambda p, a: p.experience[0].role if p.experience else "",
    ),
    (
        r"\byears of (professional |relevant |total )?experience\b|\bhow many years\b",
        lambda p, a: f"{p.basics.years_experience:g}",
    ),
    (r"\bsponsor", lambda p, a: _yn(a.requires_sponsorship)),
    (
        r"\b(legally )?(authori[sz]ed|eligible|permitted)\b.*\bwork\b|\bwork (permit|authori[sz]ation)\b",
        lambda p, a: _yn(a.work_authorized),
    ),
    (r"\breloca", lambda p, a: _yn(a.willing_to_relocate)),
    (
        r"\b(open to|willing to work|comfortable (with )?working) (fully )?remote|\bremote work\b",
        lambda p, a: _yn(a.open_to_remote),
    ),
    (r"\bnotice period\b", lambda p, a: a.notice_period),
    (
        r"\b(earliest )?start date\b|\bwhen can you (start|join)\b|\bavailab(le|ility) to (start|join)\b",
        lambda p, a: a.earliest_start,
    ),
    (r"\bcurrent (ctc|salary|compensation)\b", lambda p, a: a.current_ctc),
    (
        r"\bexpected (ctc|salary|compensation)\b|\bsalary expectation|\bdesired (salary|compensation)\b",
        lambda p, a: a.expected_ctc,
    ),
    (r"\bhow did you (hear|find|learn)\b|\breferral source\b|^source$", lambda p, a: a.how_did_you_hear),
    (r"\bpronoun", lambda p, a: a.pronouns),
    (r"\bgender\b|\bsex\b", lambda p, a: a.gender),
    (r"\brace\b|\bethnic|\bhispanic\b|\blatino\b", lambda p, a: a.race_ethnicity),
    (r"\bveteran\b", lambda p, a: a.veteran_status),
    (r"\bdisabilit", lambda p, a: a.disability_status),
    (r"\bcountry\b", lambda p, a: a.country),
    (r"\b(current )?(location|city)\b|\bwhere are you (based|located)\b", lambda p, a: p.basics.location),
]


def rule_answer(label: str, profile: MasterProfile, applicant: ApplicantSettings) -> str | None:
    norm = normalize_question(label)
    for pattern, fn in RULES:
        if re.search(pattern, norm):
            value = fn(profile, applicant)
            return value or None
    return None


def bank_answer(s: Session, label: str, threshold: float = 0.86) -> str | None:
    norm = normalize_question(label)
    best, best_score = None, 0.0
    for item in s.exec(select(QAItem)):
        score = difflib.SequenceMatcher(None, norm, normalize_question(item.question)).ratio()
        if score > best_score:
            best, best_score = item.answer, score
    return best if best_score >= threshold else None


def save_answer(s: Session, question: str, answer: str) -> None:
    norm = normalize_question(question)
    for item in s.exec(select(QAItem)):
        if normalize_question(item.question) == norm:
            item.answer = answer
            s.add(item)
            s.commit()
            return
    s.add(QAItem(question=question.strip(), answer=answer.strip()))
    s.commit()


# ---------- grounded LLM answers ----------


class LLMAnswer(BaseModel):
    key: str
    answer: str = Field(description="Empty string if unknown")
    source: Literal["profile", "applicant_facts", "qa_bank", "grounded_writing", "unknown"]


class LLMAnswers(BaseModel):
    answers: list[LLMAnswer]


def llm_answers(
    fields: list[FormField],
    *,
    profile: MasterProfile,
    applicant: ApplicantSettings,
    bank: list[QAItem],
    job_context: str,
    model: str,
    timeout: int,
) -> dict[str, str]:
    if not fields:
        return {}
    qs = "\n".join(
        f"- key={f.key} | kind={f.kind} | required={f.required} | question={f.label!r}"
        + (f" | options={f.options}" if f.options else "")
        for f in fields
    )
    payload = (
        f"<profile>\n{profile.model_dump_json()}\n</profile>\n"
        f"<applicant_facts>\n{applicant.model_dump_json()}\n</applicant_facts>\n"
        f"<qa_bank>\n" + "\n".join(f"Q: {q.question}\nA: {q.answer}" for q in bank) + "\n</qa_bank>\n"
        f"<job>\n{job_context[:6000]}\n</job>\n<questions>\n{qs}\n</questions>"
    )
    res = ask(
        "Answer the application questions following the rules. Input on stdin.",
        payload,
        LLMAnswers,
        system_prompt="answer_questions.md",
        model=model,
        timeout=timeout,
    )
    by_key = {f.key: f for f in fields}
    out: dict[str, str] = {}
    for a in res.value.answers:
        f = by_key.get(a.key)
        if f is None or a.source == "unknown" or not a.answer.strip():
            continue
        if a.source == "grounded_writing" and f.kind != "textarea":
            continue  # free writing only for open-ended textareas
        if f.options:
            picked = pick_option(f.options, a.answer)
            if picked is None:
                continue
            out[f.key] = picked
        else:
            out[f.key] = a.answer.strip()
    return out


# ---------- resolve a whole form ----------


@dataclass
class Resolution:
    answers: dict[str, str] = field(default_factory=dict)  # key -> value
    sources: dict[str, str] = field(default_factory=dict)  # key -> rule|bank|llm
    missing_required: list[FormField] = field(default_factory=list)


def resolve(
    s: Session,
    fields: list[FormField],
    *,
    profile: MasterProfile,
    applicant: ApplicantSettings,
    job_context: str = "",
    use_llm: bool = True,
    model: str = "haiku",
    timeout: int = 300,
) -> Resolution:
    res = Resolution()
    pending: list[FormField] = []
    for f in fields:
        if f.kind == "file":
            continue  # resume / cover letter uploads handled by the filler
        if f.kind == "checkbox" and not f.options and _is_consent(f.label):
            res.answers[f.key], res.sources[f.key] = "check", "consent"
            continue
        ruled = rule_answer(f.label, profile, applicant)
        value, source = (ruled, "rule") if ruled else (bank_answer(s, f.label), "bank")
        if value and f.options:
            value = pick_option(f.options, value)
        if value:
            res.answers[f.key], res.sources[f.key] = value, source
        else:
            pending.append(f)
    if pending and use_llm:
        bank = list(s.exec(select(QAItem)))
        for key, value in llm_answers(
            pending,
            profile=profile,
            applicant=applicant,
            bank=bank,
            job_context=job_context,
            model=model,
            timeout=timeout,
        ).items():
            res.answers[key], res.sources[key] = value, "llm"
    res.missing_required = [f for f in pending if f.required and f.key not in res.answers]
    return res


def _is_consent(label: str) -> bool:
    n = normalize_question(label)
    return bool(re.search(r"\b(i agree|agree|consent|acknowledge|privacy (policy|notice)|terms)\b", n))
