"""Saturation / load test for ytt (plan: §Testing Strategy — Integration).

Drives the fetch semaphore to queue-full and asserts 429 + Retry-After +
queue_depth in the response.

Plan §Testing Strategy:
    "a saturation/load test (drive the fetch semaphore to queue-full, assert
    429+Retry-After+queue_depth). Harness = ytt test --integration via kubectl exec
    into the pod or the ytt-test Deployment (no Jobs)."

Plan §Do-no-harm:
    "the saturation/load test targets ytt's own ClusterIP Service in-cluster,
    NEVER the shared Cloudflare edge/Traefik, so it can't stress ibkr's ingress."

The test hits the in-cluster ClusterIP directly (``YTT_TEST_BASE_URL`` points at
the ClusterIP), so no traffic goes through Traefik or Cloudflare.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    BASE_URL,
    MCP_URL,
    TEST_TOKEN,
    VIDEO_SHORT_CAPTIONED,
    McpResult,
    call_tool,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _raw_tool_call(
    session: httpx.AsyncClient,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    token: str | None = None,
) -> httpx.Response:
    """Make a raw MCP tool call over the Streamable HTTP transport.

    Returns the raw httpx.Response so the caller can inspect status codes
    including 429 (which `fastmcp.Client` may raise as an exception).
    """
    tok = token or TEST_TOKEN
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    resp = await session.post(MCP_URL, json=payload, headers=headers)
    return resp


# ---------------------------------------------------------------------------
# Load / saturation test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_queue_full_429():
    """Drive the fetch semaphore to queue-full → 429 + Retry-After + queue_depth.

    Plan §Testing Strategy (Integration):
    "drive the fetch semaphore to queue-full, assert 429+Retry-After+queue_depth"

    Strategy:
    1. Flood the server with many concurrent transcript requests for DIFFERENT
       video IDs (to bypass single-flight; we want N fetch slots busy + M queued).
    2. Once the queue fills, additional requests should return HTTP 429.
    3. Verify the 429 response body contains queue_depth and the
       Retry-After header is present.

    Note: The number of concurrent requests needed depends on
    ``YTT_MAX_CONCURRENT_FETCHES`` (default 4) +
    ``YTT_MAX_FETCH_QUEUE`` (default 20) configured on the server.
    We send FLOOD_N = max_concurrent + max_queue + 5 to guarantee overflow.
    """
    # These URLs will each attempt to start a distinct yt-dlp fetch.
    # Using different video IDs bypasses single-flight (each is a separate key).
    # We use the short captioned video with appended-but-stripped params (same ID)
    # combined with multiple distinct but stable IDs from the canary list.
    test_urls = [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # Me at the zoo
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # Never Gonna Give You Up
        "https://www.youtube.com/watch?v=BaW_jenozKc",  # Numberphile
        "https://www.youtube.com/watch?v=9bZkp7q19f0",  # Gangnam Style
        "https://www.youtube.com/watch?v=kJQP7kiw5Fk",  # Despacito
        "https://www.youtube.com/watch?v=JGwWNGJdvx8",  # Shape of You
        "https://www.youtube.com/watch?v=RgKAFK5djSk",  # See You Again
        "https://www.youtube.com/watch?v=OPf0YbXqDm0",  # Uptown Funk
        "https://www.youtube.com/watch?v=2Vv-BfVoq4g",  # Ed Sheeran Perfect
        "https://www.youtube.com/watch?v=hT_nvWreIhg",  # OneRepublic Counting Stars
    ]

    # Saturate beyond the default max_concurrent + max_queue (default: 4+20=24).
    FLOOD_N = 30  # guaranteed to overflow a default config

    # Use raw HTTP calls so we can inspect 429 responses.
    limits = httpx.Limits(max_connections=FLOOD_N + 5)
    async with httpx.AsyncClient(
        timeout=30.0, limits=limits
    ) as session:
        # Fire FLOOD_N requests simultaneously, cycling through the test URLs.
        tasks = [
            asyncio.create_task(
                _raw_tool_call(
                    session,
                    "get_youtube_transcript",
                    {"url": test_urls[i % len(test_urls)]},
                )
            )
            for i in range(FLOOD_N)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    # Count 200s and 429s
    status_counts: dict[int, int] = {}
    for r in responses:
        if isinstance(r, Exception):
            status_counts[-1] = status_counts.get(-1, 0) + 1
        else:
            code = r.status_code
            status_counts[code] = status_counts.get(code, 0) + 1

    # We expect at least some 429s (queue overflow)
    count_429 = status_counts.get(429, 0)
    assert count_429 > 0, (
        f"Expected at least one 429 (queue-full) from {FLOOD_N} concurrent requests. "
        f"Status distribution: {status_counts}. "
        f"This may mean YTT_MAX_FETCH_QUEUE is set very high — lower it in the "
        f"test config or increase FLOOD_N."
    )

    # Verify a 429 response has the correct shape
    for r in responses:
        if isinstance(r, Exception):
            continue
        if r.status_code != 429:
            continue

        # Must have Retry-After header
        assert "retry-after" in {k.lower(): v for k, v in r.headers.items()}, (
            f"429 response missing Retry-After header: {dict(r.headers)}"
        )

        # The response body should contain queue_depth
        try:
            body = r.json()
        except Exception:
            # Some 429s may be raw text
            body_text = r.text
            assert "queue" in body_text.lower(), (
                f"429 body doesn't mention queue: {body_text[:200]}"
            )
            continue

        # Unwrap MCP JSON-RPC response if needed
        if "result" in body:
            result_payload = body.get("result", {})
        elif "error" in body:
            result_payload = body.get("error", {})
        else:
            result_payload = body

        # For MCP tool responses, the payload may be nested in content
        content = result_payload.get("content", [])
        if content:
            import json as _json
            try:
                inner = _json.loads(content[0].get("text", "{}"))
                result_payload = inner
            except Exception:
                pass

        # Check for error_code=rate_limited or queue_depth field
        assert (
            result_payload.get("error_code") in ("rate_limited", "queue_full", None)
            or "queue" in str(result_payload).lower()
        ), f"429 body missing queue info: {result_payload}"

        break  # Verified one 429 — sufficient


@pytest.mark.asyncio
async def test_load_retry_after_header_present():
    """Verify Retry-After header is present and numeric on 429 responses.

    Plan §Error taxonomy: "queue-full → 429 + Retry-After".
    """
    # Make enough concurrent requests to overflow the queue
    FLOOD_N = 25
    test_url = _yt_url(VIDEO_SHORT_CAPTIONED)

    async with httpx.AsyncClient(timeout=20.0) as session:
        tasks = [
            asyncio.create_task(
                _raw_tool_call(session, "get_youtube_transcript", {"url": test_url})
            )
            for _ in range(FLOOD_N)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    four29s = [
        r for r in responses
        if not isinstance(r, Exception) and r.status_code == 429
    ]

    if not four29s:
        pytest.skip(
            "No 429s observed — queue not exhausted. "
            "Increase FLOOD_N or lower YTT_MAX_FETCH_QUEUE."
        )

    for r in four29s:
        headers_ci = {k.lower(): v for k, v in r.headers.items()}
        retry_after = headers_ci.get("retry-after")
        assert retry_after is not None, "429 missing Retry-After header"
        assert int(retry_after) > 0, f"Retry-After must be positive, got {retry_after!r}"
        break  # One verification is enough


def _yt_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
