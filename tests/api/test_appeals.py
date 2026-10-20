"""The appeal API (spec 2026-10-20-alocacao-automatica §7.5, §8.4): a field worker lists the
released points they may still contest and files an appeal; a manager lists the appeals and
accepts or rejects them. An accepted appeal shows up in the execution history as `APPEAL`."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from app.api.deps import get_now
from app.core.campus_clock import campus_today
from app.core.security import hash_password
from app.domain.catalog.models import FieldWorker, FieldWorkerStatus
from app.domain.identity.models import User, UserRole
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
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_route, make_service_type, make_worker

_PASSWORD = "senha-forte-o-suficiente"
# Tuesday 10:00 on campus (UTC-3).
_NOW = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)
_RELEASED_AT = _NOW - timedelta(hours=1)
_MANAGER_EMAIL = "larissa@pu.ufcg.edu.br"
_WORKER_EMAIL = "joao@empresa.com"
_OTHER_EMAIL = "maria@empresa.com"


@dataclass
class _Seeded:
    worker: FieldWorker
    route: Route
    left_campus: RouteStop
    impeded: RouteStop


def _freeze(client: TestClient) -> None:
    cast(FastAPI, client.app).dependency_overrides[get_now] = lambda: _NOW


def _release(stop: RouteStop, reason: ReleaseReason) -> None:
    stop.status = RouteStopStatus.RELEASED
    stop.release_reason = reason
    stop.released_at = _RELEASED_AT


async def _seed(db: AsyncSession) -> _Seeded:
    """João's route of today: "Sala 1" released because he left the campus, "Sala 2" impeded by
    himself. Maria works for the same company; Larissa manages."""
    db.add(
        User(
            name="Larissa",
            email=_MANAGER_EMAIL,
            password_hash=hash_password(_PASSWORD),
            role=UserRole.MANAGER,
        )
    )
    service_type = await make_service_type(db)
    assert service_type.contractor_company is not None
    points = await make_points(db, service_type=service_type, names=["Sala 1", "Sala 2"])
    worker = await make_worker(db, company=service_type.contractor_company, email=_WORKER_EMAIL)
    await make_worker(
        db, company=service_type.contractor_company, full_name="Maria", email=_OTHER_EMAIL
    )
    route = await make_route(db, worker=worker, day=campus_today(_NOW), points=points)
    route.status = RouteStatus.PAUSED
    left_campus, impeded = route.stops
    _release(left_campus, ReleaseReason.LEFT_CAMPUS)
    _release(impeded, ReleaseReason.IMPEDIMENT)
    await db.commit()
    return _Seeded(worker=worker, route=route, left_campus=left_campus, impeded=impeded)


def _auth(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _file(
    client: TestClient,
    stop_id: uuid.UUID,
    *,
    email: str = _WORKER_EMAIL,
    idempotency_key: uuid.UUID | None = None,
) -> Response:
    return client.post(
        "/appeals",
        json={
            "route_stop_id": str(stop_id),
            "reason": "LEFT_BRIEFLY",
            "note": "Fui ao banco e voltei.",
            "claimed_executed_at": (_RELEASED_AT - timedelta(minutes=15)).isoformat(),
            "idempotency_key": str(idempotency_key or uuid.uuid4()),
        },
        headers=_auth(client, email),
    )


# --- the worker ------------------------------------------------------------------------------


async def test_eligible_lists_only_the_contestable_stops(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session)
    _freeze(client)

    response = client.get("/appeals/eligible", headers=_auth(client, _WORKER_EMAIL))

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "route_stop_id": str(seeded.left_campus.id),
            "service_point_id": str(seeded.left_campus.service_point_id),
            "service_point_name": "Sala 1",
            "building_name": response.json()[0]["building_name"],
            "floor_label": "Térreo",
            "released_at": "2026-10-20T12:00:00Z",
            "release_reason": "LEFT_CAMPUS",
            "deadline": "2026-10-22T12:00:00Z",
        }
    ]
    assert response.json()[0]["building_name"].startswith("Bloco ")


async def test_file_appeal_and_replay(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)
    _freeze(client)
    key = uuid.uuid4()

    first = _file(client, seeded.left_campus.id, idempotency_key=key)
    replay = _file(client, seeded.left_campus.id, idempotency_key=key)

    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "PENDING"
    assert body["reason"] == "LEFT_BRIEFLY"
    assert body["note"] == "Fui ao banco e voltei."
    assert body["claimed_executed_at"] == "2026-10-20T11:45:00Z"
    assert body["route_stop_id"] == str(seeded.left_campus.id)
    assert body["field_worker_id"] == str(seeded.worker.id)
    assert body["field_worker_name"] == "João da Silva"
    assert body["service_point_name"] == "Sala 1"
    assert body["release_reason"] == "LEFT_CAMPUS"
    assert body["released_at"] == "2026-10-20T12:00:00Z"
    assert body["reviewed_at"] is None
    assert body["review_note"] is None
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == body["id"]
    eligible = client.get("/appeals/eligible", headers=_auth(client, _WORKER_EMAIL))
    assert eligible.json() == []


async def test_not_appealable_carries_the_reason_code(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session)
    _freeze(client)

    impeded = _file(client, seeded.impeded.id)
    not_mine = _file(client, seeded.left_campus.id, email=_OTHER_EMAIL)

    assert impeded.status_code == 409, impeded.text
    assert impeded.json()["detail"] == {
        "code": "NOT_APPEALABLE",
        "detail": impeded.json()["detail"]["detail"],
        "reason_code": "REASON_NOT_APPEALABLE",
    }
    assert str(seeded.impeded.id) in impeded.json()["detail"]["detail"]
    assert not_mine.status_code == 409, not_mine.text
    assert not_mine.json()["detail"]["reason_code"] == "NOT_OWNER"


async def test_unknown_stop_is_404(client: TestClient, db_session: AsyncSession) -> None:
    await _seed(db_session)

    response = _file(client, uuid.uuid4())

    assert response.status_code == 404, response.text


async def test_mine_lists_only_my_appeals(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)
    _freeze(client)
    filed = _file(client, seeded.left_campus.id).json()

    mine = client.get("/appeals/mine", headers=_auth(client, _WORKER_EMAIL))
    others = client.get("/appeals/mine", headers=_auth(client, _OTHER_EMAIL))

    assert mine.status_code == 200, mine.text
    assert [appeal["id"] for appeal in mine.json()] == [filed["id"]]
    assert others.json() == []


async def test_worker_endpoints_need_an_approved_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session)
    seeded.worker.status = FieldWorkerStatus.REJECTED
    await db_session.commit()
    worker = _auth(client, _WORKER_EMAIL)
    manager = _auth(client, _MANAGER_EMAIL)

    assert client.get("/appeals/eligible", headers=worker).status_code == 403
    assert client.get("/appeals/mine", headers=worker).status_code == 403
    assert client.get("/appeals/eligible", headers=manager).status_code == 403
    rejected = client.post("/appeals", json={}, headers=worker)
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "WORKER_NOT_APPROVED"


# --- the manager -----------------------------------------------------------------------------


async def test_manager_lists_pending_first_and_filters(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session)
    _freeze(client)
    filed = _file(client, seeded.left_campus.id).json()
    manager = _auth(client, _MANAGER_EMAIL)

    every = client.get("/appeals", headers=manager)
    pending = client.get("/appeals", params={"status": "PENDING"}, headers=manager)
    rejected = client.get("/appeals", params={"status": "REJECTED"}, headers=manager)

    assert every.status_code == 200, every.text
    assert [appeal["id"] for appeal in every.json()] == [filed["id"]]
    assert [appeal["id"] for appeal in pending.json()] == [filed["id"]]
    assert rejected.json() == []


async def test_workers_cannot_review(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)
    _freeze(client)
    filed = _file(client, seeded.left_campus.id).json()
    worker = _auth(client, _WORKER_EMAIL)

    assert client.get("/appeals", headers=worker).status_code == 403
    accept = client.post(f"/appeals/{filed['id']}/accept", json={}, headers=worker)
    assert accept.status_code == 403


async def test_accept_then_review_again_conflicts(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session)
    _freeze(client)
    filed = _file(client, seeded.left_campus.id).json()
    manager = _auth(client, _MANAGER_EMAIL)

    accepted = client.post(
        f"/appeals/{filed['id']}/accept", json={"note": "Conferido."}, headers=manager
    )
    again = client.post(f"/appeals/{filed['id']}/reject", json={"note": None}, headers=manager)

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ACCEPTED"
    assert accepted.json()["review_note"] == "Conferido."
    assert accepted.json()["reviewed_at"] == "2026-10-20T13:00:00Z"
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "ALREADY_REVIEWED"


async def test_reject_with_a_note(client: TestClient, db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)
    _freeze(client)
    filed = _file(client, seeded.left_campus.id).json()

    response = client.post(
        f"/appeals/{filed['id']}/reject",
        json={"note": "Sem registro."},
        headers=_auth(client, _MANAGER_EMAIL),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REJECTED"
    assert response.json()["review_note"] == "Sem registro."


async def test_review_of_an_unknown_appeal_is_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    await _seed(db_session)

    response = client.post(
        f"/appeals/{uuid.uuid4()}/accept", json={}, headers=_auth(client, _MANAGER_EMAIL)
    )

    assert response.status_code == 404, response.text


async def test_accepted_appeal_is_in_the_history_as_appeal(
    client: TestClient, db_session: AsyncSession
) -> None:
    seeded = await _seed(db_session)
    _freeze(client)
    filed = _file(client, seeded.left_campus.id).json()
    manager = _auth(client, _MANAGER_EMAIL)
    client.post(f"/appeals/{filed['id']}/accept", json={}, headers=manager)

    by_appeal = client.get("/executions", params={"source": "APPEAL"}, headers=manager)
    by_app = client.get("/executions", params={"source": "APP"}, headers=manager)

    assert by_appeal.status_code == 200, by_appeal.text
    (item,) = by_appeal.json()["items"]
    assert item["source"] == "APPEAL"
    assert item["appeal_id"] == filed["id"]
    assert item["field_worker_id"] == str(seeded.worker.id)
    assert item["checked_in_at"] == "2026-10-20T11:45:00Z"
    assert by_app.json()["items"] == []
