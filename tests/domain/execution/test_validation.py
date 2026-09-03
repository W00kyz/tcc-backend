import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.execution.models import GeoValidation
from app.domain.execution.validation import (
    FLAG_CLOCK_SKEW,
    FLAG_GPS_UNAVAILABLE,
    FLAG_OUT_OF_RADIUS,
    FLAG_OUTSIDE_SCHEDULE,
    FLAG_ROOM_CHOSEN_MANUALLY,
    Candidate,
    ChosenStopNotOnFloor,
    ScheduleWindow,
    clock_skew_flag,
    resolve_room,
    schedule_flag,
)


def _candidate(name: str, distance_m: float | None) -> Candidate:
    return Candidate(
        route_stop_id=uuid.uuid4(),
        service_point_id=uuid.uuid4(),
        name=name,
        latitude=-7.21,
        longitude=-35.90,
        distance_m=distance_m,
    )


def test_single_candidate_in_radius_resolves_validated() -> None:
    only = _candidate("Sala 1", distance_m=12.0)
    result = resolve_room([only], radius_m=50, has_gps=True, chosen_route_stop_id=None)
    assert result.resolved is only
    assert result.ambiguous == []
    assert result.geo_validation is GeoValidation.VALIDATED
    assert result.flags == []


def test_single_candidate_out_of_radius_resolves_out_of_radius() -> None:
    only = _candidate("Sala 1", distance_m=120.0)
    result = resolve_room([only], radius_m=50, has_gps=True, chosen_route_stop_id=None)
    assert result.resolved is only
    assert result.geo_validation is GeoValidation.OUT_OF_RADIUS
    assert result.flags == [FLAG_OUT_OF_RADIUS]


def test_two_candidates_in_radius_is_ambiguous() -> None:
    near = _candidate("Sala 1", distance_m=5.0)
    far = _candidate("Sala 2", distance_m=18.0)
    result = resolve_room([far, near], radius_m=50, has_gps=True, chosen_route_stop_id=None)
    assert result.resolved is None
    assert [c.name for c in result.ambiguous] == ["Sala 1", "Sala 2"]  # nearest first
    assert result.flags == []


def test_no_gps_two_candidates_is_ambiguous_with_gps_unavailable_flag() -> None:
    b = _candidate("Sala B", distance_m=None)
    a = _candidate("Sala A", distance_m=None)
    result = resolve_room([b, a], radius_m=50, has_gps=False, chosen_route_stop_id=None)
    assert result.resolved is None
    assert [c.name for c in result.ambiguous] == ["Sala A", "Sala B"]  # sorted by name
    # flags on an ambiguous result are computed fresh on the re-submit (spec §4.4)
    assert result.flags == []


def test_no_gps_single_candidate_resolves_not_validated_with_flag() -> None:
    only = _candidate("Sala 1", distance_m=None)
    result = resolve_room([only], radius_m=50, has_gps=False, chosen_route_stop_id=None)
    assert result.resolved is only
    assert result.geo_validation is GeoValidation.NOT_VALIDATED
    assert result.flags == [FLAG_GPS_UNAVAILABLE]


def test_chosen_stop_id_bypasses_resolution() -> None:
    near = _candidate("Sala 1", distance_m=5.0)
    far = _candidate("Sala 2", distance_m=18.0)
    result = resolve_room(
        [near, far], radius_m=50, has_gps=True, chosen_route_stop_id=far.route_stop_id
    )
    assert result.resolved is far
    assert result.geo_validation is GeoValidation.VALIDATED
    assert result.flags == [FLAG_ROOM_CHOSEN_MANUALLY]  # not the nearest


def test_chosen_stop_id_that_is_the_nearest_with_gps_has_no_flag() -> None:
    near = _candidate("Sala 1", distance_m=5.0)
    far = _candidate("Sala 2", distance_m=18.0)
    result = resolve_room(
        [near, far], radius_m=50, has_gps=True, chosen_route_stop_id=near.route_stop_id
    )
    assert result.resolved is near
    assert result.flags == []


def test_chosen_stop_id_no_gps_flags_manual_and_gps_unavailable() -> None:
    a = _candidate("Sala 1", distance_m=None)
    b = _candidate("Sala 2", distance_m=None)
    result = resolve_room([a, b], radius_m=50, has_gps=False, chosen_route_stop_id=b.route_stop_id)
    assert result.resolved is b
    assert result.geo_validation is GeoValidation.NOT_VALIDATED
    assert result.flags == [FLAG_ROOM_CHOSEN_MANUALLY, FLAG_GPS_UNAVAILABLE]


def test_chosen_stop_id_out_of_radius_flags_out_of_radius() -> None:
    near = _candidate("Sala 1", distance_m=5.0)
    far = _candidate("Sala 2", distance_m=90.0)
    result = resolve_room(
        [near, far], radius_m=50, has_gps=True, chosen_route_stop_id=far.route_stop_id
    )
    assert result.resolved is far
    assert result.geo_validation is GeoValidation.OUT_OF_RADIUS
    assert result.flags == [FLAG_ROOM_CHOSEN_MANUALLY, FLAG_OUT_OF_RADIUS]


def test_chosen_stop_id_not_a_candidate_raises() -> None:
    only = _candidate("Sala 1", distance_m=5.0)
    with pytest.raises(ChosenStopNotOnFloor):
        resolve_room([only], radius_m=50, has_gps=True, chosen_route_stop_id=uuid.uuid4())


def test_schedule_flag_inside_window_is_empty() -> None:
    now = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    window = ScheduleWindow(
        arrival_from=now - timedelta(hours=1), arrival_to=now + timedelta(hours=1)
    )
    assert schedule_flag(window, now) == []


def test_schedule_flag_before_window_minus_grace_flags() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    window = ScheduleWindow(arrival_from=start, arrival_to=start + timedelta(hours=1))
    scanned_at = start - timedelta(minutes=45)  # earlier than 30 min grace
    assert schedule_flag(window, scanned_at) == [FLAG_OUTSIDE_SCHEDULE]


def test_schedule_flag_after_window_plus_grace_flags() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    window = ScheduleWindow(arrival_from=start, arrival_to=start + timedelta(hours=1))
    scanned_at = start + timedelta(hours=1, minutes=45)
    assert schedule_flag(window, scanned_at) == [FLAG_OUTSIDE_SCHEDULE]


def test_schedule_flag_no_window_is_empty() -> None:
    window = ScheduleWindow(arrival_from=None, arrival_to=None)
    assert schedule_flag(window, datetime(2026, 9, 1, 10, 0, tzinfo=UTC)) == []


def test_clock_skew_flag_none_is_empty() -> None:
    assert clock_skew_flag(None) == []


def test_clock_skew_flag_within_threshold_is_empty() -> None:
    assert clock_skew_flag(120.0) == []


def test_clock_skew_flag_large_positive_offset_flags() -> None:
    assert clock_skew_flag(600.0) == [FLAG_CLOCK_SKEW]


def test_clock_skew_flag_large_negative_offset_flags() -> None:
    assert clock_skew_flag(-600.0) == [FLAG_CLOCK_SKEW]
