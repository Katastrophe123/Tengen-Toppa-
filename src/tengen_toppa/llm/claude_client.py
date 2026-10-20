"""The ONLY LLM entry point: shells out to the official `claude` CLI using your Claude subscription.

Rules enforced here:
- ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are stripped so calls never fall back to API billing.
- Never uses --bare (bare mode ignores subscription login).
- --safe-mode + empty sandbox cwd: your CLAUDE.md, hooks, plugins and MCP servers don't leak in.
- --tools "": pure text-in / JSON-out, Claude cannot touch files or the shell.
- One call at a time (process-wide lock) so the pipeline never hogs your limits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, ValidationError

from tengen_toppa.paths import get_paths
from tengen_toppa.secrets import Secret, get_secret

_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
_SUBSCRIPTION_AUTH = {"claude.ai", "oauth_token"}
_QUOTA_MARKERS = ("usage limit", "rate limit", "limit reached", "rate_limit", "out of extra usage")
_LOCK = threading.Lock()


class ClaudeError(RuntimeError):
    pass


class ClaudeQuotaExhausted(ClaudeError):
    """Subscription limit hit. Callers should pause and retry later."""


class ClaudeAuthError(ClaudeError):
    pass


@dataclass(frozen=True)
class CallResult[T: BaseModel]:
    value: T
    duration_ms: int
    est_cost_usd: float | None


def _env() -> dict[str, str]:
    import os

    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    token = get_secret(Secret.CLAUDE_OAUTH_TOKEN)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def claude_bin() -> str:
    path = shutil.which("claude")
    if not path:
        # launchd has a minimal PATH; check common install locations
        for cand in (
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
            str(Path.home() / ".local/bin/claude"),
        ):
            if Path(cand).exists():
                return cand
        raise ClaudeError("`claude` CLI not found. Install Claude Code and log in with your subscription.")
    return path


def prompt_path(name: str) -> Path:
    """User override in ~/TengenToppa/prompts/<name> wins over the packaged prompt."""
    override = get_paths().prompts_override / name
    if override.exists():
        return override
    return Path(str(resources.files("tengen_toppa.prompts").joinpath(name)))


def auth_status() -> dict:
    proc = subprocess.run([claude_bin(), "auth", "status"], capture_output=True, text=True, env=_env(), timeout=30)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeAuthError(f"Could not read `claude auth status`: {proc.stderr or proc.stdout}") from e


def assert_subscription_auth() -> dict:
    status = auth_status()
    method = status.get("authMethod")
    if not status.get("loggedIn") or method not in _SUBSCRIPTION_AUTH:
        raise ClaudeAuthError(
            f"Claude Code auth method is {method!r}; expected subscription login (claude.ai/oauth_token). "
            "Run `claude auth login` (without --console)."
        )
    return status


def _looks_like_quota(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _QUOTA_MARKERS)


def ask[T: BaseModel](
    instruction: str,
    payload: str,
    schema: type[T],
    *,
    system_prompt: str,
    model: str = "sonnet",
    timeout: int = 300,
) -> CallResult[T]:
    """Pure text-in / JSON-out call. `payload` goes via stdin, `instruction` as the prompt arg."""
    isolation = ["--safe-mode", "--tools", "", "--permission-mode", "dontAsk", "--max-turns", "4"]
    return _run(instruction, payload, schema, system_prompt, model, timeout, isolation)


def run_browser_agent[T: BaseModel](
    instruction: str,
    payload: str,
    schema: type[T],
    *,
    system_prompt: str,
    mcp_config: Path,
    model: str = "sonnet",
    max_turns: int = 40,
    timeout: int = 900,
) -> CallResult[T]:
    """Agent call whose ONLY tools are the Playwright MCP browser tools.

    --safe-mode would also disable --mcp-config servers, so isolation here comes from
    --setting-sources project (sandbox dir has no project settings: your user settings, hooks and
    plugins don't load), --strict-mcp-config, and --tools "" (no shell/file tools).
    """
    isolation = [
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--tools",
        "",
        "--allowedTools",
        "mcp__playwright__*",
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        str(max_turns),
    ]
    return _run(instruction, payload, schema, system_prompt, model, timeout, isolation)


def _run[T: BaseModel](
    instruction: str,
    payload: str,
    schema: type[T],
    system_prompt: str,
    model: str,
    timeout: int,
    isolation: list[str],
) -> CallResult[T]:
    sandbox = get_paths().llm_sandbox
    sandbox.mkdir(parents=True, exist_ok=True)
    cmd = [
        claude_bin(),
        "-p",
        instruction,
        "--model",
        model,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema.model_json_schema()),
        "--system-prompt-file",
        str(prompt_path(system_prompt)),
        "--no-session-persistence",
        *isolation,
    ]
    started = time.monotonic()
    with _LOCK:
        try:
            proc = subprocess.run(
                cmd, input=payload, capture_output=True, text=True, cwd=sandbox, env=_env(), timeout=timeout
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"claude -p timed out after {timeout}s") from e
    duration_ms = int((time.monotonic() - started) * 1000)

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        blob = (proc.stdout + proc.stderr)[-2000:]
        if _looks_like_quota(blob):
            raise ClaudeQuotaExhausted(blob) from e
        raise ClaudeError(f"Non-JSON output from claude (exit {proc.returncode}): {blob}") from e

    if data.get("is_error") or data.get("subtype") != "success":
        msg = str(data.get("result") or data.get("subtype") or data)
        if _looks_like_quota(msg):
            raise ClaudeQuotaExhausted(msg)
        if "login" in msg.lower() or "auth" in msg.lower():
            raise ClaudeAuthError(msg)
        raise ClaudeError(msg[:2000])

    raw = data.get("structured_output")
    if raw is None:
        raise ClaudeError("claude returned no structured_output")
    try:
        value = schema.model_validate(raw)
    except ValidationError as e:
        raise ClaudeError(f"structured_output failed validation: {e}") from e
    return CallResult(value=value, duration_ms=duration_ms, est_cost_usd=data.get("total_cost_usd"))
