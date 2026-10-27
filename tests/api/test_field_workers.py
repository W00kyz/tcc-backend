from app.core.security import hash_password
from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerStatus,
    Floor,
    ServicePoint,
)
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import RouteStopStatus
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.clock import today_on_campus
from tests.support.factories import make_route, make_worker


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/login", json={"email": email, "password": "senha-forte-o-suficiente"}
    )
    return str(response.json()["access_token"])


async def _seed(
    db_session: AsyncSession, role: UserRole = UserRole.MANAGER
) -> tuple[User, ContractorCompany]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=role,
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    db_session.add_all([manager, company])
    await db_session.commit()
    return manager, company


async def _headers(client: TestClient, user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, user.email)}"}


async def _audit_actions(db_session: AsyncSession, worker_id: object) -> list[str]:
    rows = await db_session.scalars(
        select(AuditTrail.action).where(AuditTrail.entity_id == worker_id)
    )
    return list(rows)


async def test_list_includes_company_and_status(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company = await _seed(db_session)
    pending = await make_worker(
        db_session,
        company=company,
        full_name="Ana",
        email="ana@empresa.com",
        status=FieldWorkerStatus.PENDING_APPROVAL,
    )
    await db_session.commit()

    response = client.get("/field-workers", headers=await _headers(client, manager))

    assert response.status_code == 200
    [item] = response.json()
    assert item["id"] == str(pending.id)
    assert item["full_name"] == "Ana"
    assert item["email"] == "ana@empresa.com"
    assert item["contractor_company_id"] == str(company.id)
    assert item["contractor_company_name"] == "Limpa Tudo"
    assert (item["status"], item["is_active"], item["rejection_reason"]) == (
        "PENDING_APPROVAL",
        True,
        None,
    )
    assert item["created_at"]
    assert item["done_today"] == 0


async def test_done_today_counts_only_todays_finished_stops(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company = await _seed(db_session)
    worker = await make_worker(db_session, company=company)
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()
    points = [
        ServicePoint(
            floor_id=floor.id, name=f"Sala {n}", description="", latitude=-7.2, longitude=-35.9
        )
        for n in range(3)
    ]
    db_session.add_all(points)
    await db_session.flush()
    route = await make_route(db_session, worker=worker, day=today_on_campus(), points=points)
    route.stops[0].status = RouteStopStatus.DONE
    route.stops[1].status = RouteStopStatus.DONE
    await db_session.commit()

    [item] = client.get("/field-workers", headers=await _headers(client, manager)).json()

    assert item["done_today"] == 2


async def test_manager_approves_a_pending_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company = await _seed(db_session)
    worker = await make_worker(
        db_session, company=company, status=FieldWorkerStatus.PENDING_APPROVAL
    )
    await db_session.commit()

    response = client.post(
        f"/field-workers/{worker.id}/approve", headers=await _headers(client, manager)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "APPROVED"
    await db_session.refresh(worker)
    assert (worker.reviewed_by, worker.reviewed_at is not None) == (manager.id, True)
    assert await _audit_actions(db_session, worker.id) == ["approve"]


async def test_reject_records_the_reason_and_approve_can_reverse_it(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company = await _seed(db_session)
    worker = await make_worker(
        db_session, company=company, status=FieldWorkerStatus.PENDING_APPROVAL
    )
    await db_session.commit()
    headers = await _headers(client, manager)

    rejected = client.post(
        f"/field-workers/{worker.id}/reject", json={"reason": "Empresa errada"}, headers=headers
    )
    assert rejected.status_code == 200, rejected.text
    assert (rejected.json()["status"], rejected.json()["rejection_reason"]) == (
        "REJECTED",
        "Empresa errada",
    )

    approved = client.post(f"/field-workers/{worker.id}/approve", headers=headers)
    assert approved.status_code == 200
    assert (approved.json()["status"], approved.json()["rejection_reason"]) == ("APPROVED", None)
    assert await _audit_actions(db_session, worker.id) == ["reject", "approve"]


async def test_reject_without_a_reason(client: TestClient, db_session: AsyncSession) -> None:
    manager, company = await _seed(db_session)
    worker = await make_worker(
        db_session, company=company, status=FieldWorkerStatus.PENDING_APPROVAL
    )
    await db_session.commit()

    response = client.post(
        f"/field-workers/{worker.id}/reject", json={}, headers=await _headers(client, manager)
    )

    assert response.status_code == 200
    assert response.json()["rejection_reason"] is None


async def test_deactivate_and_reactivate(client: TestClient, db_session: AsyncSession) -> None:
    manager, company = await _seed(db_session)
    worker = await make_worker(db_session, company=company)
    await db_session.commit()
    headers = await _headers(client, manager)

    off = client.post(f"/field-workers/{worker.id}/deactivate", headers=headers)
    assert (off.status_code, off.json()["is_active"], off.json()["status"]) == (
        200,
        False,
        "APPROVED",
    )
    on = client.post(f"/field-workers/{worker.id}/reactivate", headers=headers)
    assert (on.status_code, on.json()["is_active"]) == (200, True)
    assert await _audit_actions(db_session, worker.id) == ["deactivate", "reactivate"]


async def test_decisions_that_do_not_fit_the_status_are_refused(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, company = await _seed(db_session)
    approved = await make_worker(db_session, company=company)
    pending = await make_worker(
        db_session, company=company, status=FieldWorkerStatus.PENDING_APPROVAL
    )
    await db_session.commit()
    headers = await _headers(client, manager)

    attempts = [
        client.post(f"/field-workers/{approved.id}/approve", headers=headers),
        client.post(f"/field-workers/{approved.id}/reject", json={}, headers=headers),
        client.post(f"/field-workers/{pending.id}/deactivate", headers=headers),
        client.post(f"/field-workers/{pending.id}/reactivate", headers=headers),
    ]

    assert [(r.status_code, r.json()["detail"]["code"]) for r in attempts] == [
        (409, "INVALID_WORKER_STATUS")
    ] * 4


async def test_unknown_worker_returns_404(client: TestClient, db_session: AsyncSession) -> None:
    manager, _company = await _seed(db_session)

    response = client.post(
        "/field-workers/00000000-0000-0000-0000-000000000001/approve",
        headers=await _headers(client, manager),
    )

    assert response.status_code == 404


async def test_a_field_worker_cannot_review_anyone(
    client: TestClient, db_session: AsyncSession
) -> None:
    _manager, company = await _seed(db_session)
    worker = await make_worker(
        db_session, company=company, email="ana@empresa.com", status=FieldWorkerStatus.APPROVED
    )
    await db_session.commit()
    user = await db_session.get(User, worker.user_id)
    assert user is not None
    headers = await _headers(client, user)

    responses = [
        client.get("/field-workers", headers=headers),
        client.post(f"/field-workers/{worker.id}/approve", headers=headers),
        client.post(f"/field-workers/{worker.id}/reject", json={}, headers=headers),
        client.post(f"/field-workers/{worker.id}/deactivate", headers=headers),
        client.post(f"/field-workers/{worker.id}/reactivate", headers=headers),
    ]

    assert [r.status_code for r in responses] == [403] * 5
    assert (await db_session.get(FieldWorker, worker.id)) is not None


async def test_the_old_create_edit_and_link_endpoints_are_gone(
    client: TestClient, db_session: AsyncSession
) -> None:
    manager, _company = await _seed(db_session)
    headers = await _headers(client, manager)

    assert client.post("/field-workers", json={}, headers=headers).status_code == 405
    assert client.patch("/field-workers/x", json={}, headers=headers).status_code == 404
    assert client.get("/field-workers/linkable-users", headers=headers).status_code == 404
