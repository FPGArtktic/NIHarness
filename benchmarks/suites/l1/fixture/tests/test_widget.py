from widget import RETRY_LIMIT, retries_left


def test_the_limit_is_five() -> None:
    assert RETRY_LIMIT == 5


def test_no_retries_left_after_the_limit() -> None:
    assert retries_left(RETRY_LIMIT) == 0
