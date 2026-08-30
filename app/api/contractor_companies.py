"""Contractor company CRUD for managers/admins (RF08 prerequisite). No hard-delete endpoint —
field workers reference their contractor company by foreign key, so removing one outright
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
from app.domain.catalog.models import ContractorCompany
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/contractor-companies", tags=["contractor-companies"])


class ContractorCompanyOut(BaseModel):
    id: UUID
    name: str
    cnpj: str


class ContractorCompanyCreateRequest(BaseModel):
    name: str
    cnpj: str


class ContractorCompanyUpdateRequest(BaseModel):
    name: str
    cnpj: str


def _to_out(company: ContractorCompany) -> ContractorCompanyOut:
    return ContractorCompanyOut(id=company.id, name=company.name, cnpj=company.cnpj)


@router.get("", response_model=list[ContractorCompanyOut])
async def list_contractor_companies(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ContractorCompanyOut]:
    companies = (await db.scalars(select(ContractorCompany))).all()
    return [_to_out(company) for company in companies]


@router.post("", response_model=ContractorCompanyOut, status_code=status.HTTP_201_CREATED)
async def create_contractor_company(
    body: ContractorCompanyCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ContractorCompanyOut:
    company = ContractorCompany(name=body.name, cnpj=body.cnpj)
    db.add(company)
    try:
        await db.flush()
    except IntegrityError as exc:
        # BUG FIX (Finding I2.5): ContractorCompany.cnpj is unique — without this, a duplicate
        # cnpj hits an unhandled IntegrityError (500) instead of a clean 409.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f'Contractor company with CNPJ "{body.cnpj}" already exists.'
        ) from exc
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="contractor_company",
        entity_id=company.id,
        action="create",
        before=None,
        after={"name": company.name, "cnpj": company.cnpj},
    )
    await db.commit()
    return _to_out(company)


@router.patch("/{company_id}", response_model=ContractorCompanyOut)
async def update_contractor_company(
    company_id: UUID,
    body: ContractorCompanyUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ContractorCompanyOut:
    company = await db.get(ContractorCompany, company_id)
    if company is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Contractor company "{company_id}" not found.'
        )

    before = {"name": company.name, "cnpj": company.cnpj}
    company.name = body.name
    company.cnpj = body.cnpj
    try:
        await record_audit_trail(
            db,
            actor_id=actor.id,
            entity_type="contractor_company",
            entity_id=company.id,
            action="update",
            before=before,
            after={"name": company.name, "cnpj": company.cnpj},
        )
        await db.commit()
    except IntegrityError as exc:
        # Same duplicate-cnpj protection as create_contractor_company — cnpj is settable here too.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f'Contractor company with CNPJ "{body.cnpj}" already exists.'
        ) from exc
    return _to_out(company)
