"""Shared fixtures for the ytt integration test suite (plan: Phase 9).

All fixtures here require in-cluster execution (``ardenone-cluster``).
Configure via environment variables:

    YTT_TEST_BASE_URL       MCP server URL (default: http://ytt.ytt.svc:8080)
    YTT_TEST_TOKEN          Bearer token with sub in YTT_ALLOWED_SUBJECTS (required)
    YTT_TEST_IBKR_BASE_URL  ibkr-mcp base for do-no-harm check
                            (default: http://ibkr-mcp.ibkr-mcp.svc:8080)
    YTT_TEST_PATH_PREFIX    path prefix (default: /ytt)

Do NOT run from a datacenter IP — YouTube blocks it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Environment-derived constants
# ---------------------------------------------------------------------------

BASE_URL: str = os.environ.get("YTT_TEST_BASE_URL", "http://ytt.ytt.svc:8080")
PATH_PREFIX: str = os.environ.get("YTT_TEST_PATH_PREFIX", "/ytt")
IBKR_BASE_URL: str = os.environ.get(
    "YTT_TEST_IBKR_BASE_URL", "http://ibkr-mcp.ibkr-mcp.svc:8080"
)
TEST_TOKEN: str | None = os.environ.get("YTT_TEST_TOKEN")

MCP_URL: str = f"{BASE_URL}{PATH_PREFIX}/mcp"
HEALTH_URL: str = f"{BASE_URL}{PATH_PREFIX}/health"
METRICS_URL: str = f"{BASE_URL}{PATH_PREFIX}/metrics"
ADMIN_EGRESS_URL: str = f"{BASE_URL}{PATH_PREFIX}/admin/egress"


# ---------------------------------------------------------------------------
# Known-stable test video IDs (plan §Canary / §Integration)
# Update if a video becomes unavailable or region-blocked.
# ---------------------------------------------------------------------------

#: Short (18 s), manually captioned — "Me at the zoo" (YouTube's first video).
VIDEO_SHORT_CAPTIONED = "jNQXAC9IVRw"

#: ~3 min 32 s, manually captioned — "Never Gonna Give You Up".
VIDEO_LONG_CAPTIONED = "dQw4w9WgXcQ"

#: Auto-captioned only (no manual track) — Numberphile "What is a number?"
#: If this loses auto-captions, swap for any other auto-captioned video.
VIDEO_AUTO_CAPTIONED = "BaW_jenozKc"

#: A very short video likely to have NO captions — used for Whisper fallback test.
#: "Charlie bit my finger" — typically no captions available.
VIDEO_NO_CAPTIONS = "0EqSXDwTq6s"

#: Known-private video ID (returns private error).
VIDEO_PRIVATE = "zTD2RZz6mlo"

#: A live/premiere-only stream title pattern; use a known livestream.
#: NASA LIVE — frequently streaming; may not always be in live state.
VIDEO_LIVESTREAM = "21X5lGlDOfg"

#: A very long video (>20 min) — triggers too_long_for_asr when no captions.
#: "Back to the Future" trailer playlist is 2h+; use a stable documentary.
VIDEO_TOO_LONG_FOR_ASR = "tSugne__doU"  # 4h science lecture (public)


# ---------------------------------------------------------------------------
# MCP client helper
# ---------------------------------------------------------------------------


@dataclass
class McpResult:
    """Parsed result of a single MCP tool call."""

    raw: dict
    status: str
    error_code: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "McpResult":
        return cls(
            raw=d,
            status=d.get("status", ""),
            error_code=d.get("error_code"),
        )


async def call_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    token: str | None = None,
    timeout: float = 60.0,
) -> McpResult:
    """Call a ytt MCP tool via the Streamable HTTP transport.

    Uses ``fastmcp.Client`` (the official FastMCP 3.x client).  The bearer
    token is passed via ``httpx.BearerAuth``; omit for unauthenticated calls
    (expects a 401 response, which the caller must handle).

    Returns the parsed tool result as an ``McpResult``.
    Raises ``AssertionError`` if the MCP call itself fails (is_error=True).
    """
    from fastmcp import Client

    tok = token or TEST_TOKEN
    auth = tok  # fastmcp.Client accepts a raw string Bearer token
    async with Client(MCP_URL, auth=auth, timeout=timeout) as client:
        result = await client.call_tool(tool_name, arguments)
    # Extract the payload from the first content block
    if result.structured_content is not None:
        data = result.structured_content
    elif result.content:
        # Tool returns a dict serialised as JSON text
        raw_text = getattr(result.content[0], "text", "")
        data = json.loads(raw_text)
    else:
        data = {}
    assert not result.is_error, f"Tool call is_error=True: {data}"
    return McpResult.from_dict(data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use the default asyncio event loop for the session."""
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def http_client():
    """Synchronous httpx client for HTTP (non-MCP) endpoints."""
    with httpx.Client(timeout=30.0) as client:
        yield client


@pytest_asyncio.fixture(scope="session")
async def async_http_client():
    """Async httpx client for HTTP (non-MCP) endpoints."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def require_token():
    """Fail early if YTT_TEST_TOKEN is not set."""
    if not TEST_TOKEN:
        pytest.skip("YTT_TEST_TOKEN env var not set — skipping integration suite")


@pytest.fixture(scope="session", autouse=True)
def check_server_health(http_client):
    """Verify the ytt server is reachable before running any integration test."""
    try:
        resp = http_client.get(HEALTH_URL)
        assert resp.status_code == 200, f"Health check failed: {resp.status_code}"
        data = resp.json()
        assert data.get("status") == "ok", f"Unexpected health body: {data}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        pytest.skip(f"ytt server unreachable at {HEALTH_URL}: {exc}")


@pytest.fixture(scope="session")
def ibkr_baseline(http_client):
    """Capture ibkr .well-known responses before any ytt test runs.

    Returns a dict of {path: bytes} for each ibkr metadata path.
    Used by the do-no-harm check at session teardown.
    """
    paths = [
        "/.well-known/oauth-protected-resource/ibkr",
        "/.well-known/oauth-authorization-server/ibkr",
    ]
    baseline: dict[str, bytes] = {}
    ibkr_prefix = os.environ.get("YTT_TEST_IBKR_PATH_PREFIX", "/ibkr")
    ibkr_health = f"{IBKR_BASE_URL}{ibkr_prefix}/health"

    try:
        resp = http_client.get(ibkr_health, timeout=5.0)
        if resp.status_code != 200:
            pytest.skip("ibkr server not reachable — skipping do-no-harm baseline")
    except (httpx.ConnectError, httpx.TimeoutException):
        # ibkr not reachable in this env — skip the do-no-harm fixture
        return {}

    for path in paths:
        try:
            r = http_client.get(f"{IBKR_BASE_URL}{path}", timeout=5.0)
            baseline[path] = r.content
        except Exception:
            pass

    return baseline
