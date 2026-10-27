"""Appeals (spec 2026-10-20-alocacao-automatica §5.4, §7.5). A worker whose stop went back to the
pool while they were working it — left the campus, went silent, the day ended before the sync —
declares they did execute it. The manager accepts (the stop gets an `APPEAL` execution) or
rejects it."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.catalog.models import FieldWorker
    from app.domain.identity.models import User
    from app.domain.routing.models import RouteStop


class AppealReason(enum.StrEnum):
    NO_SIGNAL = "NO_SIGNAL"
    LEFT_BRIEFLY = "LEFT_BRIEFLY"
    APP_CLOSED = "APP_CLOSED"
    OTHER = "OTHER"


class AppealStatus(enum.StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


# Bound of the worker's note and of the manager's review note.
APPEAL_NOTE_MAX_LENGTH = 1000


class Appeal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appeals"

    # UNIQUE: one appeal per released stop — the decision on it is final.
    route_stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_stops.id"), unique=True
    )
    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id"), index=True
    )
    reason: Mapped[AppealReason] = mapped_column(Enum(AppealReason, name="appeal_reason"))
    note: Mapped[str | None] = mapped_column(String(APPEAL_NOTE_MAX_LENGTH), nullable=True)
    # The device-local check-in time the app knew, when it had one; the accepted execution is
    # stamped with it (else with the release time).
    claimed_executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[AppealStatus] = mapped_column(
        Enum(AppealStatus, name="appeal_status"), default=AppealStatus.PENDING, index=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(APPEAL_NOTE_MAX_LENGTH), nullable=True)
    # The offline app retries the filing with the same key; a replay returns the first appeal.
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)

    # Read-only lookups for the lists, one-directional like Impediment's.
    route_stop: Mapped["RouteStop"] = relationship()
    field_worker: Mapped["FieldWorker"] = relationship()
    # The manager who decided; an APPEAL execution has no evidence, so this is its audit trail.
    reviewer: Mapped["User | None"] = relationship()
