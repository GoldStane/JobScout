"""Persistent attempt ceilings and additive migration, without query/result retention."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from job_scout.discovery.budgets import SearchBudget
from job_scout.sources.base import SourceError
from job_scout.storage.database import (
    DiscoveryBudgetRow,
    JobLinkRow,
    SchemaVersion,
    initialize_database,
    migrate_database,
    open_database,
)
from job_scout.storage.repository import Repository

NOW = datetime(2026, 10, 11, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = open_database(f"sqlite:///{tmp_path}/budgets.db")
    initialize_database(engine)
    yield engine
    engine.dispose()


def counts(engine):
    with engine.connect() as connection:
        return dict(
            connection.execute(select(DiscoveryBudgetRow.period, DiscoveryBudgetRow.requests)).all()
        )


def test_daily_limit_persists_across_instances_and_month_limit_rolls_back_day(engine):
    SearchBudget(engine, 1, 2).reserve(NOW)
    with pytest.raises(SourceError, match="budget exhausted"):
        SearchBudget(engine, 1, 2).reserve(NOW)
    SearchBudget(engine, 1, 2).reserve(NOW + timedelta(days=1))
    before = counts(engine)
    with pytest.raises(SourceError):
        SearchBudget(engine, 1, 2).reserve(NOW + timedelta(days=2))
    assert counts(engine) == before  # No partial day reservation/row after month failure.
    SearchBudget(engine, 1, 2).reserve(NOW.replace(month=11))
    assert counts(engine)["brave:month:2026-11"] == 1


def test_concurrent_requests_cannot_exceed_either_ceiling(engine):
    budget = SearchBudget(engine, 3, 100)

    def reserve(_):
        try:
            budget.reserve(NOW)
            return True
        except SourceError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(12))) == 3
    assert counts(engine) == {"brave:day:2026-10-11": 3, "brave:month:2026-10": 3}


def test_budget_windows_use_utc(engine):
    budget = SearchBudget(engine, 1, 1)
    budget.reserve(datetime(2026, 11, 1, 1, tzinfo=timezone(timedelta(hours=8))))
    assert counts(engine) == {"brave:day:2026-10-31": 1, "brave:month:2026-10": 1}


def test_actual_revision_four_upgrade_preserves_identity_links(engine):
    repo = Repository(engine)
    repo.save_job_links({"a" * 64: "b" * 64})
    with engine.begin() as connection:
        DiscoveryBudgetRow.__table__.drop(connection)
        connection.execute(SchemaVersion.__table__.update().values(version=4))
    migrate_database(engine)
    migrate_database(engine)
    assert repo.job_links() == {"a" * 64: "b" * 64}
    assert not counts(engine)
    with engine.connect() as connection:
        assert connection.execute(select(SchemaVersion.version)).scalar_one() == 6
        assert connection.execute(select(JobLinkRow.feed_key)).scalar_one() == "a" * 64
    SearchBudget(engine, 1, 1).reserve(NOW)
