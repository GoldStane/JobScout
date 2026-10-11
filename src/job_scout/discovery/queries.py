"""Small deterministic daily rotation of profile role queries, without employers."""

from datetime import date

from job_scout.matching.profile import Profile


def profile_queries(profile: Profile, maximum: int, day: date) -> list[str | None]:
    roles = profile.preferences.primary_roles + profile.preferences.secondary_roles
    unique: dict[str, str] = {}
    for role in roles:
        query = " ".join(role.split())[:200]
        if query:
            unique.setdefault(query.casefold(), query)
    values = list(unique.values())
    if not values:
        return [None]  # Unfiltered search, never invent a profession or career stage.
    start = (day.toordinal() * maximum) % len(values)
    return (values[start:] + values[:start])[:maximum]
