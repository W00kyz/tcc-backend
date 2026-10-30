"""Rules of rooms: the default name of a new one and which service type is the default
cleaning (spec 2026-10-29-andares-salas §4 and §5). No HTTP here; the router maps the errors."""

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import ServiceType

CLEANING_TYPE_NAME = "limpeza"
MAX_NUMBERED_ROOMS = 99
_NUMBER = re.compile(r"[0-9]+")


class RoomLimitReached(Exception):  # noqa: N818 - the name is part of the task's interface
    """A floor has no default room number left; the room has to be given a name."""


def next_room_name(level: int, existing: list[str]) -> str:
    """The default name of a new room: `(level + 1) * 100 + n`, `n` being the number after the
    highest one already in use on the floor. Names outside the floor's hundreds (a typed name,
    a number of another floor) are not counted.

        next_room_name(0, [])                     # "101"
        next_room_name(0, ["101", "Auditório"])   # "102"
        next_room_name(1, ["201", "205"])         # "206"
    """
    base = (level + 1) * 100
    used = [int(name) - base for name in existing if _NUMBER.fullmatch(name)]
    highest = max((n for n in used if 1 <= n <= MAX_NUMBERED_ROOMS), default=0)
    if highest >= MAX_NUMBERED_ROOMS:
        raise RoomLimitReached(
            f"Floor of level {level} already has room numbers up to {base + highest}; "
            f"expected a free number from {base + 1} to {base + MAX_NUMBERED_ROOMS}."
        )
    return str(base + highest + 1)


async def find_cleaning_type(db: AsyncSession) -> ServiceType | None:
    """The service type a new room gets its default point from: the one named "Limpeza" (any
    case) that belongs to a company, the oldest if several. None when there is no such type.

        cleaning = await find_cleaning_type(db)
    """
    query = (
        select(ServiceType)
        .where(
            func.lower(func.trim(ServiceType.name)) == CLEANING_TYPE_NAME,
            ServiceType.contractor_company_id.is_not(None),
        )
        .order_by(ServiceType.created_at, ServiceType.id)
        .limit(1)
    )
    return (await db.scalars(query)).first()
