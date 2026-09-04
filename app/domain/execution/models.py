"""Execution records (spec §4.1, decisions #3 and #7). checked_in_at is the device's own
clock (scanned_at from the client); synced_at is server "now" — two timestamps because the
offline mode (Etapa 6) must not lie about when the work actually happened."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExecutionSource(enum.StrEnum):
    APP = "APP"
    MANAGER_MANUAL = "MANAGER_MANUAL"


class QrScanKind(enum.StrEnum):
    CHECK_IN = "CHECK_IN"
    CHECK_OUT = "CHECK_OUT"


class ExecutionReviewStatus(enum.StrEnum):
    NONE = "NONE"
    PENDING_REVIEW = "PENDING_REVIEW"
    RESOLVED = "RESOLVED"


class EvidenceKind(enum.StrEnum):
    PHOTO = "PHOTO"
    NOTE = "NOTE"


class Execution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "executions"

    route_stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_stops.id"), index=True
    )
    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id"), index=True
    )
    # form_version_id arrives in Etapa 7; nullable until then — there is no form this stage.
    form_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # timezone=True (not the plan's bare mapped_column): the caller always passes a
    # timezone-aware datetime (datetime.now(UTC)) — asyncpg rejects binding that into a
    # bare TIMESTAMP WITHOUT TIME ZONE column, so this matches TimestampMixin's own choice.
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[ExecutionSource] = mapped_column(Enum(ExecutionSource, name="execution_source"))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    # Check-out (RF31) is a second idempotent write on the same execution — its own key so a
    # retried check-out never collides with the check-in key stored above.
    checkout_idempotency_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=True
    )
    # default= only, no server_default: the migration drops the transient server default it
    # used to backfill existing rows, so the ORM's Python-side default is the sole source of
    # truth — matches routing.models RouteStatus / route_type.
    review_status: Mapped[ExecutionReviewStatus] = mapped_column(
        Enum(ExecutionReviewStatus, name="execution_review_status"),
        default=ExecutionReviewStatus.NONE,
    )
    validation_flags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Set when the app-reported device/server clock offset at sync exceeds the plausible
    # threshold (spec Ruling 7); drives the CLOCK_SKEW review flag. NULL until Etapa 6 sync.
    clock_skew_seconds: Mapped[float | None] = mapped_column(Float(), nullable=True)


class GeoValidation(enum.StrEnum):
    VALIDATED = "VALIDATED"
    OUT_OF_RADIUS = "OUT_OF_RADIUS"
    NOT_VALIDATED = "NOT_VALIDATED"


class QrScan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "qr_scans"

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("executions.id"), index=True
    )
    qr_code_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("qr_codes.id"))
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Always NOT_VALIDATED this stage — layers 2-5 (radius, route, time window, QR status)
    # arrive in Etapas 3 and 5. See "Decisões de escopo desta etapa", item 6.
    geo_validation: Mapped[GeoValidation] = mapped_column(
        Enum(GeoValidation, name="geo_validation")
    )
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    # nullable: the check-in rewrite (Etapa 5) stores None when the device has no GPS fix.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    kind: Mapped[QrScanKind] = mapped_column(Enum(QrScanKind, name="qr_scan_kind"))
    service_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_points.id"), nullable=True
    )


class EvidenceItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'PHOTO' AND object_key IS NOT NULL AND content_type IS NOT NULL "
            "AND byte_size IS NOT NULL AND sha256 IS NOT NULL AND text_body IS NULL) "
            "OR (kind = 'NOTE' AND text_body IS NOT NULL AND object_key IS NULL)",
            name="evidence_items_kind_shape",
        ),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("executions.id"), index=True
    )
    kind: Mapped[EvidenceKind] = mapped_column(Enum(EvidenceKind, name="evidence_kind"))
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The sync worker retries an evidence upload with the same key so a replayed request
    # never creates a duplicate row (Etapa 6). NULL for evidence created server-side.
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=True
    )


class Answer(UUIDPrimaryKeyMixin, Base):
    """One persisted response to a dynamic-form question (Etapa 7, spec §3.1).

    No FK to `form_questions`: `question_stable_key` is the logical link, and RF38 keeps
    answers whose key may no longer exist in the active version. ON DELETE CASCADE — an
    answer has no meaning without its execution."""

    __tablename__ = "answers"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "question_stable_key", name="uq_answers_execution_stable_key"
        ),
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("executions.id", ondelete="CASCADE")
    )
    question_stable_key: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManualCompletion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manual_completions"

    route_stop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_stops.id"), unique=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("executions.id"))
    completed_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(500))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
