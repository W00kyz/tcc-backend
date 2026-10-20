"""RF01 (login), RF02 (roles, via require_role on every protected route), RF04 (auth_logs)."""

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
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
from app.domain.catalog.models import FieldWorker, FieldWorkerStatus
from app.domain.identity.models import User, UserRole
from app.domain.identity.registration import (
    EmailTaken,
    NoticeOutdated,
    UnknownCompany,
    register_field_worker,
)
from app.domain.identity.service import authenticate_user, record_logout
from app.domain.privacy.service import current_notice, has_consented, record_consent

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class WorkerStanding(BaseModel):
    """Only for field workers (None for everyone else): a pending, rejected or deactivated
    one still logs in, and the app reads here why it cannot work yet. Refresh carries it too,
    so the app learns of an approval without a new login."""

    worker_status: FieldWorkerStatus | None = None
    rejection_reason: str | None = None
    worker_active: bool | None = None


class TokenResponse(WorkerStanding):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    consent_required: bool


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    contractor_company_id: UUID
    privacy_notice_version: int


class RegisterResponse(BaseModel):
    status: FieldWorkerStatus


class NoticeOut(BaseModel):
    version: int
    title: str
    body_markdown: str


class ConsentStatusOut(BaseModel):
    consent_required: bool
    notice: NoticeOut | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(WorkerStanding):
    access_token: str
    token_type: str = "bearer"


async def _worker_standing(db: AsyncSession, user: User) -> WorkerStanding:
    if user.role != UserRole.FIELD_WORKER:
        return WorkerStanding()
    worker = await db.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    if worker is None:
        return WorkerStanding()
    return WorkerStanding(
        worker_status=worker.status,
        rejection_reason=worker.rejection_reason,
        worker_active=worker.is_active,
    )


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

    consent_required = not await has_consented(db, user_id=user.id)
    standing = await _worker_standing(db, user)

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
        consent_required=consent_required,
        **standing.model_dump(),
    )


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RegisterResponse:
    """Public sign-up of a field worker; a manager approves it before it can work."""
    try:
        worker = await register_field_worker(
            db,
            name=body.name,
            email=body.email,
            password=body.password,
            company_id=body.contractor_company_id,
            notice_version=body.privacy_notice_version,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except EmailTaken as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "EMAIL_TAKEN", "detail": "This e-mail is already registered."},
        ) from exc
    except NoticeOutdated as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "NOTICE_OUTDATED",
                "detail": "The privacy notice version is not the current one.",
            },
        ) from exc
    except UnknownCompany as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown contractor company."
        ) from exc
    await db.commit()
    return RegisterResponse(status=worker.status)


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

    standing = await _worker_standing(db, user)
    return AccessTokenResponse(
        access_token=create_access_token(
            user_id=user.id,
            role=user.role,
            secret=settings.jwt_secret_key,
            minutes=settings.jwt_access_token_minutes,
        ),
        **standing.model_dump(),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    client_ip = request.client.host if request.client else "unknown"
    await record_logout(db, user=user, ip_address=client_ip)


@router.post("/consent", response_model=ConsentStatusOut)
async def accept_consent(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConsentStatusOut:
    await record_consent(
        db,
        user_id=user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return ConsentStatusOut(consent_required=False)


@router.get("/consent-status", response_model=ConsentStatusOut)
async def consent_status(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConsentStatusOut:
    notice = await current_notice(db)
    required = not await has_consented(db, user_id=user.id)
    return ConsentStatusOut(
        consent_required=required,
        notice=NoticeOut(
            version=notice.version, title=notice.title, body_markdown=notice.body_markdown
        ),
    )


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    # Same floor the dashboard's reset form enforces; also the invite path (RF05), since a new
    # user sets the first password through this endpoint.
    new_password: str = Field(min_length=8, max_length=128)


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
