"""Normalized job posting + helpers shared by all sources."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr)[^>]*>", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class JobPosting:
    source: str
    external_id: str
    company: str
    title: str
    location: str
    remote: bool | None
    url: str
    apply_url: str
    description: str
    posted_at: datetime | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.company, self.title, self.location)


def fingerprint(company: str, title: str, location: str) -> str:
    norm = "|".join(re.sub(r"\W+", " ", x).strip().lower() for x in (company, title, location))
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def html_to_text(raw: str) -> str:
    text = html.unescape(raw or "")  # Greenhouse double-escapes
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.splitlines())
    return _NL_RE.sub("\n\n", text).strip()


def parse_dt(value: str | int | float | None) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, int | float):  # Lever: epoch millis
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OSError):
        return None


def looks_remote(*texts: str) -> bool | None:
    joined = " ".join(t for t in texts if t).lower()
    if not joined:
        return None
    if "remote" in joined or "anywhere" in joined:
        return True
    return None
