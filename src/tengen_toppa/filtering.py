"""Free filters that run before any Claude call, to protect subscription limits."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from tengen_toppa.config import SearchSettings
from tengen_toppa.discovery.base import JobPosting
from tengen_toppa.paths import get_paths
from tengen_toppa.profile import MasterProfile

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterResult:
    ok: bool
    reason: str | None = None


def _contains_any(haystack: str, needles: Sequence[str]) -> str | None:
    low = haystack.lower()
    for n in needles:
        if re.search(rf"(?<!\w){re.escape(n.lower())}(?!\w)", low):
            return n
    return None


def hard_filter(job: JobPosting, cfg: SearchSettings) -> FilterResult:
    if _contains_any(job.company, cfg.company_blacklist):
        return FilterResult(False, f"blacklisted company: {job.company}")
    if bad := _contains_any(job.title, cfg.title_exclude):
        return FilterResult(False, f"title excluded by '{bad}'")
    if cfg.title_include and not _contains_any(job.title, cfg.title_include):
        return FilterResult(False, "title matches none of title_include")
    loc_ok = not cfg.locations or bool(_contains_any(job.location, cfg.locations))
    remote_ok = cfg.allow_remote and job.remote is True
    if not (loc_ok or remote_ok):
        return FilterResult(False, f"location '{job.location}' not allowed")
    return FilterResult(True)


# ---------- prefilter scoring (local, free) ----------


def skill_coverage(text: str, skills: Sequence[str]) -> float:
    """Fraction of your verified skills mentioned in the JD (cheap relevance signal)."""
    if not skills:
        return 0.0
    hits = sum(1 for s in skills if _contains_any(text, [s]))
    return hits / len(skills)


class Embedder:
    """fastembed if installed (`uv sync --extra embed`), else None and we fall back to lexical scoring."""

    _model = None
    _failed = False

    @classmethod
    def get(cls):
        if cls._model is None and not cls._failed:
            try:
                from fastembed import TextEmbedding

                cache = get_paths().home / "models"
                cache.mkdir(parents=True, exist_ok=True)
                cls._model = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=str(cache))
            except Exception as e:  # optional dependency / model download issue
                log.info("embeddings unavailable, using lexical prefilter: %s", e)
                cls._failed = True
        return cls._model


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def prefilter_scores(jobs: Sequence[JobPosting], profile: MasterProfile) -> list[float]:
    """0..1 relevance per job. Blend of embedding similarity (if available) and skill coverage."""
    lexical = [skill_coverage(f"{j.title}\n{j.description}", profile.skills_verified) for j in jobs]
    model = Embedder.get()
    if model is None or not jobs:
        return lexical
    profile_text = profile.compact_for_llm()
    docs = [profile_text] + [f"{j.title}\n{j.description[:4000]}" for j in jobs]
    vecs = [list(v) for v in model.embed(docs)]
    sims = [_cosine(vecs[0], v) for v in vecs[1:]]
    return [0.6 * s + 0.4 * lex for s, lex in zip(sims, lexical, strict=True)]
