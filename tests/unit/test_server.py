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
