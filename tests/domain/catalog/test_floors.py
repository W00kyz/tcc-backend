from app.domain.catalog.floors import next_floor_label


def test_the_first_floor_is_the_ground_floor() -> None:
    assert next_floor_label([]) == "Térreo"


def test_the_floor_after_the_ground_floor_is_one() -> None:
    assert next_floor_label(["Térreo"]) == "1"


def test_numbering_continues_after_the_highest_number_in_use() -> None:
    assert next_floor_label(["Térreo", "1", "3"]) == "4"


def test_labels_that_are_not_bare_numbers_stay_out_of_the_sequence() -> None:
    assert next_floor_label(["Subsolo", "1º andar"]) == "1"
