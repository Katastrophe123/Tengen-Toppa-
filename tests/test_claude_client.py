from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import BaseModel

from tengen_toppa.llm import claude_client
from tengen_toppa.llm.claude_client import ClaudeAuthError, ClaudeError, ClaudeQuotaExhausted, ask


class Out(BaseModel):
    skills: list[str]


class FakeRun:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout, self.returncode = stdout, returncode
        self.calls: list[dict] = []

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": cmd, **kw})
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


@pytest.fixture
def fake(monkeypatch):
    def install(payload, returncode=0):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        f = FakeRun(stdout, returncode)
        monkeypatch.setattr(claude_client.subprocess, "run", f)
        monkeypatch.setattr(claude_client, "claude_bin", lambda: "/usr/bin/claude")
        return f

    return install


def test_success_uses_subscription_safe_flags(fake, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-passed")
    f = fake(
        {"is_error": False, "subtype": "success", "structured_output": {"skills": ["Python"]}, "total_cost_usd": 0.002}
    )
    res = ask("do it", "payload", Out, system_prompt="analyze_job.md", model="haiku")

    assert res.value.skills == ["Python"] and res.est_cost_usd == 0.002
    call = f.calls[0]
    cmd = call["cmd"]
    assert "--bare" not in cmd  # bare mode would ignore the subscription
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--safe-mode" in cmd and "--json-schema" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["input"] == "payload"
    assert call["cwd"].name == "llm-sandbox"


@pytest.mark.parametrize(
    "result",
    ["Claude AI usage limit reached|1760000000", "You've hit your rate limit, resets 5pm"],
)
def test_quota_errors(fake, result):
    fake({"is_error": True, "subtype": "success", "result": result})
    with pytest.raises(ClaudeQuotaExhausted):
        ask("x", "y", Out, system_prompt="analyze_job.md")


def test_auth_error(fake):
    fake({"is_error": True, "subtype": "success", "result": "Invalid API key · Please run /login"})
    with pytest.raises(ClaudeAuthError):
        ask("x", "y", Out, system_prompt="analyze_job.md")


def test_non_json_and_missing_structured_output(fake):
    fake("boom", returncode=1)
    with pytest.raises(ClaudeError):
        ask("x", "y", Out, system_prompt="analyze_job.md")
    fake({"is_error": False, "subtype": "success", "result": "hi"})
    with pytest.raises(ClaudeError, match="no structured_output"):
        ask("x", "y", Out, system_prompt="analyze_job.md")


def test_schema_mismatch(fake):
    fake({"is_error": False, "subtype": "success", "structured_output": {"skills": "not-a-list"}})
    with pytest.raises(ClaudeError, match="validation"):
        ask("x", "y", Out, system_prompt="analyze_job.md")


def test_auth_method_check(monkeypatch, fake):
    f = fake({"loggedIn": True, "authMethod": "api_key"})
    with pytest.raises(ClaudeAuthError, match="api_key"):
        claude_client.assert_subscription_auth()
    f.stdout = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"})
    assert claude_client.assert_subscription_auth()["subscriptionType"] == "max"


def test_prompt_override(tengen_home):
    assert claude_client.prompt_path("tailor_resume.md").exists()
    override = tengen_home / "prompts" / "tailor_resume.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("custom")
    assert claude_client.prompt_path("tailor_resume.md") == override
