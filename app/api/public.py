"""Endpoints that need no login: what the sign-up screen shows before an account exists."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.domain.catalog.models import ContractorCompany

router = APIRouter(prefix="/public", tags=["public"])


class PublicCompanyOut(BaseModel):
    id: UUID
    name: str


@router.get("/contractor-companies", response_model=list[PublicCompanyOut])
async def list_public_companies(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PublicCompanyOut]:
    """The companies a worker can choose from when signing up; name only, never the CNPJ."""
    companies = (await db.scalars(select(ContractorCompany).order_by(ContractorCompany.name))).all()
    return [PublicCompanyOut(id=company.id, name=company.name) for company in companies]
