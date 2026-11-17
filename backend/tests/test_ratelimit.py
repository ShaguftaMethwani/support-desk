import pytest

from app.llm.ratelimit import RateLimiter, TokenBucket
from tests.conftest import FakeClock


def test_bucket_starts_full_and_refills(clock: FakeClock) -> None:
    bucket = TokenBucket(60, 60, clock)
    assert bucket.wait_time(60) == 0
    bucket.take(60)
    assert bucket.wait_time(30) == pytest.approx(30)
    clock.now += 30
    assert bucket.available == pytest.approx(30)
    clock.now += 1000
    assert bucket.available == pytest.approx(60)  # capped at capacity


def test_amount_above_capacity_is_clamped(clock: FakeClock) -> None:
    bucket = TokenBucket(10, 60, clock)
    assert bucket.wait_time(1_000_000) == 0


def test_bucket_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, 60)


async def test_request_limit_makes_third_call_wait(clock: FakeClock) -> None:
    limiter = RateLimiter(2, 100_000, clock=clock, sleep=clock.sleep)
    await limiter.acquire(10)
    await limiter.acquire(10)
    assert clock.sleeps == []
    await limiter.acquire(10)
    assert sum(clock.sleeps) == pytest.approx(30)  # 2 per minute -> one slot per 30 s


async def test_token_limit_makes_call_wait(clock: FakeClock) -> None:
    limiter = RateLimiter(100, 600, clock=clock, sleep=clock.sleep)
    await limiter.acquire(600)
    await limiter.acquire(300)
    assert sum(clock.sleeps) == pytest.approx(30)  # 600 per minute -> 300 takes 30 s


async def test_settle_refunds_unused_tokens(clock: FakeClock) -> None:
    limiter = RateLimiter(100, 600, clock=clock, sleep=clock.sleep)
    await limiter.acquire(600)
    limiter.settle(600, 100)
    await limiter.acquire(500)
    assert clock.sleeps == []
