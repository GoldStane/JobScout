"""Priority sorting, strict selection, partial dates, and safe configuration errors."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_scout.main import main
from job_scout.matching.eligibility import evaluate_eligibility
from job_scout.matching.profile import Profile
from job_scout.matching.scoring import rank_jobs
from job_scout.models import Job


def job(identity, **kwargs):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return Job.model_validate(
        dict(
            source="lever",
            source_job_id=identity,
            company="Example",
            company_identifier="example",
            title="Graduate Engineer",
            application_url="https://example.org/job",
            source_url="https://example.org/job",
            fetched_at=now,
            first_seen_at=now,
            last_seen_at=now,
        )
        | kwargs
    )


def ids(jobs, profile):
    return [m.job.source_job_id for m in rank_jobs(jobs, profile)]


def test_priority_sort_and_missing_dates():
    jobs = [
        job("a", location_text="Zurich", posted_at="2026-10-01T00:00:00Z"),
        job("b", location_text="Berlin", posted_at="2026-10-01T00:00:00Z"),
        job("c", location_text="Amsterdam"),
        job("d", posted_at="2026-10-02T00:00:00Z"),
    ]
    profile = Profile.model_validate(
        {
            "sort": [
                {"field": "score", "direction": "desc"},
                {"field": "posted_at", "direction": "desc"},
                {"field": "location", "direction": "asc"},
            ]
        }
    )
    assert ids(jobs, profile) == ["d", "b", "a", "c"]
    profile.sort[1].direction = "asc"
    assert ids(jobs, profile) == ["b", "a", "d", "c"]
    assert ids(list(reversed(jobs)), profile) == ids(jobs, profile)


def test_score_priority_over_date():
    profile = Profile.model_validate(
        {
            "preferences": {"primary_roles": ["engineer"]},
            "sort": [{"field": "score"}, {"field": "posted_at"}],
        }
    )
    assert ids(
        [
            job("new", title="Analyst", posted_at="2026-10-02T00:00:00Z"),
            job("old", posted_at="2026-10-01T00:00:00Z"),
        ],
        profile,
    ) == ["old", "new"]


def test_filters_intersect_and_exclusion_wins():
    profile = Profile.model_validate(
        {
            "filters": {
                "employment_types": ["graduate", "internship"],
                "unknown_employment_type": "exclude",
                "allowed_locations": ["Germany"],
                "excluded_locations": ["Munich"],
                "remote_types": ["remote"],
                "unknown_remote_type": "exclude",
                "unknown_location": "exclude",
            },
            "preferences": {"location_terms": {"Germany": ["Berlin", "Munich"]}},
        }
    )
    jobs = [
        job("yes", location_text="Berlin", remote_type="remote"),
        job("excluded", location_text="Munich, Germany", remote_type="remote"),
        job("onsite", location_text="Berlin", remote_type="on_site"),
        job("unknown", location_text="Berlin"),
        job("junior", title="Junior Engineer", location_text="Berlin", remote_type="remote"),
        job("missing", remote_type="remote"),
        job("other", location_text="Paris", remote_type="remote"),
    ]
    assert ids(jobs, profile) == ["yes"]


def test_unknown_policies_and_inactive_filters():
    unknown = job("unknown", title="Engineer")
    profile = Profile.model_validate(
        {
            "filters": {
                "employment_types": ["internship"],
                "allowed_locations": ["Italy"],
                "remote_types": ["remote"],
            }
        }
    )
    assert ids([unknown], profile) == ["unknown"]
    profile.filters.unknown_employment_type = "exclude"
    assert not ids([unknown], profile)
    assert ids([unknown], Profile.model_validate({"filters": {"unknown_location": "exclude"}}))


@pytest.mark.parametrize(
    "start,end,rejected,partial",
    [
        ("2026-01-01", "2026-12-31", True, False),
        ("2027-01-01", "2027-12-31", False, False),
        ("2027-06-01", "2027-09-01", False, True),
    ],
)
def test_graduation_year(start, end, rejected, partial):
    profile = Profile.model_validate({"education": {"expected_graduation_year": 2027}})
    result = evaluate_eligibility(
        job("1", eligibility_information={"graduation_start": start, "graduation_end": end}),
        profile,
    )
    assert bool(result.rejection_reasons) == rejected
    assert any("exact date" in text for text in result.unknowns) == partial


@pytest.mark.parametrize(
    "data",
    [
        {"education": {"expected_graduation_date": 2027}},
        {"education": {"expected_graduation_date": "2027-01-01", "expected_graduation_year": 2027}},
        {"sort": [{"field": "score"}, {"field": "score"}]},
        {"sort": []},
    ],
)
def test_invalid_profile(data):
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_validation_cli_offline_and_private(tmp_path, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "education:\n  expected_graduation_date: 2027\neligibility:\n"
        "  work_authorized_countries: PRIVATE_VALUE\n"
    )
    assert (
        main(
            [
                "validate-config",
                "--profile",
                str(profile),
                "--config",
                "config/companies.example.yaml",
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "YYYY-MM-DD" in output and "YAML list" in output
    assert "PRIVATE_VALUE" not in output
    profile.write_text("education:\n  expected_graduation_year: 2027\n")
    assert (
        main(
            [
                "validate-config",
                "--profile",
                str(profile),
                "--config",
                "config/companies.example.yaml",
            ]
        )
        == 0
    )


def test_cli_invalid_sort_before_database():
    with pytest.raises(SystemExit) as exc:
        main(["match", "--sort", "score"])
    assert exc.value.code == 2
