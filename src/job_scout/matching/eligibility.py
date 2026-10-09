"""Conservative evidence rules; silence and unsupported language remain unknown."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from job_scout.matching.profile import Profile
from job_scout.matching.text import contains, role_type
from job_scout.models import Job


class EligibilityResult(BaseModel):
    status: Literal["eligible", "ineligible", "unknown"]
    rejection_reasons: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


def _facts(job: Job) -> tuple[dict[str, str], list[str]]:
    """Extract only narrow full-sentence assertions, never instructions in job text."""
    facts = dict(job.eligibility_information)
    evidence = []
    for sentence in re.split(r"[.!?\n]+", job.description_text):
        sentence = sentence.strip()
        for pattern, key, value in [
            (r"(?:a )?phd (?:is )?required", "minimum_degree", "doctorate"),
            (r"(?:a )?master'?s degree (?:is )?required", "minimum_degree", "masters"),
            (r"(?:a )?bachelor'?s degree (?:is )?required", "minimum_degree", "bachelors"),
            (
                r"(?:applicants|candidates) must be currently enrolled",
                "enrollment_required",
                "true",
            ),
            (r"current student enrollment is required", "enrollment_required", "true"),
            (r"visa sponsorship is not available", "sponsorship_available", "false"),
        ]:
            if re.fullmatch(pattern, sentence, flags=re.IGNORECASE) and key not in facts:
                facts[key] = value
                evidence.append(sentence)
        match = re.fullmatch(
            r"(?:applicants|candidates) must graduate between "
            r"(\d{4}-\d{2}-\d{2}) and (\d{4}-\d{2}-\d{2})",
            sentence,
            flags=re.IGNORECASE,
        )
        if match and "graduation_start" not in facts and "graduation_end" not in facts:
            facts["graduation_start"], facts["graduation_end"] = match.groups()
            evidence.append(sentence)
    return facts, evidence


def evaluate_eligibility(job: Job, profile: Profile) -> EligibilityResult:
    facts, evidence = _facts(job)
    rejections: list[str] = []
    unknowns: list[str] = []
    if job.status == "closed":
        rejections.append("Posting is closed")
    # Do not confuse 'work with senior engineers' in descriptions with required seniority.
    if role_type(job.title, job.employment_type) is None and any(
        contains(job.title, term)
        for term in [
            "senior",
            "sr.",
            "sr",
            "staff engineer",
            "staff software engineer",
            "principal",
            "director",
        ]
    ):
        rejections.append("Title explicitly indicates a senior-level role")
        evidence.append(job.title)

    degree_levels = {"high_school": 0, "associate": 1, "bachelors": 2, "masters": 3, "doctorate": 4}
    required_degree = facts.get("minimum_degree")
    if required_degree:
        if required_degree not in degree_levels or profile.education.degree is None:
            unknowns.append("Mandatory degree compatibility is unknown")
        elif degree_levels[profile.education.degree] < degree_levels[required_degree]:
            rejections.append(f"Mandatory {required_degree} degree exceeds configured degree")
        else:
            evidence.append(
                f"Configured degree meets stated {required_degree} level; completion not verified"
            )
            unknowns.append("Degree completion or acceptance of current students is not verified")

    enrollment = facts.get("enrollment_required")
    if enrollment == "true":
        if profile.education.currently_enrolled is False:
            rejections.append("Current enrollment is required but profile says not enrolled")
        elif profile.education.currently_enrolled is None:
            unknowns.append("Current enrollment is required; profile enrollment is unknown")
        else:
            evidence.append("Current enrollment matches the stated requirement")
    elif enrollment != "false":
        unknowns.append("Enrollment requirements are unknown")

    start, end = facts.get("graduation_start"), facts.get("graduation_end")
    if start is not None or end is not None:
        try:
            lower, upper = date.fromisoformat(start or ""), date.fromisoformat(end or "")
            if lower > upper:
                raise ValueError("Reversed graduation window")
        except ValueError:
            unknowns.append("Graduation window is incomplete or unrecognized")
        else:
            graduation = profile.education.expected_graduation_date
            if graduation is None:
                unknowns.append("Expected graduation date is unknown")
            elif not lower <= graduation <= upper:
                rejections.append("Expected graduation falls outside the stated inclusive window")
            else:
                evidence.append("Expected graduation falls within the stated window")
    else:
        unknowns.append("Graduation eligibility is unknown")

    sponsorship = facts.get("sponsorship_available")
    if sponsorship == "false" and profile.eligibility.requires_sponsorship is True:
        rejections.append("Sponsorship is explicitly unavailable but profile requires it")
    elif sponsorship not in {"true", "false"}:
        unknowns.append("Sponsorship policy is unknown")
    elif profile.eligibility.requires_sponsorship is None:
        unknowns.append("Profile sponsorship requirement is unknown")

    country = facts.get("work_authorization_country")
    authorized = profile.eligibility.work_authorized_countries
    if country is None or authorized is None:
        unknowns.append("Work authorization compatibility is unknown")
    elif country.casefold() not in {value.casefold() for value in authorized}:
        # Sponsorship might resolve this; never infer a legal restriction from location alone.
        unknowns.append("Stated work authorization country is not confirmed in the profile")
    else:
        evidence.append("Stated authorization country matches the configured authorization list")
    if profile.eligibility.willing_to_relocate is None:
        unknowns.append("Relocation preference is unknown")
    elif profile.eligibility.willing_to_relocate is False:
        unknowns.append("Location feasibility needs review; current residence is not configured")
    return EligibilityResult(
        status="ineligible" if rejections else "unknown" if unknowns else "eligible",
        rejection_reasons=rejections,
        unknowns=unknowns,
        evidence=evidence,
    )
