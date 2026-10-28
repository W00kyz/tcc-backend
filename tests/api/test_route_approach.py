"""`GET /routes/me/approach`: the live street leg from the worker's position to the next stop. It
only reads the route the allocation wrote; OSRM is always the named fake (spec §8)."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.domain.catalog.models import ContractorCompany, FieldWorker, ServicePoint
from app.domain.privacy.service import record_consent
from app.domain.routing.models import Route, RouteStopStatus
from app.domain.routing.osrm import OsrmClient, OsrmLeg
from app.main import create_app
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_points, make_route, make_worker, seed_privacy_notice
from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "senha-forte-o-suficiente"
_HERE = {"lat": -7.2140, "lng": -35.9090}

ClientFor = Callable[[OsrmClient], TestClient]


@pytest.fixture
def client_for(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[ClientFor]:
    """Builds an app around the given OSRM fake; every client is closed at teardown."""
    opened: list[TestClient] = []

    def build(osrm: OsrmClient) -> TestClient:
        app = create_app(
            settings=test_settings,
            mailer=recording_mailer,
            osrm_client=osrm,
            object_store=FakeObjectStore(),
        )
        test_client = TestClient(app, client=("127.0.0.1", 50000))
        opened.append(test_client.__enter__())
        return test_client

    yield build
    for test_client in opened:
        test_client.__exit__(None, None, None)


@dataclass
class _Day:
    worker: FieldWorker
    route: Route
    points: list[ServicePoint]


async def _seed_day(db: AsyncSession, *, email: str = "joao@empresa.com") -> _Day:
    """A consented worker with today's route over two points of one building."""
    company = ContractorCompany(name=f"Empresa {email}", cnpj="12345678000199")
    db.add(company)
    await db.flush()
    worker = await make_worker(db, company=company, email=email)
    points = await make_points(db, service_type=None, names=["Sala 1", "Sala 2"])
    route = await make_route(db, worker=worker, day=today_on_campus(), points=points)
    await seed_privacy_notice(db)
    await record_consent(db, user_id=worker.user_id, ip_address=None, user_agent=None)
    await db.commit()
    return _Day(worker=worker, route=route, points=points)


def _get(client: TestClient, email: str, params: dict[str, float] = _HERE) -> Response:
    login = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    token = login.json()["access_token"]
    return client.get(
        "/routes/me/approach", params=params, headers={"Authorization": f"Bearer {token}"}
    )


async def test_approach_returns_the_street_leg_to_the_first_pending_stop(
    client_for: ClientFor, db_session: AsyncSession
) -> None:
    day = await _seed_day(db_session)
    leg = OsrmLeg(distance_m=120.0, duration_s=90.0, geometry=[[-35.909, -7.214], [-35.91, -7.215]])
    osrm = FakeOsrmClient(legs=[leg])

    response = _get(client_for(osrm), "joao@empresa.com")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "route_stop_id": str(day.route.stops[0].id),
        "geometry": leg.geometry,
        "distance_m": 120.0,
        "duration_s": 90.0,
    }
    target = day.points[0]
    assert osrm.calls == [
        ("route_legs", [(_HERE["lng"], _HERE["lat"]), (target.longitude, target.latitude)])
    ]


async def test_approach_skips_stops_that_are_no_longer_pending(
    client_for: ClientFor, db_session: AsyncSession
) -> None:
    day = await _seed_day(db_session)
    day.route.stops[0].status = RouteStopStatus.DONE
    await db_session.commit()

    response = _get(client_for(FakeOsrmClient()), "joao@empresa.com")

    assert response.status_code == 200, response.text
    assert response.json()["route_stop_id"] == str(day.route.stops[1].id)


async def test_approach_is_204_without_a_pending_stop(
    client_for: ClientFor, db_session: AsyncSession
) -> None:
    day = await _seed_day(db_session)
    for stop in day.route.stops:
        stop.status = RouteStopStatus.DONE
    await db_session.commit()
    osrm = FakeOsrmClient()

    response = _get(client_for(osrm), "joao@empresa.com")

    assert response.status_code == 204
    assert response.content == b""
    assert osrm.calls == []


async def test_approach_is_503_when_osrm_is_down(
    client_for: ClientFor, db_session: AsyncSession
) -> None:
    await _seed_day(db_session)

    response = _get(client_for(FakeOsrmClient(unavailable=True)), "joao@empresa.com")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OSRM_UNAVAILABLE"
    assert response.json()["detail"]["detail"]


async def test_approach_is_503_when_osrm_cannot_route_the_leg(
    client_for: ClientFor, db_session: AsyncSession
) -> None:
    await _seed_day(db_session)
    zeroed = OsrmLeg(distance_m=0.0, duration_s=0.0, geometry=[])

    response = _get(client_for(FakeOsrmClient(legs=[zeroed])), "joao@empresa.com")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OSRM_UNAVAILABLE"


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 91.0, "lng": -35.9},
        {"lat": -90.5, "lng": -35.9},
        {"lat": -7.2, "lng": 180.1},
        {"lat": -7.2, "lng": -181.0},
        {"lat": -7.2},
    ],
)
async def test_approach_rejects_out_of_range_coordinates(
    client_for: ClientFor, db_session: AsyncSession, params: dict[str, float]
) -> None:
    await _seed_day(db_session)
    osrm = FakeOsrmClient()

    response = _get(client_for(osrm), "joao@empresa.com", params)

    assert response.status_code == 422
    assert osrm.calls == []


async def test_approach_never_uses_another_workers_route(
    client_for: ClientFor, db_session: AsyncSession
) -> None:
    await _seed_day(db_session, email="maria@empresa.com")
    company = ContractorCompany(name="Outra", cnpj="99999999000199")
    db_session.add(company)
    await db_session.flush()
    joao = await make_worker(db_session, company=company, email="joao@empresa.com")
    await record_consent(db_session, user_id=joao.user_id, ip_address=None, user_agent=None)
    await db_session.commit()
    osrm = FakeOsrmClient()

    response = _get(client_for(osrm), "joao@empresa.com")

    assert response.status_code == 204
    assert osrm.calls == []
