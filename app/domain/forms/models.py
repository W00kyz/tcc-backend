"""Dynamic execution forms, versioned and immutable once published (spec §3.1, Etapa 7).

One `Form` per `ServiceType`. Each `Form` keeps exactly one mutable `DRAFT` `FormVersion`
(the builder's working copy) plus zero or more immutable `PUBLISHED` versions. A
`FormQuestion` carries a `stable_key` that follows the question across versions and a
`content_hash` the server fills in at publish time (Ruling 2)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FormVersionStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class QuestionType(enum.StrEnum):
    TEXT = "TEXT"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTI_CHOICE = "MULTI_CHOICE"


class Form(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "forms"

    # unique: one form per service type (spec §3.1). Created on demand when the manager
    # opens the builder for a type that has no form yet.
    service_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_types.id"), unique=True
    )
    name: Mapped[str] = mapped_column(String(200))

    versions: Mapped[list["FormVersion"]] = relationship(
        back_populates="form", order_by="FormVersion.version_number"
    )


class FormVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_versions"

    form_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("forms.id"))
    status: Mapped[FormVersionStatus] = mapped_column(
        Enum(FormVersionStatus, name="form_version_status")
    )
    # 0 on the draft, 1..n on published versions (spec §3.1).
    version_number: Mapped[int] = mapped_column(Integer)
    # timezone=True to match TimestampMixin — the publish path binds datetime.now(UTC).
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    form: Mapped["Form"] = relationship(back_populates="versions")
    questions: Mapped[list["FormQuestion"]] = relationship(
        back_populates="form_version", order_by="FormQuestion.order_index"
    )


class FormQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "form_questions"
    __table_args__ = (
        UniqueConstraint(
            "form_version_id", "stable_key", name="uq_form_questions_version_stable_key"
        ),
    )

    form_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("form_versions.id")
    )
    # Identity of the question across versions — the link answers are reconciled against
    # (Ruling 2). Reordering rewrites every `order_index`, so no UNIQUE on it.
    stable_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    order_index: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(String(500))
    question_type: Mapped[QuestionType] = mapped_column(Enum(QuestionType, name="question_type"))
    required: Mapped[bool] = mapped_column(Boolean)
    # List of choice labels; `[]` when the type is not a choice.
    options: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # NULL on a draft question, filled by the server at publish time (Ruling 2).
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    form_version: Mapped["FormVersion"] = relationship(back_populates="questions")
