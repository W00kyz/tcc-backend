"""Rebuilds app/seed_data/tambor_buildings.json from OpenStreetMap (one-off, needs network).

Usage: uv run python -m app.scripts.fetch_tambor_buildings

The manual-test database is centred on the author's address (Rua Raimundo da Silva Ribeiro,
359, Tambor, Campina Grande, CEP 58414-675). OSM does not map house 359, so the centre is the
building nearest the street's midpoint and the JSON records that in `center_note`. Buildings
within 250 m of the centre (widening to 350 and then 500 m when fewer than 5 qualify) with a
closed outline and 40..800 m2 are kept (residential tags first), each gets 1..3 rooms by area,
and the bbox is their extent plus 150 m of margin.

The committed JSON is the source of truth: the seed reads it and never touches the network, and
the tests do not run this script. Re-run it only to refresh the data, then review the diff.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

# OSM etiquette: a descriptive User-Agent, and Overpass answers 406 without an Accept header.
HEADERS = {"User-Agent": "tcc-ufcg-seed/1.0 (academic project)", "Accept": "*/*"}
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
STREET_NAME = "Rua Raimundo da Silva Ribeiro"
STREET_WAY_ID = 222772678
CITY = "Campina Grande"
HOUSE_NUMBER = "359"
STREET_LAT, STREET_LNG = -7.2567, -35.8900  # Nominatim's point for the street
# OSM coverage around the address is sparse (one qualifying building within 250 m, found on
# 2026-11-05), so the radius widens step by step until 5 buildings qualify.
SEARCH_RADII_M = (250, 350, 500)
BBOX_MARGIN_M = 150
MIN_AREA_M2, MAX_AREA_M2 = 40, 800
MIN_BUILDINGS, MAX_BUILDINGS = 5, 8
PREFERRED_TAGS = {"house", "residential", "apartments", "detached"}
_OUTPUT_PATH = Path(__file__).resolve().parents[1] / "seed_data" / "tambor_buildings.json"
_METERS_PER_DEGREE = 111_320.0

Fetcher = Callable[[str, dict[str, str]], Any]
Point = tuple[float, float]  # (lng, lat)


def room_count_for_area(area_m2: float) -> int:
    """Rooms a building gets from its footprint area: > 150 m2 -> 3, 80..150 -> 2, else 1.

    >>> [room_count_for_area(a) for a in (60, 100, 200)]
    [1, 2, 3]
    """
    if area_m2 > 150:
        return 3
    return 2 if area_m2 >= 80 else 1


def _to_meters(point: Point, origin: Point) -> Point:
    scale = math.cos(math.radians(origin[1]))
    return (
        (point[0] - origin[0]) * _METERS_PER_DEGREE * scale,
        (point[1] - origin[1]) * _METERS_PER_DEGREE,
    )


def distance_m(a: Point, b: Point) -> float:
    """Planar distance in metres between two (lng, lat) points; fine at a few hundred metres.

    >>> round(distance_m((0.0, 0.0), (0.0, 0.001)))
    111
    """
    x, y = _to_meters(a, b)
    return math.hypot(x, y)


def ring_area_m2(ring: list[Point]) -> float:
    """Shoelace area in m2 of a closed [lng, lat] ring, projected around its first vertex.

    >>> round(ring_area_m2([(0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001), (0, 0)]))
    12392
    """
    points = [_to_meters(p, ring[0]) for p in ring]
    twice = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in pairwise(points))
    return abs(twice) / 2


def ring_centroid(ring: list[Point]) -> Point:
    """Vertex mean of the ring without its closing position (enough to rank by distance)."""
    corners = ring[:-1]
    return (sum(p[0] for p in corners) / len(corners), sum(p[1] for p in corners) / len(corners))


def _http_get_json(url: str, params: dict[str, str]) -> Any:  # noqa: ANN401  (Nominatim and Overpass reply with different JSON shapes)
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=HEADERS)
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # The public Overpass instance answers 502/504 when busy; back off and retry.
            if error.code not in (429, 502, 503, 504) or attempt == 3:
                raise
            time.sleep(10 * attempt)
    raise AssertionError("unreachable")


def street_midpoint(geometry: list[dict[str, float]]) -> Point:
    """The point halfway along the street polyline, by length.

    >>> street_midpoint([{"lon": 0.0, "lat": 0.0}, {"lon": 0.0, "lat": 0.002}])
    (0.0, 0.001)
    """
    path = [(g["lon"], g["lat"]) for g in geometry]
    half = sum(distance_m(a, b) for a, b in pairwise(path)) / 2
    for a, b in pairwise(path):
        leg = distance_m(a, b)
        if half <= leg:
            t = half / leg
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        half -= leg
    return path[-1]


def _overpass(fetch: Fetcher, query: str) -> list[dict[str, Any]]:
    time.sleep(1)  # at most one request per second to the public instances
    elements: list[dict[str, Any]] = fetch(OVERPASS_URL, {"data": query})["elements"]
    return elements


def house_number_found(fetch: Fetcher) -> bool:
    """Whether OSM maps house 359 on this street (Nominatim first, then address tags)."""
    params = {"street": f"{HOUSE_NUMBER} {STREET_NAME}", "city": CITY, "format": "jsonv2"}
    hits = fetch(NOMINATIM_URL, params)
    if any(hit.get("category") != "highway" for hit in hits):  # a bare street hit is no house
        return True
    around = f"around:{SEARCH_RADII_M[-1] * 2},{STREET_LAT},{STREET_LNG}"
    query = f'[out:json][timeout:60];nwr["addr:housenumber"="{HOUSE_NUMBER}"]({around});out center;'
    return bool(_overpass(fetch, query))


def _closed_ring(element: dict[str, Any]) -> list[Point] | None:
    ring = [(g["lon"], g["lat"]) for g in element.get("geometry", [])]
    return ring if len(ring) >= 4 and ring[0] == ring[-1] else None


def _counter_clockwise(ring: list[Point]) -> list[Point]:
    twice = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in pairwise(ring))
    return ring if twice >= 0 else ring[::-1]


def collect_candidates(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closed building ways with 40..800 m2, as {way_id, tag, ring, area, centroid}."""
    found: list[dict[str, Any]] = []
    for element in elements:
        ring = _closed_ring(element)
        if element.get("type") != "way" or ring is None:
            continue
        area = ring_area_m2(ring)
        if MIN_AREA_M2 <= area <= MAX_AREA_M2:
            tag = element["tags"]["building"]
            found.append(
                {
                    "way_id": element["id"],
                    "tag": tag,
                    "ring": _counter_clockwise(ring),
                    "area": area,
                    "centroid": ring_centroid(ring),
                }
            )
    return found


def pick_buildings(
    candidates: list[dict[str, Any]], center: Point, radius_m: float
) -> list[dict[str, Any]]:
    """Nearest candidates within the radius, residential tags first, `yes` only to reach 5."""
    near = [c for c in candidates if distance_m(c["centroid"], center) <= radius_m]
    near.sort(key=lambda c: distance_m(c["centroid"], center))
    chosen = [c for c in near if c["tag"] in PREFERRED_TAGS][:MAX_BUILDINGS]
    fallback = [c for c in near if c["tag"] not in PREFERRED_TAGS]
    return chosen + fallback[: max(0, MIN_BUILDINGS - len(chosen))]


def pick_with_widening_radius(
    candidates: list[dict[str, Any]], center: Point
) -> tuple[list[dict[str, Any]], int]:
    """The first radius in SEARCH_RADII_M that yields 5 buildings, with the picked buildings."""
    for radius in SEARCH_RADII_M:
        chosen = pick_buildings(candidates, center, radius)
        if len(chosen) >= MIN_BUILDINGS:
            return chosen, radius
    raise SystemExit(
        f"Only {len(chosen)} buildings within {SEARCH_RADII_M[-1]} m of {center}; "
        f"expected at least {MIN_BUILDINGS}."
    )


def build_fixture(
    chosen: list[dict[str, Any]], center: Point, note: str, radius_m: int
) -> dict[str, Any]:
    """The JSON document: centre, bbox (extent + margin), and named buildings with rooms."""
    seen = {"Casa": 0, "Prédio": 0}
    buildings = []
    for item in chosen:
        kind = "Prédio" if item["tag"] == "apartments" else "Casa"
        seen[kind] += 1
        rooms = [f"Sala {i}" for i in range(1, room_count_for_area(item["area"]) + 1)]
        buildings.append(
            {
                "name": f"{kind} {seen[kind]}",
                "osm_way_id": item["way_id"],
                "building_tag": item["tag"],
                "area_m2": round(item["area"], 1),
                "footprint": {"type": "Polygon", "coordinates": [[list(p) for p in item["ring"]]]},
                "rooms": rooms,
            }
        )
    return {
        "center": {"lat": center[1], "lng": center[0]},
        "center_note": note,
        "search_radius_m": radius_m,
        "bbox": bbox_with_margin([p for c in chosen for p in c["ring"]] + [center]),
        "buildings": buildings,
    }


def bbox_with_margin(points: list[Point]) -> dict[str, float]:
    """South/west/north/east of the points, widened by 150 m on every side."""
    lat_margin = BBOX_MARGIN_M / _METERS_PER_DEGREE
    mid_lat = sum(p[1] for p in points) / len(points)
    lng_margin = lat_margin / math.cos(math.radians(mid_lat))
    return {
        "south": round(min(p[1] for p in points) - lat_margin, 7),
        "west": round(min(p[0] for p in points) - lng_margin, 7),
        "north": round(max(p[1] for p in points) + lat_margin, 7),
        "east": round(max(p[0] for p in points) + lng_margin, 7),
    }


def fetch_fixture(fetch: Fetcher = _http_get_json) -> dict[str, Any]:
    """Runs the whole import: street midpoint, centre, buildings, document.

    Usage: fixture = fetch_fixture()  # network; see the module docstring
    """
    street = _overpass(fetch, f"[out:json][timeout:60];way({STREET_WAY_ID});out geom;")
    midpoint = street_midpoint(street[0]["geometry"])
    # the centre can sit ~200 m from the midpoint, so fetch with room to spare
    around = f"around:{SEARCH_RADII_M[-1] * 2},{midpoint[1]},{midpoint[0]}"
    query = f'[out:json][timeout:90];way["building"]({around});out tags geom;'
    candidates = collect_candidates(_overpass(fetch, query))
    if house_number_found(fetch):
        raise SystemExit("House 359 exists in OSM now; extend this script to centre on it.")
    nearest = min(candidates, key=lambda c: distance_m(c["centroid"], midpoint))
    center = nearest["centroid"]
    note = (
        f"House {HOUSE_NUMBER} is not mapped in OSM; the centre is the centroid of the building "
        f"nearest the midpoint of {STREET_NAME} (way {STREET_WAY_ID}): way {nearest['way_id']}."
    )
    chosen, radius = pick_with_widening_radius(candidates, center)
    return build_fixture(chosen, center, note, radius)


def main() -> None:
    fixture = fetch_fixture()
    _OUTPUT_PATH.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(f"{len(fixture['buildings'])} buildings written to {_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
