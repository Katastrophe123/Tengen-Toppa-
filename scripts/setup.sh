#!/usr/bin/env bash
# One-time setup for Tengen-Toppa on macOS. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null 2>&1; }

echo "==> Checking tools"
need brew || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
need uv || brew install uv
need node || brew install node                 # npx runs the Playwright MCP server for the Claude fallback
need cloudflared || brew install cloudflared   # exposes the WhatsApp webhook
need claude || { echo "Install Claude Code and log in with your subscription: https://claude.com/claude-code"; exit 1; }

echo "==> Python deps (with local embeddings)"
uv sync --extra embed

if [ ! -d "/Applications/Google Chrome.app" ]; then
  echo "==> Google Chrome not found, installing Playwright Chromium instead (set [apply].browser = \"chromium\")"
  uv run playwright install chromium
fi

echo "==> Project folders, settings and profile"
uv run tengen init

echo "==> Claude subscription check"
claude auth status | grep -q '"authMethod": "claude.ai"\|"authMethod": "oauth_token"' \
  && echo "Claude subscription login OK" \
  || echo "WARNING: run 'claude auth login' (subscription, not --console)"

cat <<'EOF'

Next steps:
  1. Edit data/profile/master_profile.json  (your real resume facts + bullet pool)
  2. Edit config/settings.toml              (boards, titles, locations, [applicant] facts)
  3. WhatsApp secrets:  uv run tengen secrets set wa_access_token   (and the other wa_* names)
  4. uv run tengen doctor
  5. uv run tengen browser    -> log in to LinkedIn / Naukri once in the Tengen Chrome window
  6. uv run tengen run --tailor-limit 3
  7. Always-on: uv run tengen launchd --tunnel-name <your-tunnel>
EOF
