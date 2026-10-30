"""Building CRUD for managers/admins (RF06 prerequisite). A building is always registered with
its outline, which can be redrawn but never cleared. Removing a building takes its floors, QR
codes, points and their history with it (app/domain/catalog/deletion.py)."""

from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.deletion import (
    delete_building,
    impact_of_building,
    remove_photo_objects,
)
from app.domain.catalog.floors import GROUND_FLOOR_LABEL
from app.domain.catalog.footprint import PolygonFootprint
from app.domain.catalog.models import Building, Floor
from app.domain.catalog.points import place_points_of_building
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
    # Every building is registered with its geometry: the manager draws it on the map first.
    footprint: PolygonFootprint


class BuildingUpdateRequest(BaseModel):
    name: str
    campus_area: str


class BuildingFootprintRequest(BaseModel):
    footprint: PolygonFootprint
    # Present when the outline was picked from OpenStreetMap; absent when the manager drew it
    # by hand on the dashboard map. OSM way ids are positive and the column is a BIGINT:
    # anything else is a client bug (422), not a value to store or an overflow to surface as
    # a 500.
    osm_way_id: int | None = Field(default=None, gt=0, lt=2**63)


class DeletionImpactOut(BaseModel):
    """What a removal takes with it — shown before the manager confirms."""

    points: int
    floors: int
    routes: int
    executions: int
    photos: int
    appeals: int


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


async def _add_ground_floor(db: AsyncSession, actor: User, building: Building) -> None:
    """Every building starts with its Térreo, in the same transaction as the building."""
    floor = Floor(building_id=building.id, label=GROUND_FLOOR_LABEL, level=0)
    db.add(floor)
    await db.flush()
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="floor",
        entity_id=floor.id,
        action="create",
        before=None,
        after={"building_id": str(floor.building_id), "label": floor.label},
    )


@router.post("", response_model=BuildingOut, status_code=status.HTTP_201_CREATED)
async def create_building(
    body: BuildingCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BuildingOut:
    building = Building(
        name=body.name,
        campus_area=body.campus_area,
        footprint=body.footprint.model_dump(),
        footprint_source="drawn",
    )
    db.add(building)
    await db.flush()
    await _add_ground_floor(db, actor, building)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="building",
        entity_id=building.id,
        action="create",
        before=None,
        after={
            "name": building.name,
            "campus_area": building.campus_area,
            "footprint_source": building.footprint_source,
        },
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
    # A redrawn outline no longer matches any OSM way, so the old link goes with it.
    building.footprint_source = "drawn" if body.osm_way_id is None else "osm"
    building.osm_way_id = body.osm_way_id
    # The building's points are where the building is: they follow a redrawn outline.
    await place_points_of_building(db, building)
    await _audit_footprint(db, actor, building, "set_footprint", before)
    await db.commit()
    return _to_out(building)


@router.get("/{building_id}/deletion-impact", response_model=DeletionImpactOut)
async def building_deletion_impact(
    building_id: UUID,
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeletionImpactOut:
    await _load_building(db, building_id)
    impact = await impact_of_building(db, building_id)
    return DeletionImpactOut(**asdict(impact))


@router.delete("/{building_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_building_endpoint(
    building_id: UUID,
    request: Request,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Removes the building with its floors, QR codes, points and their history — decided by
    the product owner (spec 2026-10-20-predios-pontos-dialogs-design.md §3.4)."""
    building = await _load_building(db, building_id)
    impact = await impact_of_building(db, building_id)
    before = {"name": building.name, **asdict(impact)}
    photo_keys = await delete_building(db, building_id)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="building",
        entity_id=building_id,
        action="delete",
        before=before,
        after=None,
    )
    await db.commit()
    await remove_photo_objects(request.app.state.object_store, photo_keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
