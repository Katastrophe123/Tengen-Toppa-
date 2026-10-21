"""The exact resume PDF used for LinkedIn Easy Apply. It must agree with master_profile.json, because
form answers come from the profile while recruiters read the PDF."""

from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from sqlmodel import Session

from tengen_toppa.config import settings
from tengen_toppa.paths import get_paths
from tengen_toppa.profile import MasterProfile
from tengen_toppa.store.db import kv_get, kv_set

KV_KEY = "resume_file"


class ResumeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResumeInfo:
    path: Path
    sha256: str
    display_name: str
    pages: int


def _squash(text: str) -> str:
    """NFKC folds PDF ligatures ('ﬁ' -> 'fi'), then keep only letters and digits."""
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", text).lower())


def pdf_text(pdf: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)


def profile_mismatches(pdf: Path, profile: MasterProfile) -> list[str]:
    """Facts in the profile that the PDF does not contain."""
    body = _squash(pdf_text(pdf))
    checks: list[tuple[str, str]] = [("name", profile.basics.name), ("email", profile.basics.email)]
    phone = re.sub(r"\D", "", profile.basics.phone)[-10:]
    if phone:
        checks.append(("phone", phone))
    for e in profile.experience:
        checks += [("company", e.company), ("role", e.role), ("start date", e.start)]
    for ed in profile.education:
        checks += [("institution", ed.institution), ("degree", ed.degree)]
    for p in profile.projects:
        checks.append(("project", p.name.split("—")[0].split(" - ")[0]))
    return [f"{label} '{value}' is not in the PDF" for label, value in checks if value and _squash(value) not in body]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(s: Session, pdf: Path, profile: MasterProfile, *, force: bool = False) -> tuple[ResumeInfo, list[str]]:
    if not pdf.exists():
        raise ResumeError(f"{pdf} not found")
    try:
        pages = len(PdfReader(pdf).pages)
    except Exception as e:
        raise ResumeError(f"{pdf} is not a readable PDF: {e}") from e
    problems = profile_mismatches(pdf, profile)
    if problems and not force:
        raise ResumeError("PDF and master_profile.json disagree:\n- " + "\n- ".join(problems))
    name = settings().linkedin.resume_display_name
    dest = get_paths().profile.parent / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf, dest)
    info = ResumeInfo(dest, sha256(dest), name, pages)
    history = list(kv_get(s, "resume_history", []) or [])
    history.append({"sha256": info.sha256, "source": str(pdf)})
    kv_set(s, "resume_history", history[-20:])
    kv_set(
        s,
        KV_KEY,
        {"path": str(dest), "sha256": info.sha256, "display_name": name, "pages": pages, "version": len(history)},
    )
    return info, problems


def current(s: Session) -> ResumeInfo:
    data = kv_get(s, KV_KEY)
    if not data:
        raise ResumeError("No resume installed. Run: uv run tengen resume set <path-to-pdf>")
    path = Path(data["path"])
    if not path.exists():
        raise ResumeError(f"Installed resume missing at {path}. Run `tengen resume set` again.")
    if sha256(path) != data["sha256"]:
        raise ResumeError("The installed resume file changed on disk. Run `tengen resume set` again.")
    return ResumeInfo(path, data["sha256"], data["display_name"], data["pages"])
