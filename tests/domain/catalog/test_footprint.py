import pytest
from app.domain.catalog.footprint import PolygonFootprint
from pydantic import ValidationError

SQUARE = [[-35.909, -7.214], [-35.908, -7.214], [-35.908, -7.215], [-35.909, -7.214]]


def test_accepts_a_closed_single_ring_polygon() -> None:
    footprint = PolygonFootprint(type="Polygon", coordinates=[SQUARE])
    assert footprint.model_dump() == {"type": "Polygon", "coordinates": [SQUARE]}


@pytest.mark.parametrize(
    ("coordinates", "fragment"),
    [
        ([[*SQUARE[:-1], [-35.9, -7.2]]], "closed"),
        ([SQUARE[:3]], "at least 4"),
        ([SQUARE * 130], "at most 500"),
        ([SQUARE, SQUARE], "one ring"),
        ([[[-200.0, -7.2], [-35.9, -7.2], [-35.9, -7.3], [-200.0, -7.2]]], "longitude"),
        ([[[-35.9, -95.0], [-35.8, -7.2], [-35.8, -7.3], [-35.9, -95.0]]], "latitude"),
    ],
)
def test_rejects_malformed_rings(coordinates: list[list[list[float]]], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        PolygonFootprint(type="Polygon", coordinates=coordinates)


def test_rejects_other_geometry_types() -> None:
    with pytest.raises(ValidationError):
        PolygonFootprint(type="MultiPolygon", coordinates=[SQUARE])
