"""Points stop having a location of their own: the migration moves every existing point of a
building with an outline to that outline's centre, and leaves points of outline-less (legacy)
buildings where they were.

Runs on a scratch database (tests/support/migrations.py): the shared one is past the
irreversible allocation reset. Plain (sync) test on purpose: alembic's env.py calls
`asyncio.run()`, which would blow up inside an already-running event loop (Etapa 9 pattern,
tests/domain/monitoring/test_models.py)."""

import pytest
from alembic import command

from tests.support.migrations import LAST_REVERSIBLE_REVISION, alembic_config, run_sql

_BEFORE = "d4f1c2a9b8e7"
_RECTANGLE = (
    '{"type": "Polygon", "coordinates": [[[-35.909, -7.214], [-35.908, -7.214], '
    "[-35.908, -7.215], [-35.909, -7.215], [-35.909, -7.214]]]}"
)


_SEED = (
    "INSERT INTO buildings (id, name, campus_area, footprint) VALUES "
    f"('00000000-0000-0000-0000-0000000000b1', 'Com contorno', 'CCT', '{_RECTANGLE}'), "
    "('00000000-0000-0000-0000-0000000000b2', 'Sem contorno', 'CCT', NULL)",
    "INSERT INTO floors (id, building_id, label) VALUES "
    "('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000b1', 'T'), "
    "('00000000-0000-0000-0000-0000000000f2', '00000000-0000-0000-0000-0000000000b2', 'T')",
    "INSERT INTO service_points (id, floor_id, name, description, latitude, longitude) VALUES "
    "('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-0000000000f1', "
    "'Sala', '', -7.0, -35.0), "
    "('00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-0000000000f2', "
    "'Sala', '', -7.0, -35.0)",
)


def test_points_move_to_their_building_centre(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)
    command.downgrade(cfg, _BEFORE)
    for statement in _SEED:
        run_sql(url, statement)

    command.upgrade(cfg, LAST_REVERSIBLE_REVISION)

    selected = run_sql(url, "SELECT id, latitude, longitude FROM service_points")
    rows = {str(point_id): (lat, lon) for point_id, lat, lon in selected}
    assert rows["00000000-0000-0000-0000-0000000000a1"] == (
        pytest.approx(-7.2145),
        pytest.approx(-35.9085),
    )
    assert rows["00000000-0000-0000-0000-0000000000a2"] == (-7.0, -35.0)
