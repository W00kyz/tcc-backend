"""Identity and access (RF01, RF02, RF04). password_hash is Argon2 output from
app.core.security — this module never sees a plain-text password."""

import enum
import uuid

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserRole(enum.StrEnum):
    FIELD_WORKER = "FIELD_WORKER"
    MANAGER = "MANAGER"
    ADMIN = "ADMIN"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthLogEvent(enum.StrEnum):
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    LOGOUT = "LOGOUT"


class AuthLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "auth_logs"

    # Nullable by design: a failed login attempt with non-existent email has no user_id.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attempted_email: Mapped[str] = mapped_column(String(255))
    event: Mapped[AuthLogEvent] = mapped_column(Enum(AuthLogEvent, name="auth_log_event"))
    ip_address: Mapped[str] = mapped_column(INET)
