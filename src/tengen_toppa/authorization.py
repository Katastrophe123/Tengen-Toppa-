"""Work authorization / visa sponsorship answers, per country.

"Are you authorized to work in X?" is only true for the countries in
[applicant] work_authorized_countries. The country comes from the question itself, else from the job's
location. If neither names a country, we don't answer; the job is skipped rather than guessed, and
these answers are never reused from the Q&A bank (the same wording means different things per job).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tengen_toppa.apply.fields import normalize_question
from tengen_toppa.config import ApplicantSettings

AUTH_RE = re.compile(
    r"\b(legally )?(authori[sz]ed|eligible|permitted|allowed|right)\b.{0,40}\bwork\b"
    r"|\bwork (permit|authori[sz]ation|visa)\b|\bright to work\b|\bwork legally\b",
    re.I,
)
SPONSOR_RE = re.compile(r"\bsponsor|\bvisa\b.{0,30}\b(require|need)|\b(require|need)\w*\b.{0,30}\bvisa\b", re.I)

# alias -> canonical country
COUNTRIES: dict[str, str] = {
    "india": "India",
    "indian": "India",
    "united states": "United States",
    "united states of america": "United States",
    "usa": "United States",
    "us": "United States",
    "u s": "United States",
    "america": "United States",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "u k": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "canada": "Canada",
    "australia": "Australia",
    "new zealand": "New Zealand",
    "germany": "Germany",
    "france": "France",
    "netherlands": "Netherlands",
    "ireland": "Ireland",
    "spain": "Spain",
    "portugal": "Portugal",
    "italy": "Italy",
    "sweden": "Sweden",
    "norway": "Norway",
    "denmark": "Denmark",
    "finland": "Finland",
    "poland": "Poland",
    "switzerland": "Switzerland",
    "austria": "Austria",
    "belgium": "Belgium",
    "hungary": "Hungary",
    "croatia": "Croatia",
    "romania": "Romania",
    "czech republic": "Czech Republic",
    "czechia": "Czech Republic",
    "greece": "Greece",
    "european union": "European Union",
    "eu": "European Union",
    "eea": "European Union",
    "singapore": "Singapore",
    "malaysia": "Malaysia",
    "japan": "Japan",
    "south korea": "South Korea",
    "china": "China",
    "hong kong": "Hong Kong",
    "philippines": "Philippines",
    "indonesia": "Indonesia",
    "vietnam": "Vietnam",
    "thailand": "Thailand",
    "united arab emirates": "United Arab Emirates",
    "uae": "United Arab Emirates",
    "dubai": "United Arab Emirates",
    "saudi arabia": "Saudi Arabia",
    "qatar": "Qatar",
    "israel": "Israel",
    "turkey": "Turkey",
    "egypt": "Egypt",
    "south africa": "South Africa",
    "nigeria": "Nigeria",
    "kenya": "Kenya",
    "brazil": "Brazil",
    "mexico": "Mexico",
    "argentina": "Argentina",
    "chile": "Chile",
    "colombia": "Colombia",
    "pakistan": "Pakistan",
    "bangladesh": "Bangladesh",
    "sri lanka": "Sri Lanka",
    "nepal": "Nepal",
}
EU = {
    "Germany",
    "France",
    "Netherlands",
    "Ireland",
    "Spain",
    "Portugal",
    "Italy",
    "Sweden",
    "Denmark",
    "Finland",
    "Poland",
    "Austria",
    "Belgium",
    "Hungary",
    "Croatia",
    "Romania",
    "Czech Republic",
    "Greece",
}
US_STATES = {
    "al",
    "ak",
    "az",
    "ar",
    "ca",
    "co",
    "ct",
    "de",
    "fl",
    "ga",
    "hi",
    "id",
    "il",
    "in",
    "ia",
    "ks",
    "ky",
    "la",
    "me",
    "md",
    "ma",
    "mi",
    "mn",
    "ms",
    "mo",
    "mt",
    "ne",
    "nv",
    "nh",
    "nj",
    "nm",
    "ny",
    "nc",
    "nd",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "vt",
    "va",
    "wa",
    "wv",
    "wi",
    "wy",
    "dc",
}


def find_country(text: str) -> str | None:
    norm = f" {normalize_question(text)} "
    for alias in sorted(COUNTRIES, key=len, reverse=True):
        if f" {alias} " in norm:
            # "us" as a pronoun ("join us", "contact us") is not the United States
            if alias == "us" and not re.search(r"\b(in|the|within) us\b|\bus based\b|\bu\.s\.", text, re.I):
                continue
            return COUNTRIES[alias]
    return None


def job_country(location: str) -> str | None:
    """LinkedIn locations: 'Stockholm, Sweden', 'Austin, TX', 'Gurugram, Haryana, India', 'United States'."""
    if not location:
        return None
    parts = [p.strip() for p in location.split(",") if p.strip()]
    for part in reversed(parts):
        if part.lower() in US_STATES and len(parts) >= 2:
            return "United States"
        if c := find_country(part):
            return c
    return None


def _authorized(country: str, applicant: ApplicantSettings) -> bool:
    allowed = {find_country(c) or c for c in applicant.work_authorized_countries}
    if country == "European Union":
        return "European Union" in allowed
    if country in EU and "European Union" in allowed:
        return True
    return country in allowed


@dataclass(frozen=True)
class AuthAnswer:
    applies: bool
    answer: str | None  # None + applies => skip this job (country unclear)
    country: str | None = None


def authorization_answer(label: str, *, job_location: str, applicant: ApplicantSettings) -> AuthAnswer:
    is_sponsor = bool(SPONSOR_RE.search(label))
    is_auth = bool(AUTH_RE.search(label) or AUTH_RE.search(normalize_question(label)))
    if not (is_sponsor or is_auth):
        return AuthAnswer(False, None)
    country = find_country(label) or job_country(job_location)
    if country is None:
        return AuthAnswer(True, None)
    ok = _authorized(country, applicant)
    if is_sponsor:
        needs = applicant.requires_sponsorship if ok else True
        return AuthAnswer(True, "Yes" if needs else "No", country)
    return AuthAnswer(True, "Yes" if (ok and applicant.work_authorized) else "No", country)
