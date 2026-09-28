"""Regression pins for the unset ``YTT_WHISPER_URL`` reference-ASR path.

The existing no-Whisper, BYO-wire, proxy-isolation, ownership, quota, and
lifecycle modules each own their detailed contracts. This module ties the
reference default to those behaviors without duplicating them: it guards the
README/self-hosting disclosure, proves unset and empty resolve differently,
checks the first tool call's pending shape, and drives a reference endpoint
through success, outage, and proxy-configured paths.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import ytt.fetch
import ytt.whisper
from ytt import errors, server
from ytt.cache import TranscriptCache
from ytt.config import DEFAULT_WHISPER_URL, Settings
from ytt.errors import NoCaptionsError
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.whisper import WhisperJobRegistry, run_whisper_job

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_NOTE = REPO_ROOT / "docs" / "notes" / "reference-asr.md"
README = REPO_ROOT / "README.md"
SELF_HOSTING = REPO_ROOT / "docs" / "usage" / "self-hosting.md"
VIDEO_ID = "dQw4w9WgXcQ"


def test_reference_default_and_disclosure_cannot_drift() -> None:
    """The executable default and all user-facing disclosures stay aligned."""
    assert Settings.model_fields["whisper_url"].default == DEFAULT_WHISPER_URL

    note = REFERENCE_NOTE.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    self_hosting = SELF_HOSTING.read_text(encoding="utf-8")
    readme_flat = " ".join(readme.split())

    for document_name, document in (
        ("reference-ASR note", note),
        ("README", readme),
        ("self-hosting guide", self_hosting),
    ):
        assert DEFAULT_WHISPER_URL in document, (
            f"{document_name} lost the pinned reference endpoint "
            f"{DEFAULT_WHISPER_URL!r}"
        )

    assert "project-operated reference" in note
    assert "project-operated reference" in readme
    assert "audio egress" in note
    assert "audio" in self_hosting.lower()
    assert "network endpoint" in self_hosting
    assert "no third-party transcript APIs are used for YouTube" in readme_flat
    assert "YTT_WHISPER_URL" in note and "operator-overridable" in note
    for literal in (
        '"status": "pending"',
        'error_code="asr_failed"',
        "YTT_WHISPER_JOBS_PER_HOUR",
        "YTT_PROXY_URL",
        "not_found",
        "YTT_JOB_TTL_SEC",
        "restart",
    ):
        assert literal in note, f"reference-ASR note lost {literal!r}"


def test_unset_reference_default_and_empty_caption_only_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset selects the reference service; empty remains explicit disablement."""
    monkeypatch.delenv("YTT_WHISPER_URL", raising=False)
    assert Settings().whisper_url == DEFAULT_WHISPER_URL

    monkeypatch.setenv("YTT_WHISPER_URL", "")
    assert Settings().whisper_url == ""

    override = "http://operator-whisper.example:8000"
    monkeypatch.setenv("YTT_WHISPER_URL", override)
    assert Settings().whisper_url == override


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch: pytest.MonkeyPatch):
    """Direct FastMCP tool calls have no HTTP auth context."""
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for middleware in server.mcp.middleware:
        if isinstance(middleware, AuthMiddleware):
            monkeypatch.setattr(middleware, "auth", lambda ctx: True)


async def test_reference_first_tool_call_is_pending_not_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caption-less first call creates a job and never waits for Whisper."""
    settings = server._settings_singleton
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(settings, "whisper_url", DEFAULT_WHISPER_URL)
    monkeypatch.setattr(server, "whisper_registry", WhisperJobRegistry())
    cache = TranscriptCache(tmp_path / "cache", max_bytes=8 << 20)
    await cache.startup_scan()
    monkeypatch.setattr(server, "transcript_cache", cache)
    monkeypatch.setattr(
        server,
        "_rate_limiter",
        SubjectRateLimiter(capacity=20, refill_rate_per_sec=20 / 60),
    )
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(jobs_per_hour=10))

    async def no_captions(video_id: str, lang: str | None, active_settings):
        raise NoCaptionsError("no captions", duration_sec=21.0)

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", no_captions)

    async def do_nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(ytt.whisper, "run_whisper_job", do_nothing)

    result = await server.mcp.call_tool(
        "get_youtube_transcript", {"url": f"https://youtu.be/{VIDEO_ID}"}
    )
    payload = result.structured_content

    assert payload["status"] == "pending"
    assert payload["video_id"] == VIDEO_ID
    assert "error_code" not in payload
    assert payload["eta_sec"] == pytest.approx(21.0 * settings.whisper_realtime_factor)
    job = await server.whisper_registry.get(VIDEO_ID)
    assert job is not None and job.owner == "anonymous"
    await asyncio.sleep(0)  # let the no-op task and done callback settle


async def test_reference_default_still_honors_subject_asr_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reference endpoint does not bypass the new-job quota gate."""
    settings = server._settings_singleton
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(settings, "whisper_url", DEFAULT_WHISPER_URL)
    registry = WhisperJobRegistry()
    monkeypatch.setattr(server, "whisper_registry", registry)
    cache = TranscriptCache(tmp_path / "cache", max_bytes=8 << 20)
    await cache.startup_scan()
    monkeypatch.setattr(server, "transcript_cache", cache)
    monkeypatch.setattr(
        server,
        "_rate_limiter",
        SubjectRateLimiter(capacity=20, refill_rate_per_sec=20 / 60),
    )
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(jobs_per_hour=0))

    async def no_captions(video_id: str, lang: str | None, active_settings):
        raise NoCaptionsError("no captions", duration_sec=21.0)

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", no_captions)
    run_calls: list[object] = []

    async def should_not_run(*args, **kwargs):
        run_calls.append(args)

    monkeypatch.setattr(ytt.whisper, "run_whisper_job", should_not_run)

    result = await server.mcp.call_tool(
        "get_youtube_transcript", {"url": f"https://youtu.be/{VIDEO_ID}"}
    )
    payload = result.structured_content

    assert payload["status"] == "error"
    assert payload["error_code"] == errors.RATE_LIMITED
    assert "Whisper ASR quota exhausted" in payload["message"]
    assert await registry.get(VIDEO_ID) is None
    assert run_calls == []


async def _drive_reference_job(
    tmp_path: Path,
    *,
    whisper_url: str,
    handler,
    proxy_url: str | None = None,
) -> tuple[SimpleNamespace, list[str], list[str]]:
    """Run the real job body against a mocked Whisper endpoint."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    audio = scratch / f"{VIDEO_ID}.m4a"
    audio.write_bytes(b"fake audio")
    settings = Settings(
        whisper_url=whisper_url,
        proxy_url=proxy_url,
        scratch_dir=str(scratch),
        cache_backend="emptydir",
        cache_dir=str(tmp_path / "cache"),
    )
    registry = WhisperJobRegistry()
    cache = SimpleNamespace(put=AsyncMock(return_value=True))
    job, is_new = await registry.get_or_create(
        VIDEO_ID, 30.0, settings, owner="reference-test@example.com"
    )
    assert is_new
    requests: list[str] = []

    async def record(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        result = handler(request)
        if hasattr(result, "__await__"):
            result = await result
        return result

    download_proxies: list[str | None] = []

    def fake_download(*args, **kwargs) -> str:
        download_proxies.append(kwargs.get("proxy"))
        return str(audio)

    with patch("ytt.whisper._do_download_audio", side_effect=fake_download):
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            await run_whisper_job(
                job,
                registry,
                settings,
                cache,
                settings.whisper_model,
                http_client=client,
            )

    final = await registry.get(VIDEO_ID)
    assert final is not None
    world = SimpleNamespace(
        settings=settings,
        registry=registry,
        cache=cache,
        final=final,
    )
    return world, requests, download_proxies


@pytest.mark.asyncio
async def test_unset_reference_job_dials_reference_and_outage_is_asr_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default URL is the actual POST target; outage is stable ``asr_failed``."""
    monkeypatch.delenv("YTT_WHISPER_URL", raising=False)
    settings = Settings(
        scratch_dir=str(tmp_path / "scratch"),
        cache_backend="emptydir",
        cache_dir=str(tmp_path / "cache"),
    )
    assert settings.whisper_url == DEFAULT_WHISPER_URL

    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reference service unavailable", request=request)

    world, requests, _ = await _drive_reference_job(
        tmp_path,
        whisper_url=settings.whisper_url,
        handler=refuse,
    )

    assert requests == [f"{DEFAULT_WHISPER_URL}/v1/audio/transcriptions"]
    assert world.final.status == "error"
    assert world.final.error_code == errors.ASR_FAILED
    assert "Whisper service request failed" in (world.final.message or "")
    assert world.cache.put.await_count == 0
    assert not list(Path(settings.scratch_dir).glob(f"{VIDEO_ID}.*"))


@pytest.mark.asyncio
async def test_empty_reference_shape_has_no_routable_whisper_dial(
    tmp_path: Path,
) -> None:
    """The empty-value path never hands an HTTP(S) endpoint to the transport."""
    async def accepted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "unused", "segments": []})

    world, requests, _ = await _drive_reference_job(
        tmp_path,
        whisper_url="",
        handler=accepted,
    )

    assert requests == ["/v1/audio/transcriptions"]
    assert not any(url.startswith(("http://", "https://")) for url in requests)
    assert world.final.status == "error"
    assert world.final.error_code == errors.ASR_FAILED


@pytest.mark.asyncio
async def test_reference_job_keeps_proxy_out_of_asr_and_applies_same_path_controls(
    tmp_path: Path,
) -> None:
    """A reference job uses the normal YouTube/proxy boundary, not ASR proxying."""
    async def success(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "reference transcript",
                "language": "en",
                "segments": [{"start": 0.0, "end": 1.0, "text": "reference"}],
            },
        )

    world, requests, download_proxies = await _drive_reference_job(
        tmp_path,
        whisper_url=DEFAULT_WHISPER_URL,
        handler=success,
        proxy_url="http://proxy.example:3128",
    )

    assert world.final.status == "done"
    assert requests == [f"{DEFAULT_WHISPER_URL}/v1/audio/transcriptions"]
    # The direct-first audio leg receives no proxy on a successful direct try;
    # the ASR client is separately constructed without any proxy argument.
    assert download_proxies == [None]
    assert world.cache.put.await_count == 1
