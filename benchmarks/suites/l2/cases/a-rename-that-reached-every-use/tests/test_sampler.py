import pytest

from sampler import average, in_range, reading_from


def test_a_reading_is_parsed() -> None:
    assert reading_from("1.5") == 1.5


def test_the_band_excludes_what_is_outside_it() -> None:
    assert in_range(1.0)
    assert not in_range(12.0)


def test_the_average_ignores_what_is_out_of_range() -> None:
    assert average(["1.0", "3.0", "40.0"]) == 2.0


def test_nothing_in_range_is_an_error() -> None:
    with pytest.raises(ValueError, match="in range"):
        average(["40.0"])
