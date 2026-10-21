"""User settings loaded from ~/TengenToppa/config/settings.toml."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tengen_toppa.paths import get_paths


class SearchSettings(BaseModel):
    title_include: list[str] = Field(default_factory=lambda: ["software engineer", "backend", "python"])
    title_exclude: list[str] = Field(default_factory=lambda: ["intern", "principal", "director", "manager"])
    locations: list[str] = Field(default_factory=lambda: ["india", "remote"])
    allow_remote: bool = True
    company_blacklist: list[str] = Field(default_factory=list)
    max_years_gap: int = 2
    needs_visa_sponsorship: bool = False


class GreenhouseSource(BaseModel):
    boards: list[str] = Field(default_factory=list)


class LeverSource(BaseModel):
    companies: list[str] = Field(default_factory=list)


class AshbySource(BaseModel):
    boards: list[str] = Field(default_factory=list)


class Sources(BaseModel):
    greenhouse: GreenhouseSource = Field(default_factory=GreenhouseSource)
    lever: LeverSource = Field(default_factory=LeverSource)
    ashby: AshbySource = Field(default_factory=AshbySource)


class PipelineSettings(BaseModel):
    discover_every_hours: int = 4
    analyze_per_run: int = 25
    min_score: int = 70
    daily_tailor_cap: int = 20
    auto_apply_threshold: int = 0
    allow_project_reorder: bool = False
    max_bullets_per_role: int = 5


class LLMSettings(BaseModel):
    analyze_model: str = "haiku"
    tailor_model: str = "sonnet"
    timeout_seconds: int = 300
    max_tailor_attempts: int = 3


class WhatsAppSettings(BaseModel):
    graph_version: str = "v23.0"
    digest_template: str = "tengen_daily_digest"
    alert_template: str = "tengen_alert"
    template_language: str = "en"
    digest_hour: int = 9


class ApplicantSettings(BaseModel):
    """Facts for application forms that are not on a resume. Used verbatim, never invented."""

    country: str = "India"
    work_authorized: bool = True
    requires_sponsorship: bool = False
    # Countries you can work in without sponsorship. "European Union" covers all EU members.
    work_authorized_countries: list[str] = Field(default_factory=lambda: ["India"])
    notice_period: str = "30 days"
    earliest_start: str = "Within 30 days"
    current_ctc: str = ""
    expected_ctc: str = ""
    willing_to_relocate: bool = True
    open_to_remote: bool = True
    how_did_you_hear: str = "Company careers page"
    pronouns: str = ""
    gender: str = "Decline to self-identify"
    race_ethnicity: str = "Decline to self-identify"
    veteran_status: str = "I am not a protected veteran"
    disability_status: str = "I do not want to answer"


class ApplySettings(BaseModel):
    mode: Literal["auto_fill", "assist"] = "auto_fill"  # assist = only open page + resume
    auto_submit: bool = False  # False: fill everything, then YOU click submit on the Mac
    cdp_port: int = 9333
    browser: Literal["chrome", "chromium"] = "chrome"
    headless: bool = False  # True = invisible browser (you can't review/submit on screen)
    claude_fallback: bool = True  # unknown sites: Claude Code + Playwright MCP fills the form
    fallback_max_turns: int = 40
    cover_letter: Literal["when_field_present", "always", "never"] = "when_field_present"


class EmailSettings(BaseModel):
    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    address: str = ""  # password: `tengen secrets set email_app_password`
    poll_minutes: int = 30
    lookback_days: int = 3


DEFAULT_AI_TERMS = [
    "AI",
    "artificial intelligence",
    "machine learning",
    "ML",
    "deep learning",
    "LLM",
    "LLMs",
    "large language models",
    "generative AI",
    "GenAI",
    "NLP",
    "natural language processing",
    "RAG",
    "retrieval-augmented generation",
    "vector database",
    "vector databases",
    "embeddings",
    "LangChain",
    "LangGraph",
    "LlamaIndex",
    "Hugging Face",
    "Transformers",
    "prompt engineering",
    "fine-tuning",
    "LoRA",
    "PEFT",
    "RLHF",
    "SFT",
    "AI agents",
    "agentic AI",
    "MCP",
    "Model Context Protocol",
    "OpenAI",
    "GPT",
    "Claude",
    "Gemini",
    "Llama",
    "MLOps",
    "LLMOps",
    "PyTorch",
    "TensorFlow",
    "Keras",
    "scikit-learn",
    "XGBoost",
    "computer vision",
    "Pandas",
    "NumPy",
]


class ExperienceSettings(BaseModel):
    """Your own statement of experience, used verbatim for 'years with X' questions. Never inferred."""

    total_years: float = 1
    verified_skill_years: float = 1  # every skill in skills_verified
    ai_skill_years: float = 1  # every term in ai_terms (you stated: 1+ year across AI tech)
    ai_terms: list[str] = Field(default_factory=lambda: list(DEFAULT_AI_TERMS))
    skills: dict[str, float] = Field(default_factory=dict)  # per-skill overrides
    max_years_required: float = 1  # only apply when the job asks for at most this many years


class LinkedInSettings(BaseModel):
    enabled: bool = True
    mode: Literal["auto", "review"] = "review"  # review = you approve + tap Submit for each job
    keywords: list[str] = Field(
        default_factory=lambda: [
            "AI Engineer",
            "LLM Engineer",
            "Machine Learning Engineer",
            "Generative AI",
            "AI Software Engineer",
            "Python Developer",
            "Data Scientist",
        ]
    )
    locations: list[str] = Field(
        default_factory=lambda: [
            "Gurugram, Haryana, India",
            "Delhi, India",
            "Bengaluru, Karnataka, India",
            "India",
        ]
    )
    experience_levels: list[Literal["internship", "entry", "associate", "mid_senior"]] = Field(
        default_factory=lambda: ["entry", "associate"]
    )
    workplace: list[Literal["onsite", "remote", "hybrid"]] = Field(
        default_factory=lambda: ["onsite", "remote", "hybrid"]
    )
    first_fill_days: int = 7
    pool_target: int = 500
    max_pages_per_search: int = 8
    max_pages_per_run: int = 40
    page_delay_seconds: float = 4
    max_applications_per_day: int = 30
    max_per_hour: int = 10
    windows: list[str] = Field(default_factory=lambda: ["10:00", "14:00", "18:00"])
    per_window: int = 10
    auto_schedule: bool = False
    apply_interval_seconds: int = 90
    stop_after_consecutive_failures: int = 5
    follow_company: bool = False
    resume_display_name: str = "Kanishk_Singh_Parihar_Resume.pdf"
    dedupe_days: int = 30
    status_sync_hours: list[int] = Field(default_factory=lambda: [9, 21])
    max_steps: int = 10


class DashboardSettings(BaseModel):
    interval_minutes: int = 3
    only_when_changed: bool = True
    commands_offline_pause_minutes: int = 10


class Settings(BaseModel):
    search: SearchSettings = Field(default_factory=SearchSettings)
    sources: Sources = Field(default_factory=Sources)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    whatsapp: WhatsAppSettings = Field(default_factory=WhatsAppSettings)
    applicant: ApplicantSettings = Field(default_factory=ApplicantSettings)
    apply: ApplySettings = Field(default_factory=ApplySettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    experience: ExperienceSettings = Field(default_factory=ExperienceSettings)
    linkedin: LinkedInSettings = Field(default_factory=LinkedInSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)


def load_settings(path: Path | None = None) -> Settings:
    path = path or get_paths().settings_file
    if not path.exists():
        return Settings()
    with path.open("rb") as fh:
        return Settings.model_validate(tomllib.load(fh))


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()


SAMPLE_SETTINGS_TOML = """\
# Tengen-Toppa settings. Edit, then restart the worker.

[search]
title_include = ["software engineer", "backend", "python", "sde"]
title_exclude = ["intern", "principal", "director", "manager", "staff"]
locations = ["india", "bangalore", "bengaluru", "remote"]
allow_remote = true
company_blacklist = []
max_years_gap = 2              # skip if JD asks for > your years + this
needs_visa_sponsorship = false

# Public ATS job boards (the slug from the company's careers URL)
[sources.greenhouse]
boards = ["stripe", "databricks"]   # boards.greenhouse.io/<slug>

[sources.lever]
companies = []                      # jobs.lever.co/<slug>

[sources.ashby]
boards = []                         # jobs.ashbyhq.com/<slug>

[pipeline]
discover_every_hours = 4
analyze_per_run = 25          # top-N jobs (after free local prefilter) sent to Claude
min_score = 70                # below this, job is skipped
daily_tailor_cap = 20         # protects your Claude subscription limits
auto_apply_threshold = 0      # 0 = always ask on WhatsApp
allow_project_reorder = false # projects are always locked; this only allows reordering
max_bullets_per_role = 5

[llm]
analyze_model = "haiku"
tailor_model = "sonnet"
timeout_seconds = 300
max_tailor_attempts = 3

[whatsapp]
graph_version = "v23.0"
digest_template = "tengen_daily_digest"   # approved utility template, 1 body param: job count
alert_template = "tengen_alert"           # approved utility template, 1 body param: text
template_language = "en"
digest_hour = 9

# Facts used to fill application forms. Used verbatim, never invented.
# Anything a form asks that isn't here or in the Q&A bank is asked on WhatsApp and remembered.
[applicant]
country = "India"
work_authorized = true
requires_sponsorship = false
notice_period = "30 days"
earliest_start = "Within 30 days"
current_ctc = ""
expected_ctc = ""
willing_to_relocate = true
open_to_remote = true
how_did_you_hear = "Company careers page"
pronouns = ""
gender = "Decline to self-identify"
race_ethnicity = "Decline to self-identify"
veteran_status = "I am not a protected veteran"
disability_status = "I do not want to answer"

[apply]
mode = "auto_fill"          # "assist" = only open the page + reveal the resume
auto_submit = false         # false = fill everything, send you a screenshot, YOU click submit on the Mac
cdp_port = 9333             # dedicated Chrome profile (data/browser-profile) driven over CDP
browser = "chrome"          # "chrome" (installed Google Chrome) or "chromium" (Playwright's)
claude_fallback = true      # unknown sites: Claude Code + Playwright MCP fills the form (never submits)
fallback_max_turns = 40
cover_letter = "when_field_present"   # "always" | "never"

[email]
enabled = false             # track replies (interview / rejection / assessment) over IMAP
imap_host = "imap.gmail.com"
address = ""                # Gmail: create an App Password, then `tengen secrets set email_app_password`
poll_minutes = 30
lookback_days = 3
"""
