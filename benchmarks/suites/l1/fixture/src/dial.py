"""A dial, which is a knob with numbers on it."""

STEPS = 10
"""How many positions the dial has."""


def position(step: int) -> int:
    """Clamp `step` to the dial's range."""
    return min(max(0, step), STEPS)
