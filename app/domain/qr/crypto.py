"""Ed25519 signing/verification for QR payloads (AD-09, spec §5.3).

Payload format: "v1.<floor_id>.<version>.<signature_b64url>". The server holds the private
key and signs (Etapa 3 will call sign_qr_payload from a print/reissue endpoint); the mobile
app embeds only the derived public key, via build config, and verifies fully offline.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

_PAYLOAD_VERSION = "v1"


@dataclass(frozen=True)
class QrPayload:
    floor_id: UUID
    version: int


def _signable_string(floor_id: UUID, version: int) -> str:
    return f"{_PAYLOAD_VERSION}.{floor_id}.{version}"


def sign_qr_payload(*, floor_id: UUID, version: int, private_key_hex: str) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    signable = _signable_string(floor_id, version)
    signature = private_key.sign(signable.encode("utf-8"))
    return f"{signable}.{base64.urlsafe_b64encode(signature).decode('ascii')}"


def decode_qr_payload(payload: str, *, public_key_hex: str) -> QrPayload | None:
    """Verifies the signature and parses the payload. Returns None on any malformed, forged
    or tampered input — callers turn that into a 422, never a 500."""
    parts = payload.split(".")
    if len(parts) != 4 or parts[0] != _PAYLOAD_VERSION:
        return None

    _, raw_floor_id, raw_version, raw_signature = parts
    try:
        floor_id = UUID(raw_floor_id)
        version = int(raw_version)
        signature = base64.urlsafe_b64decode(raw_signature)
    except (ValueError, TypeError):
        return None

    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    signable = _signable_string(floor_id, version)
    try:
        public_key.verify(signature, signable.encode("utf-8"))
    except InvalidSignature:
        return None
    return QrPayload(floor_id=floor_id, version=version)


@lru_cache
def derive_public_key_hex(private_key_hex: str) -> str:
    """Pure function of the private key — cached because the server derives it on every
    check-in request (Task 8) instead of storing the public key separately."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return public_bytes.hex()
