"""Canonical persistent registry; repeated seeds never override operator decisions."""

import hashlib
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from job_scout.discovery.detect import SUPPORTED, Candidate, DiscoveryError, detect
from job_scout.discovery.validate import Validation
from job_scout.models import SourceConfig, SourceRegistry, utc_now
from job_scout.storage.database import CandidateRow, CompanyRow, SourceRow, check_schema


def identity(config: SourceConfig) -> str:
    return f"{config.source}:{config.region}:{config.board_id}"


def board_url(config: SourceConfig) -> str:
    host = {
        "greenhouse": "job-boards.greenhouse.io",
        "ashby": "jobs.ashbyhq.com",
        "lever": "jobs.eu.lever.co" if config.region == "eu" else "jobs.lever.co",
        "smartrecruiters": "careers.smartrecruiters.com",
    }[config.source]
    return f"https://{host}/{config.board_id}"


class Registry:
    def __init__(self, engine):
        check_schema(engine)
        self.engine = engine
        self.insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert

    def _source(self, session, config, *, origin, status, url=None, company_id=None):
        session.execute(
            self.insert(SourceRow)
            .values(
                provider=config.source,
                region=config.region,
                board_id=config.board_id,
                company=config.company,
                consecutive_failures=0,
            )
            .on_conflict_do_nothing(index_elements=["provider", "region", "board_id"])
        )
        source = session.scalar(
            select(SourceRow)
            .where(
                SourceRow.provider == config.source,
                SourceRow.region == config.region,
                SourceRow.board_id == config.board_id,
            )
            .with_for_update()
        )
        if source.registration_status == "unregistered":
            if company_id is not None:
                company = session.get(CompanyRow, company_id)
                if company is None:
                    raise DiscoveryError("Unknown company ID")
            else:
                # Never conflate unrelated employers just because display names match.
                session.execute(
                    self.insert(CompanyRow)
                    .values(
                        identity=identity(config),
                        display_name=config.company,
                        aliases=[],
                        discovered_at=utc_now(),
                    )
                    .on_conflict_do_nothing(index_elements=["identity"])
                )
                company = session.scalar(
                    select(CompanyRow).where(CompanyRow.identity == identity(config))
                )
            source.company_id = company.id
            source.company = company.display_name
            source.registration_origin = origin
            source.registration_status = status
            source.board_url = url
        return source

    def seed(self, seeds: SourceRegistry) -> None:
        with Session(self.engine) as session, session.begin():
            for config in seeds.sources:
                self._source(
                    session,
                    config,
                    origin="manual_seed",
                    status=(
                        "active" if config.enabled and config.source in SUPPORTED else "disabled"
                    ),
                    url=board_url(config),
                )

    def active(self, seeds: SourceRegistry | None = None) -> SourceRegistry:
        # Optional seeds supply a read-only preview of not-yet-imported sources.
        with Session(self.engine) as session:
            rows = list(session.scalars(select(SourceRow).order_by(SourceRow.id)))
            registered = {
                (r.provider, r.region, r.board_id)
                for r in rows
                if r.registration_status != "unregistered"
            }
            configs = [
                SourceConfig(
                    source=r.provider,
                    region=r.region,
                    board_id=r.board_id,
                    company=r.company,
                    enabled=True,
                )
                for r in rows
                if r.registration_status == "active" and r.provider in SUPPORTED
            ]
            configs.extend(
                s
                for s in (seeds.sources if seeds else [])
                if s.enabled
                and s.source in SUPPORTED
                and (s.source, s.region, s.board_id) not in registered
            )
            return SourceRegistry(sources=configs)

    def record(
        self,
        candidate: Candidate,
        validation: Validation,
        *,
        company_id=None,
        method: str = "manual_url",
        evidence: dict | None = None,
    ) -> dict:
        now = utc_now()
        key = hashlib.sha256(candidate.identity.encode()).hexdigest()
        with Session(self.engine) as session, session.begin():
            session.execute(
                self.insert(CandidateRow)
                .values(
                    identity=key,
                    url=candidate.url,
                    config=candidate.config.model_dump() if candidate.config else None,
                    method=method,
                    discovered_at=now,
                    state="discovered",
                    reason="User-supplied URL",
                    endpoint_valid=False,
                    audit=[],
                )
                .on_conflict_do_nothing(index_elements=["identity"])
            )
            row = session.scalar(
                select(CandidateRow).where(CandidateRow.identity == key).with_for_update()
            )
            if row.state == "rejected":
                return self._candidate_dict(row)
            row.validated_at, row.endpoint_valid, row.reason = (
                now,
                validation.endpoint_valid,
                validation.reason,
            )
            row.state = validation.state
            row.audit = [
                *row.audit,
                {
                    "at": now.isoformat(),
                    "state": validation.state,
                    "reason": validation.reason,
                    "method": method,
                    "evidence": evidence or {},
                },
            ]
            if validation.state == "verified":
                source = self._source(
                    session,
                    candidate.config,
                    origin="discovered",
                    status="active",
                    url=candidate.url,
                    company_id=company_id,
                )
                source.validated_at, source.validation_error = now, None
                row.source_id = source.id
                row.state = "active" if source.registration_status == "active" else "verified"
                row.audit = [
                    *row.audit,
                    {
                        "at": now.isoformat(),
                        "state": row.state,
                        "reason": "Auto-approved verified board"
                        if row.state == "active"
                        else "Existing disabled source preserved",
                    },
                ]
            elif row.source_id:
                source = session.get(SourceRow, row.source_id)
                source.validation_error = validation.reason
                # Discovery errors do not disable an already registered monitor.
            session.flush()
            return self._candidate_dict(row)

    def approve(self, candidate_id: int) -> None:
        with Session(self.engine) as session, session.begin():
            # UPDATE takes a SQLite write lock before read/modify; PG locks the row.
            session.execute(
                update(CandidateRow).where(CandidateRow.id == candidate_id).values(id=candidate_id)
            )
            row = session.get(CandidateRow, candidate_id)
            if row is None or not row.endpoint_valid or row.state == "rejected":
                raise DiscoveryError(
                    "Candidate must have a valid endpoint; revalidate before approval"
                )
            if row.validated_at is None or utc_now() - row.validated_at > timedelta(days=1):
                raise DiscoveryError(
                    "Validation is older than 24 hours; revalidate before approval"
                )
            config = SourceConfig.model_validate(row.config)
            if config.source not in SUPPORTED:
                raise DiscoveryError("Provider has no collection adapter")
            source = self._source(
                session, config, origin="discovered", status="active", url=row.url
            )
            source.registration_status = "active"
            source.validated_at, source.validation_error = row.validated_at, None
            row.source_id, row.state = source.id, "active"
            row.audit = [
                *row.audit,
                {
                    "at": utc_now().isoformat(),
                    "state": "active",
                    "reason": "Operator confirmed board identity",
                },
            ]

    def reject(self, candidate_id: int) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(CandidateRow).where(CandidateRow.id == candidate_id).values(id=candidate_id)
            )
            row = session.get(CandidateRow, candidate_id)
            if row is None:
                raise DiscoveryError("Unknown candidate ID")
            row.state = "rejected"
            row.audit = [
                *row.audit,
                {
                    "at": utc_now().isoformat(),
                    "state": "rejected",
                    "reason": "Operator rejected candidate",
                },
            ]
            if row.source_id:
                session.get(SourceRow, row.source_id).registration_status = "disabled"

    def disable(self, source_id: int) -> None:
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(SourceRow)
                .where(SourceRow.id == source_id)
                .values(registration_status="disabled")
            )
            if not result.rowcount:
                raise DiscoveryError("Unknown source ID")

    def enable(self, source_id: int) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(update(SourceRow).where(SourceRow.id == source_id).values(id=source_id))
            row = session.get(SourceRow, source_id)
            if (
                row is None
                or row.provider not in SUPPORTED
                or row.registration_status == "unregistered"
            ):
                raise DiscoveryError("Source must be registered with a supported provider")
            rejected = session.scalar(
                select(CandidateRow).where(
                    CandidateRow.source_id == source_id, CandidateRow.state == "rejected"
                )
            )
            if rejected is not None:
                raise DiscoveryError(
                    "Rejected candidates cannot be enabled; resolve the rejection first"
                )
            row.registration_status = "active"

    def candidate(self, candidate_id: int) -> Candidate:
        with Session(self.engine) as session:
            row = session.get(CandidateRow, candidate_id)
            if row is None:
                raise DiscoveryError("Unknown candidate ID")
            config = SourceConfig.model_validate(row.config) if row.config else None
            return detect(row.url, name=config.company if config else None)

    @staticmethod
    def _candidate_dict(row):
        return {
            "id": row.id,
            "url": row.url,
            "state": row.state,
            "reason": row.reason,
            "source_id": row.source_id,
            "endpoint_valid": row.endpoint_valid,
            "discovered_at": row.discovered_at,
            "validated_at": row.validated_at,
            "method": row.method,
            "audit": row.audit,
        }

    def candidates(self) -> list[dict]:
        with Session(self.engine) as session:
            return [
                self._candidate_dict(row)
                for row in session.scalars(select(CandidateRow).order_by(CandidateRow.id))
            ]

    def sources(self) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {
                    "id": r.id,
                    "company_id": r.company_id,
                    "company": r.company,
                    "provider": r.provider,
                    "board_id": r.board_id,
                    "region": r.region,
                    "board_url": r.board_url,
                    "status": r.registration_status,
                    "origin": r.registration_origin,
                    "validated_at": r.validated_at,
                    "validation_error": r.validation_error,
                    "last_success": r.last_success,
                    "last_attempt": r.last_attempt,
                    "last_error": r.last_error,
                }
                for r in session.scalars(select(SourceRow).order_by(SourceRow.id))
            ]
