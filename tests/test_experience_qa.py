from __future__ import annotations

import pytest

from tengen_toppa.apply.fields import FormField
from tengen_toppa.config import ApplicantSettings, ExperienceSettings
from tengen_toppa.experience import experience_answer, pick_years_option, years_required
from tengen_toppa.qa import bank_answer, resolve, same_question, save_answer

EXP = ExperienceSettings()


# ---------- C1: the bank must never reuse an answer for a different question ----------


@pytest.mark.parametrize(
    "a, b",
    [
        ("Do you have 3+ years of experience with Python?", "Do you have 5+ years of experience with Python?"),
        (
            "How many years of experience do you have with Python?",
            "How many years of experience do you have with Java?",
        ),
        (
            "Are you comfortable working from office 5 days a week?",
            "Are you comfortable working from office 3 days a week?",
        ),
        ("Do you have experience with LangChain?", "Do you have experience with LangGraph?"),
        ("Are you willing to relocate to Pune?", "Are you willing to relocate to Bengaluru?"),
    ],
)
def test_different_questions_never_match(a, b):
    assert not same_question(a, b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("Are you willing to relocate?", "Are you willing to relocate"),
        ("What is your notice period?", "What is your current notice period?"),
        ("Do you have a valid passport?", "Do you have valid passport?"),
    ],
)
def test_same_question_rewordings_match(a, b):
    assert same_question(a, b)


def test_bank_uses_strict_matching(s):
    save_answer(s, "Are you comfortable working from office 5 days a week?", "No")
    assert bank_answer(s, "Are you comfortable working from office 5 days a week") == "No"
    assert bank_answer(s, "Are you comfortable working from office 3 days a week?") is None


# ---------- experience answers come only from stated years ----------


@pytest.mark.parametrize(
    "label, kind, options, expected",
    [
        ("How many years of experience do you have with Python?", "text", [], "1"),
        ("How many years of work experience do you have with LangChain?", "number", [], "1"),
        ("How many years of total experience do you have?", "text", [], "1"),
        ("Do you have experience with RAG?", "radio", ["Yes", "No"], "Yes"),  # stated AI experience
        ("Do you have 1+ years of experience with Python?", "radio", ["Yes", "No"], "Yes"),
        ("Do you have at least 3 years of experience with Python?", "radio", ["Yes", "No"], "No"),
        ("Do you have 5+ years of experience in machine learning?", "select", ["Yes", "No"], "No"),
        (
            "Years of experience with PyTorch",
            "select",
            ["0-1 years", "1-3 years", "3-5 years", "5+ years"],
            "0-1 years",
        ),
    ],
)
def test_experience_answers(master, label, kind, options, expected):
    ea = experience_answer(label, kind=kind, options=options, profile=master, exp=EXP)
    assert ea.applies and ea.answer == expected


@pytest.mark.parametrize(
    "label",
    [
        "How many years of experience do you have with Kubernetes?",
        "Do you have experience with SAP ABAP?",
        "How many years of experience do you have in Salesforce?",
    ],
)
def test_unstated_skills_are_asked_not_guessed(master, label):
    ea = experience_answer(label, kind="text", options=[], profile=master, exp=EXP)
    assert ea.applies and ea.answer is None


def test_non_experience_question_is_untouched(master):
    assert not experience_answer(
        "What is your notice period?", kind="text", options=[], profile=master, exp=EXP
    ).applies


def test_resolve_never_sends_experience_to_llm(s, master, monkeypatch):
    seen: list[str] = []

    def fake_llm(fields, **kw):
        seen.extend(f.label for f in fields)
        return {}

    monkeypatch.setattr("tengen_toppa.qa.llm_answers", fake_llm)
    fields = [
        FormField("a", "How many years of experience do you have with Kubernetes?", "text", True),
        FormField("b", "Why do you want to join us?", "textarea", True),
        FormField("c", "How many years of experience do you have with Python?", "text", True),
    ]
    res = resolve(s, fields, profile=master, applicant=ApplicantSettings(), experience=EXP)
    assert res.answers == {"c": "1"}
    assert seen == ["Why do you want to join us?"]
    assert {f.key for f in res.missing_required} == {"a", "b"}


def test_answered_unstated_skill_is_reused_only_for_that_skill(s, master):
    save_answer(s, "How many years of experience do you have with Kubernetes?", "0")
    fields = [
        FormField("a", "How many years of experience do you have with Kubernetes?", "text", True),
        FormField("b", "How many years of experience do you have with Terraform?", "text", True),
    ]
    res = resolve(s, fields, profile=master, applicant=ApplicantSettings(), use_llm=False, experience=EXP)
    assert res.answers == {"a": "0"} and [f.key for f in res.missing_required] == ["b"]


# ---------- job requirement parsing ----------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Candidate Specification: 5+ years of Experience", 5),
        ("We need 1+ years of experience with Python", 1),
        ("Experience: 0-2 years", 0),
        ("2 to 4 years of relevant experience in ML", 2),
        ("Minimum 3 years experience", 3),
        ("Experience of at least 2 years", 2),
        ("Freshers welcome!", 0),
        ("Build LLM apps with our team.", None),
        ("Senior role: 7+ yrs experience; or 3 years with a PhD", 7),
    ],
)
def test_years_required(text, expected):
    assert years_required(text) == expected


def test_pick_years_option():
    assert pick_years_option(["Less than 1 year", "1-2 years", "3+ years"], 1) == "1-2 years"
    assert pick_years_option(["0", "1", "2", "3+"], 1) == "1"
