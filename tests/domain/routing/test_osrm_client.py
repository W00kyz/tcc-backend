"""Real HTTP parsing for HttpxOsrmClient, exercised through httpx2.MockTransport with canned
OSRM JSON. No real network — spec §8 and the Task 1 seam rule."""

import httpx2
import pytest
from app.domain.routing.osrm import HttpxOsrmClient, OsrmLeg, OsrmUnavailable

_THREE_WAYPOINTS = [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]
_ZERO_LEG = OsrmLeg(distance_m=0.0, duration_s=0.0, geometry=[])


async def test_route_legs_parses_two_legs_and_slices_shared_geometry() -> None:
    geometry = [[0.0, 0.0], [0.5, 0.5], [1.0, 1.0], [1.5, 1.5], [2.0, 2.0]]

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.startswith("/route/v1/foot/")
        return httpx2.Response(
            200,
            json={
                "code": "Ok",
                "routes": [
                    {
                        "geometry": {"coordinates": geometry},
                        "legs": [
                            {
                                "distance": 111.1,
                                "duration": 90.0,
                                "annotation": {"distance": [55.0, 56.1], "duration": [45.0, 45.0]},
                            },
                            {
                                "distance": 222.2,
                                "duration": 180.0,
                                "annotation": {
                                    "distance": [111.0, 111.2],
                                    "duration": [90.0, 90.0],
                                },
                            },
                        ],
                    }
                ],
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = HttpxOsrmClient("http://osrm:5000", http)
        legs = await client.route_legs(_THREE_WAYPOINTS)

    assert len(legs) == 2
    assert (legs[0].distance_m, legs[0].duration_s) == (111.1, 90.0)
    assert (legs[1].distance_m, legs[1].duration_s) == (222.2, 180.0)
    assert legs[0].geometry == [[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]]
    assert legs[1].geometry == [[1.0, 1.0], [1.5, 1.5], [2.0, 2.0]]
    # Consecutive legs share the boundary waypoint.
    assert legs[0].geometry[-1] == legs[1].geometry[0]


async def test_route_legs_zeroes_every_leg_on_noroute() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"code": "NoRoute"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = HttpxOsrmClient("http://osrm:5000", http)
        legs = await client.route_legs(_THREE_WAYPOINTS)

    assert len(legs) == 2
    assert all(leg == _ZERO_LEG for leg in legs)


async def test_optimize_order_returns_the_trip_permutation() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.startswith("/trip/v1/foot/")
        return httpx2.Response(
            200,
            json={
                "code": "Ok",
                "waypoints": [
                    {"waypoint_index": 0},
                    {"waypoint_index": 2},
                    {"waypoint_index": 1},
                ],
                "trips": [{"geometry": {"coordinates": []}}],
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = HttpxOsrmClient("http://osrm:5000", http)
        order = await client.optimize_order(_THREE_WAYPOINTS)

    assert order == [0, 2, 1]


async def test_route_legs_raises_on_non_200() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="internal error")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = HttpxOsrmClient("http://osrm:5000", http)
        with pytest.raises(OsrmUnavailable):
            await client.route_legs(_THREE_WAYPOINTS)


async def test_route_legs_raises_on_transport_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = HttpxOsrmClient("http://osrm:5000", http)
        with pytest.raises(OsrmUnavailable):
            await client.route_legs(_THREE_WAYPOINTS)
