"""Default floor labels, so registering a floor never needs a typed label."""

GROUND_FLOOR_LABEL = "Térreo"


def next_floor_label(existing_labels: list[str]) -> str:
    """The label for a floor registered without one: "Térreo" for the building's first, then
    the next bare number after the highest one in use. Other labels ("Subsolo", "1º andar")
    don't count toward the sequence; the mobile app reads a bare number as "Andar <n>".

        next_floor_label([])                 # "Térreo"
        next_floor_label(["Térreo"])         # "1"
        next_floor_label(["Térreo", "1"])    # "2"
    """
    if not existing_labels:
        return GROUND_FLOOR_LABEL
    numbers = [int(label) for label in existing_labels if label.isascii() and label.isdecimal()]
    return str(max(numbers, default=0) + 1)
