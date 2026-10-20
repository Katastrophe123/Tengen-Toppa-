from __future__ import annotations

import pytest

from tengen_toppa.profile import MasterProfile
from tengen_toppa.tailoring.guardrails import has_term, numbers, validate
from tengen_toppa.tailoring.schema import TailoredBullet, TailoredResume, TailoredRole

JD_TERMS = ["Kubernetes", "Go", "Python", "FastAPI", "PostgreSQL", "Kafka"]


def good(master: MasterProfile) -> TailoredResume:
    return TailoredResume(
        summary="Backend engineer with 3 years building Python and FastAPI services.",
        skills=["Python", "FastAPI", "PostgreSQL", "AWS"],
        experience=[
            TailoredRole(
                experience_id="acme",
                bullets=[
                    TailoredBullet(
                        source_id="acme-1",
                        text="Built a high-throughput FastAPI payments service serving 2M requests/day "
                        "at p99 under 120 ms.",
                    ),
                    TailoredBullet(
                        source_id="acme-2",
                        text="Reduced AWS costs 30% via spot instances and PostgreSQL tuning.",
                    ),
                ],
            )
        ],
        project_order=["proj-1"],
        changes=["Led with scale bullet"],
    )


def run(t: TailoredResume, master: MasterProfile, **kw):
    return validate(t, master, jd_terms=JD_TERMS, **kw)


def test_honest_tailoring_passes(master):
    report = run(good(master), master, ats_keywords=["FastAPI", "Kafka"])
    assert report.ok, report.violations
    assert report.keyword_coverage == 0.5
    assert report.missing_keywords == ["Kafka"]


def test_unverified_skill_rejected(master):
    t = good(master)
    t.skills.append("Kubernetes")
    report = run(t, master)
    assert not report.ok
    assert any("Kubernetes" in v for v in report.violations)


def test_skill_match_is_case_insensitive(master):
    t = good(master)
    t.skills = ["python", "fastapi"]
    assert run(t, master).ok


@pytest.mark.parametrize(
    "text, needle",
    [
        ("Built a FastAPI payments service handling 5M requests/day with p99 latency under 120 ms.", "5"),
        ("Cut AWS costs by 45% by moving batch jobs to spot instances.", "45"),
    ],
)
def test_invented_numbers_rejected(master, text, needle):
    t = good(master)
    t.experience[0].bullets[0] = TailoredBullet(source_id="acme-1" if "FastAPI" in text else "acme-2", text=text)
    t.experience[0].bullets = t.experience[0].bullets[:1]
    report = run(t, master)
    assert not report.ok
    assert any(needle in v and "numbers" in v for v in report.violations)


def test_new_tech_in_bullet_rejected_even_if_verified(master):
    t = good(master)
    # Docker is a verified skill, but the source bullet never mentions it: claiming it here is fabrication.
    t.experience[0].bullets[0].text = "Built a FastAPI payments service on Docker handling 2M requests/day."
    report = run(t, master)
    assert not report.ok
    assert any("Docker" in v for v in report.violations)


def test_jd_tech_injected_into_bullet_rejected(master):
    t = good(master)
    t.experience[0].bullets[1].text = "Cut AWS costs by 30% using Kubernetes autoscaling and PostgreSQL tuning."
    report = run(t, master)
    assert any("Kubernetes" in v for v in report.violations)


def test_bullet_from_other_role_or_unknown_rejected(master):
    t = good(master)
    t.experience[0].bullets.append(TailoredBullet(source_id="nope-9", text="Did things."))
    assert any("nope-9" in v for v in run(t, master).violations)


def test_duplicate_bullet_rejected(master):
    t = good(master)
    t.experience[0].bullets.append(t.experience[0].bullets[0].model_copy())
    assert any("used twice" in v for v in run(t, master).violations)


def test_dropped_role_rejected(master):
    t = good(master)
    t.experience = []
    assert any("every role" in v for v in run(t, master).violations)


def test_locked_project_must_stay(master):
    t = good(master)
    t.project_order = []
    assert any("locked projects" in v for v in run(t, master).violations)


def test_project_reorder_only_when_allowed(master):
    m = master.model_copy(deep=True)
    second = m.projects[0].model_copy(update={"id": "proj-2", "name": "Other"})
    m.projects.append(second)
    t = good(m)
    t.project_order = ["proj-2", "proj-1"]
    assert any("reordered" in v for v in run(t, m).violations)
    assert run(t, m, allow_project_reorder=True).ok


def test_summary_cannot_invent_years_or_tech(master):
    t = good(master)
    t.summary = "Backend engineer with 7 years of Kubernetes experience."
    vs = run(t, master).violations
    assert any("numbers" in v and "7" in v for v in vs)
    assert any("Kubernetes" in v for v in vs)


def test_padding_rejected(master):
    t = good(master)
    t.experience[0].bullets[1].text = (
        "Cut AWS costs by 30% by moving batch jobs to spot instances and tuning PostgreSQL queries, "
        "while also championing a culture of excellence, ownership, and continuous improvement across "
        "the entire engineering organisation and beyond."
    )
    assert any("padded" in v for v in run(t, master).violations)


def test_short_terms_are_case_sensitive():
    assert has_term("Wrote services in Go and Python", "Go")
    assert not has_term("Helped the product go live", "Go")
    assert has_term("Proficient in C++ and C#", "C++")
    assert not has_term("Used Kubernetes-adjacent tooling", "Kube")


def test_numbers_normalise_commas():
    assert numbers("Handled 2,000,000 rows and 30% faster") == {"2000000", "30"}
