"""Atomic UTC day/month request reservations, shared by runners using this database."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from job_scout.models import utc_now
from job_scout.sources.base import SourceError
from job_scout.storage.database import DiscoveryBudgetRow, check_schema


class BudgetExceeded(SourceError):
    def __init__(self, scope, used, limit, reset_at):
        super().__init__(f"Brave {scope} request budget exhausted")
        self.quota = {"scope": scope, "used": used, "limit": limit, "reset_at": reset_at}


class SearchBudget:
    def __init__(self, engine: Engine, daily: int, monthly: int):
        check_schema(engine)
        self.engine, self.daily, self.monthly = engine, daily, monthly

    def reserve(self, now: datetime | None = None) -> None:
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("Budget clock must be timezone-aware")
        now = now.astimezone(UTC)
        periods = [
            (f"brave:day:{now:%Y-%m-%d}", self.daily),
            (f"brave:month:{now:%Y-%m}", self.monthly),
        ]
        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
        # Insert/update acquires SQLite's write lock; PG locks rows in fixed order.
        # Failure rolls back BOTH windows. Reservations are never refunded after I/O.
        with self.engine.begin() as connection:
            for period, limit in periods:
                connection.execute(
                    insert(DiscoveryBudgetRow)
                    .values(period=period, requests=0)
                    .on_conflict_do_nothing(index_elements=["period"])
                )
                used = connection.execute(
                    select(DiscoveryBudgetRow.requests)
                    .where(DiscoveryBudgetRow.period == period)
                    .with_for_update()
                ).scalar_one()
                if used >= limit:
                    scope = "daily" if ":day:" in period else "monthly"
                    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                        days=1
                    )
                    reset = (
                        tomorrow
                        if scope == "daily"
                        else (now.replace(day=28) + timedelta(days=4)).replace(
                            day=1, hour=0, minute=0, second=0, microsecond=0
                        )
                    )
                    raise BudgetExceeded(scope, used, limit, reset.isoformat())
                connection.execute(
                    update(DiscoveryBudgetRow)
                    .where(DiscoveryBudgetRow.period == period)
                    .values(requests=used + 1)
                )
