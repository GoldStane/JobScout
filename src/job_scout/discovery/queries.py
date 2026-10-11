"""Small deterministic daily rotation of profile role queries, without employers."""

from datetime import date

from job_scout.matching.profile import Profile


def profile_roles(profile: Profile) -> list[str]:
    unique: dict[str, str] = {}
    for role in profile.preferences.primary_roles + profile.preferences.secondary_roles:
        query = " ".join(role.split())[:200]
        if query:
            unique.setdefault(query.casefold(), query)
    return list(unique.values())


def profile_queries(profile: Profile, maximum: int, day: date) -> list[str | None]:
    values = profile_roles(profile)
    if not values:
        return [None]  # Unfiltered search, never invent a profession or career stage.
    start = (day.toordinal() * maximum) % len(values)
    return (values[start:] + values[:start])[:maximum]


ATS_DOMAINS = (
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "jobs.ashbyhq.com",
)


def search_queries(profile: Profile, maximum: int, day: date, country: str | None) -> list[str]:
    """Rotate role/location families; include supported domains in each bounded query."""
    roles = profile_roles(profile)
    if not roles:
        return []  # Metered broad searches need an explicit role, unlike free feeds.
    places = [country] if country else profile.preferences.preferred_regions or [None]
    sites = "(" + " OR ".join(f"site:{host}" for host in ATS_DOMAINS) + ")"
    queries = []
    count = min(maximum, len(roles))
    for index in range(count):
        # Advance locations after a full role pass to avoid permanently coupling
        # one role to one country when list lengths share a common divisor.
        slot = day.toordinal() * count + index
        role = roles[slot % len(roles)]
        place = places[(slot // len(roles)) % len(places)]

        def phrase(value):
            return '"' + " ".join(value.replace('"', " ").split()[:12])[:100] + '"'

        text = phrase(role) + (" " + phrase(place) if place else "") + " " + sites
        # API limits are 400 characters / 50 words. Domain restrictions always survive.
        if len(text) > 400 or len(text.split()) > 50:
            raise ValueError("Role/location search terms exceed the query budget")
        queries.append(text)
    return queries
