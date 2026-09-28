"""Long Whisper results paginate through the real ASGI transport.

The Phase 7 response-shape bead (ytt-3d847702) delivered the shared cursor
layer and the done-job path. This regression pins the missing ASR-specific
surface: a long ``get_transcript_job`` result returns a cursor and the same
tool accepts that cursor until the synthesized Whisper transcript is complete.
Only the caption and Whisper work seams are stubbed; MCP session handling,
authorization, tool dispatch, cache lookup, and structuredContent all use the
production path over ``httpx.ASGITransport``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import ytt.fetch
import ytt.whisper
from ytt import server
from ytt.cache import TranscriptCache
from ytt.config import get_settings
from ytt.errors import NoCaptionsError
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.whisper import WhisperJobRegistry

from tests.unit._mcp_asgi_harness import (  # noqa: F401
    allowlisted_subject,
    authorized_bearer,
    hermetic_egress,
    open_established_session,
)


VIDEO_ID = "longAsr001x"
LONG_WORDS = [f"whisper-{index:03d}" for index in range(40)]
LONG_TEXT = " ".join(LONG_WORDS)


@pytest.fixture
def asr_environment(monkeypatch, tmp_path):
    """Wire real cache/registry objects to isolated state for this test."""

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = TranscriptCache(cache_dir, max_bytes=1 << 20, reconcile_sec=0)
    registry = WhisperJobRegistry()

    monkeypatch.setattr(server, "transcript_cache", cache)
    monkeypatch.setattr(server, "whisper_registry", registry)
    monkeypatch.setattr(
        server,
        "_rate_limiter",
        SubjectRateLimiter(capacity=20, refill_rate_per_sec=20.0 / 60.0),
    )
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(jobs_per_hour=10))
    monkeypatch.setenv("YTT_INLINE_CHAR_LIMIT", "48")
    monkeypatch.setenv("YTT_CHUNK_CHARS", "48")
    get_settings.cache_clear()

    async def no_captions(video_id: str, lang: str | None, settings: Any):
        raise NoCaptionsError("synthetic transcript has no captions", duration_sec=1.0)

    async def synthesize(
        job: Any,
        job_registry: WhisperJobRegistry,
        settings: Any,
        cache_obj: Any,
        model: str,
    ) -> None:
        await job_registry.update_status(job.video_id, "running")
        segments = [
            {"start": float(index), "duration": 1.0, "text": word}
            for index, word in enumerate(LONG_WORDS)
        ]
        await cache_obj.put(
            job.video_id,
            "whisper",
            LONG_TEXT,
            segments,
            "whisper",
            None,
        )
        await job_registry.update_status(
            job.video_id,
            "done",
            result_ref=f"{job.video_id}.whisper.txt",
        )

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", no_captions)
    monkeypatch.setattr(ytt.whisper, "run_whisper_job", synthesize)

    return registry


async def _call(session, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    message = await session.request(
        "tools/call", {"name": name, "arguments": arguments}
    )
    result = message["result"]
    assert result.get("structuredContent") is not None, result
    return result["structuredContent"]


def _without_partial_banner(text: str) -> str:
    if text.startswith("⚠️ PARTIAL:"):
        return text.split("\n\n", 1)[1]
    return text


@pytest.mark.asyncio
async def test_long_whisper_transcript_paginates_on_get_transcript_job(
    asr_environment,
):
    """A long ASR result is completeable using only get_transcript_job."""

    async with open_established_session() as session:
        started = await _call(
            session,
            "get_youtube_transcript",
            {"url": VIDEO_ID},
        )
        assert started["status"] == "pending"

        page: dict[str, Any] = {}
        for _ in range(20):
            page = await _call(
                session,
                "get_transcript_job",
                {"video_id": VIDEO_ID},
            )
            if page["status"] in {"partial", "ok", "error"}:
                break
            await asyncio.sleep(0)

        assert page["status"] == "partial", page
        assert page["source"] == "whisper"
        assert page["total_chars"] == len(LONG_TEXT)
        assert page["is_final"] is False
        assert page["next_cursor"]

        chunks = [_without_partial_banner(page["text"])]
        pages = 1
        while not page["is_final"]:
            pages += 1
            assert pages < 20, "cursor pagination did not terminate"
            page = await _call(
                session,
                "get_transcript_job",
                {
                    "video_id": VIDEO_ID,
                    "cursor": page["next_cursor"],
                },
            )
            assert page["source"] == "whisper"
            chunks.append(_without_partial_banner(page["text"]))

        assert page["status"] == "ok"
        assert page["is_final"] is True
        assert "next_cursor" not in page
        assert "cursor_stale" not in page
        assert "".join(chunks) == LONG_TEXT
