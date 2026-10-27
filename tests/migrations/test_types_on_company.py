"""Service types on the company, typed points and worker approval (spec
2026-10-20-alocacao-automatica §5.6): the data rules of the migration and its round trip.

Plain (sync) tests on purpose, like test_allocation_reset.py: alembic's env.py calls
`asyncio.run()`. They start from the revision right after the allocation reset, which the
throwaway database reaches in one extra step — the reset itself is never downgraded."""

import uuid

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "a1c5e9b3d7f2"

_COMPANY_A = "00000000-0000-0000-0000-0000000000c1"
_COMPANY_B = "00000000-0000-0000-0000-0000000000c2"
_TYPE_A = "00000000-0000-0000-0000-0000000000d1"
_TYPE_B = "00000000-0000-0000-0000-0000000000d2"
_POINT = "00000000-0000-0000-0000-0000000000a1"
_USER = "00000000-0000-0000-0000-0000000000e1"
_WORKER_WITH_LOGIN = "00000000-0000-0000-0000-0000000000e2"
_WORKER_WITHOUT_LOGIN = "00000000-0000-0000-0000-0000000000e3"


def _insert_company(url: str, company_id: str, cnpj: str) -> None:
    run_sql(
        url,
        "INSERT INTO contractor_companies (id, name, cnpj) "
        f"VALUES ('{company_id}', 'Co', '{cnpj}')",
    )


def _insert_type(url: str, type_id: str, name: str) -> None:
    run_sql(
        url,
        f"INSERT INTO service_types (id, name, average_duration_minutes) "
        f"VALUES ('{type_id}', '{name}', 30)",
    )


def _seed_registry(url: str) -> None:
    run_sql(
        url,
        "INSERT INTO buildings (id, name, campus_area) VALUES "
        "('00000000-0000-0000-0000-0000000000b1', 'Bloco CI', 'CCT')",
    )
    run_sql(
        url,
        "INSERT INTO floors (id, building_id, label) VALUES "
        "('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000b1', 'T')",
    )
    run_sql(
        url,
        "INSERT INTO service_points (id, floor_id, name, description, latitude, longitude) "
        f"VALUES ('{_POINT}', '00000000-0000-0000-0000-0000000000f1', 'Sala', '', -7.2, -35.9)",
    )


def _seed_workers(url: str, company_id: str) -> None:
    run_sql(
        url,
        "INSERT INTO users (id, name, email, password_hash, role, is_active) "
        f"VALUES ('{_USER}', 'João', 'joao@empresa.com', 'x', 'FIELD_WORKER', true)",
    )
    run_sql(
        url,
        "INSERT INTO field_workers (id, full_name, contractor_company_id, user_id) VALUES "
        f"('{_WORKER_WITH_LOGIN}', 'João', '{company_id}', '{_USER}')",
    )
    run_sql(
        url,
        "INSERT INTO field_workers (id, full_name, contractor_company_id) VALUES "
        f"('{_WORKER_WITHOUT_LOGIN}', 'Sem login', '{company_id}')",
    )


def _at_reset_revision(url: str) -> None:
    command.upgrade(alembic_config(url), ALLOCATION_RESET_REVISION)


def test_single_company_adopts_orphan_types_and_single_type_types_points(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    _at_reset_revision(url)
    _insert_company(url, _COMPANY_A, "1")
    _insert_type(url, _TYPE_A, "Limpeza")
    _seed_registry(url)
    _seed_workers(url, _COMPANY_A)

    command.upgrade(alembic_config(url), _REVISION)

    assert run_sql(url, "SELECT contractor_company_id FROM service_types") == [
        (uuid.UUID(_COMPANY_A),)
    ]
    assert run_sql(url, "SELECT service_type_id FROM service_points") == [(uuid.UUID(_TYPE_A),)]
    assert run_sql(url, "SELECT id, status::text, is_active FROM field_workers") == [
        (uuid.UUID(_WORKER_WITH_LOGIN), "APPROVED", True)
    ]


def test_several_companies_and_types_stay_unassigned(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _at_reset_revision(url)
    _insert_company(url, _COMPANY_A, "1")
    _insert_company(url, _COMPANY_B, "2")
    _insert_type(url, _TYPE_A, "Limpeza")
    _insert_type(url, _TYPE_B, "Jardinagem")
    _seed_registry(url)

    command.upgrade(alembic_config(url), _REVISION)

    assert run_sql(url, "SELECT contractor_company_id FROM service_types") == [(None,), (None,)]
    assert run_sql(url, "SELECT service_type_id FROM service_points") == [(None,)]


def test_downgrade_returns_to_the_reset_revision(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _at_reset_revision(url)
    cfg = alembic_config(url)
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, ALLOCATION_RESET_REVISION)

    assert run_sql(url, "SELECT to_regclass('field_worker_service_types')")[0][0] is not None
    assert run_sql(url, "SELECT to_regtype('field_worker_status')") == [(None,)]
