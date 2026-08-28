"""JWT issuing and verification. `type` is embedded in every token on purpose — it stops a
refresh token from being replayed as an access token at a route boundary, and vice versa."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.domain.identity.models import UserRole

_ALGORITHM = "HS256"

TokenType = Literal["access", "refresh", "password_reset"]


def _encode(claims: dict[str, Any], secret: str, expires_delta: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {**claims, "iat": now, "exp": now + expires_delta}
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def create_access_token(*, user_id: uuid.UUID, role: UserRole, secret: str, minutes: int) -> str:
    return _encode(
        {"sub": str(user_id), "role": role.value, "type": "access"},
        secret,
        timedelta(minutes=minutes),
    )


def create_refresh_token(*, user_id: uuid.UUID, secret: str, days: int) -> str:
    return _encode({"sub": str(user_id), "type": "refresh"}, secret, timedelta(days=days))


def create_password_reset_token(*, user_id: uuid.UUID, secret: str, minutes: int = 30) -> str:
    return _encode(
        {"sub": str(user_id), "type": "password_reset"}, secret, timedelta(minutes=minutes)
    )


def decode_token(token: str, secret: str) -> dict[str, Any]:
    """Raises jwt.InvalidTokenError (or a subclass) on any bad, expired or forged token."""
    return jwt.decode(token, secret, algorithms=[_ALGORITHM])
