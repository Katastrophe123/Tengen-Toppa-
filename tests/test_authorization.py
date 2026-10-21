"""Work-authorization answers: per-country, never guessed."""

from tengen_toppa.authorization import authorization_answer, find_country, job_country
from tengen_toppa.config import ApplicantSettings

AP = ApplicantSettings(work_authorized_countries=["India"])


def test_find_country_aliases():
    assert find_country("Are you authorized to work in the US?") == "United States"
    assert find_country("work permit for Germany") == "Germany"
    assert find_country("join us today") is None  # "us" as a pronoun


def test_job_country_from_linkedin_location():
    assert job_country("Stockholm, Sweden") == "Sweden"
    assert job_country("Austin, TX") == "United States"
    assert job_country("Gurugram, Haryana, India") == "India"
    assert job_country("Remote") is None


def test_authorized_only_for_india():
    a = authorization_answer("Are you authorized to work in India?", job_location="", applicant=AP)
    assert (a.applies, a.answer) == (True, "Yes")
    b = authorization_answer("Are you authorized to work in Germany?", job_location="", applicant=AP)
    assert (b.applies, b.answer) == (True, "No")
    c = authorization_answer("Are you authorized to work here?", job_location="Stockholm, Sweden", applicant=AP)
    assert (c.applies, c.answer) == (True, "No")


def test_sponsorship_questions():
    a = authorization_answer("Will you require visa sponsorship?", job_location="London, United Kingdom", applicant=AP)
    assert (a.applies, a.answer) == (True, "Yes")  # not authorized there -> always yes
    b = authorization_answer("Will you require visa sponsorship?", job_location="Bengaluru, India", applicant=AP)
    assert (b.applies, b.answer) == (True, "No")  # authorized -> your stated flag


def test_unclear_country_is_not_guessed():
    a = authorization_answer("Are you authorized to work?", job_location="Remote", applicant=AP)
    assert a.applies and a.answer is None  # asked on WhatsApp, never invented


def test_non_auth_question_passes_through():
    a = authorization_answer("How many years of Python?", job_location="", applicant=AP)
    assert not a.applies


def test_eu_shorthand():
    eu = ApplicantSettings(work_authorized_countries=["European Union"])
    a = authorization_answer("Are you authorized to work in Spain?", job_location="", applicant=eu)
    assert a.answer == "Yes"
