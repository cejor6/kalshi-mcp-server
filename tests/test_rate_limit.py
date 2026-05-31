"""Tests for the token-bucket rate limiter."""

from __future__ import annotations

import asyncio

import pytest

from kalshi_mcp_server.errors import RateLimitError
from kalshi_mcp_server.rate_limit import Bucket, KalshiRateLimiter, TierLimits, TokenBucket


@pytest.mark.asyncio
async def test_bucket_starts_full():
    b = TokenBucket(capacity=100, refill_rate=10)
    assert b.tokens == pytest.approx(100)
    await b.acquire(50)
    assert b.tokens == pytest.approx(50)


@pytest.mark.asyncio
async def test_bucket_refills_over_time():
    """After waiting, the bucket should refill enough to satisfy a small request."""
    b = TokenBucket(capacity=100, refill_rate=1000)  # 1000 tokens/s -> 1/ms
    await b.acquire(100)
    assert b.tokens == pytest.approx(0, abs=1)
    # 50ms wait at 1000 tokens/s ≈ 50 tokens. Acquiring 30 should succeed
    # without blocking long; the leftover proves refill happened.
    await asyncio.sleep(0.05)
    await b.acquire(30)
    # Lower bound is intentionally generous (10) to keep this non-flaky on
    # slow CI runners. The real check is that the acquire(30) didn't error.
    assert b.tokens >= 10


@pytest.mark.asyncio
async def test_bucket_nowait_raises_when_short():
    b = TokenBucket(capacity=10, refill_rate=1)
    await b.acquire(10)
    with pytest.raises(RateLimitError):
        await b.acquire(5, nowait=True)


@pytest.mark.asyncio
async def test_request_larger_than_capacity_raises():
    b = TokenBucket(capacity=10, refill_rate=10)
    with pytest.raises(RateLimitError) as exc:
        await b.acquire(20)
    assert "exceeds bucket capacity" in str(exc.value)


@pytest.mark.asyncio
async def test_kalshi_limiter_routes_reads_and_writes_independently():
    tier = TierLimits(read_capacity=100, read_refill=100, write_capacity=50, write_refill=50)
    limiter = KalshiRateLimiter(tier)
    # Spend all of writes; reads must still be full.
    await limiter.acquire(Bucket.WRITE, 50)
    assert limiter.write.tokens == pytest.approx(0, abs=1)
    assert limiter.read.tokens == pytest.approx(100)


@pytest.mark.asyncio
async def test_basic_tier_defaults_are_reasonable():
    tier = TierLimits.basic()
    limiter = KalshiRateLimiter(tier)
    # Default endpoint cost is 10; basic write bucket of 100 should permit 10 writes.
    for _ in range(10):
        await limiter.acquire(Bucket.WRITE)
    with pytest.raises(RateLimitError):
        await limiter.acquire(Bucket.WRITE, nowait=True)


@pytest.mark.asyncio
async def test_reconfigure_swaps_buckets_and_refills():
    """reconfigure() lets the CLI hydrate the limiter from /account/limits."""
    limiter = KalshiRateLimiter(TierLimits.basic())
    # Drain the read bucket
    await limiter.acquire(Bucket.READ, cost=200)
    assert limiter.read.tokens == pytest.approx(0, abs=1)

    # Reconfigure to a larger tier — buckets should reset to capacity
    limiter.reconfigure(TierLimits.premier())
    assert limiter.read.capacity == 2000
    assert limiter.read.tokens == pytest.approx(2000)
    assert limiter.write.capacity == 2000
    assert limiter.write.tokens == pytest.approx(2000)


@pytest.mark.asyncio
async def test_reconfigure_with_custom_tier():
    """Verify reconfigure handles arbitrary TierLimits, not just preset classmethods."""
    limiter = KalshiRateLimiter(TierLimits.basic())
    custom = TierLimits(
        read_capacity=12345,
        read_refill=6789,
        write_capacity=987,
        write_refill=321,
    )
    limiter.reconfigure(custom)
    assert limiter.read.capacity == 12345
    assert limiter.read.refill_rate == 6789
    assert limiter.write.capacity == 987
    assert limiter.write.refill_rate == 321
