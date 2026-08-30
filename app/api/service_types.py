"""Service type CRUD for managers/admins (RF07 prerequisite). No hard-delete endpoint —
service types are referenced by field worker qualifications and routes, so removing one
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
from app.domain.catalog.models import ServiceType
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/service-types", tags=["service-types"])


class ServiceTypeOut(BaseModel):
    id: UUID
    name: str
    average_duration_minutes: int


class ServiceTypeCreateRequest(BaseModel):
    name: str
    average_duration_minutes: int


class ServiceTypeUpdateRequest(BaseModel):
    name: str
    average_duration_minutes: int


def _to_out(service_type: ServiceType) -> ServiceTypeOut:
    return ServiceTypeOut(
        id=service_type.id,
        name=service_type.name,
        average_duration_minutes=service_type.average_duration_minutes,
    )


@router.get("", response_model=list[ServiceTypeOut])
async def list_service_types(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ServiceTypeOut]:
    items = (await db.scalars(select(ServiceType))).all()
    return [_to_out(item) for item in items]


@router.post("", response_model=ServiceTypeOut, status_code=status.HTTP_201_CREATED)
async def create_service_type(
    body: ServiceTypeCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServiceTypeOut:
    service_type = ServiceType(
        name=body.name, average_duration_minutes=body.average_duration_minutes
    )
    db.add(service_type)
    await db.flush()
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="service_type",
        entity_id=service_type.id,
        action="create",
        before=None,
        after={
            "name": service_type.name,
            "average_duration_minutes": service_type.average_duration_minutes,
        },
    )
    await db.commit()
    return _to_out(service_type)


@router.patch("/{service_type_id}", response_model=ServiceTypeOut)
async def update_service_type(
    service_type_id: UUID,
    body: ServiceTypeUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ServiceTypeOut:
    service_type = await db.get(ServiceType, service_type_id)
    if service_type is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Service type "{service_type_id}" not found.'
        )

    before = {
        "name": service_type.name,
        "average_duration_minutes": service_type.average_duration_minutes,
    }
    service_type.name = body.name
    service_type.average_duration_minutes = body.average_duration_minutes
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="service_type",
        entity_id=service_type.id,
        action="update",
        before=before,
        after={
            "name": service_type.name,
            "average_duration_minutes": service_type.average_duration_minutes,
        },
    )
    await db.commit()
    return _to_out(service_type)
