"""qr_codes (spec §4.1). See "Decisões de escopo desta etapa" item 4: public_code stores the
full printed/scanned payload; secret stores the raw signature bytes in isolation, useful for
audit and for Etapa 3 to reprint without re-signing."""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class QrCodeStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    REPLACED = "REPLACED"


class QrCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "qr_codes"

    floor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("floors.id"))
    public_code: Mapped[str] = mapped_column(String(500), unique=True)
    secret: Mapped[bytes] = mapped_column(LargeBinary)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[QrCodeStatus] = mapped_column(
        Enum(QrCodeStatus, name="qr_code_status"), default=QrCodeStatus.ACTIVE
    )
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qr_codes.id"), nullable=True
    )
    revocation_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
