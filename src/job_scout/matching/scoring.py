"""Explainable 0–100 relevance scores, with unknown factors treated neutrally."""

from pydantic import BaseModel

from job_scout.matching.eligibility import EligibilityResult, evaluate_eligibility
from job_scout.matching.profile import Profile
from job_scout.matching.text import contains, mentions, role_type
from job_scout.models import Job

# Deliberately small and auditable. Unrecognized locations remain unknown.
EUROPE_TERMS = [
    "Europe",
    "European Union",
    "Italy",
    "Germany",
    "France",
    "Spain",
    "Portugal",
    "Netherlands",
    "Belgium",
    "Switzerland",
    "Austria",
    "Ireland",
    "United Kingdom",
    "UK",
    "Poland",
    "Sweden",
    "Norway",
    "Denmark",
    "Finland",
    "Czechia",
    "Greece",
    "London",
    "Milan",
    "Milano",
    "Rome",
    "Paris",
    "Berlin",
    "Amsterdam",
    "Dublin",
    "Zurich",
]


class MatchResult(BaseModel):
    job: Job
    eligibility: EligibilityResult
    role_type: str | None
    score: float
    factors: dict[str, float | None]
    effective_weights: dict[str, float]
    reasons: list[str]
    unknowns: list[str]


def score_job(job: Job, profile: Profile) -> MatchResult:
    eligibility = evaluate_eligibility(job, profile)
    reasons: list[str] = []
    unknowns = list(eligibility.unknowns)
    factors: dict[str, float | None] = dict.fromkeys(type(profile.ranking_weights).model_fields)
    role_evidence = []
    for roles, title_score, description_score in [
        (profile.preferences.primary_roles, 100.0, 80.0),
        (profile.preferences.secondary_roles, 70.0, 55.0),
    ]:
        for role in roles:
            if mentions(job.title, role):
                role_evidence.append((title_score, f"Title matches preferred role: {role}"))
            elif mentions(job.description_text, role):
                role_evidence.append(
                    (description_score, f"Description mentions preferred role: {role}")
                )
    if role_evidence:
        value, explanation = max(role_evidence, key=lambda pair: pair[0])
        factors["role_alignment"] = value
        reasons.append(explanation)
    elif profile.preferences.primary_roles or profile.preferences.secondary_roles:
        factors["role_alignment"] = 0.0

    skills = list(dict.fromkeys(skill.casefold() for skill in profile.skills))
    matched = [skill for skill in skills if mentions(f"{job.title}\n{job.description_text}", skill)]
    if matched:
        factors["skills_alignment"] = min(100.0, 100.0 * len(matched) / min(4, len(skills)))
        reasons.append("Posting mentions your skills: " + ", ".join(matched[:5]))
    elif skills and job.description_text.strip():
        factors["skills_alignment"] = 0.0

    kind = role_type(job.title, job.employment_type)
    if kind:
        factors["education_experience_alignment"] = (
            100.0 if kind in profile.preferences.employment_types else 50.0
        )
        if kind in profile.preferences.employment_types:
            reasons.append(
                f"Posting is labeled as a preferred {kind.replace('_', ' ')} opportunity"
            )

    location = job.location_text or ""
    for region in profile.preferences.preferred_regions:
        terms = [region, *profile.preferences.location_terms.get(region, [])]
        if region.casefold() == "europe":
            terms += EUROPE_TERMS
        if any(contains(location, term) for term in terms):
            factors["location_preference"] = 100.0
            reasons.append(f"Location text mentions preferred region or place: {region}")
            break

    # No defensible salary comparison or employer-prestige proxy in this baseline.
    unknowns.append("Compensation and career potential are unscored; no salary or prestige assumed")
    for factor, value in factors.items():
        if value is None and factor != "compensation_career_potential":
            unknowns.append(f"Insufficient evidence for {factor.replace('_', ' ')}")
    weights = profile.ranking_weights.model_dump()
    denominator = sum(weights.values())
    effective = {name: weight / denominator for name, weight in weights.items()}
    score = sum(
        (value if value is not None else 50.0) * effective[name] for name, value in factors.items()
    )
    if not reasons:
        reasons.append("No configured role or skill match found; review the posting manually")
    return MatchResult(
        job=job,
        eligibility=eligibility,
        role_type=kind,
        score=round(score, 2),
        factors=factors,
        effective_weights=effective,
        reasons=reasons[:3],
        unknowns=list(dict.fromkeys(unknowns)),
    )


def rank_jobs(
    jobs: list[Job], profile: Profile, *, include_ineligible: bool = False
) -> list[MatchResult]:
    matches = [score_job(job, profile) for job in jobs if job.status == "open"]
    if not include_ineligible:
        matches = [match for match in matches if match.eligibility.status != "ineligible"]
    return sorted(
        matches,
        key=lambda match: (
            match.eligibility.status == "ineligible",
            -match.score,
            match.job.company.casefold(),
            match.job.title.casefold(),
            match.job.source,
            match.job.source_region,
            match.job.company_identifier,
            match.job.source_job_id,
        ),
    )
