"""RF31 — check-out via a real signed floor QR closing the open check-in on that floor.

Mirror of `test_checkins`: the QR identifies a floor, the service resolves which open
check-in on that floor to close, and two or more open check-ins answer 409 with the
candidate list so the app can re-submit with the chosen `route_stop_id`."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings
from app.domain.catalog.models import ServiceType
from app.domain.execution.models import Answer, Execution
from app.domain.forms.models import FormVersion, FormVersionStatus, QuestionType
from app.domain.forms.service import add_question, get_or_create_form, publish_form
from app.domain.identity.models import UserRole
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCodeStatus
from app.domain.routing.models import RouteStopStatus
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import (
    _POINT_LAT,
    _POINT_LNG,
    _TWO_ROOMS,
    SeededRoute,
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
    for key in (
        "route_stop_id",
        "execution_id",
        "client_clock_offset_seconds",
        "form_version_id",
        "answers",
    ):
        if key in overrides:
            body[key] = overrides.pop(key)
    return client.post("/check-outs", json=body, headers={"Authorization": f"Bearer {token}"})


@dataclass
class SeededForm:
    v1_id: uuid.UUID
    v2_id: uuid.UUID
    draft_id: uuid.UUID
    text_key: str
    bool_key: str


async def _publish_form_for_stop(db_session: AsyncSession, seeded: SeededRoute) -> SeededForm:
    """Give the first stop a service type and a form with two published versions.

    v2 is the active version; v1 is stale — the RF38 case (a check-out sent against v1)."""
    service_type = ServiceType(name="Limpeza pesada", average_duration_minutes=30)
    db_session.add(service_type)
    await db_session.flush()
    seeded.stops[0].service_type_id = service_type.id

    form = await get_or_create_form(db_session, service_type_id=service_type.id)
    for prompt, question_type in (
        ("Observações?", QuestionType.TEXT),
        ("Área limpa?", QuestionType.BOOLEAN),
    ):
        await add_question(
            db_session,
            form_id=form.id,
            prompt=prompt,
            question_type=question_type,
            required=True,
            options=[],
        )
    v1 = await publish_form(db_session, form_id=form.id)
    text_key, bool_key = (str(q.stable_key) for q in v1.questions)
    v1_id = v1.id
    v2_id = (await publish_form(db_session, form_id=form.id)).id
    draft_id = await db_session.scalar(
        select(FormVersion.id).where(
            FormVersion.form_id == form.id,
            FormVersion.status == FormVersionStatus.DRAFT,
        )
    )
    assert draft_id is not None
    await db_session.commit()
    return SeededForm(v1_id, v2_id, draft_id, text_key, bool_key)


async def test_check_out_persists_answers_against_the_sent_version(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    form = await _publish_form_for_stop(db_session, seeded)
    token = _login(client)
    assert _post(client, token, seeded.qr_code.public_code).status_code == 201

    key = "44444444-4444-4444-4444-444444444444"
    answers = [
        {"stable_key": form.text_key, "value": "tudo certo"},
        {"stable_key": form.bool_key, "value": True},
    ]
    first = _post_check_out(
        client,
        token,
        seeded.qr_code.public_code,
        checkout_idempotency_key=key,
        form_version_id=str(form.v1_id),
        answers=answers,
    )
    assert first.status_code == 201, first.text
    execution_id = uuid.UUID(first.json()["execution_id"])

    execution = await db_session.get(Execution, execution_id)
    assert execution is not None
    await db_session.refresh(execution)
    assert execution.form_version_id == form.v1_id  # the sent (stale) version, not active v2
    assert form.v1_id != form.v2_id

    rows = (
        await db_session.scalars(select(Answer).where(Answer.execution_id == execution_id))
    ).all()
    assert {(r.question_stable_key, r.value_json) for r in rows} == {
        (form.text_key, "tudo certo"),
        (form.bool_key, True),
    }

    # Replay: same checkout_idempotency_key short-circuits before any answer insert.
    second = _post_check_out(
        client,
        token,
        seeded.qr_code.public_code,
        checkout_idempotency_key=key,
        form_version_id=str(form.v1_id),
        answers=answers,
    )
    assert second.status_code == 201, second.text
    replayed = (
        await db_session.scalars(select(Answer).where(Answer.execution_id == execution_id))
    ).all()
    assert len(replayed) == 2  # no duplicates


async def test_check_out_rejects_draft_form_version(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    form = await _publish_form_for_stop(db_session, seeded)
    token = _login(client)
    assert _post(client, token, seeded.qr_code.public_code).status_code == 201

    response = _post_check_out(
        client,
        token,
        seeded.qr_code.public_code,
        form_version_id=str(form.draft_id),
        answers=[
            {"stable_key": form.text_key, "value": "x"},
            {"stable_key": form.bool_key, "value": False},
        ],
    )

    assert response.status_code == 422, response.text


async def test_check_out_rejects_answers_when_stop_has_no_service_type(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
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

    assert response.status_code == 422, response.text


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
