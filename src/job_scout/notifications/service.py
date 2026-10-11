"""Prepare/resume a daily digest; transport and collection are injected."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from job_scout.deduplicate import reconcile_jobs
from job_scout.discovery.base import DiscoveryBatch, DiscoveryResult
from job_scout.discovery.registration import RegistrationReport
from job_scout.matching.profile import Profile
from job_scout.matching.scoring import rank_jobs
from job_scout.models import CollectionReport
from job_scout.notifications.digest import build_digest, job_key
from job_scout.notifications.telegram import DeliveryError
from job_scout.storage.delivery import DeliveryRepository, recipient_key
from job_scout.storage.repository import Repository

logger = logging.getLogger(__name__)


class Sender(Protocol):
    async def send(self, text: str) -> int: ...


@dataclass(frozen=True)
class RunResult:
    messages: list[str]
    status: str
    source_failures: int = 0


async def run_digest(
    *,
    repository: Repository,
    deliveries: DeliveryRepository,
    profile: Profile,
    collect: Callable[[], Awaitable[CollectionReport]],
    discover: Callable[[], Awaitable[DiscoveryResult | DiscoveryBatch | None]] | None = None,
    register: Callable[[DiscoveryResult | DiscoveryBatch], Awaitable[RegistrationReport]]
    | None = None,
    chat_id: str | None,
    sender: Sender | None,
    now: datetime,
    dry_run: bool = False,
    retry_uncertain: bool = False,
    additional: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RunResult:
    if not dry_run and (sender is None or chat_id is None):
        raise ValueError("Telegram sender and chat ID required for delivery")
    recipient = recipient_key(chat_id or "preview-without-recipient")
    local_date = now.astimezone(ZoneInfo(profile.digest.timezone)).date().isoformat()
    saved = deliveries.existing(recipient, local_date)
    if saved and saved.complete:
        if not additional:
            return RunResult([], "already_sent")
        saved = None
    report = None
    discovery = None
    registration = None
    discoveries = []
    if saved is None:
        # No delivery lease during slow source collection. Recheck after acquiring it.
        if discover is not None:
            report, discovery = await asyncio.gather(collect(), discover())
        else:
            report = await collect()
        discoveries = (
            discovery.results
            if isinstance(discovery, DiscoveryBatch)
            else [discovery]
            if discovery is not None
            else []
        )
        if not report.sources and not discoveries:
            raise DeliveryError("No enabled sources; configure at least one source before run")
        if not dry_run:
            repository.ingest(report)
            for result in discoveries:
                repository.ingest_discovery(result)
        if discovery is not None and register is not None:
            registration = await register(discovery)
    failures = (
        [f"{r.config.company} ({r.config.source}): {r.error}" for r in report.sources if r.error]
        if report
        else []
    )
    for result in discoveries:
        if result.error:
            failures.append(f"{result.provider}: {result.error}")
        if result.skipped:
            failures.append(f"{result.provider}: {result.skipped} invalid postings skipped")
    if registration is not None and registration.failed:
        failures.append("ATS registration incomplete; see discover-jobs or company candidates")

    reconciled = None
    if report is not None:
        fresh = [job for source in report.sources if source.error is None for job in source.jobs]
        if discovery is not None:
            fresh.extend(discovery.jobs)
        history = repository.jobs(include_closed=True)
        reconciled = reconcile_jobs(fresh, history, repository.job_links())
        if not dry_run:
            repository.save_job_links(reconciled.new_links)
            # Use authoritative persisted bindings if another writer linked the same feed job.
            reconciled = reconcile_jobs(fresh, history, repository.job_links())

    def preview_parts():
        assert report is not None
        assert reconciled is not None
        delivered = deliveries.delivered_keys(recipient)
        matches = [
            match
            for match in rank_jobs(reconciled.jobs, profile)
            if not delivered.intersection(reconciled.aliases[job_key(match.job)])
            and match.score >= profile.digest.minimum_score
        ]
        return build_digest(
            matches,
            now=now,
            timezone=profile.digest.timezone,
            failures=failures,
            successful_sources=sum(source.error is None for source in report.sources)
            + sum(result.pages > 0 for result in discoveries),
            maximum_matches=profile.digest.maximum_matches,
            identity_aliases=reconciled.aliases,
        )

    if dry_run:
        texts = (
            [part.text for part in saved.parts if part.status != "sent"]
            if saved
            else [part.text for part in preview_parts()]
        )
        return RunResult(texts, "dry_run", len(failures))

    owner = deliveries.acquire(recipient)
    try:
        saved = deliveries.existing(recipient, local_date)
        if saved and saved.complete:
            if not additional or report is None:
                return RunResult([], "already_sent")
            saved = None
        if saved is None:
            logger.info("digest_preparing date=%s", local_date)
            deliveries.prepare(
                recipient,
                owner,
                local_date,
                profile.digest.timezone,
                preview_parts(),
                extend_completed=additional,
            )
            saved = deliveries.existing(recipient, local_date)
        assert saved is not None and sender is not None
        for part in saved.parts:
            if part.status == "sent":
                continue
            if part.status in {"sending", "uncertain"} and not retry_uncertain:
                raise DeliveryError(
                    "Uncertain delivery saved; inspect Telegram, "
                    "then use --retry-uncertain if needed",
                    uncertain=True,
                )
            deliveries.begin_attempt(recipient, owner, part.id)
            try:
                message_id = await sender.send(part.text)
            except DeliveryError as exc:
                logger.warning("delivery_failed part=%d uncertain=%s", part.id, exc.uncertain)
                deliveries.finish_attempt(
                    recipient, owner, part.id, error=str(exc), uncertain=exc.uncertain
                )
                raise
            deliveries.finish_attempt(recipient, owner, part.id, message_id=message_id)
            logger.info("delivery_confirmed part=%d message_id=%d", part.id, message_id)
            await sleep(1.1)  # Personal-chat pacing between digest parts.
        return RunResult([], "sent", len(failures))
    finally:
        deliveries.release(recipient, owner)
