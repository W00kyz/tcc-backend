"""QR code issuance, substituição and revocation (RF09, RF10). No hard-delete endpoint —
replaced/revoked codes stay in the table as history; `status` tracks the lifecycle."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.catalog.models import Floor
from app.domain.identity.models import User, UserRole
from app.domain.qr.models import QrCode, QrCodeStatus
from app.domain.qr.pdf import generate_qr_sheet_pdf
from app.domain.qr.service import issue_qr_code, revoke_qr_code

router = APIRouter(tags=["qr-codes"])


class QrCodeOut(BaseModel):
    id: UUID
    floor_id: UUID
    public_code: str
    version: int
    status: str


class IssueQrCodeRequest(BaseModel):
    reason: str


class RevokeQrCodeRequest(BaseModel):
    reason: str


def _to_out(qr_code: QrCode) -> QrCodeOut:
    return QrCodeOut(
        id=qr_code.id,
        floor_id=qr_code.floor_id,
        public_code=qr_code.public_code,
        version=qr_code.version,
        status=qr_code.status.value,
    )


@router.post(
    "/floors/{floor_id}/qr-codes", response_model=QrCodeOut, status_code=status.HTTP_201_CREATED
)
async def issue_qr_code_endpoint(
    floor_id: UUID,
    body: IssueQrCodeRequest,
    request: Request,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QrCodeOut:
    # Without this check, an unknown floor_id would only fail later at the QrCode insert's FK
    # constraint, surfacing as an unhandled IntegrityError (500) instead of a clean 404 — the
    # same existence-check-before-write pattern as update_floor in app/api/floors.py.
    floor = await db.get(Floor, floor_id)
    if floor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Floor "{floor_id}" not found.')

    settings = request.app.state.settings
    qr_code = await issue_qr_code(
        db,
        floor_id=floor_id,
        private_key_hex=settings.qr_signing_private_key_hex,
        reason=body.reason,
    )
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="qr_code",
        entity_id=qr_code.id,
        action="issue",
        before=None,
        after={"floor_id": str(floor_id), "version": qr_code.version, "reason": body.reason},
    )
    await db.commit()
    return _to_out(qr_code)


@router.get("/floors/{floor_id}/qr-codes/active/pdf")
async def download_active_qr_code_pdf(
    floor_id: UUID,
    _actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    floor = await db.scalar(
        select(Floor).where(Floor.id == floor_id).options(selectinload(Floor.building))
    )
    if floor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Floor "{floor_id}" not found.')

    qr_code = await db.scalar(
        select(QrCode).where(QrCode.floor_id == floor_id, QrCode.status == QrCodeStatus.ACTIVE)
    )
    if qr_code is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Floor "{floor_id}" has no active QR code.')

    pdf_bytes = generate_qr_sheet_pdf(
        building_name=floor.building.name, floor_label=floor.label, qr_code=qr_code
    )
    return Response(content=pdf_bytes, media_type="application/pdf")


@router.post("/qr-codes/{qr_code_id}/revoke", response_model=QrCodeOut)
async def revoke_qr_code_endpoint(
    qr_code_id: UUID,
    body: RevokeQrCodeRequest,
    actor: Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QrCodeOut:
    qr_code = await db.get(QrCode, qr_code_id)
    if qr_code is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'QR code "{qr_code_id}" not found.')

    before = {"status": qr_code.status.value}
    qr_code = await revoke_qr_code(db, qr_code=qr_code, reason=body.reason)
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="qr_code",
        entity_id=qr_code.id,
        action="revoke",
        before=before,
        after={"status": qr_code.status.value, "reason": body.reason},
    )
    await db.commit()
    return _to_out(qr_code)
