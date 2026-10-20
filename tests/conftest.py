from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Must be set before any tengen_toppa import (the Huey queue path is computed at import time).
os.environ["TENGEN_HOME"] = tempfile.mkdtemp(prefix="tengen-test-")
for _k in list(os.environ):
    if _k.startswith("TENGEN_") and _k != "TENGEN_HOME":
        del os.environ[_k]

import pytest

from tengen_toppa.config import settings
from tengen_toppa.profile import SAMPLE_PROFILE, MasterProfile
from tengen_toppa.store import db
from tengen_toppa.whatsapp import notify


@pytest.fixture(autouse=True)
def tengen_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TENGEN_HOME", str(tmp_path))
    for name in (
        "WA_ACCESS_TOKEN",
        "WA_PHONE_NUMBER_ID",
        "WA_MY_NUMBER",
        "WA_APP_SECRET",
        "WA_VERIFY_TOKEN",
        "CLAUDE_OAUTH_TOKEN",
    ):
        monkeypatch.setenv(f"TENGEN_{name}", "")
    monkeypatch.setattr("tengen_toppa.secrets.keyring.get_password", lambda *_a: None)
    db.engine.cache_clear()
    settings.cache_clear()
    notify.set_client(None)

    from tengen_toppa.pipeline import tasks

    tasks.huey.immediate = True

    prof_path = tmp_path / "data" / "profile" / "master_profile.json"
    prof_path.parent.mkdir(parents=True)
    prof_path.write_text(json.dumps(SAMPLE_PROFILE))
    yield tmp_path
    db.engine.cache_clear()
    notify.set_client(None)


@pytest.fixture
def master() -> MasterProfile:
    return MasterProfile.model_validate(SAMPLE_PROFILE)


@pytest.fixture
def s():
    with db.session() as sess:
        yield sess
