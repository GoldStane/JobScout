"""Conservative feed/ATS equivalence, preserving every occurrence's delivery key."""

from dataclasses import dataclass

from job_scout.discovery.posting_links import posting_identity
from job_scout.models import FEED_PROVIDERS, Job
from job_scout.notifications.digest import job_key


@dataclass
class ReconciledJobs:
    jobs: list[Job]
    aliases: dict[str, list[str]]
    new_links: dict[str, str]


def reconcile_jobs(fresh: list[Job], history: list[Job], links: dict[str, str]) -> ReconciledJobs:
    # The newest record for each identity supplies content; retain earlier first-seen times.
    records = {job_key(job): job for job in history}
    for job in fresh:
        key = job_key(job)
        previous = records.get(key)
        records[key] = job.model_copy(
            update={
                "first_seen_at": min(job.first_seen_at, previous.first_seen_at)
                if previous
                else job.first_seen_at
            }
        )
    publishers = {}
    for key, job in records.items():
        if job.source in FEED_PROVIDERS:
            continue
        identity = (job.source, job.source_region, job.company_identifier, job.source_job_id)
        # Only publisher URLs whose identity agrees with the independently fetched row.
        if posting_identity(str(job.source_url)) == identity:
            publishers[identity] = (key, job)
    new_links = {}
    for key, job in records.items():
        if job.source not in FEED_PROVIDERS or key in links:
            continue
        leads = job.source_metadata.get("ats_posting_leads", [])
        if not isinstance(leads, list):
            continue
        identities = {posting_identity(url) for url in leads if isinstance(url, str)} - {None}
        # Multiple different posting links are ambiguous, even if only one is monitored.
        if len(identities) != 1:
            continue
        publisher = publishers.get(next(iter(identities)))
        if publisher and " ".join(job.title.casefold().split()) == " ".join(
            publisher[1].title.casefold().split()
        ):
            new_links[key] = publisher[0]
    bindings = links | new_links
    groups: dict[str, list[Job]] = {}
    for key in dict.fromkeys(job_key(job) for job in fresh):
        groups.setdefault(bindings.get(key, key), []).append(records[key])
    all_keys: dict[str, set[str]] = {}
    for key in records.keys() | bindings.keys() | set(bindings.values()):
        all_keys.setdefault(bindings.get(key, key), set()).add(key)
    jobs, aliases = [], {}
    for group, observations in groups.items():
        # Prefer a freshly collected publisher job, never substitute stale publisher content.
        chosen = max(
            observations,
            key=lambda job: (
                job.source not in FEED_PROVIDERS,
                job.application_url is not None,
                len(job.description_text),
            ),
        )
        related = [records[key] for key in all_keys[group] if key in records]
        updates = {"first_seen_at": min(job.first_seen_at for job in related)}
        if not chosen.description_text:
            updates["description_text"] = max(
                (job.description_text for job in observations), key=len, default=""
            )
        # Supplement missing publisher facts only with agreeing, fresh observations.
        for field in ("salary_text", "timezone_restrictions"):
            values = [
                getattr(job, field) for job in observations if getattr(job, field) is not None
            ]
            if getattr(chosen, field) is None and values and all(v == values[0] for v in values):
                updates[field] = values[0]
        attribution_links = {}
        for job in sorted(related, key=lambda job: job.source):
            attribution_links.update(job.attribution_links)
            if job.attribution and job.attribution_url:
                attribution_links[job.attribution] = job.attribution_url
        updates["attribution_links"] = attribution_links
        chosen = chosen.model_copy(update=updates)
        jobs.append(chosen)
        aliases[job_key(chosen)] = sorted(all_keys[group])
    return ReconciledJobs(jobs, aliases, new_links)
