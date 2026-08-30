"""Field worker CRUD for managers/admins (RF08). No hard-delete endpoint — field workers are
referenced by routes and check-ins, so removing one outright would orphan history; create/list/
edit is all this task needs. `user_id` optionally links to an existing FIELD_WORKER-role User
(spec Ruling 6) — that link is created or edited here, never via a combined signup screen."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import ContractorCompany, FieldWorker, FieldWorkerServiceType
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/field-workers", tags=["field-workers"])


class FieldWorkerOut(BaseModel):
    id: UUID
    full_name: str
    contractor_company_id: UUID
    service_type_ids: list[UUID]
    user_id: UUID | None


class LinkableUserOut(BaseModel):
    """Minimal projection for the dashboard /profissionais link-dropdown (Finding C2) — a
    dedicated model instead of importing UserOut from app/api/users.py, so this router stays
    self-contained per this repo's "no cross-router imports" convention."""

    id: UUID
    name: str
    email: str


class FieldWorkerCreateRequest(BaseModel):
    full_name: str
    contractor_company_id: UUID
    service_type_ids: list[UUID]
    user_id: UUID | None = None


class FieldWorkerUpdateRequest(BaseModel):
    full_name: str
    contractor_company_id: UUID
    service_type_ids: list[UUID]
    user_id: UUID | None = None


async def _load(db: AsyncSession, worker_id: UUID) -> FieldWorker | None:
    # Assigned before returning, not `return await db.scalar(...)` — mypy loses the generic
    # through a direct-return await chained onto .options() and reports it as Any.
    #
    # BUG FIX: populate_existing=True is required here. The session has expire_on_commit=False
    # (app/db/session.py), and _sync_service_types() mutates FieldWorkerServiceType rows through
    # a separately queried set — FieldWorker.service_type_links has no back_populates, so those
    # writes never touch the collection already cached on a `worker` instance sitting in this
    # session's identity map. Without populate_existing, a second _load() for the same worker_id
    # returns that instance with its stale, pre-update collection instead of re-reading it from
    # this query's result — the update handler's response would echo the old service types even
    # though the database write is correct (a follow-up GET in a fresh request shows the truth).
    worker = await db.scalar(
        select(FieldWorker)
        .where(FieldWorker.id == worker_id)
        .options(selectinload(FieldWorker.service_type_links))
        .execution_options(populate_existing=True)
    )
    return worker


def _to_out(worker: FieldWorker) -> FieldWorkerOut:
    return FieldWorkerOut(
        id=worker.id,
        full_name=worker.full_name,
        contractor_company_id=worker.contractor_company_id,
        service_type_ids=[link.service_type_id for link in worker.service_type_links],
        user_id=worker.user_id,
    )


async def _assert_user_available(
    db: AsyncSession, user_id: UUID | None, worker_id: UUID | None
) -> None:
    """RF08 links an existing FIELD_WORKER-role User to a FieldWorker — spec Ruling 6. A User
    links to at most one FieldWorker, so both create and update must reject a `user_id` already
    claimed by a different field worker.

    BUG FIX (Finding I1): the uniqueness check alone never verified that `user_id` actually
    points at a FIELD_WORKER-role User — linking an ADMIN's or MANAGER's user_id used to
    succeed silently. This fetches the User (also turning an unknown user_id into a clean 404
    instead of a later FK-violation 500 on insert) and rejects any role other than
    FIELD_WORKER."""
    if user_id is None:
        return
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'User "{user_id}" not found.')
    if user.role != UserRole.FIELD_WORKER:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'User "{user_id}" has role {user.role.value}; only a FIELD_WORKER-role user can be'
            " linked to a field worker.",
        )
    conflict = await db.scalar(
        select(FieldWorker).where(FieldWorker.user_id == user_id, FieldWorker.id != worker_id)
    )
    if conflict is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'User "{user_id}" is already linked to another field worker.',
        )


async def _assert_contractor_company_exists(db: AsyncSession, company_id: UUID) -> None:
    # BUG FIX (Finding I2.3): without this check, an unknown contractor_company_id only fails
    # later at the FieldWorker insert/update's FK constraint, surfacing as an unhandled
    # IntegrityError (500) instead of a clean 404 — same existence-check-before-write pattern
    # as update_floor in app/api/floors.py.
    company = await db.get(ContractorCompany, company_id)
    if company is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Contractor company "{company_id}" not found.'
        )


async def _sync_service_types(
    db: AsyncSession, worker: FieldWorker, service_type_ids: list[UUID]
) -> None:
    # BUG FIX: a raw FieldWorkerServiceType.__table__.delete() bypasses the ORM identity map, so
    # on update the just-selectinload()-ed link rows stay identity-mapped while the re-add below
    # creates new objects with the same (field_worker_id, service_type_id) primary key —
    # SQLAlchemy warns of a conflicting/replaced identity. Deleting through the ORM instead keeps
    # the identity map consistent with the database. `worker.service_type_links` itself isn't
    # touched here — on create the relationship is unloaded on the just-flushed worker, and
    # reading it would force a lazy SELECT outside of async-safe context (MissingGreenlet); an
    # explicit, awaited query sidesteps that for both create and update.
    existing_links = (
        await db.scalars(
            select(FieldWorkerServiceType).where(
                FieldWorkerServiceType.field_worker_id == worker.id
            )
        )
    ).all()
    for link in existing_links:
        await db.delete(link)
    await db.flush()
    for service_type_id in service_type_ids:
        db.add(FieldWorkerServiceType(field_worker_id=worker.id, service_type_id=service_type_id))
    await db.flush()


@router.get("", response_model=list[FieldWorkerOut])
async def list_field_workers(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[FieldWorkerOut]:
    workers = (
        await db.scalars(select(FieldWorker).options(selectinload(FieldWorker.service_type_links)))
    ).all()
    return [_to_out(worker) for worker in workers]


@router.get("/linkable-users", response_model=list[LinkableUserOut])
async def list_linkable_users(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[LinkableUserOut]:
    """Finding C2: the dashboard's /profissionais screen needs FIELD_WORKER-role Users not yet
    linked to a FieldWorker, for its link dropdown. GET /users is Admin-only (RF05), so a
    Manager gets 403 there — this router serves its own minimal projection instead. Registered
    before any `/{worker_id}`-shaped route so "linkable-users" is never captured as a path
    parameter (this router currently has no GET "/{worker_id}", so no collision exists today
    either way, but the ordering is kept safe against one being added later)."""
    linked_user_ids = select(FieldWorker.user_id).where(FieldWorker.user_id.is_not(None))
    users = (
        await db.scalars(
            select(User).where(User.role == UserRole.FIELD_WORKER, User.id.not_in(linked_user_ids))
        )
    ).all()
    return [LinkableUserOut(id=user.id, name=user.name, email=user.email) for user in users]


@router.post("", response_model=FieldWorkerOut, status_code=status.HTTP_201_CREATED)
async def create_field_worker(
    body: FieldWorkerCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FieldWorkerOut:
    await _assert_contractor_company_exists(db, body.contractor_company_id)
    await _assert_user_available(db, body.user_id, worker_id=None)
    worker = FieldWorker(
        full_name=body.full_name,
        contractor_company_id=body.contractor_company_id,
        user_id=body.user_id,
    )
    db.add(worker)
    await db.flush()
    await _sync_service_types(db, worker, body.service_type_ids)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="field_worker",
        entity_id=worker.id,
        action="create",
        before=None,
        after={
            "full_name": worker.full_name,
            "contractor_company_id": str(worker.contractor_company_id),
        },
    )
    await db.commit()
    reloaded = await _load(db, worker.id)
    assert reloaded is not None
    return _to_out(reloaded)


@router.patch("/{worker_id}", response_model=FieldWorkerOut)
async def update_field_worker(
    worker_id: UUID,
    body: FieldWorkerUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FieldWorkerOut:
    worker = await _load(db, worker_id)
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Field worker "{worker_id}" not found.')
    await _assert_contractor_company_exists(db, body.contractor_company_id)
    await _assert_user_available(db, body.user_id, worker_id=worker.id)

    before = {
        "full_name": worker.full_name,
        "user_id": str(worker.user_id) if worker.user_id else None,
    }
    worker.full_name = body.full_name
    worker.contractor_company_id = body.contractor_company_id
    worker.user_id = body.user_id
    await _sync_service_types(db, worker, body.service_type_ids)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="field_worker",
        entity_id=worker.id,
        action="update",
        before=before,
        after={
            "full_name": worker.full_name,
            "user_id": str(worker.user_id) if worker.user_id else None,
        },
    )
    await db.commit()
    reloaded = await _load(db, worker.id)
    assert reloaded is not None
    return _to_out(reloaded)
