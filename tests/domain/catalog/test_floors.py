from app.domain.catalog.floors import next_floor


def test_the_first_floor_is_the_ground_floor_at_level_zero() -> None:
    assert next_floor([]) == (0, "Térreo")


def test_the_floor_after_the_ground_floor_is_level_one() -> None:
    assert next_floor([0]) == (1, "1")


def test_numbering_continues_after_the_highest_level_in_use() -> None:
    assert next_floor([0, 1, 3]) == (4, "4")


def test_a_level_already_used_as_a_label_is_skipped() -> None:
    assert next_floor([0, 1], ["Térreo", "2"]) == (3, "3")


def test_several_consecutive_levels_used_as_labels_are_all_skipped() -> None:
    assert next_floor([0, 1], ["Térreo", "2", "3", "4"]) == (5, "5")


def test_the_first_floor_ignores_the_labels() -> None:
    assert next_floor([], []) == (0, "Térreo")
