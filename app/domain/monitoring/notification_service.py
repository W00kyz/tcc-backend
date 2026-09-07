"""Etapa 9 — the read/mark side of the notification centre (RF51). `notifications.py` writes
the canonical rows and fans out to the push seam; this module is what the panel's notification
centre and the app's polling list read back.

Transaction discipline: flush, never commit — the endpoint on `app/api/notifications.py` owns
the boundary. The per-user ownership check for a single-row mark-read lives at the API layer.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.monitoring.models import Notification


async def list_user_notifications(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    unread_only: bool,
    page: int,
    page_size: int,
) -> tuple[Sequence[Notification], int]:
    """One page of a user's notifications, newest first, plus the total row count for that
    same filter — the `page`/`page_size` pattern used across the repo."""
    condition = Notification.user_id == user_id
    if unread_only:
        condition = condition & Notification.read_at.is_(None)

    rows = (
        await db.scalars(
            select(Notification)
            .where(condition)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    total = int(
        await db.scalar(select(func.count()).select_from(Notification).where(condition)) or 0
    )
    return rows, total


async def unread_count(db: AsyncSession, *, user_id: uuid.UUID) -> int:
    """The badge number — how many of this user's notifications are still unread (RF51)."""
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


async def mark_read(db: AsyncSession, *, notification: Notification) -> Notification:
    """Stamp `read_at` if it is not set yet. Idempotent — a second call is a no-op."""
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await db.flush()
    return notification


async def mark_all_read(db: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Bulk-stamp every unread notification for this user; return how many rows moved."""
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await db.flush()
    return int(cast("CursorResult[Any]", result).rowcount or 0)
