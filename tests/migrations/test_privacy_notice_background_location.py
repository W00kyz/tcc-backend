"""The privacy notice that covers background location (spec 2026-10-20-alocacao-automatica §7.3):
a new current version, so the Etapa 11 consent gate asks every user to accept it again.

Plain (sync) tests on purpose, like test_appeals.py: alembic's env.py calls `asyncio.run()`. They
start from the allocation reset, which the throwaway database reaches in one extra step — the
reset itself is never downgraded."""

from alembic import command

from tests.support.migrations import ALLOCATION_RESET_REVISION, alembic_config, run_sql

_REVISION = "643c2dd3ca42"
_PREVIOUS = "c6e1f8a2b4d9"

_SENTENCE = (
    "Durante a rota, se você permitir, a localização também é coletada com o app em segundo plano."
)

_NOTICES = "SELECT version, body_markdown, is_current FROM privacy_notices ORDER BY version"


def _at_previous_head(url: str) -> None:
    cfg = alembic_config(url)
    command.upgrade(cfg, ALLOCATION_RESET_REVISION)
    command.upgrade(cfg, _PREVIOUS)
    # The author may have replaced the v1 placeholder by then: the new version builds on
    # whatever the current body is, never on a hard-coded copy.
    run_sql(url, "UPDATE privacy_notices SET body_markdown = 'Texto revisado.' WHERE version = 1")


def test_upgrade_publishes_a_new_current_version_with_the_sentence(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    _at_previous_head(url)

    command.upgrade(alembic_config(url), _REVISION)

    assert run_sql(url, _NOTICES) == [
        (1, "Texto revisado.", False),
        (2, f"Texto revisado.\n\n{_SENTENCE}", True),
    ]


def test_downgrade_removes_only_the_new_version(pre_reset_database_url: str) -> None:
    url = pre_reset_database_url
    _at_previous_head(url)
    cfg = alembic_config(url)
    command.upgrade(cfg, _REVISION)
    # A user who already accepted the new version: the downgrade takes that acceptance with it
    # (it points at a notice that no longer exists) and leaves everything else alone.
    run_sql(
        url,
        "INSERT INTO users (id, name, email, password_hash, role, is_active) VALUES "
        "('00000000-0000-0000-0000-0000000000e1', 'W', 'w@x.br', 'x', 'FIELD_WORKER', true)",
    )
    run_sql(
        url,
        "INSERT INTO consent_records (id, user_id, privacy_notice_id, accepted_at) "
        "SELECT gen_random_uuid(), '00000000-0000-0000-0000-0000000000e1', id, now() "
        "FROM privacy_notices",
    )

    command.downgrade(cfg, _PREVIOUS)

    assert run_sql(url, _NOTICES) == [(1, "Texto revisado.", True)]
    assert run_sql(
        url,
        "SELECT n.version FROM consent_records c "
        "JOIN privacy_notices n ON n.id = c.privacy_notice_id",
    ) == [(1,)]


def test_downgrade_never_removes_a_later_notice_it_did_not_insert(
    pre_reset_database_url: str,
) -> None:
    url = pre_reset_database_url
    _at_previous_head(url)
    cfg = alembic_config(url)
    command.upgrade(cfg, _REVISION)
    # The author later publishes v3 by hand, also ending with the sentence.
    run_sql(url, "UPDATE privacy_notices SET is_current = false")
    run_sql(
        url,
        "INSERT INTO privacy_notices (id, version, title, body_markdown, published_at, "
        "is_current) VALUES (gen_random_uuid(), 3, 'Aviso', "
        f"'Novo texto.\n\n{_SENTENCE}', now(), true)",
    )

    command.downgrade(cfg, _PREVIOUS)

    assert [row[0] for row in run_sql(url, _NOTICES)] == [1, 2, 3]
    assert run_sql(url, _NOTICES)[2][2] is True
