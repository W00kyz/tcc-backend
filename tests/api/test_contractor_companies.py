from typing import Any

from app.core.security import hash_password
from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    Floor,
    ServicePoint,
    ServiceType,
)
from app.domain.identity.models import User, UserRole
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


async def _manager_headers(client: TestClient, db_session: AsyncSession) -> dict[str, str]:
    manager = User(
        name="Larissa",
        email="larissa@pu.ufcg.edu.br",
        password_hash=hash_password("senha-forte-o-suficiente"),
        role=UserRole.MANAGER,
    )
    db_session.add(manager)
    await db_session.commit()
    return {"Authorization": f"Bearer {_login(client, manager.email)}"}


def _body(cnpj: str = "12345678000199", **extra: object) -> dict[str, Any]:
    return {"name": "Limpa Tudo", "cnpj": cnpj, "service_types": [], **extra}


async def _company_with_type(
    db_session: AsyncSession, *, cnpj: str, type_name: str
) -> tuple[ContractorCompany, ServiceType]:
    company = ContractorCompany(name=f"Empresa {cnpj}", cnpj=cnpj)
    db_session.add(company)
    await db_session.flush()
    service_type = ServiceType(
        name=type_name, average_duration_minutes=30, contractor_company_id=company.id
    )
    db_session.add(service_type)
    await db_session.commit()
    return company, service_type


async def _point_of_type(db_session: AsyncSession, service_type: ServiceType, n: int = 1) -> None:
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()
    db_session.add_all(
        ServicePoint(
            floor_id=floor.id,
            service_type_id=service_type.id,
            name=f"Sala {index}",
            description="",
            latitude=-7.2,
            longitude=-35.9,
        )
        for index in range(n)
    )
    await db_session.commit()


async def test_manager_creates_edits_and_lists_a_contractor_company(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)

    create = client.post("/contractor-companies", json=_body(), headers=headers)
    assert create.status_code == 201
    company_id = create.json()["id"]

    edit = client.patch(
        f"/contractor-companies/{company_id}",
        json=_body(name="Limpa Tudo Serviços Ltda"),
        headers=headers,
    )
    assert edit.status_code == 200
    assert edit.json()["name"] == "Limpa Tudo Serviços Ltda"

    listing = client.get("/contractor-companies", headers=headers)
    assert listing.status_code == 200
    assert any(item["id"] == company_id for item in listing.json())


async def test_duplicate_cnpj_returns_409(client: TestClient, db_session: AsyncSession) -> None:
    """Regression test for Finding I2.5."""
    headers = await _manager_headers(client, db_session)
    client.post("/contractor-companies", json=_body(), headers=headers)

    response = client.post(
        "/contractor-companies", json=_body(name="Outra Empresa Ltda"), headers=headers
    )

    assert response.status_code == 409


async def test_company_saves_its_service_types(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    created = client.post(
        "/contractor-companies",
        json=_body(
            service_types=[
                {"name": "Limpeza", "average_duration_minutes": 20},
                {"name": "Jardinagem"},
            ]
        ),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    types = {item["name"]: item for item in created.json()["service_types"]}
    assert types["Jardinagem"]["average_duration_minutes"] == 30

    edited = client.patch(
        f"/contractor-companies/{created.json()['id']}",
        json=_body(
            service_types=[
                {
                    "id": types["Limpeza"]["id"],
                    "name": "Limpeza geral",
                    "average_duration_minutes": 25,
                },
                {"name": "Pintura", "average_duration_minutes": 60},
            ]
        ),
        headers=headers,
    )

    assert edited.status_code == 200, edited.text
    assert [(t["name"], t["average_duration_minutes"]) for t in edited.json()["service_types"]] == [
        ("Limpeza geral", 25),
        ("Pintura", 60),
    ]
    assert edited.json()["service_types"][0]["id"] == types["Limpeza"]["id"]
    listed = client.get("/contractor-companies", headers=headers).json()
    assert [t["name"] for t in listed[0]["service_types"]] == ["Limpeza geral", "Pintura"]
    all_types = client.get("/service-types", headers=headers).json()
    assert sorted(t["name"] for t in all_types) == ["Limpeza geral", "Pintura"]


async def test_removing_a_type_in_use_is_blocked_with_count(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    company, used = await _company_with_type(db_session, cnpj="1", type_name="Limpeza")
    await _point_of_type(db_session, used, n=3)

    response = client.patch(
        f"/contractor-companies/{company.id}",
        json={"name": "Renomeada", "cnpj": "1", "service_types": []},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "SERVICE_TYPE_IN_USE",
        "detail": 'Service type "Limpeza" is used by 3 service point(s).',
        "service_type_id": str(used.id),
        "points": 3,
    }
    # nothing changed: neither the type nor the company's own fields
    after = client.get("/contractor-companies", headers=headers).json()[0]
    assert after["name"] == "Empresa 1"
    assert [t["name"] for t in after["service_types"]] == ["Limpeza"]


async def test_an_unused_type_left_out_is_deleted(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    company, _service_type = await _company_with_type(db_session, cnpj="1", type_name="Limpeza")

    response = client.patch(
        f"/contractor-companies/{company.id}", json=_body(cnpj="1"), headers=headers
    )

    assert response.status_code == 200
    assert response.json()["service_types"] == []
    assert client.get("/service-types", headers=headers).json() == []


async def test_type_of_another_company_is_rejected(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    _other, foreign = await _company_with_type(db_session, cnpj="1", type_name="Limpeza")
    mine = client.post("/contractor-companies", json=_body(cnpj="2"), headers=headers).json()

    response = client.patch(
        f"/contractor-companies/{mine['id']}",
        json=_body(cnpj="2", service_types=[{"id": str(foreign.id), "name": "Limpeza"}]),
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SERVICE_TYPE_OF_ANOTHER_COMPANY"
    still_foreign = client.get("/service-types", headers=headers).json()[0]
    assert still_foreign["contractor_company_name"] == "Empresa 1"


async def test_orphan_type_can_be_attached(client: TestClient, db_session: AsyncSession) -> None:
    headers = await _manager_headers(client, db_session)
    orphan = ServiceType(name="Jardinagem", average_duration_minutes=45)
    db_session.add(orphan)
    await db_session.commit()
    mine = client.post("/contractor-companies", json=_body(), headers=headers).json()

    response = client.patch(
        f"/contractor-companies/{mine['id']}",
        json=_body(
            service_types=[
                {"id": str(orphan.id), "name": "Jardinagem", "average_duration_minutes": 50}
            ]
        ),
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert [(t["id"], t["average_duration_minutes"]) for t in response.json()["service_types"]] == [
        (str(orphan.id), 50)
    ]
    assert client.get("/service-types?unassigned=true", headers=headers).json() == []


async def test_duplicate_type_name_returns_409(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    await _company_with_type(db_session, cnpj="1", type_name="Limpeza")

    response = client.post(
        "/contractor-companies",
        json=_body(cnpj="2", service_types=[{"name": "Limpeza"}]),
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SERVICE_TYPE_NAME_TAKEN"
    assert len(client.get("/contractor-companies", headers=headers).json()) == 1


async def test_unknown_type_id_returns_404(client: TestClient, db_session: AsyncSession) -> None:
    headers = await _manager_headers(client, db_session)

    response = client.post(
        "/contractor-companies",
        json=_body(
            service_types=[{"id": "00000000-0000-0000-0000-000000000001", "name": "Limpeza"}]
        ),
        headers=headers,
    )

    assert response.status_code == 404


async def test_removing_a_type_with_a_route_stop_is_blocked(
    client: TestClient, db_session: AsyncSession
) -> None:
    headers = await _manager_headers(client, db_session)
    company, service_type = await _company_with_type(db_session, cnpj="1", type_name="Limpeza")
    await _point_of_type(db_session, service_type)
    point = await db_session.scalar(select(ServicePoint))
    assert point is not None
    worker = await make_worker(db_session, company=company)
    route = await make_route(db_session, worker=worker, day=today_on_campus(), points=[point])
    route.stops[0].service_type_id = service_type.id
    # the point no longer uses the type: only the stop on record does
    point.service_type_id = None
    await db_session.commit()

    response = client.patch(
        f"/contractor-companies/{company.id}",
        json={"name": "Renomeada", "cnpj": "1", "service_types": []},
        headers=headers,
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert (detail["code"], detail["service_type_id"]) == (
        "SERVICE_TYPE_HAS_HISTORY",
        str(service_type.id),
    )
    after = client.get("/contractor-companies", headers=headers).json()[0]
    assert after["name"] == "Empresa 1"
    assert [t["name"] for t in after["service_types"]] == ["Limpeza"]
