"""The fixed OSM fixture the manual-test seed uses: real building outlines around the author's
address (Rua Raimundo da Silva Ribeiro, Tambor, Campina Grande). Pure file checks, no network."""

from app.domain.catalog.footprint import PolygonFootprint
from app.domain.execution.geo import haversine_meters
from app.seed_data import TamborFixture, load_tambor_buildings

# Where Nominatim places the street (the house number itself is not mapped).
STREET_LAT, STREET_LNG = -7.2567, -35.8900


def _fixture() -> TamborFixture:
    return load_tambor_buildings()


def test_has_at_least_five_buildings() -> None:
    assert len(_fixture().buildings) >= 5


def test_every_building_has_one_to_three_rooms() -> None:
    for building in _fixture().buildings:
        assert 1 <= len(building.rooms) <= 3, building.name


def test_footprints_pass_the_catalog_validator() -> None:
    for building in _fixture().buildings:
        PolygonFootprint.model_validate(building.footprint)
        ring = building.footprint["coordinates"][0]
        assert ring[0] == ring[-1], building.name


def test_every_footprint_is_inside_the_bbox() -> None:
    fixture = _fixture()
    box = fixture.bbox
    for building in fixture.buildings:
        for lng, lat in building.footprint["coordinates"][0]:
            assert box.west <= lng <= box.east, building.name
            assert box.south <= lat <= box.north, building.name


def test_center_is_inside_the_bbox() -> None:
    fixture = _fixture()
    box = fixture.bbox
    assert box.west <= fixture.center.lng <= box.east
    assert box.south <= fixture.center.lat <= box.north


def test_center_is_within_300_m_of_the_street() -> None:
    center = _fixture().center
    assert haversine_meters(center.lat, center.lng, STREET_LAT, STREET_LNG) <= 300


def test_osm_way_ids_are_unique() -> None:
    ids = [building.osm_way_id for building in _fixture().buildings]
    assert len(ids) == len(set(ids))
