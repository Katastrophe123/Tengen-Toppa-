"""Merge tailored output with immutable master data and render a PDF with Typst."""

from __future__ import annotations

import json
import re
import shutil
from importlib import resources
from pathlib import Path

import typst
from pypdf import PdfReader

from tengen_toppa.profile import MasterProfile
from tengen_toppa.tailoring.schema import TailoredResume


def build_document(master: MasterProfile, tailored: TailoredResume | None) -> dict:
    """Company, role, dates, projects, education always come from master. Only wording/selection changes."""
    if tailored is None:  # untailored master resume
        summary = master.summary
        skills = master.skills_verified
        roles = {e.id: [b.text for b in e.bullets] for e in master.experience}
        project_ids = [p.id for p in master.projects]
    else:
        summary = tailored.summary
        skills = tailored.skills
        roles = {r.experience_id: [b.text for b in r.bullets] for r in tailored.experience}
        project_ids = tailored.project_order

    projects_by_id = {p.id: p for p in master.projects}
    return {
        "basics": master.basics.model_dump(),
        "summary": summary,
        "skills": skills,
        "experience": [
            {
                "company": e.company,
                "role": e.role,
                "location": e.location,
                "start": e.start,
                "end": e.end,
                "bullets": roles.get(e.id, []),
            }
            for e in master.experience
        ],
        "projects": [projects_by_id[pid].model_dump() for pid in project_ids if pid in projects_by_id],
        "education": [ed.model_dump() for ed in master.education],
        "certifications": master.certifications,
        "achievements": master.achievements,
    }


def render_pdf(doc: dict, out_dir: Path, filename: str = "resume.pdf") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = Path(str(resources.files("tengen_toppa.tailoring").joinpath("templates/resume.typ")))
    shutil.copyfile(template, out_dir / "resume.typ")
    (out_dir / "data.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    out = out_dir / filename
    typst.compile(str(out_dir / "resume.typ"), output=str(out), root=str(out_dir))
    return out


def pdf_filename(name: str, company: str, title: str) -> str:
    def slug(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:30]

    return f"{slug(name)}_{slug(company)}_{slug(title)}.pdf"


def ats_parse_check(pdf: Path, master: MasterProfile) -> list[str]:
    """Round-trip: can a text extractor (like an ATS) read the essentials back out?"""
    text = " ".join((page.extract_text() or "") for page in PdfReader(pdf).pages)
    flat = re.sub(r"\s+", " ", text).lower()
    problems = []
    for label, value in [("name", master.basics.name), ("email", master.basics.email)] + [
        ("company", e.company) for e in master.experience
    ]:
        if value and re.sub(r"\s+", " ", value).lower() not in flat:
            problems.append(f"ATS parse: {label} '{value}' not extractable from PDF")
    pages = len(PdfReader(pdf).pages)
    if pages > 2:
        problems.append(f"resume is {pages} pages")
    return problems
