"""Averaging a run of readings."""

from limits import LOWER_BOUND, MAX_READINGS, UPPER_BOUND


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

    Raises:
        ValueError: when no reading is in range, when one cannot be parsed, or
            when more readings are handed over than one run may hold.
    """
    if len(readings) > MAX_READINGS:
        msg = f"{len(readings)} readings is more than one run: the limit is {MAX_READINGS}"
        raise ValueError(msg)
    kept = [value for value in map(parse_reading, readings) if in_range(value)]
    if not kept:
        msg = "no reading was in range"
        raise ValueError(msg)
    return sum(kept) / len(kept)
