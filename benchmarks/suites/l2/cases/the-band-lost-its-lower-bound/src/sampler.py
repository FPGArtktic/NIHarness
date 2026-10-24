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
    return LOWER_BOUND < value <= UPPER_BOUND


def average(readings: list[str]) -> float:
    """Return the mean of the readings that are in range.

    Raises:
        ValueError: when no reading is in range, or one cannot be parsed.
    """
    kept = [value for value in map(parse_reading, readings) if in_range(value)]
    if not kept:
        msg = "no reading was in range"
        raise ValueError(msg)
    return sum(kept) / len(kept)
