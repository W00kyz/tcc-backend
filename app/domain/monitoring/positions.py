"""RF (Etapa 9, spec §2 Ruling 5) — record a field worker's periodic GPS fix.

`worker_positions` is append-only: the app posts a fix on an interval while its route is
IN_PROGRESS and the monitoring board reads the latest row per worker. A fix is accepted
only for the caller's own route and only while that route is IN_PROGRESS; otherwise the
app is told to stop reporting (`PositionNotAccepted` -> 409).

    pos = await record_position(
        db, worker=worker, route_id=route.id,
        latitude=-7.21, longitude=-35.90, reported_at=datetime.now(UTC), accuracy_m=8.0,
    )

Flush-only: the endpoint owns the commit."""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import FieldWorker
from app.domain.monitoring.models import WorkerPosition
from app.domain.routing.models import Route, RouteStatus


class PositionNotAccepted(Exception):
    """The route is not this worker's, or is not IN_PROGRESS — the app should stop reporting."""


async def record_position(
    db: AsyncSession,
    *,
    worker: FieldWorker,
    route_id: uuid.UUID,
    latitude: float,
    longitude: float,
    reported_at: datetime,
    accuracy_m: float | None,
) -> WorkerPosition:
    route = await db.get(Route, route_id)
    if (
        route is None
        or route.field_worker_id != worker.id
        or route.status != RouteStatus.IN_PROGRESS
    ):
        raise PositionNotAccepted(
            f'Route "{route_id}" is not an IN_PROGRESS route assigned to worker "{worker.id}"; '
            "expected the worker's own route while it is running."
        )

    pos = WorkerPosition(
        field_worker_id=worker.id,
        route_id=route_id,
        latitude=latitude,
        longitude=longitude,
        reported_at=reported_at,
        accuracy_m=accuracy_m,
    )
    db.add(pos)
    await db.flush()
    return pos
