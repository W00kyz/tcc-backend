import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from app.api.deps import get_campus_today
from app.core.config import Settings
from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerStatus,
    Floor,
    ServicePoint,
)
from app.domain.execution.models import Execution
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode, QrCodeStatus
from app.domain.routing.models import (
    ReleaseReason,
    Route,
    RouteStatus,
    RouteStop,
    RouteStopStatus,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_route

_WORKER_EMAIL = "joao@empresa.com"
_WORKER_PASSWORD = "senha-forte-o-suficiente"
_POINT_LAT = -7.2
_POINT_LNG = -35.9

# (name, latitude, longitude) for the service points seeded on the QR's floor.
PointSpec = tuple[str, float, float]

# Two rooms on one floor, both inside the check radius (~22 m apart) — "Sala A" is nearer.
_TWO_ROOMS: list["PointSpec"] = [
    ("Sala A", _POINT_LAT, _POINT_LNG),
    ("Sala B", _POINT_LAT - 0.0002, _POINT_LNG),
]


@dataclass
class SeededRoute:
    worker: FieldWorker
    route: Route
    floor: Floor
    qr_code: QrCode
    stops: list[RouteStop]
    points: list[ServicePoint]


async def _seed_route(
    db_session: AsyncSession,
    settings: Settings,
    *,
    point_specs: list[PointSpec] | None = None,
    started: bool = True,
    qr_status: QrCodeStatus = QrCodeStatus.ACTIVE,
    worker_email: str = _WORKER_EMAIL,
    footprint: dict[str, object] | None = None,
) -> SeededRoute:
    """One field worker, one floor with its signed QR, and a route (started by default) whose
    PENDING stops are `point_specs` (a single room at the GPS origin when omitted)."""
    specs = point_specs or [("Sala 101", _POINT_LAT, _POINT_LNG)]

    worker_user = User(
        name="João",
        email=worker_email,
        password_hash=hash_password(_WORKER_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT", footprint=footprint)
    db_session.add_all([worker_user, company, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    worker = FieldWorker(
        full_name="João",
        contractor_company_id=company.id,
        user_id=worker_user.id,
        status=FieldWorkerStatus.APPROVED,
    )
    points = [
        ServicePoint(floor_id=floor.id, name=name, description="Sala", latitude=lat, longitude=lng)
        for name, lat, lng in specs
    ]
    db_session.add_all([worker, *points])
    await db_session.flush()

    qr_payload = sign_qr_payload(
        floor_id=floor.id, version=1, private_key_hex=settings.qr_signing_private_key_hex
    )
    qr_code = QrCode(
        floor_id=floor.id, public_code=qr_payload, secret=b"sig", version=1, status=qr_status
    )
    db_session.add(qr_code)
    route = await make_route(db_session, worker=worker, day=today_on_campus(), points=points)
    if not started:
        route.started_at = None
    await db_session.commit()
    return SeededRoute(
        worker=worker,
        route=route,
        floor=floor,
        qr_code=qr_code,
        stops=list(route.stops),
        points=points,
    )


async def _extra_floor_qr(
    db_session: AsyncSession, settings: Settings, building_id: uuid.UUID
) -> QrCode:
    """A second floor in the same building with a valid QR but no route stop on it."""
    floor = Floor(building_id=building_id, label="1º andar")
    db_session.add(floor)
    await db_session.flush()
    payload = sign_qr_payload(
        floor_id=floor.id, version=1, private_key_hex=settings.qr_signing_private_key_hex
    )
    qr_code = QrCode(floor_id=floor.id, public_code=payload, secret=b"sig", version=1)
    db_session.add(qr_code)
    await db_session.commit()
    return qr_code


async def _add_user(db_session: AsyncSession, email: str, role: UserRole) -> None:
    db_session.add(
        User(
            name=email.split("@")[0],
            email=email,
            password_hash=hash_password(_WORKER_PASSWORD),
            role=role,
        )
    )
    await db_session.commit()


async def _add_field_worker(db_session: AsyncSession, email: str) -> None:
    user = User(
        name=email.split("@")[0],
        email=email,
        password_hash=hash_password(_WORKER_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Outra Limpeza", cnpj="98765432000155")
    db_session.add_all([user, company])
    await db_session.flush()
    db_session.add(
        FieldWorker(
            full_name=email,
            contractor_company_id=company.id,
            user_id=user.id,
            status=FieldWorkerStatus.APPROVED,
        )
    )
    await db_session.commit()


async def _release_stop(
    db_session: AsyncSession,
    stop_id: uuid.UUID,
    reason: ReleaseReason,
    *,
    released_at: datetime | None = None,
) -> None:
    """Give the stop back to the pool as the release scan or an impediment would, and pause its
    route — what a delayed offline sync finds when it lands."""
    stop = await db_session.get(RouteStop, stop_id)
    assert stop is not None
    stop.status = RouteStopStatus.RELEASED
    stop.release_reason = reason
    stop.released_at = released_at or datetime.now(UTC)
    route = await db_session.get(Route, stop.route_id)
    assert route is not None
    route.status = RouteStatus.PAUSED
    await db_session.commit()


def _stop_released(stop_id: uuid.UUID, reason: ReleaseReason, *, appealable: bool) -> object:
    return {
        "code": "STOP_RELEASED",
        "detail": f'Route stop "{stop_id}" was released back to the pool ({reason.value}).',
        "route_stop_id": str(stop_id),
        "release_reason": reason.value,
        "appealable": appealable,
    }


def _login(client: TestClient, email: str = _WORKER_EMAIL, password: str = _WORKER_PASSWORD) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    return str(response.json()["access_token"])


def _post(client: TestClient, token: str, qr_payload: str, **overrides: object) -> Response:
    body: dict[str, object] = {
        "qr_payload": qr_payload,
        "latitude": overrides.pop("latitude", _POINT_LAT),
        "longitude": overrides.pop("longitude", _POINT_LNG),
        "scanned_at": overrides.pop("scanned_at", datetime.now(UTC).isoformat()),
        "idempotency_key": overrides.pop("idempotency_key", str(uuid.uuid4())),
    }
    for key in ("route_stop_id", "execution_id", "client_clock_offset_seconds"):
        if key in overrides:
            body[key] = overrides.pop(key)
    return client.post("/check-ins", json=body, headers={"Authorization": f"Bearer {token}"})


async def test_check_in_single_candidate_201(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["route_stop_id"] == str(seeded.stops[0].id)
    assert payload["geo_validation"] == "VALIDATED"
    assert payload["validation_flags"] == []
    assert payload["review_status"] == "NONE"


async def test_check_in_ambiguous_409_lists_candidates(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(
        db_session,
        test_settings,
        point_specs=_TWO_ROOMS,
    )
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    candidates = response.json()["detail"]["candidates"]
    assert len(candidates) == 2
    assert candidates[0]["name"] == "Sala A"  # nearest first
    assert candidates[0]["distance_m"] < candidates[1]["distance_m"]


async def test_check_in_resubmit_with_route_stop_id_201(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(
        db_session,
        test_settings,
        point_specs=_TWO_ROOMS,
    )
    token = _login(client)

    ambiguous = _post(client, token, seeded.qr_code.public_code)
    chosen = ambiguous.json()["detail"]["candidates"][1]["route_stop_id"]

    response = _post(client, token, seeded.qr_code.public_code, route_stop_id=chosen)

    assert response.status_code == 201, response.text
    assert response.json()["route_stop_id"] == chosen


async def test_check_in_out_of_radius_201_pending_review(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code, latitude=-7.3, longitude=-35.9)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["geo_validation"] == "OUT_OF_RADIUS"
    assert "OUT_OF_RADIUS" in payload["validation_flags"]
    assert payload["review_status"] == "PENDING_REVIEW"


async def test_check_in_echoes_app_supplied_execution_id_and_flags_clock_skew(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    execution_id = str(uuid.uuid4())

    response = _post(
        client,
        token,
        seeded.qr_code.public_code,
        execution_id=execution_id,
        client_clock_offset_seconds=600.0,
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["execution_id"] == execution_id
    assert "CLOCK_SKEW" in payload["validation_flags"]
    assert payload["review_status"] == "PENDING_REVIEW"


async def test_check_in_revoked_qr_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings, qr_status=QrCodeStatus.REVOKED)
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 422, response.text


async def test_check_in_bad_signature_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    await _seed_route(db_session, test_settings)
    token = _login(client)
    forged = sign_qr_payload(floor_id=uuid.uuid4(), version=1, private_key_hex="99" * 32)

    response = _post(client, token, forged)

    assert response.status_code == 422, response.text


async def test_check_in_floor_not_on_route_422(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    other_qr = await _extra_floor_qr(db_session, test_settings, seeded.floor.building_id)
    token = _login(client)

    response = _post(client, token, other_qr.public_code)

    assert response.status_code == 422, response.text


async def test_check_in_wrong_worker_403(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_field_worker(db_session, "maria@empresa.com")
    token = _login(client, email="maria@empresa.com")

    response = _post(
        client, token, seeded.qr_code.public_code, route_stop_id=str(seeded.stops[0].id)
    )

    assert response.status_code == 403, response.text


async def test_check_in_route_not_started_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings, started=False)
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text


async def test_check_in_idempotent_returns_same_execution(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)
    key = "11111111-1111-1111-1111-111111111111"

    first = _post(client, token, seeded.qr_code.public_code, idempotency_key=key)
    second = _post(client, token, seeded.qr_code.public_code, idempotency_key=key)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["execution_id"] == second.json()["execution_id"]


async def test_field_worker_role_required_401_403(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    await _add_user(db_session, "gerente@empresa.com", UserRole.MANAGER)

    anonymous = client.post(
        "/check-ins",
        json={
            "qr_payload": seeded.qr_code.public_code,
            "scanned_at": datetime.now(UTC).isoformat(),
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert anonymous.status_code in (401, 403)

    manager_token = _login(client, email="gerente@empresa.com")
    forbidden = _post(client, manager_token, seeded.qr_code.public_code)
    assert forbidden.status_code == 403, forbidden.text


async def test_check_in_with_a_real_signed_qr_succeeds(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    token = _login(client)

    response = client.post(
        "/check-ins",
        json={
            "route_stop_id": str(seeded.stops[0].id),
            "qr_payload": seeded.qr_code.public_code,
            "latitude": _POINT_LAT,
            "longitude": _POINT_LNG,
            "scanned_at": datetime.now(UTC).isoformat(),
            "idempotency_key": "22222222-2222-2222-2222-222222222222",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["execution_id"] is not None
    assert body["geo_validation"] == "VALIDATED"


async def test_check_in_looks_for_the_route_of_the_campus_day(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """Regression: the active route was looked up by `date.today()`, the UTC date in the API
    container, so every check-in from 21:00 in Campina Grande failed with "No active route"."""
    seeded = await _seed_route(db_session, test_settings)
    # The route is on today's campus date; a campus day that moved on finds no route for it.
    cast(FastAPI, client.app).dependency_overrides[get_campus_today] = lambda: (
        today_on_campus() + timedelta(days=1)
    )
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    assert "No active route" in response.json()["detail"]


def _outline_around(half_side_deg: float) -> dict[str, object]:
    """A square building outline centred on the GPS origin (`_POINT_LAT`, `_POINT_LNG`)."""
    west, east = _POINT_LNG - half_side_deg, _POINT_LNG + half_side_deg
    south, north = _POINT_LAT - half_side_deg, _POINT_LAT + half_side_deg
    ring = [[west, north], [east, north], [east, south], [west, south], [west, north]]
    return {"type": "Polygon", "coordinates": [ring]}


async def test_two_rooms_inside_the_building_ask_for_the_room(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """Points are located by their building, so GPS cannot tell two rooms apart: the app gets
    the list, and the room the worker then picks is not flagged."""
    same_spot = [("Sala A", _POINT_LAT, _POINT_LNG), ("Sala B", _POINT_LAT, _POINT_LNG)]
    seeded = await _seed_route(
        db_session, test_settings, point_specs=same_spot, footprint=_outline_around(0.001)
    )
    token = _login(client)

    ambiguous = _post(client, token, seeded.qr_code.public_code)
    assert ambiguous.status_code == 409, ambiguous.text
    assert len(ambiguous.json()["detail"]["candidates"]) == 2

    chosen = _post(client, token, seeded.qr_code.public_code, route_stop_id=str(seeded.stops[1].id))
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["geo_validation"] == "VALIDATED"
    assert chosen.json()["validation_flags"] == []


async def test_a_fix_anywhere_inside_a_large_building_is_validated(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    """The outline, not the distance to its centre, decides: a fix ~200 m from the centre of a
    large building is still inside it."""
    seeded = await _seed_route(db_session, test_settings, footprint=_outline_around(0.003))
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code, latitude=_POINT_LAT - 0.0018)

    assert response.status_code == 201, response.text
    assert response.json()["geo_validation"] == "VALIDATED"


async def test_a_fix_outside_the_building_is_out_of_radius(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings, footprint=_outline_around(0.0003))
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code, latitude=_POINT_LAT - 0.01)

    assert response.status_code == 201, response.text
    assert response.json()["geo_validation"] == "OUT_OF_RADIUS"


# --- a check-in that lands after the stop was released (spec §7.5) --------------------------


async def test_checkin_on_released_stop_is_409_appealable(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # The worker checked in offline underground; the silence release ran before the sync.
    seeded = await _seed_route(db_session, test_settings)
    stop_id = seeded.stops[0].id
    await _release_stop(db_session, stop_id, ReleaseReason.LOCATION_SILENCE)
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _stop_released(
        stop_id, ReleaseReason.LOCATION_SILENCE, appealable=True
    )
    assert await db_session.scalar(select(Execution.id)) is None


async def test_checkin_after_the_appeal_window_is_not_appealable(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    seeded = await _seed_route(db_session, test_settings)
    stop_id = seeded.stops[0].id
    await _release_stop(
        db_session,
        stop_id,
        ReleaseReason.END_OF_DAY,
        released_at=datetime.now(UTC) - timedelta(hours=49),
    )
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["appealable"] is False


async def test_checkin_prefers_a_pending_stop_to_a_released_one(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # One room of the floor was impeded; the other is still the worker's to do.
    seeded = await _seed_route(db_session, test_settings, point_specs=_TWO_ROOMS)
    await _release_stop(db_session, seeded.stops[0].id, ReleaseReason.IMPEDIMENT)
    route = await db_session.get(Route, seeded.route.id)
    assert route is not None
    route.status = RouteStatus.IN_PROGRESS  # a single-stop impediment does not pause the route
    await db_session.commit()
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code)

    assert response.status_code == 201, response.text
    assert response.json()["route_stop_id"] == str(seeded.stops[1].id)


async def test_checkin_choosing_a_released_room_is_409(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # The re-submit names the room the worker was in, which was released meanwhile; the other
    # room of the floor being still pending must not turn it into "not on this floor".
    seeded = await _seed_route(db_session, test_settings, point_specs=_TWO_ROOMS)
    released_id = seeded.stops[0].id
    await _release_stop(db_session, released_id, ReleaseReason.LEFT_CAMPUS)
    token = _login(client)

    response = _post(client, token, seeded.qr_code.public_code, route_stop_id=str(released_id))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == _stop_released(
        released_id, ReleaseReason.LEFT_CAMPUS, appealable=True
    )
