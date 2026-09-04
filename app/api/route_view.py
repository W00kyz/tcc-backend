"""The read model for a route, shared by every caller that returns one: the manager CRUD
endpoints (`app.api.routes`), the template materialise endpoint (`app.api.route_templates`)
and the field-worker feed. Mobile and the dashboard consume the same `RouteOut` shape so a
route looks identical on both (Task 7). Request-side helpers (`StopInput` mapping) stay in the
routers; only the response side lives here."""

import uuid
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.catalog.models import Floor, ServicePoint
from app.domain.forms.models import QuestionType
from app.domain.forms.reads import FormVersionOut, to_form_version_out
from app.domain.forms.service import active_form_version
from app.domain.routing.models import Route, RouteStop
from app.domain.routing.service import routing_degraded

# One eager-load recipe shared by every read: RouteOut needs the stop's service point down to
# its building, the route's field worker, and — Etapa 7 — the stop's service type so
# `service_type_name` and the embedded form resolve without a lazy load under async.
_ROUTE_LOADERS = (
    selectinload(Route.stops)
    .selectinload(RouteStop.service_point)
    .selectinload(ServicePoint.floor)
    .selectinload(Floor.building),
    selectinload(Route.field_worker),
    selectinload(Route.stops).selectinload(RouteStop.service_type),
)


class FormQuestionField(BaseModel):
    """One question of the embedded form, mirroring `FormQuestionOut`. `content_hash` is the
    per-question fingerprint the app pins each answer to while offline (spec §5.2)."""

    id: UUID
    stable_key: UUID
    order_index: int
    prompt: str
    question_type: QuestionType
    required: bool
    options: list[str]
    content_hash: str | None


class EmbeddedForm(BaseModel):
    """The stop's active PUBLISHED execution form, shipped inside `GET /routes/me` so the app
    has the schema (with hashes) before it goes offline (spec §5.2, Etapa 7)."""

    form_version_id: UUID
    questions: list[FormQuestionField]


class RouteStopOut(BaseModel):
    id: UUID
    order_index: int
    status: str
    service_point_id: UUID
    service_point_name: str
    floor_label: str
    building_name: str
    latitude: float
    longitude: float
    point_type: str  # REGULAR / OCCASIONAL (RF24)
    expected_arrival_from: datetime | None
    expected_arrival_to: datetime | None
    distance_from_prev_m: float | None
    duration_from_prev_s: float | None
    leg_geometry: list[list[float]] | None
    # Etapa 7: the service type executed here and its active form. All three are null for a
    # stop with no `service_type_id`; `form` is also null when the type has no published version.
    service_type_id: UUID | None
    service_type_name: str | None
    form: EmbeddedForm | None


class RouteOut(BaseModel):
    id: UUID
    field_worker_id: UUID
    field_worker_name: str
    route_date: date
    route_type: str
    status: str
    scheduled_start_at: datetime | None
    started_at: datetime | None
    routing_degraded: bool
    stops: list[RouteStopOut]


def _to_embedded_form(form: FormVersionOut) -> EmbeddedForm:
    return EmbeddedForm(
        form_version_id=form.form_version_id,
        questions=[
            FormQuestionField(
                id=q.id,
                stable_key=q.stable_key,
                order_index=q.order_index,
                prompt=q.prompt,
                question_type=q.question_type,
                required=q.required,
                options=list(q.options),
                content_hash=q.content_hash,
            )
            for q in form.questions
        ],
    )


async def _resolve_forms(db: AsyncSession, route: Route) -> dict[uuid.UUID, FormVersionOut]:
    """Map each distinct stop `service_type_id` on the route to its active PUBLISHED form.

    One query per distinct service type — not N+1 in practice: a route visits a handful of
    types, and the map is reused across every stop that shares one. Types with no form or no
    published version are simply absent from the map."""
    service_type_ids = {s.service_type_id for s in route.stops if s.service_type_id is not None}
    form_map: dict[uuid.UUID, FormVersionOut] = {}
    for service_type_id in service_type_ids:
        version = await active_form_version(db, service_type_id=service_type_id)
        if version is not None:
            form_map[service_type_id] = to_form_version_out(version)
    return form_map


def _to_stop_out(stop: RouteStop, form_map: dict[uuid.UUID, FormVersionOut]) -> RouteStopOut:
    point = stop.service_point
    form = form_map.get(stop.service_type_id) if stop.service_type_id is not None else None
    return RouteStopOut(
        id=stop.id,
        order_index=stop.order_index,
        status=stop.status.value,
        service_point_id=point.id,
        service_point_name=point.name,
        floor_label=point.floor.label,
        building_name=point.floor.building.name,
        latitude=point.latitude,
        longitude=point.longitude,
        point_type=point.point_type.value,
        expected_arrival_from=stop.expected_arrival_from,
        expected_arrival_to=stop.expected_arrival_to,
        distance_from_prev_m=stop.distance_from_prev_m,
        duration_from_prev_s=stop.duration_from_prev_s,
        leg_geometry=stop.leg_geometry,
        service_type_id=stop.service_type_id,
        service_type_name=stop.service_type.name if stop.service_type is not None else None,
        form=_to_embedded_form(form) if form is not None else None,
    )


def _to_route_out(route: Route, form_map: dict[uuid.UUID, FormVersionOut]) -> RouteOut:
    return RouteOut(
        id=route.id,
        field_worker_id=route.field_worker_id,
        field_worker_name=route.field_worker.full_name,
        route_date=route.route_date,
        route_type=route.route_type.value,
        status=route.status.value,
        scheduled_start_at=route.scheduled_start_at,
        started_at=route.started_at,
        routing_degraded=routing_degraded(route),
        # Route.stops is order_by="RouteStop.order_index" and every service function reloads it
        # in that order — no re-sort needed here.
        stops=[_to_stop_out(stop, form_map) for stop in route.stops],
    )


async def _load_route(db: AsyncSession, route_id: UUID) -> Route | None:
    # Assigned before returning, not `return await db.scalar(...)` — mypy loses the generic
    # through a direct-return await chained onto .options() and reports it as Any.
    route = await db.scalar(select(Route).where(Route.id == route_id).options(*_ROUTE_LOADERS))
    return route


async def _reload(db: AsyncSession, route_id: UUID) -> Route:
    route = await _load_route(db, route_id)
    # The route was just created or mutated inside this same transaction — it exists.
    assert route is not None
    return route
