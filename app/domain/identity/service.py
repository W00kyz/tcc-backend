"""Login/logout business logic, kept out of the router so it's testable without an HTTP
client and reusable — the mobile and dashboard clients hit the same router either way, but a
future integration (RNF17) could call this directly."""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jwt import create_password_reset_token
from app.core.mail import Mailer
from app.core.security import hash_password, verify_password
from app.domain.identity.models import AuthLog, AuthLogEvent, User, UserRole


async def authenticate_user(
    db: AsyncSession, *, email: str, password: str, ip_address: str
) -> User | None:
    """Returns the user on success; on failure, records the auth_log entry itself and returns
    None — the caller only needs to decide the HTTP response."""
    user = await db.scalar(select(User).where(User.email == email))

    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        db.add(
            AuthLog(
                user_id=user.id if user else None,
                attempted_email=email,
                event=AuthLogEvent.LOGIN_FAILURE,
                ip_address=ip_address,
            )
        )
        await db.commit()
        return None

    db.add(
        AuthLog(
            user_id=user.id,
            attempted_email=email,
            event=AuthLogEvent.LOGIN_SUCCESS,
            ip_address=ip_address,
        )
    )
    await db.commit()
    return user


async def record_logout(db: AsyncSession, *, user: User, ip_address: str) -> None:
    db.add(
        AuthLog(
            user_id=user.id,
            attempted_email=user.email,
            event=AuthLogEvent.LOGOUT,
            ip_address=ip_address,
        )
    )
    await db.commit()


async def create_invited_user(
    db: AsyncSession,
    mailer: Mailer,
    *,
    name: str,
    email: str,
    role: UserRole,
    dashboard_base_url: str,
    jwt_secret_key: str,
) -> User:
    """Admin-created users never receive a plaintext password (RF05, spec Ruling 5) — the
    password_hash is a random, unusable Argon2 hash, and the account is only usable after the
    invitee follows the same reset-password link the "forgot password" flow already sends."""
    unusable_password = secrets.token_urlsafe(32)
    user = User(
        name=name,
        email=email,
        password_hash=hash_password(unusable_password),
        role=role,
    )
    db.add(user)
    await db.flush()

    # 1440 minutes (24h), not the 30-minute default: an invitee needs more time to check
    # their e-mail and set a password than someone actively resetting a forgotten one now
    # (spec Ruling 5).
    token = create_password_reset_token(user_id=user.id, secret=jwt_secret_key, minutes=1440)
    reset_link = f"{dashboard_base_url}/redefinir-senha?token={token}"
    await mailer.send(
        to=user.email,
        subject="Bem-vindo(a) — Monitoramento de Rotas PU/UFCG",
        body=(
            "Uma conta foi criada para você no sistema de monitoramento de rotas da PU. "
            f"Use o link a seguir para definir sua senha (válido por 24 horas):\n{reset_link}"
        ),
    )
    return user
