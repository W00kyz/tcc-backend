"""RF33 — photo/note evidence attached to an execution, listed by managers or the owner,
and served through an authenticated content proxy (never a public MinIO URL)."""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import cast

from app.core.config import Settings
from app.domain.catalog.models import Building, FieldWorker, Floor, ServicePoint
from app.domain.execution.models import (
    EvidenceItem,
    EvidenceKind,
    Execution,
    ExecutionReviewStatus,
    ExecutionSource,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import ReleaseReason
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import (
    _POINT_LAT,
    _POINT_LNG,
    _add_field_worker,
    _add_user,
    _login,
    _post,
    _release_stop,
    _seed_route,
)
from tests.support.clock import today_on_campus
from tests.support.factories import make_route
from tests.support.object_store import FakeObjectStore

# A minimal but real JPEG header — the upload endpoint checks the SOI magic bytes and
# rejects anything else (e.g. SVG) with 422.
_PHOTO_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x00"
_PHOTO_SHA = hashlib.sha256(_PHOTO_BYTES).hexdigest()
_SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


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
    idempotency_key: str | None = None,
) -> Response:
    return client.post(
        f"/executions/{execution_id}/evidence/photo",
        files={"file": ("photo.jpg", data, content_type)},
        data={
            "sha256": sha256,
            "captured_at": datetime.now(UTC).isoformat(),
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _upload_note(
    client: TestClient,
    token: str,
    execution_id: str,
    text_body: str,
    *,
    idempotency_key: str | None = None,
) -> Response:
    return client.post(
        f"/executions/{execution_id}/evidence/note",
        json={
            "text_body": text_body,
            "captured_at": datetime.now(UTC).isoformat(),
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )


async def _seed_other_worker_execution(
    client: TestClient, db_session: AsyncSession, email: str = "maria@empresa.com"
) -> tuple[str, str, str]:
    """A *second* field worker with her own started route + check-in execution + one photo.
    Returns (her token, her execution id, her evidence id). Used to prove one worker cannot
    read another's evidence even though both are FIELD_WORKERs."""
    await _add_field_worker(db_session, email)
    worker = await db_session.scalar(
        select(FieldWorker).join(User, FieldWorker.user_id == User.id).where(User.email == email)
    )
    assert worker is not None

    building = Building(name=f"Bloco {email}", campus_area="CCT")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()
    point = ServicePoint(
        floor_id=floor.id, name="Sala X", description="Sala", latitude=-7.1, longitude=-35.8
    )
    db_session.add(point)
    await db_session.flush()
    route = await make_route(db_session, worker=worker, day=today_on_campus(), points=[point])
    stop = route.stops[0]
    execution = Execution(
        route_stop_id=stop.id,
        field_worker_id=worker.id,
        checked_in_at=datetime.now(UTC),
        synced_at=datetime.now(UTC),
        source=ExecutionSource.APP,
        idempotency_key=uuid.uuid4(),
    )
    db_session.add(execution)
    await db_session.commit()

    token = _login(client, email=email)
    upload = _upload_photo(client, token, str(execution.id), _PHOTO_BYTES, _PHOTO_SHA)
    assert upload.status_code == 201, upload.text
    return token, str(execution.id), upload.json()["id"]


async def test_upload_photo_stores_object_and_row(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "PHOTO"
    assert body["byte_size"] == len(_PHOTO_BYTES)
    assert body["content_type"] == "image/jpeg"
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
    assert row.byte_size == len(_PHOTO_BYTES)
    assert row.content_type == "image/jpeg"
    assert row.sha256 == _PHOTO_SHA
    assert row.object_key == key


async def test_upload_photo_accepts_uppercase_declared_sha(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA.upper())

    assert response.status_code == 201, response.text


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
    not_a_jpeg = b"%PDF-1.4 not really a pdf"

    response = _upload_photo(
        client, token, execution_id, not_a_jpeg, hashlib.sha256(not_a_jpeg).hexdigest()
    )

    assert response.status_code == 422, response.text
    assert _store(client).objects == {}


async def test_upload_photo_svg_rejected_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """An SVG with a valid declared sha256 still fails the JPEG magic-byte check — this is
    the stored-XSS guard for the content proxy."""
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_photo(
        client,
        token,
        execution_id,
        _SVG_BYTES,
        hashlib.sha256(_SVG_BYTES).hexdigest(),
        content_type="image/svg+xml",
    )

    assert response.status_code == 422, response.text
    assert _store(client).objects == {}
    row = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.execution_id == uuid.UUID(execution_id))
    )
    assert row is None


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


async def test_upload_note_too_long_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    response = _upload_note(client, token, execution_id, "a" * 2001)

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
    assert response.headers["x-content-type-options"] == "nosniff"
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
    token_a, execution_a = await _seed_execution(client, db_session, test_settings)
    evidence_a = _upload_photo(client, token_a, execution_a, _PHOTO_BYTES, _PHOTO_SHA).json()["id"]
    token_b, _execution_b, evidence_b = await _seed_other_worker_execution(client, db_session)

    def _get(evidence_id: str, token: str) -> Response:
        return client.get(
            f"/evidence/{evidence_id}/content", headers={"Authorization": f"Bearer {token}"}
        )

    assert _get(evidence_a, token_a).status_code == 200
    assert _get(evidence_b, token_b).status_code == 200
    assert _get(evidence_b, token_a).status_code == 403
    assert _get(evidence_a, token_b).status_code == 403


async def test_list_evidence_owner_200_and_non_owner_403(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token_a, execution_a = await _seed_execution(client, db_session, test_settings)
    assert _upload_note(client, token_a, execution_a, "Nota do titular.").status_code == 201
    token_b, _execution_b, _evidence_b = await _seed_other_worker_execution(client, db_session)

    owner = client.get(
        f"/executions/{execution_a}/evidence", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert owner.status_code == 200, owner.text
    assert len(owner.json()) == 1

    other_worker = client.get(
        f"/executions/{execution_a}/evidence", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert other_worker.status_code == 403, other_worker.text


async def test_upload_note_repeat_idempotency_key_returns_existing(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """spec §8: the app's sync worker retries the same note after a network drop — the
    second POST returns 200 + the stored row, not a duplicate."""
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    key = str(uuid.uuid4())

    first = _upload_note(client, token, execution_id, "Piso molhado.", idempotency_key=key)
    second = _upload_note(client, token, execution_id, "Piso molhado.", idempotency_key=key)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]

    rows = (
        await db_session.scalars(
            select(EvidenceItem).where(EvidenceItem.execution_id == uuid.UUID(execution_id))
        )
    ).all()
    assert len(rows) == 1


async def test_upload_photo_repeat_idempotency_key_returns_existing(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    key = str(uuid.uuid4())

    first = _upload_photo(
        client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA, idempotency_key=key
    )
    second = _upload_photo(
        client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA, idempotency_key=key
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]

    assert len(_store(client).objects) == 1
    rows = (
        await db_session.scalars(
            select(EvidenceItem).where(EvidenceItem.execution_id == uuid.UUID(execution_id))
        )
    ).all()
    assert len(rows) == 1


async def test_upload_note_distinct_idempotency_keys_create_two_rows(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)

    first = _upload_note(client, token, execution_id, "Nota 1.")
    second = _upload_note(client, token, execution_id, "Nota 2.")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]

    rows = (
        await db_session.scalars(
            select(EvidenceItem).where(EvidenceItem.execution_id == uuid.UUID(execution_id))
        )
    ).all()
    assert len(rows) == 2


async def test_listed_evidence_carries_its_idempotency_key(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """The key the app generated offline is how a MEDIA answer points at its photos."""
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    key = str(uuid.uuid4())
    assert (
        _upload_note(client, token, execution_id, "Nota.", idempotency_key=key).status_code == 201
    )

    listing = client.get(
        f"/executions/{execution_id}/evidence", headers={"Authorization": f"Bearer {token}"}
    )

    assert [item["idempotency_key"] for item in listing.json()] == [key]


async def test_upload_for_a_released_stop_is_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # Photos taken offline for a stop released before they synced: the stop is in the pool
    # now, so they are refused with the same answer as the check-out (spec §7.5).
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    execution = await db_session.get(Execution, uuid.UUID(execution_id))
    assert execution is not None
    await _release_stop(db_session, execution.route_stop_id, ReleaseReason.LOCATION_SILENCE)

    photo = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)
    note = _upload_note(client, token, execution_id, "Banheiro limpo")

    for response in (photo, note):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "STOP_RELEASED"
        assert response.json()["detail"]["appealable"] is True
    assert await db_session.scalar(select(EvidenceItem.id)) is None


# --- a late photo reconciles INSUFFICIENT_EVIDENCE (spec 2026-10-26 §6.2) -----------------------


def _check_out(client: TestClient, token: str, qr_payload: str, **extra: object) -> Response:
    body: dict[str, object] = {
        "qr_payload": qr_payload,
        "latitude": _POINT_LAT,
        "longitude": _POINT_LNG,
        "scanned_at": datetime.now(UTC).isoformat(),
        "checkout_idempotency_key": str(uuid.uuid4()),
        **extra,
    }
    return client.post("/check-outs", json=body, headers={"Authorization": f"Bearer {token}"})


async def _checked_out_short_of_photos(
    client: TestClient,
    db_session: AsyncSession,
    test_settings: Settings,
    **check_out_overrides: object,
) -> tuple[str, str]:
    """A stop that requires evidence, checked in, given one photo and checked out: flagged
    INSUFFICIENT_EVIDENCE. Returns (worker token, execution id)."""
    seeded = await _seed_route(db_session, test_settings)
    seeded.stops[0].requires_evidence = True
    await db_session.commit()
    token = _login(client)
    checkin = _post(client, token, seeded.qr_code.public_code)
    execution_id = str(checkin.json()["execution_id"])
    assert _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA).status_code == 201
    checkout = _check_out(client, token, seeded.qr_code.public_code, **check_out_overrides)
    assert checkout.status_code == 201, checkout.text
    assert "INSUFFICIENT_EVIDENCE" in checkout.json()["validation_flags"]
    return token, execution_id


async def _reload(db_session: AsyncSession, execution_id: str) -> Execution:
    execution = await db_session.get(Execution, uuid.UUID(execution_id))
    assert execution is not None
    await db_session.refresh(execution)
    return execution


async def test_late_photo_completing_two_clears_flag_and_review(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _checked_out_short_of_photos(client, db_session, test_settings)

    response = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)

    assert response.status_code == 201, response.text
    execution = await _reload(db_session, execution_id)
    assert "INSUFFICIENT_EVIDENCE" not in execution.validation_flags
    assert execution.review_status.value == "NONE"


async def test_late_photo_keeps_other_flags_pending(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _checked_out_short_of_photos(
        client, db_session, test_settings, latitude=None, longitude=None
    )

    response = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)

    assert response.status_code == 201, response.text
    execution = await _reload(db_session, execution_id)
    assert execution.validation_flags == ["GPS_UNAVAILABLE"]
    assert execution.review_status.value == "PENDING_REVIEW"


async def test_late_photo_after_manager_resolved_keeps_resolved(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _checked_out_short_of_photos(client, db_session, test_settings)
    execution = await _reload(db_session, execution_id)
    execution.review_status = ExecutionReviewStatus.RESOLVED
    await db_session.commit()

    response = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)

    assert response.status_code == 201, response.text
    execution = await _reload(db_session, execution_id)
    assert "INSUFFICIENT_EVIDENCE" not in execution.validation_flags
    assert execution.review_status is ExecutionReviewStatus.RESOLVED


async def test_late_photo_still_short_keeps_flag(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """A note is not a photo: it never completes the count."""
    token, execution_id = await _checked_out_short_of_photos(client, db_session, test_settings)

    assert _upload_note(client, token, execution_id, "uma nota").status_code == 201

    execution = await _reload(db_session, execution_id)
    assert "INSUFFICIENT_EVIDENCE" in execution.validation_flags
    assert execution.review_status.value == "PENDING_REVIEW"


async def test_photo_before_check_out_does_not_touch_flags(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, execution_id = await _seed_execution(client, db_session, test_settings)
    execution = await _reload(db_session, execution_id)
    execution.validation_flags = ["INSUFFICIENT_EVIDENCE"]
    await db_session.commit()

    for _ in range(2):
        assert (
            _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA).status_code == 201
        )

    execution = await _reload(db_session, execution_id)
    assert execution.checked_out_at is None
    assert execution.validation_flags == ["INSUFFICIENT_EVIDENCE"]
