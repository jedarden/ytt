"""No-Whisper captioned success, measured on the Whisper wire — the MCP
exchange slice (bead ``ytt-ef876bde``, slice 2 of the ``ytt-ab79a7e8``
caption-only chain; the startup wire slice is
``test_no_whisper_startup.py``).

``test_no_whisper_captions.py`` already pins the caller-visible half of the
runbook §5 caption promise ("captioned videos are unaffected, always")
through this same real-ASGI harness: the tool result is the caption
transcript, and no ASR work happened — proven there by a fail-hard stub on
the job runner (a seam *above* the wire), an empty job registry, and a
socket-level tripwire (a layer *below* the wire, refusing ``connect``).
What that belt deliberately never does is measure the wire itself: a
captioned exchange that dialed Whisper *around* the runner — the documented
``check_model_guard`` probe, or any new direct client — is caught there only
by the tripwire's refusal, never counted.

This module pins the same exchange with the counted-transport pattern
(``_CountingASRTransport``, imported from ``test_no_whisper_captionless.py``
— the same class slice 1 wires the guard through): the tree has exactly two
Whisper wire URLs (the guard's ``GET {whisper_url}/v1/models`` and the job's
``POST {whisper_url}/v1/audio/transcriptions``, both in ``ytt/whisper.py`` —
the startup slice's scope note), so both seams ride ONE counted transport
for the whole exchange — initialize, ``get_youtube_transcript``,
``get_transcript_job`` — and the transport must end it with an empty log:
zero requests handed to any whisper endpoint while an MCP client held a
live session.

Nothing is re-derived, everything imported: the harness trio of autouse
fixtures and ``open_established_session`` (the real ``build_asgi_app()``
under its own lifespan), the captioned world (fresh singletons + recorded
caption stub + caption assertions) from ``test_no_whisper_captions.py``, the
runner wrapper (``_install_asr`` — real ``run_whisper_job`` over the
transport) from ``test_no_whisper_captionless.py``, and the guard wrapper
(``_count_the_guard_seam``) from ``test_no_whisper_startup.py``. Two
refinements keep the wider wiring hermetic and honest:

- the world's fail-hard runner stub is *replaced* by the counted wrapper
  (last patch wins; monkeypatch unwinds the stack), so a hypothetical
  regression that created a job would run the real job body — and its
  audio download is refused fail-hard before anything else, so no real
  yt-dlp fetch can start. On the green path nothing fires at all, which is
  the point being pinned;
- the zero must not be vacuous: after the exchange, the same transport is
  proven live by driving one real ``check_model_guard`` probe through the
  wrapped seam — recorded, refused (the guard's documented fail-soft), with
  the exact URL artifact each shape produces (the scheme-less
  ``/v1/models`` under the empty spelling — no endpoint to hit — and the
  reference default's under unset).

Both env shapes are covered because they resolve differently and the
exchange must be wire-clean under each: **empty** (``YTT_WHISPER_URL=""``,
the runbook's deliberate no-Whisper spelling) and **unset** (resolves to
the reference in-cluster endpoint, imported from ``test_no_whisper_boot.py``
so all three slices hold one expectation).
"""

from __future__ import annotations

from types import SimpleNamespace

import ytt.whisper
from ytt.config import get_settings

from tests.unit._mcp_asgi_harness import open_established_session
from tests.unit._mcp_asgi_harness import (  # noqa: F401
    allowlisted_subject,
    authorized_bearer,
    hermetic_egress,
)
from tests.unit.test_no_whisper_boot import DEFAULT_WHISPER_URL
from tests.unit.test_no_whisper_captions import (
    VIDEO_ID,
    _assert_poll_finds_no_job,
    _assert_served_from_captions,
    _call_tool,
    _captioned_video_world,
)
from tests.unit.test_no_whisper_captionless import (
    _CountingASRTransport,
    _install_asr,
)
from tests.unit.test_no_whisper_startup import _count_the_guard_seam

#: The captioned video under test — the sibling module's fixture id, so
#: every slice of the chain exercises one video.
URL = f"https://youtu.be/{VIDEO_ID}"


def _refuse_audio_download(monkeypatch) -> None:
    """Fail hard if a Whisper job ever reaches for the video's audio.

    The counted-transport runner wrapper below runs the *real* job body —
    which starts with a yt-dlp download. On the captioned success path no
    job is ever created (the registry assertion below holds that), so this
    only ever fires on a regression, and then it must fail here rather than
    attempt a real download.
    """

    def _no_download(*args, **kwargs):
        raise AssertionError(
            "audio download attempted during a captioned-success exchange: "
            "a Whisper job was created for a video the caption fetch served"
        )

    monkeypatch.setattr(ytt.whisper, "_do_download_audio", _no_download)


async def _captioned_exchange_over_one_counted_wire(
    monkeypatch, tmp_path
) -> tuple[_CountingASRTransport, SimpleNamespace, dict, dict]:
    """The whole MCP exchange for the captioned video, on one counted wire.

    Both Whisper seams (the job runner and the model guard — the only two
    wire URLs in the tree) are wrapped over the same ``_CountingASRTransport``
    before the session opens, and the exchange is the full client-visible
    conversation: session establishment, the transcript call, and the poll
    for a job that must not exist.
    """
    transport = _CountingASRTransport([])
    world = await _captioned_video_world(monkeypatch, tmp_path)
    _refuse_audio_download(monkeypatch)
    # Last patch wins: the world's fail-hard runner stub gives way to the
    # real job body over the counted transport (the captionless module's
    # wiring, reused verbatim).
    _install_asr(monkeypatch, transport)
    _count_the_guard_seam(monkeypatch, transport)

    async with open_established_session() as session:
        sc = await _call_tool(session, "get_youtube_transcript", {"url": URL})
        poll = await _call_tool(session, "get_transcript_job", {"video_id": VIDEO_ID})

    return transport, world, sc, poll


async def test_captioned_exchange_hands_whisper_nothing_when_url_empty(
    monkeypatch, tmp_path
) -> None:
    """``YTT_WHISPER_URL=""`` — the deliberate no-Whisper spelling: the
    captioned exchange answers the MCP client with the caption transcript,
    creates no job, and hands the counted transport nothing — no ``/v1/
    models`` probe, no transcription POST — across the whole session."""
    monkeypatch.setenv("YTT_WHISPER_URL", "")
    get_settings.cache_clear()
    resolved = get_settings()
    assert resolved.whisper_url == ""

    transport, world, sc, poll = await _captioned_exchange_over_one_counted_wire(
        monkeypatch, tmp_path
    )

    # The tool result IS the caption transcript — not an error, not a
    # synthesized fallback (the sibling's caller-visible assertions).
    _assert_served_from_captions(sc)
    # And the caller-visible form of "no job was created": nothing to poll.
    _assert_poll_finds_no_job(poll)
    assert world.fetches == [(VIDEO_ID, None)]
    assert world.registry.size == 0

    # The wire: zero requests handed to any whisper endpoint during the
    # whole exchange.
    assert transport.requested == []

    # Non-vacuity: the counter was watching. One real guard probe through
    # the wrapped seam is recorded and refused (fail-soft), and the URL it
    # was handed is the scheme-less artifact of the empty resolution — even
    # a regression could not have reached an endpoint this way.
    active = await ytt.whisper.check_model_guard(
        resolved.whisper_url, resolved.whisper_model
    )
    assert active == resolved.whisper_model
    assert transport.requested == ["/v1/models"]


async def test_captioned_exchange_hands_whisper_nothing_when_url_unset(
    monkeypatch, tmp_path
) -> None:
    """No ``YTT_WHISPER_URL`` at all: the exchange resolves the reference
    in-cluster endpoint (unreachable from a test process) and must still
    hand the counted wire nothing — the same clean exchange under the other
    resolution."""
    monkeypatch.delenv("YTT_WHISPER_URL", raising=False)
    get_settings.cache_clear()
    resolved = get_settings()
    assert resolved.whisper_url == DEFAULT_WHISPER_URL

    transport, world, sc, poll = await _captioned_exchange_over_one_counted_wire(
        monkeypatch, tmp_path
    )

    _assert_served_from_captions(sc)
    _assert_poll_finds_no_job(poll)
    assert world.fetches == [(VIDEO_ID, None)]
    assert world.registry.size == 0
    assert transport.requested == []

    # Non-vacuity, under the configured resolution: the probe is handed the
    # reference default's models URL — the same literal slice 1's boot probe
    # is refused with.
    active = await ytt.whisper.check_model_guard(
        resolved.whisper_url, resolved.whisper_model
    )
    assert active == resolved.whisper_model
    assert transport.requested == [f"{DEFAULT_WHISPER_URL}/v1/models"]
