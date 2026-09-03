"""Server-timing regression for `POST /check-ins` (spec §10 open point 1, RNF07).

The end-to-end "< 5 s on 4G" budget stays a manual author step — network and device are
out of scope for pytest. What this pins is the *server path*: QR signature verify, the five
anti-fraud layers, room resolution and the execution/scan write, measured in-process against
the Postgres testcontainer. A regression there (an N+1 query, a missing index, a synchronous
call sneaking onto the request path) shows up as p95 latency creeping toward the budget.

Marked `slow` and excluded from the default run (`-m "not slow"` in pyproject `addopts`);
select it with `uv run pytest -m slow`.
"""

import time
import uuid
from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.domain.catalog.models import Floor, ServicePoint
from app.domain.qr.crypto import sign_qr_payload
from app.domain.qr.models import QrCode
from app.domain.routing.models import RouteStop
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkins import _POINT_LAT, _POINT_LNG, _login, _seed_route

# One warm-up call (connection pool, SQLAlchemy statement cache, PostGIS planner) then the
# 20 the assertion is about. p95 of 20 samples is the 19th value in ascending order
# (nearest-rank: ceil(0.95 * 20) = 19 -> index 18).
_WARMUP = 1
_MEASURED = 20
_P95_BUDGET_MS = 800.0


async def _seed_many_floor_stops(
    db_session: AsyncSession, test_settings: Settings, count: int
) -> list[str]:
    """A started route whose PENDING stops are one-per-floor, each floor with its own signed
    QR and a service point at the GPS origin. Returns the signed QR payloads, one per stop —
    a clean single-candidate check-in each, so every timed request exercises the full 201
    path rather than the ambiguity branch."""
    seeded = await _seed_route(db_session, test_settings)
    payloads = [seeded.qr_code.public_code]

    for index in range(2, count + 1):
        floor = Floor(building_id=seeded.floor.building_id, label=f"Andar {index}")
        db_session.add(floor)
        await db_session.flush()
        point = ServicePoint(
            floor_id=floor.id,
            name=f"Sala {index}",
            description="Sala",
            latitude=_POINT_LAT,
            longitude=_POINT_LNG,
        )
        db_session.add(point)
        await db_session.flush()
        payload = sign_qr_payload(
            floor_id=floor.id, version=1, private_key_hex=test_settings.qr_signing_private_key_hex
        )
        db_session.add(QrCode(floor_id=floor.id, public_code=payload, secret=b"sig", version=1))
        db_session.add(
            RouteStop(route_id=seeded.route.id, service_point_id=point.id, order_index=index)
        )
        payloads.append(payload)

    await db_session.commit()
    return payloads


def _check_in(client: TestClient, token: str, qr_payload: str) -> float:
    """Fire one check-in at the GPS origin and return the round-trip time in milliseconds.
    Asserts 201 so a regression that also breaks correctness fails loudly, not silently slow."""
    body = {
        "qr_payload": qr_payload,
        "latitude": _POINT_LAT,
        "longitude": _POINT_LNG,
        "scanned_at": datetime.now(UTC).isoformat(),
        "idempotency_key": str(uuid.uuid4()),
    }
    start = time.perf_counter()
    response = client.post("/check-ins", json=body, headers={"Authorization": f"Bearer {token}"})
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert response.status_code == 201, response.text
    return elapsed_ms


@pytest.mark.slow
async def test_check_in_server_path_p95_under_budget(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    payloads = await _seed_many_floor_stops(db_session, test_settings, _WARMUP + _MEASURED)
    token = _login(client)

    for warmup_payload in payloads[:_WARMUP]:
        _check_in(client, token, warmup_payload)

    timings_ms = sorted(_check_in(client, token, payload) for payload in payloads[_WARMUP:])

    # nearest-rank p95 of 20 samples -> ceil(0.95 * 20) = 19th value -> index 18
    p95_ms = timings_ms[18]
    # Printed so the number (not just pass/fail) shows up when this is run locally with `-s`
    # — a p95 drifting toward the budget is the early warning. Pytest swallows it on a pass
    # otherwise, CI's `-m slow` run included; the assertion message carries it on a failure.
    print(
        f"\nPOST /check-ins server-path timings (ms): "
        f"min={timings_ms[0]:.1f} median={timings_ms[len(timings_ms) // 2]:.1f} "
        f"p95={p95_ms:.1f} max={timings_ms[-1]:.1f}"
    )
    assert p95_ms < _P95_BUDGET_MS, (
        f"POST /check-ins server-path p95 was {p95_ms:.1f} ms, over the {_P95_BUDGET_MS:.0f} ms "
        f"budget (spec §10 open point 1). Sorted samples (ms): "
        f"{[round(value, 1) for value in timings_ms]}"
    )
