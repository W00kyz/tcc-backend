"""The automatic-allocation reset (spec 2026-10-20-alocacao-automatica §5.6): the upgrade wipes
every operational row, drops the manual-planning tables and keeps the registry (company, type,
building, point, user) untouched.

Plain (sync) test on purpose: alembic's env.py calls `asyncio.run()`, which would blow up
inside an already-running event loop (Etapa 9 pattern, tests/domain/monitoring/test_models.py)."""

import uuid

import pytest
from alembic import command

from tests.support.migrations import LAST_REVERSIBLE_REVISION, alembic_config, run_sql

_OPERATIONAL_TABLES = (
    "routes",
    "route_stops",
    "executions",
    "alerts",
    "worker_positions",
    "impediments",
    "notifications",
)
_DROPPED_TABLES = (
    "route_templates",
    "route_template_stops",
    "stop_assignments",
    "manual_completions",
)

_COMPANY = "00000000-0000-0000-0000-0000000000c1"
_TYPE = "00000000-0000-0000-0000-0000000000d1"
_BUILDING = "00000000-0000-0000-0000-0000000000b1"
_FLOOR = "00000000-0000-0000-0000-0000000000f1"
_POINT = "00000000-0000-0000-0000-0000000000a1"
_USER = "00000000-0000-0000-0000-0000000000e1"
_WORKER = "00000000-0000-0000-0000-0000000000e2"
_ROUTE = "00000000-0000-0000-0000-000000000011"
_STOP = "00000000-0000-0000-0000-000000000012"
_EXECUTION = "00000000-0000-0000-0000-000000000013"

# One statement per call: asyncpg refuses several commands in one prepared statement.
_SEED_AT_PREVIOUS_REVISION = (
    f"INSERT INTO contractor_companies (id, name, cnpj) VALUES ('{_COMPANY}', 'Limpa', '1')",
    f"INSERT INTO service_types (id, name, average_duration_minutes) "
    f"VALUES ('{_TYPE}', 'Limpeza', 30)",
    f"INSERT INTO buildings (id, name, campus_area) VALUES ('{_BUILDING}', 'Bloco CI', 'CCT')",
    f"INSERT INTO floors (id, building_id, label) VALUES ('{_FLOOR}', '{_BUILDING}', 'Térreo')",
    f"INSERT INTO service_points (id, floor_id, name, description, latitude, longitude) "
    f"VALUES ('{_POINT}', '{_FLOOR}', 'Sala 101', '', -7.2, -35.9)",
    f"INSERT INTO users (id, name, email, password_hash, role, is_active) "
    f"VALUES ('{_USER}', 'João', 'joao@empresa.com', 'x', 'FIELD_WORKER', true)",
    f"INSERT INTO field_workers (id, full_name, contractor_company_id, user_id) "
    f"VALUES ('{_WORKER}', 'João', '{_COMPANY}', '{_USER}')",
    f"INSERT INTO routes (id, field_worker_id, route_date, route_type, status) "
    f"VALUES ('{_ROUTE}', '{_WORKER}', '2026-10-19', 'REGULAR', 'PLANNED')",
    f"INSERT INTO route_stops (id, route_id, service_point_id, order_index, status) "
    f"VALUES ('{_STOP}', '{_ROUTE}', '{_POINT}', 1, 'DONE')",
    f"INSERT INTO stop_assignments (id, route_stop_id, field_worker_id, sequence, assigned_by) "
    f"VALUES ('00000000-0000-0000-0000-000000000014', '{_STOP}', '{_WORKER}', 1, '{_USER}')",
    "INSERT INTO executions (id, route_stop_id, field_worker_id, checked_in_at, synced_at, "
    "source, idempotency_key, review_status, validation_flags) "
    f"VALUES ('{_EXECUTION}', '{_STOP}', '{_WORKER}', now(), now(), 'MANAGER_MANUAL', "
    "'00000000-0000-0000-0000-000000000015', 'NONE', '[]')",
    "INSERT INTO manual_completions "
    "(id, route_stop_id, execution_id, completed_by, reason, completed_at) "
    f"VALUES ('00000000-0000-0000-0000-000000000016', '{_STOP}', '{_EXECUTION}', '{_USER}', "
    "'sem acesso', now())",
    "INSERT INTO alerts (id, kind, severity, dedup_key, description, route_id, status) "
    "VALUES ('00000000-0000-0000-0000-000000000017', 'LATE_START', 'WARNING', 'LATE_START:x', "
    f"'atrasada', '{_ROUTE}', 'OPEN')",
    "INSERT INTO worker_positions "
    "(id, field_worker_id, route_id, latitude, longitude, reported_at) "
    f"VALUES ('00000000-0000-0000-0000-000000000018', '{_WORKER}', '{_ROUTE}', -7.2, -35.9, now())",
    "INSERT INTO impediments (id, route_id, scope, reason, reported_by, reported_at, status) "
    f"VALUES ('00000000-0000-0000-0000-000000000019', '{_ROUTE}', 'ROUTE_REMAINDER', 'OTHER', "
    f"'{_WORKER}', now(), 'PENDING')",
    "INSERT INTO notifications (id, user_id, kind, title, body, data) "
    f"VALUES ('00000000-0000-0000-0000-00000000001a', '{_USER}', 'ROUTE_ASSIGNED', 't', 'b', "
    "'{}')",
)


def _count(url: str, table: str) -> int:
    rows = run_sql(url, f"SELECT count(*) FROM {table}")
    return int(str(rows[0][0]))


def _exists(url: str, table: str) -> bool:
    return run_sql(url, f"SELECT to_regclass('{table}')")[0][0] is not None


def test_upgrade_wipes_operational_data_and_keeps_registry(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    for statement in _SEED_AT_PREVIOUS_REVISION:
        run_sql(url, statement)

    command.upgrade(alembic_config(url), "head")

    assert {table: _count(url, table) for table in _OPERATIONAL_TABLES} == dict.fromkeys(
        _OPERATIONAL_TABLES, 0
    )
    assert [table for table in _DROPPED_TABLES if _exists(url, table)] == []
    registry = {
        "contractor_companies": _COMPANY,
        "service_types": _TYPE,
        "buildings": _BUILDING,
        "service_points": _POINT,
        "users": _USER,
    }
    for table, row_id in registry.items():
        assert run_sql(url, f"SELECT id FROM {table}") == [(uuid.UUID(row_id),)]


def test_upgrade_reseeds_one_alert_rule_per_new_kind(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    command.upgrade(alembic_config(url), "head")

    rules = run_sql(url, "SELECT rule_kind::text, enabled, threshold_minutes FROM alert_rules")
    assert sorted(rules) == [
        ("APPEAL_FILED", True, None),
        ("BATCH_RELEASED", True, None),
        ("NO_EVIDENCE", True, None),
        ("POINTS_UNSERVED", True, None),
        ("POINT_IMPEDED", True, None),
    ]


def test_downgrade_is_refused(pre_reset_database_url: str) -> None:
    cfg = alembic_config(pre_reset_database_url)
    command.upgrade(cfg, "head")

    with pytest.raises(NotImplementedError, match="restore from backup"):
        command.downgrade(cfg, LAST_REVERSIBLE_REVISION)
