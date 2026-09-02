"""OSRM routing client (spec §4, Rulings 5-7). The backend calls OSRM on route writes and
persists the result; the app never talks to OSRM directly. Perfil `foot` — see
infra/scripts/build-osrm.sh."""

from dataclasses import dataclass
from typing import Protocol

import httpx2


@dataclass(frozen=True)
class OsrmLeg:
    distance_m: float
    duration_s: float
    geometry: list[list[float]]  # [[lng, lat], ...] — GeoJSON coordinate order


class OsrmUnavailable(Exception):
    """OSRM did not respond, or responded with a transport-level failure. A single
    un-routable leg is NOT this — that comes back as a zeroed OsrmLeg (Ruling 6)."""


_ZERO_LEG = OsrmLeg(distance_m=0.0, duration_s=0.0, geometry=[])


def _coords_param(coordinates: list[tuple[float, float]]) -> str:
    return ";".join(f"{lng},{lat}" for lng, lat in coordinates)


class OsrmClient(Protocol):
    async def route_legs(self, coordinates: list[tuple[float, float]]) -> list[OsrmLeg]: ...
    async def optimize_order(self, coordinates: list[tuple[float, float]]) -> list[int]: ...


class HttpxOsrmClient:
    def __init__(self, base_url: str, http_client: httpx2.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http_client

    async def route_legs(self, coordinates: list[tuple[float, float]]) -> list[OsrmLeg]:
        if len(coordinates) < 2:
            return []
        url = f"{self._base_url}/route/v1/foot/{_coords_param(coordinates)}"
        params = {"overview": "full", "geometries": "geojson", "annotations": "distance,duration"}
        try:
            response = await self._http.get(url, params=params)
        except httpx2.HTTPError as exc:
            raise OsrmUnavailable(f"OSRM did not respond at {url!r}: {exc}") from exc
        if response.status_code != 200:
            raise OsrmUnavailable(
                f"OSRM answered {response.status_code} for {url!r}: {response.text[:200]}"
            )
        body = response.json()
        if body.get("code") != "Ok" or not body.get("routes"):
            # Whole request failed to route — every leg is degraded, not a transport error.
            return [_ZERO_LEG for _ in range(len(coordinates) - 1)]
        route = body["routes"][0]
        full_geometry: list[list[float]] = route["geometry"]["coordinates"]
        legs_json = route["legs"]
        # OSRM gives per-leg distance/duration directly; slice the full geometry per leg
        # using the per-leg annotation lengths.
        legs: list[OsrmLeg] = []
        cursor = 0
        for leg in legs_json:
            # A leg without an `annotation` block (OSRM omits it for a degenerate/zero-length
            # leg) must degrade to an empty geometry slice, not raise KeyError.
            annotation_distances = (leg.get("annotation") or {}).get("distance") or []
            step_count = len(annotation_distances)
            geo_slice = full_geometry[cursor : cursor + step_count + 1]
            cursor += step_count
            legs.append(
                OsrmLeg(
                    distance_m=float(leg["distance"]),
                    duration_s=float(leg["duration"]),
                    geometry=geo_slice if len(geo_slice) >= 2 else [],
                )
            )
        return legs

    async def optimize_order(self, coordinates: list[tuple[float, float]]) -> list[int]:
        if len(coordinates) < 2:
            return list(range(len(coordinates)))
        url = f"{self._base_url}/trip/v1/foot/{_coords_param(coordinates)}"
        params = {"source": "first", "roundtrip": "false", "geometries": "geojson"}
        try:
            response = await self._http.get(url, params=params)
        except httpx2.HTTPError as exc:
            raise OsrmUnavailable(f"OSRM did not respond at {url!r}: {exc}") from exc
        if response.status_code != 200:
            raise OsrmUnavailable(f"OSRM answered {response.status_code} for {url!r}.")
        body = response.json()
        if body.get("code") != "Ok" or not body.get("waypoints"):
            raise OsrmUnavailable(f"OSRM trip returned {body.get('code')!r} for {url!r}.")
        # waypoints[i].waypoint_index = position of input point i in the optimised tour.
        order = sorted(
            range(len(body["waypoints"])),
            key=lambda i: body["waypoints"][i]["waypoint_index"],
        )
        return order
