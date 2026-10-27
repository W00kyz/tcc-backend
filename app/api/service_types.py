"""Read-only list of service types for managers/admins. Types are created, renamed and removed
inside their company (app/api/contractor_companies.py), never on their own."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.catalog.models import ServiceType
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/service-types", tags=["service-types"])


class ServiceTypeOut(BaseModel):
    id: UUID
    name: str
    average_duration_minutes: int
    contractor_company_id: UUID | None
    contractor_company_name: str | None


def _to_out(service_type: ServiceType) -> ServiceTypeOut:
    company = service_type.contractor_company
    return ServiceTypeOut(
        id=service_type.id,
        name=service_type.name,
        average_duration_minutes=service_type.average_duration_minutes,
        contractor_company_id=service_type.contractor_company_id,
        contractor_company_name=company.name if company else None,
    )


@router.get("", response_model=list[ServiceTypeOut])
async def list_service_types(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    unassigned: bool = False,
) -> list[ServiceTypeOut]:
    """Every type, or with `?unassigned=true` only those no company has adopted yet."""
    query = (
        select(ServiceType)
        .options(selectinload(ServiceType.contractor_company))
        .order_by(ServiceType.name)
    )
    if unassigned:
        query = query.where(ServiceType.contractor_company_id.is_(None))
    return [_to_out(item) for item in (await db.scalars(query)).all()]
