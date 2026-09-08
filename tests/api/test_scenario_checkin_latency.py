"""RNF04's 5 s budget includes 4G round-trip + on-device GPS capture, measured in the field by the author; this asserts only the server-side floor (spec Ruling 10)."""  # noqa: E501

import math
import time

import pytest
from app.core.config import Settings
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_checkin_timing import _seed_many_floor_stops
from tests.api.test_checkins import _login, _post

# Mirror test_checkin_timing.py: one warm-up (pool, statement cache, PostGIS planner)
# then the 20 the assertion is about.
_WARMUP = 1
_MEASURED = 20
_P95_BUDGET_S = 0.5


@pytest.mark.slow
async def test_scenario_check_in_latency_p95_under_500ms(
    client: TestClient, db_session: AsyncSession, test_settings: Settings
) -> None:
    payloads = await _seed_many_floor_stops(db_session, test_settings, _WARMUP + _MEASURED)
    token = _login(client)

    for warmup_payload in payloads[:_WARMUP]:
        assert _post(client, token, warmup_payload).status_code == 201

    samples: list[float] = []
    for payload in payloads[_WARMUP:]:
        t0 = time.perf_counter()
        response = _post(client, token, payload)
        elapsed = time.perf_counter() - t0
        assert response.status_code == 201, response.text
        samples.append(elapsed)

    ordered = sorted(samples)
    p50 = ordered[len(ordered) // 2]
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]

    # Printed so `pytest -m slow -s` surfaces the numbers for the pilot checklist.
    print(
        f"RNF04 server-side check-in latency: p50={p50 * 1000:.1f}ms p95={p95 * 1000:.1f}ms (n=20)"
    )

    assert p95 < _P95_BUDGET_S, (
        f"server-side check-in p95 was {p95 * 1000:.1f}ms, over the 500ms budget (RNF04 floor). "
        f"Sorted samples (ms): {[round(value * 1000, 1) for value in ordered]}"
    )
