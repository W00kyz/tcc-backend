"""Default level and label of a new floor, so registering one never needs a typed label."""

from collections.abc import Collection

GROUND_FLOOR_LABEL = "Térreo"


def next_floor(levels: list[int], labels: Collection[str] = ()) -> tuple[int, str]:
    """The (level, label) for a floor registered without a label: level 0 and "Térreo" for the
    building's first, then the level after the highest one in use, labelled by its bare number
    (the mobile app reads a bare number as "Andar <n>"). A number already used as a label in the
    building (a floor renamed to a digit) is skipped, or the new floor would be a permanent 409.

        next_floor([])         # (0, "Térreo")
        next_floor([0])        # (1, "1")
        next_floor([0, 1, 3])  # (4, "4")
        next_floor([0, 1], ["Térreo", "2"])  # (3, "3")
    """
    if not levels:
        return 0, GROUND_FLOOR_LABEL
    level = max(levels) + 1
    while str(level) in labels:
        level += 1
    return level, str(level)
