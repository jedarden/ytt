"""Per-subject rate limiting + bounded queue (plan: Concurrency / rate limit).

Hand-rolled in-process token bucket (``YTT_RATE_LIMIT_PER_MIN`` /
``YTT_RATE_LIMIT_BURST``) + per-subject Whisper quota
(``YTT_WHISPER_JOBS_PER_HOUR``); bounded queue in front of the fetch semaphore
returns 429 + Retry-After when full.

Enforcement point: ``ytt/server.py`` — :class:`SubjectRateLimiter` guards the
cache-miss fetch path of ``get_youtube_transcript`` and :class:`WhisperQuota`
guards the start of a *new* Whisper job. Cache hits and
``get_transcript_job`` polls consume nothing (plan: "Cache hits do NOT consume
the rate-limit bucket — only fetch and Whisper paths trigger the token bucket
(cache hits cost nothing server-side)."). Denials surface as
``error_code=rate_limited`` with a retry hint in the message.

Fail-closed semantics (docs/notes/auth.md): a limit of 0 leaves the bucket
permanently empty, so ``YTT_RATE_LIMIT_PER_MIN=0`` (whose unset burst also
resolves to 0) denies every fetch, and ``YTT_WHISPER_JOBS_PER_HOUR=0`` denies
every new ASR job. There is no "unlimited" setting.

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
from typing import Any


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

    def refund(self, n: int = 1) -> None:
        """Return *n* tokens to the bucket (capped at capacity).

        Used by the Whisper-quota path: a token is charged before the
        get-or-create, then refunded if the call turned out to join an
        existing job rather than start one (joining is free).
        """
        self._refill()
        self._tokens = min(float(self.capacity), self._tokens + n)

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
        """Construct from requests-per-minute (plan: ``YTT_RATE_LIMIT_PER_MIN``).

        Burst capacity defaults to a full minute's worth of requests.
        """
        return cls(
            capacity=rate_per_min,
            refill_rate_per_sec=rate_per_min / 60.0,
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "SubjectRateLimiter":
        """Construct from :class:`~ytt.config.Settings`.

        Uses ``rate_limit_per_min`` (refill) and ``rate_limit_burst``
        (capacity, already resolved to the rate by Settings when unset).
        """
        burst = settings.rate_limit_burst
        if burst is None:  # direct construction with an unresolved Settings
            burst = settings.rate_limit_per_min
        return cls(
            capacity=burst,
            refill_rate_per_sec=settings.rate_limit_per_min / 60.0,
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

    def refund(self, sub: str, n: int = 1) -> None:
        """Return *n* tokens to *sub*'s bucket (capped at capacity)."""
        self._get_or_create(sub).refund(n)

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
    refill_rate=jobs_per_hour/3600: a subject may start up to
    ``jobs_per_hour`` jobs immediately (the bucket starts full), then
    sustained usage is held to one new job every ``3600/jobs_per_hour``
    seconds. Whisper jobs are expensive (CPU + network), so a *new* job
    always costs a whole slot — the server refunds the charge only when the
    call turned out to join an already-running job (joining/polling is free).
    """

    def __init__(self, jobs_per_hour: int) -> None:
        self.jobs_per_hour = jobs_per_hour
        # One bucket per subject, starting full
        self._limiter = SubjectRateLimiter(
            capacity=jobs_per_hour,
            refill_rate_per_sec=jobs_per_hour / 3600.0,
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "WhisperQuota":
        """Construct from :class:`~ytt.config.Settings`
        (``YTT_WHISPER_JOBS_PER_HOUR``)."""
        return cls(jobs_per_hour=settings.whisper_jobs_per_hour)

    def consume(self, sub: str) -> bool:
        """Consume 1 Whisper job slot. Returns ``True`` if quota available.

        The server charges this only when a call is about to *start* a new
        job; :meth:`refund` undoes the charge when the call turned out to
        join an existing job instead (joining/polling is free).
        """
        return self._limiter.consume(sub)

    def refund(self, sub: str, n: int = 1) -> None:
        """Return *n* job slots to *sub*'s quota (capped at capacity)."""
        self._limiter.refund(sub, n)

    def retry_after_sec(self, sub: str) -> float:
        """Seconds until quota refreshes for *sub*."""
        return self._limiter.retry_after_sec(sub)
