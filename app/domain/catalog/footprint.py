"""The building outline the dashboard draws: a GeoJSON Polygon with one closed ring
(spec 2026-10-11). Validated here so the JSONB column never holds a shape the map cannot draw."""

from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, field_validator

MIN_POSITIONS = 4  # a triangle plus the repeated closing position
MAX_POSITIONS = 500

Position = list[float]


def _check_position(position: Position) -> None:
    if len(position) != 2:
        raise ValueError(f"position {position} must be [longitude, latitude].")
    longitude, latitude = position
    if not -180 <= longitude <= 180:
        raise ValueError(f"longitude {longitude} out of range; expected -180..180.")
    if not -90 <= latitude <= 90:
        raise ValueError(f"latitude {latitude} out of range; expected -90..90.")


def _check_ring(ring: list[Position]) -> None:
    if len(ring) < MIN_POSITIONS:
        raise ValueError(f"ring has {len(ring)} positions; expected at least 4.")
    if len(ring) > MAX_POSITIONS:
        raise ValueError(f"ring has {len(ring)} positions; expected at most 500.")
    if ring[0] != ring[-1]:
        raise ValueError(f"ring starts at {ring[0]} and ends at {ring[-1]}; it must be closed.")
    for position in ring:
        _check_position(position)


class PolygonFootprint(BaseModel):
    type: Literal["Polygon"]
    coordinates: list[list[Position]]

    @field_validator("coordinates")
    @classmethod
    def _single_closed_ring(cls, rings: list[list[Position]]) -> list[list[Position]]:
        if len(rings) != 1:
            raise ValueError(f"polygon has {len(rings)} rings; expected exactly one ring.")
        _check_ring(rings[0])
        return rings


def _ring(footprint: dict[str, Any]) -> list[Position]:
    rings: list[list[Position]] = footprint["coordinates"]
    return rings[0]


def outline_centroid(footprint: dict[str, Any]) -> tuple[float, float]:
    """Area-weighted centre (lon, lat) of the outline — where the building's points are.

    Planar on lon/lat: a campus building is a few hundred metres at most, far below where the
    earth's curvature would move the result. Falls back to the vertex mean for a degenerate
    (zero-area) ring.

    >>> square = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
    >>> outline_centroid({"type": "Polygon", "coordinates": [square]})
    (1.0, 1.0)
    """
    ring = _ring(footprint)
    twice_area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in pairwise(ring):
        cross = x0 * y1 - x1 * y0
        twice_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if twice_area == 0:
        corners = ring[:-1]
        return (
            sum(x for x, _ in corners) / len(corners),
            sum(y for _, y in corners) / len(corners),
        )
    return cx / (3 * twice_area), cy / (3 * twice_area)


def outline_contains(footprint: dict[str, Any], longitude: float, latitude: float) -> bool:
    """Ray casting: whether (longitude, latitude) falls inside the outline.

    >>> triangle = [[0, 0], [2, 0], [2, 2], [0, 0]]
    >>> outline_contains({"type": "Polygon", "coordinates": [triangle]}, 1.5, 0.5)
    True
    """
    ring = _ring(footprint)
    inside = False
    for (x0, y0), (x1, y1) in pairwise(ring):
        crosses = (y0 > latitude) != (y1 > latitude)
        if crosses and longitude < (x1 - x0) * (latitude - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside
