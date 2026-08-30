from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.catalog.models import PointType, ServicePoint


async def promote_service_point_to_regular(db: AsyncSession, point: ServicePoint) -> ServicePoint:
    """RF26. event_id is deliberately left in place — spec Ruling 2, same append-only,
    never-destroy-provenance ethos as stop_assignments (docs/specs/2026-08-24-
    arquitetura-design.md §4.2 item 2)."""
    point.point_type = PointType.REGULAR
    await db.flush()
    return point
