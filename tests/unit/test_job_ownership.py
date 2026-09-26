"""Whisper job ownership — the poll handle is bound to its creator's subject.

Pins the ownership rule documented in ``docs/notes/auth.md`` (§Job ownership)
and ``docs/notes/whisper-lifecycle.md`` (§3/§4): every registry-created ASR
job records the authenticated subject of the call that started it, and
``get_transcript_job`` answers a poll from any *other* subject with the
byte-identical ``not_found`` an unknown video_id gets — so job handles are not
enumerable across subjects. The property is checked across the whole job
lifetime the task names: pending, running, completed, failed, and restarted
(the re-kick re-owns the replacement job to the subject that re-kicked it),
plus the join path (shared work, private handle) and the two deliberate
affordances (normalized subject keys, hand-built owner-less records).

The harness is the same shape as ``test_whisper_contract.py`` — real tools,
real registry, real cache over a temp dir, real ``run_whisper_job`` with only
the socket-level edges stubbed — with the per-call subject simulated the way
``test_subject_limits_e2e.py`` does it (patching
``fastmcp.server.dependencies.get_access_token``, the exact namespace
``ytt.server._request_subject`` reads).
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
from ytt.whisper import WhisperJobRegistry

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


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """Ownership is enforced *inside* the tool body — these tests drive it
    with valid-but-simulated tokens, so the allowlist middleware itself is
    bypassed (both subjects below are allowlisted in any real deployment)."""
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


def _auth_as(monkeypatch, email: str | None) -> None:
    """Resolve ``_request_subject()`` to *email* for the calls that follow.

    Same seam as test_subject_limits_e2e._auth_as: the production subject
    resolver imports ``get_access_token`` at call time from this namespace.
    """
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
    """Real ``run_whisper_job`` with the ASR socket on a deterministic fake."""
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


async def _start(monkeypatch, email: str) -> dict:
    _auth_as(monkeypatch, email)
    result = await mcp.call_tool("get_youtube_transcript", {"url": URL})
    return result.structured_content


async def _poll(monkeypatch, email: str, video_id: str = VIDEO_ID) -> dict:
    _auth_as(monkeypatch, email)
    result = await mcp.call_tool("get_transcript_job", {"video_id": video_id})
    return result.structured_content


async def _drain_background_jobs() -> None:
    tasks = [t for t in list(server._background_jobs) if not t.done()]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
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


# ---------------------------------------------------------------------------
# Pending / running — the queued and in-flight handle is owner-only
# ---------------------------------------------------------------------------


class TestPendingAndRunning:
    async def test_pending_job_polls_for_owner_only(self, env, monkeypatch) -> None:
        """A pending job answers its owner with the queued shape and every
        other subject with not_found — whose payload is byte-identical to an
        unknown video_id's."""
        _auth_as(monkeypatch, ALICE)
        await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner=ALICE
        )

        owner_view = await _poll(monkeypatch, ALICE)
        assert owner_view["status"] == "pending"
        assert owner_view["eta_sec"] == pytest.approx(
            50.0 * env.settings.whisper_realtime_factor
        )

        stranger_view = await _poll(monkeypatch, BOB)
        assert stranger_view == _not_found(VIDEO_ID)

    async def test_running_job_is_invisible_to_other_subjects(
        self, env, monkeypatch
    ) -> None:
        """The running state (the FSM state with the live Whisper slot) is
        likewise owner-only."""
        _auth_as(monkeypatch, ALICE)
        await env.registry.get_or_create(VIDEO_ID, 50.0, env.settings, owner=ALICE)
        await env.registry.update_status(VIDEO_ID, "running")

        owner_view = await _poll(monkeypatch, ALICE)
        assert owner_view["status"] == "running"

        stranger_view = await _poll(monkeypatch, BOB)
        assert stranger_view == _not_found(VIDEO_ID)

    async def test_normalized_subject_keys_match_across_calls(
        self, env, monkeypatch
    ) -> None:
        """Ownership binds the *normalized* subject key (lowercased email, the
        ``_request_subject`` rule), not the raw claim: a start under a
        mixed-case token email polls under its lowercase form."""
        _auth_as(monkeypatch, "Alice@Example.COM")
        await env.registry.get_or_create(
            VIDEO_ID, 50.0, env.settings, owner="alice@example.com"
        )

        owner_view = await _poll(monkeypatch, "ALICE@EXAMPLE.COM")
        assert owner_view["status"] == "pending"

        # ...and a different subject is still not_found under the same rule.
        assert await _poll(monkeypatch, "alicex@example.com") == _not_found(VIDEO_ID)

    async def test_ownerless_record_is_polled_as_unrestricted(
        self, env, monkeypatch
    ) -> None:
        """Hand-built records without an owner (the documented unit-test
        scaffolding affordance — no production path creates one) poll as
        unrestricted. Pinned so the exemption can't silently flip into a
        denial that strands existing scaffolding."""
        from ytt.models import WhisperJob

        job = WhisperJob(video_id=VIDEO_ID, status="pending", created_at=time.time())
        env.registry._jobs[VIDEO_ID] = job

        for viewer in (None, ALICE, BOB):
            view = await _poll(monkeypatch, viewer)
            assert view["status"] == "pending"


# ---------------------------------------------------------------------------
# Completed — the transcript is delivered to its owner only
# ---------------------------------------------------------------------------


class TestCompleted:
    async def test_done_job_delivers_transcript_to_owner_only(
        self, env, monkeypatch
    ) -> None:
        """Full path as Alice: start → run to done → her poll returns the
        transcript. Bob's poll of the same id is not_found — even though the
        finished unit sits in the shared cache, the *handle* never exposes it
        to another subject."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch)

        started = await _start(monkeypatch, ALICE)
        assert started["status"] == "pending"
        assert (await env.registry.get(VIDEO_ID)).owner == ALICE

        await _drain_background_jobs()
        assert (await env.registry.get(VIDEO_ID)).status == "done"

        owner_view = await _poll(monkeypatch, ALICE)
        assert owner_view["status"] == "ok"
        assert owner_view["source"] == "whisper"
        assert owner_view["text"] == "Never gonna give you up"

        stranger_view = await _poll(monkeypatch, BOB)
        assert stranger_view == _not_found(VIDEO_ID)
        assert "text" not in stranger_view

        # The owner's delivery stays repeatable after the stranger's denial.
        assert (await _poll(monkeypatch, ALICE))["text"] == owner_view["text"]


# ---------------------------------------------------------------------------
# Failed — the stable error is owner-only too
# ---------------------------------------------------------------------------


class TestFailed:
    async def test_failed_job_error_shape_is_owner_only(self, env, monkeypatch) -> None:
        """A failed job relays its stable code + message to its owner; any
        other subject gets not_found — failures are not observable across
        subjects either."""
        _install_no_captions(monkeypatch)
        _install_download(
            monkeypatch, fail=YttError(errors.IP_BLOCKED, "egress blocked")
        )
        _install_asr(monkeypatch)

        started = await _start(monkeypatch, ALICE)
        assert started["status"] == "pending"
        await _drain_background_jobs()
        assert (await env.registry.get(VIDEO_ID)).status == "error"

        owner_view = await _poll(monkeypatch, ALICE)
        assert owner_view["status"] == "error"
        assert owner_view["error_code"] == errors.IP_BLOCKED
        assert "egress blocked" in owner_view["message"]
        assert "Re-call get_youtube_transcript" in owner_view["message"]

        stranger_view = await _poll(monkeypatch, BOB)
        assert stranger_view == _not_found(VIDEO_ID)


# ---------------------------------------------------------------------------
# Restarted — the re-kick re-owns the replacement job
# ---------------------------------------------------------------------------


class TestRestarted:
    async def test_rekick_after_failure_transfers_the_handle_to_the_rekicker(
        self, env, monkeypatch
    ) -> None:
        """Alice's job fails; Bob's documented recovery (re-call
        get_youtube_transcript) replaces the terminal entry with a fresh job
        *he* owns: his poll works, Alice's handle is gone."""
        _install_no_captions(monkeypatch)
        _install_download(
            monkeypatch, fail=YttError(errors.IP_BLOCKED, "egress blocked")
        )
        _install_asr(monkeypatch)

        await _start(monkeypatch, ALICE)
        await _drain_background_jobs()
        assert (await env.registry.get(VIDEO_ID)).owner == ALICE

        # Bob re-kicks; the replacement is his.
        _install_download(monkeypatch)
        rekicked = await _start(monkeypatch, BOB)
        assert rekicked["status"] == "pending"

        replacement = await env.registry.get(VIDEO_ID)
        assert replacement.status == "pending"
        assert replacement.owner == BOB

        assert (await _poll(monkeypatch, BOB))["status"] in ("pending", "running")
        assert await _poll(monkeypatch, ALICE) == _not_found(VIDEO_ID)

        await _drain_background_jobs()
        done_view = await _poll(monkeypatch, BOB)
        assert done_view["status"] == "ok"
        assert done_view["text"] == "Never gonna give you up"

    async def test_joining_another_subjects_job_shares_work_not_the_handle(
        self, env, monkeypatch
    ) -> None:
        """A second subject requesting the same caption-less video joins the
        in-flight job (Invariant 2 — no duplicate Whisper run) but gains no
        poll handle: her poll is not_found while the owner's keeps advancing.
        Her recovery is the cache-first re-call once the job lands."""
        _install_no_captions(monkeypatch)
        _install_asr(monkeypatch)
        gate = threading.Event()
        _install_download(monkeypatch, gate=gate)

        started = await _start(monkeypatch, ALICE)
        assert started["status"] == "pending"

        await asyncio.sleep(0.1)  # let the task reach the (blocked) download
        assert (await env.registry.get(VIDEO_ID)).status == "running"

        joined = await _start(monkeypatch, BOB)
        assert joined["status"] == "pending"  # the join itself is allowed…
        assert env.registry.size == 1  # …and no second job exists

        assert await _poll(monkeypatch, BOB) == _not_found(VIDEO_ID)
        assert (await _poll(monkeypatch, ALICE))["status"] == "running"

        gate.set()
        await _drain_background_jobs()

        # The owner polls the delivered transcript; the joiner recovers via
        # the cache-first re-call (never a job), as documented.
        assert (await _poll(monkeypatch, ALICE))["status"] == "ok"

        from ytt.cache import CacheHit

        assert isinstance(
            await env.cache.get(VIDEO_ID, "whisper"), CacheHit
        )  # the shared unit Bob's re-call will answer from


# ---------------------------------------------------------------------------
# Non-leaking denial — one payload for every "not yours or not there"
# ---------------------------------------------------------------------------


class TestNonLeakingDenial:
    async def test_cross_subject_and_unknown_polls_are_indistinguishable(
        self, env, monkeypatch
    ) -> None:
        """The three denials a caller can provoke — unknown id, another
        subject's pending job, another subject's finished job — are one
        byte-identical payload. Nothing in any response distinguishes "the
        job exists, someone else owns it" from "no such job"."""
        _install_no_captions(monkeypatch)
        _install_download(monkeypatch)
        _install_asr(monkeypatch)

        unknown = await _poll(monkeypatch, BOB)

        await _start(monkeypatch, ALICE)  # pending
        on_pending = await _poll(monkeypatch, BOB)

        await _drain_background_jobs()  # → done
        on_done = await _poll(monkeypatch, BOB)

        expected = _not_found(VIDEO_ID)
        assert unknown == on_pending == on_done == expected
