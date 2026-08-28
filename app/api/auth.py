"""RF01 (login), RF02 (roles, via require_role on every protected route), RF04 (auth_logs)."""

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.jwt import (
    create_access_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
)
from app.core.security import hash_password
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


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    settings = request.app.state.settings
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is None:
        # Same response with or without a user: never leak which e-mails are registered.
        return

    token = create_password_reset_token(user_id=user.id, secret=settings.jwt_secret_key)
    reset_link = f"{settings.dashboard_base_url}/redefinir-senha?token={token}"
    await request.app.state.mailer.send(
        to=user.email,
        subject="Redefinição de senha — Monitoramento de Rotas PU/UFCG",
        body=f"Use o link a seguir para redefinir sua senha (válido por 30 minutos):\n{reset_link}",
    )


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    body: PasswordResetConfirm,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    settings = request.app.state.settings
    try:
        payload = decode_token(body.token, settings.jwt_secret_key)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired reset token."
        ) from exc
    if payload.get("type") != "password_reset":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token is not a password-reset token.")

    user = await db.get(User, UUID(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found.")

    user.password_hash = hash_password(body.new_password)
    await db.commit()
