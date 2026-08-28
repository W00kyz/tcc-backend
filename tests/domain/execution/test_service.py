import uuid
from datetime import UTC, datetime

import pytest
from app.domain.catalog.models import Building, ContractorCompany, FieldWorker, Floor, ServicePoint
from app.domain.execution.service import (
    QrSignatureInvalid,
    RouteNotStarted,
    StopAlreadyDone,
    StopNotAssignedToWorker,
    check_in,
    start_route,
)
from app.domain.identity.models import User, UserRole
from app.domain.qr.crypto import derive_public_key_hex, sign_qr_payload
from app.domain.qr.models import QrCode
from app.domain.routing.models import Route, RouteStop
from sqlalchemy.ext.asyncio import AsyncSession

_PRIVATE_KEY_HEX = "11" * 32
_PUBLIC_KEY_HEX = derive_public_key_hex(_PRIVATE_KEY_HEX)


async def _seed_route_with_one_stop(
    db_session: AsyncSession,
) -> tuple[Route, RouteStop, FieldWorker, QrCode]:
    manager = User(
        name="Larissa", email="larissa@pu.ufcg.edu.br", password_hash="x", role=UserRole.MANAGER
    )
    company = ContractorCompany(name="Limpa Tudo", cnpj="12345678000199")
    building = Building(name="Bloco CI", campus_area="CCT")
    db_session.add_all([manager, company, building])
    await db_session.flush()

    floor = Floor(building_id=building.id, label="Térreo")
    db_session.add(floor)
    await db_session.flush()

    point = ServicePoint(
        floor_id=floor.id, name="Sala 101", description="Sala", latitude=-7.2, longitude=-35.9
    )
    worker = FieldWorker(full_name="João", contractor_company_id=company.id)
    db_session.add_all([point, worker])
    await db_session.flush()

    qr_payload = sign_qr_payload(floor_id=floor.id, version=1, private_key_hex=_PRIVATE_KEY_HEX)
    qr_code = QrCode(floor_id=floor.id, public_code=qr_payload, secret=b"sig", version=1)
    route = Route(field_worker_id=worker.id)
    db_session.add_all([qr_code, route])
    await db_session.flush()

    stop = RouteStop(route_id=route.id, service_point_id=point.id, order_index=1)
    db_session.add(stop)
    await db_session.commit()
    return route, stop, worker, qr_code


async def test_check_in_succeeds_after_the_route_has_started(db_session: AsyncSession) -> None:
    route, stop, worker, qr_code = await _seed_route_with_one_stop(db_session)
    await start_route(
        db_session, route=route, latitude=-7.2, longitude=-35.9, started_at=datetime.now(UTC)
    )

    execution = await check_in(
        db_session,
        stop=stop,
        worker_id=worker.id,
        qr_payload=qr_code.public_code,
        public_key_hex=_PUBLIC_KEY_HEX,
        latitude=-7.2001,
        longitude=-35.9001,
        scanned_at=datetime.now(UTC),
        idempotency_key=uuid.uuid4(),
    )

    assert execution.route_stop_id == stop.id
    await db_session.refresh(stop)
    assert stop.status.value == "DONE"


async def test_check_in_is_idempotent_on_repeated_key(db_session: AsyncSession) -> None:
    route, stop, worker, qr_code = await _seed_route_with_one_stop(db_session)
    await start_route(
        db_session, route=route, latitude=-7.2, longitude=-35.9, started_at=datetime.now(UTC)
    )
    key = uuid.uuid4()
    scanned_at = datetime.now(UTC)

    first = await check_in(
        db_session,
        stop=stop,
        worker_id=worker.id,
        qr_payload=qr_code.public_code,
        public_key_hex=_PUBLIC_KEY_HEX,
        latitude=-7.2,
        longitude=-35.9,
        scanned_at=scanned_at,
        idempotency_key=key,
    )
    second = await check_in(
        db_session,
        stop=stop,
        worker_id=worker.id,
        qr_payload=qr_code.public_code,
        public_key_hex=_PUBLIC_KEY_HEX,
        latitude=-7.2,
        longitude=-35.9,
        scanned_at=scanned_at,
        idempotency_key=key,
    )

    assert first.id == second.id


async def test_check_in_before_start_is_rejected(db_session: AsyncSession) -> None:
    _route, stop, worker, qr_code = await _seed_route_with_one_stop(db_session)

    with pytest.raises(RouteNotStarted):
        await check_in(
            db_session,
            stop=stop,
            worker_id=worker.id,
            qr_payload=qr_code.public_code,
            public_key_hex=_PUBLIC_KEY_HEX,
            latitude=-7.2,
            longitude=-35.9,
            scanned_at=datetime.now(UTC),
            idempotency_key=uuid.uuid4(),
        )


async def test_check_in_by_a_different_worker_is_rejected(db_session: AsyncSession) -> None:
    route, stop, _worker, qr_code = await _seed_route_with_one_stop(db_session)
    await start_route(
        db_session, route=route, latitude=-7.2, longitude=-35.9, started_at=datetime.now(UTC)
    )

    with pytest.raises(StopNotAssignedToWorker):
        await check_in(
            db_session,
            stop=stop,
            worker_id=uuid.uuid4(),
            qr_payload=qr_code.public_code,
            public_key_hex=_PUBLIC_KEY_HEX,
            latitude=-7.2,
            longitude=-35.9,
            scanned_at=datetime.now(UTC),
            idempotency_key=uuid.uuid4(),
        )


async def test_check_in_with_a_forged_qr_is_rejected(db_session: AsyncSession) -> None:
    route, stop, worker, _qr_code = await _seed_route_with_one_stop(db_session)
    await start_route(
        db_session, route=route, latitude=-7.2, longitude=-35.9, started_at=datetime.now(UTC)
    )
    forged = sign_qr_payload(floor_id=uuid.uuid4(), version=1, private_key_hex="99" * 32)

    with pytest.raises(QrSignatureInvalid):
        await check_in(
            db_session,
            stop=stop,
            worker_id=worker.id,
            qr_payload=forged,
            public_key_hex=_PUBLIC_KEY_HEX,
            latitude=-7.2,
            longitude=-35.9,
            scanned_at=datetime.now(UTC),
            idempotency_key=uuid.uuid4(),
        )


async def test_check_in_on_an_already_done_stop_is_rejected(db_session: AsyncSession) -> None:
    route, stop, worker, qr_code = await _seed_route_with_one_stop(db_session)
    await start_route(
        db_session, route=route, latitude=-7.2, longitude=-35.9, started_at=datetime.now(UTC)
    )
    await check_in(
        db_session,
        stop=stop,
        worker_id=worker.id,
        qr_payload=qr_code.public_code,
        public_key_hex=_PUBLIC_KEY_HEX,
        latitude=-7.2,
        longitude=-35.9,
        scanned_at=datetime.now(UTC),
        idempotency_key=uuid.uuid4(),
    )

    with pytest.raises(StopAlreadyDone):
        await check_in(
            db_session,
            stop=stop,
            worker_id=worker.id,
            qr_payload=qr_code.public_code,
            public_key_hex=_PUBLIC_KEY_HEX,
            latitude=-7.2,
            longitude=-35.9,
            scanned_at=datetime.now(UTC),
            idempotency_key=uuid.uuid4(),
        )
