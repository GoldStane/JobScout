"""Deterministic matching tests: exclusions, uncertainty, and ranking."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_scout.matching.eligibility import evaluate_eligibility
from job_scout.matching.profile import Profile, RankingWeights, load_profile
from job_scout.matching.scoring import rank_jobs, score_job
from job_scout.matching.text import mentions
from job_scout.models import Job, utc_now


@pytest.fixture
def profile():
    return load_profile(Path("config/profile.example.yaml"))


def job(title="Graduate C++ Engineer", description="Build C++ and Linux systems", **changes):
    now = utc_now()
    return Job.model_validate(
        dict(
            source="lever",
            source_job_id="1",
            company="Example",
            company_identifier="example",
            title=title,
            description_text=description,
            application_url="https://example.org/apply",
            source_url="https://example.org/job",
            fetched_at=now,
            first_seen_at=now,
            last_seen_at=now,
        )
        | changes
    )


def test_missing_policy_is_unknown_not_excluded(profile):
    result = evaluate_eligibility(job(), profile)
    assert result.status == "unknown"
    assert not result.rejection_reasons
    assert any("Sponsorship" in text for text in result.unknowns)


@pytest.mark.parametrize(
    "title", ["Senior Software Engineer", "Staff Software Engineer", "Principal Engineer"]
)
def test_senior_roles_excluded(profile, title):
    assert evaluate_eligibility(job(title), profile).status == "ineligible"


@pytest.mark.parametrize(
    "title,description",
    [
        ("Junior Engineer", "Work with senior engineers."),
        ("Research Wizard", "Work on Linux systems."),
        ("Support Staff", "Support customers."),
        ("Software Intern", "A PhD is preferred."),
        ("Software Intern", "A PhD is not required."),
        ("Software Intern", "A PhD is required or equivalent experience."),
    ],
)
def test_ambiguous_and_optional_requirements_not_excluded(profile, title, description):
    assert evaluate_eligibility(job(title, description), profile).status != "ineligible"


def test_explicit_mandatory_degree_excluded(profile):
    result = evaluate_eligibility(job(description="A PhD is required."), profile)
    assert result.status == "ineligible"
    assert any("doctorate" in text for text in result.rejection_reasons)


def test_enrollment_requirement(profile):
    profile.education.currently_enrolled = False
    result = evaluate_eligibility(
        job(description="Applicants must be currently enrolled."), profile
    )
    assert result.status == "ineligible"
    profile.education.currently_enrolled = None
    assert (
        evaluate_eligibility(
            job(description="Applicants must be currently enrolled."), profile
        ).status
        == "unknown"
    )


@pytest.mark.parametrize(
    "graduation,excluded",
    [
        (date(2027, 1, 1), False),
        (date(2027, 12, 31), False),
        (date(2028, 1, 1), True),
        (None, False),
    ],
)
def test_graduation_window_is_inclusive(profile, graduation, excluded):
    profile.education.expected_graduation_date = graduation
    result = evaluate_eligibility(
        job(description="Candidates must graduate between 2027-01-01 and 2027-12-31."), profile
    )
    assert (result.status == "ineligible") == excluded


def test_malformed_graduation_is_unknown(profile):
    result = evaluate_eligibility(
        job(
            eligibility_information={"graduation_start": "invalid", "graduation_end": "2027-01-01"}
        ),
        profile,
    )
    assert result.status == "unknown"


def test_sponsorship_explicit_mismatch_only(profile):
    profile.eligibility.requires_sponsorship = True
    assert evaluate_eligibility(job(), profile).status == "unknown"
    result = evaluate_eligibility(job(description="Visa sponsorship is not available."), profile)
    assert result.status == "ineligible"


def test_eligible_requires_known_compatibility(profile):
    profile.education.expected_graduation_date = date(2027, 6, 1)
    profile.eligibility.requires_sponsorship = False
    profile.eligibility.willing_to_relocate = True
    profile.eligibility.work_authorized_countries = ["Italy"]
    result = evaluate_eligibility(
        job(
            eligibility_information={
                "graduation_start": "2027-01-01",
                "graduation_end": "2027-12-31",
                "enrollment_required": "false",
                "sponsorship_available": "true",
                "work_authorization_country": "Italy",
            }
        ),
        profile,
    )
    assert result.status == "eligible"
    assert not result.unknowns


def test_score_orders_relevant_jobs_and_explains(profile):
    relevant = job(location_text="Milan, Italy")
    unrelated = job("Marketing Coordinator", "Marketing and customer outreach", source_job_id="2")
    matches = rank_jobs([unrelated, relevant], profile)
    assert matches[0].job.source_job_id == "1"
    assert matches[0].score > matches[1].score
    assert 1 <= len(matches[0].reasons) <= 3
    assert matches[0].factors["compensation_career_potential"] is None
    assert matches[0].effective_weights["compensation_career_potential"] == pytest.approx(0.10)


def test_title_alone_is_not_perfect(profile):
    result = score_job(job("C++ Software Engineer", ""), profile)
    assert result.score < 100
    assert result.unknowns


def test_unknown_factors_are_neutral():
    result = score_job(job("Mystery", ""), Profile())
    assert result.score == 50
    assert all(value is None for value in result.factors.values())


def test_weights_are_configurable(profile):
    profile.ranking_weights = RankingWeights(
        role_alignment=1,
        skills_alignment=0,
        education_experience_alignment=0,
        compensation_career_potential=0,
        location_preference=0,
    )
    assert score_job(job("C++ Software Engineer"), profile).score == 100


def test_europe_preference_never_excludes_other_regions(profile):
    europe = score_job(job(location_text="Paris, France"), profile)
    hong_kong = score_job(job(location_text="Hong Kong"), profile)
    assert europe.score > hong_kong.score
    assert hong_kong.eligibility.status != "ineligible"


def test_tracks_do_not_mutate_base_profile(profile):
    original = profile.model_dump()
    track = profile.for_track("systems")
    assert track.skills == ["C++", "Linux", "CMake", "embedded systems"]
    assert profile.model_dump() == original
    with pytest.raises(ValueError):
        profile.for_track("missing")


def test_rank_filters_closed_and_exclusions(profile):
    results = rank_jobs(
        [job(), job("Senior Engineer", source_job_id="2"), job(status="closed", source_job_id="3")],
        profile,
    )
    assert len(results) == 1
    assert len(rank_jobs([job("Senior Engineer")], profile, include_ineligible=True)) == 1


@pytest.mark.parametrize(
    "weights",
    [
        {"role_alignment": -1},
        {"role_alignment": float("nan")},
        {key: 0 for key in RankingWeights.model_fields},
    ],
)
def test_invalid_weights_rejected(weights):
    with pytest.raises(ValidationError):
        RankingWeights(**weights)


def test_profile_validation_and_safe_yaml(tmp_path):
    with pytest.raises(ValidationError):
        Profile.model_validate({"digest": {"timezone": "Not/AZone"}})
    with pytest.raises(ValidationError):
        Profile.model_validate({"eligibility": {"requires_sponsorship": "unknown"}})
    with pytest.raises(ValidationError):
        Profile.model_validate({"typo": 1})
    path = tmp_path / "profile.yaml"
    path.write_text("!!python/object:builtins.object {}")
    import yaml

    with pytest.raises(yaml.YAMLError):
        load_profile(path)


def test_skill_boundaries():
    assert mentions("C++ software", "C++")
    assert not mentions("JavaScript", "Java")
    assert not mentions("C++", "C")
