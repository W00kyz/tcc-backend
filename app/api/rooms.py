"""Room CRUD for managers/admins (spec 2026-10-29-andares-salas §4 and §5). A new room gets a
default cleaning point; other services are added one point per (room, service type)."""

from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deletion_impact import DeletionImpactOut
from app.api.deps import require_role
from app.api.point_checks import add_audited_point, require_floor_location, require_service_type
from app.api.service_points import ServicePointOut, point_to_out
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.deletion import delete_room, impact_of_room, remove_photo_objects
from app.domain.catalog.models import Floor, Room, ServicePoint
from app.domain.catalog.points import floor_location
from app.domain.catalog.rooms import RoomLimitReached, find_cleaning_type, next_room_name
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/rooms", tags=["rooms"])

CleaningOutcome = Literal["CREATED", "SKIPPED", "NO_CLEANING_TYPE", "NO_OUTLINE"]
RoomName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RoomOut(BaseModel):
    id: UUID
    floor_id: UUID
    name: str


class RoomCreateRequest(BaseModel):
    floor_id: UUID
    # Omitted or blank, the server numbers the room by its floor ("101", "102", ...).
    name: Annotated[str, StringConstraints(max_length=200)] | None = None
    with_cleaning: bool = True


class RoomCreateOut(RoomOut):
    # Why the default cleaning point is missing, so the dashboard can say so.
    cleaning: CleaningOutcome


class RoomUpdateRequest(BaseModel):
    name: RoomName


class RoomServiceRequest(BaseModel):
    service_type_id: UUID


def _to_out(room: Room) -> RoomOut:
    return RoomOut(id=room.id, floor_id=room.floor_id, name=room.name)


def _name_in_use(name: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, f'Room "{name}" already exists on this floor; pick another name.'
    )


async def _require_room(db: AsyncSession, room_id: UUID) -> Room:
    room = await db.get(Room, room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Room "{room_id}" not found.')
    return room


async def _default_name(db: AsyncSession, floor: Floor) -> str:
    existing = list(await db.scalars(select(Room.name).where(Room.floor_id == floor.id)))
    try:
        return next_room_name(floor.level, existing)
    except RoomLimitReached as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Floor "{floor.label}" has no default room number left; give the room a name. ({exc})',
        ) from exc


async def _add_cleaning_point(
    db: AsyncSession, actor: User, floor: Floor, room: Room
) -> CleaningOutcome:
    """The room's default Limpeza point, or why there is none (the room is kept either way)."""
    cleaning = await find_cleaning_type(db)
    if cleaning is None:
        return "NO_CLEANING_TYPE"
    location = await floor_location(db, floor)
    if location is None:
        return "NO_OUTLINE"
    await add_audited_point(
        db,
        actor,
        floor_id=floor.id,
        name=room.name,
        description="",
        service_type=cleaning,
        location=location,
        room_id=room.id,
    )
    return "CREATED"


@router.get("", response_model=list[RoomOut])
async def list_rooms(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    floor_id: UUID | None = None,
    building_id: UUID | None = None,
) -> list[RoomOut]:
    # Registration order, so a renamed room does not jump to the end of the floor's list.
    query = select(Room).order_by(Room.created_at, Room.id)
    if floor_id is not None:
        query = query.where(Room.floor_id == floor_id)
    if building_id is not None:
        query = query.join(Floor, Room.floor_id == Floor.id).where(Floor.building_id == building_id)
    return [_to_out(room) for room in (await db.scalars(query)).all()]


@router.post("", response_model=RoomCreateOut, status_code=status.HTTP_201_CREATED)
async def create_room(
    body: RoomCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RoomCreateOut:
    floor = await db.get(Floor, body.floor_id)
    if floor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Floor "{body.floor_id}" not found.')
    typed = (body.name or "").strip()
    name = typed or await _default_name(db, floor)

    room = Room(floor_id=floor.id, name=name)
    db.add(room)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _name_in_use(name) from exc
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="room",
        entity_id=room.id,
        action="create",
        before=None,
        after={"floor_id": str(room.floor_id), "name": room.name},
    )
    cleaning: CleaningOutcome = "SKIPPED"
    if body.with_cleaning:
        cleaning = await _add_cleaning_point(db, actor, floor, room)
    await db.commit()
    return RoomCreateOut(**_to_out(room).model_dump(), cleaning=cleaning)


@router.patch("/{room_id}", response_model=RoomOut)
async def update_room(
    room_id: UUID,
    body: RoomUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RoomOut:
    room = await _require_room(db, room_id)
    before = {"name": room.name}
    room.name = body.name
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _name_in_use(body.name) from exc
    # The point carries the room's name: it is what the field worker sees in the app.
    await db.execute(
        update(ServicePoint)
        .where(ServicePoint.room_id == room.id)
        .values(name=room.name)
        .execution_options(synchronize_session=False)
    )
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="room",
        entity_id=room.id,
        action="update",
        before=before,
        after={"name": room.name},
    )
    await db.commit()
    return _to_out(room)


@router.post(
    "/{room_id}/services", response_model=ServicePointOut, status_code=status.HTTP_201_CREATED
)
async def add_room_service(
    room_id: UUID,
    body: RoomServiceRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServicePointOut:
    room = await _require_room(db, room_id)
    floor = await db.get(Floor, room.floor_id)
    assert floor is not None  # rooms.floor_id is a foreign key
    service_type = await require_service_type(db, body.service_type_id)
    await _reject_repeated_service(db, room, body)
    location = await require_floor_location(db, floor)
    point = await add_audited_point(
        db,
        actor,
        floor_id=floor.id,
        name=room.name,
        description="",
        service_type=service_type,
        location=location,
        room_id=room.id,
    )
    await db.commit()
    return point_to_out(point)


async def _reject_repeated_service(db: AsyncSession, room: Room, body: RoomServiceRequest) -> None:
    """At most one point per (room, service type); no DB constraint, so checked here."""
    repeated = await db.scalar(
        select(ServicePoint.id).where(
            ServicePoint.room_id == room.id,
            ServicePoint.service_type_id == body.service_type_id,
        )
    )
    if repeated is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "ROOM_SERVICE_EXISTS",
                "detail": f'Room "{room.name}" already has this service; edit or remove its '
                "point instead.",
                "service_type_id": str(body.service_type_id),
            },
        )


@router.get("/{room_id}/deletion-impact", response_model=DeletionImpactOut)
async def room_deletion_impact(
    room_id: UUID,
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeletionImpactOut:
    await _require_room(db, room_id)
    impact = await impact_of_room(db, room_id)
    return DeletionImpactOut(**asdict(impact))


@router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room_endpoint(
    room_id: UUID,
    request: Request,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Removes the room with its points and their history; the floor and the other rooms stay
    (spec 2026-10-29-andares-salas-design.md §4 "Remover sala")."""
    room = await _require_room(db, room_id)
    impact = await impact_of_room(db, room_id)
    before = {"name": room.name, **asdict(impact)}
    photo_keys = await delete_room(db, room_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="room",
        entity_id=room_id,
        action="delete",
        before=before,
        after=None,
    )
    await db.commit()
    await remove_photo_objects(request.app.state.object_store, photo_keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
