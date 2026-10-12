"""Building CRUD for managers/admins (RF06 prerequisite). No hard-delete endpoint —
buildings are referenced by floors, service points, and routes, so removing one
outright would orphan history; create/list/edit is all this task needs."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.footprint import PolygonFootprint
from app.domain.catalog.models import Building
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/buildings", tags=["buildings"])


class BuildingOut(BaseModel):
    id: UUID
    name: str
    campus_area: str
    footprint: dict[str, Any] | None = None
    footprint_source: str | None = None
    osm_way_id: int | None = None


class BuildingCreateRequest(BaseModel):
    name: str
    campus_area: str


class BuildingUpdateRequest(BaseModel):
    name: str
    campus_area: str


class BuildingFootprintRequest(BaseModel):
    footprint: PolygonFootprint
    # OSM way ids are positive and the column is a BIGINT: anything else is a client bug (422),
    # not a value to store or an overflow to surface as a 500.
    osm_way_id: int = Field(gt=0, lt=2**63)


def _to_out(building: Building) -> BuildingOut:
    return BuildingOut(
        id=building.id,
        name=building.name,
        campus_area=building.campus_area,
        footprint=building.footprint,
        footprint_source=building.footprint_source,
        osm_way_id=building.osm_way_id,
    )


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


async def _load_building(db: AsyncSession, building_id: UUID) -> Building:
    building = await db.get(Building, building_id)
    if building is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Building "{building_id}" not found.')
    return building


async def _audit_footprint(
    db: AsyncSession, actor: User, building: Building, action: str, before: int | None
) -> None:
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="building",
        entity_id=building.id,
        action=action,
        before={"osm_way_id": before},
        after={"osm_way_id": building.osm_way_id},
    )


@router.put("/{building_id}/footprint", response_model=BuildingOut)
async def set_building_footprint(
    building_id: UUID,
    body: BuildingFootprintRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BuildingOut:
    building = await _load_building(db, building_id)
    before = building.osm_way_id
    building.footprint = body.footprint.model_dump()
    building.footprint_source = "osm"
    building.osm_way_id = body.osm_way_id
    await _audit_footprint(db, actor, building, "set_footprint", before)
    await db.commit()
    return _to_out(building)


@router.delete("/{building_id}/footprint", response_model=BuildingOut)
async def clear_building_footprint(
    building_id: UUID,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BuildingOut:
    building = await _load_building(db, building_id)
    before = building.osm_way_id
    building.footprint = None
    building.footprint_source = None
    building.osm_way_id = None
    await _audit_footprint(db, actor, building, "clear_footprint", before)
    await db.commit()
    return _to_out(building)
