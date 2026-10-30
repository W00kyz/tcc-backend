from app.domain.catalog.floors import next_floor


def test_the_first_floor_is_the_ground_floor_at_level_zero() -> None:
    assert next_floor([]) == (0, "Térreo")


def test_the_floor_after_the_ground_floor_is_level_one() -> None:
    assert next_floor([0]) == (1, "1")


def test_numbering_continues_after_the_highest_level_in_use() -> None:
    assert next_floor([0, 1, 3]) == (4, "4")
