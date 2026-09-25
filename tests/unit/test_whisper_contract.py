"""Whisper job lifecycle & restart contract tests (docs/notes/whisper-lifecycle.md).

This module pins the async ASR job contract end to end: the job state machine,
what each tool call returns in every state, polling idempotence, expiration,
cleanup timing, and what a process restart does to the in-memory registry and
the scratch volume. The spec is ``docs/notes/whisper-lifecycle.md`` — every
clause below cites the section it pins.

The harness is deliberately close to production: the *real* MCP tools
(``mcp.call_tool``), the *real* ``WhisperJobRegistry``, the *real*
``TranscriptCache`` over a temp dir, and the *real* ``run_whisper_job`` body
drive each scenario. Only the three socket-level edges are stubbed — the
caption fetch (raises ``NoCaptionsError``, exactly what a real caption-less
video produces), the yt-dlp audio download, and the ASR HTTP socket
(``httpx.MockTransport``; the wire protocol itself is pinned separately by
``test_whisper_asr_contract.py``). The background task is the one the tool
handler really starts (resolved from :mod:`ytt.whisper` at call time — the
mechanism the server explicitly provides for stubbing the job body), with a
MockTransport client injected; download and POST bytes flow through the real
pipeline into the real cache.

Contract areas (spec §8):

- start (§2): pending shape, ETA math, join-not-duplicate, duration cap
- polling (§3): all six shapes, idempotence, budget-free, never re-triggers
- success (§1+§3): lifecycle → delivered transcript → repeatable → cache-first
- failure (§1+§3): stable error, never cached, no silent re-trigger, re-kick
  restarts work (terminal entries are never joinable — spec §1)
- expiration (§4): TTL GC of terminal handles, stale-running GC, pending never
- restart (§6): registry lost → not_found → re-kick; completed result survives
  in cache; without a surviving cache the re-kick starts a fresh job
- stale scratch (§5): startup sweep counts/bytes/idempotence/dirs untouched,
  per-video sweep isolation, and the every-attempt sweep in the job's finally
"""

from __future__ import annotations

import asyncio
import threading
import time
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from ytt import errors
from ytt import server
from ytt import whisper as ytt_whisper
from ytt.cache import TranscriptCache
from ytt.errors import NoCaptionsError, YttError
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.server import mcp
from ytt.whisper import (
    WhisperJobRegistry,
    _sweep_video_scratch,
    startup_sweep,
)

VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://youtu.be/{VIDEO_ID}"
OTHER_VIDEO_ID = "abcdefghijk"
AUDIO_BYTES = b"\x00\x01fake-bestaudio-bytes"

VERBOSE_JSON: dict[str, Any] = {
    "text": "Never gonna give you up",
    "language": "en",
    "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": "Never gonna give you up"}],
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """These tests exercise the lifecycle contract, not auth (see the same
    fixture in test_server.py): direct ``mcp.call_tool`` runs with no HTTP
    request, so there is no real token to resolve."""
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


class _FakeASR:
    """Minimal OpenAI-compatible transcription service stand-in.

    The ASR wire contract (multipart shape, field names, model selection,
    error mapping) is pinned by ``test_whisper_asr_contract.py``; this fake
    only needs to answer deterministically so the *lifecycle* around the
    socket can be observed.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: Any = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.json_body = json_body if json_body is not None else dict(VERBOSE_JSON)
        self.raise_exc = raise_exc
        self.calls = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return httpx.Response(self.status_code, json=self.json_body)


@pytest.fixture
def contract_env(monkeypatch, tmp_path):
    """Real registry + real cache + real scratch dir, wired into the server.

    Replaces the module singletons the tool handlers resolve at call time with
    per-test instances over ``tmp_path`` so tests never touch the configured
    cache/scratch volumes. Also installs fresh limiters (the module ones are
    process-global and other tests share them) and points the settings
    singleton's ``scratch_dir`` at the temp dir — ``run_whisper_job`` reads it
    for both the download target and the finally-block sweep.
    """
    cache_dir = tmp_path / "cache"
    scratch_dir = tmp_path / "scratch"
    cache_dir.mkdir()
    scratch_dir.mkdir()

    cache = TranscriptCache(cache_dir=cache_dir, max_bytes=8 * 1024 * 1024)
    registry = WhisperJobRegistry()
    settings = server._settings_singleton

    # Handlers resolve settings via the lru_cached ``get_settings()``. Any
    # earlier test that clears that cache (test_auth.py legitimately does)
    # makes ``get_settings()`` return a fresh env-built Settings — a different
    # object from the singleton patched below — so e.g. ``scratch_dir`` would
    # silently revert to the env default mid-suite. Pin the server's resolver
    # to the singleton for the duration of the test.
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
    """Caption fetch raises NoCaptionsError — the real shape of a caption-less
    video (the trigger for the whole Whisper fallback, spec §2)."""

    async def fake_fetch_transcript(video_id, lang, settings):
        raise NoCaptionsError(
            "No caption track available for this video.", duration_sec=duration_sec
        )

    monkeypatch.setattr("ytt.fetch.fetch_transcript", fake_fetch_transcript)


def _install_download(
    monkeypatch,
    *,
    fail: YttError | None = None,
    gate: threading.Event | None = None,
) -> None:
    """Stub the yt-dlp audio download at its source module.

    With ``gate``, the fake blocks until the event is set — a deterministic
    "job still in flight" window for the join test.
    """

    def fake_download(
        video_id, scratch_dir, max_audio_bytes, proxy=None, *, max_asr_duration_sec=None
    ):
        if gate is not None:
            assert gate.wait(timeout=15), "download gate never opened"
        if fail is not None:
            raise fail
        path = Path(scratch_dir) / f"{video_id}.m4a"
        path.write_bytes(AUDIO_BYTES)
        return str(path)

    monkeypatch.setattr(ytt_whisper, "_do_download_audio", fake_download)


def _install_asr(monkeypatch, service: _FakeASR) -> None:
    """Run the real ``run_whisper_job`` with the ASR socket on MockTransport.

    The tool handler resolves ``run_whisper_job`` from :mod:`ytt.whisper` at
    call time (documented test seam on ``_run_whisper_job_bounded``), so the
    tool-started background task picks up this wrapper — real FSM transitions,
    real download stub, real cache write, only the socket replaced.
    """
    real_run = ytt_whisper.run_whisper_job

    async def run_with_stub_socket(job, registry, settings, cache, active_model):
        transport = httpx.MockTransport(service.handle)
        async with httpx.AsyncClient(transport=transport) as client:
            await real_run(
                job, registry, settings, cache, active_model, http_client=client
            )

    monkeypatch.setattr(ytt_whisper, "run_whisper_job", run_with_stub_socket)


async def _start(url: str = URL) -> dict:
    result = await mcp.call_tool("get_youtube_transcript", {"url": url})
    return result.structured_content


async def _poll(video_id: str = VIDEO_ID) -> dict:
    result = await mcp.call_tool("get_transcript_job", {"video_id": video_id})
    return result.structured_content


async def _drain_background_jobs() -> None:
    """Await every background transcription task the tools have started."""
    tasks = [t for t in list(server._background_jobs) if not t.done()]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
    await asyncio.sleep(0)  # let done-callbacks run before callers snapshot


def _quota_tokens(subject: str = "anonymous") -> float:
    """Remaining ASR-quota tokens for *subject* (unborn bucket = full)."""
    quota = server._whisper_quota
    bucket = quota._limiter._buckets.get(subject)
    if bucket is None:
        return float(quota.jobs_per_hour)
    return bucket._tokens


# ---------------------------------------------------------------------------
# §2 Start contract
# ---------------------------------------------------------------------------


class TestStartContract:
    async def test_start_response_shape_and_real_task_runs(
        self, contract_env, monkeypatch
    ) -> None:
        """§2 — pending shape, then the real background task drives the job to
        done: the start path doesn't just *describe* work, it starts it."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())

        sc = await _start()
        assert sc["video_id"] == VIDEO_ID
        assert sc["status"] == "pending"
        assert sc["eta_sec"] == pytest.approx(
            50.0 * contract_env.settings.whisper_realtime_factor
        )
        assert "Whisper ASR" in sc["message"]
        assert f"~{sc['eta_sec']:.0f}s" in sc["message"]

        job = await contract_env.registry.get(VIDEO_ID)
        assert job is not None and job.status == "pending"

        await _drain_background_jobs()
        job = await contract_env.registry.get(VIDEO_ID)
        assert job is not None and job.status == "done"
        assert job.result_ref == f"{VIDEO_ID}.whisper"

    async def test_start_without_known_duration_omits_eta(
        self, contract_env, monkeypatch
    ) -> None:
        """§2 — ``duration_sec=None`` (plain empty_body, no metadata) →
        ``eta_sec`` is null and the message omits the ETA clause."""
        _install_no_captions(monkeypatch, duration_sec=None)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())

        sc = await _start()
        assert sc["status"] == "pending"
        assert sc["eta_sec"] is None
        assert "~" not in sc["message"]

        await _drain_background_jobs()

    async def test_second_start_joins_in_flight_job_never_duplicates(
        self, contract_env, monkeypatch
    ) -> None:
        """§1/§2 — Invariant 2: a start while the job is in flight joins it
        (same single registry entry, no second task, no double quota charge —
        the join's charge is refunded)."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        gate = threading.Event()
        _install_download(monkeypatch, gate=gate)

        first = await _start()
        assert first["status"] == "pending"

        # Let the background task reach the (blocked) download.
        await asyncio.sleep(0.1)
        job = await contract_env.registry.get(VIDEO_ID)
        assert job is not None and job.status == "running"

        second = await _start()
        assert second["status"] == "pending"
        assert second["video_id"] == VIDEO_ID
        assert contract_env.registry.size == 1

        # Exactly one start's worth of quota spent: the join refunded its own
        # charge (spec §2 gate 2).
        assert _quota_tokens() == pytest.approx(
            float(contract_env.settings.whisper_jobs_per_hour) - 1.0, abs=0.05
        )

        gate.set()
        await _drain_background_jobs()
        job = await contract_env.registry.get(VIDEO_ID)
        assert job is not None and job.status == "done"

    async def test_duration_cap_refuses_before_any_state_change(
        self, contract_env, monkeypatch
    ) -> None:
        """§2 gate 3 — over-long video: too_long_for_asr, no registry entry,
        no quota spent, no background task (the cap lands before registration)."""
        _install_no_captions(
            monkeypatch,
            duration_sec=contract_env.settings.max_asr_duration_sec + 1.0,
        )

        sc = await _start()
        assert sc["status"] == "error"
        assert sc["error_code"] == errors.TOO_LONG_FOR_ASR
        assert contract_env.registry.size == 0
        assert _quota_tokens() == pytest.approx(
            float(contract_env.settings.whisper_jobs_per_hour), abs=0.05
        )
        assert not server._background_jobs


# ---------------------------------------------------------------------------
# §3 Polling contract — one tool, one argument, every registry state
# ---------------------------------------------------------------------------


class TestPollingContract:
    async def test_poll_absent_video_is_not_found_and_creates_nothing(
        self, contract_env
    ) -> None:
        """§3/§4 — unknown id: not_found pointing at the re-kick; polls never
        register a job or start work."""
        sc = await _poll()
        assert sc["status"] == "error"
        assert sc["error_code"] == errors.NOT_FOUND
        assert "get_youtube_transcript" in sc["message"]
        assert contract_env.registry.size == 0
        assert not server._background_jobs

    async def test_poll_pending_shape(self, contract_env) -> None:
        """§3 — pending: status=pending with the job's ETA."""
        await contract_env.registry.get_or_create(
            VIDEO_ID, 50.0, contract_env.settings
        )
        sc = await _poll()
        assert sc["status"] == "pending"
        assert sc["eta_sec"] == pytest.approx(
            50.0 * contract_env.settings.whisper_realtime_factor
        )
        assert "queued" in sc["message"]

    async def test_poll_running_shape(self, contract_env) -> None:
        """§3 — running: status=running with the ETA (the FSM state the job
        task enters as its first act)."""
        await contract_env.registry.get_or_create(
            VIDEO_ID, 50.0, contract_env.settings
        )
        await contract_env.registry.update_status(VIDEO_ID, "running")
        sc = await _poll()
        assert sc["status"] == "running"
        assert sc["eta_sec"] is not None
        assert "in progress" in sc["message"]

    async def test_poll_error_shape_relays_stable_code_and_message(
        self, contract_env
    ) -> None:
        """§3 — error: the job's stable error_code verbatim plus its
        relayable message with the re-call-to-retry instruction."""
        await contract_env.registry.get_or_create(
            VIDEO_ID, 50.0, contract_env.settings
        )
        await contract_env.registry.update_status(
            VIDEO_ID,
            "error",
            error_code=errors.IP_BLOCKED,
            message="Audio download failed: sign in to confirm you're not a bot.",
        )
        sc = await _poll()
        assert sc["status"] == "error"
        assert sc["error_code"] == errors.IP_BLOCKED
        assert sc["message"] == (
            "Audio download failed: sign in to confirm you're not a bot."
            " Re-call get_youtube_transcript with the video URL to retry."
        )

    async def test_polls_are_budget_free_under_fail_closed_limits(
        self, contract_env, monkeypatch
    ) -> None:
        """§3 idempotence — with a deny-all rate limiter AND a deny-all ASR
        quota installed, polling still works: a client waiting on one
        transcription must not drain its own budget."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        await _start()
        await _drain_background_jobs()

        monkeypatch.setattr(
            server, "_rate_limiter", SubjectRateLimiter(capacity=0, refill_rate_per_sec=0.0)
        )
        monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(jobs_per_hour=0))

        for _ in range(3):
            sc = await _poll()
            assert sc["status"] == "ok"  # the done transcript, not rate_limited

    async def test_polling_is_read_only_and_repeatable(
        self, contract_env, monkeypatch
    ) -> None:
        """§3 idempotence — repeated polls of a done job return the same
        transcript, never mutate the registry, and never start work."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        await _start()
        await _drain_background_jobs()

        first = await _poll()
        tasks_after_first = len(server._background_jobs)
        for _ in range(3):
            again = await _poll()
            assert again["status"] == first["status"] == "ok"
            assert again["text"] == first["text"]
            assert again["source"] == "whisper"
        assert contract_env.registry.size == 1
        assert (await contract_env.registry.get(VIDEO_ID)).status == "done"
        # The drained task set is empty (finished tasks discard themselves)
        # and the polls added nothing to it — polls never start work (§3).
        assert tasks_after_first == 0
        assert len(server._background_jobs) == tasks_after_first


# ---------------------------------------------------------------------------
# §1 + §3 Success — lifecycle → delivered transcript → cache-first re-kick
# ---------------------------------------------------------------------------


class TestSuccessContract:
    async def test_full_lifecycle_delivers_transcript_and_never_caches_audio(
        self, contract_env, monkeypatch
    ) -> None:
        """§1 pending→running→done; §3 done row — the poll returns the
        transcript itself (build_page full shape), the unit is ``lang='whisper'``
        with the detected language in the sidecar metadata, the result is
        repeatable, and the scratch audio is gone (Invariant 4)."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())

        started = await _start()
        assert started["status"] == "pending"

        await _drain_background_jobs()

        sc = await _poll()
        assert sc["status"] == "ok"
        assert sc["source"] == "whisper"
        assert sc["lang"] == "whisper"  # the fallback unit key, not the BCP-47 tag
        assert sc["text"] == "Never gonna give you up"
        assert sc["is_final"] is True
        assert sc["video_id"] == VIDEO_ID

        # Repeatable: same transcript on the next poll.
        again = await _poll()
        assert again["text"] == sc["text"]

        # §5 — audio deleted in the job's finally; nothing left in scratch.
        assert list(contract_env.scratch.iterdir()) == []

        # The cache unit exists as <id>.whisper.* with the detected language
        # in the sidecar (where §3's "lang" row really lives).
        unit_txt = contract_env.cache_dir / f"{VIDEO_ID}.whisper.txt"
        assert unit_txt.is_file()
        sidecar = (contract_env.cache_dir / f"{VIDEO_ID}.whisper.json").read_text()
        assert '"detected_language"' in sidecar

    async def test_rekick_after_done_is_answered_cache_first(
        self, contract_env, monkeypatch
    ) -> None:
        """§4 recovery — re-calling get_youtube_transcript after a completed
        job is answered from the cache (status=ok immediately; no new job, no
        second transcription, no quota spend)."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        asr = _FakeASR()
        _install_asr(monkeypatch, asr)

        await _start()
        await _drain_background_jobs()
        assert asr.calls == 1

        tokens_before = _quota_tokens()
        sc = await _start()
        assert sc["status"] == "ok"
        assert sc["source"] == "whisper"
        assert sc["text"] == "Never gonna give you up"
        assert asr.calls == 1  # the POST ran exactly once, ever
        assert contract_env.registry.size == 1
        assert (await contract_env.registry.get(VIDEO_ID)).status == "done"
        assert _quota_tokens() == tokens_before


# ---------------------------------------------------------------------------
# §1 + §3 Failure — stable error, never cached, re-kick restarts work
# ---------------------------------------------------------------------------


class TestFailureContract:
    async def test_failure_yields_stable_repeatable_error_and_never_caches(
        self, contract_env, monkeypatch
    ) -> None:
        """§1 running→error; §3 error row; §5 — a failed job surfaces a stable
        error_code with a relayable message, repeats on every poll, triggers
        nothing, writes no cache unit, and leaves no scratch audio."""
        _install_no_captions(monkeypatch)
        _install_download(
            monkeypatch, fail=YttError(errors.IP_BLOCKED, "egress blocked")
        )
        _install_asr(monkeypatch, _FakeASR())

        started = await _start()
        assert started["status"] == "pending"
        await _drain_background_jobs()

        first = await _poll()
        assert first["status"] == "error"
        assert first["error_code"] == errors.IP_BLOCKED
        assert "egress blocked" in first["message"]
        assert "Re-call get_youtube_transcript" in first["message"]

        for _ in range(2):
            again = await _poll()
            assert again == first  # repeatable, byte-identical

        # §5 — failed transcripts are never cached.
        assert await contract_env.cache.get(VIDEO_ID, "whisper") is None
        assert not (contract_env.cache_dir / f"{VIDEO_ID}.whisper.txt").exists()
        assert list(contract_env.scratch.iterdir()) == []

        # §3 — polling never re-triggers work: no new task (the job's one
        # task already ran and discarded itself from the drained set), and
        # the terminal entry stays pollable.
        assert not server._background_jobs
        assert (await contract_env.registry.get(VIDEO_ID)).status == "error"

    async def test_rekick_after_failure_replaces_terminal_entry_and_restarts_work(
        self, contract_env, monkeypatch
    ) -> None:
        """§1 — terminal entries are never joinable: the documented retry
        (re-call get_youtube_transcript) replaces the dead ``error`` entry
        with a fresh pending job and restarts the work, instead of dead-ending
        on the old entry until TTL GC."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        _install_download(
            monkeypatch, fail=YttError(errors.IP_BLOCKED, "egress blocked")
        )

        await _start()
        await _drain_background_jobs()
        failed = await contract_env.registry.get(VIDEO_ID)
        assert failed.status == "error"

        # Now let the retry actually succeed.
        _install_download(monkeypatch)
        retried = await _start()
        assert retried["status"] == "pending"

        replacement = await contract_env.registry.get(VIDEO_ID)
        assert replacement is not failed
        assert replacement.status == "pending"
        assert contract_env.registry.size == 1

        await _drain_background_jobs()
        done = await contract_env.registry.get(VIDEO_ID)
        assert done.status == "done"
        sc = await _poll()
        assert sc["status"] == "ok"


# ---------------------------------------------------------------------------
# §4 not_found and expiration — TTL GC / stale-running GC / pending exemption
# ---------------------------------------------------------------------------


class TestExpirationContract:
    async def test_terminal_handle_expires_to_not_found(
        self, contract_env, monkeypatch
    ) -> None:
        """§4 — a done entry older than YTT_JOB_TTL_SEC is removed by the TTL
        GC; the result stays in the cache but the handle is gone, so the next
        poll is a plain not_found."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        await _start()
        await _drain_background_jobs()
        assert (await _poll())["status"] == "ok"

        job = await contract_env.registry.get(VIDEO_ID)
        job.created_at -= contract_env.settings.job_ttl_sec + 1.0

        removed = await contract_env.registry.run_ttl_gc(contract_env.settings)
        assert removed == 1
        assert await contract_env.registry.get(VIDEO_ID) is None

        sc = await _poll()
        assert sc["error_code"] == errors.NOT_FOUND

    async def test_stale_running_handle_is_reaped_by_gc(
        self, contract_env
    ) -> None:
        """§4 — a running entry whose task can no longer be alive (age past
        WHISPER_TIMEOUT + TTL) is removed; a pending entry has no TTL and
        survives the same GC pass (the queue cap bounds it instead)."""
        await contract_env.registry.get_or_create(
            VIDEO_ID, 50.0, contract_env.settings
        )
        await contract_env.registry.update_status(VIDEO_ID, "running")
        stale = await contract_env.registry.get(VIDEO_ID)
        stale.started_at -= (
            contract_env.settings.whisper_timeout_sec
            + contract_env.settings.job_ttl_sec
            + 1.0
        )

        await contract_env.registry.get_or_create(
            OTHER_VIDEO_ID, 50.0, contract_env.settings
        )  # pending — never GC'd (§4 table, last row)

        removed = await contract_env.registry.run_ttl_gc(contract_env.settings)
        assert removed == 1
        assert await contract_env.registry.get(VIDEO_ID) is None
        assert await contract_env.registry.get(OTHER_VIDEO_ID) is not None


# ---------------------------------------------------------------------------
# §3/§6 done-with-evicted-result — the poll that removes the handle
# ---------------------------------------------------------------------------


class TestEvictedResultContract:
    async def test_done_job_with_evicted_unit_polls_not_found_and_unregisters(
        self, contract_env, monkeypatch
    ) -> None:
        """§3/§4 way in #3 — a done job whose cache unit was evicted before
        any poll: the poll returns not_found AND removes the registry entry,
        so the next poll is a plain not_found and the re-kick starts fresh."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        await _start()
        await _drain_background_jobs()
        assert (await contract_env.registry.get(VIDEO_ID)).status == "done"

        await contract_env.cache.evict_lru(contract_env.cache.max_bytes)

        sc = await _poll()
        assert sc["status"] == "error"
        assert sc["error_code"] == errors.NOT_FOUND
        assert "evicted" in sc["message"]
        assert await contract_env.registry.get(VIDEO_ID) is None

        # The follow-up poll is the ordinary absent-video shape.
        again = await _poll()
        assert again["error_code"] == errors.NOT_FOUND
        assert contract_env.registry.size == 0


# ---------------------------------------------------------------------------
# §6 Restart contract — in-memory registry lost, cache volume survives
# ---------------------------------------------------------------------------


class TestRestartContract:
    async def test_restart_loses_registry_poll_not_found_rekick_cache_answers(
        self, contract_env, monkeypatch
    ) -> None:
        """§6 — a process restart empties the registry (in-memory) while the
        cache volume survives: the pre-restart job polls as not_found, and the
        documented re-kick is answered from cache without ever going pending."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        await _start()
        await _drain_background_jobs()
        assert (await _poll())["status"] == "ok"

        # Simulate the restart: fresh in-memory registry, same cache object
        # (same volume — the PVC case in §6's table).
        fresh_registry = WhisperJobRegistry()
        monkeypatch.setattr(server, "whisper_registry", fresh_registry)

        sc = await _poll()
        assert sc["status"] == "error"
        assert sc["error_code"] == errors.NOT_FOUND

        rekick = await _start()
        assert rekick["status"] == "ok"
        assert rekick["source"] == "whisper"
        assert rekick["text"] == "Never gonna give you up"
        assert fresh_registry.size == 0  # the re-kick never registered a job

    async def test_restart_without_surviving_cache_rekick_starts_fresh_job(
        self, contract_env, monkeypatch, tmp_path
    ) -> None:
        """§6 — the emptyDir case: neither the registry nor the cache survives.
        The re-kick then starts a *new* job (fresh pending + ETA) instead of
        dead-ending on not_found."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        await _start()
        await _drain_background_jobs()

        fresh_registry = WhisperJobRegistry()
        monkeypatch.setattr(server, "whisper_registry", fresh_registry)
        lost_cache_dir = tmp_path / "cache-after-restart"
        lost_cache_dir.mkdir()
        monkeypatch.setattr(
            server,
            "transcript_cache",
            TranscriptCache(cache_dir=lost_cache_dir, max_bytes=8 * 1024 * 1024),
        )

        rekick = await _start()
        assert rekick["status"] == "pending"
        assert fresh_registry.size == 1
        assert (await fresh_registry.get(VIDEO_ID)).status == "pending"


# ---------------------------------------------------------------------------
# §5 Stale scratch data — startup sweep + per-video sweep
# ---------------------------------------------------------------------------


class TestStaleScratchContract:
    def test_startup_sweep_deletes_every_file_reports_counts_idempotent(
        self, tmp_path
    ) -> None:
        """§5/§6 — the boot sweep deletes every file in the scratch dir
        unconditionally, returns (files, bytes), leaves directories alone, and
        is idempotent (a second sweep finds nothing)."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        keep_dir = scratch / "subdir"
        keep_dir.mkdir()
        (keep_dir / "nested.bin").write_bytes(b"x" * 10)
        (scratch / "a.m4a").write_bytes(b"a" * 100)
        (scratch / "b.part").write_bytes(b"b" * 32)

        deleted, freed = startup_sweep(str(scratch))
        assert (deleted, freed) == (2, 132)
        assert [p.name for p in scratch.iterdir()] == ["subdir"]
        assert (keep_dir / "nested.bin").is_file()

        assert startup_sweep(str(scratch)) == (0, 0)

    def test_per_video_sweep_is_isolated_to_its_own_glob(self, tmp_path) -> None:
        """§5 — the failure-path sweep deletes only ``{video_id}.*``; another
        video's files (or a job still in flight) are untouched. The canonical
        11-char id means no glob metacharacters, ever."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / f"{VIDEO_ID}.m4a").write_bytes(b"x")
        (scratch / f"{VIDEO_ID}.part").write_bytes(b"xy")
        (scratch / f"{OTHER_VIDEO_ID}.m4a").write_bytes(b"z")

        deleted = _sweep_video_scratch(VIDEO_ID, str(scratch))
        assert deleted == 2
        assert (scratch / f"{OTHER_VIDEO_ID}.m4a").is_file()
        assert not list(scratch.glob(f"{VIDEO_ID}.*"))

    async def test_failed_job_leaves_no_partial_scratch_files(
        self, contract_env, monkeypatch
    ) -> None:
        """§5 — the job's finally sweeps the video's scratch glob after every
        attempt, success or failure: a partial download from a timed-out or
        aborted attempt cannot accumulate into a disk-exhaustion vector."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch, _FakeASR())
        # A partial file the (failing) downloader left behind — audio_path was
        # never assigned, so only the glob sweep can remove it.
        partial = contract_env.scratch / f"{VIDEO_ID}.m4a"
        partial.write_bytes(AUDIO_BYTES)
        _install_download(
            monkeypatch, fail=YttError(errors.ASR_FAILED, "download aborted")
        )

        await _start()
        await _drain_background_jobs()

        assert (await contract_env.registry.get(VIDEO_ID)).status == "error"
        assert list(contract_env.scratch.iterdir()) == []
