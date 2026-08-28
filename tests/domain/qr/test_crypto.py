import uuid

from app.domain.qr.crypto import decode_qr_payload, derive_public_key_hex, sign_qr_payload

_PRIVATE_KEY_HEX = "11" * 32  # 32 bytes — deterministic test key, never use in production


def test_sign_and_decode_round_trip() -> None:
    floor_id = uuid.uuid4()
    public_key_hex = derive_public_key_hex(_PRIVATE_KEY_HEX)

    payload = sign_qr_payload(floor_id=floor_id, version=1, private_key_hex=_PRIVATE_KEY_HEX)
    decoded = decode_qr_payload(payload, public_key_hex=public_key_hex)

    assert decoded is not None
    assert decoded.floor_id == floor_id
    assert decoded.version == 1


def test_decode_rejects_a_payload_signed_with_a_different_key() -> None:
    floor_id = uuid.uuid4()
    other_key_hex = "22" * 32
    payload = sign_qr_payload(floor_id=floor_id, version=1, private_key_hex=other_key_hex)

    decoded = decode_qr_payload(payload, public_key_hex=derive_public_key_hex(_PRIVATE_KEY_HEX))

    assert decoded is None


def test_decode_rejects_a_tampered_floor_id() -> None:
    # Attack: take a real QR, swap the floor_id, keep the original signature.
    floor_id = uuid.uuid4()
    public_key_hex = derive_public_key_hex(_PRIVATE_KEY_HEX)
    payload = sign_qr_payload(floor_id=floor_id, version=1, private_key_hex=_PRIVATE_KEY_HEX)
    _, _, version, signature = payload.split(".")
    tampered = f"v1.{uuid.uuid4()}.{version}.{signature}"

    assert decode_qr_payload(tampered, public_key_hex=public_key_hex) is None


def test_decode_rejects_malformed_input() -> None:
    public_key_hex = derive_public_key_hex(_PRIVATE_KEY_HEX)

    assert decode_qr_payload("not-a-qr-payload", public_key_hex=public_key_hex) is None
    assert decode_qr_payload("v2.abc.1.sig", public_key_hex=public_key_hex) is None
