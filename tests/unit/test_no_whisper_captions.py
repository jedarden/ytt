"""No-Whisper caption success — the captioned-video half of the runbook's
§5 no-Whisper promise (bead ``ytt-bd5e9fa7``, the caption slice of the
caption-only regression coverage; the boot slice is
``test_no_whisper_boot.py``).

``deploy/ASR-RUNBOOK.md`` §5 ("Whisper unset (no-Whisper mode)") lists what
callers see under a deliberate no-Whisper deployment, and its third item is
the core caption-only promise this module pins end to end: *"Captioned
videos are unaffected, always."* The boot sibling already holds the
startup half ("startup is **not** blocked … health stays green") through
the same real boot path; here a captioned video is driven *through* that
booted server — the full shipping request path, real ASGI app and MCP
Streamable-HTTP transport included — and must come back ``status='ok'``
with the transcript served from captions.

Both no-Whisper env shapes get the proof, because they resolve
differently and must both stay caption-clean:

- **unset** resolves to the reference in-cluster ASR endpoint (the same
  ``DEFAULT_WHISPER_URL`` literal ``test_no_whisper_boot.py`` pins —
  imported from there so the two slices hold one expectation). The ASR
  endpoint is therefore *configured* during these requests, and an
  in-cluster DNS name no test process can resolve or reach — so a caption
  path that dialed it would fail loudly at the socket tripwire, not pass.
- **empty** (``YTT_WHISPER_URL=""``) is the runbook's deliberate
  no-Whisper spelling: a distinct resolution (never coerced to the
  default), under which the same captioned request must succeed
  identically.

The ASR path is never invoked — asserted at three independent layers, so
the claim survives a refactor that dodges any one of them:

1. **Job runner**: ``ytt.whisper.run_whisper_job`` — the only seam that
   executes ASR work — is replaced by a recorder that raises; a zero-length
   record after the request means no job body ever ran, hence no outbound
   ASR POST (the runner is what builds the ASR HTTP client).
2. **Job registry**: the server's real ``WhisperJobRegistry`` is swapped
   for a fresh one, and must still be empty after the request — no whisper
   job was created — and the public poll tool must answer ``not_found``
   for the video, the caller-visible form of "no job exists".
3. **Socket layer**: an autouse tripwire (modeled on
   ``test_egress_boundary.py``'s socket guard, kept module-local) refuses
   and records every ``connect``/``getaddrinfo`` — nothing under the
   stubbed seams can reach the network either, and a swallowed attempt
   still fails the teardown re-check.

Shared infrastructure — all reused, none re-derived: the harness trio of
autouse fixtures and ``open_established_session`` from
``tests/unit/_mcp_asgi_harness.py`` (the real ``build_asgi_app()`` app
under its own lifespan — the same boot ``serve()`` and
``test_no_whisper_boot.py`` drive), and the env-manipulation recipe from
that boot module (``monkeypatch`` the variable, then
``get_settings.cache_clear()`` so the next read reconstructs; the
harness's ``allowlisted_subject`` fixture clears the cache again on the
way out, after the env is restored). The caption fetch is stubbed at the
same seam the harness fail-hards (``ytt.fetch.fetch_transcript``,
late-imported by the tool at call time), returning a ``caption_auto``
result through the *real* fetch pool and single-flight path; fresh
limiter/quota/cache singletons keep the request deterministic (the
``test_server.py`` pattern). The video's caption unit landing in the real
``TranscriptCache`` afterwards is the storage-side proof the caption
pipeline — not ASR — produced the answer.
"""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from typing import Any

import pytest

import ytt.fetch
import ytt.whisper
from ytt import errors, server
from ytt.cache import TranscriptCache
from ytt.config import get_settings
from ytt.fetch import FetchResult
from ytt.models import Segment
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.whisper import WhisperJobRegistry

from tests.unit._mcp_asgi_harness import open_established_session
from tests.unit._mcp_asgi_harness import (  # noqa: F401
    allowlisted_subject,
    authorized_bearer,
    hermetic_egress,
)
from tests.unit.test_no_whisper_boot import DEFAULT_WHISPER_URL

#: The captioned video under test (any 11-character id — the fetch seam is
#: stubbed, so the id only has to canonicalize).
VIDEO_ID = "dQw4w9WgXcQ"

#: What the stubbed caption fetch returns — a plain auto-caption result, the
#: shape ``yt-dlp`` success produces and the runbook's "captioned videos"
#: means. Module-level and shared: ``FetchResult`` is read-only downstream.
CAPTION_RESULT = FetchResult(
    segments=[
        Segment(start=0.0, duration=2.0, text="Never gonna give you up"),
        Segment(start=2.0, duration=2.0, text="never gonna let you down"),
    ],
    source="caption_auto",
    served_lang="en",
    requested_lang=None,
    available_langs=["en"],
    title="Rick Astley - Never Gonna Give You Up",
)


@pytest.fixture(autouse=True)
def _no_outbound_egress(monkeypatch: pytest.MonkeyPatch):
    """Refuse and record every connection attempt at the socket layer.

    The caption path under test rides the in-process ASGI transport and a
    stubbed fetch — a real connection attempt can only mean the ASR path
    (or new egress code) started dialing, which is exactly what these tests
    exist to catch. Modeled on ``test_egress_boundary.py``'s
    ``_no_real_egress``: raising turns an attempt into an immediate
    host-naming failure, and the teardown re-assert keeps a broad
    ``except Exception`` in production code from swallowing one.
    """
    attempts: list[str] = []

    def refuse(syscall: str, address: object) -> None:
        host = (
            address[0]
            if isinstance(address, (tuple, list)) and address
            else address
        )
        desc = f"{syscall}({host!r})"
        attempts.append(desc)
        raise AssertionError(
            f"outbound connection attempted in a no-Whisper caption test: "
            f"{desc} — the caption path must answer from the stubbed "
            f"caption fetch alone; nothing may dial out, least of all the "
            f"ASR endpoint"
        )

    def refused_connect(sock: Any, address: object, *a: object, **k: object) -> None:
        refuse("socket.connect", address)

    def refused_connect_ex(sock: Any, address: object, *a: object, **k: object) -> None:
        refuse("socket.connect_ex", address)

    def refused_create_connection(address: object, *a: object, **k: object) -> None:
        refuse("socket.create_connection", address)

    def refused_getaddrinfo(host: object, port: object, *a: object, **k: object) -> None:
        refuse("socket.getaddrinfo", host)

    monkeypatch.setattr(socket.socket, "connect", refused_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refused_connect_ex)
    monkeypatch.setattr(socket, "create_connection", refused_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", refused_getaddrinfo)
    yield
    assert not attempts, (
        f"swallowed outbound connection attempt(s): {attempts} — production "
        f"code caught the tripwire's exception, but the attempt itself "
        f"breaks the caption-only promise under test"
    )


async def _captioned_video_world(monkeypatch, tmp_path) -> SimpleNamespace:
    """Fresh singletons + a recorded caption stub + a fail-hard ASR spy.

    Swaps the module-level registry, limits, and cache for per-test
    instances (the ``test_server.py`` determinism pattern) so the
    no-job-created claim is measured against a known-empty registry and a
    cache whose only possible entry is the one this request stores.
    """
    registry = WhisperJobRegistry()
    monkeypatch.setattr(server, "whisper_registry", registry)
    monkeypatch.setattr(
        server,
        "_rate_limiter",
        SubjectRateLimiter(capacity=20, refill_rate_per_sec=20.0 / 60.0),
    )
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(jobs_per_hour=10))

    cache = TranscriptCache(tmp_path / "cache", max_bytes=64 << 20, reconcile_sec=0)
    await cache.startup_scan()
    monkeypatch.setattr(server, "transcript_cache", cache)

    fetches: list[tuple[str, str | None]] = []

    async def captioned_fetch(video_id: str, lang: str | None, settings: Any):
        fetches.append((video_id, lang))
        return CAPTION_RESULT

    # The tool late-imports fetch_transcript at call time, so patching the
    # module attribute reroutes the real fetch pool onto the stub.
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", captioned_fetch)

    whisper_calls: list[tuple] = []

    async def no_asr_ever(*args: Any, **kwargs: Any) -> None:
        whisper_calls.append(args)
        raise AssertionError(
            "ASR path invoked for a captioned video: run_whisper_job called"
        )

    monkeypatch.setattr(ytt.whisper, "run_whisper_job", no_asr_ever)

    return SimpleNamespace(
        registry=registry,
        cache=cache,
        fetches=fetches,
        whisper_calls=whisper_calls,
    )


async def _call_tool(session, name: str, arguments: dict) -> dict:
    """One tools/call over the live session → the structured result.

    A tool-level answer is *data* (``structuredContent``); its absence is a
    protocol-level failure worth failing loudly on (the
    ``test_mcp_tool_contract.py`` reading).
    """
    message = await session.request(
        "tools/call", {"name": name, "arguments": arguments}
    )
    assert "result" in message, message
    sc = message["result"].get("structuredContent")
    assert sc is not None, (
        f"{name}: no structuredContent: {json.dumps(message['result'])[:300]}"
    )
    return sc


def _assert_served_from_captions(sc: dict) -> None:
    """The §5 sentence, caller-visible form: the transcript came back, and
    it came from captions (``source='caption_auto'``), final, no ASR ETA."""
    assert sc["status"] == "ok", sc
    assert sc["source"] == "caption_auto", sc
    assert sc["lang"] == "en", sc
    assert "Never gonna give you up" in sc["text"], sc
    assert sc["is_final"] is True, sc
    assert sc.get("title") == CAPTION_RESULT.title, sc


async def _assert_asr_untouched(world: SimpleNamespace) -> None:
    """No whisper job was created and no outbound ASR request was attempted.

    The runner record is empty (nothing ever reached ASR work, so no ASR
    HTTP client was ever built), the swapped-in real registry is still
    empty — by key as well as in aggregate — the caption fetch ran exactly
    once, and the unit the request stored is the caption unit: the
    storage-side provenance of the answer.
    """
    assert world.whisper_calls == []
    assert world.registry.size == 0
    assert await world.registry.get(VIDEO_ID) is None
    assert world.fetches == [(VIDEO_ID, None)]
    assert world.cache.unit_count == 1
    hit = await world.cache.get(VIDEO_ID, "en")
    assert hit is not None and hit.source == "caption_auto"


def _assert_poll_finds_no_job(poll: dict) -> None:
    """The caller-visible form of "no whisper job was created": the public
    poll tool answers ``not_found`` for the video."""
    assert poll["status"] == "error" and poll["error_code"] == errors.NOT_FOUND
    assert "Job not found" in poll["message"]


async def test_captioned_video_succeeds_with_whisper_url_unset(
    monkeypatch, tmp_path
) -> None:
    """No ``YTT_WHISPER_URL`` at all: the server resolves the reference ASR
    endpoint (the boot module's pinned literal) yet a captioned video still
    completes ``ok`` from captions — runbook §5 item 3."""
    monkeypatch.delenv("YTT_WHISPER_URL", raising=False)
    get_settings.cache_clear()

    resolved = get_settings()
    # The ASR endpoint is *configured* for these requests — the same
    # reference default the boot tests pin — and unreachable from a test
    # process, so any attempt to use it fails at the socket tripwire.
    assert resolved.whisper_url == DEFAULT_WHISPER_URL

    world = await _captioned_video_world(monkeypatch, tmp_path)

    async with open_established_session() as session:
        sc = await _call_tool(
            session,
            "get_youtube_transcript",
            {"url": f"https://youtu.be/{VIDEO_ID}"},
        )
        # The caller-visible form of "no job was created": the poll tool
        # finds nothing to poll.
        poll = await _call_tool(
            session, "get_transcript_job", {"video_id": VIDEO_ID}
        )

    _assert_served_from_captions(sc)
    await _assert_asr_untouched(world)
    _assert_poll_finds_no_job(poll)

    # The request neither rewrote nor re-resolved the endpoint: the tool
    # saw the same resolution asserted above (get_settings() is the same
    # lru_cache slot the tool read).
    assert get_settings().whisper_url == DEFAULT_WHISPER_URL


async def test_captioned_video_succeeds_with_whisper_url_empty(
    monkeypatch, tmp_path
) -> None:
    """``YTT_WHISPER_URL=""`` — the runbook's deliberate no-Whisper
    spelling: the same captioned request succeeds identically, under the
    empty-string resolution, with the ASR path equally untouched."""
    monkeypatch.setenv("YTT_WHISPER_URL", "")
    get_settings.cache_clear()

    resolved = get_settings()
    assert resolved.whisper_url == ""

    world = await _captioned_video_world(monkeypatch, tmp_path)

    async with open_established_session() as session:
        sc = await _call_tool(
            session,
            "get_youtube_transcript",
            {"url": f"https://youtu.be/{VIDEO_ID}"},
        )
        poll = await _call_tool(
            session, "get_transcript_job", {"video_id": VIDEO_ID}
        )

    _assert_served_from_captions(sc)
    await _assert_asr_untouched(world)
    _assert_poll_finds_no_job(poll)

    assert get_settings().whisper_url == ""
