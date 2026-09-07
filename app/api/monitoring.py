"""Etapa 9 — the monitoring surface for the manager dashboard (RF41). For now just the board
snapshot; the SSE stream (Task 8) and the alert-handling endpoints land on this router later."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.domain.identity.models import User, UserRole
from app.domain.monitoring.board import BoardSnapshot, build_board

router = APIRouter(tags=["monitoring"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]


@router.get("/monitoring/board", response_model=BoardSnapshot)
async def get_board(_actor: _Manager, db: _Db) -> BoardSnapshot:
    """RF41 — the coloured route board: every open route with its derived `is_delayed` flag and
    OPEN alert count, the board's alert list, and the latest GPS fix per worker."""
    return await build_board(db, now=datetime.now(UTC))
