"""Completion of an outdoor-place stop by the worker's position (spec 2026-10-30-locais-externos
§4): `POST /location-completions` creates one finished execution, or refuses and leaves the stop
open. The evidence endpoint then attaches the photos to the new execution."""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.catalog.models import ServicePoint
from app.domain.execution.models import Execution, ExecutionReviewStatus, QrScan
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind, AlertRule, AlertStatus, Notification
from app.domain.routing.models import ReleaseReason, Route, RouteStop, RouteStopStatus
from app.domain.settings.service import get_settings_row
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import _add_user, _login, _release_stop, _stop_released
from tests.api.test_evidence import _PHOTO_BYTES, _PHOTO_SHA, _upload_photo
from tests.support.clock import today_on_campus
from tests.support.factories import (
    ON_CAMPUS,
    make_place_point,
    make_points,
    make_route,
    make_service_type,
    make_worker,
)

_LAT, _LNG = ON_CAMPUS
_DEGREES_PER_METRE = 180 / (math.pi * 6_371_000)
_WORKER_EMAIL = "jardineiro@empresa.com"


@dataclass
class _Seed:
    route: Route
    stop: RouteStop
    point: ServicePoint


async def _seed(
    db: AsyncSession,
    *,
    email: str = _WORKER_EMAIL,
    requires_evidence: bool = False,
    building_stop: bool = False,
    company_name: str = "Limpa Tudo",
) -> _Seed:
    """A started route for the worker `email` with one stop at an outdoor place (or in a
    building room when asked)."""
    service_type = await make_service_type(
        db, company_name=company_name, type_name=f"Serviço {company_name}"
    )
    company = service_type.contractor_company
    assert company is not None
    worker = await make_worker(db, company=company, email=email)
    if building_stop:
        point = (await make_points(db, service_type=service_type, names=["Sala 1"]))[0]
    else:
        point = await make_place_point(
            db, service_type=service_type, name="Jardim", place_name=f"Praça {email}"
        )
    route = await make_route(db, worker=worker, day=today_on_campus(), points=[point])
    route.stops[0].requires_evidence = requires_evidence
    await db.commit()
    return _Seed(route=route, stop=route.stops[0], point=point)


def _body(stop: RouteStop, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "route_stop_id": str(stop.id),
        "execution_id": str(uuid.uuid4()),
        "idempotency_key": str(uuid.uuid4()),
        "latitude": _LAT,
        "longitude": _LNG,
        "accuracy_m": 8.5,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    body.update(overrides)
    return body


def _post(client: TestClient, token: str | None, body: dict[str, object]) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/location-completions", json=body, headers=headers)


async def _count(db: AsyncSession, model: type) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def test_completion_inside_the_radius_201(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)
    body = _body(seed.stop, complaints_suggestions="Grama alta")

    response = _post(client, token, body)

    assert response.status_code == 201, response.text
    assert response.json() == {
        "execution_id": body["execution_id"],
        "route_stop_id": str(seed.stop.id),
        "completed_at": datetime.fromisoformat(str(body["completed_at"]))
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "validation_flags": [],
        "review_status": "NONE",
    }
    await db_session.refresh(seed.stop)
    assert seed.stop.status is RouteStopStatus.DONE
    assert await _count(db_session, QrScan) == 0
    execution = await db_session.get(Execution, uuid.UUID(str(body["execution_id"])))
    assert execution is not None
    assert execution.complaints_suggestions == "Grama alta"


async def test_replay_returns_the_first_execution_without_new_rows(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)
    body = _body(seed.stop)

    first = _post(client, token, body)
    replay = _post(client, token, body)

    assert first.status_code == replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    assert await _count(db_session, Execution) == 1


async def test_far_from_the_place_422_out_of_radius_and_stop_stays_pending(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)
    far = _LAT + 120 * _DEGREES_PER_METRE

    response = _post(client, token, _body(seed.stop, latitude=far))

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "OUT_OF_RADIUS"
    assert detail["radius_m"] == 50
    assert 119 < detail["distance_m"] < 121
    assert isinstance(detail["detail"], str)
    assert await _count(db_session, Execution) == 0
    await db_session.refresh(seed.stop)
    assert seed.stop.status is RouteStopStatus.PENDING


async def test_the_radius_is_the_system_setting(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    (await get_settings_row(db_session)).check_radius_meters = 10
    await db_session.commit()
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, latitude=_LAT + 30 * _DEGREES_PER_METRE))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["radius_m"] == 10


async def test_without_a_position_422_location_required(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, latitude=None, longitude=None))

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "LOCATION_REQUIRED"
    assert isinstance(detail["detail"], str)
    assert await _count(db_session, Execution) == 0


async def test_a_building_stop_422_not_a_location_stop(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session, building_stop=True)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "NOT_A_LOCATION_STOP"
    assert await _count(db_session, Execution) == 0


async def test_a_released_stop_409_stop_released(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    await _release_stop(db_session, seed.stop.id, ReleaseReason.LOCATION_SILENCE)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _stop_released(
        seed.stop.id, ReleaseReason.LOCATION_SILENCE, appealable=True
    )
    assert await _count(db_session, Execution) == 0


async def test_a_stop_already_done_409_says_already(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)
    assert _post(client, token, _body(seed.stop)).status_code == 201

    # A different execution (new keys) for the same, now finished, stop.
    response = _post(client, token, _body(seed.stop))

    assert response.status_code == 409, response.text
    assert "already" in response.text
    assert await _count(db_session, Execution) == 1


async def test_an_unknown_stop_404(client: TestClient, db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, route_stop_id=str(uuid.uuid4())))

    assert response.status_code == 404, response.text


async def test_another_workers_stop_403(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)
    other = await _seed(db_session, email="outro@empresa.com", company_name="Verde")
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(other.stop))

    assert response.status_code == 403, response.text
    assert await _count(db_session, Execution) == 0


async def test_a_manager_403(client: TestClient, db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    await _add_user(db_session, "gestor@ufcg.edu.br", UserRole.MANAGER)
    token = _login(client, "gestor@ufcg.edu.br")

    response = _post(client, token, _body(seed.stop))

    assert response.status_code == 403, response.text


async def test_without_a_token_401(client: TestClient, db_session: AsyncSession) -> None:
    seed = await _seed(db_session)

    response = _post(client, None, _body(seed.stop))

    assert response.status_code == 401, response.text


async def test_a_skewed_device_clock_is_flagged_for_review(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, client_clock_offset_seconds=600.0))

    assert response.status_code == 201, response.text
    assert response.json()["validation_flags"] == ["CLOCK_SKEW"]
    assert response.json()["review_status"] == "PENDING_REVIEW"


async def test_oversized_complaints_422(client: TestClient, db_session: AsyncSession) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, complaints_suggestions="x" * 2001))

    assert response.status_code == 422, response.text


async def test_photos_uploaded_afterwards_clear_the_evidence_flag(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session, requires_evidence=True)
    token = _login(client, _WORKER_EMAIL)
    body = _body(seed.stop)
    execution_id = str(body["execution_id"])

    response = _post(client, token, body)

    assert response.status_code == 201, response.text
    assert response.json()["validation_flags"] == ["INSUFFICIENT_EVIDENCE"]
    assert response.json()["review_status"] == "PENDING_REVIEW"

    async def flags_and_review() -> tuple[list[str], ExecutionReviewStatus]:
        execution = await db_session.get(Execution, uuid.UUID(execution_id))
        assert execution is not None
        await db_session.refresh(execution)
        return execution.validation_flags, execution.review_status

    first = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)
    assert first.status_code == 201, first.text
    assert await flags_and_review() == (
        ["INSUFFICIENT_EVIDENCE"],
        ExecutionReviewStatus.PENDING_REVIEW,
    )

    second = _upload_photo(client, token, execution_id, _PHOTO_BYTES, _PHOTO_SHA)
    assert second.status_code == 201, second.text
    assert await flags_and_review() == ([], ExecutionReviewStatus.NONE)


async def test_a_refused_far_completion_alerts_the_managers_and_survives_the_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    await _add_user(db_session, "gestor@ufcg.edu.br", UserRole.MANAGER)
    manager = await db_session.scalar(select(User).where(User.email == "gestor@ufcg.edu.br"))
    assert manager is not None
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, latitude=_LAT + 120 * _DEGREES_PER_METRE))

    assert response.status_code == 422, response.text
    alert = await db_session.scalar(select(Alert))
    assert alert is not None
    assert alert.kind is AlertKind.COMPLETION_REFUSED
    assert alert.status is AlertStatus.OPEN
    assert alert.route_stop_id == seed.stop.id
    assert alert.route_id == seed.route.id
    assert alert.field_worker_id == seed.route.field_worker_id
    assert "a 120 m do local (limite 50 m)" in alert.description
    notification = await db_session.scalar(select(Notification))
    assert notification is not None
    assert notification.user_id == manager.id
    assert notification.body == alert.description
    await db_session.refresh(seed.stop)
    assert seed.stop.status is RouteStopStatus.PENDING
    assert await _count(db_session, Execution) == 0


async def test_a_second_refusal_the_same_day_keeps_one_alert(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    await _add_user(db_session, "gestor@ufcg.edu.br", UserRole.MANAGER)
    token = _login(client, _WORKER_EMAIL)
    far = _LAT + 120 * _DEGREES_PER_METRE

    first = _post(client, token, _body(seed.stop, latitude=far))
    second = _post(client, token, _body(seed.stop, latitude=far + 30 * _DEGREES_PER_METRE))

    assert first.status_code == second.status_code == 422
    assert await _count(db_session, Alert) == 1
    assert await _count(db_session, Notification) == 1


async def test_a_refusal_without_a_position_uses_the_location_required_wording(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, latitude=None, longitude=None))

    assert response.status_code == 422, response.text
    alert = await db_session.scalar(select(Alert))
    assert alert is not None
    assert alert.description.endswith("sem localização ativa.")


async def test_with_the_rule_off_the_refusal_is_still_422_but_raises_no_alert(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    await _add_user(db_session, "gestor@ufcg.edu.br", UserRole.MANAGER)
    await db_session.merge(AlertRule(rule_kind=AlertKind.COMPLETION_REFUSED, enabled=False))
    await db_session.commit()
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop, latitude=_LAT + 120 * _DEGREES_PER_METRE))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "OUT_OF_RADIUS"
    assert await _count(db_session, Alert) == 0
    assert await _count(db_session, Notification) == 0


async def test_an_accepted_completion_raises_no_alert(
    client: TestClient, db_session: AsyncSession
) -> None:
    seed = await _seed(db_session)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop))

    assert response.status_code == 201, response.text
    assert await _count(db_session, Alert) == 0


async def test_other_refusals_raise_no_alert(client: TestClient, db_session: AsyncSession) -> None:
    seed = await _seed(db_session, building_stop=True)
    token = _login(client, _WORKER_EMAIL)

    response = _post(client, token, _body(seed.stop))

    assert response.status_code == 422, response.text
    assert await _count(db_session, Alert) == 0
