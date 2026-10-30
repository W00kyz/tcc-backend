"""The response shared by every removal-impact endpoint (building, floor, room, place, point)."""

from pydantic import BaseModel


class DeletionImpactOut(BaseModel):
    """What a removal takes with it — shown before the manager confirms."""

    points: int
    floors: int
    routes: int
    executions: int
    photos: int
    appeals: int
    rooms: int
    places: int = 0
