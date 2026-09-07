"""Etapa 8 — the impediment API: a field worker reports, a manager lists the queue, ranks
candidates, reassigns or dismisses. Domain mechanics are covered under
tests/domain/impediments/; here we pin the HTTP surface (auth, ownership, status mapping)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    Floor,
    ServicePoint,
)
from app.domain.identity.models import User, UserRole
from app.domain.impediments import service as impediments_service
from app.domain.impediments.models import (
    Impediment,
    ImpedimentReason,
    ImpedimentScope,
    ImpedimentStatus,
)
from app.domain.monitoring.models import Notification
from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteStopStatus, RouteType
from app.domain.routing.service import StopInput, create_route
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "senha-forte-o-suficiente"
_ROUTE_DATE = date.today()


@pytest.fixture
def client(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[TestClient]:
    # Local override so every reassignment goes through the in-memory OSRM fake (spec §8).
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=FakeOsrmClient(),
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Fixture:
    def __init__(
        self,
        *,
        manager: User,
        worker_a_user: User,
        worker_b_user: User,
        worker_a: FieldWorker,
        worker_b: FieldWorker,
        source: Route,
        target: Route,
    ) -> None:
        self.manager = manager
        self.worker_a_user = worker_a_user
        self.worker_b_user = worker_b_user
        self.worker_a = worker_a
        self.worker_b = worker_b
        self.source = source
        self.target = target


async def _seed(db: AsyncSession, *, source_points: int = 2, target_points: int = 1) -> _Fixture:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.MANAGER,
    )
    worker_a_user = User(
        name="Joao",
        email="joao@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    worker_b_user = User(
        name="Maria",
        email="maria@empresa.com",
        password_hash=hash_password(_PASSWORD),
        role=UserRole.FIELD_WORKER,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db.add_all([manager, worker_a_user, worker_b_user, company, building])
    await db.flush()

    floor = Floor(building_id=building.id, label="Terreo")
    db.add(floor)
    await db.flush()

    n_points = source_points + target_points
    points = [
        ServicePoint(
            floor_id=floor.id,
            name=f"Ponto {i}",
            description="Sala",
            latitude=-7.20 - i * 0.01,
            longitude=-35.90 - i * 0.01,
        )
        for i in range(n_points)
    ]
    worker_a = FieldWorker(
        full_name="Joao da Silva", contractor_company_id=company.id, user_id=worker_a_user.id
    )
    worker_b = FieldWorker(
        full_name="Maria Souza", contractor_company_id=company.id, user_id=worker_b_user.id
    )
    db.add_all([*points, worker_a, worker_b])
    await db.flush()

    source = await create_route(
        db,
        field_worker_id=worker_a.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[:source_points]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    source.status = RouteStatus.IN_PROGRESS
    target = await create_route(
        db,
        field_worker_id=worker_b.id,
        route_date=_ROUTE_DATE,
        route_type=RouteType.REGULAR,
        scheduled_start_at=None,
        stops=[StopInput(service_point_id=p.id) for p in points[source_points:]],
        actor_id=manager.id,
        osrm=FakeOsrmClient(),
    )
    await db.commit()
    return _Fixture(
        manager=manager,
        worker_a_user=worker_a_user,
        worker_b_user=worker_b_user,
        worker_a=worker_a,
        worker_b=worker_b,
        source=source,
        target=target,
    )


def _report_body(*, route_stop_id: str | None, scope: str = "STOP") -> dict[str, object]:
    return {
        "route_stop_id": route_stop_id,
        "scope": scope,
        "reason": "OTHER",
        "note": None,
        "reported_at": datetime.now(UTC).isoformat(),
    }


# --- POST /routes/{route_id}/impediments (field worker) --------------------------------


async def test_report_stop_impediment_created(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.worker_a_user.email)

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=_report_body(route_stop_id=str(fx.source.stops[0].id)),
        headers=_auth(token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["route_id"] == str(fx.source.id)
    assert body["route_stop_id"] == str(fx.source.stops[0].id)
    assert body["status"] == "PENDING"
    assert body["received_at"] is not None


async def test_report_on_route_not_owned_is_403(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.worker_b_user.email)  # worker B does not own the source route

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=_report_body(route_stop_id=str(fx.source.stops[0].id)),
        headers=_auth(token),
    )

    assert response.status_code == 403


async def test_report_on_unknown_route_is_404(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.worker_a_user.email)

    response = client.post(
        f"/routes/{uuid.uuid4()}/impediments",
        json=_report_body(route_stop_id=None, scope="ROUTE_REMAINDER"),
        headers=_auth(token),
    )

    assert response.status_code == 404


async def test_report_when_route_not_in_progress_is_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    route = await db_session.get(Route, fx.source.id)
    assert route is not None
    route.status = RouteStatus.PLANNED
    await db_session.commit()
    token = _login(client, fx.worker_a_user.email)

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=_report_body(route_stop_id=str(fx.source.stops[0].id)),
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_report_stop_done_is_409(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    stop = await db_session.get(RouteStop, fx.source.stops[0].id)
    assert stop is not None
    stop.status = RouteStopStatus.DONE
    await db_session.commit()
    token = _login(client, fx.worker_a_user.email)

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=_report_body(route_stop_id=str(fx.source.stops[0].id)),
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_report_stop_in_progress_is_201(client: TestClient, db_session: AsyncSession) -> None:
    # F1 fix (Ruling P3): a worker who checked in (stop → IN_PROGRESS) and then hit a wall
    # must still be able to signal the impediment — no 409.
    fx = await _seed(db_session)
    stop = await db_session.get(RouteStop, fx.source.stops[0].id)
    assert stop is not None
    stop.status = RouteStopStatus.IN_PROGRESS
    await db_session.commit()
    token = _login(client, fx.worker_a_user.email)

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=_report_body(route_stop_id=str(fx.source.stops[0].id)),
        headers=_auth(token),
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "PENDING"


async def test_report_forbidden_for_manager(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.manager.email)

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=_report_body(route_stop_id=str(fx.source.stops[0].id)),
        headers=_auth(token),
    )

    assert response.status_code == 403


async def test_report_note_over_column_length_is_422(
    client: TestClient, db_session: AsyncSession
) -> None:
    # F1 fix: a 501-char note exceeds the `impediments.note` column (`String(500)`); the
    # Pydantic `max_length=500` guard must reject it with 422, not let Postgres 500.
    fx = await _seed(db_session)
    token = _login(client, fx.worker_a_user.email)
    body = _report_body(route_stop_id=str(fx.source.stops[0].id))
    body["note"] = "x" * 501

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=body,
        headers=_auth(token),
    )

    assert response.status_code == 422, response.text


async def test_report_remainder_forces_route_stop_id_null(
    client: TestClient, db_session: AsyncSession
) -> None:
    # F3 fix (spec §3): `route_stop_id` is meaningless for a ROUTE_REMAINDER report; a stray
    # non-null id in the body is dropped, not persisted.
    fx = await _seed(db_session)
    token = _login(client, fx.worker_a_user.email)
    body = _report_body(route_stop_id=str(fx.source.stops[0].id), scope="ROUTE_REMAINDER")

    response = client.post(
        f"/routes/{fx.source.id}/impediments",
        json=body,
        headers=_auth(token),
    )

    assert response.status_code == 201, response.text
    assert response.json()["route_stop_id"] is None


# --- GET /impediments (manager) -------------------------------------------------------


async def _make_impediment(
    db: AsyncSession,
    fx: _Fixture,
    *,
    stop_id: uuid.UUID | None,
    scope: ImpedimentScope = ImpedimentScope.STOP,
) -> Impediment:
    imp = Impediment(
        route_id=fx.source.id,
        route_stop_id=stop_id,
        scope=scope,
        reason=ImpedimentReason.OTHER,
        note=None,
        reported_by=fx.worker_a.id,
        reported_at=datetime.now(UTC),
    )
    db.add(imp)
    await db.commit()
    return imp


async def test_list_pending_returns_queue(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.manager.email)

    response = client.get("/impediments?status=PENDING", headers=_auth(token))

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [str(imp.id)]


async def test_list_pending_forbidden_for_field_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.worker_a_user.email)

    response = client.get("/impediments", headers=_auth(token))

    assert response.status_code == 403


async def test_list_invalid_status_is_422(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.manager.email)

    response = client.get("/impediments?status=BOGUS", headers=_auth(token))

    assert response.status_code == 422
    assert "BOGUS" in response.json()["detail"]


# --- GET /impediments/{id}/candidates (manager) -------------------------------------


async def test_candidates_ranked(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.manager.email)

    response = client.get(f"/impediments/{imp.id}/candidates", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert [row["field_worker_id"] for row in body] == [str(fx.worker_b.id)]
    assert body[0]["pending_stop_count"] == 1
    assert body[0]["distance_m"] >= 0


async def test_candidates_unknown_impediment_is_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.manager.email)

    response = client.get(f"/impediments/{uuid.uuid4()}/candidates", headers=_auth(token))

    assert response.status_code == 404


# --- POST /impediments/{id}/reassign (manager) -------------------------------------


async def test_reassign_moves_stop_and_resolves(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    moved_stop_id = fx.source.stops[0].id
    target_id = fx.target.id
    imp = await _make_impediment(db_session, fx, stop_id=moved_stop_id)
    token = _login(client, fx.manager.email)

    response = client.post(
        f"/impediments/{imp.id}/reassign",
        json={"field_worker_id": str(fx.worker_b.id), "reason": "afastamento medico"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "RESOLVED"
    db_session.expire_all()  # drop the identity-map copies seeded before the request's commit
    moved = await db_session.get(RouteStop, moved_stop_id)
    assert moved is not None
    assert moved.route_id == target_id
    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "impediment", AuditTrail.action == "reassign")
    )
    assert audit_count == 1


async def test_reassign_notifies_the_target_route_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.manager.email)

    response = client.post(
        f"/impediments/{imp.id}/reassign",
        json={"field_worker_id": str(fx.worker_b.id), "reason": "afastamento medico"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    notes = (
        await db_session.scalars(
            select(Notification).where(Notification.user_id == fx.worker_b_user.id)
        )
    ).all()
    assert len(notes) == 1
    assert notes[0].kind == "STOP_REASSIGNED"
    assert notes[0].data["route_id"] == str(fx.target.id)


async def test_reassign_already_resolved_is_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    imp_row = await db_session.get(Impediment, imp.id)
    assert imp_row is not None
    imp_row.status = ImpedimentStatus.RESOLVED
    await db_session.commit()
    token = _login(client, fx.manager.email)

    response = client.post(
        f"/impediments/{imp.id}/reassign",
        json={"field_worker_id": str(fx.worker_b.id), "reason": "x"},
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_reassign_worker_without_route_on_date_is_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    orphan = FieldWorker(
        full_name="Sem Rota", contractor_company_id=fx.worker_a.contractor_company_id
    )
    db_session.add(orphan)
    await db_session.commit()
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.manager.email)

    response = client.post(
        f"/impediments/{imp.id}/reassign",
        json={"field_worker_id": str(orphan.id), "reason": "x"},
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_reassign_same_route_target_is_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.manager.email)

    response = client.post(
        f"/impediments/{imp.id}/reassign",
        json={"field_worker_id": str(fx.worker_a.id), "reason": "x"},
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_reassign_stale_stop_propagates_409(
    client: TestClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that became DONE between the report and the reassign (finished by another
    path) makes `reassign_stops` reject the batch — propagated as 409 via
    StopNotReassignable."""
    fx = await _seed(db_session, source_points=2, target_points=1)
    stale = await db_session.get(RouteStop, fx.source.stops[1].id)
    assert stale is not None
    stale.status = RouteStopStatus.DONE
    await db_session.commit()
    imp = await _make_impediment(
        db_session, fx, stop_id=None, scope=ImpedimentScope.ROUTE_REMAINDER
    )

    real_stops = [fx.source.stops[0], fx.source.stops[1]]

    async def _stale_affected(db: AsyncSession, impediment: Impediment) -> list[RouteStop]:
        return real_stops

    monkeypatch.setattr(impediments_service, "_affected_stops", _stale_affected)
    token = _login(client, fx.manager.email)

    response = client.post(
        f"/impediments/{imp.id}/reassign",
        json={"field_worker_id": str(fx.worker_b.id), "reason": "x"},
        headers=_auth(token),
    )

    assert response.status_code == 409


async def test_reassign_forbidden_for_field_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.worker_a_user.email)

    response = client.post(
        f"/impediments/{imp.id}/reassign",
        json={"field_worker_id": str(fx.worker_b.id), "reason": "x"},
        headers=_auth(token),
    )

    assert response.status_code == 403


# --- POST /impediments/{id}/dismiss (manager) -------------------------------------


async def test_dismiss_marks_resolved_and_audits(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.manager.email)

    response = client.post(f"/impediments/{imp.id}/dismiss", headers=_auth(token))

    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"
    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditTrail)
        .where(AuditTrail.entity_type == "impediment", AuditTrail.action == "dismiss")
    )
    assert audit_count == 1


async def test_dismiss_twice_is_409(client: TestClient, db_session: AsyncSession) -> None:
    fx = await _seed(db_session)
    imp = await _make_impediment(db_session, fx, stop_id=fx.source.stops[0].id)
    token = _login(client, fx.manager.email)

    first = client.post(f"/impediments/{imp.id}/dismiss", headers=_auth(token))
    second = client.post(f"/impediments/{imp.id}/dismiss", headers=_auth(token))

    assert first.status_code == 200
    assert second.status_code == 409


async def test_dismiss_unknown_impediment_is_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    fx = await _seed(db_session)
    token = _login(client, fx.manager.email)

    response = client.post(f"/impediments/{uuid.uuid4()}/dismiss", headers=_auth(token))

    assert response.status_code == 404
