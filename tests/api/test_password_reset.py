from app.core.jwt import create_access_token, create_password_reset_token
from app.core.mail import RecordingMailer
from app.core.security import hash_password, verify_password
from app.domain.identity.models import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_user(db_session: AsyncSession) -> User:
    user = User(
        name="João",
        email="joao@empresa.com",
        password_hash=hash_password("senha-antiga-valida"),
        role=UserRole.FIELD_WORKER,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def test_request_sends_one_email_with_a_reset_token(
    client: TestClient, db_session: AsyncSession, recording_mailer: RecordingMailer
) -> None:
    await _seed_user(db_session)

    response = client.post("/auth/password-reset/request", json={"email": "joao@empresa.com"})

    assert response.status_code == 202
    assert len(recording_mailer.sent) == 1
    assert recording_mailer.sent[0]["to"] == "joao@empresa.com"


async def test_request_for_an_unknown_email_still_returns_202_and_sends_nothing(
    client: TestClient, recording_mailer: RecordingMailer
) -> None:
    # Not revealing whether an e-mail exists is a deliberate choice — RNF10/LGPD.
    response = client.post("/auth/password-reset/request", json={"email": "ninguem@empresa.com"})

    assert response.status_code == 202
    assert len(recording_mailer.sent) == 0


async def test_confirm_with_a_valid_token_changes_the_password(
    client: TestClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)

    token = create_password_reset_token(
        user_id=user.id, secret="test-secret-do-not-use-in-production"
    )

    response = client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "senha-nova-valida"}
    )

    assert response.status_code == 204
    await db_session.refresh(user)
    assert verify_password("senha-nova-valida", user.password_hash)


async def test_confirm_with_an_access_token_is_rejected(
    client: TestClient, db_session: AsyncSession
) -> None:
    # A regular token must not work here — only one with type="password_reset".
    user = await _seed_user(db_session)

    token = create_access_token(
        user_id=user.id,
        role=UserRole.FIELD_WORKER,
        secret="test-secret-do-not-use-in-production",
        minutes=15,
    )

    response = client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "senha-nova-valida"}
    )

    assert response.status_code == 401
