"""The pure selection helpers of the one-off OSM importer; the script itself needs network and
is never run here."""

from app.scripts.fetch_tambor_buildings import (
    bbox_with_margin,
    pick_buildings,
    pick_with_widening_radius,
    room_count_for_area,
)

CENTER = (-35.8900, -7.2567)
METERS_PER_DEGREE_LAT = 111_320.0


def _candidate(way_id: int, tag: str, north_m: float) -> dict[str, object]:
    centroid = (CENTER[0], CENTER[1] + north_m / METERS_PER_DEGREE_LAT)
    return {"way_id": way_id, "tag": tag, "centroid": centroid, "area": 100.0, "ring": []}


def test_room_count_follows_the_area_thresholds() -> None:
    assert [room_count_for_area(a) for a in (40, 79.9, 80, 150, 150.1, 800)] == [1, 1, 2, 2, 3, 3]


def test_residential_tags_come_before_yes() -> None:
    near = [_candidate(1, "yes", 10), _candidate(2, "house", 50)]
    assert pick_buildings(near, CENTER, 250)[0]["way_id"] == 2


def test_yes_only_fills_up_to_five() -> None:
    near = [_candidate(i, "yes", 10 * i) for i in range(1, 9)]
    assert len(pick_buildings(near, CENTER, 250)) == 5


def test_radius_widens_until_five_qualify() -> None:
    near = [_candidate(i, "yes", 300 + i) for i in range(5)]
    chosen, radius = pick_with_widening_radius(near, CENTER)
    assert (len(chosen), radius) == (5, 350)


def test_bbox_adds_the_margin_on_every_side() -> None:
    box = bbox_with_margin([(-35.89, -7.25), (-35.88, -7.24)])
    assert box["south"] < -7.25 < -7.24 < box["north"]
    assert box["west"] < -35.89 < -35.88 < box["east"]
