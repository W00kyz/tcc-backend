"""RF33 — photo/note evidence attached to an execution, listed by managers or the owner,
and served through an authenticated content proxy (never a public MinIO URL)."""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import cast

from app.core.config import Settings
from app.domain.execution.models import EvidenceItem, EvidenceKind
from app.domain.identity.models import UserRole
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import (
    _add_field_worker,
    _add_user,
    _login,
    _post,
    _seed_route,
)
from tests.support.object_store import FakeObjectStore

_PHOTO_BYTES = b"abc"
_PHOTO_SHA = hashlib.sha256(_PHOTO_BYTES).hexdigest()


def _store(client: TestClient) -> FakeObjectStore:
    """The FakeObjectStore the `client` fixture wired into this app."""
    return cast(FakeObjectStore, client.app.state.object_store)  # type: ignore[attr-defined]


async def _seed_execution(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> tuple[str, str]:
    """A started route + one check-in; returns (worker token, execution id)."""
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    checkin = _post(client, token, seeded.qr_code.public_code)
    assert checkin.status_code == 201, checkin.text
    return token, checkin.json()["execution_id"]


def _upload_photo(
    client: TestClient,
    token: str,
    execution_id: str,
    data: bytes,
    sha256: str,
    *,
    content_type: str = "image/jpeg",
) -> Response:
    return client.post(
        f"/executions/{execution_id}/evidence/photo",
        files={"file": ("photo.jpg", data, content_type)},
        data={"sha256": sha256, "captured_at": datetime.now(UTC).isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )


def _upload_note(client: TestClient, token: str, execution_id: str, text_body: str) -> Response:
    return client.post(
        f"/executions/{execution_id}/evidence/note",
        json={"text_body": text_body, "captured_at": datetime.now(UTC).isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_upload_photo_stores_object_and_row(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "PHOTO"
    assert body["byte_size"] == 3
    assert body["content_url"] == f"/evidence/{body['id']}/content"

    store = _store(client)
    assert len(store.objects) == 1
    key = next(iter(store.objects))
    assert key.startswith(f"evidence/{execution_id}/")
    assert key.endswith(".jpg")
    stored_bytes, stored_type = store.objects[key]
    assert stored_bytes == _PHOTO_BYTES
    assert stored_type == "image/jpeg"

    row = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.execution_id == uuid.UUID(execution_id))
    )
    assert row is not None
    assert row.kind is EvidenceKind.PHOTO
    assert row.byte_size == 3
    assert row.sha256 == _PHOTO_SHA
    assert row.object_key == key


async def test_upload_photo_sha_mismatch_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_photo(client, token, execution_id, _PHOTO_BYTES, "00" * 32)

    assert response.status_code == 422, response.text
    assert _store(client).objects == {}
    row = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.execution_id == uuid.UUID(execution_id))
    )
    assert row is None


async def test_upload_photo_not_owner_403(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    _, execution_id = await _seed_execution(client, db_session, test_settings)
    await _add_field_worker(db_session, "maria@empresa.com")
    other_token = _login(client, email="maria@empresa.com")

    response = _upload_photo(client, other_token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)

    assert response.status_code == 403, response.text
    assert _store(client).objects == {}


async def test_upload_photo_too_large_413(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    oversized = b"x" * (5 * 1024 * 1024 + 1)

    response = _upload_photo(
        client, token, execution_id, oversized, hashlib.sha256(oversized).hexdigest()
    )

    assert response.status_code == 413, response.text
    assert _store(client).objects == {}


async def test_upload_photo_non_image_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_photo(
        client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA, content_type="application/pdf"
    )

    assert response.status_code == 422, response.text
    assert _store(client).objects == {}


async def test_upload_note_201(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_note(client, token, execution_id, "Piso molhado no corredor.")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "NOTE"
    assert body["text_body"] == "Piso molhado no corredor."
    assert body["content_url"] is None
    assert body["byte_size"] is None


async def test_upload_note_empty_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_note(client, token, execution_id, "   ")

    assert response.status_code == 422, response.text
    row = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.execution_id == uuid.UUID(execution_id))
    )
    assert row is None


async def test_list_evidence_manager_sees_all(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    assert _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA).status_code == 201
    assert _upload_note(client, token, execution_id, "Observação final.").status_code == 201
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    manager_token = _login(client, email="gerente@empresa.com")

    response = client.get(
        f"/executions/{execution_id}/evidence",
        headers={"Authorization": f"Bearer {manager_token}"},
    )

    assert response.status_code == 200, response.text
    kinds = sorted(item["kind"] for item in response.json())
    assert kinds == ["NOTE", "PHOTO"]


async def test_get_content_streams_bytes_with_content_type(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    upload = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)
    evidence_id = upload.json()["id"]
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)
    manager_token = _login(client, email="gerente@empresa.com")

    response = client.get(
        f"/evidence/{evidence_id}/content",
        headers={"Authorization": f"Bearer {manager_token}"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == _PHOTO_BYTES


async def test_get_content_not_found_404(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    note = _upload_note(client, token, execution_id, "Sem foto.")
    note_id = note.json()["id"]

    missing = client.get(
        f"/evidence/{uuid.uuid4()}/content", headers={"Authorization": f"Bearer {token}"}
    )
    assert missing.status_code == 404, missing.text

    note_content = client.get(
        f"/evidence/{note_id}/content", headers={"Authorization": f"Bearer {token}"}
    )
    assert note_content.status_code == 404, note_content.text


async def test_field_worker_can_get_own_content_but_not_others(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    evidence_id = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA).json()["id"]
    await _add_field_worker(db_session, "maria@empresa.com")
    other_token = _login(client, email="maria@empresa.com")

    owner = client.get(
        f"/evidence/{evidence_id}/content", headers={"Authorization": f"Bearer {token}"}
    )
    assert owner.status_code == 200, owner.text

    intruder = client.get(
        f"/evidence/{evidence_id}/content", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert intruder.status_code == 403, intruder.text
