"""Removing a building or a service point together with its history (spec
2026-10-20-predios-pontos-dialogs-design.md §3.4, decided by the product owner: removal deletes
the history). Everything below runs in the caller's transaction; the photo object keys come back
so the caller deletes the objects from storage only after the commit.

Order matters: every table referencing a row is cleared before the row (no FK cascades here,
except `answers` on `executions`)."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.object_store import ObjectStore, ObjectStoreError
from app.domain.appeals.models import Appeal
from app.domain.catalog.models import Building, Floor, ServicePoint
from app.domain.execution.models import (
    Answer,
    EvidenceItem,
    EvidenceKind,
    Execution,
    QrScan,
)
from app.domain.forms.models import Form, FormQuestion, FormVersion
from app.domain.impediments.models import Impediment
from app.domain.monitoring.models import Alert, Notification, WorkerPosition
from app.domain.qr.models import QrCode
from app.domain.routing.models import Route, RouteStop

logger = logging.getLogger(__name__)

Ids = Sequence[uuid.UUID]


@dataclass(frozen=True)
class DeletionImpact:
    """What a removal takes with it, shown to the manager before confirming."""

    points: int
    floors: int
    routes: int
    executions: int
    photos: int
    appeals: int


async def _ids(db: AsyncSession, query: Select[Any]) -> list[uuid.UUID]:
    return list((await db.scalars(query)).all())


def _points_of_floors(floor_ids: Ids) -> Select[Any]:
    return select(ServicePoint.id).where(ServicePoint.floor_id.in_(floor_ids))


def _stops_of_points(point_ids: Ids) -> Select[Any]:
    return select(RouteStop.id).where(RouteStop.service_point_id.in_(point_ids))


def _executions_of_stops(stop_ids: Ids) -> Select[Any]:
    return select(Execution.id).where(Execution.route_stop_id.in_(stop_ids))


async def _impact(db: AsyncSession, point_ids: Ids, floors: int) -> DeletionImpact:
    stop_ids = await _ids(db, _stops_of_points(point_ids))
    execution_ids = await _ids(db, _executions_of_stops(stop_ids))
    routes = await db.scalar(
        select(func.count(func.distinct(RouteStop.route_id))).where(RouteStop.id.in_(stop_ids))
    )
    photos = await db.scalar(
        select(func.count()).where(
            EvidenceItem.execution_id.in_(execution_ids), EvidenceItem.kind == EvidenceKind.PHOTO
        )
    )
    appeals = await db.scalar(select(func.count()).where(Appeal.route_stop_id.in_(stop_ids)))
    return DeletionImpact(
        points=len(point_ids),
        floors=floors,
        routes=int(routes or 0),
        executions=len(execution_ids),
        photos=int(photos or 0),
        appeals=int(appeals or 0),
    )


async def impact_of_point(db: AsyncSession, point_id: uuid.UUID) -> DeletionImpact:
    """Example: ``impact = await impact_of_point(db, point.id)``"""
    return await _impact(db, [point_id], floors=0)


async def impact_of_building(db: AsyncSession, building_id: uuid.UUID) -> DeletionImpact:
    """Example: ``impact = await impact_of_building(db, building.id)``"""
    floor_ids = await _ids(db, select(Floor.id).where(Floor.building_id == building_id))
    point_ids = await _ids(db, _points_of_floors(floor_ids))
    return await _impact(db, point_ids, floors=len(floor_ids))


async def _delete_executions(db: AsyncSession, execution_ids: Ids) -> list[str]:
    keys = await db.scalars(
        select(EvidenceItem.object_key).where(
            EvidenceItem.execution_id.in_(execution_ids), EvidenceItem.object_key.is_not(None)
        )
    )
    photo_keys = [key for key in keys if key is not None]
    for model in (Answer, EvidenceItem, QrScan):
        await db.execute(delete(model).where(model.execution_id.in_(execution_ids)))
    await db.execute(delete(Execution).where(Execution.id.in_(execution_ids)))
    return photo_keys


async def service_type_has_history(db: AsyncSession, service_type_id: uuid.UUID) -> bool:
    """True when a route stop was made for the type or an execution answered one of its form
    versions: removing the type would orphan that history.

    Example: ``if await service_type_has_history(db, service_type.id): ...``
    """
    version_ids = select(FormVersion.id).join(Form).where(Form.service_type_id == service_type_id)
    stop = await db.scalar(
        select(RouteStop.id).where(RouteStop.service_type_id == service_type_id).limit(1)
    )
    execution = await db.scalar(
        select(Execution.id).where(Execution.form_version_id.in_(version_ids)).limit(1)
    )
    return stop is not None or execution is not None


async def delete_form_of_type(db: AsyncSession, service_type_id: uuid.UUID) -> None:
    """Drop the form built for a type (versions and questions with it), which has no history.

    Example: ``await delete_form_of_type(db, service_type.id)``
    """
    form_ids = select(Form.id).where(Form.service_type_id == service_type_id)
    version_ids = select(FormVersion.id).where(FormVersion.form_id.in_(form_ids))
    await db.execute(delete(FormQuestion).where(FormQuestion.form_version_id.in_(version_ids)))
    await db.execute(delete(FormVersion).where(FormVersion.form_id.in_(form_ids)))
    await db.execute(delete(Form).where(Form.service_type_id == service_type_id))


async def _delete_stops(db: AsyncSession, stop_ids: Ids) -> list[str]:
    # An accepted appeal's APPEAL execution references the stop like any other: it goes above.
    photo_keys = await _delete_executions(db, await _ids(db, _executions_of_stops(stop_ids)))
    for model in (Alert, Impediment, Appeal):
        await db.execute(delete(model).where(model.route_stop_id.in_(stop_ids)))
    await db.execute(delete(RouteStop).where(RouteStop.id.in_(stop_ids)))
    return photo_keys


async def _delete_routes(db: AsyncSession, route_ids: Ids) -> None:
    for model in (Alert, Impediment, WorkerPosition):
        await db.execute(delete(model).where(model.route_id.in_(route_ids)))
    # Notifications point at their route only inside the JSON payload, not by foreign key.
    route_texts = [str(route_id) for route_id in route_ids]
    await db.execute(
        delete(Notification).where(Notification.data["route_id"].astext.in_(route_texts))
    )
    await db.execute(delete(Route).where(Route.id.in_(route_ids)))


async def _settle_routes(db: AsyncSession, route_ids: Ids) -> None:
    """A route left with no stop goes; the others get their stops renumbered 1..n."""
    empty: list[uuid.UUID] = []
    for route_id in route_ids:
        stops = await _ids(
            db,
            select(RouteStop.id)
            .where(RouteStop.route_id == route_id)
            .order_by(RouteStop.order_index),
        )
        if not stops:
            empty.append(route_id)
        for index, stop_id in enumerate(stops, start=1):
            await db.execute(
                update(RouteStop).where(RouteStop.id == stop_id).values(order_index=index)
            )
    await _delete_routes(db, empty)


async def _delete_points(db: AsyncSession, point_ids: Ids) -> list[str]:
    stop_ids = await _ids(db, _stops_of_points(point_ids))
    route_ids = await _ids(
        db, select(RouteStop.route_id).where(RouteStop.id.in_(stop_ids)).distinct()
    )
    photo_keys = await _delete_stops(db, stop_ids)
    await _settle_routes(db, route_ids)
    await db.execute(delete(QrScan).where(QrScan.service_point_id.in_(point_ids)))
    await db.execute(delete(ServicePoint).where(ServicePoint.id.in_(point_ids)))
    return photo_keys


async def delete_point(db: AsyncSession, point_id: uuid.UUID) -> list[str]:
    """Deletes the point and its history; returns the photo object keys to remove after commit.

    Example: ``keys = await delete_point(db, point.id); await db.commit()``
    """
    return await _delete_points(db, [point_id])


async def delete_building(db: AsyncSession, building_id: uuid.UUID) -> list[str]:
    """Deletes the building, its floors, QR codes, points and their history; returns the photo
    object keys to remove after commit.

    Example: ``keys = await delete_building(db, building.id); await db.commit()``
    """
    floor_ids = await _ids(db, select(Floor.id).where(Floor.building_id == building_id))
    photo_keys = await _delete_points(db, await _ids(db, _points_of_floors(floor_ids)))
    qr_ids = await _ids(db, select(QrCode.id).where(QrCode.floor_id.in_(floor_ids)))
    await db.execute(delete(QrScan).where(QrScan.qr_code_id.in_(qr_ids)))
    # A replaced code points at its replacement: unlink before deleting the floor's codes.
    await db.execute(update(QrCode).where(QrCode.id.in_(qr_ids)).values(replaced_by_id=None))
    await db.execute(delete(QrCode).where(QrCode.id.in_(qr_ids)))
    await db.execute(delete(Floor).where(Floor.id.in_(floor_ids)))
    await db.execute(delete(Building).where(Building.id == building_id))
    return photo_keys


async def remove_photo_objects(store: ObjectStore, keys: Sequence[str]) -> None:
    """Deletes the photos of removed evidence from storage, after the rows are committed.

    A failure is logged, not raised: the records are already gone, and an orphan object is
    harmless next to telling the manager a committed removal failed.

    Example: ``await remove_photo_objects(request.app.state.object_store, keys)``
    """
    for key in keys:
        try:
            await store.delete(key)
        except ObjectStoreError:
            logger.warning(
                "photo object left in storage after its evidence was removed",
                extra={"event": "evidence_object_delete_failed", "object_key": key},
            )
