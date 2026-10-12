"""The building outline the dashboard draws: a GeoJSON Polygon with one closed ring
(spec 2026-10-11). Validated here so the JSONB column never holds a shape the map cannot draw."""

from typing import Literal

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
