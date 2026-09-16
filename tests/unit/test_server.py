"""Unit tests for the server skeleton (Phase 1) + Phase 7 pipeline wiring.

Tests FastMCP instance construction, tool registration, the unauthenticated
``/ytt/health`` endpoint, and the Phase 7 wired pipeline (cache-hit path,
get_transcript_job done path) — all without starting uvicorn or hitting the network.

Network-dependent paths (yt-dlp fetch, Whisper transcription) are not tested
here; they require the in-cluster integration suite (Phase 9).
"""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from ytt.server import build_asgi_app, mcp


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """This file tests tool/business logic (Phase 7 pipeline wiring), not
    auth — see test_auth.py for the AuthMiddleware/allowlist tests. Direct
    ``mcp.call_tool()``/``mcp.list_tools()`` calls run with no HTTP request,
    so there's no real Google-verified token to resolve; bypass the
    AuthMiddleware gate rather than fabricate one here.
    """
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_length():
    """Two tools must be registered (plan: Tools)."""
    tools = await mcp.list_tools()
    assert len(tools) == 2


@pytest.mark.asyncio
async def test_tools_list_names():
    """The two registered tools must have the exact plan-specified names."""
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"get_youtube_transcript", "get_transcript_job"}


@pytest.mark.asyncio
async def test_tool_get_youtube_transcript_description():
    """get_youtube_transcript must have a non-empty description mentioning cursor."""
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_youtube_transcript")
    assert tool.description
    # Description must tell the model to relay the ETA on pending responses
    assert "pending" in tool.description.lower()
    # Description must mention cursor continuation
    assert "cursor" in tool.description.lower()


@pytest.mark.asyncio
async def test_tool_get_transcript_job_description():
    """get_transcript_job must have a non-empty description mentioning polling."""
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_transcript_job")
    assert tool.description
    assert "pending" in tool.description.lower() or "poll" in tool.description.lower()


# ---------------------------------------------------------------------------
# Tool behaviour — fast / no-network paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_youtube_transcript_channel_url_returns_bad_url():
    """Channel/handle URLs are rejected at canonicalize (no network call).

    Plan §URL→canonical video_id: "Reject playlist-only, channel (/channel/,
    /@handle), search → error_code: bad_url".
    """
    result = await mcp.call_tool(
        "get_youtube_transcript",
        {"url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"},
    )
    assert result is not None
    sc = result.structured_content
    assert sc is not None
    assert sc.get("status") == "error"
    assert sc.get("error_code") == "bad_url"


@pytest.mark.asyncio
async def test_get_youtube_transcript_handle_url_returns_bad_url():
    """@handle URLs are rejected at canonicalize (no network call)."""
    result = await mcp.call_tool(
        "get_youtube_transcript",
        {"url": "https://www.youtube.com/@SomeCreator"},
    )
    sc = result.structured_content
    assert sc.get("status") == "error"
    assert sc.get("error_code") == "bad_url"


@pytest.mark.asyncio
async def test_get_youtube_transcript_cache_hit(monkeypatch):
    """Cache hit → inline transcript returned via build_page (no network call).

    Plan §Caching: "Cache-first. Check before any network call; hit returns
    immediately and touches both files."
    """
    from ytt import server
    from ytt.cache import CacheHit

    fake_hit = CacheHit(
        video_id="dQw4w9WgXcQ",
        lang="en",
        source="caption_auto",
        text="Never gonna give you up never gonna let you down.",
        segments=[
            {"start": 0.0, "duration": 2.0, "text": "Never gonna give you up"},
            {"start": 2.0, "duration": 2.0, "text": "never gonna let you down."},
        ],
        metadata={"title": "Rick Astley", "channel": "RickAstleyVEVO"},
    )

    async def mock_get(video_id: str, lang: str):
        if video_id == "dQw4w9WgXcQ":
            return fake_hit
        return None

    monkeypatch.setattr(server.transcript_cache, "get", mock_get)

    result = await mcp.call_tool(
        "get_youtube_transcript",
        {"url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    sc = result.structured_content
    assert sc is not None
    assert sc["status"] == "ok"
    assert sc["lang"] == "en"
    assert sc["source"] == "caption_auto"
    assert "Never gonna" in sc["text"]
    assert sc["is_final"] is True
    assert sc.get("title") == "Rick Astley"


@pytest.mark.asyncio
async def test_get_youtube_transcript_cache_hit_query_filter(monkeypatch):
    """Cache hit with query filter → only matching segments returned."""
    from ytt import server
    from ytt.cache import CacheHit

    segs = [
        {"start": 0.0, "duration": 1.0, "text": "alpha text"},
        {"start": 1.0, "duration": 1.0, "text": "beta text"},
        {"start": 2.0, "duration": 1.0, "text": "gamma text"},
    ]
    fake_hit = CacheHit(
        video_id="abcdefghijk",
        lang="en",
        source="caption_manual",
        text="alpha text beta text gamma text",
        segments=segs,
    )

    async def mock_get(video_id, lang):
        if video_id == "abcdefghijk":
            return fake_hit
        return None

    monkeypatch.setattr(server.transcript_cache, "get", mock_get)

    result = await mcp.call_tool(
        "get_youtube_transcript",
        {"url": "abcdefghijk", "query": "beta"},
    )
    sc = result.structured_content
    assert sc["status"] == "ok"
    # "beta" segment + ±2 context
    assert "beta" in sc["text"]


@pytest.mark.asyncio
async def test_get_youtube_transcript_cache_hit_paginated(monkeypatch):
    """Cache hit with long transcript → status=partial + next_cursor."""
    from ytt import server
    from ytt.cache import CacheHit

    # 20000 chars > default inline_char_limit (18000)
    long_text = "x" * 20000
    fake_hit = CacheHit(
        video_id="longvid11111",  # 12 chars — won't work; use 11-char id below
        lang="en",
        source="caption_auto",
        text=long_text,
        segments=None,
    )
    # Use a valid 11-char video ID (plan §URL→canonical video_id)
    fake_vid = "longvid1111"  # exactly 11 chars
    fake_hit = CacheHit(
        video_id=fake_vid,
        lang="en",
        source="caption_auto",
        text=long_text,
        segments=None,
    )

    async def mock_get(video_id, lang):
        if video_id == fake_vid:
            return fake_hit
        return None

    monkeypatch.setattr(server.transcript_cache, "get", mock_get)

    result = await mcp.call_tool(
        "get_youtube_transcript",
        {"url": fake_vid, "mode": "full"},
    )
    sc = result.structured_content
    assert sc["status"] == "partial"
    assert sc.get("next_cursor") is not None
    assert sc["is_final"] is False
    assert "⚠️ PARTIAL:" in sc["text"]
    assert sc["total_chars"] == 20000


@pytest.mark.asyncio
async def test_get_transcript_job_not_found():
    """Polling for an unknown video_id returns not_found."""
    result = await mcp.call_tool(
        "get_transcript_job",
        {"video_id": "dQw4w9WgXcQ"},
    )
    assert result is not None
    sc = result.structured_content
    assert sc is not None
    assert sc.get("status") == "error"
    assert sc.get("error_code") == "not_found"


@pytest.mark.asyncio
async def test_get_transcript_job_pending(monkeypatch):
    """Polling a pending job returns status=pending + eta_sec."""
    from ytt import server
    from ytt.models import WhisperJob

    fake_job = WhisperJob(
        video_id="dQw4w9WgXcQ",
        status="pending",
        created_at=time.time(),
        eta_sec=120.0,
    )

    async def mock_get(video_id):
        return fake_job

    monkeypatch.setattr(server.whisper_registry, "get", mock_get)

    result = await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
    sc = result.structured_content
    assert sc["status"] == "pending"
    assert sc.get("eta_sec") == 120.0


@pytest.mark.asyncio
async def test_get_transcript_job_running(monkeypatch):
    """Polling a running job returns status=running."""
    from ytt import server
    from ytt.models import WhisperJob

    fake_job = WhisperJob(
        video_id="dQw4w9WgXcQ",
        status="running",
        created_at=time.time(),
        eta_sec=60.0,
    )

    async def mock_get(video_id):
        return fake_job

    monkeypatch.setattr(server.whisper_registry, "get", mock_get)

    result = await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
    sc = result.structured_content
    assert sc["status"] == "running"
    assert sc.get("eta_sec") == 60.0


@pytest.mark.asyncio
async def test_get_transcript_job_done_returns_transcript(monkeypatch):
    """When Whisper job is done, returns the transcript directly via build_page.

    Plan §Tools: "get_transcript_job: when done, returns the transcript
    directly (same shape/pagination), collapsing 3 calls to 2."
    Phase 7 replaces the Phase 6 stub (text=None).
    """
    from ytt import server
    from ytt.cache import CacheHit
    from ytt.models import WhisperJob

    fake_job = WhisperJob(
        video_id="dQw4w9WgXcQ",
        status="done",
        created_at=time.time(),
        result_ref="dQw4w9WgXcQ.whisper",
    )

    fake_hit = CacheHit(
        video_id="dQw4w9WgXcQ",
        lang="whisper",
        source="whisper",
        text="This is the Whisper ASR transcript.",
        segments=None,
    )

    async def mock_job_get(video_id):
        return fake_job

    async def mock_cache_get(video_id, lang):
        if video_id == "dQw4w9WgXcQ" and lang == "whisper":
            return fake_hit
        return None

    monkeypatch.setattr(server.whisper_registry, "get", mock_job_get)
    monkeypatch.setattr(server.transcript_cache, "get", mock_cache_get)

    result = await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
    sc = result.structured_content
    assert sc["status"] == "ok"
    assert sc["source"] == "whisper"
    assert "Whisper ASR" in sc["text"]
    assert sc["is_final"] is True
    assert sc.get("text") is not None  # NOT None (old Phase 6 stub returned None)


@pytest.mark.asyncio
async def test_get_transcript_job_done_evicted(monkeypatch):
    """When job is done but transcript was evicted, return cursor_stale not_found."""
    from ytt import server
    from ytt.models import WhisperJob

    fake_job = WhisperJob(
        video_id="dQw4w9WgXcQ",
        status="done",
        created_at=time.time(),
        result_ref="dQw4w9WgXcQ.whisper",
    )

    async def mock_job_get(video_id):
        return fake_job

    async def mock_cache_get(video_id, lang):
        return None  # evicted

    removed = []

    async def mock_remove(video_id):
        removed.append(video_id)

    monkeypatch.setattr(server.whisper_registry, "get", mock_job_get)
    monkeypatch.setattr(server.transcript_cache, "get", mock_cache_get)
    monkeypatch.setattr(server.whisper_registry, "remove", mock_remove)

    result = await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
    sc = result.structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "not_found"
    assert "dQw4w9WgXcQ" in removed


@pytest.mark.asyncio
async def test_get_transcript_job_error(monkeypatch):
    """A failed Whisper job surfaces error_code + message."""
    from ytt import server
    from ytt.models import WhisperJob

    fake_job = WhisperJob(
        video_id="dQw4w9WgXcQ",
        status="error",
        created_at=time.time(),
        error_code="asr_failed",
        message="Whisper service timed out.",
    )

    async def mock_get(video_id):
        return fake_job

    monkeypatch.setattr(server.whisper_registry, "get", mock_get)

    result = await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
    sc = result.structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "asr_failed"
    assert "timed out" in sc.get("message", "").lower()


# ---------------------------------------------------------------------------
# Per-subject limits — rate limit + Whisper quota (Phase 5, docs/notes/auth.md)
#
# Direct mcp.call_tool() runs with no HTTP request context, so
# _request_subject() resolves to the shared "anonymous" bucket for every
# call in this section — the per-subject isolation itself is covered in
# test_ratelimit.py. Each test installs fresh limiter/quota singletons so
# bucket state never leaks across tests.
# ---------------------------------------------------------------------------


def _install_limits(monkeypatch, limiter, quota):
    from ytt import server

    monkeypatch.setattr(server, "_rate_limiter", limiter)
    monkeypatch.setattr(server, "_whisper_quota", quota)


def _cache_miss(monkeypatch):
    from ytt import server

    async def mock_get(video_id, lang):
        return None

    monkeypatch.setattr(server.transcript_cache, "get", mock_get)


def _fetch_raises(monkeypatch, exc):
    """Make the fetch pool raise *exc* instead of running yt-dlp."""
    from ytt import server

    async def fake_run(fn, video_id=None):
        raise exc

    monkeypatch.setattr(server._concurrency.fetch_pool, "run", fake_run)


def _limited_count(subject: str) -> float:
    """Current ytt_rate_limited_total value for *subject*'s hash."""
    import hashlib

    from ytt.observability import ytt_rate_limited_total

    h = hashlib.sha256(subject.encode()).hexdigest()[:8]
    return ytt_rate_limited_total.labels(subject_hash=h)._value.get()


def test_request_subject_falls_back_to_anonymous():
    """No auth context (direct tool calls, local runs) → one shared bucket,
    so the limiter still bounds total volume."""
    from ytt.server import _request_subject

    assert _request_subject() == "anonymous"


@pytest.mark.asyncio
async def test_rate_limit_denies_cache_miss_without_fetching(monkeypatch):
    """Exhausted bucket → error_code=rate_limited BEFORE any yt-dlp work.

    YTT_RATE_LIMIT_PER_MIN=0 fail-closed: capacity 0 denies every fetch.
    """
    from ytt import server
    from ytt.errors import YttError
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=0, refill_rate_per_sec=0.0),
        WhisperQuota(jobs_per_hour=10),
    )
    _cache_miss(monkeypatch)

    async def must_not_run(fn, video_id=None):
        raise AssertionError("fetch must not run when the bucket is empty")

    monkeypatch.setattr(server._concurrency.fetch_pool, "run", must_not_run)

    before = _limited_count("anonymous")
    result = await mcp.call_tool(
        "get_youtube_transcript", {"url": "https://youtu.be/dQw4w9WgXcQ"}
    )
    sc = result.structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Rate limit exceeded" in sc["message"]
    # Denial is observable: ytt_rate_limited_total{subject_hash} increments.
    assert _limited_count("anonymous") == before + 1


@pytest.mark.asyncio
async def test_rate_limit_cache_hit_bypasses_limiter(monkeypatch):
    """Cache hits cost nothing — a fail-closed (0) limiter must not block a
    hit the cache can serve (plan: "Cache hits do NOT consume the rate-limit
    bucket")."""
    from ytt import server
    from ytt.cache import CacheHit
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=0, refill_rate_per_sec=0.0),
        WhisperQuota(jobs_per_hour=10),
    )

    fake_hit = CacheHit(
        video_id="dQw4w9WgXcQ",
        lang="en",
        source="caption_auto",
        text="Never gonna give you up.",
        segments=None,
    )

    async def mock_get(video_id, lang):
        return fake_hit

    monkeypatch.setattr(server.transcript_cache, "get", mock_get)

    result = await mcp.call_tool(
        "get_youtube_transcript", {"url": "https://youtu.be/dQw4w9WgXcQ"}
    )
    sc = result.structured_content
    assert sc["status"] == "ok"
    assert "Never gonna" in sc["text"]


@pytest.mark.asyncio
async def test_rate_limit_exhaustion_after_burst(monkeypatch):
    """Burst N admits N fetches (failed fetches spend a token too — the limit
    guards yt-dlp/egress effort, not successful responses); call N+1 is
    denied with a retry hint."""
    from ytt.errors import UNAVAILABLE, YttError
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=2, refill_rate_per_sec=1.0 / 60.0),
        WhisperQuota(jobs_per_hour=10),
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(UNAVAILABLE, "Video unavailable"))

    for _ in range(2):  # burst of 2 — failures consume their tokens
        sc = (await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})).structured_content
        assert sc["error_code"] == "unavailable"  # not rate_limited yet

    sc = (await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    # Retry hint: 1 token short at 1/min → ~60s.
    assert "Try again in ~60s" in sc["message"]


@pytest.mark.asyncio
async def test_whisper_quota_exhaustion_denies_new_job(monkeypatch):
    """YTT_WHISPER_JOBS_PER_HOUR=0 (fail-closed): a caption-less video can
    not start a NEW ASR job — denied rate_limited before get_or_create."""
    from ytt import server
    from ytt.errors import EMPTY_BODY, YttError
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=10, refill_rate_per_sec=10.0 / 60.0),
        WhisperQuota(jobs_per_hour=0),
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(EMPTY_BODY, "empty body"))

    async def must_not_create(*a, **kw):
        raise AssertionError("get_or_create must not run when quota is 0")

    monkeypatch.setattr(server.whisper_registry, "get_or_create", must_not_create)

    before = _limited_count("anonymous")
    sc = (await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Whisper ASR quota exhausted" in sc["message"]
    assert _limited_count("anonymous") == before + 1


@pytest.mark.asyncio
async def test_whisper_quota_joined_job_is_not_charged(monkeypatch):
    """Joining an in-flight job is free: the pre-charge is refunded, so the
    caller's quota is intact afterwards."""
    from ytt import server
    from ytt.errors import EMPTY_BODY, YttError
    from ytt.models import WhisperJob
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    quota = WhisperQuota(jobs_per_hour=1)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=10, refill_rate_per_sec=10.0 / 60.0),
        quota,
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(EMPTY_BODY, "empty body"))

    existing = WhisperJob(
        video_id="dQw4w9WgXcQ", status="running", created_at=time.time(), eta_sec=30.0
    )

    async def mock_get_or_create(video_id, duration_sec, settings):
        return existing, False  # joins — a job is already in flight

    monkeypatch.setattr(server.whisper_registry, "get_or_create", mock_get_or_create)

    sc = (await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})).structured_content
    assert sc["status"] == "pending"  # joined the in-flight job
    # Refunded: the single slot is available again.
    assert quota.consume("anonymous") is True


@pytest.mark.asyncio
async def test_whisper_quota_charged_when_new_job_starts(monkeypatch):
    """Starting a NEW job consumes a slot; the next caption-less video from
    the same subject is denied once the quota is exhausted."""
    from ytt import server
    from ytt import whisper as ytt_whisper
    from ytt.errors import EMPTY_BODY, YttError
    from ytt.models import WhisperJob
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    quota = WhisperQuota(jobs_per_hour=1)
    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=10, refill_rate_per_sec=10.0 / 60.0),
        quota,
    )
    _cache_miss(monkeypatch)
    _fetch_raises(monkeypatch, YttError(EMPTY_BODY, "empty body"))

    new_job = WhisperJob(
        video_id="dQw4w9WgXcQ", status="pending", created_at=time.time(), eta_sec=60.0
    )

    async def mock_get_or_create(video_id, duration_sec, settings):
        return new_job, True  # starts a new job

    async def noop_run(*a, **kw):
        return None

    monkeypatch.setattr(server.whisper_registry, "get_or_create", mock_get_or_create)
    # The tool imports run_whisper_job at call time — patch it at the source
    # module so the new job's background task is a no-op.
    monkeypatch.setattr(ytt_whisper, "run_whisper_job", noop_run)

    sc = (await mcp.call_tool("get_youtube_transcript", {"url": "dQw4w9WgXcQ"})).structured_content
    assert sc["status"] == "pending"  # new job started — slot consumed
    assert quota.consume("anonymous") is False  # quota now exhausted

    # Second caption-less video (no existing job) → denied.
    sc = (await mcp.call_tool("get_youtube_transcript", {"url": "abcdefghijk"})).structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Whisper ASR quota exhausted" in sc["message"]


@pytest.mark.asyncio
async def test_get_transcript_job_poll_consumes_nothing(monkeypatch):
    """get_transcript_job polls are free: with a fail-closed limiter AND a
    fail-closed quota installed, polling a pending job still works (clients
    waiting on one transcription must not drain their own budget)."""
    from ytt import server
    from ytt.models import WhisperJob
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota

    _install_limits(
        monkeypatch,
        SubjectRateLimiter(capacity=0, refill_rate_per_sec=0.0),
        WhisperQuota(jobs_per_hour=0),
    )

    fake_job = WhisperJob(
        video_id="dQw4w9WgXcQ",
        status="pending",
        created_at=time.time(),
        eta_sec=120.0,
    )

    async def mock_get(video_id):
        return fake_job

    monkeypatch.setattr(server.whisper_registry, "get", mock_get)

    for _ in range(3):  # repeated polls never hit either limit
        sc = (await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})).structured_content
        assert sc["status"] == "pending"


# ---------------------------------------------------------------------------
# Health endpoint (unauthenticated)
# ---------------------------------------------------------------------------


def _get_test_client() -> TestClient:
    """Build a Starlette TestClient for the ASGI app."""
    app = build_asgi_app()
    return TestClient(app, raise_server_exceptions=True)


def test_health_endpoint_returns_200():
    """GET /ytt/health must return 200 without auth (plan: liveness probe)."""
    client = _get_test_client()
    resp = client.get("/ytt/health")
    assert resp.status_code == 200


def test_health_endpoint_returns_ok_json():
    """GET /ytt/health must return JSON {status: ok}."""
    client = _get_test_client()
    resp = client.get("/ytt/health")
    data = resp.json()
    assert data.get("status") == "ok"


def test_health_endpoint_content_type():
    """GET /ytt/health must return application/json."""
    client = _get_test_client()
    resp = client.get("/ytt/health")
    assert "application/json" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# FastMCP instance properties
# ---------------------------------------------------------------------------


def test_mcp_name():
    """The FastMCP instance must be named 'ytt'."""
    assert mcp.name == "ytt"


def test_mcp_version():
    """The FastMCP version must match the package version."""
    import ytt as ytt_pkg

    assert mcp.version == ytt_pkg.__version__


def test_mcp_has_instructions():
    """The FastMCP instance must carry non-empty instructions."""
    assert mcp.instructions
    assert "YouTube" in mcp.instructions
