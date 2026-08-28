"""Login/logout business logic, kept out of the router so it's testable without an HTTP
client and reusable — the mobile and dashboard clients hit the same router either way, but a
future integration (RNF17) could call this directly."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.domain.identity.models import AuthLog, AuthLogEvent, User


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
