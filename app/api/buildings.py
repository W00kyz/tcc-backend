"""Building CRUD for managers/admins (RF06 prerequisite). No hard-delete endpoint —
buildings are referenced by floors, service points, and routes, so removing one
outright would orphan history; create/list/edit is all this task needs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import Building
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/buildings", tags=["buildings"])


class BuildingOut(BaseModel):
    id: UUID
    name: str
    campus_area: str


class BuildingCreateRequest(BaseModel):
    name: str
    campus_area: str


class BuildingUpdateRequest(BaseModel):
    name: str
    campus_area: str


def _to_out(building: Building) -> BuildingOut:
    return BuildingOut(id=building.id, name=building.name, campus_area=building.campus_area)


@router.get("", response_model=list[BuildingOut])
async def list_buildings(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[BuildingOut]:
    buildings = (await db.scalars(select(Building))).all()
    return [_to_out(building) for building in buildings]


@router.post("", response_model=BuildingOut, status_code=status.HTTP_201_CREATED)
async def create_building(
    body: BuildingCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BuildingOut:
    building = Building(name=body.name, campus_area=body.campus_area)
    db.add(building)
    await db.flush()
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="building",
        entity_id=building.id,
        action="create",
        before=None,
        after={"name": building.name, "campus_area": building.campus_area},
    )
    await db.commit()
    return _to_out(building)


@router.patch("/{building_id}", response_model=BuildingOut)
async def update_building(
    building_id: UUID,
    body: BuildingUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BuildingOut:
    building = await db.get(Building, building_id)
    if building is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Building "{building_id}" not found.')

    before = {"name": building.name, "campus_area": building.campus_area}
    building.name = body.name
    building.campus_area = body.campus_area
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="building",
        entity_id=building.id,
        action="update",
        before=before,
        after={"name": building.name, "campus_area": building.campus_area},
    )
    await db.commit()
    return _to_out(building)
