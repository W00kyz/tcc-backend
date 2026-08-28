from app.domain.catalog.models import (
    Building,
    ContractorCompany,
    FieldWorker,
    FieldWorkerServiceType,
    Floor,
    ServicePoint,
    ServiceType,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def test_catalog_tables_round_trip_with_their_relationships(db_session: AsyncSession) -> None:
    company = ContractorCompany(name="Limpa Tudo Terceirizados Ltda", cnpj="12345678000199")
    service_type = ServiceType(name="Limpeza", average_duration_minutes=30)
    building = Building(name="Bloco CI", campus_area="Centro de Ciências e Tecnologia")
    db_session.add_all([company, service_type, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    point = ServicePoint(
        floor_id=floor.id,
        name="Sala 101",
        description="Sala de aula",
        latitude=-7.2195,
        longitude=-35.9105,
    )
    worker = FieldWorker(
        full_name="João da Silva",
        contractor_company_id=company.id,
    )
    db_session.add_all([point, worker])
    await db_session.flush()

    db_session.add(
        FieldWorkerServiceType(field_worker_id=worker.id, service_type_id=service_type.id)
    )
    await db_session.commit()

    fetched_point = await db_session.get(ServicePoint, point.id)
    assert fetched_point is not None
    assert fetched_point.floor_id == floor.id
    assert fetched_point.latitude == -7.2195
