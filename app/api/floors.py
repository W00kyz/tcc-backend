"""Floor CRUD for managers/admins (RF06 prerequisite). No hard-delete endpoint —
floors are referenced by service points and QR codes, so removing one outright
would orphan history; create/list/edit is all this task needs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import Floor
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/floors", tags=["floors"])


class FloorOut(BaseModel):
    id: UUID
    building_id: UUID
    label: str


class FloorCreateRequest(BaseModel):
    building_id: UUID
    label: str


class FloorUpdateRequest(BaseModel):
    label: str


def _to_out(floor: Floor) -> FloorOut:
    return FloorOut(id=floor.id, building_id=floor.building_id, label=floor.label)


@router.get("", response_model=list[FloorOut])
async def list_floors(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    building_id: UUID | None = None,
) -> list[FloorOut]:
    query = select(Floor)
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
    floor = Floor(building_id=body.building_id, label=body.label)
    db.add(floor)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'Floor "{body.label}" already exists for this building.',
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
