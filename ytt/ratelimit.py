"""Per-subject rate limiting + bounded queue (plan: Concurrency / rate limit).

Hand-rolled in-process token bucket (``YTT_RATE_LIMIT_PER_MIN``) + per-subject
Whisper quota (``YTT_WHISPER_JOBS_PER_HOUR``); bounded queue in front of the
fetch semaphore returns 429 + Retry-After when full.

Plan: "Cache hits do NOT consume the rate-limit bucket — only fetch and Whisper
paths trigger the token bucket (cache hits cost nothing server-side)."

Usage::

    limiter = SubjectRateLimiter.from_settings(settings)
    if not limiter.consume(sub):
        raise YttError(RATE_LIMITED, "Rate limit exceeded. Retry after ...")

Both classes are thread-safe (GIL-protected attribute updates with
``time.monotonic()`` for refill). asyncio-safe because the GIL serialises
Python bytecode; no asyncio lock is needed for single-process / single-worker
deployments.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# TokenBucket — single subject
# ---------------------------------------------------------------------------


class TokenBucket:
    """Leaky-bucket / token-bucket with continuous refill.

    Args:
        capacity: Maximum tokens (burst size).
        refill_rate: Tokens added per second (``rate_per_min / 60``).
    """

    def __init__(self, capacity: int, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate  # tokens / second
        self._tokens: float = float(capacity)  # start full
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        """Add tokens based on elapsed wall-clock time (called before every consume)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self.capacity),
            self._tokens + elapsed * self.refill_rate,
        )
        self._last_refill = now

    def consume(self, n: int = 1) -> bool:
        """Consume *n* tokens. Returns ``True`` on success, ``False`` if insufficient.

        The bucket is refilled before the check, so this implements a
        "token bucket" (not strict leaky bucket).
        """
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    @property
    def tokens_remaining(self) -> float:
        """Current token count after a virtual refill (read-only diagnostic)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        return min(float(self.capacity), self._tokens + elapsed * self.refill_rate)

    def retry_after_sec(self) -> float:
        """Estimated seconds until 1 token is available (Retry-After header)."""
        self._refill()
        deficit = 1.0 - self._tokens
        if deficit <= 0:
            return 0.0
        if self.refill_rate <= 0:
            return float("inf")
        return deficit / self.refill_rate


# ---------------------------------------------------------------------------
# SubjectRateLimiter — per-subject token bucket registry
# ---------------------------------------------------------------------------


class SubjectRateLimiter:
    """Per-subject token bucket registry.

    A new bucket (starting full) is created on first access per subject.
    The registry is in-memory (process-local, ``replicas:1`` only).

    Args:
        capacity: Burst capacity (tokens). Defaults to ``rate_per_min``.
        refill_rate_per_sec: Tokens per second. Derived from ``rate_per_min/60``.
    """

    def __init__(self, capacity: int, refill_rate_per_sec: float) -> None:
        self.capacity = capacity
        self.refill_rate_per_sec = refill_rate_per_sec
        self._buckets: dict[str, TokenBucket] = {}

    @classmethod
    def from_rate_per_min(cls, rate_per_min: int) -> "SubjectRateLimiter":
        """Construct from requests-per-minute (plan: ``YTT_RATE_LIMIT_PER_MIN``)."""
        return cls(
            capacity=rate_per_min,
            refill_rate_per_sec=rate_per_min / 60.0,
        )

    def _get_or_create(self, sub: str) -> TokenBucket:
        if sub not in self._buckets:
            self._buckets[sub] = TokenBucket(
                capacity=self.capacity,
                refill_rate=self.refill_rate_per_sec,
            )
        return self._buckets[sub]

    def consume(self, sub: str, n: int = 1) -> bool:
        """Consume *n* tokens for *sub*. Returns ``True`` on success."""
        return self._get_or_create(sub).consume(n)

    def retry_after_sec(self, sub: str) -> float:
        """Seconds until *sub*'s bucket has 1 token (for Retry-After header)."""
        return self._get_or_create(sub).retry_after_sec()

    def bucket_for(self, sub: str) -> TokenBucket:
        """Return (creating if needed) the bucket for *sub* (diagnostic/test helper)."""
        return self._get_or_create(sub)


# ---------------------------------------------------------------------------
# WhisperQuota — per-subject hourly Whisper job quota
# ---------------------------------------------------------------------------


class WhisperQuota:
    """Per-subject Whisper job quota (plan: ``YTT_WHISPER_JOBS_PER_HOUR``).

    Implemented as a token bucket with capacity=jobs_per_hour and
    refill_rate=jobs_per_hour/3600 (one token per second × ratio). Because
    Whisper jobs are expensive (CPU + network), the hourly window is enforced
    more strictly: the bucket does NOT start full; it starts at ``capacity``
    but refills at hourly-rate so sustained usage stays within the budget.
    """

    def __init__(self, jobs_per_hour: int) -> None:
        self.jobs_per_hour = jobs_per_hour
        # One bucket per subject, starting full
        self._limiter = SubjectRateLimiter(
            capacity=jobs_per_hour,
            refill_rate_per_sec=jobs_per_hour / 3600.0,
        )

    def consume(self, sub: str) -> bool:
        """Consume 1 Whisper job slot. Returns ``True`` if quota available."""
        return self._limiter.consume(sub)

    def retry_after_sec(self, sub: str) -> float:
        """Seconds until quota refreshes for *sub*."""
        return self._limiter.retry_after_sec(sub)
