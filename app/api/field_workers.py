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
from app.domain.catalog.models import FieldWorker, FieldWorkerServiceType
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/field-workers", tags=["field-workers"])


class FieldWorkerOut(BaseModel):
    id: UUID
    full_name: str
    contractor_company_id: UUID
    service_type_ids: list[UUID]
    user_id: UUID | None


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
    claimed by a different field worker."""
    if user_id is None:
        return
    conflict = await db.scalar(
        select(FieldWorker).where(FieldWorker.user_id == user_id, FieldWorker.id != worker_id)
    )
    if conflict is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f'User "{user_id}" is already linked to another field worker.',
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


@router.post("", response_model=FieldWorkerOut, status_code=status.HTTP_201_CREATED)
async def create_field_worker(
    body: FieldWorkerCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FieldWorkerOut:
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
