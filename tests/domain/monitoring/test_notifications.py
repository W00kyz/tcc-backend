"""Notification helpers against the real testcontainer: rows are persisted first (flush, not
commit), the push seam is best-effort (a raising sender never undoes a row), `notify_alert`
targets exactly MANAGER + ADMIN and carries the route only when the alert has one."""

import uuid

from app.core.security import hash_password
from app.domain.catalog.models import ContractorCompany
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity, Notification
from app.domain.monitoring.notifications import notify_alert, notify_users
from app.domain.routing.models import Route
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_worker


class RecordingPushSender:
    def __init__(self) -> None:
        self.sent: list[uuid.UUID] = []

    async def send(
        self, *, user_id: uuid.UUID, title: str, body: str, data: dict[str, object]
    ) -> None:
        self.sent.append(user_id)


class ExplodingPushSender:
    async def send(
        self, *, user_id: uuid.UUID, title: str, body: str, data: dict[str, object]
    ) -> None:
        raise RuntimeError("FCM is down")


async def _user(db: AsyncSession, role: UserRole) -> User:
    user = User(
        name="U",
        email=f"u-{uuid.uuid4()}@x.com",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=role,
    )
    db.add(user)
    await db.flush()
    return user


async def _route_with_worker(db: AsyncSession) -> Route:
    company = ContractorCompany(name="Co", cnpj=str(uuid.uuid4().int)[:14])
    db.add(company)
    await db.flush()
    worker = await make_worker(db, company=company, full_name="W")
    route = Route(field_worker_id=worker.id, route_date=today_on_campus())
    db.add(route)
    await db.flush()
    return route


async def test_notify_users_creates_one_row_per_deduped_id(db_session: AsyncSession) -> None:
    users = [await _user(db_session, UserRole.MANAGER) for _ in range(3)]
    ids = [u.id for u in users]
    push = RecordingPushSender()

    rows = await notify_users(
        db_session,
        user_ids=ids,
        kind="ALERT",
        title="T",
        body="B",
        data={"k": "v"},
        push=push,
    )

    assert len(rows) == 3
    assert {r.user_id for r in rows} == set(ids)
    assert all(r.kind == "ALERT" and r.title == "T" and r.body == "B" for r in rows)
    assert push.sent == ids

    dup_user = await _user(db_session, UserRole.MANAGER)
    deduped = await notify_users(
        db_session,
        user_ids=[dup_user.id, dup_user.id, ids[0]],
        kind="ALERT",
        title="T",
        body="B",
        data={},
        push=push,
    )
    assert len(deduped) == 2


async def test_push_failure_does_not_undo_rows(db_session: AsyncSession) -> None:
    user = await _user(db_session, UserRole.MANAGER)

    rows = await notify_users(
        db_session,
        user_ids=[user.id],
        kind="ALERT",
        title="T",
        body="B",
        data={},
        push=ExplodingPushSender(),
    )

    assert len(rows) == 1
    persisted = (
        await db_session.scalars(select(Notification).where(Notification.user_id == user.id))
    ).all()
    assert len(persisted) == 1


async def test_notify_alert_targets_manager_and_admin_only(db_session: AsyncSession) -> None:
    manager = await _user(db_session, UserRole.MANAGER)
    admin = await _user(db_session, UserRole.ADMIN)
    field_worker = await _user(db_session, UserRole.FIELD_WORKER)
    route = await _route_with_worker(db_session)
    alert = Alert(
        kind=AlertKind.BATCH_RELEASED,
        severity=AlertSeverity.WARNING,
        dedup_key=f"BATCH_RELEASED:{uuid.uuid4()}",
        description="Pontos liberados: saiu do campus",
        route_id=route.id,
    )
    db_session.add(alert)
    await db_session.flush()

    rows = await notify_alert(db_session, alert=alert, push=RecordingPushSender())

    assert {r.user_id for r in rows} == {manager.id, admin.id}
    assert field_worker.id not in {r.user_id for r in rows}
    assert all(r.kind == "ALERT" and r.title == "Pontos liberados" for r in rows)
    assert rows[0].data == {"alert_id": str(alert.id), "route_id": str(route.id)}


async def test_notify_alert_without_route_carries_only_the_alert(
    db_session: AsyncSession,
) -> None:
    manager = await _user(db_session, UserRole.MANAGER)
    alert = Alert(
        kind=AlertKind.POINTS_UNSERVED,
        severity=AlertSeverity.WARNING,
        dedup_key="POINTS_UNSERVED:2026-10-20",
        description="3 pontos não atendidos hoje",
    )
    db_session.add(alert)
    await db_session.flush()

    rows = await notify_alert(db_session, alert=alert, push=RecordingPushSender())

    assert [r.user_id for r in rows] == [manager.id]
    assert rows[0].title == "Pontos não atendidos"
    assert rows[0].data == {"alert_id": str(alert.id)}
