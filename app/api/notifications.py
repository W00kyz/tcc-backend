"""Etapa 9 — the notification centre (RF51). Every authenticated user reads their own
notifications here; the panel shows the manager's, the app polls the field worker's.

`list_user_notifications`/`mark_read`/... flush; this router owns `db.commit()`. A single-row
mark-read is refused with 403 unless the row belongs to the caller.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Notification
from app.domain.monitoring.notification_service import (
    list_user_notifications,
    mark_all_read,
    mark_read,
    unread_count,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_AnyUser = Annotated[
    User, Depends(require_role(UserRole.FIELD_WORKER, UserRole.MANAGER, UserRole.ADMIN))
]
_Db = Annotated[AsyncSession, Depends(get_db)]

_MAX_PAGE_SIZE = 100


class NotificationOut(BaseModel):
    id: UUID
    kind: str
    title: str
    body: str
    data: dict[str, object]
    read_at: datetime | None
    created_at: datetime


class PagedNotifications(BaseModel):
    items: list[NotificationOut]
    total: int
    page: int
    page_size: int


class UnreadCountOut(BaseModel):
    count: int


class MarkAllReadOut(BaseModel):
    marked: int


def _to_out(notification: Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        kind=notification.kind,
        title=notification.title,
        body=notification.body,
        data=notification.data,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


@router.get("/me", response_model=PagedNotifications)
async def list_my_notifications(
    actor: _AnyUser,
    db: _Db,
    unread_only: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> PagedNotifications:
    page = max(page, 1)
    page_size = min(max(page_size, 1), _MAX_PAGE_SIZE)
    rows, total = await list_user_notifications(
        db, user_id=actor.id, unread_only=unread_only, page=page, page_size=page_size
    )
    return PagedNotifications(
        items=[_to_out(row) for row in rows], total=total, page=page, page_size=page_size
    )


@router.get("/me/unread-count", response_model=UnreadCountOut)
async def my_unread_count(actor: _AnyUser, db: _Db) -> UnreadCountOut:
    return UnreadCountOut(count=await unread_count(db, user_id=actor.id))


@router.post("/me/read-all", response_model=MarkAllReadOut)
async def mark_all_my_notifications_read(actor: _AnyUser, db: _Db) -> MarkAllReadOut:
    marked = await mark_all_read(db, user_id=actor.id)
    await db.commit()
    return MarkAllReadOut(marked=marked)


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: UUID, actor: _AnyUser, db: _Db
) -> NotificationOut:
    """Mark one notification read. 404 when it does not exist, 403 when it is not the
    caller's — a user can only touch their own centre."""
    notification = await db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f'Notification "{notification_id}" not found.'
        )
    if notification.user_id != actor.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This notification belongs to another user.")
    notification = await mark_read(db, notification=notification)
    await db.commit()
    await db.refresh(notification)
    return _to_out(notification)
