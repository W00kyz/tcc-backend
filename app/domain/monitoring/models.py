"""Real-time monitoring: alerts, configurable alert rules, notifications and worker GPS
positions (RF41-RF42, RF47-RF52, Etapa 9).

The periodic alert scan (`app/domain/monitoring/scan.py`) turns route/stop
state into `Alert` rows keyed by a deterministic `dedup_key` — a UNIQUE column so an
`INSERT ... ON CONFLICT DO NOTHING` keeps a re-run of the scan from duplicating an open
alert (spec §2 Ruling 1/2). `alert_rules` holds one row per rule kind with an on/off switch
and an optional threshold override; when `threshold_minutes` is NULL the scan falls back to
the matching global default on `system_settings` (spec §2 Ruling 8).

`notifications` is the canonical delivery record — the panel's notification centre and the
app's list both read from it; real push (FCM/APNs) sits behind a seam and never writes here
(spec §2 Ruling 3). `worker_positions` is append-only: the app posts a fix while its route is
IN_PROGRESS and the board reads the latest row per worker (spec §2 Ruling 5)."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.catalog.models import FieldWorker
    from app.domain.routing.models import Route, RouteStop


class AlertKind(enum.StrEnum):
    # Spec 2026-10-20-alocacao-automatica §8.7. The schedule-based kinds (late start, worker
    # absent, late stop) went with the planned times.
    # NO_EVIDENCE: a DONE app stop with no evidence past the grace period.
    # POINT_IMPEDED: a worker reported an impediment; the point went back to the pool.
    # BATCH_RELEASED: a worker's batch was released (left campus / location silence).
    # POINTS_UNSERVED: the workday ended with points not served (one per day, no route).
    # APPEAL_FILED: a worker contested a released stop.
    NO_EVIDENCE = "NO_EVIDENCE"
    POINT_IMPEDED = "POINT_IMPEDED"
    BATCH_RELEASED = "BATCH_RELEASED"
    POINTS_UNSERVED = "POINTS_UNSERVED"
    APPEAL_FILED = "APPEAL_FILED"


class AlertSeverity(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertStatus(enum.StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class Alert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "alerts"

    kind: Mapped[AlertKind] = mapped_column(Enum(AlertKind, name="alert_kind"))
    severity: Mapped[AlertSeverity] = mapped_column(Enum(AlertSeverity, name="alert_severity"))
    # Deterministic per violation (e.g. "NO_EVIDENCE:{route_stop_id}"): the UNIQUE constraint is
    # what makes the scan idempotent — a resolved violation that still holds does not reopen.
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(String(500))
    # NULL for a day-level alert that belongs to no route (POINTS_UNSERVED).
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routes.id"), index=True, nullable=True
    )
    # NULL for route-level alerts.
    route_stop_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("route_stops.id"), nullable=True
    )
    field_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id"), nullable=True
    )
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus, name="alert_status"), default=AlertStatus.OPEN, index=True
    )
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    route: Mapped["Route | None"] = relationship()
    route_stop: Mapped["RouteStop | None"] = relationship()
    field_worker: Mapped["FieldWorker | None"] = relationship()


class AlertRule(Base):
    __tablename__ = "alert_rules"

    # One row per rule kind (the enum is the PK). Seeded by the migration, all enabled with a
    # NULL threshold (use the global default from system_settings).
    rule_kind: Mapped[AlertKind] = mapped_column(
        Enum(AlertKind, name="alert_kind"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(default=True)
    threshold_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    # 'ALERT' | 'IMPEDIMENT_REPORTED' — a plain string, not an enum: the set grows as features
    # land and none of it drives a schema decision.
    kind: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(1000))
    data: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerPosition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "worker_positions"

    field_worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_workers.id")
    )
    route_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("routes.id"))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Device clock (when the app captured the fix) vs. server clock (when it landed).
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    field_worker: Mapped["FieldWorker"] = relationship()
    route: Mapped["Route"] = relationship()
