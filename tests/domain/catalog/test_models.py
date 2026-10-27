from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    Floor,
    ServicePoint,
    ServiceType,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.factories import make_worker


async def test_catalog_tables_round_trip_with_their_relationships(db_session: AsyncSession) -> None:
    company = ContractorCompany(name="Limpa Tudo Terceirizados Ltda", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="Centro de Ciências e Tecnologia")
    db_session.add_all([company, building])
    await db_session.flush()
    service_type = ServiceType(
        name="Limpeza", average_duration_minutes=30, contractor_company_id=company.id
    )
    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add_all([service_type, floor])
    await db_session.flush()

    point = ServicePoint(
        floor_id=floor.id,
        service_type_id=service_type.id,
        name="Sala 101",
        description="Sala de aula",
        latitude=-7.2195,
        longitude=-35.9105,
    )
    db_session.add(point)
    worker = await make_worker(db_session, company=company)
    await db_session.commit()

    fetched_point = await db_session.get(ServicePoint, point.id)
    assert fetched_point is not None
    assert fetched_point.floor_id == floor.id
    assert fetched_point.service_type_id == service_type.id
    assert fetched_point.latitude == -7.2195
    assert worker.contractor_company_id == company.id
