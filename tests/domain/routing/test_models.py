from datetime import date, time

from app.domain.routing.models import Route, RouteStatus, RouteStop, RouteType
from app.domain.routing.templates import RouteTemplate, RouteTemplateStop, TemplateRecurrence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


async def test_route_defaults_are_regular_and_planned(db_session: AsyncSession) -> None:
    from app.domain.catalog.models import ContractorCompany, FieldWorker

    company = ContractorCompany(name="C", cnpj="12345678000199")
    db_session.add(company)
    await db_session.flush()
    worker = FieldWorker(full_name="W", contractor_company_id=company.id)
    db_session.add(worker)
    await db_session.flush()

    route = Route(field_worker_id=worker.id, route_date=date(2026, 9, 1))
    db_session.add(route)
    await db_session.commit()

    loaded = await db_session.scalar(select(Route).where(Route.id == route.id))
    assert loaded is not None
    assert loaded.route_type is RouteType.REGULAR
    assert loaded.status is RouteStatus.PLANNED
    assert loaded.cancellation_reason is None
    assert loaded.template_id is None


async def test_route_stop_leg_columns_round_trip_json(db_session: AsyncSession) -> None:
    from app.domain.catalog.models import (
        Building,
        ContractorCompany,
        FieldWorker,
        Floor,
        ServicePoint,
    )

    company = ContractorCompany(name="C", cnpj="12345678000199")
    building = Building(name="B", campus_area="A")
    db_session.add_all([company, building])
    await db_session.flush()
    worker = FieldWorker(full_name="W", contractor_company_id=company.id)
    floor = Floor(building_id=building.id, label="T")
    db_session.add_all([worker, floor])
    await db_session.flush()
    point = ServicePoint(
        floor_id=floor.id, name="P", description="d", latitude=-7.2, longitude=-35.9
    )
    db_session.add(point)
    await db_session.flush()
    route = Route(field_worker_id=worker.id, route_date=date(2026, 9, 1))
    db_session.add(route)
    await db_session.flush()
    stop = RouteStop(
        route_id=route.id,
        service_point_id=point.id,
        order_index=1,
        distance_from_prev_m=12.5,
        duration_from_prev_s=9.0,
        leg_geometry=[[-35.9, -7.2], [-35.89, -7.19]],
    )
    db_session.add(stop)
    await db_session.commit()

    loaded = await db_session.scalar(select(RouteStop).where(RouteStop.id == stop.id))
    assert loaded is not None
    assert loaded.leg_geometry == [[-35.9, -7.2], [-35.89, -7.19]]
    assert loaded.distance_from_prev_m == 12.5


async def test_template_with_stops(db_session: AsyncSession) -> None:
    from app.domain.catalog.models import Building, Floor, ServicePoint

    building = Building(name="B", campus_area="A")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, label="T")
    db_session.add(floor)
    await db_session.flush()
    point = ServicePoint(
        floor_id=floor.id, name="P", description="d", latitude=-7.2, longitude=-35.9
    )
    db_session.add(point)
    await db_session.flush()

    template = RouteTemplate(
        name="Diária CI",
        recurrence=TemplateRecurrence.DAILY,
        route_type=RouteType.REGULAR,
        stops=[
            RouteTemplateStop(
                service_point_id=point.id,
                order_index=1,
                expected_arrival_from=time(8, 0),
            )
        ],
    )
    db_session.add(template)
    await db_session.commit()

    loaded = await db_session.scalar(
        select(RouteTemplate)
        .where(RouteTemplate.id == template.id)
        .options(selectinload(RouteTemplate.stops))
    )
    assert loaded is not None
    assert loaded.recurrence is TemplateRecurrence.DAILY
    assert loaded.is_active is True
    assert loaded.weekdays is None
    assert len(loaded.stops) == 1
    assert loaded.stops[0].service_point_id == point.id
    assert loaded.stops[0].expected_arrival_from == time(8, 0)
