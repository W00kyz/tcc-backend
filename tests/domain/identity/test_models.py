from app.core.security import hash_password
from app.domain.identity.models import AuthLog, AuthLogEvent, User, UserRole
from sqlalchemy.ext.asyncio import AsyncSession


async def test_user_round_trips_through_the_database(db_session: AsyncSession) -> None:
    user = User(
        name="Larissa Almeida",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(user)
    await db_session.commit()

    fetched = await db_session.get(User, user.id)
    assert fetched is not None
    assert fetched.email == "larissa@pu.ufcg.edu.br"
    assert fetched.role is UserRole.MANAGER
    assert fetched.is_active is True


async def test_auth_log_accepts_a_user_without_resolved_identity(db_session: AsyncSession) -> None:
    # Login attempt with non-existent email: no user_id to associate.
    entry = AuthLog(
        user_id=None,
        attempted_email="alguem@desconhecido.com",
        event=AuthLogEvent.LOGIN_FAILURE,
        ip_address="127.0.0.1",
    )
    db_session.add(entry)
    await db_session.commit()

    assert entry.id is not None
    assert entry.user_id is None
