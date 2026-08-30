"""Service point CRUD for managers/admins (RF06, RF22 prerequisites), plus the RF25
archival filter and the RF26 promote endpoint. No hard-delete endpoint — a service
point can be referenced by route stops, so removing one outright would orphan them."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import Event, PointType, ServicePoint
from app.domain.catalog.service import promote_service_point_to_regular
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/service-points", tags=["service-points"])


class ServicePointOut(BaseModel):
    id: UUID
    floor_id: UUID
    name: str
    description: str
    latitude: float
    longitude: float
    point_type: PointType
    event_id: UUID | None


class ServicePointCreateRequest(BaseModel):
    floor_id: UUID
    name: str
    description: str
    latitude: float
    longitude: float
    point_type: PointType = PointType.REGULAR
    event_id: UUID | None = None

    @model_validator(mode="after")
    def _occasional_requires_event(self) -> "ServicePointCreateRequest":
        if self.point_type == PointType.OCCASIONAL and self.event_id is None:
            raise ValueError("event_id is required when point_type is OCCASIONAL.")
        return self


class ServicePointUpdateRequest(BaseModel):
    name: str
    description: str
    latitude: float
    longitude: float


def _to_out(point: ServicePoint) -> ServicePointOut:
    return ServicePointOut(
        id=point.id,
        floor_id=point.floor_id,
        name=point.name,
        description=point.description,
        latitude=point.latitude,
        longitude=point.longitude,
        point_type=point.point_type,
        event_id=point.event_id,
    )


@router.get("", response_model=list[ServicePointOut])
async def list_service_points(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    floor_id: UUID | None = None,
    include_archived: bool = False,
) -> list[ServicePointOut]:
    """RF25 — an OCCASIONAL point past its event's valid_until is hidden here by default (no
    archived_at column: computed at query time, spec Ruling 1). include_archived=true is what
    the historical-reports view (RF43, later etapa) will pass."""
    query = select(ServicePoint).options(selectinload(ServicePoint.event))
    if floor_id is not None:
        query = query.where(ServicePoint.floor_id == floor_id)
    if not include_archived:
        query = query.outerjoin(Event, ServicePoint.event_id == Event.id).where(
            or_(
                ServicePoint.point_type == PointType.REGULAR,
                Event.valid_until >= date.today(),
            )
        )
    points = (await db.scalars(query)).all()
    return [_to_out(point) for point in points]


@router.post("", response_model=ServicePointOut, status_code=status.HTTP_201_CREATED)
async def create_service_point(
    body: ServicePointCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServicePointOut:
    point = ServicePoint(
        floor_id=body.floor_id,
        name=body.name,
        description=body.description,
        latitude=body.latitude,
        longitude=body.longitude,
        point_type=body.point_type,
        event_id=body.event_id,
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
        after={"name": point.name, "point_type": point.point_type.value},
    )
    await db.commit()
    return _to_out(point)


@router.patch("/{point_id}", response_model=ServicePointOut)
async def update_service_point(
    point_id: UUID,
    body: ServicePointUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServicePointOut:
    point = await db.get(ServicePoint, point_id)
    if point is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Service point "{point_id}" not found.')

    before = {"name": point.name, "description": point.description}
    point.name = body.name
    point.description = body.description
    point.latitude = body.latitude
    point.longitude = body.longitude
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="service_point",
        entity_id=point.id,
        action="update",
        before=before,
        after={"name": point.name, "description": point.description},
    )
    await db.commit()
    return _to_out(point)


@router.post("/{point_id}/promote", response_model=ServicePointOut)
async def promote_service_point(
    point_id: UUID,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServicePointOut:
    point = await db.get(ServicePoint, point_id)
    if point is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Service point "{point_id}" not found.')
    if point.point_type != PointType.OCCASIONAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only an OCCASIONAL point can be promoted.")

    before = {"point_type": point.point_type.value}
    point = await promote_service_point_to_regular(db, point)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="service_point",
        entity_id=point.id,
        action="promote",
        before=before,
        after={"point_type": point.point_type.value},
    )
    await db.commit()
    return _to_out(point)
