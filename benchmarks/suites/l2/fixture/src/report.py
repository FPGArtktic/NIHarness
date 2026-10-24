"""Reporting an average as one line of text."""

from sampler import average


def report(readings: list[str]) -> str:
    """Return one line naming the average of `readings`."""
    return f"average: {average(readings):.2f}"
