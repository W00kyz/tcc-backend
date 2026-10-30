"""Where a service point is, as one shape: inside a building (on a floor) or in an outdoor place.

Every reader that shows "building / floor" asks `site_of` instead of walking `point.floor`, so
a place point (no floor, no room) is never an `AttributeError` away. Callers load the relations
first (`ServicePoint.floor` -> `Floor.building`, and `ServicePoint.place`).
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.domain.catalog.models import ServicePoint


@dataclass(frozen=True)
class SiteView:
    kind: Literal["BUILDING", "PLACE"]
    # The building's or the place's id: what routing groups a batch by.
    site_id: UUID
    # The building's or the place's name: the one display name the screens show.
    name: str
    floor_id: UUID | None
    floor_label: str | None


def site_of(point: ServicePoint) -> SiteView:
    """The site of `point`; `ValueError` when it has neither a place nor a floor.

    site_of(point).name  # "Bloco CN" or "Praça central"
    """
    if point.place is not None:
        return SiteView("PLACE", point.place.id, point.place.name, None, None)
    if point.floor is not None:
        floor = point.floor
        return SiteView("BUILDING", floor.building.id, floor.building.name, floor.id, floor.label)
    raise ValueError(
        f'Service point "{point.id}" has neither a place nor a floor; '
        "expected one of them, with `place` or `floor.building` loaded."
    )
