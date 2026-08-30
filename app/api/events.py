"""Event CRUD for managers/admins (RF22 prerequisite). No hard-delete endpoint —
service points can reference an event, so removing one outright would orphan them;
create/list/edit is all this task needs."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import Event
from app.domain.identity.models import User, UserRole

router = APIRouter(prefix="/events", tags=["events"])


class EventOut(BaseModel):
    id: UUID
    name: str
    valid_from: date
    valid_until: date


class EventCreateRequest(BaseModel):
    name: str
    valid_from: date
    valid_until: date

    @model_validator(mode="after")
    def _valid_from_before_valid_until(self) -> "EventCreateRequest":
        # BUG FIX (minor): an inverted window (valid_from > valid_until) would silently mean
        # "instantly archived" once fed into the RF25 archival filter in
        # app/api/service_points.py (Event.valid_until >= date.today()) — reject it up front.
        if self.valid_from > self.valid_until:
            raise ValueError("valid_from must not be after valid_until.")
        return self


class EventUpdateRequest(BaseModel):
    name: str
    valid_from: date
    valid_until: date

    @model_validator(mode="after")
    def _valid_from_before_valid_until(self) -> "EventUpdateRequest":
        if self.valid_from > self.valid_until:
            raise ValueError("valid_from must not be after valid_until.")
        return self


def _to_out(event: Event) -> EventOut:
    return EventOut(
        id=event.id, name=event.name, valid_from=event.valid_from, valid_until=event.valid_until
    )


@router.get("", response_model=list[EventOut])
async def list_events(
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[EventOut]:
    events = (await db.scalars(select(Event))).all()
    return [_to_out(event) for event in events]


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EventOut:
    event = Event(name=body.name, valid_from=body.valid_from, valid_until=body.valid_until)
    db.add(event)
    await db.flush()
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="event",
        entity_id=event.id,
        action="create",
        before=None,
        after={
            "name": event.name,
            "valid_from": event.valid_from.isoformat(),
            "valid_until": event.valid_until.isoformat(),
        },
    )
    await db.commit()
    return _to_out(event)


@router.patch("/{event_id}", response_model=EventOut)
async def update_event(
    event_id: UUID,
    body: EventUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EventOut:
    event = await db.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Event "{event_id}" not found.')

    before = {
        "name": event.name,
        "valid_from": event.valid_from.isoformat(),
        "valid_until": event.valid_until.isoformat(),
    }
    event.name = body.name
    event.valid_from = body.valid_from
    event.valid_until = body.valid_until
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="event",
        entity_id=event.id,
        action="update",
        before=before,
        after={
            "name": event.name,
            "valid_from": event.valid_from.isoformat(),
            "valid_until": event.valid_until.isoformat(),
        },
    )
    await db.commit()
    return _to_out(event)
