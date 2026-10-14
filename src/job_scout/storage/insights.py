"""Discovery telemetry, bounded feed scheduling, and explicit relevance feedback."""

import re
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from job_scout.discovery.companies.diagnostics import ACTIONS, STAGES, safe_quota
from job_scout.matching.scoring import rank_jobs
from job_scout.models import utc_now
from job_scout.notifications.digest import job_key
from job_scout.storage.database import (
    DiscoveryRunRow,
    DiscoveryScheduleRow,
    FeedbackRow,
    check_schema,
)


class Insights:
    def __init__(self, engine):
        check_schema(engine)
        self.engine = engine
        self.insert = sqlite_insert if engine.dialect.name == "sqlite" else postgres_insert

    def claim_refresh(self, provider, hours, *, now=None, force=False):
        """Atomic reservation before networking; failures/crashes retain their cooldown."""
        now = now or utc_now()
        with Session(self.engine) as session, session.begin():
            session.execute(
                self.insert(DiscoveryScheduleRow)
                .values(
                    provider=provider,
                    next_due=now,
                )
                .on_conflict_do_nothing(index_elements=["provider"])
            )
            row = session.scalars(
                select(DiscoveryScheduleRow)
                .where(
                    DiscoveryScheduleRow.provider == provider,
                )
                .with_for_update()
            ).one()
            if row.next_due > now and not force:
                return False
            row.next_due = now + timedelta(hours=hours)
            return True

    def record(self, result, counts, profile=None):
        status = getattr(result, "status", "completed")
        if status == "completed":
            status = (
                "partial" if result.error and result.pages else "failed" if result.error else "ok"
            )
            if result.skipped and result.provider != "brave" and status == "ok":
                status = "partial"
        counters = {
            "requests": result.requests,
            "publisher_requests": getattr(result, "publisher_requests", 0),
            "pages": result.pages,
            "queries": result.queries,
            "observations": len(result.jobs) if status not in {"cached", "dry_run_skipped"} else 0,
            "inserted": counts.inserted,
            "updated": counts.updated,
            "skipped": result.skipped,
            "expired": result.expired,
            "limited": int(result.limited),
        }
        counters["qualifying_observations"] = (
            sum(m.score >= profile.digest.minimum_score for m in rank_jobs(result.jobs, profile))
            if profile is not None and status not in {"cached", "dry_run_skipped"}
            else 0
        )
        # Only allowlisted counts cross this boundary, including for search providers.
        with Session(self.engine) as session, session.begin():
            session.execute(
                self.insert(DiscoveryRunRow)
                .values(
                    provider=result.provider,
                    attempted_at=result.attempted_at,
                    completed_at=result.completed_at,
                    status=status,
                    counters=counters,
                )
                .on_conflict_do_nothing(index_elements=["provider", "attempted_at"])
            )

    def record_companies(self, result, attempted_at):
        # No search result content, URLs or snippets enter the health log.
        counters = {key: value for key, value in result.as_dict().items() if type(value) is int}
        if quota := safe_quota(result.quota):
            counters["quota"] = quota
        for item in result.diagnostics:
            if item.get("stage") in STAGES and item.get("code") in ACTIONS:
                key = f"diagnostic:{item['stage']}:{item['code']}"
                counters[key] = counters.get(key, 0) + 1
        with Session(self.engine) as session, session.begin():
            session.add(
                DiscoveryRunRow(
                    provider="company_search",
                    attempted_at=attempted_at,
                    completed_at=utc_now(),
                    status="ok" if result.status == "completed" else result.status,
                    counters=counters,
                )
            )

    def next_refresh(self, provider):
        with Session(self.engine) as session:
            row = session.get(DiscoveryScheduleRow, provider)
            return row.next_due.isoformat() if row else None

    def last_company_report(self):
        with Session(self.engine) as session:
            row = session.scalars(
                select(DiscoveryRunRow)
                .where(DiscoveryRunRow.provider == "company_search")
                .order_by(DiscoveryRunRow.attempted_at.desc())
                .limit(1)
            ).first()
            if row is None:
                result = {"status": "no_saved_report", "diagnostics": []}
            else:
                diagnostics = []
                for key, count in row.counters.items():
                    parts = key.split(":")
                    if (
                        len(parts) == 3
                        and parts[0] == "diagnostic"
                        and parts[1] in STAGES
                        and parts[2] in ACTIONS
                    ):
                        diagnostics.append(
                            {
                                "stage": parts[1],
                                "code": parts[2],
                                "count": count,
                                "action": ACTIONS[parts[2]],
                            }
                        )
                result = {
                    "status": row.status,
                    "attempted_at": row.attempted_at.isoformat(),
                    "completed_at": row.completed_at.isoformat(),
                    "counters": {
                        k: v
                        for k, v in row.counters.items()
                        if not k.startswith("diagnostic:") and k != "quota"
                    },
                    "quota": safe_quota(row.counters.get("quota")),
                    "diagnostics": diagnostics,
                    "diagnostics_available": bool(diagnostics),
                }
        result["next_due"] = self.next_refresh("company_search")
        return result

    def health(self, days=30, *, now=None):
        if not 1 <= days <= 365:
            raise ValueError("Health window must be between 1 and 365 days")
        cutoff = (now or utc_now()) - timedelta(days=days)
        groups = {}
        with Session(self.engine) as session:
            for row in session.scalars(
                select(DiscoveryRunRow)
                .where(
                    DiscoveryRunRow.attempted_at >= cutoff,
                )
                .order_by(DiscoveryRunRow.attempted_at)
            ):
                group = groups.setdefault(
                    row.provider,
                    {
                        "provider": row.provider,
                        "runs": 0,
                        "failed_or_partial": 0,
                        "last_success": None,
                        "totals": {},
                    },
                )
                group["runs"] += 1
                group["failed_or_partial"] += row.status in {"failed", "partial", "unavailable"}
                group["last_attempt"] = row.attempted_at.isoformat()
                group["last_status"] = row.status
                if row.status not in {"cached", "dry_run_skipped"}:
                    group["last_collection_status"] = row.status
                    group["last_collection_attempt"] = row.attempted_at.isoformat()
                if row.status in {"ok", "no_new_boards"}:
                    group["last_success"] = row.completed_at.isoformat()
                for key, value in row.counters.items():
                    group["totals"][key] = group["totals"].get(key, 0) + value
            schedules = {
                r.provider: r.next_due.isoformat()
                for r in session.scalars(select(DiscoveryScheduleRow))
            }
        for provider, group in groups.items():
            totals = group["totals"]
            requests = (
                totals.get("requests", 0)
                + totals.get("publisher_requests", 0)
                + totals.get("site_requests", 0)
            )
            group["new_records_per_100_requests"] = (
                round(100 * totals.get("inserted", 0) / requests, 2) if requests else None
            )
            if provider == "company_search":
                group["verified_boards_per_100_requests"] = (
                    round(100 * totals.get("registered", 0) / requests, 2) if requests else None
                )
            group["next_due"] = schedules.get(provider)
        return [groups[key] for key in sorted(groups)]

    @staticmethod
    def validate_scope(scope):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", scope):
            raise ValueError(
                "Feedback scope requires 1–64 letters, numbers, underscores or hyphens"
            )

    def feedback(self, scope="default"):
        self.validate_scope(scope)
        with Session(self.engine) as session:
            return {
                row.job_key: row.rating
                for row in session.scalars(
                    select(FeedbackRow)
                    .where(FeedbackRow.scope == scope)
                    .order_by(FeedbackRow.job_key)
                )
            }

    def set_feedback(self, key, rating, repository, scope="default"):
        self.validate_scope(scope)
        if rating not in {"relevant", "irrelevant", "clear"}:
            raise ValueError("Unknown feedback rating")
        if key not in {job_key(job) for job in repository.jobs(include_closed=True)}:
            raise ValueError("Unknown job key; copy job_key from job-scout jobs")
        with Session(self.engine) as session, session.begin():
            if rating == "clear":
                session.execute(
                    delete(FeedbackRow).where(
                        FeedbackRow.scope == scope,
                        FeedbackRow.job_key == key,
                    )
                )
            else:
                values = dict(scope=scope, job_key=key, rating=rating, updated_at=utc_now())
                session.execute(
                    self.insert(FeedbackRow)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["scope", "job_key"],
                        set_={"rating": rating, "updated_at": values["updated_at"]},
                    )
                )

    def filter_jobs(self, jobs, scope="default", aliases=None):
        dismissed = {key for key, rating in self.feedback(scope).items() if rating == "irrelevant"}
        return [
            job
            for job in jobs
            if not dismissed.intersection((aliases or {}).get(job_key(job), [job_key(job)]))
        ]
