<p align="center">
  <img src="docs/assets/hero.jpg" alt="Tengen Toppa Gurren Lagann" width="320">
</p>

<h1 align="center">Tengen-Toppa</h1>

<p align="center"><b>Version 1.1.0</b> · Local-first AI job-application autopilot · Claude Code + WhatsApp</p>

A job-application autopilot that runs on your own Mac. It finds jobs, tailors your resume **without inventing anything**, fills application forms, asks you only what it can't know, and tracks replies. You control it from **WhatsApp**. All AI runs through your **Claude Code subscription** (no API key). All data stays on your Mac.

```
 Greenhouse / Lever / Ashby boards ─┐            any job link you send on WhatsApp
                                    ▼            (LinkedIn, Naukri, company sites)
        free filters + local embeddings  ◄───────────────┘
                     ▼
        Claude Haiku: JD analysis + fit score
                     ▼
        Claude Sonnet: resume tailoring ──► code guardrails (reject + retry on any fabrication)
                     ▼
        Typst PDF + ATS parse-back check
                     ▼
        WhatsApp card: PDF + [Apply] [Request edit] [Skip]
                     ▼  Apply
        dedicated "Tengen Chrome" on your Mac:
          Greenhouse / Lever / Ashby  → Playwright adapter fills the form
          other sites                 → Claude Code + Playwright MCP fills it (never submits)
          LinkedIn / Naukri / Workday → opened for you, you submit (assist mode)
          unknown required question   → asked on WhatsApp, answer saved to the Q&A bank
                     ▼
        screenshot to WhatsApp + [Submit now]   (or auto_submit = true)
                     ▼
        IMAP reply tracking → interview / assessment / rejection / offer alerts
```

## Status (v1.1.0)

| Area | State |
|---|---|
| Job discovery: Greenhouse, Lever, Ashby public boards; dedupe | Working, tested against live boards |
| Add any job by link (WhatsApp or CLI), incl. LinkedIn via your own session | Working, verified with a live LinkedIn job |
| JD analysis + fit score (Haiku), resume tailoring (Sonnet) with code guardrails | Working, verified on real jobs |
| Typst resume and cover-letter PDFs, ATS parse-back check | Working |
| Form filling: Greenhouse / Lever / Ashby adapters, Claude + Playwright MCP fallback | Working on local test forms; not yet run against live ATS forms |
| WhatsApp two-way (cards, buttons, commands, form questions), signed webhook | Working end to end |
| Email reply tracking (IMAP) | Built and tested offline; off by default |
| Background running via launchd | Built; quick tunnel needs `tunnel-sync` after sleep |
| **LinkedIn Easy Apply autopilot**: standing pool (≥500), harvester, years filter, Easy Apply engine, run controller | Harvester, job-page reader and pre-run checks verified live (read-only). Engine verified on a local copy of the Easy Apply flow; **first live applications pending in review mode** |
| WhatsApp live dashboard (every 3 min, only when changed), run commands, kill switches | Built and tested |
| Tests | 135 passing (`uv run pytest`), incl. real headless-Chrome form filling and Easy Apply flows |

**Known limits**
- A cloudflared *quick* tunnel dies when the Mac sleeps or the network changes. Restart cloudflared, then run `uv run tengen tunnel-sync`. A named Cloudflare tunnel (needs a domain) avoids this.
- The Meta test number can only message numbers verified in the Meta dashboard. A real business number needs a payment method, business verification and a complete business profile.
- WhatsApp allows free-form bot messages only for 24h after your last message. Approved templates (`tengen_daily_digest`, `tengen_alert`) cover the rest.
- Naukri and Workday are assist-only by design. LinkedIn Easy Apply has its own engine (below), capped at 30/day.
- **Automating LinkedIn is against LinkedIn's terms and can get the account restricted.** Tengen-Toppa paces itself and stops on any checkpoint, but it does not hide that it's automated.

## Folder layout

```
tengen-toppa/
├── config/
│   ├── settings.toml               ← EDIT: boards, filters, [applicant] facts, [apply], [email]
│   └── master_profile.example.json  template for your profile
├── data/                           (git-ignored, personal)
│   ├── profile/master_profile.json ← EDIT: your resume facts (single source of truth)
│   ├── tengen.db / queue.db        SQLite: jobs, resumes, applications, Q&A bank, logs
│   ├── resumes/<job>/vN/           tailored resume + cover letter per version
│   ├── screenshots/<job>/          filled / submitted / captcha screenshots
│   └── browser-profile/            the Tengen Chrome profile (your logins)
├── docs/privacy-policy.md          privacy policy (required to publish the Meta app)
├── prompts/                        optional prompt overrides (see prompts/README.md)
├── scripts/setup.sh                one-time setup
├── src/tengen_toppa/               the code
├── tests/                          74 tests
└── logs/ backups/ models/ llm-sandbox/ launchd/   (git-ignored contents)
```
Secrets live in the macOS Keychain (service `tengen-toppa`), never in files. Set `TENGEN_HOME` to keep data somewhere else.


## LinkedIn Easy Apply autopilot

Applies to LinkedIn Easy Apply jobs with **one fixed resume PDF**, at most **30 per day**, and only to jobs asking for **up to 1 year** of experience.

```
pool (≥500 eligible Easy Apply jobs, topped up from the past 24h each run)
  → read job page (no clicks): closed? applied? Easy Apply? years required ≤ 1?
  → write "submitting" record + check today's cap          ← crash-safe, cap checked at the click
  → Easy Apply window: contact → resume (this exact PDF) → questions → review → submit
  → unknown required question → skip job, ask you once on WhatsApp, job re-queued after your answer
  → LinkedIn checkpoint / limit / login wall → stop the run, alert you
```

**Setup**
```bash
uv run tengen resume set ~/Downloads/ResumeV1.pdf   # checked against master_profile.json
uv run tengen browser                               # sign in to LinkedIn in Tengen Chrome (once)
uv run tengen li-preflight                          # read-only checks
uv run tengen li-harvest --pages 3                  # read-only: fill the pool, see what it finds
uv run tengen li-pool                               # top of the pool
```
Settings live in `config/settings.toml` under `[linkedin]`, `[experience]` and `[dashboard]`.
- `[experience]`: your stated years. Every skill in your profile and every term in `ai_terms` counts as 1 year; forms are answered from this only. A skill not listed (e.g. Kubernetes) is asked once on WhatsApp.
- `[linkedin] mode`:
  - `"review"` (the default, for the test flight): each job asks for your **Apply**, then sends a screenshot with **Submit now**.
  - `"auto"`: fully autonomous. Needs the `tengen_alert` template approved.

**Run** (worker running): send `run` (or `run dry` to read and rank only) on WhatsApp, or `uv run tengen li-run [--dry]`.

**Dashboard** (every 3 min during a run, only when something changed):
```
📊 Tengen-Toppa · Run #12 · applying (1h 05m) · auto mode
Pool 486 · harvested 120 · new eligible 31
✅ Applied 9/30 · ⏭ Skipped 4 · ❌ Failed 1 · ❓ Waiting 2
Today 9/30 · last hour 6/10
Last: ✅ AI Engineer @ Acme · ⏭ ML Engineer @ Foo · ✅ LLM Dev @ Bar
```
Commands: `run` · `run dry` · `stop` · `pause` · `resume` · `dash` · `failed` · `skipped` · `answers` · `cap N` · `report`.

**Safety**
- Every click goes through a guard. "Easy Apply" (which submits on one-click jobs) and "Submit application" can only be clicked by a run you started, after the cap check.
- Aborting never clicks Dismiss or Discard: the tab is navigated away.
- Daily cap of 30 and hourly cap of 10, counted from the database at the moment of submit. Optional windows: 10:00 / 14:00 / 18:00, 10 each.
- Kill switches: `stop` on WhatsApp, `uv run tengen li-stop`, or a file named `STOP` in the project folder. The run pauses by itself if your WhatsApp commands can't reach the Mac for 10 minutes.
- Status sync reads LinkedIn's Applied tracker twice a day (`li-sync-status`).

## Requirements
macOS, Python 3.12 (via `uv`), Claude Code logged in with a Pro/Max subscription, Google Chrome (or Playwright Chromium), Node (for the Playwright MCP fallback), `cloudflared`, a Meta developer account for WhatsApp.

## Setup

```bash
./scripts/setup.sh          # installs uv/node/cloudflared if missing, deps, folders, checks Claude login
```

### 1. Profile and settings
- Copy `config/master_profile.example.json` to `data/profile/master_profile.json` (or run `uv run tengen init`) and fill it with **only true facts** from your resume. Each role has a pool of `bullets`. `skills_verified` is the only skill list the AI may use. Projects with `"locked": true` are never reworded.
- Edit `config/settings.toml`: `[sources]` boards, `[search]` titles and locations, `[applicant]` facts (notice period, CTC, visa, EEO). Blank `[applicant]` answers are asked once on WhatsApp and remembered.
- `uv run tengen preview-master` renders your untailored resume to check it.

### 2. WhatsApp (Meta Cloud API)
1. developers.facebook.com → create a **Business** app → add **WhatsApp**. In API Setup, add and verify your own number as a recipient. Note the **Phone number ID** and the **WhatsApp Business Account ID**.
2. business.facebook.com → Settings → System users → create an **Admin** system user → Assign assets (the app and the WhatsApp account, full control) → **Generate token** with expiry **Never** and `whatsapp_business_messaging` + `whatsapp_business_management`.
3. Copy the **App secret** (App settings → Basic).
4. Save secrets (type the command, then paste the value at the hidden prompt):
   ```bash
   uv run tengen secrets set wa_access_token     # permanent system-user token (starts with EAA)
   uv run tengen secrets set wa_phone_number_id
   uv run tengen secrets set wa_app_secret
   uv run tengen secrets set wa_verify_token     # any string you invent
   uv run tengen secrets set wa_my_number        # digits with country code, e.g. 919876543210
   uv run tengen wa-verify                       # every line must be [OK]
   ```
5. Publish the app: host `docs/privacy-policy.md` (e.g. a public GitHub Gist), set it as the Privacy Policy URL in App settings → Basic, then **Publish**. **Unpublished apps receive only dashboard test webhooks, never real messages.**
6. Link the WhatsApp account to the app (Meta's setup flow often skips this):
   ```bash
   uv run tengen wa-subscribe <WhatsApp Business Account ID>
   ```
7. Webhook:
   ```bash
   uv run tengen serve                              # window 1
   cloudflared tunnel --url http://localhost:8765   # window 2
   uv run tengen tunnel-sync                        # registers the tunnel URL with Meta
   ```
   In WhatsApp → Configuration → Webhook fields, **Subscribe** to `messages`. Run `uv run tengen wa-verify` again.
8. Optional: create Utility templates `tengen_daily_digest` (`You have {{1}} tailored jobs waiting. Reply START to review.`) and `tengen_alert` (`Tengen-Toppa: {{1}}`).

### 3. Browser
`uv run tengen browser` opens the dedicated Tengen Chrome. Sign in to LinkedIn and Naukri there once. Job links you send are read with that session.

### 4. Email tracking (optional)
Gmail → App passwords → `uv run tengen secrets set email_app_password`, then set `[email] enabled = true` and `address`.

### 5. Check
`uv run tengen doctor` (whole system) and `uv run tengen wa-verify` (WhatsApp).

## Run

```bash
uv run tengen serve      # window 1: WhatsApp webhook
cloudflared tunnel --url http://localhost:8765   # window 2
uv run tengen worker     # window 3: pipeline, schedules, Claude calls
```
Then send `help` on WhatsApp. Always-on: `uv run tengen launchd --tunnel-name <name>` writes launchd agents (worker runs under `caffeinate`).

### WhatsApp commands
| Send | Does |
|---|---|
| any job link | add + analyze + tailor + card |
| `apply N` / `submit N` / `skip N` / `done N` | same as the buttons / mark as submitted by you |
| `edit N <what to change>` | re-tailor with your note, new PDF |
| `job N` · `more` · `status` · `question` | resend card · next batch · status · repeat pending form question |
| `pause` · `resume` · `limit N` · `help` | pipeline controls |

### CLI
| Command | Purpose |
|---|---|
| `init` · `doctor` · `preview-master` · `backup` | setup, health check, resume preview, DB backup |
| `discover` · `analyze` · `tailor N` · `run` | pipeline steps / one full pass |
| `add <url>` · `apply N` · `submit N` · `cover-letter N` | single-job actions |
| `jobs [-s status]` · `show N` · `status` | inspect |
| `qa list \| add \| remove` | saved form answers |
| `secrets set \| list` | Keychain secrets (values validated before saving) |
| `wa-verify` · `wa-subscribe <WABA_ID>` · `tunnel-sync` · `wa-test` | WhatsApp diagnostics and wiring |
| `browser` · `email-check` · `serve` · `worker` · `launchd` | services |

## Honesty and safety (enforced in code)
- **Resume guardrails** (`tailoring/guardrails.py`): rejected if it adds an unverified skill, a number or technology not in the source bullet, pads text, changes roles, or touches locked projects. Company, title, dates and project text are never LLM output.
- **Cover letters**: same checks plus a word limit.
- **Form answers**: fixed rules → your Q&A bank → Haiku (only from your data). Unknown answers are asked, never guessed.
- **Submitting**: only when you tap *Submit now* or set `auto_submit = true`. No account creation, no CAPTCHA solving: those are handed to you with a screenshot.
- **Claude subscription only**: `ANTHROPIC_API_KEY` is stripped, `--bare` is never used. Text calls run with `--safe-mode --tools ""`. The browser agent gets only Playwright MCP tools. `doctor` verifies the auth method.
- **Webhook**: every request is checked against your App secret (`X-Hub-Signature-256`); only your own number is answered.
- **Quota care**: free filters first, Haiku for cheap steps, one Claude call at a time, daily cap, 30-minute back-off with a WhatsApp alert.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Meta "callback URL couldn't be validated" | Server or tunnel not running, or verify token mismatch. The server log names the reason. |
| Dashboard *Test* arrives, real messages don't | App unpublished, WhatsApp account not subscribed (`wa-subscribe`), or the tunnel died (`tunnel-sync`). |
| `tunnel-sync`: "cloudflared lost its connection" | Mac slept. Restart cloudflared, run `tunnel-sync` again. |
| `wa-verify`: token type USER / expires | You saved a temporary token. Generate one on the system user with expiry Never. |
| LinkedIn link gives a login error | Sign in to LinkedIn inside Tengen Chrome (`tengen browser`). |

## Dev
```bash
uv run pytest
uv run ruff check src tests
```

## Changelog
- **1.1.0**: LinkedIn Easy Apply autopilot (pool harvester, job-page reader, guarded Easy Apply engine, crash-safe run controller, 30/day + 10/hour caps, review/auto modes), WhatsApp live dashboard and run commands, kill switches and command-channel health, `resume set` with profile matching, experience engine (`[experience]`). Fix: the Q&A bank no longer reuses answers across questions that differ in numbers or skills.
- **1.0.1**: WhatsApp diagnostics (`wa-verify`, `wa-subscribe`, `tunnel-sync`), webhook accepts root/trailing-slash URLs and logs rejection reasons, secret validation on `secrets set`, clearer skip-reason note on cards, ATS coverage counts the headline, project-local data folder, privacy policy.
- **1.0.0**: full pipeline: discovery, intake by link, analysis, guarded tailoring, cover letters, form filling with Claude fallback, Q&A over WhatsApp, reply tracking.
