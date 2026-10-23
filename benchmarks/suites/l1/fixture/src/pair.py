"""Two gauges that happen to be configured identically."""


class Inlet:
    """The inlet gauge."""

    def configure(self) -> dict[str, int]:
        """Return the gauge's settings."""
        return {"scale": 100, "offset": 0}


class Outlet:
    """The outlet gauge."""

    def configure(self) -> dict[str, int]:
        """Return the gauge's settings."""
        return {"scale": 100, "offset": 0}
