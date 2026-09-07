"""Etapa 9 — the notification centre HTTP surface (RF51): per-user listing, the unread
badge, single-row mark-read with its ownership guard, and bulk mark-all-read."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.core.mail import RecordingMailer
from app.core.security import hash_password
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Notification
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.object_store import FakeObjectStore

_PASSWORD = "senha-forte-o-suficiente"


@pytest.fixture
def client(test_settings: Settings, recording_mailer: RecordingMailer) -> Iterator[TestClient]:
    app = create_app(
        settings=test_settings, mailer=recording_mailer, object_store=FakeObjectStore()
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


def _login(client: TestClient, email: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _user(db: AsyncSession, email: str, role: UserRole) -> User:
    user = User(
        name=email.split("@")[0],
        email=email,
        password_hash=hash_password(_PASSWORD),
        role=role,
    )
    db.add(user)
    await db.flush()
    return user


def _note(user: User, *, title: str, read: bool = False) -> Notification:
    return Notification(
        user_id=user.id,
        kind="ROUTE_ASSIGNED",
        title=title,
        body="corpo",
        data={},
        read_at=datetime.now(UTC) if read else None,
    )


async def test_me_lists_only_own_notifications(
    client: TestClient, db_session: AsyncSession
) -> None:
    alice = await _user(db_session, "alice@pu.ufcg.edu.br", UserRole.MANAGER)
    bob = await _user(db_session, "bob@pu.ufcg.edu.br", UserRole.MANAGER)
    db_session.add_all([_note(alice, title="para alice"), _note(bob, title="para bob")])
    await db_session.commit()
    token = _login(client, alice.email)

    body = client.get("/notifications/me", headers=_auth(token)).json()

    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["para alice"]


async def test_unread_only_and_pagination(client: TestClient, db_session: AsyncSession) -> None:
    alice = await _user(db_session, "alice@pu.ufcg.edu.br", UserRole.MANAGER)
    db_session.add_all(
        [
            _note(alice, title="lida", read=True),
            _note(alice, title="n1"),
            _note(alice, title="n2"),
            _note(alice, title="n3"),
        ]
    )
    await db_session.commit()
    token = _login(client, alice.email)

    unread = client.get("/notifications/me?unread_only=true", headers=_auth(token)).json()
    assert unread["total"] == 3
    assert {item["title"] for item in unread["items"]} == {"n1", "n2", "n3"}

    page1 = client.get(
        "/notifications/me?unread_only=true&page=1&page_size=2", headers=_auth(token)
    ).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["page"] == 1
    assert page1["page_size"] == 2

    page2 = client.get(
        "/notifications/me?unread_only=true&page=2&page_size=2", headers=_auth(token)
    ).json()
    assert len(page2["items"]) == 1


async def test_unread_count(client: TestClient, db_session: AsyncSession) -> None:
    alice = await _user(db_session, "alice@pu.ufcg.edu.br", UserRole.MANAGER)
    db_session.add_all(
        [_note(alice, title="a"), _note(alice, title="b"), _note(alice, title="c", read=True)]
    )
    await db_session.commit()
    token = _login(client, alice.email)

    assert client.get("/notifications/me/unread-count", headers=_auth(token)).json() == {"count": 2}


async def test_mark_own_read_sets_read_at(client: TestClient, db_session: AsyncSession) -> None:
    alice = await _user(db_session, "alice@pu.ufcg.edu.br", UserRole.FIELD_WORKER)
    note = _note(alice, title="a")
    db_session.add(note)
    await db_session.commit()
    token = _login(client, alice.email)

    response = client.post(f"/notifications/{note.id}/read", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json()["read_at"] is not None
    assert client.get("/notifications/me/unread-count", headers=_auth(token)).json()["count"] == 0


async def test_mark_someone_elses_read_is_403(client: TestClient, db_session: AsyncSession) -> None:
    alice = await _user(db_session, "alice@pu.ufcg.edu.br", UserRole.MANAGER)
    bob = await _user(db_session, "bob@pu.ufcg.edu.br", UserRole.MANAGER)
    note = _note(bob, title="do bob")
    db_session.add(note)
    await db_session.commit()
    token = _login(client, alice.email)

    response = client.post(f"/notifications/{note.id}/read", headers=_auth(token))

    assert response.status_code == 403


async def test_read_all_marks_every_unread_and_returns_count(
    client: TestClient, db_session: AsyncSession
) -> None:
    alice = await _user(db_session, "alice@pu.ufcg.edu.br", UserRole.FIELD_WORKER)
    bob = await _user(db_session, "bob@pu.ufcg.edu.br", UserRole.MANAGER)
    db_session.add_all(
        [
            _note(alice, title="a"),
            _note(alice, title="b"),
            _note(alice, title="c", read=True),
            _note(bob, title="do bob"),
        ]
    )
    await db_session.commit()
    token = _login(client, alice.email)

    response = client.post("/notifications/me/read-all", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json() == {"marked": 2}
    assert client.get("/notifications/me/unread-count", headers=_auth(token)).json()["count"] == 0
    # Bob's notification is untouched.
    bob_token = _login(client, bob.email)
    assert (
        client.get("/notifications/me/unread-count", headers=_auth(bob_token)).json()["count"] == 1
    )


async def test_notifications_unknown_id_is_404(
    client: TestClient, db_session: AsyncSession
) -> None:
    alice = await _user(db_session, "alice@pu.ufcg.edu.br", UserRole.MANAGER)
    await db_session.commit()
    token = _login(client, alice.email)

    response = client.post(
        "/notifications/00000000-0000-0000-0000-000000000000/read", headers=_auth(token)
    )

    assert response.status_code == 404
