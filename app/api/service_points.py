"""Service point CRUD for managers/admins (RF06). A point is located by its building: the
server writes its coordinates (app/domain/catalog/points.py), the API never takes them."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.deletion import (
    delete_point,
    impact_of_point,
    remove_photo_objects,
)
from app.domain.catalog.models import Building, Floor, ServicePoint, ServiceType
from app.domain.catalog.points import point_location
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/service-points", tags=["service-points"])


class ServicePointOut(BaseModel):
    id: UUID
    floor_id: UUID
    name: str
    description: str
    latitude: float
    longitude: float
    service_type_id: UUID | None
    service_type_name: str | None


class ServicePointCreateRequest(BaseModel):
    # A point is located by its building (its outline's centre), never by coordinates sent here.
    model_config = ConfigDict(extra="forbid")

    floor_id: UUID
    name: str
    description: str
    service_type_id: UUID


class ServicePointUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    # Optional so a rename does not have to resend it; a point can only change type, never lose it.
    service_type_id: UUID | None = None


class DeletionImpactOut(BaseModel):
    """What a removal takes with it — shown before the manager confirms."""

    points: int
    floors: int
    routes: int
    executions: int
    photos: int
    appeals: int


def _to_out(point: ServicePoint) -> ServicePointOut:
    return ServicePointOut(
        id=point.id,
        floor_id=point.floor_id,
        name=point.name,
        description=point.description,
        latitude=point.latitude,
        longitude=point.longitude,
        service_type_id=point.service_type_id,
        service_type_name=point.service_type.name if point.service_type else None,
    )


async def _require_service_type(db: AsyncSession, service_type_id: UUID) -> ServiceType:
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


@router.get("", response_model=list[ServicePointOut])
async def list_service_points(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    floor_id: UUID | None = None,
    building_id: UUID | None = None,
) -> list[ServicePointOut]:
    # Registration order, so a renamed point does not jump to the end of the building's list.
    query = (
        select(ServicePoint)
        .options(selectinload(ServicePoint.service_type))
        .order_by(ServicePoint.created_at, ServicePoint.id)
    )
    if floor_id is not None:
        query = query.where(ServicePoint.floor_id == floor_id)
    if building_id is not None:
        query = query.join(Floor, ServicePoint.floor_id == Floor.id).where(
            Floor.building_id == building_id
        )
    points = (await db.scalars(query)).all()
    return [_to_out(point) for point in points]


@router.post("", response_model=ServicePointOut, status_code=status.HTTP_201_CREATED)
async def create_service_point(
    body: ServicePointCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServicePointOut:
    # BUG FIX (Finding I2.2): without this check, an unknown floor_id only fails
    # later at the ServicePoint insert's FK constraint, surfacing as an unhandled
    # IntegrityError (500) instead of a clean 404 — same existence-check-before-write pattern
    # as update_floor in app/api/floors.py.
    floor = await db.get(Floor, body.floor_id)
    if floor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Floor "{body.floor_id}" not found.')
    building = await db.get(Building, floor.building_id)
    location = None if building is None else point_location(building)
    if location is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f'Building "{floor.building_id}" has no outline; draw it before adding points.',
        )

    service_type = await _require_service_type(db, body.service_type_id)
    latitude, longitude = location
    point = ServicePoint(
        service_type=service_type,
        floor_id=body.floor_id,
        name=body.name,
        description=body.description,
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

    before = {
        "name": point.name,
        "description": point.description,
        "service_type_id": str(point.service_type_id) if point.service_type_id else None,
    }
    if body.service_type_id is not None:
        point.service_type = await _require_service_type(db, body.service_type_id)
    else:
        await db.refresh(point, ["service_type"])
    point.name = body.name
    point.description = body.description
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="service_point",
        entity_id=point.id,
        action="update",
        before=before,
        after={
            "name": point.name,
            "description": point.description,
            "service_type_id": str(point.service_type_id) if point.service_type_id else None,
        },
    )
    await db.commit()
    return _to_out(point)


async def _load_point(db: AsyncSession, point_id: UUID) -> ServicePoint:
    point = await db.get(ServicePoint, point_id)
    if point is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Service point "{point_id}" not found.')
    return point


@router.get("/{point_id}/deletion-impact", response_model=DeletionImpactOut)
async def service_point_deletion_impact(
    point_id: UUID,
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeletionImpactOut:
    await _load_point(db, point_id)
    impact = await impact_of_point(db, point_id)
    return DeletionImpactOut(**asdict(impact))


@router.delete("/{point_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_point(
    point_id: UUID,
    request: Request,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Removes the point and its history (route stops, executions, photos) — decided by the
    product owner (spec 2026-10-20-predios-pontos-dialogs-design.md §3.4)."""
    point = await _load_point(db, point_id)
    impact = await impact_of_point(db, point_id)
    before = {"name": point.name, **asdict(impact)}
    photo_keys = await delete_point(db, point_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="service_point",
        entity_id=point_id,
        action="delete",
        before=before,
        after=None,
    )
    await db.commit()
    await remove_photo_objects(request.app.state.object_store, photo_keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
