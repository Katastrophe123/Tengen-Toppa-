from __future__ import annotations

import httpx
import respx

from tengen_toppa.config import SearchSettings
from tengen_toppa.discovery.base import JobPosting, fingerprint, html_to_text
from tengen_toppa.discovery.sources import fetch_all
from tengen_toppa.filtering import hard_filter, skill_coverage

GH = {
    "jobs": [
        {
            "id": 101,
            "title": "Backend Software Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            "location": {"name": "Bengaluru, India"},
            "content": "&lt;p&gt;We use &lt;b&gt;Python&lt;/b&gt; &amp;amp; FastAPI.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;APIs&lt;/li&gt;&lt;/ul&gt;",
            "updated_at": "2026-10-01T10:00:00-04:00",
            "company_name": "Acme",
        }
    ]
}
LEVER = [
    {
        "id": "abc",
        "text": "Python Engineer",
        "hostedUrl": "https://jobs.lever.co/beta/abc",
        "applyUrl": "https://jobs.lever.co/beta/abc/apply",
        "categories": {"location": "Remote"},
        "workplaceType": "remote",
        "descriptionPlain": "Build things.",
        "lists": [{"text": "Requirements", "content": "<li>Python</li><li>Postgres</li>"}],
        "createdAt": 1760000000000,
    }
]
ASHBY = {
    "jobs": [
        {
            "id": "x1",
            "title": "Software Engineer, Platform",
            "location": "Bangalore",
            "isRemote": False,
            "jobUrl": "https://jobs.ashbyhq.com/gamma/x1",
            "applyUrl": "https://jobs.ashbyhq.com/gamma/x1/application",
            "descriptionPlain": "Platform work with Go.",
            "publishedAt": "2026-10-02T00:00:00.000Z",
        },
        {"id": "x2", "title": "Hidden", "isListed": False},
    ]
}


@respx.mock
def test_fetch_all_normalises_three_sources_and_isolates_failures():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(return_value=httpx.Response(200, json=GH))
    respx.get("https://boards-api.greenhouse.io/v1/boards/broken/jobs").mock(return_value=httpx.Response(404))
    respx.get("https://api.lever.co/v0/postings/beta").mock(return_value=httpx.Response(200, json=LEVER))
    respx.get("https://api.ashbyhq.com/posting-api/job-board/gamma").mock(return_value=httpx.Response(200, json=ASHBY))

    postings, errors = fetch_all(["acme", "broken"], ["beta"], ["gamma"])

    assert len(errors) == 1 and "broken" in errors[0]
    by_source = {p.source: p for p in postings}
    assert set(by_source) == {"greenhouse", "lever", "ashby"}
    gh = by_source["greenhouse"]
    assert gh.external_id == "acme:101" and gh.company == "Acme"
    assert "Python & FastAPI." in gh.description and "<" not in gh.description
    lv = by_source["lever"]
    assert lv.remote is True and lv.apply_url.endswith("/apply")
    assert "Requirements" in lv.description and "Postgres" in lv.description
    assert len([p for p in postings if p.source == "ashby"]) == 1  # unlisted skipped


def test_html_to_text_and_fingerprint():
    assert html_to_text("<p>Hi&nbsp;there</p><p>Line 2</p>") == "Hi\xa0there\n\nLine 2"
    assert fingerprint("Acme", "Backend Engineer!", "Remote") == fingerprint("acme ", "backend engineer", "remote")


def _p(title="Backend Engineer", company="Acme", location="Bengaluru, India", remote=None):
    return JobPosting("greenhouse", "1", company, title, location, remote, "u", "u", "desc")


def test_hard_filter_rules():
    cfg = SearchSettings(
        title_include=["engineer"],
        title_exclude=["intern", "manager"],
        locations=["india"],
        allow_remote=True,
        company_blacklist=["EvilCorp"],
    )
    assert hard_filter(_p(), cfg).ok
    assert not hard_filter(_p(title="Engineering Manager"), cfg).ok
    assert not hard_filter(_p(title="Software Engineer Intern"), cfg).ok
    assert not hard_filter(_p(title="Designer"), cfg).ok
    assert not hard_filter(_p(company="EvilCorp"), cfg).ok
    assert not hard_filter(_p(location="Berlin"), cfg).ok
    assert hard_filter(_p(location="Berlin", remote=True), cfg).ok
    # "intern" must not match "internal"
    assert hard_filter(_p(title="Engineer, Internal Tools"), cfg).ok


def test_skill_coverage():
    assert skill_coverage("We need Python and PostgreSQL", ["Python", "PostgreSQL", "Go", "AWS"]) == 0.5
