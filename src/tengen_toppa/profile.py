"""Master profile: the single source of truth for every resume the system produces."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class Link(BaseModel):
    label: str
    url: str


class Basics(BaseModel):
    name: str
    email: str
    phone: str = ""
    location: str = ""
    headline: str = ""
    years_experience: float = 0
    links: list[Link] = Field(default_factory=list)


class Bullet(BaseModel):
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    id: str
    company: str
    role: str
    location: str = ""
    start: str
    end: str = "Present"
    bullets: list[Bullet]


class Project(BaseModel):
    id: str
    name: str
    url: str = ""
    tech: list[str] = Field(default_factory=list)
    bullets: list[str]
    locked: bool = True  # tailoring can never change the text of a locked project


class Education(BaseModel):
    institution: str
    degree: str
    start: str = ""
    end: str = ""
    score: str = ""


class MasterProfile(BaseModel):
    basics: Basics
    summary: str = ""
    skills_verified: list[str]
    experience: list[Experience]
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> MasterProfile:
        ids = [e.id for e in self.experience] + [p.id for p in self.projects]
        ids += [b.id for e in self.experience for b in e.bullets]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Duplicate ids in master profile: {sorted(dupes)}")
        return self

    def experience_by_id(self, exp_id: str) -> Experience | None:
        return next((e for e in self.experience if e.id == exp_id), None)

    def compact_for_llm(self) -> str:
        """Short profile view for cheap analysis/scoring calls."""
        roles = "; ".join(f"{e.role} @ {e.company} ({e.start}-{e.end})" for e in self.experience)
        return (
            f"Name: {self.basics.name}\nHeadline: {self.basics.headline}\n"
            f"Years experience: {self.basics.years_experience}\nLocation: {self.basics.location}\n"
            f"Roles: {roles}\nVerified skills: {', '.join(self.skills_verified)}\n"
            f"Projects: {', '.join(p.name + ' [' + ', '.join(p.tech) + ']' for p in self.projects)}"
        )


def load_profile(path: Path) -> MasterProfile:
    return MasterProfile.model_validate(json.loads(path.read_text()))


SAMPLE_PROFILE: dict = {
    "basics": {
        "name": "Your Name",
        "email": "you@example.com",
        "phone": "+91 90000 00000",
        "location": "Bengaluru, India",
        "headline": "Backend Software Engineer",
        "years_experience": 3,
        "links": [
            {"label": "GitHub", "url": "https://github.com/you"},
            {"label": "LinkedIn", "url": "https://linkedin.com/in/you"},
        ],
    },
    "summary": "Backend engineer with 3 years building Python services and data pipelines.",
    "skills_verified": ["Python", "FastAPI", "PostgreSQL", "Docker", "AWS", "Redis", "Git", "REST APIs"],
    "experience": [
        {
            "id": "acme",
            "company": "Acme Corp",
            "role": "Software Engineer",
            "location": "Bengaluru",
            "start": "Jul 2022",
            "end": "Present",
            "bullets": [
                {
                    "id": "acme-1",
                    "text": "Built a FastAPI payments service handling 2M requests/day with p99 latency under 120 ms.",
                    "tags": ["backend", "scale"],
                },
                {
                    "id": "acme-2",
                    "text": "Cut AWS costs by 30% by moving batch jobs to spot instances and tuning PostgreSQL.",
                    "tags": ["cloud", "cost"],
                },
                {
                    "id": "acme-3",
                    "text": "Mentored 2 junior engineers and introduced code review guidelines for the team.",
                    "tags": ["leadership"],
                },
            ],
        }
    ],
    "projects": [
        {
            "id": "proj-1",
            "name": "Tengen-Toppa",
            "url": "https://github.com/you/tengen-toppa",
            "tech": ["Python", "SQLite", "Claude"],
            "bullets": ["Local-first job application autopilot with guarded resume tailoring."],
            "locked": True,
        }
    ],
    "education": [
        {
            "institution": "Your University",
            "degree": "B.Tech, Computer Science",
            "start": "2018",
            "end": "2022",
        }
    ],
    "certifications": [],
    "achievements": [],
}
