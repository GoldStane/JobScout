"""Prepare/resume a daily digest; transport and collection are injected."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

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
    chat_id: str | None,
    sender: Sender | None,
    now: datetime,
    dry_run: bool = False,
    retry_uncertain: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RunResult:
    if not dry_run and (sender is None or chat_id is None):
        raise ValueError("Telegram sender and chat ID required for delivery")
    recipient = recipient_key(chat_id or "preview-without-recipient")
    local_date = now.astimezone(ZoneInfo(profile.digest.timezone)).date().isoformat()
    saved = deliveries.existing(recipient, local_date)
    if saved and saved.complete:
        return RunResult([], "already_sent")
    report = None
    if saved is None:
        # No delivery lease during slow source collection. Recheck after acquiring it.
        report = await collect()
        if not report.sources:
            raise DeliveryError("No enabled sources; configure at least one source before run")
        if not dry_run:
            repository.ingest(report)
    failures = (
        [f"{r.config.company} ({r.config.source}): {r.error}" for r in report.sources if r.error]
        if report
        else []
    )

    def preview_parts():
        assert report is not None
        delivered = deliveries.delivered_keys(recipient)
        # Only freshly observed jobs from successful sources, never stale/disabled boards.
        jobs = [job for source in report.sources if source.error is None for job in source.jobs]
        matches = [
            match
            for match in rank_jobs(jobs, profile)
            if job_key(match.job) not in delivered and match.score >= profile.digest.minimum_score
        ]
        return build_digest(
            matches,
            now=now,
            timezone=profile.digest.timezone,
            failures=failures,
            successful_sources=sum(source.error is None for source in report.sources),
            maximum_matches=profile.digest.maximum_matches,
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
            return RunResult([], "already_sent")
        if saved is None:
            logger.info("digest_preparing date=%s", local_date)
            deliveries.prepare(
                recipient, owner, local_date, profile.digest.timezone, preview_parts()
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
