"""Contractor company CRUD for managers/admins (RF08 prerequisite). No hard-delete endpoint —
field workers reference their contractor company by foreign key, so removing one outright
would orphan history. The company owns its service types: the dialog sends the whole set and
the server reconciles it (spec 2026-10-20-alocacao-automatica §5.1)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.deletion import service_type_has_history
from app.domain.catalog.models import ContractorCompany, ServicePoint, ServiceType
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/contractor-companies", tags=["contractor-companies"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]


class CompanyServiceTypeOut(BaseModel):
    id: UUID
    name: str
    average_duration_minutes: int


class ServiceTypeRow(BaseModel):
    """One line of the company dialog: a type to keep (`id` set) or to create (`id` empty)."""

    id: UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    average_duration_minutes: int = Field(default=30, gt=0)


class ContractorCompanyOut(BaseModel):
    id: UUID
    name: str
    cnpj: str
    service_types: list[CompanyServiceTypeOut]


class ContractorCompanyCreateRequest(BaseModel):
    name: str
    cnpj: str
    service_types: list[ServiceTypeRow]


class ContractorCompanyUpdateRequest(BaseModel):
    name: str
    cnpj: str
    service_types: list[ServiceTypeRow]


def _to_out(company: ContractorCompany) -> ContractorCompanyOut:
    return ContractorCompanyOut(
        id=company.id,
        name=company.name,
        cnpj=company.cnpj,
        service_types=[
            CompanyServiceTypeOut(
                id=item.id, name=item.name, average_duration_minutes=item.average_duration_minutes
            )
            for item in sorted(company.service_types, key=lambda item: item.name)
        ],
    )


def _snapshot(company: ContractorCompany) -> dict[str, object]:
    return {
        "name": company.name,
        "cnpj": company.cnpj,
        "service_types": sorted(item.name for item in company.service_types),
    }


def _conflict(code: str, detail: str, **extra: object) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": code, "detail": detail, **extra})


async def _points_using(db: AsyncSession, service_type_id: UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(ServicePoint)
        .where(ServicePoint.service_type_id == service_type_id)
    )
    return int(count or 0)


async def _reconcile_service_types(
    db: AsyncSession, company: ContractorCompany, rows: list[ServiceTypeRow]
) -> None:
    """Make the company's types exactly `rows`: update the ones it has, adopt untied ones, create
    the new ones and delete the ones left out. Raises before the caller commits, so a refusal
    leaves everything as it was."""
    owned = {item.id: item for item in company.service_types}
    kept_ids = {row.id for row in rows if row.id is not None}

    for service_type_id, service_type in owned.items():
        if service_type_id in kept_ids:
            continue
        points = await _points_using(db, service_type_id)
        if points:
            raise _conflict(
                "SERVICE_TYPE_IN_USE",
                f'Service type "{service_type.name}" is used by {points} service point(s).',
                service_type_id=str(service_type_id),
                points=points,
            )
        if await service_type_has_history(db, service_type_id):
            raise _conflict(
                "SERVICE_TYPE_HAS_HISTORY",
                f'Service type "{service_type.name}" has routes or executions on record.',
                service_type_id=str(service_type_id),
            )
        company.service_types.remove(service_type)
        await db.delete(service_type)
    await db.flush()

    for row in rows:
        if row.id is None:
            company.service_types.append(
                ServiceType(name=row.name, average_duration_minutes=row.average_duration_minutes)
            )
            continue
        found = owned.get(row.id) or await db.get(ServiceType, row.id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'Service type "{row.id}" not found.')
        service_type = found
        if service_type.contractor_company_id not in (None, company.id):
            raise _conflict(
                "SERVICE_TYPE_OF_ANOTHER_COMPANY",
                f'Service type "{service_type.name}" belongs to another company.',
                service_type_id=str(row.id),
            )
        service_type.name = row.name
        service_type.average_duration_minutes = row.average_duration_minutes
        if service_type.contractor_company_id is None:
            company.service_types.append(service_type)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _conflict(
            "SERVICE_TYPE_NAME_TAKEN", "A service type name is already in use."
        ) from exc


@router.get("", response_model=list[ContractorCompanyOut])
async def list_contractor_companies(_actor: _Manager, db: _Db) -> list[ContractorCompanyOut]:
    companies = (
        await db.scalars(
            select(ContractorCompany)
            .options(selectinload(ContractorCompany.service_types))
            .order_by(ContractorCompany.name)
        )
    ).all()
    return [_to_out(company) for company in companies]


@router.post("", response_model=ContractorCompanyOut, status_code=status.HTTP_201_CREATED)
async def create_contractor_company(
    body: ContractorCompanyCreateRequest, actor: _Manager, db: _Db
) -> ContractorCompanyOut:
    company = ContractorCompany(name=body.name, cnpj=body.cnpj)
    company.service_types = []
    db.add(company)
    try:
        await db.flush()
    except IntegrityError as exc:
        # BUG FIX (Finding I2.5): ContractorCompany.cnpj is unique — without this, a duplicate
        # cnpj hits an unhandled IntegrityError (500) instead of a clean 409.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f'Contractor company with CNPJ "{body.cnpj}" already exists.'
        ) from exc
    await _reconcile_service_types(db, company, body.service_types)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="contractor_company",
        entity_id=company.id,
        action="create",
        before=None,
        after=_snapshot(company),
    )
    await db.commit()
    return _to_out(company)


@router.patch("/{company_id}", response_model=ContractorCompanyOut)
async def update_contractor_company(
    company_id: UUID, body: ContractorCompanyUpdateRequest, actor: _Manager, db: _Db
) -> ContractorCompanyOut:
    company = await db.scalar(
        select(ContractorCompany)
        .where(ContractorCompany.id == company_id)
        .options(selectinload(ContractorCompany.service_types))
    )
    if company is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Contractor company "{company_id}" not found.'
        )

    before = _snapshot(company)
    company.name = body.name
    company.cnpj = body.cnpj
    try:
        await db.flush()
    except IntegrityError as exc:
        # Same duplicate-cnpj protection as create_contractor_company — cnpj is settable here too.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f'Contractor company with CNPJ "{body.cnpj}" already exists.'
        ) from exc
    await _reconcile_service_types(db, company, body.service_types)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="contractor_company",
        entity_id=company.id,
        action="update",
        before=before,
        after=_snapshot(company),
    )
    await db.commit()
    return _to_out(company)
