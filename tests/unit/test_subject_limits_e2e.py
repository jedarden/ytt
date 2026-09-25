"""End-to-end per-subject limit tests: rate limit + Whisper quota (Phase 5).

Complements the unit coverage in ``test_ratelimit.py`` (bucket maths, thread
safety) and the anonymous-path enforcement tests in ``test_server.py``
§Per-subject limits. This file drives ``mcp.call_tool()`` with *authenticated*
subjects — real FastMCP ``AccessToken``s resolved through the production
``fastmcp.server.dependencies.get_access_token`` path that
``ytt.server._request_subject`` reads — and proves the properties
docs/notes/auth.md promises:

- **Independence**: one subject exhausting the rate limit or the Whisper
  quota never touches another subject's bucket — denials are accounted to
  the denied subject's hash alone.
- **Fail-closed**: a limiter that *raises* denies the request instead of
  admitting it (and degrades its retry hint rather than raising into the
  tool); there is no error path that opens the protected work.
- **Refill**: an exhausted subject is re-admitted end-to-end once the
  bucket refills (deterministic clock, no real sleeping).
- **Job polling cannot bypass the limits**: polls and finished-work
  retrieval are free, but they start no work — and an exhausted rate-limit
  bucket cannot reach the quota's join-for-free courtesy by re-calling a
  video whose job is in flight (the refund belongs to the Whisper quota,
  never the fetch-path rate limiter).
"""

from __future__ import annotations

import time

import pytest

from ytt.server import mcp


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """This file tests limit enforcement, not the allowlist gate — see
    test_auth.py / test_authz_tool_gate.py for the AuthMiddleware tests.
    Direct ``mcp.call_tool()`` runs with no HTTP request, so bypass the
    gate; the subject itself is simulated per-call via :func:`_auth_as`.
    """
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


def _auth_as(monkeypatch, email: str | None) -> None:
    """Make the production subject-resolution path resolve to *email*.

    ``ytt.server._request_subject`` imports ``get_access_token`` from
    ``fastmcp.server.dependencies`` at call time, so patching that
    namespace redirects the real code path — the test never re-implements
    the keying rule it is verifying. A real ``AccessToken``: consumers of
    ``get_access_token()`` type-check it.
    """
    from fastmcp.server import dependencies as deps
    from fastmcp.server.auth.auth import AccessToken

    claims = {} if email is None else {"email": email, "email_verified": True}
    token = AccessToken(
        token="faketoken",
        client_id="test-client",
        scopes=[],
        expires_at=None,
        claims=claims,
    )
    monkeypatch.setattr(deps, "get_access_token", lambda: token)


def _install_limits(monkeypatch, limiter, quota) -> None:
    from ytt import server

    monkeypatch.setattr(server, "_rate_limiter", limiter)
    monkeypatch.setattr(server, "_whisper_quota", quota)


def _cache_miss(monkeypatch) -> None:
    from ytt import server

    async def mock_get(video_id, lang):
        return None

    monkeypatch.setattr(server.transcript_cache, "get", mock_get)


def _fetch_raises(monkeypatch, exc) -> None:
    """Make the fetch pool raise *exc* instead of running yt-dlp."""
    from ytt import server

    async def fake_run(fn, video_id=None):
        raise exc

    monkeypatch.setattr(server._concurrency.fetch_pool, "run", fake_run)


def _job_registry(monkeypatch, jobs: dict) -> None:
    """Back the registry singleton with a *jobs* dict
    (video_id → WhisperJob). ``get_or_create`` starts a new pending job
    only for videos without one, so join-vs-new behaves like production."""
    from ytt import server
    from ytt.models import WhisperJob

    async def mock_get(video_id):
        return jobs.get(video_id)

    async def mock_active_count():
        return len(jobs)

    async def mock_get_or_create(video_id, duration_sec=None, settings=None):
        job = jobs.get(video_id)
        if job is not None:
            return job, False
        job = WhisperJob(
            video_id=video_id, status="pending", created_at=time.time(), eta_sec=60.0
        )
        jobs[video_id] = job
        return job, True

    monkeypatch.setattr(server.whisper_registry, "get", mock_get)
    monkeypatch.setattr(server.whisper_registry, "active_count", mock_active_count)
    monkeypatch.setattr(server.whisper_registry, "get_or_create", mock_get_or_create)


def _no_new_jobs(monkeypatch) -> None:
    """Fail the test if anything tries to start a Whisper job."""
    from ytt import server

    async def must_not_create(*a, **kw):
        raise AssertionError("get_or_create must not run in this scenario")

    monkeypatch.setattr(server.whisper_registry, "get_or_create", must_not_create)


def _limited_count(subject: str) -> float:
    """Current ytt_rate_limited_total value for *subject*'s hash."""
    import hashlib

    from ytt.observability import ytt_rate_limited_total

    h = hashlib.sha256(subject.encode()).hexdigest()[:8]
    return ytt_rate_limited_total.labels(subject_hash=h)._value.get()


def _quota_left(quota, subject: str) -> float:
    """Read-only view of *subject*'s remaining Whisper-quota slots."""
    return quota._limiter.bucket_for(subject).tokens_remaining


@pytest.fixture
def clock(monkeypatch):
    """Deterministic ``time.monotonic`` for the token buckets (no sleeping).

    Patch ``ytt.ratelimit``'s clock before any limiter is installed, so the
    buckets under test are created — and refill — against fake time.
    """
    from ytt import ratelimit

    class FakeClock:
        def __init__(self, start: float = 1000.0) -> None:
            self.now = start

        def monotonic(self) -> float:
            return self.now

        def advance(self, dt: float) -> None:
            self.now += dt

    c = FakeClock()
    monkeypatch.setattr(ratelimit, "time", c)
    return c


ALICE = "alice@example.com"
BOB = "bob@example.com"


# ---------------------------------------------------------------------------
# Subject resolution — the accounting key
# ---------------------------------------------------------------------------


async def test_request_subject_normalises_the_email_claim(monkeypatch):
    """The bucket key is the lowercased token ``email`` claim (and nothing
    else on the token): case variants of one address share a bucket, and a
    token without an email falls back to the shared anonymous bucket."""
    from ytt.server import _request_subject

    _auth_as(monkeypatch, "Mixed@Case.Example.COM")
    assert _request_subject() == "mixed@case.example.com"

    _auth_as(monkeypatch, None)
    assert _request_subject() == "anonymous"


# ---------------------------------------------------------------------------
# Independence — one subject's exhaustion never touches another's
# ---------------------------------------------------------------------------


async def test_rate_limit_independent_between_authenticated_subjects(monkeypatch):
    """Alice drains her burst → she is denied with rate_limited while Bob is
    still admitted, and the denial is accounted to Alice's hash alone."""
    from ytt.errors import UNAVAILABLE, YttError
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _auth_as(monkeypatch, ALICE)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=2, refill_rate_per_sec=0.0),
        WhisperQuota(jobs_per_hour=10),
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(UNAVAILABLE, "Video unavailable"))

    for _ in range(2):  # Alice drains her own burst
        sc = (
            await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
        ).structured_content
        assert sc["error_code"] == "unavailable"

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["error_code"] == "rate_limited"
    alice_denials = _limited_count(ALICE)
    assert alice_denials > 0

    # Bob's first call lands in his own fresh bucket — admitted (fails the
    # fetch as "unavailable", which proves the limiter did not fire).
    _auth_as(monkeypatch, BOB)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["error_code"] == "unavailable"
    assert _limited_count(BOB) == 0.0  # Bob was never denied

    # Interleaved: Alice stays shut out while Bob keeps working.
    _auth_as(monkeypatch, ALICE)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["error_code"] == "rate_limited"
    assert _limited_count(ALICE) == alice_denials + 1

    _auth_as(monkeypatch, BOB)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "abcdefghijk"})
    ).structured_content
    assert sc["error_code"] == "unavailable"


async def test_whisper_quota_independent_and_join_is_free(monkeypatch, clock):
    """Alice starts a job (her quota spends a slot); Bob joining that job is
    refunded (his quota untouched) and Bob can still start his own job —
    Alice's exhaustion never leaks into Bob's accounting.

    Frozen clock: the quota-slot assertions below are exact (no virtual
    refill drift between the spend and the read)."""
    from ytt.errors import EMPTY_BODY, YttError
    from ytt import whisper as ytt_whisper
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _auth_as(monkeypatch, ALICE)
    quota = WhisperQuota(jobs_per_hour=1)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=50, refill_rate_per_sec=50.0 / 60.0),
        quota,
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(EMPTY_BODY, "empty body"))
    jobs: dict = {}
    _job_registry(monkeypatch, jobs)

    async def noop_run(*a, **kw):
        return None

    monkeypatch.setattr(ytt_whisper, "run_whisper_job", noop_run)

    # Alice starts v1 — her only slot is spent.
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "v1abcdefghi"})
    ).structured_content
    assert sc["status"] == "pending"
    assert _quota_left(quota, ALICE) == 0.0

    # Alice's next NEW video is denied — her quota is exhausted.
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "v2abcdefghi"})
    ).structured_content
    assert sc["error_code"] == "rate_limited"
    assert "Whisper ASR quota exhausted" in sc["message"]

    # Bob joins Alice's in-flight v1 — admitted, and the pre-charge is
    # refunded: his quota is still full.
    _auth_as(monkeypatch, BOB)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "v1abcdefghi"})
    ).structured_content
    assert sc["status"] == "pending"
    assert _quota_left(quota, BOB) == 1.0

    # And Bob can start his own job on a fresh video.
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "v2abcdefghi"})
    ).structured_content
    assert sc["status"] == "pending"
    assert _quota_left(quota, BOB) == 0.0
    assert _quota_left(quota, ALICE) == 0.0  # Bob's spend never touched Alice


# ---------------------------------------------------------------------------
# Fail-closed — a broken limiter denies, it never admits
# ---------------------------------------------------------------------------


class _BrokenLimiter:
    """A limiter whose storage is failing — every call raises."""

    def __init__(self, break_retry_hint: bool = False) -> None:
        self.break_retry_hint = break_retry_hint

    def consume(self, subject, n: int = 1) -> bool:
        raise RuntimeError("storage unavailable")

    def retry_after_sec(self, subject) -> float:
        if self.break_retry_hint:
            raise RuntimeError("storage unavailable")
        return 60.0


async def test_broken_rate_limiter_denies_fail_closed(monkeypatch):
    """A raising limiter is broken storage, not an admission signal: the
    fetch is denied with the deterministic rate_limited shape and no
    yt-dlp work starts."""
    from ytt import server
    from ytt.ratelimit import WhisperQuota

    _auth_as(monkeypatch, ALICE)
    _install_limits(monkeypatch, _BrokenLimiter(), WhisperQuota(jobs_per_hour=10))
    _cache_miss(monkeypatch)

    async def must_not_run(fn, video_id=None):
        raise AssertionError("fetch must not run when the limiter is broken")

    monkeypatch.setattr(server._concurrency.fetch_pool, "run", must_not_run)

    before = _limited_count(ALICE)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Rate limit exceeded" in sc["message"]
    assert _limited_count(ALICE) == before + 1


async def test_broken_retry_hint_degrades_without_raising(monkeypatch):
    """A denial whose retry hint also fails stays a clean rate_limited —
    the hint is dropped (no 'Try again' text), no exception escapes."""
    from ytt.errors import UNAVAILABLE, YttError
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _auth_as(monkeypatch, ALICE)
    broken = _BrokenLimiter(break_retry_hint=True)

    class _HalfBroken(SubjectRateLimiter):
        """Real bucket (exhaustible) with a broken retry-hint probe."""

        def retry_after_sec(self, subject):
            return broken.retry_after_sec(subject)

    _install_limits(
        monkeypatch,
        _HalfBroken(capacity=1, refill_rate_per_sec=1.0 / 60.0),
        WhisperQuota(jobs_per_hour=10),
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(UNAVAILABLE, "Video unavailable"))

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["error_code"] == "unavailable"  # first call admitted

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Try again" not in sc["message"]  # hint degraded, not crashed


async def test_broken_whisper_quota_denies_new_job(monkeypatch):
    """A raising quota limiter denies the NEW-job path before get_or_create,
    with the quota-exhausted shape — a broken quota can never admit ASR
    work."""
    from ytt.errors import EMPTY_BODY, YttError
    from ytt.ratelimit import SubjectRateLimiter

    _auth_as(monkeypatch, ALICE)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=10, refill_rate_per_sec=10.0 / 60.0),
        _BrokenLimiter(),
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(EMPTY_BODY, "empty body"))
    _no_new_jobs(monkeypatch)

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Whisper ASR quota exhausted" in sc["message"]


# ---------------------------------------------------------------------------
# Refill — an exhausted subject is re-admitted once the bucket refills
# ---------------------------------------------------------------------------


async def test_rate_limit_refills_end_to_end(monkeypatch, clock):
    """Deny → advance the clock past the refill interval → the SAME subject
    is admitted again through the real tool path (continuous refill)."""
    from ytt.errors import UNAVAILABLE, YttError
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _auth_as(monkeypatch, ALICE)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=1, refill_rate_per_sec=1.0),
        WhisperQuota(jobs_per_hour=10),
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(UNAVAILABLE, "Video unavailable"))

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["error_code"] == "unavailable"  # burst token spent

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["error_code"] == "rate_limited"
    assert "Try again in ~1s" in sc["message"]  # hint matches the refill math

    clock.advance(1.0)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["error_code"] == "unavailable"  # refilled — admitted again


async def test_whisper_quota_refills_end_to_end(monkeypatch, clock):
    """Quota denial → clock advance → one slot refills and the SAME subject
    can start exactly one more NEW job before being denied again."""
    from ytt.errors import EMPTY_BODY, YttError
    from ytt import whisper as ytt_whisper
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _auth_as(monkeypatch, ALICE)
    # jobs_per_hour=2 → 2 slots now, refilling 1 slot per 1800s.
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=50, refill_rate_per_sec=50.0 / 60.0),
        WhisperQuota(jobs_per_hour=2),
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(EMPTY_BODY, "empty body"))
    jobs: dict = {}
    _job_registry(monkeypatch, jobs)

    async def noop_run(*a, **kw):
        return None

    monkeypatch.setattr(ytt_whisper, "run_whisper_job", noop_run)

    for vid in ("v1abcdefghi", "v2abcdefghi"):
        sc = (
            await mcp.call_tool("get_youtube_transcript", {"url": vid})
        ).structured_content
        assert sc["status"] == "pending"

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "v3abcdefghi"})
    ).structured_content
    assert sc["error_code"] == "rate_limited"
    assert "Whisper ASR quota exhausted" in sc["message"]

    clock.advance(1801.0)  # one slot refilled (with margin over 1800s)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "v3abcdefghi"})
    ).structured_content
    assert sc["status"] == "pending"  # refilled slot spent on v3

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "v4abcdefghi"})
    ).structured_content
    assert sc["error_code"] == "rate_limited"  # and the bucket is empty again


# ---------------------------------------------------------------------------
# Job polling cannot bypass the limits
# ---------------------------------------------------------------------------


async def test_exhausted_rate_limit_cannot_reach_the_join(monkeypatch, clock):
    """The join-an-in-flight-job courtesy belongs to the Whisper quota only:
    an exhausted rate-limit bucket is denied at the fetch charge — BEFORE the
    whisper path — even when the video's job is right there in the registry.

    Frozen clock: the quota-slot assertions below are exact (no virtual
    refill drift between the spend and the read)."""
    from ytt.errors import EMPTY_BODY, YttError
    from ytt import whisper as ytt_whisper
    from ytt.models import WhisperJob
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _auth_as(monkeypatch, ALICE)
    quota = WhisperQuota(jobs_per_hour=10)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=1, refill_rate_per_sec=0.0),
        quota,
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(EMPTY_BODY, "empty body"))
    jobs: dict = {}
    _job_registry(monkeypatch, jobs)

    async def noop_run(*a, **kw):
        return None

    monkeypatch.setattr(ytt_whisper, "run_whisper_job", noop_run)

    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["status"] == "pending"  # job started; Alice's bucket now empty

    # Re-call while the job is still pending: the rate-limiter charge comes
    # first, so Alice is denied rather than waved through to the join.
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Rate limit exceeded" in sc["message"]  # the fetch limiter fired…
    assert "quota" not in sc["message"]  # …not the ASR quota
    assert _quota_left(quota, ALICE) == 9.0  # quota untouched by the denial

    # A fresh subject is still waved through to the (free) join.
    _auth_as(monkeypatch, BOB)
    sc = (
        await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["status"] == "pending"
    assert _quota_left(quota, BOB) == 10.0  # Bob's join was refunded


async def test_polling_starts_no_work_and_retrieval_is_free(monkeypatch):
    """Under fully fail-closed limits (0 rate, 0 quota): polls of an unknown
    video return not_found and start nothing, and retrieving a FINISHED
    job's transcript still works — paid work is deliverable, no new work is
    reachable through the polling path."""
    from ytt import server
    from ytt.cache import CacheHit
    from ytt.models import WhisperJob
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _auth_as(monkeypatch, ALICE)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=0, refill_rate_per_sec=0.0),
        WhisperQuota(jobs_per_hour=0),
    )
    _no_new_jobs(monkeypatch)

    # Unknown video: not_found, and no job was started by polling.
    sc = (
        await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
    ).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "not_found"

    # Finished job + cached transcript: delivery is free under fail-closed
    # limits — the work was charged when it started, not when retrieved.
    done = WhisperJob(
        video_id="dQw4w9WgXcQ", status="done", created_at=time.time()
    )

    async def mock_registry_get(video_id):
        return done if video_id == "dQw4w9WgXcQ" else None

    fake_hit = CacheHit(
        video_id="dQw4w9WgXcQ",
        lang="whisper",
        source="whisper",
        text="Never gonna give you up.",
        segments=None,
    )

    async def mock_cache_get(video_id, lang):
        return fake_hit

    monkeypatch.setattr(server.whisper_registry, "get", mock_registry_get)
    monkeypatch.setattr(server.transcript_cache, "get", mock_cache_get)

    for _ in range(2):  # repeated retrieval never hits either limit
        sc = (
            await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
        ).structured_content
        assert sc["status"] == "ok"
        assert "Never gonna" in sc["text"]
