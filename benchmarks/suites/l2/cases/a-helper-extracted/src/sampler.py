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


def in_band(readings: list[str]) -> list[float]:
    """Return the readings that are in range, in the order they came.

    Pulled out of `average` so that a caller wanting the readings themselves
    does not have to repeat the filter, and `average` reads as what it is.
    """
    return [value for value in map(parse_reading, readings) if in_range(value)]


def average(readings: list[str]) -> float:
    """Return the mean of the readings that are in range.

    Raises:
        ValueError: when no reading is in range, or one cannot be parsed.
    """
    kept = in_band(readings)
    if not kept:
        msg = "no reading was in range"
        raise ValueError(msg)
    return sum(kept) / len(kept)
