"""Durable daily outbox and short database leases around external delivery."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_scout.models import utc_now
from job_scout.notifications.digest import DigestPart
from job_scout.storage.database import DeliveryLock, DigestRow, NotificationRow, check_schema


class DeliveryBusy(Exception):
    """Another sender owns the recipient lease, or our lease has expired."""


def recipient_key(chat_id: str) -> str:
    try:
        chat_id = str(int(chat_id))
    except ValueError:
        pass  # A fixed local-only namespace is used for previews without a chat ID.
    return hashlib.sha256(f"telegram:{chat_id}".encode()).hexdigest()


@dataclass(frozen=True)
class PendingPart:
    id: int
    text: str
    status: str


@dataclass(frozen=True)
class SavedDigest:
    id: int
    local_date: str
    parts: list[PendingPart]
    complete: bool


class DeliveryRepository:
    def __init__(self, engine: Engine) -> None:
        check_schema(engine)
        self.engine = engine

    def acquire(self, recipient: str) -> str:
        now, owner = utc_now(), str(uuid.uuid4())
        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        with Session(self.engine) as session, session.begin():
            session.execute(
                insert(DeliveryLock)
                .values(
                    recipient=recipient,
                    owner="",
                    expires_at=now,
                )
                .on_conflict_do_nothing(index_elements=["recipient"])
            )
            result = session.execute(
                update(DeliveryLock)
                .where(
                    DeliveryLock.recipient == recipient,
                    DeliveryLock.expires_at <= now,
                )
                .values(owner=owner, expires_at=now + timedelta(minutes=5))
            )
            if result.rowcount != 1:
                raise DeliveryBusy("Another sender is active; retry later")
        return owner

    def _guard(self, session: Session, recipient: str, owner: str) -> None:
        now = utc_now()
        result = session.execute(
            update(DeliveryLock)
            .where(
                DeliveryLock.recipient == recipient,
                DeliveryLock.owner == owner,
                DeliveryLock.expires_at > now,
            )
            .values(expires_at=now + timedelta(minutes=5))
        )
        if result.rowcount != 1:
            raise DeliveryBusy("Delivery lease expired or changed; retry later")

    def release(self, recipient: str, owner: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(DeliveryLock)
                .where(
                    DeliveryLock.recipient == recipient,
                    DeliveryLock.owner == owner,
                )
                .values(expires_at=utc_now())
            )

    def existing(self, recipient: str, local_date: str) -> SavedDigest | None:
        with Session(self.engine) as session:
            # Finish an older partial digest before starting another day.
            row = session.scalars(
                select(DigestRow)
                .where(
                    DigestRow.recipient == recipient,
                    DigestRow.completed_at.is_(None),
                )
                .order_by(DigestRow.id)
            ).first()
            if row is None:
                row = session.scalars(
                    select(DigestRow).where(
                        DigestRow.recipient == recipient,
                        DigestRow.local_date == local_date,
                    )
                ).first()
            if row is None:
                return None
            parts = [
                PendingPart(part.id, part.text, part.status)
                for part in session.scalars(
                    select(NotificationRow)
                    .where(NotificationRow.digest_id == row.id)
                    .order_by(NotificationRow.part_index)
                )
            ]
            return SavedDigest(row.id, row.local_date, parts, row.completed_at is not None)

    def delivered_keys(self, recipient: str) -> set[str]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(NotificationRow)
                .join(DigestRow)
                .where(
                    DigestRow.recipient == recipient,
                    NotificationRow.status == "sent",
                )
            )
            return {key for row in rows for key in row.job_keys}

    def prepare(
        self,
        recipient: str,
        owner: str,
        local_date: str,
        timezone: str,
        parts: list[DigestPart],
        *,
        extend_completed: bool = False,
    ) -> None:
        if not parts:
            raise ValueError("Digest must contain at least one message")
        with Session(self.engine) as session, session.begin():
            self._guard(session, recipient, owner)
            row = session.scalar(
                select(DigestRow).where(
                    DigestRow.recipient == recipient,
                    DigestRow.local_date == local_date,
                )
            )
            offset = 0
            if row is not None:
                if not extend_completed or row.completed_at is None:
                    raise ValueError("Existing digest must be resumed, not overwritten")
                offset = (
                    session.scalar(
                        select(func.max(NotificationRow.part_index)).where(
                            NotificationRow.digest_id == row.id,
                        )
                    )
                    + 1
                )
                row.completed_at = None
            else:
                row = DigestRow(
                    recipient=recipient,
                    local_date=local_date,
                    timezone=timezone,
                    created_at=utc_now(),
                )
                session.add(row)
                session.flush()
            for index, part in enumerate(parts, start=offset):
                session.add(
                    NotificationRow(
                        digest_id=row.id,
                        part_index=index,
                        text=part.text,
                        job_keys=part.job_keys,
                        status="pending",
                        attempts=0,
                    )
                )

    def begin_attempt(self, recipient: str, owner: str, part_id: int) -> None:
        with Session(self.engine) as session, session.begin():
            self._guard(session, recipient, owner)
            part = session.scalars(
                select(NotificationRow)
                .join(DigestRow)
                .where(
                    NotificationRow.id == part_id,
                    DigestRow.recipient == recipient,
                )
            ).one()
            if part.status == "sent":
                raise ValueError("Confirmed messages must never be resent")
            part.status, part.last_attempt, part.last_error = "sending", utc_now(), None
            part.attempts += 1

    def finish_attempt(
        self,
        recipient: str,
        owner: str,
        part_id: int,
        *,
        message_id: int | None = None,
        error: str | None = None,
        uncertain: bool = False,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            self._guard(session, recipient, owner)
            part = session.scalars(
                select(NotificationRow)
                .join(DigestRow)
                .where(
                    NotificationRow.id == part_id,
                    DigestRow.recipient == recipient,
                )
            ).one()
            if part.status != "sending":
                raise ValueError("No active delivery attempt")
            part.status = (
                "sent" if message_id is not None else "uncertain" if uncertain else "failed"
            )
            part.message_id, part.last_error = message_id, error
            session.flush()
            unfinished = session.scalars(
                select(NotificationRow.id).where(
                    NotificationRow.digest_id == part.digest_id,
                    NotificationRow.status != "sent",
                )
            ).first()
            if unfinished is None:
                session.get(DigestRow, part.digest_id).completed_at = utc_now()

    def status(self, recipient: str) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {
                    "date": digest.local_date,
                    "part": part.part_index + 1,
                    "status": part.status,
                    "attempts": part.attempts,
                    "error": part.last_error,
                    "message_id": part.message_id,
                }
                for digest, part in session.execute(
                    select(DigestRow, NotificationRow)
                    .join(NotificationRow)
                    .where(DigestRow.recipient == recipient)
                    .order_by(DigestRow.id, NotificationRow.part_index)
                )
            ]
