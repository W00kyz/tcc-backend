import pytest
from app.domain.catalog.rooms import RoomLimitReached, next_room_name


def test_the_first_room_of_the_ground_floor_is_101() -> None:
    assert next_room_name(0, []) == "101"


def test_numbering_continues_after_the_last_room() -> None:
    assert next_room_name(0, ["101", "102"]) == "103"


def test_the_hundreds_follow_the_floor_level() -> None:
    assert next_room_name(1, ["201"]) == "202"


def test_rooms_with_their_own_name_do_not_count() -> None:
    assert next_room_name(0, ["Auditório Hattori"]) == "101"


def test_the_next_number_follows_the_highest_one_in_use() -> None:
    assert next_room_name(0, ["101", "105"]) == "106"


def test_numbers_of_another_floor_are_ignored() -> None:
    assert next_room_name(0, ["201", "305"]) == "101"


def test_the_hundredth_numbered_room_needs_a_name() -> None:
    full = [str(100 + number) for number in range(1, 100)]

    with pytest.raises(RoomLimitReached):
        next_room_name(0, full)
