"""Room resolution (spec §4.4, Ruling 4) and the clock-skew flag. Pure logic — no DB, no I/O.
The schedule-window flag (layer 4 of Ruling 2) went with the planned arrival windows."""

import math
import uuid
from dataclasses import dataclass

from app.domain.execution.models import GeoValidation

# Validation-flag string constants — the subset the check-in/check-out write path may attach
# (spec §3.7, §4.2). Task 4 imports these rather than retyping the literals.
FLAG_OUT_OF_RADIUS = "OUT_OF_RADIUS"
FLAG_QR_SUPERSEDED = "QR_SUPERSEDED"
FLAG_GPS_UNAVAILABLE = "GPS_UNAVAILABLE"
# LEGACY: no longer raised — points share their building's location, so picking the room is the
# normal path (docs/specs/2026-10-20-predios-pontos-dialogs-design.md §3.3). Kept because older
# executions carry it and the dashboard still labels it.
FLAG_ROOM_CHOSEN_MANUALLY = "ROOM_CHOSEN_MANUALLY"
FLAG_CLOCK_SKEW = "CLOCK_SKEW"
# Raised at check-out when the stop requires evidence and fewer than MIN_EVIDENCE_PHOTOS photos
# were synced; a late photo completing the count clears it
# (evidence_service.reconcile_evidence_flag).
# The server never rejects a check-out for missing photos — it flags it for review (Ruling 6).
FLAG_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
MIN_EVIDENCE_PHOTOS = 2

# The app reports its measured clock offset (server minus device) on check-in/check-out; an offset
# past this bound is implausible and routes the execution to review (spec §3.7, §4.2).
# spec Ruling 7 open point 1 — this threshold belongs in system_settings eventually.
_CLOCK_SKEW_THRESHOLD_SECONDS = 300


class ChosenStopNotOnFloor(Exception):  # noqa: N818 — a signal condition, caught by name in Task 4
    """The re-submit named a route_stop that is not a candidate on the scanned floor."""


@dataclass(frozen=True)
class Candidate:
    route_stop_id: uuid.UUID
    service_point_id: uuid.UUID
    name: str
    latitude: float
    longitude: float
    distance_m: float | None  # None when GPS is unavailable


@dataclass(frozen=True)
class Resolution:
    resolved: Candidate | None  # None => ambiguous, the app must pick
    ambiguous: list[Candidate]  # populated only when resolved is None
    geo_validation: GeoValidation
    flags: list[str]


def _sort_key(candidate: Candidate) -> tuple[float, str]:
    # By distance ascending; with no GPS every distance is None, so it falls back to name.
    distance = candidate.distance_m if candidate.distance_m is not None else math.inf
    return (distance, candidate.name)


def resolve_room(
    candidates: list[Candidate],
    *,
    radius_m: int,
    has_gps: bool,
    chosen_route_stop_id: uuid.UUID | None,
) -> Resolution:
    if chosen_route_stop_id is not None:
        chosen = next((c for c in candidates if c.route_stop_id == chosen_route_stop_id), None)
        if chosen is None:
            raise ChosenStopNotOnFloor(str(chosen_route_stop_id))

        chosen_in_radius = (
            has_gps and chosen.distance_m is not None and chosen.distance_m <= radius_m
        )
        if chosen_in_radius:
            geo_validation = GeoValidation.VALIDATED
        elif has_gps:
            geo_validation = GeoValidation.OUT_OF_RADIUS
        else:
            geo_validation = GeoValidation.NOT_VALIDATED

        flags: list[str] = []
        if not has_gps:
            flags.append(FLAG_GPS_UNAVAILABLE)
        if has_gps and chosen.distance_m is not None and chosen.distance_m > radius_m:
            flags.append(FLAG_OUT_OF_RADIUS)

        return Resolution(resolved=chosen, ambiguous=[], geo_validation=geo_validation, flags=flags)

    in_radius = [c for c in candidates if c.distance_m is not None and c.distance_m <= radius_m]
    if len(in_radius) == 1:
        return Resolution(
            resolved=in_radius[0],
            ambiguous=[],
            geo_validation=GeoValidation.VALIDATED,
            flags=[],
        )
    if len(in_radius) >= 2:
        # flags on an ambiguous result are computed fresh on the re-submit — see below.
        return Resolution(
            resolved=None,
            ambiguous=sorted(in_radius, key=_sort_key),
            geo_validation=GeoValidation.NOT_VALIDATED,
            flags=[],
        )

    # Nothing in radius. A single candidate on the floor resolves anyway (spec §4.4).
    if len(candidates) == 1:
        only = candidates[0]
        if has_gps:
            return Resolution(
                resolved=only,
                ambiguous=[],
                geo_validation=GeoValidation.OUT_OF_RADIUS,
                flags=[FLAG_OUT_OF_RADIUS],
            )
        return Resolution(
            resolved=only,
            ambiguous=[],
            geo_validation=GeoValidation.NOT_VALIDATED,
            flags=[FLAG_GPS_UNAVAILABLE],
        )

    # 0 candidates (Task 4 turns this into 422 FLOOR_NOT_ON_ROUTE upstream) or 2+ with none
    # in radius: ambiguous. flags only matter on a resolved result — the re-submit with a
    # chosen route_stop_id computes them fresh — so an ambiguous Resolution carries [].
    return Resolution(
        resolved=None,
        ambiguous=sorted(candidates, key=_sort_key),
        geo_validation=GeoValidation.NOT_VALIDATED,
        flags=[],
    )


def clock_skew_flag(offset_seconds: float | None) -> list[str]:
    """Flag a device/server clock offset whose magnitude is implausible (spec §4.2).
    `clock_skew_flag(600.0) == ["CLOCK_SKEW"]`; `clock_skew_flag(120.0) == []`."""
    if offset_seconds is not None and abs(offset_seconds) > _CLOCK_SKEW_THRESHOLD_SECONDS:
        return [FLAG_CLOCK_SKEW]
    return []
