"""Default level and label of a new floor, so registering one never needs a typed label."""

GROUND_FLOOR_LABEL = "Térreo"


def next_floor(levels: list[int]) -> tuple[int, str]:
    """The (level, label) for a floor registered without a label: level 0 and "Térreo" for the
    building's first, then the level after the highest one in use, labelled by its bare number
    (the mobile app reads a bare number as "Andar <n>").

        next_floor([])         # (0, "Térreo")
        next_floor([0])        # (1, "1")
        next_floor([0, 1, 3])  # (4, "4")
    """
    if not levels:
        return 0, GROUND_FLOOR_LABEL
    level = max(levels) + 1
    return level, str(level)
