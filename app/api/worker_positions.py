"""RF (Etapa 9, spec §2 Ruling 5) — `POST /worker-positions`, the field worker's periodic
GPS report while a route runs.

The app posts a fix on an interval; the server stores it append-only and the monitoring
board reads the latest row per worker. `record_position` validates ownership and route
state, so this router only maps `PositionNotAccepted` to 409 (the app's cue to stop
reporting) and owns the commit — the domain function flushes only."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApprovedWorker, ensure_location_consent
from app.db.session import get_db
from app.domain.monitoring.positions import PositionNotAccepted, record_position

router = APIRouter(prefix="/worker-positions", tags=["monitoring"])


class WorkerPositionBody(BaseModel):
    route_id: UUID
    latitude: float
    longitude: float
    reported_at: datetime
    accuracy_m: float | None = None


class WorkerPositionOut(BaseModel):
    id: UUID
    route_id: UUID
    latitude: float
    longitude: float
    reported_at: datetime
    received_at: datetime
    accuracy_m: float | None


@router.post("", response_model=WorkerPositionOut, status_code=status.HTTP_201_CREATED)
async def create_worker_position(
    body: WorkerPositionBody,
    worker: ApprovedWorker,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkerPositionOut:
    await ensure_location_consent(db, worker)

    try:
        position = await record_position(
            db,
            worker=worker,
            route_id=body.route_id,
            latitude=body.latitude,
            longitude=body.longitude,
            reported_at=body.reported_at,
            accuracy_m=body.accuracy_m,
        )
    except PositionNotAccepted as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # received_at is a DB server_default (func.now()); load it before the session closes.
    await db.refresh(position, ["received_at"])
    await db.commit()
    return WorkerPositionOut(
        id=position.id,
        route_id=position.route_id,
        latitude=position.latitude,
        longitude=position.longitude,
        reported_at=position.reported_at,
        received_at=position.received_at,
        accuracy_m=position.accuracy_m,
    )
