"""Fallback for career sites without an adapter: Claude Code drives YOUR dedicated Chrome via Playwright MCP.

The agent fills the form and uploads the resume but is instructed never to submit. Python then takes
the screenshot and asks you on WhatsApp. Uploads are copied into the sandbox, because Playwright MCP may
only read files inside its working directory.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from tengen_toppa.config import ApplicantSettings
from tengen_toppa.llm.claude_client import CallResult, run_browser_agent
from tengen_toppa.paths import get_paths
from tengen_toppa.profile import MasterProfile


class AgentQuestion(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)


class AgentReport(BaseModel):
    reached_form: bool
    form_url: str = Field(description="Exact URL of the tab where you filled the form")
    filled_fields: list[str] = Field(description="Labels of fields you filled")
    uploaded_resume: bool
    unanswered_required: list[AgentQuestion] = Field(description="Required questions you could not answer")
    captcha_or_login: bool = Field(description="True if a CAPTCHA, login or account creation blocks you")
    ready_to_submit: bool = Field(description="All required fields filled; NOT submitted")
    notes: str = ""


def write_mcp_config(cdp_port: int) -> Path:
    cfg = get_paths().mcp_config
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["-y", "@playwright/mcp@latest", "--cdp-endpoint", f"http://127.0.0.1:{cdp_port}"],
                    }
                }
            },
            indent=2,
        )
    )
    return cfg


def stage_uploads(job_id: int, resume: Path, cover_letter: Path | None) -> tuple[Path, Path | None]:
    dest = get_paths().llm_sandbox / "uploads" / str(job_id)
    dest.mkdir(parents=True, exist_ok=True)
    r = Path(shutil.copy2(resume, dest / resume.name))
    c = Path(shutil.copy2(cover_letter, dest / cover_letter.name)) if cover_letter else None
    return r, c


def run_agent(
    *,
    job_id: int,
    url: str,
    profile: MasterProfile,
    applicant: ApplicantSettings,
    qa_bank: dict[str, str],
    resume: Path,
    cover_letter: Path | None,
    cdp_port: int,
    model: str,
    max_turns: int,
) -> CallResult[AgentReport]:
    r, c = stage_uploads(job_id, resume, cover_letter)
    facts = {
        "basics": profile.basics.model_dump(),
        "current_role": profile.experience[0].model_dump(include={"company", "role", "start"})
        if profile.experience
        else None,
        "applicant_facts": applicant.model_dump(),
        "qa_bank": qa_bank,
        "resume_file": str(r),
        "cover_letter_file": str(c) if c else None,
    }
    payload = f"<application_url>{url}</application_url>\n<candidate>\n{json.dumps(facts, indent=1)}\n</candidate>"
    return run_browser_agent(
        "Fill the job application described on stdin, following the system rules.",
        payload,
        AgentReport,
        system_prompt="browser_agent.md",
        mcp_config=write_mcp_config(cdp_port),
        model=model,
        max_turns=max_turns,
    )
