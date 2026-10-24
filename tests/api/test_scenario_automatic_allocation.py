"""Cenário ponta a ponta da alocação automática (spec 2026-10-20-alocacao-automatica §5-§8).

Uma terça-feira inteira no campus, com o app real, OSRM falso e relógio congelado, prova que
cadastro/aprovação (B2), alocação em lotes (B3), check-in/out, impedimento e varredura de
liberação (B4), recurso (B5) e relatório/quadro (B6) se encaixam:

- 08:00 dois profissionais se cadastram, a gestora aprova e ambos tocam "Iniciar rota" no mesmo
  prédio (8 pontos, lote de 4): lotes disjuntos;
- A faz check-in/out de um ponto, deixa um check-in aberto e reporta impedimento em outro: alerta
  `POINT_IMPEDED`, ponto livre no pool;
- B termina o lote e o `next-batch` lhe entrega o ponto impedido por A;
- A some por 46 min: a varredura libera o lote vivo de A (`BATCH_RELEASED`); o check-out tardio
  volta 409 `STOP_RELEASED` com `appealable`; A recorre, a gestora aceita;
- 17:00: a varredura fecha o dia, `POINTS_UNSERVED` uma única vez, e o relatório traz um desfecho
  por ponto (DONE / DONE_BY_APPEAL / UNSERVED).
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from app.api.deps import get_campus_today, get_now
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.domain.catalog.models import FieldWorker
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind
from app.domain.monitoring.release_scan import run_release_scan
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode
from app.domain.reports.point_days import point_day_rows
from app.domain.settings.service import get_settings_row
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_points, make_service_type, seed_privacy_notice
from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient

_PASSWORD = "uma-senha-longa-123"
_MANAGER_EMAIL = "gestora@pu.ufcg.edu.br"
_DAY = date(2026, 10, 20)  # a Tuesday
_POINT = (-7.2150, -35.9100)  # inside the campus rectangle; every point sits here
_BATCH = 4


def _at(hour: int, minute: int = 0) -> datetime:
    """A campus (UTC-3) wall-clock time of `_DAY` as an aware UTC instant."""
    return datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=UTC) + timedelta(hours=3)


@pytest.fixture
def client(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[TestClient]:
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=FakeOsrmClient(),
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _freeze(client: TestClient, now: datetime) -> None:
    overrides = cast(FastAPI, client.app).dependency_overrides
    overrides[get_now] = lambda: now
    overrides[get_campus_today] = lambda: _DAY


def _bearer(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _allocate(client: TestClient, headers: dict[str, str], path: str, now: datetime) -> Response:
    body = {"latitude": _POINT[0], "longitude": _POINT[1], "captured_at": now.isoformat()}
    return client.post(path, json=body, headers=headers)


def _stops_of(response: Response, batch: int) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    stops = cast(list[dict[str, Any]], response.json()["route"]["stops"])
    return [stop for stop in stops if stop["batch_number"] == batch]


def _check_in(
    client: TestClient, headers: dict[str, str], qr: str, stop_id: str, now: datetime
) -> Response:
    return client.post(
        "/check-ins",
        json={
            "qr_payload": qr,
            "latitude": _POINT[0],
            "longitude": _POINT[1],
            "scanned_at": now.isoformat(),
            "idempotency_key": str(uuid.uuid4()),
            "route_stop_id": stop_id,
        },
        headers=headers,
    )


def _check_out(
    client: TestClient, headers: dict[str, str], qr: str, stop_id: str, scanned_at: datetime
) -> Response:
    return client.post(
        "/check-outs",
        json={
            "qr_payload": qr,
            "latitude": _POINT[0],
            "longitude": _POINT[1],
            "scanned_at": scanned_at.isoformat(),
            "checkout_idempotency_key": str(uuid.uuid4()),
            "route_stop_id": stop_id,
        },
        headers=headers,
    )


def _serve(
    client: TestClient, headers: dict[str, str], qr: str, stop_id: str, start: datetime
) -> None:
    assert _check_in(client, headers, qr, stop_id, start).status_code == 201
    out = _check_out(client, headers, qr, stop_id, start + timedelta(minutes=2))
    assert out.status_code == 201, out.text


def _alerts(client: TestClient, manager: dict[str, str], kind: AlertKind) -> list[dict[str, Any]]:
    response = client.get("/alerts", headers=manager)
    assert response.status_code == 200, response.text
    return [alert for alert in response.json() if alert["kind"] == kind.value]


async def test_automatic_allocation_day_end_to_end(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # --- seed: the catalog and the manager (the worker side goes through the API) ------------
    db = db_session
    service_type = await make_service_type(db)
    company = service_type.contractor_company
    assert company is not None
    company_id = company.id
    points = await make_points(
        db, service_type=service_type, names=[f"Sala {i}" for i in range(1, 9)], at=_POINT
    )
    # The report counts a point from the campus day it was created; the scenario's day is fixed,
    # so the points must predate it whatever the wall clock says when the test runs.
    for point in points:
        point.created_at = _at(0) - timedelta(days=30)
    qr = sign_qr_payload(
        floor_id=points[0].floor_id,
        version=1,
        private_key_hex=test_settings.qr_signing_private_key_hex,
    )
    db.add(QrCode(floor_id=points[0].floor_id, public_code=qr, secret=b"sig", version=1))
    db.add(
        User(
            name="Larissa",
            email=_MANAGER_EMAIL,
            password_hash=hash_password(_PASSWORD),
            role=UserRole.MANAGER,
        )
    )
    (await get_settings_row(db)).allocation_batch_size = _BATCH
    await seed_privacy_notice(db)
    await db.commit()
    point_name = {point.id: point.name for point in points}

    # --- 08:00 register A and B, the manager approves ---------------------------------------
    _freeze(client, _at(8))
    for name, email in (("Ana", "ana@empresa.com"), ("Bruno", "bruno@empresa.com")):
        registered = client.post(
            "/auth/register",
            json={
                "name": name,
                "email": email,
                "password": _PASSWORD,
                "contractor_company_id": str(company_id),
                "privacy_notice_version": 1,
            },
        )
        assert registered.status_code == 201, registered.text
        assert registered.json() == {"status": "PENDING_APPROVAL"}
    manager = _bearer(client, _MANAGER_EMAIL)
    worker_ids: dict[str, uuid.UUID] = {}
    for name in ("Ana", "Bruno"):
        worker = await db.scalar(select(FieldWorker).where(FieldWorker.full_name == name))
        assert worker is not None
        worker_ids[name] = worker.id
        approved = client.post(f"/field-workers/{worker.id}/approve", headers=manager)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
    ana, bruno = _bearer(client, "ana@empresa.com"), _bearer(client, "bruno@empresa.com")

    # --- "Iniciar rota" at the same building: two disjoint batches of 4 ----------------------
    started_a = _allocate(client, ana, "/routes/start", _at(8))
    started_b = _allocate(client, bruno, "/routes/start", _at(8))
    stops_a, stops_b = _stops_of(started_a, 1), _stops_of(started_b, 1)
    assert (len(stops_a), len(stops_b)) == (_BATCH, _BATCH)
    points_a = {stop["service_point_id"] for stop in stops_a}
    points_b = {stop["service_point_id"] for stop in stops_b}
    assert points_a.isdisjoint(points_b)
    assert started_a.json()["route"]["status"] == "IN_PROGRESS"
    assert started_a.json()["route"]["route_date"] == _DAY.isoformat()
    route_a, route_b = started_a.json()["route"]["id"], started_b.json()["route"]["id"]

    # --- 08:01 A serves one point, leaves a check-in open on another, impedes a third --------
    served, impeded, open_stop, untouched = stops_a
    _freeze(client, _at(8, 1))
    _serve(client, ana, qr, served["id"], _at(8, 1))
    _freeze(client, _at(8, 4))
    assert _check_in(client, ana, qr, open_stop["id"], _at(8, 4)).status_code == 201

    _freeze(client, _at(8, 5))
    reported = client.post(
        f"/routes/{route_a}/impediments",
        json={
            "route_stop_id": impeded["id"],
            "scope": "STOP",
            "reason": "ACCESS_BLOCKED",
            "reported_at": _at(8, 5).isoformat(),
        },
        headers=ana,
    )
    assert reported.status_code == 201, reported.text
    (impeded_alert,) = _alerts(client, manager, AlertKind.POINT_IMPEDED)
    assert impeded_alert["route_id"] == route_a
    assert impeded_alert["route_stop_id"] == impeded["id"]
    assert impeded_alert["status"] == "OPEN"
    day_a = client.get("/routes/me", headers=ana).json()["route"]
    status_by_stop = {stop["id"]: stop["status"] for stop in day_a["stops"]}
    assert status_by_stop[impeded["id"]] == "RELEASED"
    board = client.get("/monitoring/board", headers=manager).json()
    (kpi,) = board["pool"]
    assert (kpi["total"], kpi["done"], kpi["in_progress"], kpi["free"]) == (8, 1, 6, 1)

    # --- B finishes the batch and receives A's impeded point ---------------------------------
    for index, stop in enumerate(stops_b):
        when = _at(8, 6 + 3 * index)
        _freeze(client, when)
        _serve(client, bruno, qr, stop["id"], when)
    _freeze(client, _at(8, 20))
    second = _allocate(client, bruno, f"/routes/{route_b}/next-batch", _at(8, 20))
    batch_two = _stops_of(second, 2)
    assert [stop["service_point_id"] for stop in batch_two] == [impeded["service_point_id"]]
    assert second.json()["pool_empty"] is False

    # --- 08:50 A has been silent for 46 min: the live batch goes back to the pool ------------
    _freeze(client, _at(8, 50))
    alerts = await run_release_scan(db, now=_at(8, 50))
    assert [alert.kind for alert in alerts] == [AlertKind.BATCH_RELEASED]
    assert alerts[0].field_worker_id == worker_ids["Ana"]
    day_a = client.get("/routes/me", headers=ana).json()["route"]
    assert day_a["status"] == "PAUSED"
    released = {
        stop["id"]: stop["release_reason"]
        for stop in day_a["stops"]
        if stop["status"] == "RELEASED"
    }
    assert released == {
        impeded["id"]: "IMPEDIMENT",
        open_stop["id"]: "LOCATION_SILENCE",
        untouched["id"]: "LOCATION_SILENCE",
    }
    # B, still active, keeps their batch.
    assert client.get("/routes/me", headers=bruno).json()["route"]["status"] == "IN_PROGRESS"

    # --- the late check-out: 409 STOP_RELEASED, appealable, nothing lost ----------------------
    _freeze(client, _at(8, 55))
    late = _check_out(client, ana, qr, open_stop["id"], _at(8, 40))
    assert late.status_code == 409, late.text
    assert late.json()["detail"] == {
        "code": "STOP_RELEASED",
        "detail": late.json()["detail"]["detail"],
        "route_stop_id": open_stop["id"],
        "release_reason": "LOCATION_SILENCE",
        "appealable": True,
    }

    # --- A appeals, the manager accepts -------------------------------------------------------
    eligible = client.get("/appeals/eligible", headers=ana).json()
    assert {item["route_stop_id"] for item in eligible} == {open_stop["id"], untouched["id"]}
    filed = client.post(
        "/appeals",
        json={
            "route_stop_id": open_stop["id"],
            "reason": "NO_SIGNAL",
            "note": "Estava no subsolo, sem sinal.",
            "claimed_executed_at": _at(8, 40).isoformat(),
            "idempotency_key": str(uuid.uuid4()),
        },
        headers=ana,
    )
    assert filed.status_code == 201, filed.text
    assert filed.json()["status"] == "PENDING"
    assert len(_alerts(client, manager, AlertKind.APPEAL_FILED)) == 1
    accepted = client.post(f"/appeals/{filed.json()['id']}/accept", json={}, headers=manager)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ACCEPTED"

    # --- 17:00 the day closes: unserved points, one alert, however often the scan runs -------
    # B kept reporting positions while holding the impeded point, so only the clock closes it.
    _freeze(client, _at(16, 59))
    position = client.post(
        "/worker-positions",
        json={
            "route_id": route_b,
            "latitude": _POINT[0],
            "longitude": _POINT[1],
            "reported_at": _at(16, 59).isoformat(),
        },
        headers=bruno,
    )
    assert position.status_code == 201, position.text
    _freeze(client, _at(17))
    first_pass = await run_release_scan(db, now=_at(17))
    second_pass = await run_release_scan(db, now=_at(17, 1))
    assert [alert.kind for alert in first_pass] == [AlertKind.POINTS_UNSERVED]
    assert second_pass == []
    unserved_alerts = (
        await db.scalars(select(Alert).where(Alert.kind == AlertKind.POINTS_UNSERVED))
    ).all()
    assert len(unserved_alerts) == 1
    assert unserved_alerts[0].route_id is None
    assert "2 ponto(s) não atendido(s)" in unserved_alerts[0].description
    assert client.get("/routes/me", headers=bruno).json()["route"]["status"] == "DONE"

    # --- report: one row per point, the appealed one DONE_BY_APPEAL ---------------------------
    db.expire_all()
    rows = await point_day_rows(db, date_from=_DAY, date_to=_DAY)
    outcome_by_point = {point_name[row.service_point_id]: row.outcome for row in rows}
    assert len(rows) == len(outcome_by_point) == 8
    served_names = {point_name[uuid.UUID(sid)] for sid in points_b} | {
        point_name[uuid.UUID(served["service_point_id"])]
    }
    appealed_name = point_name[uuid.UUID(open_stop["service_point_id"])]
    unserved_names = {
        point_name[uuid.UUID(impeded["service_point_id"])],
        point_name[uuid.UUID(untouched["service_point_id"])],
    }
    assert {n for n, o in outcome_by_point.items() if o == "DONE"} == served_names
    assert {n for n, o in outcome_by_point.items() if o == "DONE_BY_APPEAL"} == {appealed_name}
    assert {n for n, o in outcome_by_point.items() if o == "UNSERVED"} == unserved_names

    report = client.get(
        "/reports/compliance",
        params={"granularity": "DAILY", "date_from": _DAY.isoformat(), "date_to": _DAY.isoformat()},
        headers=manager,
    )
    assert report.status_code == 200, report.text
    totals = report.json()["totals"]
    assert totals["points_total"] == 8
    assert totals["points_done"] == 6  # includes the one done by appeal
    assert totals["points_done_by_appeal"] == 1
    assert totals["points_unserved"] == 2
    assert totals["points_impeded"] == 1
    assert totals["released_location_silence"] == 2
    assert totals["released_end_of_day"] >= 1
    assert totals["completion_rate"] == pytest.approx(6 / 8)
    assert [p["period_start"] for p in report.json()["periods"]] == [_DAY.isoformat()]
    only_unserved = client.get(
        "/reports/compliance",
        params={
            "granularity": "DAILY",
            "date_from": _DAY.isoformat(),
            "date_to": _DAY.isoformat(),
            "outcome": "UNSERVED",
        },
        headers=manager,
    )
    assert only_unserved.json()["totals"]["points_total"] == 2
