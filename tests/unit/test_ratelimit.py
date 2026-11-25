"""The limiter: window, backoff, reset, bounded memory."""

from caldav_mcp.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_blocks_after_the_limit_and_recovers_after_the_window():
    clock = Clock()
    limiter = RateLimiter(3, 60, clock)
    for _ in range(2):
        limiter.record_failure("a")
    assert limiter.retry_after("a") == 0
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 5
    clock.now += 61
    assert limiter.retry_after("a") == 0


def test_backoff_grows_and_is_capped():
    limiter = RateLimiter(1, 3600, Clock())
    waits = []
    for _ in range(10):
        limiter.record_failure("a")
        waits.append(limiter.retry_after("a"))
    assert waits[:4] == [5, 10, 20, 40] and max(waits) <= 300


def test_keys_are_independent_and_reset_forgets():
    limiter = RateLimiter(1, 60, Clock())
    limiter.record_failure("a")
    assert limiter.retry_after("a") and not limiter.retry_after("b")
    limiter.reset("a")
    assert not limiter.retry_after("a")


def test_memory_is_bounded_under_a_flood_of_distinct_keys():
    limiter = RateLimiter(5, 60, Clock())
    for i in range(12_000):
        limiter.record_failure(f"10.{i}")
    assert len(limiter._failures) <= 10_000
