"""Shared FastAPI dependencies: who is calling, and are they allowed here."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.campus_clock import campus_today
from app.core.jwt import decode_token
from app.db.session import get_db
from app.domain.identity.models import User, UserRole

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


def get_campus_today() -> date:
    """Today on campus, the day a route belongs to. A dependency so tests can pin the clock.

    `date.today()` in the API container is the UTC date, already tomorrow from 21:00 in
    Campina Grande: the worker's "rota do dia" vanished and check-ins failed with "No active
    route for today" every night.

    Example: ``async def endpoint(today: CampusToday) -> ...``
    """
    return campus_today(datetime.now(UTC))


CampusToday = Annotated[date, Depends(get_campus_today)]
