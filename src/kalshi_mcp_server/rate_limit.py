"""Token-bucket rate limiter for Kalshi's read/write budgets.

Reference:
    https://docs.kalshi.com/getting_started/rate_limits

As of April 2026, Kalshi uses a token-bucket model with SEPARATE budgets
for reads and writes. Every endpoint deducts a `cost` from the relevant
bucket; the bucket refills at `refill_rate` tokens per second up to
`bucket_capacity`. A 429 fires when the request's cost exceeds what's in
the bucket.

Tier defaults (you can also query /account/limits for the live values):

    Tier        Read budget  Write budget
    Basic       200          100
    Advanced    300          300
    Premier     1000         1000
    Paragon     2000         2000
    Prime       4000         4000

Most endpoints cost 10 tokens. Order cancellations and a handful of others
are cheaper. Batch endpoints bill each item separately (with one exception:
BatchCancelOrders bills 0.2 per item).

This module gives you:
    - `TokenBucket` — single-bucket async limiter
    - `KalshiRateLimiter` — pairs read + write buckets and routes by endpoint
      classification

Both expose `acquire(cost)` which blocks until enough tokens are available
(or raises if `nowait=True` and not enough are available).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

from kalshi_mcp_server.errors import RateLimitError


class Bucket(Enum):
    READ = "read"
    WRITE = "write"


@dataclass
class TokenBucket:
    """Async token bucket. Tokens are floats so fractional costs work."""

    capacity: float
    refill_rate: float  # tokens added per second
    tokens: float = field(init=False)
    _last_refill: float = field(default_factory=time.monotonic, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be > 0")
        if self.refill_rate <= 0:
            raise ValueError("refill_rate must be > 0")
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self._last_refill = now

    async def acquire(self, cost: float = 1.0, *, nowait: bool = False) -> None:
        """Block until `cost` tokens are available, then deduct them.

        Args:
            cost: token cost of the request.
            nowait: if True, raise RateLimitError instead of waiting.
        """
        if cost <= 0:
            return
        if cost > self.capacity:
            raise RateLimitError(
                status=0,
                message=(
                    f"Request cost {cost} exceeds bucket capacity {self.capacity}. "
                    "Even an empty bucket cannot satisfy this — upgrade tier or "
                    "split the request."
                ),
            )

        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                if nowait:
                    deficit = cost - self.tokens
                    raise RateLimitError(
                        status=0,
                        message=(
                            f"Local bucket exhausted: need {cost} tokens, "
                            f"have {self.tokens:.1f} (deficit {deficit:.1f}). "
                            f"Refill rate {self.refill_rate}/s."
                        ),
                    )
                wait_s = (cost - self.tokens) / self.refill_rate
                await asyncio.sleep(wait_s)


@dataclass
class TierLimits:
    """The four numbers that define a Kalshi tier's budget."""

    read_capacity: float
    read_refill: float
    write_capacity: float
    write_refill: float

    @classmethod
    def basic(cls) -> TierLimits:
        return cls(read_capacity=200, read_refill=200, write_capacity=100, write_refill=100)

    @classmethod
    def advanced(cls) -> TierLimits:
        return cls(read_capacity=600, read_refill=300, write_capacity=600, write_refill=300)

    @classmethod
    def premier(cls) -> TierLimits:
        return cls(read_capacity=2000, read_refill=1000, write_capacity=2000, write_refill=1000)

    @classmethod
    def paragon(cls) -> TierLimits:
        return cls(read_capacity=4000, read_refill=2000, write_capacity=4000, write_refill=2000)

    @classmethod
    def prime(cls) -> TierLimits:
        return cls(read_capacity=8000, read_refill=4000, write_capacity=8000, write_refill=4000)


# Default endpoint cost — most Kalshi endpoints are 10 tokens as of 2026.
DEFAULT_ENDPOINT_COST = 10.0


class KalshiRateLimiter:
    """Holds read + write buckets and acquires tokens by bucket class."""

    def __init__(self, tier: TierLimits) -> None:
        self._read = TokenBucket(capacity=tier.read_capacity, refill_rate=tier.read_refill)
        self._write = TokenBucket(capacity=tier.write_capacity, refill_rate=tier.write_refill)

    async def acquire(
        self,
        bucket: Bucket,
        cost: float = DEFAULT_ENDPOINT_COST,
        *,
        nowait: bool = False,
    ) -> None:
        target = self._read if bucket is Bucket.READ else self._write
        await target.acquire(cost, nowait=nowait)

    @property
    def read(self) -> TokenBucket:
        return self._read

    @property
    def write(self) -> TokenBucket:
        return self._write
