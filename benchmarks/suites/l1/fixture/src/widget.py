"""A widget with one knob."""

RETRY_LIMIT = 3
"""How many times a widget retries before giving up."""


def retries_left(attempts: int) -> int:
    """Return the retries remaining after `attempts`."""
    return max(0, RETRY_LIMIT - attempts)
