# Prompt overrides

Drop a file here with the same name as a packaged prompt to override it, without touching the code:

| File | Used for |
|---|---|
| `analyze_job.md` | JD analysis + fit score (Haiku) |
| `tailor_resume.md` | resume tailoring rules (Sonnet) |
| `cover_letter.md` | cover letter rules (Sonnet) |
| `answer_questions.md` | application-form answers (Haiku) |
| `browser_agent.md` | Claude + Playwright MCP form filling for unknown sites |
| `extract_job.md` | reading a job from any URL (LinkedIn, Naukri, ...) |
| `classify_email.md` | recruiter reply classification |

The originals live in `src/tengen_toppa/prompts/`. The guardrail code still checks every output, so an override can't switch off the no-fabrication rules.
