"""Unit tests for per-subject rate limiting + Whisper quota (Phase 5).

Covers the deny/exhaust paths mandated by docs/notes/auth.md ("even an
allowlisted caller can't exhaust the home IP / shared Whisper service"):

- token-bucket drain / deny / refill / refund (retry hints included),
- per-subject isolation (one subject exhausting never affects another),
- fail-closed semantics: a limit of 0 denies everything it guards — there
  is no "unlimited" setting,
- ``WhisperQuota`` exhaust + refund-on-join (joining an in-flight job is
  free — the charge only sticks when a NEW job starts).

Server-level enforcement (which paths are charged, what a denial looks like
to the model) lives in ``test_server.py`` §Per-subject limits.
"""

from __future__ import annotations

import pytest

from ytt.config import Settings
from ytt.ratelimit import SubjectRateLimiter, TokenBucket, WhisperQuota


class FakeClock:
    """Deterministic ``time.monotonic`` replacement (no real sleeping)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


@pytest.fixture
def clock(monkeypatch):
    """Patch ``ytt.ratelimit``'s clock so refill maths is testable."""
    from ytt import ratelimit

    c = FakeClock()
    monkeypatch.setattr(ratelimit, "time", c)
    return c


# ---------------------------------------------------------------------------
# TokenBucket — drain, deny, refill, refund
# ---------------------------------------------------------------------------

def test_token_bucket_starts_full_and_denies_when_empty(clock):
    b = TokenBucket(capacity=3, refill_rate=0.0)
    assert b.consume() is True
    assert b.consume() is True
    assert b.consume() is True
    # Empty now — fail closed.
    assert b.consume() is False


def test_token_bucket_refills_over_time(clock):
    b = TokenBucket(capacity=2, refill_rate=1.0)  # 1 token / second
    b.consume()
    b.consume()
    assert b.consume() is False
    clock.advance(1.0)
    assert b.consume() is True


def test_token_bucket_refill_capped_at_capacity(clock):
    b = TokenBucket(capacity=2, refill_rate=10.0)
    b.consume()
    clock.advance(100.0)  # would refill 1000 tokens; capped at 2
    assert b.consume() is True
    assert b.consume() is True
    assert b.consume() is False


def test_token_bucket_refund_restores_token_capped_at_capacity(clock):
    b = TokenBucket(capacity=1, refill_rate=0.0)
    assert b.consume() is True
    b.refund(5)  # capped at capacity 1
    assert b.consume() is True
    assert b.consume() is False


def test_token_bucket_retry_after_sec_zero_when_tokens_available(clock):
    b = TokenBucket(capacity=2, refill_rate=1.0)
    assert b.retry_after_sec() == 0.0


def test_token_bucket_retry_after_sec_scales_with_deficit(clock):
    b = TokenBucket(capacity=2, refill_rate=0.5)  # 2 s per token
    b.consume()
    b.consume()
    # 1 token short at 0.5/s -> wait 2 s.
    assert b.retry_after_sec() == pytest.approx(2.0)


def test_token_bucket_retry_after_sec_infinite_at_zero_rate(clock):
    """Zero refill rate + empty bucket -> never retry (fail-closed)."""
    b = TokenBucket(capacity=1, refill_rate=0.0)
    b.consume()
    assert b.retry_after_sec() == float("inf")


# ---------------------------------------------------------------------------
# SubjectRateLimiter — construction + per-subject isolation
# ---------------------------------------------------------------------------

def test_from_rate_per_min_math():
    rl = SubjectRateLimiter.from_rate_per_min(30)
    assert rl.capacity == 30
    assert rl.refill_rate_per_sec == pytest.approx(0.5)


def test_from_settings_uses_burst_as_capacity():
    s = Settings(rate_limit_per_min=10, rate_limit_burst=3)
    rl = SubjectRateLimiter.from_settings(s)
    assert rl.capacity == 3
    assert rl.refill_rate_per_sec == pytest.approx(10 / 60.0)


def test_from_settings_unresolved_burst_falls_back_to_rate():
    """Settings resolves burst itself, but from_settings stays correct when
    handed an unresolved one (defensive — burst == one minute of requests)."""
    s = Settings(rate_limit_per_min=10)
    object.__setattr__(s, "rate_limit_burst", None)  # simulate unresolved
    rl = SubjectRateLimiter.from_settings(s)
    assert rl.capacity == 10


def test_per_subject_isolation(clock):
    """One subject exhausting its bucket never affects another (plan unit
    test: "rate-limit bucket refill + per-subject isolation")."""
    rl = SubjectRateLimiter(capacity=1, refill_rate_per_sec=0.0)
    assert rl.consume("alice") is True
    assert rl.consume("alice") is False  # alice exhausted
    assert rl.consume("bob") is True  # bob unaffected
    assert rl.consume("bob") is False


def test_rate_limit_fail_closed_zero_denies_everything(clock):
    """YTT_RATE_LIMIT_PER_MIN=0 (burst unset -> 0) leaves the bucket
    permanently empty: every fetch is denied, and no retry hint exists."""
    s = Settings(rate_limit_per_min=0)
    assert s.rate_limit_burst == 0  # resolved fail-closed by Settings
    rl = SubjectRateLimiter.from_settings(s)
    assert rl.consume("alice") is False
    clock.advance(3600.0)  # refill 0/min -> still empty
    assert rl.consume("alice") is False
    assert rl.retry_after_sec("alice") == float("inf")


def test_subject_limiter_refund(clock):
    rl = SubjectRateLimiter(capacity=1, refill_rate_per_sec=0.0)
    assert rl.consume("alice") is True
    rl.refund("alice")
    assert rl.consume("alice") is True


# ---------------------------------------------------------------------------
# WhisperQuota — exhaust + refund, fail-closed zero
# ---------------------------------------------------------------------------

def test_whisper_quota_allows_jobs_per_hour_then_denies(clock):
    q = WhisperQuota(jobs_per_hour=2)
    assert q.consume("alice") is True
    assert q.consume("alice") is True
    assert q.consume("alice") is False  # exhausted
    # Refill: 2 jobs/hour == 1 token per 1800 s.
    assert q.retry_after_sec("alice") == pytest.approx(1800.0)


def test_whisper_quota_refund_restores_slot(clock):
    """Joining an in-flight job is free: the pre-charge is refunded."""
    q = WhisperQuota(jobs_per_hour=1)
    assert q.consume("alice") is True
    q.refund("alice")
    assert q.consume("alice") is True


def test_whisper_quota_per_subject_isolation(clock):
    q = WhisperQuota(jobs_per_hour=1)
    assert q.consume("alice") is True
    assert q.consume("alice") is False
    assert q.consume("bob") is True


def test_whisper_quota_fail_closed_zero_denies_all_asr(clock):
    """YTT_WHISPER_JOBS_PER_HOUR=0 denies every NEW ASR job."""
    q = WhisperQuota.from_settings(Settings(whisper_jobs_per_hour=0))
    assert q.consume("alice") is False
    clock.advance(3600.0)
    assert q.consume("alice") is False
