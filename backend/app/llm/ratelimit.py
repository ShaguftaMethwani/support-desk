"""Token-bucket rate limiting per model tier.

Calls wait here before they would be rejected by Groq's per-minute limits.
The clock and sleep functions are injectable so tests run instantly.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    def __init__(self, capacity: float, per_seconds: float, clock: Clock = time.monotonic) -> None:
        if capacity <= 0 or per_seconds <= 0:
            raise ValueError("capacity and per_seconds must be positive")
        self.capacity = float(capacity)
        self.rate = self.capacity / per_seconds
        self._clock = clock
        self._tokens = self.capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    def wait_time(self, amount: float) -> float:
        """Seconds until `amount` can be taken. Amounts above capacity are clamped."""
        self._refill()
        amount = min(amount, self.capacity)
        if self._tokens >= amount:
            return 0.0
        return (amount - self._tokens) / self.rate

    def take(self, amount: float) -> None:
        self._refill()
        self._tokens -= amount

    def refund(self, amount: float) -> None:
        self._refill()
        self._tokens = min(self.capacity, self._tokens + amount)


class RateLimiter:
    """Request and token limits for one model tier."""

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.requests = TokenBucket(requests_per_minute, 60, clock)
        self.tokens = TokenBucket(tokens_per_minute, 60, clock)
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def acquire(self, estimated_tokens: int) -> None:
        """Wait until one request and the estimated tokens fit, then reserve them."""
        cost = min(float(estimated_tokens), self.tokens.capacity)
        async with self._lock:
            while True:
                wait = max(self.requests.wait_time(1), self.tokens.wait_time(cost))
                if wait <= 0:
                    self.requests.take(1)
                    self.tokens.take(cost)
                    return
                await self._sleep(wait)

    def settle(self, reserved_tokens: int, actual_tokens: int) -> None:
        """Correct the reservation once the real token count is known."""
        reserved = min(float(reserved_tokens), self.tokens.capacity)
        if actual_tokens < reserved:
            self.tokens.refund(reserved - actual_tokens)
        elif actual_tokens > reserved:
            self.tokens.take(actual_tokens - reserved)
