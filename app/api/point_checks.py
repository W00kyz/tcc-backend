"""Checks and the insert that every way of creating a service point shares: `POST
/service-points`, the room endpoints (`POST /rooms`, `POST /rooms/{id}/services`) and the place
endpoints (`POST /places`, `POST /places/{id}/services`)."""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import Floor, ServicePoint, ServiceType
from app.domain.catalog.points import floor_location
from app.domain.identity.models import User


async def require_service_type(db: AsyncSession, service_type_id: UUID) -> ServiceType:
    """A point's type must exist and belong to a company, or no worker could ever receive it."""
    service_type = await db.get(ServiceType, service_type_id)
    if service_type is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Service type "{service_type_id}" not found.'
        )
    if service_type.contractor_company_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "SERVICE_TYPE_WITHOUT_COMPANY",
                "detail": f'Service type "{service_type.name}" belongs to no company; add it to '
                "a company first.",
                "service_type_id": str(service_type_id),
            },
        )
    return service_type


async def reject_repeated_service(
    db: AsyncSession,
    *,
    parent: Literal["room", "place"],
    parent_id: UUID,
    parent_name: str,
    service_type_id: UUID,
    exclude_point_id: UUID | None = None,
) -> None:
    """At most one point per (room or place, service type); no DB constraint, so checked here.
    `exclude_point_id` is the point being retyped, which must not clash with itself.

        await reject_repeated_service(db, parent="room", parent_id=room.id, ...)
    """
    column = ServicePoint.room_id if parent == "room" else ServicePoint.place_id
    query = select(ServicePoint.id).where(
        column == parent_id, ServicePoint.service_type_id == service_type_id
    )
    if exclude_point_id is not None:
        query = query.where(ServicePoint.id != exclude_point_id)
    if await db.scalar(query.limit(1)) is None:
        return
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": f"{parent.upper()}_SERVICE_EXISTS",
            "detail": f'{parent.capitalize()} "{parent_name}" already has this service; edit or '
            "remove its point instead.",
            "service_type_id": str(service_type_id),
        },
    )


async def require_floor_location(db: AsyncSession, floor: Floor) -> tuple[float, float]:
    """The (latitude, longitude) of the floor's building, or a 422 while it has no outline."""
    location = await floor_location(db, floor)
    if location is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f'Building "{floor.building_id}" has no outline; draw it before adding points.',
        )
    return location


async def add_audited_point(
    db: AsyncSession,
    actor: User,
    *,
    floor_id: UUID | None = None,
    place_id: UUID | None = None,
    name: str,
    description: str,
    service_type: ServiceType,
    location: tuple[float, float],
    room_id: UUID | None = None,
) -> ServicePoint:
    """Inserts the point (flushed, not committed) and writes its `create` audit entry. The
    point sits on a floor (optionally in a room) or in a place, never on both and never nowhere:
    the database only forbids the mix, so this is where "nowhere" is refused.

        await add_audited_point(db, actor, place_id=place.id, name=..., ...)
    """
    if (floor_id is None) == (place_id is None):
        raise ValueError(
            f"A service point needs exactly one of floor_id and place_id; got floor_id={floor_id} "
            f"and place_id={place_id}."
        )
    if place_id is not None and room_id is not None:
        raise ValueError(f'A point of place "{place_id}" cannot be in room "{room_id}".')
    latitude, longitude = location
    point = ServicePoint(
        service_type=service_type,
        floor_id=floor_id,
        place_id=place_id,
        room_id=room_id,
        name=name,
        description=description,
        latitude=latitude,
        longitude=longitude,
    )
    db.add(point)
    await db.flush()
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="service_point",
        entity_id=point.id,
        action="create",
        before=None,
        after={"name": point.name, "service_type_id": str(service_type.id)},
    )
    return point
