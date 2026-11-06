"""Fixed data the manual-test seed loads (no network at seed time).

`tambor_buildings.json` holds real OpenStreetMap building outlines around the author's address
in Tambor, Campina Grande; `app/scripts/fetch_tambor_buildings.py` documents and reproduces it.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIXTURE_PATH = Path(__file__).resolve().parent / "tambor_buildings.json"


@dataclass(frozen=True)
class LatLng:
    lat: float
    lng: float


@dataclass(frozen=True)
class BoundingBox:
    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True)
class TamborBuilding:
    name: str
    osm_way_id: int
    footprint: dict[str, Any]  # GeoJSON Polygon, [lng, lat], closed ring
    rooms: tuple[str, ...]


@dataclass(frozen=True)
class TamborFixture:
    center: LatLng
    center_note: str
    bbox: BoundingBox
    buildings: tuple[TamborBuilding, ...]


def load_tambor_buildings(path: Path = _FIXTURE_PATH) -> TamborFixture:
    """Reads the committed fixture: centre, campus bbox and the buildings with their rooms.

    >>> fixture = load_tambor_buildings()
    >>> len(fixture.buildings) >= 5
    True
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TamborFixture(
        center=LatLng(**raw["center"]),
        center_note=raw.get("center_note", ""),
        bbox=BoundingBox(**raw["bbox"]),
        buildings=tuple(
            TamborBuilding(
                name=b["name"],
                osm_way_id=b["osm_way_id"],
                footprint=b["footprint"],
                rooms=tuple(b["rooms"]),
            )
            for b in raw["buildings"]
        ),
    )
