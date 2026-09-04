"""RF43 — the manager execution-history read model.

`list_executions` builds one denormalised row per execution (worker, service point, building,
floor, the check-in GPS verdict and the evidence count) with the RF44 filters applied
conditionally; `get_execution_detail` adds the scans, evidence items and the manual-completion
record; `resolve_review` closes a review flag.

Transaction discipline: this module flushes, never commits — the endpoint owns the commit
(only `execution/service.py` self-commits).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, ScalarSelect, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import (
    Building,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.execution.models import (
    Answer,
    EvidenceItem,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
    GeoValidation,
    ManualCompletion,
    QrScan,
    QrScanKind,
)
from app.domain.forms.models import FormVersion
from app.domain.routing.models import RouteStop, RouteStopStatus


class ReviewNotPending(Exception):  # noqa: N818 - names the condition the endpoint maps to 409
    """`resolve_review` was called on an execution not in PENDING_REVIEW."""


class ExecutionNotFound(Exception):  # noqa: N818 - names the condition the endpoint maps to 404
    """`resolve_review` was called with an execution id that does not exist."""


@dataclass(frozen=True)
class ExecutionFilters:
    date_from: date | None = None
    date_to: date | None = None
    field_worker_id: UUID | None = None
    service_point_id: UUID | None = None
    building_id: UUID | None = None
    review_status: str | None = None
    geo_validation: str | None = None
    source: str | None = None
    route_stop_status: str | None = None


@dataclass(frozen=True)
class ExecutionListRow:
    execution_id: UUID
    field_worker_id: UUID
    field_worker_name: str
    service_point_id: UUID
    service_point_name: str
    building_id: UUID
    building_name: str
    floor_label: str
    checked_in_at: datetime
    checked_out_at: datetime | None
    geo_validation: str | None  # from the CHECK_IN scan; None for a MANAGER_MANUAL execution
    review_status: str
    validation_flags: list[str]
    source: str
    evidence_count: int
    route_id: UUID
    clock_skew_seconds: float | None  # app-reported device/server offset, set only when flagged


@dataclass(frozen=True)
class ExecutionScanRow:
    kind: str
    geo_validation: str
    latitude: float | None
    longitude: float | None
    distance_m: float | None
    service_point_id: UUID | None
    scanned_at: datetime


@dataclass(frozen=True)
class ExecutionEvidenceRow:
    id: UUID
    kind: str
    text_body: str | None
    content_type: str | None
    byte_size: int | None
    captured_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class ManualCompletionRow:
    completed_by: UUID
    reason: str
    completed_at: datetime


@dataclass(frozen=True)
class AnswerDetailRow:
    question_stable_key: str
    prompt: str | None  # None when the key is no longer in the recorded form version (Ruling 15)
    value: Any


@dataclass(frozen=True)
class ExecutionDetailRow:
    execution: ExecutionListRow
    scans: list[ExecutionScanRow]
    evidence: list[ExecutionEvidenceRow]
    manual_completion: ManualCompletionRow | None
    form_version_id: UUID | None
    answers: list[AnswerDetailRow]


def _checkin_geo_subquery() -> ScalarSelect[Any]:
    return (
        select(QrScan.geo_validation)
        .where(QrScan.execution_id == Execution.id, QrScan.kind == QrScanKind.CHECK_IN)
        .limit(1)
        .scalar_subquery()
    )


def _evidence_count_subquery() -> ScalarSelect[Any]:
    return (
        select(func.count(EvidenceItem.id))
        .where(EvidenceItem.execution_id == Execution.id)
        .scalar_subquery()
    )


def _row_query() -> Select[Any]:
    """The list/detail column set with the fixed joins, no filters or ordering yet."""
    return (
        select(
            Execution,
            FieldWorker.full_name,
            ServicePoint.id,
            ServicePoint.name,
            Building.id,
            Building.name,
            Floor.label,
            _checkin_geo_subquery().label("checkin_geo"),
            _evidence_count_subquery().label("evidence_count"),
            RouteStop.route_id,
        )
        .select_from(Execution)
        .join(RouteStop, Execution.route_stop_id == RouteStop.id)
        .join(ServicePoint, RouteStop.service_point_id == ServicePoint.id)
        .join(Floor, ServicePoint.floor_id == Floor.id)
        .join(Building, Floor.building_id == Building.id)
        .join(FieldWorker, Execution.field_worker_id == FieldWorker.id)
    )


def _id_conditions(f: ExecutionFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if f.date_from is not None:
        conditions.append(func.date(Execution.checked_in_at) >= f.date_from)
    if f.date_to is not None:
        conditions.append(func.date(Execution.checked_in_at) <= f.date_to)
    if f.field_worker_id is not None:
        conditions.append(Execution.field_worker_id == f.field_worker_id)
    if f.service_point_id is not None:
        conditions.append(RouteStop.service_point_id == f.service_point_id)
    if f.building_id is not None:
        conditions.append(Floor.building_id == f.building_id)
    return conditions


def _enum_conditions(f: ExecutionFilters) -> list[ColumnElement[bool]]:
    """Raises ValueError on an unknown value — the endpoint catches it and answers 422."""
    conditions: list[ColumnElement[bool]] = []
    if f.review_status is not None:
        conditions.append(Execution.review_status == ExecutionReviewStatus(f.review_status))
    if f.geo_validation is not None:
        conditions.append(_checkin_geo_subquery() == GeoValidation(f.geo_validation))
    if f.source is not None:
        conditions.append(Execution.source == ExecutionSource(f.source))
    if f.route_stop_status is not None:
        conditions.append(RouteStop.status == RouteStopStatus(f.route_stop_status))
    return conditions


def _filtered(stmt: Select[Any], f: ExecutionFilters) -> Select[Any]:
    return stmt.where(*_id_conditions(f), *_enum_conditions(f))


def _to_list_row(
    execution: Execution,
    worker_name: str,
    service_point_id: UUID,
    service_point_name: str,
    building_id: UUID,
    building_name: str,
    floor_label: str,
    checkin_geo: GeoValidation | None,
    evidence_count: int,
    route_id: UUID,
) -> ExecutionListRow:
    return ExecutionListRow(
        execution_id=execution.id,
        field_worker_id=execution.field_worker_id,
        field_worker_name=worker_name,
        service_point_id=service_point_id,
        service_point_name=service_point_name,
        building_id=building_id,
        building_name=building_name,
        floor_label=floor_label,
        checked_in_at=execution.checked_in_at,
        checked_out_at=execution.checked_out_at,
        geo_validation=checkin_geo.value if checkin_geo is not None else None,
        review_status=execution.review_status.value,
        validation_flags=list(execution.validation_flags),
        source=execution.source.value,
        evidence_count=evidence_count,
        route_id=route_id,
        clock_skew_seconds=execution.clock_skew_seconds,
    )


async def list_executions(
    db: AsyncSession, filters: ExecutionFilters, *, page: int, page_size: int
) -> tuple[list[ExecutionListRow], int]:
    stmt = (
        _filtered(_row_query(), filters)
        .order_by(Execution.checked_in_at.desc(), Execution.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = [_to_list_row(*row) for row in (await db.execute(stmt)).all()]

    count_src = _filtered(
        select(Execution.id)
        .select_from(Execution)
        .join(RouteStop, Execution.route_stop_id == RouteStop.id)
        .join(ServicePoint, RouteStop.service_point_id == ServicePoint.id)
        .join(Floor, ServicePoint.floor_id == Floor.id),
        filters,
    ).subquery()
    total = int(await db.scalar(select(func.count()).select_from(count_src)) or 0)
    return rows, total


def _to_scan_row(scan: QrScan) -> ExecutionScanRow:
    return ExecutionScanRow(
        kind=scan.kind.value,
        geo_validation=scan.geo_validation.value,
        latitude=scan.latitude,
        longitude=scan.longitude,
        distance_m=scan.distance_m,
        service_point_id=scan.service_point_id,
        scanned_at=scan.scanned_at,
    )


def _to_evidence_row(item: EvidenceItem) -> ExecutionEvidenceRow:
    return ExecutionEvidenceRow(
        id=item.id,
        kind=item.kind.value,
        text_body=item.text_body,
        content_type=item.content_type,
        byte_size=item.byte_size,
        captured_at=item.captured_at,
        created_at=item.created_at,
    )


async def get_execution_detail(db: AsyncSession, execution_id: UUID) -> ExecutionDetailRow | None:
    row = (await db.execute(_row_query().where(Execution.id == execution_id))).first()
    if row is None:
        return None

    form_version_id: UUID | None = row[0].form_version_id
    answers = await _load_answers(db, execution_id, form_version_id)

    scans = (
        await db.scalars(
            select(QrScan).where(QrScan.execution_id == execution_id).order_by(QrScan.scanned_at)
        )
    ).all()
    evidence = (
        await db.scalars(
            select(EvidenceItem)
            .where(EvidenceItem.execution_id == execution_id)
            .order_by(EvidenceItem.captured_at)
        )
    ).all()
    completion = await db.scalar(
        select(ManualCompletion).where(ManualCompletion.execution_id == execution_id)
    )
    return ExecutionDetailRow(
        execution=_to_list_row(*row),
        scans=[_to_scan_row(scan) for scan in scans],
        evidence=[_to_evidence_row(item) for item in evidence],
        manual_completion=(
            ManualCompletionRow(
                completed_by=completion.completed_by,
                reason=completion.reason,
                completed_at=completion.completed_at,
            )
            if completion is not None
            else None
        ),
        form_version_id=form_version_id,
        answers=answers,
    )


async def _load_answers(
    db: AsyncSession, execution_id: UUID, form_version_id: UUID | None
) -> list[AnswerDetailRow]:
    """Ruling 15 — the execution's form answers, each joined to its question prompt from the
    version the execution recorded. No form version means no answers to surface."""
    if form_version_id is None:
        return []

    version = await db.scalar(
        select(FormVersion)
        .where(FormVersion.id == form_version_id)
        .options(selectinload(FormVersion.questions))
    )
    prompt_map: dict[str, str] = (
        {str(q.stable_key): q.prompt for q in version.questions} if version is not None else {}
    )
    answers = (
        await db.scalars(
            select(Answer).where(Answer.execution_id == execution_id).order_by(Answer.created_at)
        )
    ).all()
    return [
        AnswerDetailRow(
            question_stable_key=a.question_stable_key,
            prompt=prompt_map.get(a.question_stable_key),
            value=a.value_json,
        )
        for a in answers
    ]


async def resolve_review(db: AsyncSession, *, execution_id: UUID) -> Execution:
    execution = await db.get(Execution, execution_id)
    if execution is None:
        raise ExecutionNotFound(f'Execution "{execution_id}" not found.')
    if execution.review_status is not ExecutionReviewStatus.PENDING_REVIEW:
        raise ReviewNotPending(
            f'Execution "{execution_id}" is {execution.review_status.value}; '
            "only a PENDING_REVIEW execution can be resolved."
        )
    execution.review_status = ExecutionReviewStatus.RESOLVED
    await db.flush()
    return execution
