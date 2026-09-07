"""Notification helpers against the real testcontainer: rows are persisted first (flush, not
commit), the push seam is best-effort (a raising sender never undoes a row), `notify_alert`
targets exactly MANAGER + ADMIN, and `notify_route_assigned` no-ops for a field worker without
app access."""

import uuid
from datetime import UTC, datetime

import pytest
from app.core.security import hash_password
from app.domain.catalog.models import ContractorCompany, FieldWorker
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.models import Alert, AlertKind, AlertSeverity, Notification
from app.domain.monitoring.notifications import (
    notify_alert,
    notify_route_assigned,
    notify_users,
)
from app.domain.routing.models import Route, RouteStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


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


async def _route_with_worker(db: AsyncSession, *, with_user: bool) -> Route:
    company = ContractorCompany(name="Co", cnpj=str(uuid.uuid4().int)[:14])
    db.add(company)
    await db.flush()
    user_id = (await _user(db, UserRole.FIELD_WORKER)).id if with_user else None
    worker = FieldWorker(full_name="W", contractor_company_id=company.id, user_id=user_id)
    db.add(worker)
    await db.flush()
    route = Route(
        field_worker_id=worker.id,
        route_date=datetime.now(UTC).date(),
        status=RouteStatus.PLANNED,
    )
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
    route = await _route_with_worker(db_session, with_user=False)
    alert = Alert(
        kind=AlertKind.LATE_STOP,
        severity=AlertSeverity.WARNING,
        dedup_key=f"LATE_STOP:{uuid.uuid4()}",
        description="Ponto fora da janela de chegada",
        route_id=route.id,
    )
    db_session.add(alert)
    await db_session.flush()

    rows = await notify_alert(db_session, alert=alert, push=RecordingPushSender())

    assert {r.user_id for r in rows} == {manager.id, admin.id}
    assert field_worker.id not in {r.user_id for r in rows}
    assert all(r.kind == "ALERT" and r.title == "Ponto atrasado" for r in rows)
    assert rows[0].data["alert_id"] == str(alert.id)


async def test_notify_route_assigned_with_user(db_session: AsyncSession) -> None:
    route = await _route_with_worker(db_session, with_user=True)
    worker = await db_session.get(FieldWorker, route.field_worker_id)
    assert worker is not None

    rows = await notify_route_assigned(
        db_session, route=route, kind="ROUTE_ASSIGNED", push=RecordingPushSender()
    )

    assert len(rows) == 1
    assert rows[0].user_id == worker.user_id
    assert rows[0].kind == "ROUTE_ASSIGNED"
    assert rows[0].data["route_id"] == str(route.id)


async def test_notify_route_assigned_no_op_without_user(db_session: AsyncSession) -> None:
    route = await _route_with_worker(db_session, with_user=False)

    rows = await notify_route_assigned(
        db_session, route=route, kind="ROUTE_ASSIGNED", push=RecordingPushSender()
    )

    assert rows == []
    assert (await db_session.scalars(select(Notification))).all() == []


async def test_notify_route_assigned_rejects_unknown_kind(db_session: AsyncSession) -> None:
    route = await _route_with_worker(db_session, with_user=True)
    with pytest.raises(ValueError, match="Unknown route-assignment notification kind"):
        await notify_route_assigned(
            db_session, route=route, kind="BOGUS", push=RecordingPushSender()
        )
