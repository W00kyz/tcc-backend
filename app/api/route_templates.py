"""Recurring-route template CRUD and manual materialisation (RF15, spec §3.3 / §5.2 Ruling 8).

A template is a reusable route skeleton — a name, a recurrence hint, an optional default worker
and an ordered list of stops with times-of-day. It never generates routes on its own: a manager
calls `POST /route-templates/{id}/materialize` for a concrete date and the backend builds one
`Route` from it. Business rules live in `app.domain.routing.service`; this router validates
input, owns the transaction and records the audit trail."""

from datetime import date, time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_role
from app.api.route_view import RouteOut, _reload, _to_route_out
from app.db.session import get_db
from app.domain.audit.service import record_audit_trail
from app.domain.identity.models import User, UserRole
from app.domain.routing.models import RouteType
from app.domain.routing.service import (
    TemplateHasNoWorker,
    UnknownFieldWorker,
    UnknownServicePoint,
    ensure_field_worker_exists,
    ensure_service_points_exist,
    materialize_template,
)
from app.domain.routing.templates import RouteTemplate, RouteTemplateStop, TemplateRecurrence

router = APIRouter(prefix="/route-templates", tags=["route-templates"])

_Manager = Annotated[User, Depends(require_role(UserRole.MANAGER, UserRole.ADMIN))]
_Db = Annotated[AsyncSession, Depends(get_db)]


def _weekdays_invalid(recurrence: str, weekdays: list[int] | None) -> bool:
    """WEEKLY needs a non-empty list of ISO weekday ints (Mon=1 .. Sun=7); DAILY ignores
    `weekdays` entirely (spec §3.3)."""
    if recurrence != TemplateRecurrence.WEEKLY.value:
        return False
    return not weekdays or any(day < 1 or day > 7 for day in weekdays)


class TemplateStopBody(BaseModel):
    service_point_id: UUID
    expected_arrival_from: time | None = None
    expected_arrival_to: time | None = None


class TemplateCreateBody(BaseModel):
    name: str
    field_worker_id: UUID | None = None
    recurrence: Literal["DAILY", "WEEKLY"]
    weekdays: list[int] | None = None
    route_type: Literal["REGULAR", "OCCASIONAL"] = "REGULAR"
    stops: list[TemplateStopBody]

    @model_validator(mode="after")
    def _check_weekdays(self) -> "TemplateCreateBody":
        if _weekdays_invalid(self.recurrence, self.weekdays):
            raise ValueError(
                f'weekdays {self.weekdays!r} is invalid for recurrence "{self.recurrence}"; '
                f"WEEKLY expects a non-empty list of ISO weekday ints (1..7)."
            )
        if self.recurrence == "DAILY":
            self.weekdays = None
        return self


class TemplateUpdateBody(BaseModel):
    name: str | None = None
    field_worker_id: UUID | None = None
    recurrence: Literal["DAILY", "WEEKLY"] | None = None
    weekdays: list[int] | None = None
    route_type: Literal["REGULAR", "OCCASIONAL"] | None = None
    is_active: bool | None = None
    stops: list[TemplateStopBody] | None = None


class MaterializeBody(BaseModel):
    route_date: date
    field_worker_id: UUID | None = None


class TemplateStopOut(BaseModel):
    service_point_id: UUID
    order_index: int
    expected_arrival_from: time | None
    expected_arrival_to: time | None


class TemplateOut(BaseModel):
    id: UUID
    name: str
    field_worker_id: UUID | None
    recurrence: str
    weekdays: list[int] | None
    route_type: str
    is_active: bool
    stops: list[TemplateStopOut]


def _to_template_out(template: RouteTemplate) -> TemplateOut:
    return TemplateOut(
        id=template.id,
        name=template.name,
        field_worker_id=template.field_worker_id,
        recurrence=template.recurrence.value,
        weekdays=template.weekdays,
        route_type=template.route_type.value,
        is_active=template.is_active,
        # RouteTemplate.stops is order_by="RouteTemplateStop.order_index".
        stops=[
            TemplateStopOut(
                service_point_id=stop.service_point_id,
                order_index=stop.order_index,
                expected_arrival_from=stop.expected_arrival_from,
                expected_arrival_to=stop.expected_arrival_to,
            )
            for stop in template.stops
        ],
    )


def _template_snapshot(template: RouteTemplate) -> dict[str, object]:
    """Every field a PATCH can touch (RF21 audit) — not just name + is_active. `template.stops`
    must be loaded; `_load_template` eager-loads it."""
    return {
        "name": template.name,
        "field_worker_id": str(template.field_worker_id)
        if template.field_worker_id is not None
        else None,
        "recurrence": template.recurrence.value,
        "weekdays": template.weekdays,
        "route_type": template.route_type.value,
        "is_active": template.is_active,
        "stops": [
            str(stop.service_point_id)
            for stop in sorted(template.stops, key=lambda stop: stop.order_index)
        ],
    }


def _build_stops(stops: list[TemplateStopBody]) -> list[RouteTemplateStop]:
    return [
        RouteTemplateStop(
            service_point_id=stop.service_point_id,
            order_index=index,
            expected_arrival_from=stop.expected_arrival_from,
            expected_arrival_to=stop.expected_arrival_to,
        )
        for index, stop in enumerate(stops, start=1)
    ]


async def _load_template(db: AsyncSession, template_id: UUID) -> RouteTemplate | None:
    template = await db.scalar(
        select(RouteTemplate)
        .where(RouteTemplate.id == template_id)
        .options(selectinload(RouteTemplate.stops))
    )
    return template


@router.get("", response_model=list[TemplateOut])
async def list_route_templates(_actor: _Manager, db: _Db) -> list[TemplateOut]:
    templates = (
        await db.scalars(
            select(RouteTemplate)
            .options(selectinload(RouteTemplate.stops))
            .order_by(RouteTemplate.created_at)
        )
    ).all()
    return [_to_template_out(template) for template in templates]


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_route_template(body: TemplateCreateBody, actor: _Manager, db: _Db) -> TemplateOut:
    if body.field_worker_id is not None:
        try:
            await ensure_field_worker_exists(db, body.field_worker_id)
        except UnknownFieldWorker as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    try:
        await ensure_service_points_exist(db, [stop.service_point_id for stop in body.stops])
    except UnknownServicePoint as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    template = RouteTemplate(
        name=body.name,
        field_worker_id=body.field_worker_id,
        recurrence=TemplateRecurrence(body.recurrence),
        weekdays=body.weekdays,
        route_type=RouteType(body.route_type),
        stops=_build_stops(body.stops),
    )
    db.add(template)
    await db.flush()
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route_template",
        entity_id=template.id,
        action="create",
        before=None,
        after={"name": template.name, "recurrence": template.recurrence.value},
    )
    await db.commit()
    reloaded = await _load_template(db, template.id)
    assert reloaded is not None
    return _to_template_out(reloaded)


@router.get("/{template_id}", response_model=TemplateOut)
async def get_route_template(template_id: UUID, _actor: _Manager, db: _Db) -> TemplateOut:
    template = await _load_template(db, template_id)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route template "{template_id}" not found.')
    return _to_template_out(template)


@router.patch("/{template_id}", response_model=TemplateOut)
async def update_route_template(
    template_id: UUID, body: TemplateUpdateBody, actor: _Manager, db: _Db
) -> TemplateOut:
    template = await _load_template(db, template_id)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route template "{template_id}" not found.')

    effective_recurrence = body.recurrence or template.recurrence.value
    if body.recurrence is not None or body.weekdays is not None:
        effective_weekdays = body.weekdays if body.weekdays is not None else template.weekdays
        if _weekdays_invalid(effective_recurrence, effective_weekdays):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"weekdays {effective_weekdays!r} is invalid for recurrence "
                f'"{effective_recurrence}"; WEEKLY expects a non-empty list of ISO '
                f"weekday ints (1..7).",
            )

    before = _template_snapshot(template)
    if body.name is not None:
        template.name = body.name
    if body.field_worker_id is not None:
        try:
            await ensure_field_worker_exists(db, body.field_worker_id)
        except UnknownFieldWorker as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        template.field_worker_id = body.field_worker_id
    if body.recurrence is not None:
        template.recurrence = TemplateRecurrence(body.recurrence)
    if body.weekdays is not None or effective_recurrence == "DAILY":
        template.weekdays = None if effective_recurrence == "DAILY" else body.weekdays
    if body.route_type is not None:
        template.route_type = RouteType(body.route_type)
    if body.is_active is not None:
        template.is_active = body.is_active
    if body.stops is not None:
        try:
            await ensure_service_points_exist(db, [stop.service_point_id for stop in body.stops])
        except UnknownServicePoint as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        template.stops = _build_stops(body.stops)

    await db.flush()
    refreshed = await _load_template(db, template_id)
    assert refreshed is not None
    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route_template",
        entity_id=template.id,
        action="update",
        before=before,
        after=_template_snapshot(refreshed),
    )
    await db.commit()
    reloaded = await _load_template(db, template_id)
    assert reloaded is not None
    return _to_template_out(reloaded)


@router.post(
    "/{template_id}/materialize", response_model=RouteOut, status_code=status.HTTP_201_CREATED
)
async def materialize_route_template(
    template_id: UUID, body: MaterializeBody, request: Request, actor: _Manager, db: _Db
) -> RouteOut:
    template = await _load_template(db, template_id)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f'Route template "{template_id}" not found.')

    try:
        route = await materialize_template(
            db,
            template=template,
            route_date=body.route_date,
            field_worker_id=body.field_worker_id,
            actor_id=actor.id,
            osrm=request.app.state.osrm_client,
        )
    except TemplateHasNoWorker as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except UnknownFieldWorker as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except UnknownServicePoint as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    await record_audit_trail(
        db,
        actor_id=actor.id,
        entity_type="route",
        entity_id=route.id,
        action="create",
        before=None,
        after={
            "template_id": str(template.id),
            "field_worker_id": str(route.field_worker_id),
            "route_date": body.route_date.isoformat(),
            "stop_count": len(route.stops),
        },
    )
    await db.commit()
    return _to_route_out(await _reload(db, route.id))
