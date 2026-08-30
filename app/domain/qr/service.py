import base64
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode, QrCodeStatus


def _extract_signature_bytes(signed_payload: str) -> bytes:
    """`sign_qr_payload` returns "v1.<floor_id>.<version>.<signature_b64url>" — the last
    dot-segment is the signature, base64url-encoded. This mirrors exactly how
    `decode_qr_payload` (same module) parses a payload back apart, so `secret` ends up holding
    the same bytes whichever direction the payload is read from.

    IMPORTANT: `secret` must never hold the private signing key — see the module docstring on
    `app/domain/qr/models.py` ("the raw signature bytes in isolation"). Storing the key instead
    would put the server's Ed25519 private key in a queryable DB column, readable by anyone
    with DB/backup access or a SQL injection, compromising every QR code's signature.
    """
    signature_b64url = signed_payload.rsplit(".", 1)[-1]
    return base64.urlsafe_b64decode(signature_b64url)


async def issue_qr_code(
    db: AsyncSession, *, floor_id: uuid.UUID, private_key_hex: str, reason: str
) -> QrCode:
    """RF09/RF10. A floor has at most one ACTIVE QrCode at any time — spec §4.2 item 5 ("o QR
    pertence ao andar"). Calling this again is what RF10 "substituição" means: the previous
    ACTIVE code, if any, becomes REPLACED with `reason` recorded as why it left circulation."""
    previous = await db.scalar(
        select(QrCode).where(QrCode.floor_id == floor_id, QrCode.status == QrCodeStatus.ACTIVE)
    )
    next_version = (previous.version + 1) if previous else 1

    signed = sign_qr_payload(
        floor_id=floor_id, version=next_version, private_key_hex=private_key_hex
    )
    new_code = QrCode(
        floor_id=floor_id,
        public_code=signed,
        secret=_extract_signature_bytes(signed),
        version=next_version,
        status=QrCodeStatus.ACTIVE,
    )
    db.add(new_code)
    await db.flush()

    if previous is not None:
        previous.status = QrCodeStatus.REPLACED
        previous.replaced_by_id = new_code.id
        previous.revocation_reason = reason
        await db.flush()

    return new_code


async def revoke_qr_code(db: AsyncSession, *, qr_code: QrCode, reason: str) -> QrCode:
    """RF10 "invalidação" pura — no replacement issued, e.g. the floor is temporarily out of
    service for renovation. Check-in against this floor fails until a new code is issued."""
    qr_code.status = QrCodeStatus.REVOKED
    qr_code.revocation_reason = reason
    await db.flush()
    return qr_code
