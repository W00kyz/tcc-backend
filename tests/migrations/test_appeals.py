"""The appeals table (spec 2026-10-20-alocacao-automatica §5.4) and its round trip.

Plain (sync) tests on purpose, like test_types_on_company.py: alembic's env.py calls
`asyncio.run()`. They start from the allocation reset, which the throwaway database reaches in
one extra step — the reset itself is never downgraded."""

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "c6e1f8a2b4d9"
_PREVIOUS = "a1c5e9b3d7f2"


def test_upgrade_creates_appeals_and_downgrade_drops_them(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _REVISION)

    assert run_sql(url, "SELECT to_regclass('appeals')")[0][0] is not None
    assert run_sql(url, "SELECT enum_range(NULL::appeal_status)::text") == [
        ("{PENDING,ACCEPTED,REJECTED}",)
    ]
    assert run_sql(url, "SELECT enum_range(NULL::appeal_reason)::text") == [
        ("{NO_SIGNAL,LEFT_BRIEFLY,APP_CLOSED,OTHER}",)
    ]

    command.downgrade(cfg, _PREVIOUS)

    assert run_sql(url, "SELECT to_regclass('appeals')") == [(None,)]
    assert run_sql(url, "SELECT to_regtype('appeal_status')") == [(None,)]
    assert run_sql(url, "SELECT to_regtype('appeal_reason')") == [(None,)]
