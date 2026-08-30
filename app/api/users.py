"""User CRUD for admins (RF05). No hard-delete endpoint — deactivate is the only way to
remove a user from active use, so audit history and past route assignments stay intact."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.identity.models import User, UserRole
from app.domain.identity.service import create_invited_user

router = APIRouter(prefix="/users", tags=["users"])


class UserOut(BaseModel):
    id: UUID
    name: str
    email: str
    role: UserRole
    is_active: bool


class UserCreateRequest(BaseModel):
    name: str
    email: EmailStr
    role: UserRole


class UserUpdateRequest(BaseModel):
    name: str
    email: EmailStr
    role: UserRole


def _to_user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, name=user.name, email=user.email, role=user.role, is_active=user.is_active
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    _actor: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[UserOut]:
    users = (await db.scalars(select(User))).all()
    return [_to_user_out(user) for user in users]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    request: Request,
    actor: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    settings = request.app.state.settings
    user = await create_invited_user(
        db,
        request.app.state.mailer,
        name=body.name,
        email=body.email,
        role=body.role,
        dashboard_base_url=settings.dashboard_base_url,
        jwt_secret_key=settings.jwt_secret_key,
    )
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="user",
        entity_id=user.id,
        action="create",
        before=None,
        after={"name": user.name, "email": user.email, "role": user.role.value},
    )
    await db.commit()
    return _to_user_out(user)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: UUID,
    body: UserUpdateRequest,
    actor: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'User "{user_id}" not found.')

    before = {"name": user.name, "email": user.email, "role": user.role.value}
    user.name = body.name
    user.email = body.email
    user.role = body.role
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="user",
        entity_id=user.id,
        action="update",
        before=before,
        after={"name": user.name, "email": user.email, "role": user.role.value},
    )
    await db.commit()
    return _to_user_out(user)


async def _set_active(user_id: UUID, is_active: bool, actor: User, db: AsyncSession) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'User "{user_id}" not found.')

    before = {"is_active": user.is_active}
    user.is_active = is_active
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="user",
        entity_id=user.id,
        action="activate" if is_active else "deactivate",
        before=before,
        after={"is_active": user.is_active},
    )
    await db.commit()
    return _to_user_out(user)


@router.post("/{user_id}/activate", response_model=UserOut)
async def activate_user(
    user_id: UUID,
    actor: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    return await _set_active(user_id, True, actor, db)


@router.post("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: UUID,
    actor: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    return await _set_active(user_id, False, actor, db)
