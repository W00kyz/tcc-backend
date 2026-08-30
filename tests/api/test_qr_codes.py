import base64
import uuid

from app.core.security import hash_password
from app.domain.catalog.models import Building, Floor
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import derive_public_key_hex
from app.domain.qr.models import QrCode, QrCodeStatus
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PRIVATE_KEY_HEX = "11" * 32  # matches conftest's test_settings.qr_signing_private_key_hex


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def _seed_floor(db_session: AsyncSession) -> tuple[User, Floor]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, building])
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.commit()
    return manager, floor


async def test_first_generation_creates_an_active_version_1_qr_code(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Emissão inicial do andar"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["status"] == "ACTIVE"
    assert body["public_code"].startswith("v1.")


async def test_regenerating_replaces_the_previous_active_code(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)
    first = client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Emissão inicial"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    second = client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Etiqueta danificada"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert second.status_code == 201
    assert second.json()["version"] == 2
    old = await db_session.get(QrCode, first["id"])
    assert old is not None
    await db_session.refresh(old)
    assert old.status == QrCodeStatus.REPLACED
    assert str(old.replaced_by_id) == second.json()["id"]
    assert old.revocation_reason == "Etiqueta danificada"


async def test_revoking_without_replacement_leaves_the_floor_without_an_active_code(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)
    created = client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Emissão inicial"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    response = client.post(
        f"/qr-codes/{created['id']}/revoke",
        json={"reason": "Reforma do andar"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "REVOKED"
    active = await db_session.scalar(
        select(QrCode).where(QrCode.floor_id == floor.id, QrCode.status == QrCodeStatus.ACTIVE)
    )
    assert active is None


async def test_reissuing_after_a_pure_revoke_succeeds_with_a_fresh_version(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test for Finding C1: issue -> revoke (no replacement) -> issue again used to
    reset next_version to 1 because it was derived only from the current ACTIVE row (none, post
    revoke) — sign_qr_payload is deterministic, so that reproduced the already-revoked v1 row's
    exact public_code and hit its unique constraint (500), permanently bricking reissue for the
    floor. This is exactly the "reforma" (renovation) flow: revoke while out of service, then
    issue a fresh code once work is done."""
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)
    first = client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Emissão inicial"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    revoke = client.post(
        f"/qr-codes/{first['id']}/revoke",
        json={"reason": "Reforma do andar"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert revoke.status_code == 200

    response = client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Reforma concluída"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 2
    assert body["status"] == "ACTIVE"
    assert body["public_code"] != first["public_code"]


async def test_issuing_for_an_unknown_floor_returns_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.post(
        f"/floors/{uuid.uuid4()}/qr-codes",
        json={"reason": "Emissão inicial"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


async def test_revoking_an_unknown_qr_code_returns_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.post(
        f"/qr-codes/{uuid.uuid4()}/revoke",
        json={"reason": "Reforma do andar"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


async def test_issued_secret_is_the_signature_not_the_private_signing_key(
    client: TestClient, db_session: AsyncSession
) -> None:
    """Regression test: the brief's own sample `issue_qr_code` did
    `secret=bytes.fromhex(private_key_hex)`, which would store the server's private Ed25519
    signing key in a queryable database column — any DB read, backup leak, or SQL injection
    would then expose the key used to sign every QR code in the system. `secret` must hold the
    signature bytes carried inside `public_code` instead, exactly like `decode_qr_payload`
    extracts them when parsing a payload back apart."""
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Emissão inicial do andar"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = response.json()

    stored = await db_session.get(QrCode, body["id"])
    assert stored is not None

    private_key_bytes = bytes.fromhex(_PRIVATE_KEY_HEX)
    assert stored.secret != private_key_bytes

    # An Ed25519 signature is always 64 bytes, and must match the signature encoded in the
    # tail of the printed/scanned payload.
    assert len(stored.secret) == 64
    expected_signature = base64.urlsafe_b64decode(body["public_code"].split(".")[-1])
    assert stored.secret == expected_signature

    # And it must actually verify against the server's public key over the signed string.
    public_key_hex = derive_public_key_hex(_PRIVATE_KEY_HEX)
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    signable_string = ".".join(body["public_code"].split(".")[:-1])
    try:
        public_key.verify(stored.secret, signable_string.encode("utf-8"))
    except InvalidSignature:
        raise AssertionError("stored secret does not verify as the payload's signature") from None


async def test_downloading_the_pdf_for_a_floor_without_an_active_code_is_a_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)

    response = client.get(
        f"/floors/{floor.id}/qr-codes/active/pdf", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 404


async def test_downloading_the_pdf_for_an_active_code(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, floor = await _seed_floor(db_session)
    token = _login(client, manager.email)
    client.post(
        f"/floors/{floor.id}/qr-codes",
        json={"reason": "Emissão inicial"},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.get(
        f"/floors/{floor.id}/qr-codes/active/pdf", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
