"""A field worker registers themselves and waits for a manager's approval (spec
2026-10-20-alocacao-automatica §5.2)."""

from app.domain.audit.models import AuditTrail
from app.domain.catalog.models import ContractorCompany, FieldWorker, FieldWorkerStatus
from app.domain.identity.models import User
from app.domain.privacy.models import ConsentRecord
from app.domain.privacy.service import has_consented
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_worker, seed_privacy_notice

_PASSWORD = "uma-senha-longa-123"


async def _company(db_session: AsyncSession, name: str = "Limpa Tudo") -> ContractorCompany:
    company = ContractorCompany(name=name, cnpj=name[:14].ljust(14, "0"))
    db_session.add(company)
    await db_session.commit()
    return company


def _register(
    client: TestClient,
    company: ContractorCompany,
    *,
    email: str = "ana@empresa.com",
    **extra: object,
) -> Response:
    return client.post(
        "/auth/register",
        json={
            "name": "Ana Souza",
            "email": email,
            "password": _PASSWORD,
            "contractor_company_id": str(company.id),
            "privacy_notice_version": 1,
            **extra,
        },
    )


def _login(client: TestClient, email: str, password: str = _PASSWORD) -> dict[str, object]:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


async def test_register_creates_pending_worker_with_consent(
    client: TestClient, db_session: AsyncSession
) -> None:
    await seed_privacy_notice(db_session)
    company = await _company(db_session)

    response = _register(client, company)

    assert response.status_code == 201, response.text
    assert response.json() == {"status": "PENDING_APPROVAL"}
    user = await db_session.scalar(select(User).where(User.email == "ana@empresa.com"))
    assert user is not None
    worker = await db_session.scalar(select(FieldWorker).where(FieldWorker.user_id == user.id))
    assert worker is not None
    assert (worker.full_name, worker.contractor_company_id) == ("Ana Souza", company.id)
    assert worker.status is FieldWorkerStatus.PENDING_APPROVAL
    assert await has_consented(db_session, user_id=user.id)
    consents = (await db_session.scalars(select(ConsentRecord))).all()
    assert [c.user_id for c in consents] == [user.id]
    audit = await db_session.scalar(
        select(AuditTrail).where(AuditTrail.entity_id == worker.id, AuditTrail.action == "register")
    )
    assert audit is not None


async def test_email_taken(client: TestClient, db_session: AsyncSession) -> None:
    await seed_privacy_notice(db_session)
    company = await _company(db_session)
    assert _register(client, company).status_code == 201

    response = _register(client, company)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EMAIL_TAKEN"
    assert len((await db_session.scalars(select(FieldWorker))).all()) == 1


async def test_outdated_notice(client: TestClient, db_session: AsyncSession) -> None:
    await seed_privacy_notice(db_session, version=2)
    company = await _company(db_session)

    response = _register(client, company, privacy_notice_version=1)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "NOTICE_OUTDATED"
    assert (await db_session.scalars(select(User))).all() == []


async def test_unknown_company_is_rejected(client: TestClient, db_session: AsyncSession) -> None:
    await seed_privacy_notice(db_session)
    company = ContractorCompany(name="Fantasma", cnpj="0")  # never saved

    response = _register(client, company)

    assert response.status_code == 422


async def test_short_password_is_rejected(client: TestClient, db_session: AsyncSession) -> None:
    await seed_privacy_notice(db_session)
    company = await _company(db_session)

    assert _register(client, company, password="curta").status_code == 422


async def test_public_companies_needs_no_auth(client: TestClient, db_session: AsyncSession) -> None:
    company = await _company(db_session)

    response = client.get("/public/contractor-companies")

    assert response.status_code == 200
    assert response.json() == [{"id": str(company.id), "name": "Limpa Tudo"}]


async def test_pending_worker_can_login_and_sees_status(
    client: TestClient, db_session: AsyncSession
) -> None:
    await seed_privacy_notice(db_session)
    company = await _company(db_session)
    _register(client, company)

    body = _login(client, "ana@empresa.com")

    assert body["worker_status"] == "PENDING_APPROVAL"
    assert body["rejection_reason"] is None


async def test_pending_worker_gets_403_on_routes_me(
    client: TestClient, db_session: AsyncSession
) -> None:
    await seed_privacy_notice(db_session)
    company = await _company(db_session)
    _register(client, company)
    token = _login(client, "ana@empresa.com")["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/routes/me", headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "WORKER_NOT_APPROVED"
    # personal data stays reachable: the worker is still a data subject
    assert client.get("/me/personal-data", headers=headers).status_code == 200


async def test_rejected_worker_sees_reason(client: TestClient, db_session: AsyncSession) -> None:
    company = await _company(db_session)
    worker = await make_worker(
        db_session, company=company, email="ana@empresa.com", status=FieldWorkerStatus.REJECTED
    )
    worker.rejection_reason = "Empresa não reconhecida"
    await db_session.commit()

    body = _login(client, "ana@empresa.com", "senha-forte-o-suficiente")

    assert body["worker_status"] == "REJECTED"
    assert body["rejection_reason"] == "Empresa não reconhecida"


async def test_deactivated_worker_gets_403(client: TestClient, db_session: AsyncSession) -> None:
    company = await _company(db_session)
    await make_worker(db_session, company=company, email="ana@empresa.com", is_active=False)
    await db_session.commit()
    token = _login(client, "ana@empresa.com", "senha-forte-o-suficiente")["access_token"]

    response = client.get("/routes/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "WORKER_NOT_APPROVED"


async def test_approved_worker_reaches_routes_me(
    client: TestClient, db_session: AsyncSession
) -> None:
    company = await _company(db_session)
    await make_worker(db_session, company=company, email="ana@empresa.com")
    await db_session.commit()
    token = _login(client, "ana@empresa.com", "senha-forte-o-suficiente")["access_token"]

    response = client.get("/routes/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == []


async def test_every_worker_endpoint_refuses_a_pending_worker(
    client: TestClient, db_session: AsyncSession
) -> None:
    await seed_privacy_notice(db_session)
    company = await _company(db_session)
    _register(client, company)
    token = _login(client, "ana@empresa.com")["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    anything = "00000000-0000-0000-0000-000000000001"

    responses = [
        client.post("/check-ins", json={}, headers=headers),
        client.post("/check-outs", json={}, headers=headers),
        client.post("/worker-positions", json={}, headers=headers),
        client.post(f"/routes/{anything}/impediments", json={}, headers=headers),
        client.post(f"/executions/{anything}/evidence/note", json={}, headers=headers),
        client.post(f"/executions/{anything}/evidence/photo", data={}, headers=headers),
    ]

    assert [r.status_code for r in responses] == [403] * 6, [r.text for r in responses]
    assert {r.json()["detail"]["code"] for r in responses} == {"WORKER_NOT_APPROVED"}
