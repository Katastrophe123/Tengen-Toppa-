"""Filesystem layout. Everything lives inside the project folder (or TENGEN_HOME if set).

tengen-toppa/
  config/settings.toml          your settings
  data/profile/master_profile.json   your resume source of truth
  data/...                      SQLite db, queue, resumes, screenshots, browser profile
  prompts/                      optional prompt overrides
  logs/ backups/ models/ llm-sandbox/ launchd/
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def db(self) -> Path:
        return self.data / "tengen.db"

    @property
    def queue_db(self) -> Path:
        return self.data / "queue.db"

    @property
    def profile(self) -> Path:
        return self.data / "profile" / "master_profile.json"

    @property
    def resumes(self) -> Path:
        return self.data / "resumes"

    @property
    def screenshots(self) -> Path:
        return self.data / "screenshots"

    @property
    def jd_raw(self) -> Path:
        return self.data / "jd_raw"

    @property
    def browser_profile(self) -> Path:
        return self.data / "browser-profile"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.toml"

    @property
    def prompts_override(self) -> Path:
        return self.home / "prompts"

    @property
    def llm_sandbox(self) -> Path:
        return self.home / "llm-sandbox"

    @property
    def models(self) -> Path:
        return self.home / "models"

    @property
    def mcp_config(self) -> Path:
        return self.config_dir / "playwright-mcp.json"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def launchd(self) -> Path:
        return self.home / "launchd"

    def resume_dir(self, job_id: int) -> Path:
        return self.resumes / str(job_id)

    def ensure(self) -> None:
        for d in (
            self.data,
            self.profile.parent,
            self.resumes,
            self.screenshots,
            self.jd_raw,
            self.browser_profile,
            self.backups,
            self.config_dir,
            self.prompts_override,
            self.llm_sandbox,
            self.logs,
            self.launchd,
            self.models,
        ):
            d.mkdir(parents=True, exist_ok=True)


def project_root() -> Path | None:
    """The repo folder (src/tengen_toppa/paths.py -> ../../). None if installed as a plain wheel."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() else None


def get_paths() -> Paths:
    env = os.environ.get("TENGEN_HOME")
    if env:
        return Paths(Path(env).expanduser())
    return Paths(project_root() or Path.home() / "TengenToppa")
