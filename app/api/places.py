"""Outdoor place CRUD for managers/admins (spec 2026-10-30-locais-externos §3). A new place gets
its first service point, which sits at the place's position and is named after it; other services
are added one point per (place, service type). Name and position of a place are the points'."""

from dataclasses import asdict
from typing import Annotated, Any, Self
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deletion_impact import DeletionImpactOut
from app.api.deps import require_role
from app.api.point_checks import add_audited_point, require_service_type
from app.api.service_points import ServicePointOut, point_to_out
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.deletion import delete_place, impact_of_place, remove_photo_objects
from app.domain.catalog.models import Place, ServicePoint
from app.domain.catalog.places import OutsideCampus, ensure_inside_campus
from app.domain.identity.models import User, UserRole
from app.domain.settings.service import get_settings_row

router = APIRouter(prefix="/places", tags=["places"])

# Stable codes of the 4xx the dashboard keys on.
OUTSIDE_CAMPUS = "OUTSIDE_CAMPUS"
PLACE_SERVICE_EXISTS = "PLACE_SERVICE_EXISTS"
PlaceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]


class PlaceOut(BaseModel):
    id: UUID
    name: str
    latitude: float
    longitude: float


class PlaceCreateRequest(BaseModel):
    name: PlaceName
    latitude: Latitude
    longitude: Longitude
    # The place is born with its first point, so the type of that point is part of the request.
    service_type_id: UUID


class PlaceUpdateRequest(BaseModel):
    # Omitted fields stay as they are; at least one has to be sent.
    name: PlaceName | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> Self:
        if self.name is None and self.latitude is None and self.longitude is None:
            raise ValueError("expected at least one of name, latitude and longitude.")
        return self


class PlaceServiceRequest(BaseModel):
    service_type_id: UUID


def _to_out(place: Place) -> PlaceOut:
    return PlaceOut(
        id=place.id, name=place.name, latitude=place.latitude, longitude=place.longitude
    )


def _snapshot(place: Place) -> dict[str, Any]:
    return {"name": place.name, "latitude": place.latitude, "longitude": place.longitude}


def _name_in_use(name: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, f'Place "{name}" already exists; pick another name.'
    )


async def _require_place(db: AsyncSession, place_id: UUID) -> Place:
    place = await db.get(Place, place_id)
    if place is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Place "{place_id}" not found.')
    return place


async def _require_on_campus(db: AsyncSession, latitude: float, longitude: float) -> None:
    try:
        ensure_inside_campus(latitude, longitude, bounds=await get_settings_row(db))
    except OutsideCampus as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": OUTSIDE_CAMPUS, "detail": str(exc)},
        ) from exc


async def _reject_repeated_service(db: AsyncSession, place: Place, service_type_id: UUID) -> None:
    """At most one point per (place, service type); no DB constraint, so checked here."""
    repeated = await db.scalar(
        select(ServicePoint.id).where(
            ServicePoint.place_id == place.id,
            ServicePoint.service_type_id == service_type_id,
        )
    )
    if repeated is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": PLACE_SERVICE_EXISTS,
                "detail": f'Place "{place.name}" already has this service; edit or remove its '
                "point instead.",
                "service_type_id": str(service_type_id),
            },
        )


@router.get("", response_model=list[PlaceOut])
async def list_places(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PlaceOut]:
    # Registration order, so a renamed place does not jump to the end of the list.
    query = select(Place).order_by(Place.created_at, Place.id)
    return [_to_out(place) for place in (await db.scalars(query)).all()]


@router.post("", response_model=PlaceOut, status_code=status.HTTP_201_CREATED)
async def create_place(
    body: PlaceCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlaceOut:
    service_type = await require_service_type(db, body.service_type_id)
    await _require_on_campus(db, body.latitude, body.longitude)
    place = Place(name=body.name, latitude=body.latitude, longitude=body.longitude)
    db.add(place)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _name_in_use(body.name) from exc
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="place",
        entity_id=place.id,
        action="create",
        before=None,
        after=_snapshot(place),
    )
    await add_audited_point(
        db,
        actor,
        place_id=place.id,
        name=place.name,
        description="",
        service_type=service_type,
        location=(place.latitude, place.longitude),
    )
    await db.commit()
    return _to_out(place)


@router.patch("/{place_id}", response_model=PlaceOut)
async def update_place(
    place_id: UUID,
    body: PlaceUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PlaceOut:
    place = await _require_place(db, place_id)
    before = _snapshot(place)
    moved = body.latitude is not None or body.longitude is not None
    latitude = place.latitude if body.latitude is None else body.latitude
    longitude = place.longitude if body.longitude is None else body.longitude
    if moved:
        # Only a move is checked: renaming a place the campus rectangle later left stays possible.
        await _require_on_campus(db, latitude, longitude)
    name = place.name if body.name is None else body.name
    place.name = name
    place.latitude, place.longitude = latitude, longitude
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _name_in_use(name) from exc
    # The points carry the place's name and position: the field worker's app reads them there.
    await db.execute(
        update(ServicePoint)
        .where(ServicePoint.place_id == place.id)
        .values(name=place.name, latitude=place.latitude, longitude=place.longitude)
        .execution_options(synchronize_session=False)
    )
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="place",
        entity_id=place.id,
        action="update",
        before=before,
        after=_snapshot(place),
    )
    await db.commit()
    return _to_out(place)


@router.post(
    "/{place_id}/services", response_model=ServicePointOut, status_code=status.HTTP_201_CREATED
)
async def add_place_service(
    place_id: UUID,
    body: PlaceServiceRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServicePointOut:
    place = await _require_place(db, place_id)
    service_type = await require_service_type(db, body.service_type_id)
    await _reject_repeated_service(db, place, body.service_type_id)
    point = await add_audited_point(
        db,
        actor,
        place_id=place.id,
        name=place.name,
        description="",
        service_type=service_type,
        location=(place.latitude, place.longitude),
    )
    await db.commit()
    return point_to_out(point)


@router.get("/{place_id}/deletion-impact", response_model=DeletionImpactOut)
async def place_deletion_impact(
    place_id: UUID,
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeletionImpactOut:
    await _require_place(db, place_id)
    impact = await impact_of_place(db, place_id)
    return DeletionImpactOut(**asdict(impact))


@router.delete("/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_place_endpoint(
    place_id: UUID,
    request: Request,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Removes the place with its points and their history; the other places and the buildings
    stay (spec 2026-10-30-locais-externos-design.md §3)."""
    place = await _require_place(db, place_id)
    impact = await impact_of_place(db, place_id)
    before = {"name": place.name, **asdict(impact)}
    photo_keys = await delete_place(db, place_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="place",
        entity_id=place_id,
        action="delete",
        before=before,
        after=None,
    )
    await db.commit()
    await remove_photo_objects(request.app.state.object_store, photo_keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
