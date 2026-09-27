"""Whisper fallback failures & queue exhaustion — end to end through the tools.

``test_asr_runbook.py`` pins the failure *behaviors* (unreachable, timing-out,
overloaded, saturated) by driving ``run_whisper_job`` directly over a mock
cache and mock settings; this module is the client-side twin: every scenario
is driven through the real MCP tool surface — ``get_youtube_transcript``
starting the real bounded background task, ``get_transcript_job`` polling the
real registry — with only the socket-level edges stubbed (the yt-dlp download
and the ASR HTTP handler), the same harness shape as ``test_whisper_contract.py``
and ``test_whisper_reconcile.py``.

Pinned here, per failure mode:

- **Unavailable service** (connection refused at the ASR POST, runbook §6) —
  start still answers ``pending`` (job creation never probes Whisper), the
  handle walks pending → running → terminal ``asr_failed`` with the relayable
  "Whisper service request failed" message plus the retry instruction, the
  failure is owner-only, nothing is cached, the scratch audio is swept, the
  queue slot is released, and the documented re-kick — once the service is
  back — replaces the terminal entry and delivers the transcript.
- **Timing-out service** (read timeout, runbook §7) — same terminal shape
  under ``httpx.ReadTimeout``, with the queue slot freed immediately.
- **Restarting service** (connection dies mid-POST) — an in-flight job meets
  a service restart as an ordinary retriable ``asr_failed``, *not* as task
  death: the job's message is the service-failure text, never the
  ``RECONCILED_MESSAGE`` (that one is reserved for a dead driving task,
  lifecycle §6.1); the retry after the restart completes the work.
- **Saturated queue** (runbook §8) — a real backlog at
  ``YTT_MAX_PENDING_WHISPER_JOBS`` (real jobs, queued on the real
  ``YTT_MAX_CONCURRENT_WHISPER`` semaphore) denies a *new* caption-less
  request with the exact ``rate_limited`` "Whisper queue full (N/N …)"
  denial **without creating a registry entry**, while joining a queued job
  stays admitted; draining the backlog readmits the denied video, whose
  re-kick then runs to a delivered transcript.

Downstream details intentionally not duplicated here: the wire contract
(``test_whisper_asr_contract.py``), ownership across the lifecycle
(``test_job_ownership.py``), task-death reconciliation
(``test_whisper_reconcile.py``), and the operator-facing runbook pin
(``test_asr_runbook.py``).
"""

from __future__ import annotations

import asyncio
import threading
import types
from typing import Any

import httpx
import pytest

from ytt import errors
from ytt import server
from ytt import whisper as ytt_whisper
from ytt.cache import TranscriptCache
from ytt.concurrency import ConcurrencyState
from ytt.errors import NoCaptionsError
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.server import mcp
from ytt.whisper import RECONCILED_MESSAGE, WhisperJobRegistry

VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://youtu.be/{VIDEO_ID}"

ALICE = "alice@example.com"
BOB = "bob@example.com"

AUDIO_BYTES = b"\x00\x01fake-bestaudio-bytes"

VERBOSE_JSON: dict[str, Any] = {
    "text": "Never gonna give you up",
    "language": "en",
    "segments": [
        {"id": 0, "start": 0.0, "end": 2.0, "text": "Never gonna give you up"}
    ],
}

RETRY_INSTRUCTION = " Re-call get_youtube_transcript with the video URL to retry."


# ---------------------------------------------------------------------------
# Harness (same shape as test_whisper_reconcile.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """Direct ``mcp.call_tool`` runs with no HTTP request, so there is no real
    token to resolve. Ownership is simulated per call via ``_auth_as``."""
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


def _auth_as(monkeypatch, email: str | None) -> None:
    """Resolve ``_request_subject()`` to *email* (same seam as
    test_job_ownership._auth_as)."""
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


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real registry + cache + scratch over tmp dirs, wired into the server."""
    cache_dir = tmp_path / "cache"
    scratch_dir = tmp_path / "scratch"
    cache_dir.mkdir()
    scratch_dir.mkdir()

    cache = TranscriptCache(cache_dir=cache_dir, max_bytes=8 * 1024 * 1024)
    registry = WhisperJobRegistry()
    settings = server._settings_singleton

    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "whisper_registry", registry)
    monkeypatch.setattr(server, "transcript_cache", cache)
    monkeypatch.setattr(
        server, "_rate_limiter", SubjectRateLimiter.from_settings(settings)
    )
    monkeypatch.setattr(
        server, "_whisper_quota", WhisperQuota.from_settings(settings)
    )
    monkeypatch.setattr(settings, "scratch_dir", str(scratch_dir))
    monkeypatch.setattr(settings, "proxy_url", None)

    return types.SimpleNamespace(
        registry=registry,
        cache=cache,
        cache_dir=cache_dir,
        scratch=scratch_dir,
        settings=settings,
    )


def _install_no_captions(monkeypatch, duration_sec: float | None = 50.0) -> None:
    async def fake_fetch_transcript(video_id, lang, settings):
        raise NoCaptionsError(
            "No caption track available for this video.", duration_sec=duration_sec
        )

    monkeypatch.setattr("ytt.fetch.fetch_transcript", fake_fetch_transcript)


def _install_download(
    monkeypatch,
    *,
    gate: threading.Event | None = None,
) -> None:
    def fake_download(
        video_id, scratch_dir, max_audio_bytes, proxy=None, *, max_asr_duration_sec=None
    ):
        if gate is not None:
            assert gate.wait(timeout=15), "download gate never opened"
        path = f"{scratch_dir}/{video_id}.m4a"
        with open(path, "wb") as f:
            f.write(AUDIO_BYTES)
        return path

    monkeypatch.setattr(ytt_whisper, "_do_download_audio", fake_download)


# --- ASR service personalities (the one stubbed socket edge) ---------------


async def _refuse(request: httpx.Request) -> httpx.Response:
    """Service down: the TCP connect itself is refused (runbook §6)."""
    raise httpx.ConnectError("[Errno 111] Connection refused")


async def _hang_past_timeout(request: httpx.Request) -> httpx.Response:
    """Service alive but wedged: the read blows the client timeout (§7)."""
    raise httpx.ReadTimeout("timed out")


async def _die_mid_post(request: httpx.Request) -> httpx.Response:
    """Service restarts between accepting the POST and answering it."""
    await request.aread()
    raise httpx.RemoteProtocolError("Server disconnected without sending a response")


async def _transcribe(request: httpx.Request) -> httpx.Response:
    """The service, healthy."""
    await request.aread()
    return httpx.Response(200, json=VERBOSE_JSON)


#: The pristine job body, bound at import time (before any patching) so
#: ``_install_asr`` can be re-invoked mid-test (fail → service returns) —
#: resolving ``run_whisper_job`` at install time instead would capture the
#: previous test's wrapper and double-wrap.
_REAL_RUN = ytt_whisper.run_whisper_job


def _install_asr(monkeypatch, handler) -> None:
    """Real ``run_whisper_job`` with the ASR POST on a MockTransport *handler*."""

    async def run_with_stub_socket(job, registry, settings, cache, active_model):
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await _REAL_RUN(
                job, registry, settings, cache, active_model, http_client=client
            )

    monkeypatch.setattr(ytt_whisper, "run_whisper_job", run_with_stub_socket)


async def _start(
    monkeypatch=None, email: str | None = None, url: str = URL
) -> dict:
    if email is not None:
        _auth_as(monkeypatch, email)
    result = await mcp.call_tool("get_youtube_transcript", {"url": url})
    return result.structured_content


async def _poll(
    monkeypatch=None, email: str | None = None, video_id: str = VIDEO_ID
) -> dict:
    if email is not None:
        _auth_as(monkeypatch, email)
    result = await mcp.call_tool("get_transcript_job", {"video_id": video_id})
    return result.structured_content


async def _drain_background_jobs() -> None:
    tasks = [t for t in list(server._background_jobs) if not t.done()]
    if tasks:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=30
        )
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                raise r
    await asyncio.sleep(0)  # let done-callbacks run before callers snapshot


def _not_found(video_id: str) -> dict:
    """The stable denial payload — exactly what an unknown video_id returns."""
    return {
        "video_id": video_id,
        "status": "error",
        "error_code": errors.NOT_FOUND,
        "message": (
            "Job not found. Re-call get_youtube_transcript with the video URL "
            "to start a new request."
        ),
    }


def _assert_failed_work_left_nothing_behind(env) -> None:
    """A failed job writes no cache unit and sweeps its temporary audio."""
    assert list(env.cache_dir.iterdir()) == []
    assert list(env.scratch.iterdir()) == []


# ---------------------------------------------------------------------------
# Unavailable service (runbook §6) — through the tools
# ---------------------------------------------------------------------------


class TestUnavailableService:
    async def test_full_failure_lifecycle_through_the_tools(
        self, env, monkeypatch
    ) -> None:
        """Whisper down: start → pending (job creation never probes the
        service), the handle walks pending → running → terminal ``asr_failed``
        with the relayable message and the retry instruction; the failure is
        owner-only; nothing is cached; the scratch audio is gone; the queue
        slot went with the terminal state."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _refuse)
        gate = threading.Event()
        _install_download(monkeypatch, gate=gate)

        started = await _start(monkeypatch, ALICE)
        assert started["status"] == "pending"
        assert started["eta_sec"] == pytest.approx(
            50.0 * env.settings.whisper_realtime_factor
        )

        await asyncio.sleep(0.1)  # let the task reach the (gated) download
        assert (await _poll(monkeypatch, ALICE))["status"] == "running"

        gate.set()
        await _drain_background_jobs()

        failed = await _poll(monkeypatch, ALICE)
        assert failed["status"] == "error"
        assert failed["error_code"] == errors.ASR_FAILED
        assert "Whisper service request failed" in failed["message"]
        assert "Connection refused" in failed["message"]  # the operator's one clue
        assert "Traceback" not in failed["message"]       # verbatim-relayable
        assert failed["message"].endswith(RETRY_INSTRUCTION)

        again = await _poll(monkeypatch, ALICE)
        assert again == failed  # repeatable — same shape every poll

        # The failure handle is owner-only, like every other job state.
        assert await _poll(monkeypatch, BOB) == _not_found(VIDEO_ID)

        _assert_failed_work_left_nothing_behind(env)
        assert await env.registry.active_count() == 0  # slot released
        assert env.registry.size == 1  # terminal entry stays pollable till TTL

    async def test_retry_once_the_service_is_back_delivers(
        self, env, monkeypatch
    ) -> None:
        """The documented recovery is a plain re-call once the service
        returns: the terminal entry is replaced (never joined, never
        duplicated) by a fresh pending job the re-kicking subject owns, and
        the poll then delivers the transcript — the cache unit that answers
        it is the retry's, since the failed attempt cached nothing."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _refuse)
        _install_download(monkeypatch)

        first = await _start(monkeypatch, ALICE)
        assert first["status"] == "pending"
        await _drain_background_jobs()
        assert (await env.registry.get(VIDEO_ID)).status == "error"

        _install_asr(monkeypatch, _transcribe)
        retried = await _start(monkeypatch, ALICE)
        assert retried["status"] == "pending"

        replacement = await env.registry.get(VIDEO_ID)
        assert replacement.status == "pending"
        assert replacement.owner == ALICE  # the re-kick re-owns
        assert env.registry.size == 1  # replaced, not duplicated

        await _drain_background_jobs()
        delivered = await _poll(monkeypatch, ALICE)
        assert delivered["status"] == "ok"
        assert delivered["source"] == "whisper"
        assert delivered["text"] == "Never gonna give you up"
        assert await env.cache.get(VIDEO_ID, "whisper") is not None


# ---------------------------------------------------------------------------
# Timing-out service (runbook §7) — through the tools
# ---------------------------------------------------------------------------


class TestTimingOutService:
    async def test_read_timeout_fails_retriable_and_frees_the_queue_slot(
        self, env, monkeypatch
    ) -> None:
        """A service that accepts the POST and then wedges past the read
        timeout fails the job ``asr_failed`` with the timeout message — and
        the ``YTT_MAX_CONCURRENT_WHISPER`` slot goes back the moment the job
        turns terminal, so the shared service's only lane is not held by a
        corpse."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _hang_past_timeout)
        _install_download(monkeypatch)

        started = await _start(monkeypatch, ALICE)
        assert started["status"] == "pending"
        await _drain_background_jobs()

        failed = await _poll(monkeypatch, ALICE)
        assert failed["status"] == "error"
        assert failed["error_code"] == errors.ASR_FAILED
        assert "Whisper service timed out" in failed["message"]
        assert failed["message"].endswith(RETRY_INSTRUCTION)

        assert await _poll(monkeypatch, BOB) == _not_found(VIDEO_ID)
        _assert_failed_work_left_nothing_behind(env)
        assert await env.registry.active_count() == 0


# ---------------------------------------------------------------------------
# Restarting service — an in-flight job meets a mid-POST connection death
# ---------------------------------------------------------------------------


class TestRestartingService:
    async def test_mid_post_restart_is_retriable_not_reconciled(
        self, env, monkeypatch
    ) -> None:
        """A Whisper restart kills the *connection*, not the job's task: the
        job fails with the ordinary retriable service-failure code and a
        service-failure message. ``RECONCILED_MESSAGE`` is reserved for a
        dead driving task (lifecycle §6.1) and must not fire for a live one —
        the distinction an operator greps logs by."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _die_mid_post)
        _install_download(monkeypatch)

        started = await _start(monkeypatch, ALICE)
        assert started["status"] == "pending"
        await _drain_background_jobs()

        failed = await _poll(monkeypatch, ALICE)
        assert failed["status"] == "error"
        assert failed["error_code"] == errors.ASR_FAILED
        assert "Whisper service request failed" in failed["message"]
        assert "disconnected" in failed["message"]
        assert RECONCILED_MESSAGE not in failed["message"]

        assert await _poll(monkeypatch, BOB) == _not_found(VIDEO_ID)
        _assert_failed_work_left_nothing_behind(env)
        assert await env.registry.active_count() == 0

    async def test_retry_after_the_service_restarts_completes(
        self, env, monkeypatch
    ) -> None:
        """The full outage arc through the tools: the service dies mid-POST,
        comes back, and the documented re-kick runs the video to a delivered
        transcript on the fresh process."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _die_mid_post)
        _install_download(monkeypatch)

        await _start(monkeypatch, ALICE)
        await _drain_background_jobs()
        assert (await env.registry.get(VIDEO_ID)).status == "error"

        _install_asr(monkeypatch, _transcribe)
        retried = await _start(monkeypatch, ALICE)
        assert retried["status"] == "pending"

        await _drain_background_jobs()
        delivered = await _poll(monkeypatch, ALICE)
        assert delivered["status"] == "ok"
        assert delivered["text"] == "Never gonna give you up"


# ---------------------------------------------------------------------------
# Queue exhaustion (runbook §8) — a real backlog through the tool surface
# ---------------------------------------------------------------------------


def _queued_video(n: int) -> str:
    return f"queuedaa{n:03d}"  # 11 characters, canonicalizable


async def _saturate_queue(monkeypatch, env, *, depth: int) -> ConcurrencyState:
    """Fill the queue to *depth* with real tool-started jobs, parked on the
    whisper semaphore (the shared service's one lane is busy). Returns the
    patched ConcurrencyState — the caller must ``release()`` the held slot
    before draining background jobs."""
    _install_no_captions(monkeypatch)
    _install_asr(monkeypatch, _transcribe)
    _install_download(monkeypatch)

    concurrency = ConcurrencyState(max_concurrent_fetches=4, max_concurrent_whisper=1)
    monkeypatch.setattr(server, "_concurrency", concurrency)
    await concurrency.whisper_sem.acquire()  # the lane is busy
    monkeypatch.setattr(env.settings, "max_pending_whisper_jobs", depth)

    for n in range(depth):
        started = await _start(url=f"https://youtu.be/{_queued_video(n)}")
        assert started["status"] == "pending"
    assert await env.registry.active_count() == depth
    return concurrency


class TestQueueExhaustion:
    async def test_saturated_queue_denies_new_work_without_creating_a_job(
        self, env, monkeypatch
    ) -> None:
        """At the backlog cap a *new* caption-less request gets the exact
        ``rate_limited`` "Whisper queue full" denial — with no registry entry
        created and the backlog untouched — while joining an already-queued
        job stays admitted (it adds no work)."""
        concurrency = await _saturate_queue(monkeypatch, env, depth=2)

        denied = await _start()  # a third caption-less video
        assert denied["status"] == "error"
        assert denied["error_code"] == errors.RATE_LIMITED
        assert "Whisper queue full (2/2" in denied["message"]

        assert await env.registry.get(VIDEO_ID) is None  # nothing was created
        assert await env.registry.active_count() == 2    # backlog unchanged

        # Joining a queued job at full backlog: admitted, no second entry.
        rejoined = await _start(url=f"https://youtu.be/{_queued_video(0)}")
        assert rejoined["status"] == "pending"
        assert env.registry.size == 2

        concurrency.whisper_sem.release()
        await _drain_background_jobs()

    async def test_draining_the_backlog_readmits_the_denied_video(
        self, env, monkeypatch
    ) -> None:
        """Exhaustion is a queue, not a quota: once the backlog drains, the
        same video's documented re-kick is admitted, starts real work, and
        delivers the transcript — no operator intervention, no residue from
        the denial (nothing was ever created for it)."""
        concurrency = await _saturate_queue(monkeypatch, env, depth=2)

        denied = await _start()
        assert denied["error_code"] == errors.RATE_LIMITED

        concurrency.whisper_sem.release()
        await _drain_background_jobs()
        assert await env.registry.active_count() == 0

        readmitted = await _start()
        assert readmitted["status"] == "pending"  # real work this time
        fresh = await env.registry.get(VIDEO_ID)
        assert fresh is not None and fresh.status == "pending"

        await _drain_background_jobs()
        delivered = await _poll()
        assert delivered["status"] == "ok"
        assert delivered["source"] == "whisper"
        assert delivered["text"] == "Never gonna give you up"
