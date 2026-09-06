"""Cenário 4 ponta a ponta — substituição emergencial e reatribuição (spec §7, design §7).

The etapa deliverable: one worker hits a wall on a stop, the manager sees it in the queue,
asks PostGIS for ranked candidates, reassigns the stop to a second worker, and that worker
actually executes it against the real stack — every hop a real HTTP request against a real
Postgres/PostGIS testcontainer, no fakes on the data path.

What stays out of scope for pytest (manual author steps, same treatment as
`tests/api/test_checkin_timing.py`):
- the mobile client and the "< 5 s on 4G" budget — network and device are not pytest's job;
- OSRM leg geometry — an external routing service; every test in this suite injects
  `FakeOsrmClient` at the network boundary and this one is no different. Everything the
  scenario asserts (impediment queue, candidate ranking, the stop move, the assignment
  chain, check-in/check-out) runs against the real database.

Marked `slow` and excluded from the default run (`-m "not slow"` in pyproject `addopts`);
select it with `uv run pytest -m slow`.

Seeding note: the affected stops carry no `service_type_id`, so candidate ranking only
requires the substitute to have a route on the same date with a linked user account — the
service-type qualification filter (covered in tests/domain/impediments/) is exercised
elsewhere.
"""

import math
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.domain.catalog.models import ServicePoint
from app.domain.impediments.models import Impediment, ImpedimentStatus
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode
from app.domain.routing.models import RouteStop, RouteStopStatus
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_impediments import _auth, _login, _seed
from tests.support.object_store import FakeObjectStore
from tests.support.osrm import FakeOsrmClient


@pytest.fixture
def client(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[TestClient]:
    # Same OSRM boundary as tests/api/test_impediments.py — the reassignment path recomputes
    # legs, and no test in this suite touches the real routing service.
    app = create_app(
        settings=test_settings,
        mailer=recording_mailer,
        osrm_client=FakeOsrmClient(),
        object_store=FakeObjectStore(),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance on a sphere of radius 6371 km — the same model PostGIS
    `ST_DistanceSphere` uses, so the API's `distance_m` should land within a percent of this."""
    radius_m = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_m * math.asin(math.sqrt(a))


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.slow
async def test_emergency_reassignment_end_to_end(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    # --- Step 1: the day's board -----------------------------------------------------------
    # manager "Larissa"; worker A "Joao da Silva" with an IN_PROGRESS route of 2 stops;
    # worker B "Maria Souza" with a PLANNED route of 1 stop, same date; every service point
    # at real distinct coordinates.
    fx = await _seed(db_session, source_points=2, target_points=1)
    a_route_id = str(fx.source.id)
    b_route_id = str(fx.target.id)
    # Snapshot the seeded identifiers now: a later `db_session.expire_all()` would turn any
    # `fx.*` ORM access into a sync lazy load and raise MissingGreenlet.
    worker_b_id = str(fx.worker_b.id)
    worker_a_name = fx.worker_a.full_name
    worker_b_name = fx.worker_b.full_name

    manager_token = _login(client, fx.manager.email)
    worker_a_token = _login(client, fx.worker_a_user.email)
    worker_b_token = _login(client, fx.worker_b_user.email)

    a_route = client.get(f"/routes/{a_route_id}", headers=_auth(manager_token)).json()
    assert a_route["status"] == "IN_PROGRESS"
    assert len(a_route["stops"]) == 2
    affected = a_route["stops"][0]
    affected_stop_id = affected["id"]
    affected_lat, affected_lng = affected["latitude"], affected["longitude"]

    b_route_before = client.get(f"/routes/{b_route_id}", headers=_auth(manager_token)).json()
    assert [s["id"] for s in b_route_before["stops"]] != []
    b_original_point = b_route_before["stops"][0]

    # --- Step 2: worker A signals the impediment -----------------------------------------
    report = client.post(
        f"/routes/{a_route_id}/impediments",
        json={
            "route_stop_id": affected_stop_id,
            "scope": "STOP",
            "reason": "ACCESS_BLOCKED",
            "note": "Sala interditada pela manutencao",
            "reported_at": _now(),
        },
        headers=_auth(worker_a_token),
    )
    assert report.status_code == 201, report.text
    impediment_id = report.json()["id"]
    assert report.json()["status"] == "PENDING"

    # --- Step 3: the impediment shows up in the manager's queue --------------------------
    queue = client.get("/impediments?status=PENDING", headers=_auth(manager_token))
    assert queue.status_code == 200
    assert impediment_id in [row["id"] for row in queue.json()]

    # --- Step 4: ranked candidates with a REAL PostGIS distance -------------------------
    candidates = client.get(
        f"/impediments/{impediment_id}/candidates", headers=_auth(manager_token)
    )
    assert candidates.status_code == 200
    ranked = candidates.json()
    worker_b_row = next(row for row in ranked if row["field_worker_id"] == worker_b_id)
    api_distance_m = worker_b_row["distance_m"]
    expected_m = _haversine_m(
        affected_lat, affected_lng, b_original_point["latitude"], b_original_point["longitude"]
    )
    # Real coordinates ~1.5 km apart: not zero, not mocked, and within a percent of the
    # independently computed great-circle distance (PostGIS uses the same spherical model).
    assert api_distance_m > 100.0
    assert abs(api_distance_m - expected_m) / expected_m < 0.02, (
        f"candidate distance {api_distance_m:.1f} m is not the great-circle distance "
        f"{expected_m:.1f} m between the seeded points"
    )

    # --- Step 5: the manager reassigns --------------------------------------------------
    reassign = client.post(
        f"/impediments/{impediment_id}/reassign",
        json={"field_worker_id": worker_b_id, "reason": "Substituicao emergencial"},
        headers=_auth(manager_token),
    )
    assert reassign.status_code == 200, reassign.text
    assert reassign.json()["status"] == "RESOLVED"

    # --- Step 6: the stop left worker A's route, remainder reindexed --------------------
    a_route_after = client.get(f"/routes/{a_route_id}", headers=_auth(manager_token)).json()
    assert affected_stop_id not in [s["id"] for s in a_route_after["stops"]]
    assert [s["order_index"] for s in a_route_after["stops"]] == list(
        range(1, len(a_route_after["stops"]) + 1)
    )

    # --- Step 7: the stop landed on worker B's route with the assignment chain ----------
    b_route_after = client.get(f"/routes/{b_route_id}", headers=_auth(manager_token)).json()
    moved = next(s for s in b_route_after["stops"] if s["id"] == affected_stop_id)
    assert moved["reassigned"] is True
    history = moved["assignment_history"]
    assert history is not None
    assert [row["sequence"] for row in history] == [1, 2]
    assert history[0]["full_name"] == worker_a_name
    assert history[0]["outcome"] == "REASSIGNED"
    assert history[1]["full_name"] == worker_b_name
    assert history[1]["sequence"] == 2

    # --- Step 8: worker B actually executes the reassigned stop ------------------------
    moved_point = await db_session.get(ServicePoint, uuid.UUID(moved["service_point_id"]))
    assert moved_point is not None
    qr_payload = sign_qr_payload(
        floor_id=moved_point.floor_id,
        version=1,
        private_key_hex=test_settings.qr_signing_private_key_hex,
    )
    db_session.add(
        QrCode(floor_id=moved_point.floor_id, public_code=qr_payload, secret=b"sig", version=1)
    )
    await db_session.commit()

    started = client.post(
        f"/routes/{b_route_id}/start",
        json={"latitude": affected_lat, "longitude": affected_lng, "started_at": _now()},
        headers=_auth(worker_b_token),
    )
    assert started.status_code == 200, started.text

    check_in = client.post(
        "/check-ins",
        json={
            "route_stop_id": affected_stop_id,
            "qr_payload": qr_payload,
            "latitude": affected_lat,
            "longitude": affected_lng,
            "scanned_at": _now(),
            "idempotency_key": "33333333-3333-3333-3333-333333333333",
        },
        headers=_auth(worker_b_token),
    )
    assert check_in.status_code == 201, check_in.text
    execution_id = check_in.json()["execution_id"]

    check_out = client.post(
        "/check-outs",
        json={
            "route_stop_id": affected_stop_id,
            "qr_payload": qr_payload,
            "latitude": affected_lat,
            "longitude": affected_lng,
            "scanned_at": _now(),
            "checkout_idempotency_key": "44444444-4444-4444-4444-444444444444",
        },
        headers=_auth(worker_b_token),
    )
    assert check_out.status_code == 201, check_out.text

    db_session.expire_all()
    moved_row = await db_session.get(RouteStop, uuid.UUID(affected_stop_id))
    assert moved_row is not None
    assert moved_row.status is RouteStopStatus.DONE

    # --- Step 9: the history final state — check-out auto-sets the last row EXECUTED ----
    # `check_out` -> `_mark_assignment_executed` (app/domain/execution/service.py) stamps the
    # top-of-chain assignment EXECUTED, so the sequence-2 row flips REASSIGNED path -> EXECUTED.
    execution = client.get(f"/executions/{execution_id}", headers=_auth(manager_token))
    assert execution.status_code == 200, execution.text
    detail = execution.json()
    assert detail["field_worker_id"] == worker_b_id
    assert detail["field_worker_name"] == worker_b_name
    assert detail["reassigned"] is True
    exec_history = detail["assignment_history"]
    assert [row["sequence"] for row in exec_history] == [1, 2]
    assert exec_history[0]["outcome"] == "REASSIGNED"
    assert exec_history[1]["outcome"] == "EXECUTED"

    # And the same through the route read model.
    b_route_done = client.get(f"/routes/{b_route_id}", headers=_auth(manager_token)).json()
    done_stop = next(s for s in b_route_done["stops"] if s["id"] == affected_stop_id)
    assert done_stop["status"] == "DONE"
    assert done_stop["assignment_history"][1]["outcome"] == "EXECUTED"

    # Impediment is closed for good — gone from the PENDING queue, RESOLVED in the row.
    queue_after = client.get("/impediments?status=PENDING", headers=_auth(manager_token))
    assert impediment_id not in [row["id"] for row in queue_after.json()]

    db_session.expire_all()
    imp_row = await db_session.get(Impediment, uuid.UUID(impediment_id))
    assert imp_row is not None
    assert imp_row.status is ImpedimentStatus.RESOLVED
