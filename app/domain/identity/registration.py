"""A field worker signs themselves up (spec 2026-10-20-alocacao-automatica §5.2): the account,
the worker profile awaiting a manager's approval and the acceptance of the privacy notice are
created together, so a registration never leaves half of them behind."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import ContractorCompany, FieldWorker, FieldWorkerStatus
from app.domain.identity.models import User, UserRole
from app.domain.privacy.service import current_notice, record_consent


class EmailTaken(Exception):  # noqa: N818 — a domain refusal, named for what it says
    """An account with this e-mail already exists."""


class NoticeOutdated(Exception):  # noqa: N818
    """The privacy notice version the client showed is not the current one."""


class UnknownCompany(Exception):  # noqa: N818
    """The chosen contractor company does not exist."""


async def register_field_worker(
    db: AsyncSession,
    *,
    name: str,
    email: str,
    password: str,
    company_id: uuid.UUID,
    notice_version: int,
    ip_address: str | None,
    user_agent: str | None = None,
) -> FieldWorker:
    """Create the FIELD_WORKER account, its PENDING_APPROVAL profile and the consent record.
    Flushes, never commits — the caller owns the transaction.

        worker = await register_field_worker(db, name="Ana", email="ana@x.com", password="...",
                                             company_id=company.id, notice_version=1,
                                             ip_address="10.0.0.1")
        await db.commit()
    """
    if (await current_notice(db)).version != notice_version:
        raise NoticeOutdated
    if await db.get(ContractorCompany, company_id) is None:
        raise UnknownCompany
    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise EmailTaken

    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        role=UserRole.FIELD_WORKER,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:  # two registrations with the same e-mail racing each other
        raise EmailTaken from exc

    worker = FieldWorker(
        full_name=name,
        contractor_company_id=company_id,
        user_id=user.id,
        status=FieldWorkerStatus.PENDING_APPROVAL,
    )
    db.add(worker)
    await db.flush()
    await record_consent(db, user_id=user.id, ip_address=ip_address, user_agent=user_agent)
    await record_audit_trail(
        db,
        actor_id=user.id,
        entity_type="field_worker",
        entity_id=worker.id,
        action="register",
        before=None,
        after={"full_name": name, "contractor_company_id": str(company_id)},
    )
    return worker
