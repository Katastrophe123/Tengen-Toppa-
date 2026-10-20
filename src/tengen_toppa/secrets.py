"""Secrets live in the macOS Keychain (service 'tengen-toppa'), never in files."""

from __future__ import annotations

import os
from enum import StrEnum

import keyring

SERVICE = "tengen-toppa"


class Secret(StrEnum):
    CLAUDE_OAUTH_TOKEN = "claude_oauth_token"  # optional: from `claude setup-token`, for launchd
    WA_ACCESS_TOKEN = "wa_access_token"  # Meta System User permanent token
    WA_PHONE_NUMBER_ID = "wa_phone_number_id"
    WA_APP_SECRET = "wa_app_secret"  # for X-Hub-Signature-256 verification
    WA_VERIFY_TOKEN = "wa_verify_token"  # any random string you also paste into Meta dashboard
    WA_MY_NUMBER = "wa_my_number"  # your number, digits only with country code e.g. 919876543210
    EMAIL_APP_PASSWORD = "email_app_password"  # IMAP app password for reply tracking


class MissingSecret(RuntimeError):
    pass


def get_secret(name: Secret) -> str | None:
    """Env var TENGEN_<NAME> overrides Keychain (useful for tests)."""
    env = os.environ.get(f"TENGEN_{name.name}")
    if env:
        return env
    return keyring.get_password(SERVICE, name.value)


def require_secret(name: Secret) -> str:
    value = get_secret(name)
    if not value:
        raise MissingSecret(f"Missing secret '{name.value}'. Run: tengen secrets set {name.value}")
    return value


def set_secret(name: Secret, value: str) -> None:
    keyring.set_password(SERVICE, name.value, value)
