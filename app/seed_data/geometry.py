"""Small planar geometry helpers for the manual-test seed: where to put service points inside a
building outline and how far to pad the campus rectangle. Building-sized distances only, so a
flat lat/lng plane is accurate enough."""

import math
from collections.abc import Sequence

from app.seed_data import BoundingBox

_METERS_PER_DEGREE_LAT = 111_320.0
# How far past the fixture bbox the test campus reaches: the street of the author's house lies
# just outside the raw bbox, and starting a route from home must not be refused OUTSIDE_CAMPUS.
CAMPUS_PADDING_METERS = 500.0
_GRID_STEPS = 15


def point_in_ring(lat: float, lng: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray casting over a GeoJSON ring (`[lng, lat]` pairs, closed or not).

    >>> point_in_ring(1.0, 1.0, [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]])
    True
    """
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]], strict=True):
        if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def pad_bbox(box: BoundingBox, meters: float = CAMPUS_PADDING_METERS) -> BoundingBox:
    """`box` grown by `meters` on every side (longitude degrees shrink with the latitude).

    >>> pad_bbox(BoundingBox(-7.0, -35.0, -6.9, -34.9), 111.32).south
    -7.001
    """
    dlat = meters / _METERS_PER_DEGREE_LAT
    dlng = dlat / math.cos(math.radians((box.south + box.north) / 2))
    return BoundingBox(
        south=box.south - dlat, west=box.west - dlng, north=box.north + dlat, east=box.east + dlng
    )


def interior_points(ring: Sequence[Sequence[float]], count: int) -> list[tuple[float, float]]:
    """`count` distinct `(lat, lng)` points strictly inside `ring`, spread apart: the first is the
    interior grid point nearest the ring's bounding-box centre, each next one the farthest from
    those already chosen. Deterministic.

    >>> interior_points([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]], 2)[0]
    (1.0, 1.0)
    """
    candidates = _grid_inside(ring)
    if len(candidates) < count:
        raise ValueError(f"Outline has room for {len(candidates)} points; {count} requested.")
    centre = _box_centre(ring)
    chosen = [min(candidates, key=lambda p: _distance(p, centre))]
    while len(chosen) < count:
        chosen.append(max(candidates, key=lambda p: min(_distance(p, c) for c in chosen)))
    return chosen


def _grid_inside(ring: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    lngs = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    points = [
        (
            min(lats) + (max(lats) - min(lats)) * (row + 0.5) / _GRID_STEPS,
            min(lngs) + (max(lngs) - min(lngs)) * (col + 0.5) / _GRID_STEPS,
        )
        for row in range(_GRID_STEPS)
        for col in range(_GRID_STEPS)
    ]
    return [p for p in points if point_in_ring(p[0], p[1], ring)]


def _box_centre(ring: Sequence[Sequence[float]]) -> tuple[float, float]:
    lngs = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return ((min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
