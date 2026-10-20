"""Shared FastAPI dependencies: who is calling, and are they allowed here."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.campus_clock import campus_today
from app.core.jwt import decode_token
from app.db.session import get_db
from app.domain.catalog.models import FieldWorker, FieldWorkerStatus
from app.domain.identity.models import User, UserRole
from app.domain.privacy.service import has_consented

_bearer_scheme = HTTPBearer(auto_error=True)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    settings = request.app.state.settings
    try:
        payload = decode_token(credentials.credentials, settings.jwt_secret_key)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token.") from exc
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token is not an access token.")

    user = await db.get(User, UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive.")
    return user


def require_role(*roles: UserRole) -> Callable[..., Any]:
    async def dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            allowed = ", ".join(role.value for role in roles)
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f'Role "{user.role.value}" is not permitted here; expected one of [{allowed}].',
            )
        return user

    return dependency


async def get_approved_worker(
    user: Annotated[User, Depends(require_role(UserRole.FIELD_WORKER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FieldWorker:
    """The caller's field worker profile, only while a manager has approved it and not switched
    it off — the gate in front of every endpoint that acts as a worker in the field.

    Example: ``async def endpoint(worker: ApprovedWorker) -> ...``
    """
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None or worker.status != FieldWorkerStatus.APPROVED or not worker.is_active:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "WORKER_NOT_APPROVED",
                "detail": "This field worker account is not approved or is deactivated.",
            },
        )
    return worker


ApprovedWorker = Annotated[FieldWorker, Depends(get_approved_worker)]


async def ensure_location_consent(db: AsyncSession, worker: FieldWorker) -> None:
    """403 until the worker has accepted the current privacy notice: no location data is
    accepted before that (RNF10).

    Example: ``await ensure_location_consent(db, worker)``
    """
    if not await has_consented(db, user_id=worker.user_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Consent to the current privacy notice is required before location data can be "
            "collected.",
        )


def get_now() -> datetime:
    """The current instant (aware, UTC). A dependency so tests can pin the clock; campus rules
    read it through `app.core.campus_clock`.

    Example: ``async def endpoint(now: Now) -> ...``
    """
    return datetime.now(UTC)


Now = Annotated[datetime, Depends(get_now)]


def get_campus_today(now: Now) -> date:
    """Today on campus, the day a route belongs to. A dependency so tests can pin the clock.

    `date.today()` in the API container is the UTC date, already tomorrow from 21:00 in
    Campina Grande: the worker's "rota do dia" vanished and check-ins failed with "No active
    route for today" every night.

    Example: ``async def endpoint(today: CampusToday) -> ...``
    """
    return campus_today(now)


CampusToday = Annotated[date, Depends(get_campus_today)]
