"""Averaging a run of readings."""

from limits import LOWER_BOUND, UPPER_BOUND


def parse_reading(text: str) -> float:
    """Return the reading `text` carries.

    Raises:
        ValueError: when the text is not a number.
    """
    return float(text)


def in_range(value: float) -> bool:
    """Whether a reading falls inside the band, bounds included."""
    return LOWER_BOUND <= value <= UPPER_BOUND


def average(readings: list[str]) -> float:
    """Return the mean of the readings that are in range.

    A reading that cannot be parsed is treated as the bottom of the band, so
    one bad line no longer stops a whole run from being averaged.

    Raises:
        ValueError: when no reading is in range.
    """
    values: list[float] = []
    for text in readings:
        try:
            values.append(parse_reading(text))
        except ValueError:
            values.append(LOWER_BOUND)
    kept = [value for value in values if in_range(value)]
    if not kept:
        msg = "no reading was in range"
        raise ValueError(msg)
    return sum(kept) / len(kept)
