import pytest
from app.domain.execution.geo import haversine_meters


def test_same_point_is_zero() -> None:
    assert haversine_meters(-7.21, -35.90, -7.21, -35.90) == pytest.approx(0.0, abs=1e-6)


def test_known_short_distance_within_one_percent() -> None:
    # ~111.32 m per 0.001 deg latitude near the equator
    d = haversine_meters(-7.2100, -35.9000, -7.2110, -35.9000)
    assert d == pytest.approx(111.3, rel=0.01)


def test_is_symmetric() -> None:
    a = haversine_meters(-7.21, -35.90, -7.22, -35.91)
    b = haversine_meters(-7.22, -35.91, -7.21, -35.90)
    assert a == pytest.approx(b)
