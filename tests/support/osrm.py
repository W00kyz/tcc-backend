"""Shared OSRM fake. No test touches the real routing service (spec §8)."""

from itertools import pairwise

from app.domain.routing.osrm import OsrmLeg, OsrmUnavailable


class FakeOsrmClient:
    def __init__(
        self,
        *,
        legs: list[OsrmLeg] | None = None,
        order: list[int] | None = None,
        unavailable: bool = False,
    ) -> None:
        self._legs = legs
        self._order = order
        self._unavailable = unavailable
        self.calls: list[tuple[str, list[tuple[float, float]]]] = []

    async def route_legs(self, coordinates: list[tuple[float, float]]) -> list[OsrmLeg]:
        self.calls.append(("route_legs", list(coordinates)))
        if self._unavailable:
            raise OsrmUnavailable("FakeOsrmClient configured unavailable.")
        if self._legs is not None:
            return self._legs
        return [
            OsrmLeg(distance_m=10.0, duration_s=8.0, geometry=[list(a), list(b)])
            for a, b in pairwise(coordinates)
        ]

    async def optimize_order(self, coordinates: list[tuple[float, float]]) -> list[int]:
        self.calls.append(("optimize_order", list(coordinates)))
        if self._unavailable:
            raise OsrmUnavailable("FakeOsrmClient configured unavailable.")
        return self._order if self._order is not None else list(range(len(coordinates)))
