from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from tengen_toppa.config import Settings
from tengen_toppa.linkedin.pool import card_reason, fingerprint, ingest_cards, pool_size
from tengen_toppa.linkedin.search import parse_cards, search_url
from tengen_toppa.store.models import LiJob, LiState

# Shapes recorded from the live LinkedIn search page (read-only probe)
RAW = [
    {
        "id": "4471544631",
        "rendered": True,
        "title": "AI Engineer - GO/JC/4594/2026",
        "company": "Golden Opportunities",
        "caption": "Bhubaneswar, Odisha, India (On-site)",
        "footer": ["3 hours ago\nWithin the past 24 hours", "Easy Apply"],
        "text": "AI Engineer - GO/JC/4594/2026 | Golden Opportunities | Easy Apply",
    },
    {
        "id": "4470910379",
        "rendered": True,
        "title": "Software Engineer - AI - Innovation Engineering [T500-29587]",
        "company": "Costco IT",
        "caption": "Hyderabad, Telangana, India (On-site)",
        "footer": ["2 hours ago", "Easy Apply"],
        "text": "...",
    },
    {
        "id": "4455232678",
        "rendered": True,
        "title": "Principal Staff Software Engineer, Linkedin Learning Solutions",
        "company": "LinkedIn",
        "caption": "Bengaluru, Karnataka, India (Hybrid)",
        "footer": ["5 hours ago", "Easy Apply"],
        "text": "...",
    },
    {
        "id": "1",
        "rendered": True,
        "title": "LLM Engineer",
        "company": "Acme",
        "caption": "Gurugram, Haryana, India (Remote)",
        "footer": ["Applied"],
        "text": "LLM Engineer | Applied",
    },
    {"id": "2", "rendered": False, "title": "", "company": "", "caption": "", "footer": [], "text": ""},
    {
        "id": "3",
        "rendered": True,
        "title": "SF Data Cloud Data Consultant\nSF Data Cloud Data Consultant",
        "company": "Customertimes",
        "caption": "India (Remote)",
        "footer": ["1 day ago", "Easy Apply"],
        "text": "",
    },
]


def test_search_url_has_easy_apply_and_filters():
    cfg = Settings().linkedin
    q = parse_qs(urlparse(search_url(cfg, "AI Engineer", "India", posted_within_seconds=86400, start=50)).query)
    assert q["f_AL"] == ["true"] and q["f_TPR"] == ["r86400"] and q["start"] == ["50"]
    assert q["f_E"] == ["2,3"]  # entry + associate (1+ years)
    assert q["sortBy"] == ["DD"] and q["keywords"] == ["AI Engineer"]


def test_parse_cards_from_live_shapes():
    cards = {c.job_id: c for c in parse_cards(RAW)}
    assert "2" not in cards  # not rendered yet
    c = cards["4471544631"]
    assert (c.title, c.company, c.location, c.workplace, c.easy_apply) == (
        "AI Engineer - GO/JC/4594/2026",
        "Golden Opportunities",
        "Bhubaneswar, Odisha, India",
        "on-site",
        True,
    )
    assert cards["1"].applied and cards["3"].title == "SF Data Cloud Data Consultant"


def test_card_filters():
    cfg = Settings()
    cards = {c.job_id: c for c in parse_cards(RAW)}
    assert card_reason(cards["4471544631"], cfg) is None
    assert card_reason(cards["4470910379"], cfg) is None
    assert "senior title" in card_reason(cards["4455232678"], cfg) or "excluded" in card_reason(
        cards["4455232678"], cfg
    )
    assert card_reason(cards["1"], cfg) == "already applied"
    assert card_reason(cards["3"], cfg) == "title not relevant"


def test_ingest_pool_and_dedupe_reposts(s):
    cfg = Settings()
    cards = parse_cards(RAW)
    res = ingest_cards(s, cards, cfg, search="AI Engineer @ India")
    assert res.new == 5 and res.eligible_new == 2 and pool_size(s) == 2
    assert s.get(LiJob, "1").state == LiState.APPLIED
    # the same role re-posted under a new id (and another requisition code) is a duplicate
    repost = dict(RAW[0], id="999", title="AI Engineer - GO/JC/9999/2026")
    res2 = ingest_cards(s, parse_cards([repost]), cfg, search="x")
    assert res2.eligible_new == 0 and "duplicate of 4471544631" in s.get(LiJob, "999").skip_reason
    # seen again: no duplicate rows
    assert ingest_cards(s, cards, cfg, search="x").new == 0


def test_fingerprint_ignores_requisition_codes():
    assert fingerprint("Costco IT", "Software Engineer - AI [T500-29587]") == fingerprint(
        "Costco IT", "Software Engineer - AI [T500-1]"
    )
