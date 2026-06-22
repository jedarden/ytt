"""Unit tests for the server skeleton (Phase 1, plan: Components / Tools).

Tests the FastMCP instance construction, tool registration, tool stubs, and the
unauthenticated ``/ytt/health`` endpoint — all without starting uvicorn or
hitting the network.
"""

from __future__ import annotations

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
# Tool stub behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_youtube_transcript_stub_returns_error():
    """Phase 1 stub must return status=error (not raise) so MCP serializes it.

    FastMCP call_tool returns a ToolResult with .structured_content (dict)
    and .content (list of TextContent).
    """
    result = await mcp.call_tool("get_youtube_transcript", {"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert result is not None
    # structured_content is the dict returned by the tool function
    sc = result.structured_content
    assert sc is not None
    assert sc.get("status") == "error"


@pytest.mark.asyncio
async def test_get_transcript_job_stub_returns_not_found():
    """Phase 1 stub must return not_found for any video_id."""
    result = await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
    assert result is not None
    sc = result.structured_content
    assert sc is not None
    assert sc.get("status") == "error"
    assert sc.get("error_code") == "not_found"


# ---------------------------------------------------------------------------
# Health endpoint (unauthenticated)
# ---------------------------------------------------------------------------


def _get_test_client() -> TestClient:
    """Build a Starlette TestClient for the ASGI app.

    Uses the module-level ``mcp`` singleton (already built with default settings);
    ``build_asgi_app()`` derives the mount path from the same cached settings.
    No env-var mutation — callers that need custom settings must patch
    ``ytt.server.mcp`` or use ``monkeypatch`` on the settings.
    """
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
