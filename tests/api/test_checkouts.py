"""RF31 — check-out via a real signed floor QR closing the open check-in on that floor.

Mirror of `test_checkins`: the QR identifies a floor, the service resolves which open
check-in on that floor to close, and two or more open check-ins answer 409 with the
candidate list so the app can re-submit with the chosen `route_stop_id`."""

import uuid
from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.domain.execution.models import Execution
from app.domain.identity.models import UserRole
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCodeStatus
from app.domain.routing.models import ReleaseReason, RouteStop, RouteStopStatus
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
    _release_stop,
    _seed_route,
    _stop_released,
)
from tests.api.test_evidence import _PHOTO_BYTES, _PHOTO_SHA, _upload_photo


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
    for key in (
        "route_stop_id",
        "execution_id",
        "client_clock_offset_seconds",
        "complaints_suggestions",
        "form_version_id",
        "answers",
    ):
        if key in overrides:
            body[key] = overrides.pop(key)
    return client.post("/check-outs", json=body, headers={"Authorization": f"Bearer {token}"})


async def test_check_out_ignores_legacy_form_fields(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """Apps in the field still send the dynamic-form fields: they are ignored, never a 422."""
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    assert _post(client, token, seeded.qr_code.public_code).status_code == 201

    response = _post_check_out(
        client,
        token,
        seeded.qr_code.public_code,
        form_version_id=str(uuid.uuid4()),
        answers=[{"stable_key": str(uuid.uuid4()), "value": "x"}],
    )

    assert response.status_code == 201, response.text


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


# --- a check-out that lands after the stop was released (spec §7.5) -------------------------


async def test_checkout_after_release_is_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # The check-in reached the server; the worker then lost signal, left the campus, and the
    # release ran before the check-out synced.
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    checkin = _post(client, token, seeded.qr_code.public_code)
    assert checkin.status_code == 201, checkin.text
    stop_id = seeded.stops[0].id
    await _release_stop(db_session, stop_id, ReleaseReason.LEFT_CAMPUS)

    response = _post_check_out(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _stop_released(
        stop_id, ReleaseReason.LEFT_CAMPUS, appealable=True
    )
    # Nothing is lost server-side: the check-in stands, still open, for the appeal to build on.
    execution = await db_session.get(Execution, uuid.UUID(checkin.json()["execution_id"]))
    assert execution is not None
    await db_session.refresh(execution)
    assert execution.checked_out_at is None
    stop = await db_session.get(RouteStop, stop_id)
    assert stop is not None
    await db_session.refresh(stop)
    assert stop.status is RouteStopStatus.RELEASED


async def test_checkout_without_checkin_on_a_released_stop_is_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # Both scans were offline: the check-in already answered 409, and the check-out queued
    # behind it must say the same thing rather than "no open check-in".
    seeded = await _seed_route(db_session, test_settings)
    await _release_stop(db_session, seeded.stops[0].id, ReleaseReason.LOCATION_SILENCE)
    token = _login(client)

    response = _post_check_out(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "STOP_RELEASED"
    assert response.json()["detail"]["appealable"] is True


async def test_impeded_stop_not_appealable(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # The worker released the stop themselves: there is nothing to contest.
    seeded = await _seed_route(db_session, test_settings, point_specs=_TWO_ROOMS)
    token = _login(client)
    stop_id = seeded.stops[0].id
    checkin = _post(client, token, seeded.qr_code.public_code, route_stop_id=str(stop_id))
    assert checkin.status_code == 201, checkin.text
    impediment = client.post(
        f"/routes/{seeded.route.id}/impediments",
        json={
            "route_stop_id": str(stop_id),
            "scope": "STOP",
            "reason": "ACCESS_BLOCKED",
            "reported_at": datetime.now(UTC).isoformat(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert impediment.status_code == 201, impediment.text

    response = _post_check_out(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _stop_released(
        stop_id, ReleaseReason.IMPEDIMENT, appealable=False
    )


# --- complaints_suggestions and the INSUFFICIENT_EVIDENCE flag (spec 2026-10-26 §6.2) ----------


def _get_execution(client: TestClient, manager_token: str, execution_id: str) -> dict[str, object]:
    response = client.get(
        f"/executions/{execution_id}", headers={"Authorization": f"Bearer {manager_token}"}
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def _check_in_with_photos(
    client: TestClient,
    db_session: AsyncSession,
    test_settings: Settings,
    *,
    requires_evidence: bool,
    photos: int,
) -> tuple[str, str, str]:
    """A started route whose single stop may require evidence, checked in and given `photos`
    photos; returns (worker token, QR payload, execution id). Not yet checked out."""
    seeded = await _seed_route(db_session, test_settings)
    seeded.stops[0].requires_evidence = requires_evidence
    await db_session.commit()
    token = _login(client)
    checkin = _post(client, token, seeded.qr_code.public_code)
    assert checkin.status_code == 201, checkin.text
    execution_id = str(checkin.json()["execution_id"])
    for _ in range(photos):
        assert (
            _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA).status_code == 201
        )
    return token, seeded.qr_code.public_code, execution_id


async def test_check_out_stores_complaints_suggestions(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, code, execution_id = await _check_in_with_photos(
        client, db_session, test_settings, requires_evidence=False, photos=0
    )
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)

    response = _post_check_out(client, token, code, complaints_suggestions="Falta sabão")

    assert response.status_code == 201, response.text
    detail = _get_execution(client, _login(client, email="gerente@empresa.com"), execution_id)
    assert detail["complaints_suggestions"] == "Falta sabão"


async def test_check_out_blank_complaints_suggestions_stored_as_null(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, code, execution_id = await _check_in_with_photos(
        client, db_session, test_settings, requires_evidence=False, photos=0
    )
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)

    response = _post_check_out(client, token, code, complaints_suggestions="   ")

    assert response.status_code == 201, response.text
    detail = _get_execution(client, _login(client, email="gerente@empresa.com"), execution_id)
    assert detail["complaints_suggestions"] is None


async def test_check_out_rejects_complaints_suggestions_over_2000(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, code, _ = await _check_in_with_photos(
        client, db_session, test_settings, requires_evidence=False, photos=0
    )

    response = _post_check_out(client, token, code, complaints_suggestions="x" * 2001)

    assert response.status_code == 422, response.text
    assert "2000" in response.text


async def test_check_out_accepts_complaints_suggestions_of_exactly_2000(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, code, _ = await _check_in_with_photos(
        client, db_session, test_settings, requires_evidence=False, photos=0
    )

    response = _post_check_out(client, token, code, complaints_suggestions="x" * 2000)

    assert response.status_code == 201, response.text


@pytest.mark.parametrize("photos", [0, 1])
async def test_check_out_requires_evidence_with_too_few_photos_is_flagged(
    client: TestClient, db_session: AsyncSession, test_settings: Settings, photos: int
) -> None:
    token, code, _ = await _check_in_with_photos(
        client, db_session, test_settings, requires_evidence=True, photos=photos
    )

    response = _post_check_out(client, token, code)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert "INSUFFICIENT_EVIDENCE" in payload["validation_flags"]
    assert payload["review_status"] == "PENDING_REVIEW"


async def test_check_out_requires_evidence_with_two_photos_not_flagged(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, code, _ = await _check_in_with_photos(
        client, db_session, test_settings, requires_evidence=True, photos=2
    )

    response = _post_check_out(client, token, code)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert "INSUFFICIENT_EVIDENCE" not in payload["validation_flags"]
    assert payload["review_status"] == "NONE"


async def test_check_out_without_requires_evidence_and_no_photos_not_flagged(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    token, code, _ = await _check_in_with_photos(
        client, db_session, test_settings, requires_evidence=False, photos=0
    )

    response = _post_check_out(client, token, code)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["validation_flags"] == []
    assert payload["review_status"] == "NONE"
