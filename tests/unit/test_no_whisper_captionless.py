"""Caption-less video in no-Whisper mode — the failure contract at the
transport level (bead ``ytt-62c350b3``, the caption-less slice of the
caption-only chain; the boot slice is ``test_no_whisper_boot.py``, the
captioned sibling ``test_no_whisper_captions.py``).

Under a deliberate no-Whisper deployment (``YTT_WHISPER_URL=""``) a video
with no captions is the one request the deployment cannot serve, and
``docs/usage/tools.md`` ("``no_captions_asr_failed`` is not a tool error
code") states the caller-visible shape precisely:

1. ``get_youtube_transcript`` answers ``status="pending"`` with **no
   ``error_code`` field** — job creation never probes Whisper;
2. the poll ends ``status="error"`` with the job's real code,
   ``asr_failed`` — never ``no_captions_asr_failed``, which per
   ``ytt/errors.py`` is a WhisperJob-internal / metric-only label, not a
   ``TranscriptResult`` error_code.

``test_asr_runbook.py`` holds the registry-level outcome for exactly this
scenario (``run_whisper_job`` driven directly over a bare real client) but
says nothing about the wire; ``test_whisper_failure_e2e.py`` drives the
tools but with a *configured* service. The delta pinned here — through the
real shipping request path (``get_youtube_transcript`` starting the real
bounded background task, ``get_transcript_job`` polling the real registry),
the same harness shape as that e2e module — is the transport itself:

- a **counted transport** on the ASR client records every request the job
  tries to put on the wire, and refuses each one, so "no ASR call" is
  measured, not inferred: whatever the transport was handed was attempted,
  and whatever it was never handed never left the process. httpx hands
  scheme-less URLs to a custom transport without validating the scheme
  (the real connection pool is what rejects them), so the empty-URL POST
  surfaces there as ``/v1/audio/transcriptions`` — an artifact with no
  endpoint to hit. The no-outbound-call claim is therefore: nothing the
  transport was ever handed carried an http(s) endpoint.
- the audio-download seam runs first and exactly once, recorded in the
  same ordered event log the transport appends to — download, then the
  single refused POST, never the reverse — so the no-call claim is not
  vacuously true of a job that died before doing work, and job creation
  (everything up to and including the pending answer) demonstrably never
  reached for ASR.

And the storage guarantees the runbook §6 pin holds for a configured-but-down
service hold here too: ``cache.put`` is never awaited (a failed job writes
no cache unit), both store dirs stay empty, and the scratch audio is swept.

Finally, ``ytt_whisper_errors_total{reason="no_captions_asr_failed"}`` — the
label this very failure is *named* for — is pinned the only way the scope
allows. The counter is registered-but-uninstrumented (runbook §4: "no call
site in 0.2.21, so the series is absent"; plan §Observability still
describes it as live), so an increment assertion would pin a behavior no
production code performs, and adding the call site is a production change
this bead explicitly rules out. What *is* pinned is the honest inverse: the
caption-less lifecycle leaves the counter untouched (0.0 before, 0.0
after). That is a drift guard in both directions — if instrumentation ever
lands, this test fails and forces the flip to a real increment assertion
*in the same commit* as the runbook §4 doc update that §4 reserves for
exactly that change; and if the failure path ever started mutating
counters without the docs, the same assertion catches it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import types
from unittest.mock import AsyncMock

import httpx
import pytest

from ytt import errors
from ytt import server
from ytt import whisper as ytt_whisper
from ytt.cache import TranscriptCache
from ytt.errors import NoCaptionsError
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.server import mcp
from ytt.whisper import WhisperJobRegistry

VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://youtu.be/{VIDEO_ID}"

ALICE = "alice@example.com"

#: What the stubbed fetch metadata says the video runs for — it feeds the
#: pending ETA the caller is told to relay.
DURATION_SEC = 50.0

AUDIO_BYTES = b"\x00\x01fake-bestaudio-bytes"

RETRY_INSTRUCTION = " Re-call get_youtube_transcript with the video URL to retry."


# ---------------------------------------------------------------------------
# Harness (same shape as test_whisper_failure_e2e.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """Direct ``mcp.call_tool`` runs with no HTTP request, so there is no real
    token to resolve."""
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


def _auth_as(monkeypatch, email: str) -> None:
    """Resolve ``_request_subject()`` to *email* (the test_job_ownership seam)."""
    from fastmcp.server import dependencies as deps
    from fastmcp.server.auth.auth import AccessToken

    token = AccessToken(
        token="faketoken",
        client_id="test-client",
        scopes=[],
        expires_at=None,
        claims={"email": email, "email_verified": True},
    )
    monkeypatch.setattr(deps, "get_access_token", lambda: token)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real registry + cache + scratch over tmp dirs, wired into the server —
    with the ASR endpoint deliberately unconfigured (the no-Whisper
    deployment's ``YTT_WHISPER_URL=""`` spelling, never the default)."""
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
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota.from_settings(settings))
    monkeypatch.setattr(settings, "scratch_dir", str(scratch_dir))
    monkeypatch.setattr(settings, "proxy_url", None)
    monkeypatch.setattr(settings, "whisper_url", "")

    return types.SimpleNamespace(
        registry=registry,
        cache=cache,
        cache_dir=cache_dir,
        scratch=scratch_dir,
        settings=settings,
    )


def _install_no_captions(monkeypatch) -> None:
    """The video is real and fetchable — it just has no caption track."""

    async def fake_fetch_transcript(video_id, lang, settings):
        raise NoCaptionsError(
            "No caption track available for this video.", duration_sec=DURATION_SEC
        )

    monkeypatch.setattr("ytt.fetch.fetch_transcript", fake_fetch_transcript)


def _install_download(monkeypatch, events: list[str], gate: threading.Event) -> None:
    """The yt-dlp audio download, on disk and in the shared event log —
    parked on *gate* so a test can look around before the job's one egress
    (the ASR POST) is attempted."""

    def fake_download(
        video_id, scratch_dir, max_audio_bytes, proxy=None, *, max_asr_duration_sec=None
    ):
        assert gate.wait(timeout=15), "download gate never opened"
        path = f"{scratch_dir}/{video_id}.m4a"
        with open(path, "wb") as f:
            f.write(AUDIO_BYTES)
        events.append("audio-download")
        return path

    monkeypatch.setattr(ytt_whisper, "_do_download_audio", fake_download)


class _CountingASRTransport(httpx.AsyncBaseTransport):
    """Record every request the ASR client tries to put on the wire — and
    refuse it. Nothing can leave the process through this transport, which
    is what makes its log a proof."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        #: Every URL the transport was handed, in order.
        self.requested: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        self._events.append("asr-post")
        raise httpx.ConnectError(
            "[no-Whisper pin] an outbound ASR request must never get this far"
        )


#: The pristine job body, bound at import time (before any patching) so the
#: install helper can never capture a previous test's wrapper.
_REAL_RUN = ytt_whisper.run_whisper_job


def _install_asr(monkeypatch, transport: _CountingASRTransport) -> None:
    """Real ``run_whisper_job``, its ASR POST riding the counted transport."""

    async def run_over_the_counted_transport(
        job, registry, settings, cache, active_model
    ):
        async with httpx.AsyncClient(transport=transport) as client:
            await _REAL_RUN(
                job, registry, settings, cache, active_model, http_client=client
            )

    monkeypatch.setattr(ytt_whisper, "run_whisper_job", run_over_the_counted_transport)


async def _start(monkeypatch) -> dict:
    _auth_as(monkeypatch, ALICE)
    result = await mcp.call_tool("get_youtube_transcript", {"url": URL})
    return result.structured_content


async def _poll(monkeypatch) -> dict:
    _auth_as(monkeypatch, ALICE)
    result = await mcp.call_tool("get_transcript_job", {"video_id": VIDEO_ID})
    return result.structured_content


async def _drain_background_jobs() -> None:
    tasks = [t for t in list(server._background_jobs) if not t.done()]
    if tasks:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=30
        )
        for r in results:
            if isinstance(r, BaseException) and not isinstance(
                r, asyncio.CancelledError
            ):
                raise r
    await asyncio.sleep(0)  # let done-callbacks run before callers snapshot


def _whisper_error_sample(reason: str) -> float:
    """The ``ytt_whisper_errors_total`` value for *reason* — 0.0 when the
    child series does not exist (the registered-but-inert state of runbook
    §4 leaves it absent, and an absent series and a 0.0 series are equally
    "untouched" to a scraper)."""
    from prometheus_client import REGISTRY

    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == "ytt_whisper_errors_total" and (
                sample.labels.get("reason") == reason
            ):
                return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# The pending answer — no error_code field, no ASR contact
# ---------------------------------------------------------------------------


async def test_start_answers_pending_with_no_error_code_field(env, monkeypatch) -> None:
    """Job creation never probes Whisper: the caption-less request is
    answered ``pending`` — a pollable handle plus a relayable ETA — and the
    pending payload carries no ``error_code`` at all, even though the job it
    started is already doomed. While the job sits parked on its download
    (before its one egress), the counted transport has been handed nothing."""
    _install_no_captions(monkeypatch)
    events: list[str] = []
    gate = threading.Event()
    _install_download(monkeypatch, events, gate)
    transport = _CountingASRTransport(events)
    _install_asr(monkeypatch, transport)

    started = await _start(monkeypatch)

    assert started["status"] == "pending"
    assert started["video_id"] == VIDEO_ID
    assert "error_code" not in started  # the contract's exact shape
    assert started["eta_sec"] == pytest.approx(
        DURATION_SEC * env.settings.whisper_realtime_factor
    )
    assert started["message"]  # relayable, non-empty

    # The handle is real, and while the job waits on its download — the step
    # that precedes its only possible ASR contact — the wire is untouched.
    assert await env.registry.get(VIDEO_ID) is not None
    await asyncio.sleep(0.1)  # let the task reach the (gated) download
    assert transport.requested == []

    gate.set()
    await _drain_background_jobs()


# ---------------------------------------------------------------------------
# The terminal poll — asr_failed, having never reached an ASR endpoint
# ---------------------------------------------------------------------------


async def test_poll_ends_asr_failed_having_never_reached_an_asr_endpoint(
    env, monkeypatch
) -> None:
    """The full caption-less no-Whisper lifecycle on one wire: the job runs
    for real (download first, exactly once), its single POST attempt is
    refused by the counted transport carrying a scheme-less URL — the
    unconfigured-endpoint artifact, with no http(s) endpoint to hit — and
    the caller's poll ends ``error`` / ``asr_failed`` with the retry
    instruction. ``no_captions_asr_failed`` (the metric-only label) appears
    nowhere in the payload, nothing is cached, and the scratch audio is
    swept."""
    _install_no_captions(monkeypatch)
    events: list[str] = []
    gate = threading.Event()
    _install_download(monkeypatch, events, gate)
    transport = _CountingASRTransport(events)
    _install_asr(monkeypatch, transport)

    # Criterion: "Nothing is cached (cache.put await count 0)" — a spy over
    # the real store, so the disk emptiness below and the await count are
    # two views of the same guarantee.
    put_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(env.cache, "put", put_spy)

    started = await _start(monkeypatch)
    assert started["status"] == "pending"

    gate.set()
    await _drain_background_jobs()

    failed = await _poll(monkeypatch)
    assert failed["status"] == "error"
    assert failed["error_code"] == errors.ASR_FAILED
    # The metric-only label is not the tool's code — not as the error_code,
    # and not anywhere in the relayable payload.
    assert failed["error_code"] != errors.NO_CAPTIONS_ASR_FAILED
    assert "no_captions_asr_failed" not in json.dumps(failed)
    # The message is the ASR-POST step's request-failure shape (a download
    # failure would read differently), verbatim-relayable, with the
    # documented recovery attached.
    assert "Whisper service request failed" in failed["message"]
    assert "Traceback" not in failed["message"]
    assert failed["message"].endswith(RETRY_INSTRUCTION)

    again = await _poll(monkeypatch)
    assert again == failed  # repeatable — same shape every poll

    # The wire: download first (the job really ran), then exactly one
    # refused POST — and the one URL the transport was ever handed is the
    # scheme-less unconfigured-endpoint artifact. No http(s) URL was ever
    # attempted, so no request could have reached an ASR endpoint.
    assert events == ["audio-download", "asr-post"]
    assert transport.requested == ["/v1/audio/transcriptions"]
    assert not [u for u in transport.requested if u.startswith(("http://", "https://"))]

    # A failed job writes no cache unit (the spy never awaited; the store on
    # disk stayed empty too) and the scratch audio was swept.
    assert put_spy.await_count == 0
    assert list(env.cache_dir.iterdir()) == []
    assert list(env.scratch.iterdir()) == []

    # The queue slot went with the terminal state; the handle stays
    # pollable until TTL GC.
    assert await env.registry.active_count() == 0
    assert env.registry.size == 1


# ---------------------------------------------------------------------------
# The metric — the reason-labelled counter this failure is named for
# ---------------------------------------------------------------------------


async def test_whisper_errors_counter_stays_inert_through_the_failure(
    env, monkeypatch
) -> None:
    """``ytt_whisper_errors_total{reason="no_captions_asr_failed"}`` — the
    label *named* for this exact failure — does not move through the full
    lifecycle, because the counter has no call site (runbook §4: "no call
    site in 0.2.21, so the series is absent"; the only ``.labels()`` calls
    in the tree are tests, the only ``.inc()`` sites are the canary, the
    rate limiter and the fetch-block counter). An increment assertion here
    would pin a behavior no production code performs, and adding the call
    site is a production change this bead's scope rules out — so the
    honest pin is the inverse, and it guards in both directions:

    - instrumentation landing later makes ``after == 0.0`` fail, forcing
      the flip to a real increment assertion in the same commit as the
      runbook §4 doc update that §4 reserves for exactly that change;
    - the failure path starting to mutate counters without the docs fails
      the same assertion.

    Either way the caption-less failure's caller-visible contract is
    already fully pinned by the two transport tests above — the caller
    never learns or loses anything through this counter.
    """
    before = _whisper_error_sample("no_captions_asr_failed")

    _install_no_captions(monkeypatch)
    events: list[str] = []
    gate = threading.Event()
    _install_download(monkeypatch, events, gate)
    _install_asr(monkeypatch, _CountingASRTransport(events))

    started = await _start(monkeypatch)
    assert started["status"] == "pending"

    gate.set()
    await _drain_background_jobs()

    failed = await _poll(monkeypatch)
    assert failed["status"] == "error"
    assert failed["error_code"] == errors.ASR_FAILED

    after = _whisper_error_sample("no_captions_asr_failed")
    assert before == 0.0
    assert after == 0.0
