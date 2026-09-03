"""RF31 — check-out via a real signed floor QR closing the open check-in on that floor.

Mirror of `test_checkins`: the QR identifies a floor, the service resolves which open
check-in on that floor to close, and two or more open check-ins answer 409 with the
candidate list so the app can re-submit with the chosen `route_stop_id`."""

import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.domain.identity.models import UserRole
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCodeStatus
from app.domain.routing.models import RouteStopStatus
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import (
    _POINT_LAT,
    _POINT_LNG,
    _TWO_ROOMS,
    _add_field_worker,
    _add_user,
    _login,
    _post,
    _seed_route,
)


def _post_check_out(
    client: TestClient, token: str, qr_payload: str, **overrides: object
) -> Response:
    body: dict[str, object] = {
        "qr_payload": qr_payload,
        "latitude": overrides.pop("latitude", _POINT_LAT),
        "longitude": overrides.pop("longitude", _POINT_LNG),
        "scanned_at": overrides.pop("scanned_at", datetime.now(UTC).isoformat()),
        "checkout_idempotency_key": overrides.pop("checkout_idempotency_key", str(uuid.uuid4())),
    }
    for key in ("route_stop_id", "execution_id", "client_clock_offset_seconds"):
        if key in overrides:
            body[key] = overrides.pop(key)
    return client.post("/check-outs", json=body, headers={"Authorization": f"Bearer {token}"})


async def test_check_out_after_check_in_marks_done(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    assert _post(client, token, seeded.qr_code.public_code).status_code == 201

    response = _post_check_out(client, token, seeded.qr_code.public_code)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["route_stop_id"] == str(seeded.stops[0].id)
    assert payload["checked_out_at"] is not None
    assert payload["review_status"] == "NONE"

    # The service committed on its own session; refresh this session's stale copy.
    await db_session.refresh(seeded.stops[0])
    assert seeded.stops[0].status is RouteStopStatus.DONE


async def test_check_out_accepts_execution_id_and_flags_clock_skew(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    assert _post(client, token, seeded.qr_code.public_code).status_code == 201

    response = _post_check_out(
        client,
        token,
        seeded.qr_code.public_code,
        execution_id=str(uuid.uuid4()),
        client_clock_offset_seconds=600.0,
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["execution_id"] is not None  # check-out ignores the app-supplied id
    assert "CLOCK_SKEW" in payload["validation_flags"]
    assert payload["review_status"] == "PENDING_REVIEW"


async def test_check_out_no_open_check_in_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)

    response = _post_check_out(client, token, seeded.qr_code.public_code)

    assert response.status_code == 422, response.text


async def test_check_out_idempotent(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    assert _post(client, token, seeded.qr_code.public_code).status_code == 201
    key = "33333333-3333-3333-3333-333333333333"

    code = seeded.qr_code.public_code
    first = _post_check_out(client, token, code, checkout_idempotency_key=key)
    second = _post_check_out(client, token, code, checkout_idempotency_key=key)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["execution_id"] == second.json()["execution_id"]


async def test_check_out_ambiguous_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings, point_specs=_TWO_ROOMS)
    token = _login(client)
    ambiguous = _post(client, token, seeded.qr_code.public_code)
    first_room = ambiguous.json()["detail"]["candidates"][0]["route_stop_id"]
    assert (
        _post(client, token, seeded.qr_code.public_code, route_stop_id=first_room).status_code
        == 201
    )
    # The second room is now the only PENDING stop on the floor — it resolves on its own.
    assert _post(client, token, seeded.qr_code.public_code).status_code == 201

    response = _post_check_out(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    candidates = response.json()["detail"]["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["distance_m"] <= candidates[1]["distance_m"]


async def test_check_out_wrong_worker_403(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_field_worker(db_session, "maria@empresa.com")
    token = _login(client, email="maria@empresa.com")

    response = _post_check_out(
        client, token, seeded.qr_code.public_code, route_stop_id=str(seeded.stops[0].id)
    )

    assert response.status_code == 403, response.text


async def test_check_out_revoked_qr_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings, qr_status=QrCodeStatus.REVOKED)
    token = _login(client)

    response = _post_check_out(client, token, seeded.qr_code.public_code)

    assert response.status_code == 422, response.text


async def test_check_out_bad_signature_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _seed_route(db_session, test_settings)
    token = _login(client)
    forged = sign_qr_payload(floor_id=uuid.uuid4(), version=1, private_key_hex="99" * 32)

    response = _post_check_out(client, token, forged)

    assert response.status_code == 422, response.text


async def test_check_out_field_worker_role_required(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)

    anonymous = client.post(
        "/check-outs",
        json={
            "qr_payload": seeded.qr_code.public_code,
            "scanned_at": datetime.now(UTC).isoformat(),
            "checkout_idempotency_key": str(uuid.uuid4()),
        },
    )
    assert anonymous.status_code in (401, 403)

    manager_token = _login(client, email="gerente@empresa.com")
    forbidden = _post_check_out(client, manager_token, seeded.qr_code.public_code)
    assert forbidden.status_code == 403, forbidden.text
