"""Floor CRUD for managers/admins (RF06 prerequisite). Removing a floor takes its rooms, QR
codes, points and their history with it (app/domain/catalog/deletion.py)."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deletion_impact import DeletionImpactOut
from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.deletion import delete_floor, impact_of_floor, remove_photo_objects
from app.domain.catalog.floors import next_floor
from app.domain.catalog.models import Building, Floor
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/floors", tags=["floors"])


class FloorOut(BaseModel):
    id: UUID
    building_id: UUID
    label: str
    level: int


class FloorCreateRequest(BaseModel):
    building_id: UUID
    # Omitted, the server picks it: "Térreo" for the first floor, then the next number.
    label: str | None = None


class FloorUpdateRequest(BaseModel):
    label: str


def _to_out(floor: Floor) -> FloorOut:
    return FloorOut(
        id=floor.id, building_id=floor.building_id, label=floor.label, level=floor.level
    )


@router.get("", response_model=list[FloorOut])
async def list_floors(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    building_id: UUID | None = None,
) -> list[FloorOut]:
    # Level first, then registration order (id breaks ties): without an ORDER BY, Postgres
    # returns an edited row last and the dashboard's floor tabs reshuffle after a rename.
    query = select(Floor).order_by(Floor.level, Floor.created_at, Floor.id)
    if building_id is not None:
        query = query.where(Floor.building_id == building_id)
    floors = (await db.scalars(query)).all()
    return [_to_out(floor) for floor in floors]


@router.post("", response_model=FloorOut, status_code=status.HTTP_201_CREATED)
async def create_floor(
    body: FloorCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FloorOut:
    # BUG FIX (Finding I2.1): without this check, an unknown building_id used to hit the
    # IntegrityError handler below, which only ever expected the (building_id, label) unique
    # violation and always reported it as a duplicate-label 409 — factually wrong for an FK
    # violation. This existence check turns that case into a clean 404 before the insert is
    # even attempted; the genuine duplicate-label case still falls through to the 409 below.
    building = await db.get(Building, body.building_id)
    if building is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Building "{body.building_id}" not found.')

    levels = list(
        await db.scalars(select(Floor.level).where(Floor.building_id == body.building_id))
    )
    level, default_label = next_floor(levels)
    # A typed label wins over the default; the level is positional either way.
    label = body.label if body.label is not None else default_label

    floor = Floor(building_id=body.building_id, label=label, level=level)
    db.add(floor)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Floor "{label}" already exists for this building.',
        ) from exc
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="floor",
        entity_id=floor.id,
        action="create",
        before=None,
        after={"building_id": str(floor.building_id), "label": floor.label},
    )
    await db.commit()
    return _to_out(floor)


@router.patch("/{floor_id}", response_model=FloorOut)
async def update_floor(
    floor_id: UUID,
    body: FloorUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FloorOut:
    floor = await db.get(Floor, floor_id)
    if floor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Floor "{floor_id}" not found.')

    before = {"label": floor.label}
    floor.label = body.label
    try:
        await record_audit_trail(
            db,
            actor_id=actor.id,
            entity_type="floor",
            entity_id=floor.id,
            action="update",
            before=before,
            after={"label": floor.label},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Floor "{body.label}" already exists for this building.',
        ) from exc
    return _to_out(floor)


async def _load_floor(db: AsyncSession, floor_id: UUID) -> Floor:
    floor = await db.get(Floor, floor_id)
    if floor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Floor "{floor_id}" not found.')
    return floor


@router.get("/{floor_id}/deletion-impact", response_model=DeletionImpactOut)
async def floor_deletion_impact(
    floor_id: UUID,
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeletionImpactOut:
    await _load_floor(db, floor_id)
    impact = await impact_of_floor(db, floor_id)
    return DeletionImpactOut(**asdict(impact))


@router.delete("/{floor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_floor_endpoint(
    floor_id: UUID,
    request: Request,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Removes the floor with its rooms, QR codes, points and their history (spec
    2026-10-29-andares-salas-design.md §3, same rule as the building)."""
    floor = await _load_floor(db, floor_id)
    impact = await impact_of_floor(db, floor_id)
    before = {"label": floor.label, **asdict(impact)}
    photo_keys = await delete_floor(db, floor_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="floor",
        entity_id=floor_id,
        action="delete",
        before=before,
        after=None,
    )
    await db.commit()
    await remove_photo_objects(request.app.state.object_store, photo_keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
