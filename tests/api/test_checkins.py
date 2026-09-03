import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.core.config import Settings
from app.core.security import hash_password
from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, Floor, ServicePoint
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode, QrCodeStatus
from app.domain.routing.models import Route, RouteStatus, RouteStop
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

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
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([worker_user, company, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    worker = FieldWorker(full_name="João", contractor_company_id=company.id, user_id=worker_user.id)
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
    route = Route(
        field_worker_id=worker.id,
        route_date=date.today(),
        status=RouteStatus.IN_PROGRESS if started else RouteStatus.PLANNED,
        started_at=datetime.now(UTC) if started else None,
    )
    db_session.add_all([qr_code, route])
    await db_session.flush()

    stops = [
        RouteStop(route_id=route.id, service_point_id=point.id, order_index=index + 1)
        for index, point in enumerate(points)
    ]
    db_session.add_all(stops)
    await db_session.commit()
    return SeededRoute(
        worker=worker, route=route, floor=floor, qr_code=qr_code, stops=stops, points=points
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
    db_session.add(FieldWorker(full_name=email, contractor_company_id=company.id, user_id=user.id))
    await db_session.commit()


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
