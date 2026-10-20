"""Grounded cover letter (Sonnet) + the same honesty checks as the resume, rendered with Typst."""

from __future__ import annotations

import json
import shutil
from datetime import date
from importlib import resources
from pathlib import Path

import typst
from pydantic import BaseModel, Field

from tengen_toppa.analysis import JobAnalysis
from tengen_toppa.llm.claude_client import ask
from tengen_toppa.profile import MasterProfile
from tengen_toppa.tailoring.guardrails import numbers, terms_in

MAX_WORDS = 350


class CoverLetter(BaseModel):
    greeting: str = Field(description='e.g. "Dear Hiring Team,"')
    paragraphs: list[str] = Field(description="3-4 short paragraphs")
    closing: str = Field(description='e.g. "Sincerely,"')

    @property
    def text(self) -> str:
        return "\n\n".join([self.greeting, *self.paragraphs, self.closing])


def check_cover_letter(letter: CoverLetter, master: MasterProfile, jd_terms: list[str]) -> list[str]:
    body = " ".join(letter.paragraphs)
    problems = []
    master_text = master.model_dump_json()
    if extra := numbers(body) - numbers(master_text):
        problems.append(f"numbers not in the profile: {sorted(extra)}")
    verified = {s.lower() for s in master.skills_verified} | {t.lower() for p in master.projects for t in p.tech}
    vocab = set(jd_terms) | set(master.skills_verified)
    unverified = {t for t in terms_in(body, vocab) if t.lower() not in verified}
    if unverified:
        problems.append(f"claims unverified tech: {sorted(unverified)}")
    words = len(body.split())
    if words > MAX_WORDS:
        problems.append(f"too long: {words} words (max {MAX_WORDS})")
    return problems


def write_cover_letter(
    master: MasterProfile,
    analysis: JobAnalysis,
    *,
    company: str,
    description: str,
    model: str,
    timeout: int,
    attempts: int = 2,
) -> tuple[CoverLetter | None, list[str]]:
    jd_terms = analysis.must_have_skills + analysis.nice_to_have_skills
    feedback = ""
    problems: list[str] = []
    for _ in range(attempts):
        payload = (
            f"<master_profile>\n{master.model_dump_json()}\n</master_profile>\n"
            f"<company>{company}</company>\n<job_analysis>\n{analysis.model_dump_json()}\n</job_analysis>\n"
            f"<job_description>\n{description[:8000]}\n</job_description>{feedback}"
        )
        res = ask(
            "Write the cover letter following the rules. Input on stdin.",
            payload,
            CoverLetter,
            system_prompt="cover_letter.md",
            model=model,
            timeout=timeout,
        )
        problems = check_cover_letter(res.value, master, jd_terms)
        if not problems:
            return res.value, []
        feedback = "\n<rejected_because>\n" + "\n".join(f"- {p}" for p in problems) + "\n</rejected_because>"
    return None, problems


def render_cover_letter(letter: CoverLetter, master: MasterProfile, company: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = Path(str(resources.files("tengen_toppa.tailoring").joinpath("templates/cover_letter.typ")))
    shutil.copyfile(template, out_dir / "cover_letter.typ")
    data = {
        "basics": master.basics.model_dump(),
        "company": company,
        "date": date.today().strftime("%d %B %Y"),
        "greeting": letter.greeting,
        "paragraphs": letter.paragraphs,
        "closing": letter.closing,
    }
    (out_dir / "cover_letter.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (out_dir / "cover_letter.txt").write_text(letter.text + f"\n{master.basics.name}\n")
    out = out_dir / "cover_letter.pdf"
    typst.compile(str(out_dir / "cover_letter.typ"), output=str(out), root=str(out_dir))
    return out
