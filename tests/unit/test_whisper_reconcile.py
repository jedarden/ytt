"""Whisper task-death reconciliation contract (docs/notes/whisper-lifecycle.md §6.1).

Pins the restart-reconciliation semantics for queued and running jobs: a
registry entry never outlives its driving asyncio task in a non-terminal
state. Before this contract a task killed by cancellation — server shutdown,
loop teardown, a semaphore-queue cancellation before the job body ever ran —
stranded its entry: a stranded ``running`` job polled a lying "in progress"
until the stale GC reaped it at ``whisper_timeout + job_ttl``, and a stranded
``pending`` job polled "queued" forever (pending has no TTL), stayed joinable
(Invariant 2), held a ``MAX_PENDING_WHISPER_JOBS`` slot permanently, and
dead-ended every documented re-kick until process restart.

The deterministic outcome pinned here (spec §6.1, driven by
``WhisperJobRegistry.note_task_done`` from ``run_whisper_job``'s
``CancelledError`` handler plus the server's task done-callback):

- non-terminal at task death → ``error`` with the stable ``asr_failed`` code
  and the fixed ``RECONCILED_MESSAGE``; the poller gets the same repeatable
  terminal shape any other failed job produces, never ``not_found``;
- terminal at task death → untouched (reconciliation never overwrites a real
  outcome);
- replaced/removed record → untouched (a dead task never clobbers a newer
  job);
- ownership preserved — the reconciled entry keeps its creator; a stranger
  still gets the byte-identical ``not_found``;
- the queue heals immediately (``active_count``), and the re-kick — the
  sanctioned "resume" — starts fresh, quota-charged work.

Both mechanisms that drive it are exercised: the coroutine-level
``CancelledError`` handler (cancellation *inside* the job body) through the
real tools, and the done-callback backstop (cancellation while queued on the
semaphore slot, where the body never runs) through the real bounded wrapper.

The harness is the same shape as ``test_whisper_contract.py`` — real tools,
real registry, real cache over a temp dir, real ``run_whisper_job`` with only
the socket-level edges stubbed.
"""

from __future__ import annotations

import asyncio
import threading
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from ytt import errors
from ytt import server
from ytt import whisper as ytt_whisper
from ytt.cache import TranscriptCache
from ytt.concurrency import ConcurrencyState
from ytt.errors import NoCaptionsError, YttError
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

RETRY_INSTRUCTION = (
    " Re-call get_youtube_transcript with the video URL to retry."
)


# ---------------------------------------------------------------------------
# Harness (same shape as test_whisper_contract.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """Direct ``mcp.call_tool`` runs with no HTTP request, so there is no real
    token to resolve (see the same fixture in test_whisper_contract.py). The
    ownership test simulates per-call subjects via ``_auth_as`` instead."""
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
    fail: YttError | None = None,
    gate: threading.Event | None = None,
) -> None:
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


def _install_asr(monkeypatch) -> None:
    real_run = ytt_whisper.run_whisper_job

    async def handle(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(200, json=VERBOSE_JSON)

    async def run_with_stub_socket(job, registry, settings, cache, active_model):
        transport = httpx.MockTransport(handle)
        async with httpx.AsyncClient(transport=transport) as client:
            await real_run(
                job, registry, settings, cache, active_model, http_client=client
            )

    monkeypatch.setattr(ytt_whisper, "run_whisper_job", run_with_stub_socket)


async def _start(monkeypatch=None, email: str | None = None, url: str = URL) -> dict:
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
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
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


async def _cancel_and_reap(task: asyncio.Task) -> None:
    """Cancel *task* and let its cancellation (and done-callbacks) settle."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# note_task_done — every lifecycle state, at the registry level
# ---------------------------------------------------------------------------


class TestNoteTaskDone:
    async def test_pending_job_reconciles_to_stable_error(self, env) -> None:
        """A queued job whose task died (never even started work) lands in the
        stable terminal error — not a forever-"queued" zombie: the stable
        code, the fixed relayable message, the owner kept, and the queue
        freed."""
        job, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )
        assert await env.registry.active_count() == 1

        env.registry.note_task_done(job)

        final = await env.registry.get(VIDEO_ID)
        assert final is not None and final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        assert final.message == RECONCILED_MESSAGE
        assert final.owner == ALICE  # ownership preserved
        assert await env.registry.active_count() == 0  # queue slot freed

    async def test_running_job_reconciles_to_stable_error(self, env) -> None:
        """Same outcome for a job that died mid-flight: the poller sees a
        terminal error immediately instead of a lying "in progress" until the
        stale-GC threshold."""
        job, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )
        await env.registry.update_status(VIDEO_ID, "running")

        env.registry.note_task_done(job)

        final = await env.registry.get(VIDEO_ID)
        assert final is not None and final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        assert final.message == RECONCILED_MESSAGE
        assert final.started_at is not None  # when the dead run began

    async def test_reconcile_is_idempotent(self, env) -> None:
        """The coroutine handler and the done-callback can both fire; the
        second is a no-op on the already-terminal record."""
        job, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )
        env.registry.note_task_done(job)
        first = await env.registry.get(VIDEO_ID)

        env.registry.note_task_done(job)

        assert await env.registry.get(VIDEO_ID) == first

    async def test_done_job_is_untouched(self, env) -> None:
        """A task that reached ``done`` before dying keeps its real outcome —
        reconciliation never overwrites a delivered result."""
        job, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )
        await env.registry.update_status(
            VIDEO_ID, "done", result_ref=f"{VIDEO_ID}.whisper"
        )

        env.registry.note_task_done(job)

        final = await env.registry.get(VIDEO_ID)
        assert final is not None and final.status == "done"
        assert final.result_ref == f"{VIDEO_ID}.whisper"
        assert final.error_code is None and final.message is None

    async def test_error_job_is_untouched(self, env) -> None:
        """A real failure recorded by the job body is never clobbered by the
        backstop — the caller's specific error survives."""
        job, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )
        await env.registry.update_status(
            VIDEO_ID,
            "error",
            error_code=errors.IP_BLOCKED,
            message="Audio download failed: sign in to confirm you're not a bot.",
        )

        env.registry.note_task_done(job)

        final = await env.registry.get(VIDEO_ID)
        assert final is not None and final.status == "error"
        assert final.error_code == errors.IP_BLOCKED
        assert final.message == (
            "Audio download failed: sign in to confirm you're not a bot."
        )

    async def test_replacement_job_is_not_clobbered(self, env) -> None:
        """A late/stray callback for a dead task must never touch the *new*
        job a re-kick put in its place (identity check, spec §6.1 rule 1)."""
        job1, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )
        env.registry.note_task_done(job1)
        job2, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=BOB
        )
        assert job2.status == "pending"

        env.registry.note_task_done(job1)  # the dead task's record

        final = await env.registry.get(VIDEO_ID)
        assert final is job2
        assert final.status == "pending"
        assert final.owner == BOB

    async def test_removed_entry_is_ignored(self, env) -> None:
        """A record removed after task death (evicted-result poll, TTL GC) is
        not resurrected by a late reconcile."""
        job, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )
        await env.registry.remove(VIDEO_ID)

        env.registry.note_task_done(job)

        assert await env.registry.get(VIDEO_ID) is None
        assert env.registry.size == 0

    async def test_done_task_argument_is_accepted(self, env) -> None:
        """The done-callback call shape — a finished task object — works the
        same, including for a task that failed rather than cancelled."""
        job, _ = await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )

        async def die():
            raise RuntimeError("wrapper-level death")

        task = asyncio.create_task(die())
        with pytest.raises(RuntimeError):
            await task
        env.registry.note_task_done(job, task)

        final = await env.registry.get(VIDEO_ID)
        assert final is not None and final.status == "error"
        assert final.message == RECONCILED_MESSAGE


# ---------------------------------------------------------------------------
# Through the real tools — cancellation mid-run (the coroutine handler)
# ---------------------------------------------------------------------------


class TestCancelledMidRunContract:
    async def test_polls_stable_repeatable_terminal_error(
        self, env, monkeypatch
    ) -> None:
        """§6.1 — a job cancelled mid-download: the owner polls the stable
        terminal error immediately (not "in progress", not not_found), the
        poll is repeatable, the scratch sweep still ran, and the entry stays
        pollable until TTL GC."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        gate = threading.Event()
        _install_download(monkeypatch, gate=gate)

        started = await _start()
        assert started["status"] == "pending"

        await asyncio.sleep(0.1)
        job = await env.registry.get(VIDEO_ID)
        assert job is not None and job.status == "running"
        (task,) = server._background_jobs

        await _cancel_and_reap(task)
        gate.set()  # let the zombie downloader thread exit

        polled = await _poll()
        assert polled["status"] == "error"
        assert polled["error_code"] == errors.ASR_FAILED
        assert polled["message"] == RECONCILED_MESSAGE + RETRY_INSTRUCTION
        assert polled["video_id"] == VIDEO_ID

        again = await _poll()
        assert again == polled  # repeatable — same shape every poll

        # The handle survives as a terminal record (registry entries only
        # leave via TTL GC / evicted-result polls) and holds no queue slot.
        assert env.registry.size == 1
        assert await env.registry.active_count() == 0

    async def test_owner_kept_stranger_still_not_found(
        self, env, monkeypatch
    ) -> None:
        """§6.1 — reconciliation preserves subject ownership: the creator
        polls the stable terminal error; every other subject gets the
        byte-identical not_found an unknown video_id gets."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        gate = threading.Event()
        _install_download(monkeypatch, gate=gate)

        started = await _start(monkeypatch, ALICE)
        assert started["status"] == "pending"

        await asyncio.sleep(0.1)
        (task,) = server._background_jobs
        await _cancel_and_reap(task)
        gate.set()

        owner_view = await _poll(monkeypatch, ALICE)
        assert owner_view["status"] == "error"
        assert owner_view["error_code"] == errors.ASR_FAILED

        stranger_view = await _poll(monkeypatch, BOB)
        assert stranger_view == _not_found(VIDEO_ID)

    async def test_reconciled_job_never_cached(self, env, monkeypatch) -> None:
        """§5 — a reconciled (interrupted) job produced no transcript: no
        cache unit exists, so the re-kick cannot be answered from cache and
        must start real work."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        gate = threading.Event()
        _install_download(monkeypatch, gate=gate)

        await _start()
        await asyncio.sleep(0.1)
        (task,) = server._background_jobs
        await _cancel_and_reap(task)
        gate.set()

        assert await env.cache.get(VIDEO_ID, "whisper") is None
        assert list(env.cache_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Through the real tools — cancellation while queued (the done-callback
# backstop; the job body never ran, so only the callback can catch it)
# ---------------------------------------------------------------------------


class TestCancelledWhileQueuedContract:
    async def test_polls_stable_terminal_error(self, env, monkeypatch) -> None:
        """§6.1 — a job cancelled while it is still waiting for the
        YTT_MAX_CONCURRENT_WHISPER slot: cancelled before the job body ever
        ran, so the done-callback is the only mechanism that can reconcile
        it. Without it the entry would poll "queued" forever (pending has no
        TTL) and dead-end every re-kick."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        _install_download(monkeypatch)
        # One-slot semaphore I hold myself: the started job must queue.
        concurrency = ConcurrencyState(max_concurrent_fetches=2, max_concurrent_whisper=1)
        monkeypatch.setattr(server, "_concurrency", concurrency)
        await concurrency.whisper_sem.acquire()

        started = await _start()
        assert started["status"] == "pending"
        await asyncio.sleep(0.1)
        job = await env.registry.get(VIDEO_ID)
        assert job is not None and job.status == "pending"  # queued, not running
        (task,) = server._background_jobs

        await _cancel_and_reap(task)
        concurrency.whisper_sem.release()  # unhold the slot for later tests

        polled = await _poll()
        assert polled["status"] == "error"
        assert polled["error_code"] == errors.ASR_FAILED
        assert polled["message"] == RECONCILED_MESSAGE + RETRY_INSTRUCTION
        assert await env.registry.active_count() == 0

    async def test_rekick_after_reconciliation_starts_fresh_work(
        self, env, monkeypatch
    ) -> None:
        """§6.1 — the re-kick after a reconciled job starts *new* work: the
        terminal entry is replaced (never joined, spec §1), the replacement
        is a fresh pending record, and the queue is healthy again — the
        ratcheted-shut-queue failure mode is gone."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        _install_download(monkeypatch)
        concurrency = ConcurrencyState(max_concurrent_fetches=2, max_concurrent_whisper=1)
        monkeypatch.setattr(server, "_concurrency", concurrency)
        await concurrency.whisper_sem.acquire()

        await _start()
        await asyncio.sleep(0.1)
        (task,) = server._background_jobs
        await _cancel_and_reap(task)

        corpse = await env.registry.get(VIDEO_ID)
        assert corpse is not None and corpse.status == "error"

        rekick = await _start()
        assert rekick["status"] == "pending"

        fresh = await env.registry.get(VIDEO_ID)
        assert fresh is not corpse  # replaced, not joined
        assert fresh.status == "pending"
        assert fresh.owner == "anonymous"  # owned by the re-kicking caller
        assert await env.registry.active_count() == 1  # the fresh job only

        concurrency.whisper_sem.release()
        await _drain_background_jobs()

    async def test_reconciliation_preserves_owner_and_hides_from_strangers(
        self, env, monkeypatch
    ) -> None:
        """§6.1 — the queued-then-cancelled case keeps its creator too: ALICE
        gets the stable terminal error, BOB gets the byte-identical
        not_found, and a BOB re-kick re-owns the replacement job."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        _install_download(monkeypatch)
        concurrency = ConcurrencyState(max_concurrent_fetches=2, max_concurrent_whisper=1)
        monkeypatch.setattr(server, "_concurrency", concurrency)
        await concurrency.whisper_sem.acquire()

        await _start(monkeypatch, ALICE)
        await asyncio.sleep(0.1)
        (task,) = server._background_jobs
        await _cancel_and_reap(task)
        concurrency.whisper_sem.release()

        reconciled = await env.registry.get(VIDEO_ID)
        assert reconciled is not None
        assert reconciled.owner == ALICE  # unchanged by reconciliation

        owner_view = await _poll(monkeypatch, ALICE)
        assert owner_view["status"] == "error"
        assert owner_view["error_code"] == errors.ASR_FAILED

        stranger_view = await _poll(monkeypatch, BOB)
        assert stranger_view == _not_found(VIDEO_ID)

        rekick = await _start(monkeypatch, BOB)
        assert rekick["status"] == "pending"
        replacement = await env.registry.get(VIDEO_ID)
        assert replacement is not None and replacement.owner == BOB

        await _drain_background_jobs()


# ---------------------------------------------------------------------------
# Terminal records are never overwritten — through the tool surface
# ---------------------------------------------------------------------------


class TestTerminalRecordsUntouched:
    async def test_successful_job_untouched_by_reconciliation(
        self, env, monkeypatch
    ) -> None:
        """§6.1 — a job that reached ``done`` keeps its result even when a
        reconcile fires afterwards (the backstop must be inert on real
        outcomes): the poll still delivers the transcript."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch)

        await _start()
        (task,) = server._background_jobs
        await _drain_background_jobs()
        assert (await env.registry.get(VIDEO_ID)).status == "done"
        job = await env.registry.get(VIDEO_ID)

        env.registry.note_task_done(job, task)  # a late callback for the task

        assert (await env.registry.get(VIDEO_ID)).status == "done"
        polled = await _poll()
        assert polled["status"] == "ok"
        assert polled["text"] == "Never gonna give you up"

    async def test_failed_job_untouched_by_reconciliation(
        self, env, monkeypatch
    ) -> None:
        """§6.1 — a job that failed on its own keeps its specific stable
        error: the backstop never replaces it with the generic interrupted
        message."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        _install_download(
            monkeypatch, fail=YttError(errors.IP_BLOCKED, "download aborted")
        )

        await _start()
        (task,) = server._background_jobs
        await _drain_background_jobs()
        job = await env.registry.get(VIDEO_ID)
        assert job.status == "error"
        assert job.error_code == errors.IP_BLOCKED

        env.registry.note_task_done(job, task)  # a late callback for the task

        final = await env.registry.get(VIDEO_ID)
        assert final.error_code == errors.IP_BLOCKED
        assert final.message == "download aborted"
