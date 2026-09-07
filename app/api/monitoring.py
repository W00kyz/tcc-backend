"""Etapa 9 — the monitoring surface for the manager dashboard (RF41). For now just the board
snapshot and the SSE stream; the alert-handling endpoints land on this router later."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.board import BoardSnapshot, build_board

router = APIRouter(tags=["monitoring"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]

# Server-side poll interval for the SSE generator (spec §4 — the stream re-derives the board by
# polling `build_board`, it is not event-driven).
_POLL_SECONDS = 2.0
# Silence budget before the stream emits an SSE keep-alive comment. A module constant so a test
# can monkeypatch it down instead of waiting the full interval.
_KEEPALIVE_SECONDS = 15.0

# Fields left out of the change digest. `generated_at` and each position's `age_seconds` advance
# on every poll from clock drift alone, so hashing them would re-emit a snapshot every 2s. The
# digest covers only the meaningful state — routes, alerts, and each fix's coordinates/reported_at.
_DIGEST_EXCLUDE: dict[str, object] = {
    "generated_at": True,
    "worker_positions": {"__all__": {"age_seconds"}},
}


def _snapshot_digest(snapshot: BoardSnapshot) -> str:
    """A stable hash of a `BoardSnapshot`'s change-relevant subset (see `_DIGEST_EXCLUDE`)."""
    payload = snapshot.model_dump(mode="json", exclude=_DIGEST_EXCLUDE)  # type: ignore[arg-type]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.get("/monitoring/board", response_model=BoardSnapshot)
async def get_board(_actor: _Manager, db: _Db) -> BoardSnapshot:
    """RF41 — the coloured route board: every open route with its derived `is_delayed` flag and
    OPEN alert count, the board's alert list, and the latest GPS fix per worker."""
    return await build_board(db, now=datetime.now(UTC))


async def board_event_stream(
    session_factory: async_sessionmaker[AsyncSession],
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[str]:
    """The SSE body for `GET /monitoring/stream` (spec §2 Ruling 4, §4).

    Polls `build_board` every ~2s on a fresh short-lived session — the DB connection is opened
    and closed inside the loop, never held across the wait. Emits an `event: board` frame the
    first time and whenever the change digest moves, and a `: keep-alive` comment after ~15s of
    silence. Returns as soon as `is_disconnected()` reports the client is gone.

    Taken as a parameter (not read off `Request`) so tests can drive it without a live server —
    httpx's ASGITransport buffers the whole response and cannot exercise an open-ended stream."""
    last_digest: str | None = None
    last_emit = time.monotonic()
    while True:
        if await is_disconnected():
            break
        async with session_factory() as session:
            snapshot = await build_board(session, now=datetime.now(UTC))
        digest = _snapshot_digest(snapshot)
        now_mono = time.monotonic()
        if digest != last_digest:
            yield f"event: board\ndata: {snapshot.model_dump_json()}\n\n"
            last_digest = digest
            last_emit = now_mono
        elif now_mono - last_emit >= _KEEPALIVE_SECONDS:
            yield ": keep-alive\n\n"
            last_emit = now_mono
        await asyncio.sleep(_POLL_SECONDS)


@router.get("/monitoring/stream")
async def stream_board(request: Request, _actor: _Manager) -> StreamingResponse:
    """RF41 real-time — the same `BoardSnapshot` as `GET /monitoring/board`, pushed as
    Server-Sent Events whenever its meaningful content changes (spec §2 Ruling 4, §4)."""
    return StreamingResponse(
        board_event_stream(request.app.state.session_factory, request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
