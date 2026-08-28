"""RF01 (login), RF02 (roles, via require_role on every protected route), RF04 (auth_logs)."""

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.jwt import create_access_token, create_refresh_token, decode_token
from app.db.session import get_db
from app.domain.identity.models import User
from app.domain.identity.service import authenticate_user, record_logout

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    settings = request.app.state.settings
    client_ip = request.client.host if request.client else "unknown"
    user = await authenticate_user(
        db, email=body.email, password=body.password, ip_address=client_ip
    )
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid e-mail or password.")

    return TokenResponse(
        access_token=create_access_token(
            user_id=user.id,
            role=user.role,
            secret=settings.jwt_secret_key,
            minutes=settings.jwt_access_token_minutes,
        ),
        refresh_token=create_refresh_token(
            user_id=user.id, secret=settings.jwt_secret_key, days=settings.jwt_refresh_token_days
        ),
    )


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccessTokenResponse:
    settings = request.app.state.settings
    try:
        payload = decode_token(body.refresh_token, settings.jwt_secret_key)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token."
        ) from exc
    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token is not a refresh token.")

    user = await db.get(User, UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive.")

    return AccessTokenResponse(
        access_token=create_access_token(
            user_id=user.id,
            role=user.role,
            secret=settings.jwt_secret_key,
            minutes=settings.jwt_access_token_minutes,
        )
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    client_ip = request.client.host if request.client else "unknown"
    await record_logout(db, user=user, ip_address=client_ip)
