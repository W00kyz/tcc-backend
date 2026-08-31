import pytest
from app.domain.routing.osrm import OsrmLeg, OsrmUnavailable

from tests.support.osrm import FakeOsrmClient


async def test_fake_route_legs_returns_one_leg_per_gap() -> None:
    fake = FakeOsrmClient(
        legs=[OsrmLeg(distance_m=12.0, duration_s=9.0, geometry=[[-35.91, -7.21], [-35.90, -7.21]])]
    )
    legs = await fake.route_legs([(-35.91, -7.21), (-35.90, -7.21)])
    assert len(legs) == 1
    assert legs[0].distance_m == 12.0
    assert fake.calls == [("route_legs", [(-35.91, -7.21), (-35.90, -7.21)])]


async def test_fake_optimize_order_defaults_to_identity() -> None:
    fake = FakeOsrmClient()
    assert await fake.optimize_order([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]) == [0, 1, 2]


async def test_fake_raises_when_unavailable() -> None:
    fake = FakeOsrmClient(unavailable=True)
    with pytest.raises(OsrmUnavailable):
        await fake.route_legs([(0.0, 0.0), (1.0, 1.0)])
