"""Non-YouTube input security contract (docs/notes/input-security.md).

``get_youtube_transcript(url, …)`` takes a model-authored free-form string.
This module pins the *security* half of the canonicalizer — the part that
keeps untrusted input away from the network:

- **Reject before network** — every malformed / non-YouTube / redirecting /
  off-shape input dies in ``canonicalize`` with ``error_code: bad_url``
  before the cache, the rate limiter, or any socket use. Enforced with a
  socket-level tripwire plus fetch/Whisper boobytraps: a single connection
  attempt — even one a broad ``except`` swallowed — fails the test at
  teardown, and a fetch seam call fails it immediately.
- **Only the canonical URL reaches yt-dlp** — the caller's string is
  discarded, not sanitized. Driven through the real ``fetch_transcript``
  with a recording ``YoutubeDL`` stand-in (the URL ``extract_info`` receives
  must be the literal template over the validated id), plus a static leg
  asserting all three YouTube-bound call sites share that template.
- **Accepted input forwards only the id** — messy-but-accepted forms drive
  the full tool path and the fetch boundary observes the bare id, nothing
  else (userinfo, ports, tracking params do not survive).
- **Arbitrary video ids** — any 11-char ``[A-Za-z0-9_-]`` string is accepted
  verbatim with no existence probe (Hypothesis over the full alphabet);
  anything off that shape — wrong length, non-alphabet characters — is
  ``bad_url``.
- **get_transcript_job's id is an opaque key** — a fabricated id (or any
  hostile string) misses the registry to ``not_found`` and is never embedded
  in a URL.

All tests are offline: no network, no DNS, no extraction.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import ytt.fetch
import ytt.whisper
from ytt.canonicalize import canonicalize
from ytt.config import Settings
from ytt.errors import BAD_URL, UNAVAILABLE, YttError
from ytt.fetch import fetch_transcript
from ytt.ratelimit import SubjectRateLimiter
from ytt.server import mcp

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Dedicated id for the accepted-forms leg — deliberately NOT an id any other
#: test module may have put in the transcript cache (test ordering must not
#: turn a cache hit into a skipped fetch boundary).
VID = "inSecVid001"

#: The one URL shape yt-dlp may ever see (docs §The output invariant).
CANONICAL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"

_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
_ID_ONLY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Rejection table — (input, expected reason fragment), grouped per the doc
# ---------------------------------------------------------------------------

REJECTED_INPUTS: list[tuple[str, str]] = [
    # -- §1 malformed --------------------------------------------------------
    ("", "empty"),
    ("   ", "empty"),
    ("https://", "not a youtube host"),  # scheme, no host
    ("https:/youtube.com/watch?v=" + VID, "not a youtube host"),  # single slash
    ("https:\\\\evil.com\\watch?v=" + VID, "not a youtube host"),  # backslashes
    (VID + "\r\nHost: evil.com", "not a youtube host"),  # header-injection shape
    (
        f"https://youtu.be/{VID}\r\nGET / HTTP/1.1",
        "is not 11 valid chars",  # CRLF glued to the id slot
    ),
    # stdlib parser refuses these shapes outright (urlsplit ValueError) — the
    # gate must translate that to bad_url, not leak a raw ValueError.
    ("00000[000000", "malformed URL structure"),  # unbalanced '[' → Invalid IPv6
    ("https://youtube.com]/watch?v=" + VID, "malformed URL structure"),
    (
        "https://youtube.com／watch?v=" + VID,
        "malformed URL structure",  # fullwidth solidus NFKC-normalizes to '/'
    ),
    # …while these bracket/NFKC neighbors parse fine and reject on merits.
    ("https://www.youtube.com/watch?v=]", "is not 11 valid chars"),
    (f"https://youtube.com］/watch?v={VID}", "not a youtube host"),
    # -- §2 non-YouTube hosts: lookalikes / supersets ------------------------
    (f"https://example.com/watch?v={VID}", "not a youtube host"),
    (f"https://evilyoutube.com/watch?v={VID}", "not a youtube host"),
    (f"https://notyoutube.com/watch?v={VID}", "not a youtube host"),
    (f"https://youtube.com.evil.com/watch?v={VID}", "not a youtube host"),
    (f"https://youtu.be.evil.com/{VID}", "not a youtube host"),
    (f"https://m.youtube.com.com/watch?v={VID}", "not a youtube host"),
    (f"https://youtube-nocookie.com.evil.com/embed/{VID}", "not a youtube host"),
    (f"https://youtube.com./watch?v={VID}", "not a youtube host"),  # trailing dot
    (f"https://xn--utube-9re.com/watch?v={VID}", "not a youtube host"),  # punycode
    # -- §2 non-YouTube hosts: IP literals / internal targets (SSRF set) -----
    (f"https://127.0.0.1/watch?v={VID}", "not a youtube host"),
    (f"https://[::1]/watch?v={VID}", "not a youtube host"),
    ("http://169.254.169.254/latest/meta-data", "not a youtube host"),
    (f"https://localhost/watch?v={VID}", "not a youtube host"),
    # -- §2 userinfo smuggling resolves in the safe direction ----------------
    (f"https://youtube.com@evil.com/watch?v={VID}", "not a youtube host"),
    (f"https://youtube.com:8443@evil.com/watch?v={VID}", "not a youtube host"),
    # -- §3 redirecting URLs are never resolved ------------------------------
    ("https://bit.ly/3xYzAbC", "not a youtube host"),
    (f"https://t.co/{VID}", "not a youtube host"),
    (f"https://goo.gl/{VID}", "not a youtube host"),
    (
        "https://google.com/url?q=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D" + VID,
        "not a youtube host",  # redirector host, even pointing at a real video
    ),
    (
        "https://www.youtube.com/attribution_link?u="
        "https%3A%2F%2Fevil.com%2Fwatch%3Fv%3D" + VID,
        "unrecognized youtube.com path",  # u= target never parsed or followed
    ),
    (
        "https://www.youtube.com/redirect?q="
        "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D" + VID,
        "unrecognized youtube.com path",  # even a canonical-looking target
    ),
    # -- §3 non-video youtube.com paths (channel/handle/search/playlist) -----
    ("https://www.youtube.com/channel/UCabcdefghijklmnop", "channel/playlist/search/handle"),
    ("https://www.youtube.com/@SomeHandle", "channel/playlist/search/handle"),
    (f"https://www.youtube.com/results?search_query={VID}", "channel/playlist/search/handle"),
    ("https://www.youtube.com/playlist?list=PLabc", "channel/playlist/search/handle"),
    (
        "https://user:pass@youtube.com/playlist?list=PLabc",
        "channel/playlist/search/handle",  # credentials do not rehabilitate a path
    ),
    # -- §4 arbitrary video ids: off-shape -----------------------------------
    ("dQw4w9WgXc", "not a youtube host"),  # 10 chars → parsed as a host, rejected
    ("dQw4w9WgXcQd", "not a youtube host"),  # 12 chars
    ("https://youtu.be/", "is not 11 valid chars"),  # empty id slot
    ("https://www.youtube.com/watch?v=tooSHORT", "is not 11 valid chars"),
    (f"https://youtu.be/dQw4 w9WgXcQ", "is not 11 valid chars"),  # space in id slot
    ("dQw4w9WgXcΩ", "not a youtube host"),  # unicode lookalike, 11 glyphs
    ("ｄＱｗ４ｗ９ＷｇＸｃＱ", "not a youtube host"),  # fullwidth, 11 glyphs
    ("../../etc/passwd", "not a youtube host"),
    ("dQw4w9W?XcQ", "not a youtube host"),  # URL metacharacters
    ("dQw4w9W/XcQ", "not a youtube host"),
    ("dQw4w9W&XcQ=v", "not a youtube host"),
]


#: Messy-but-accepted forms that must all forward exactly ``VID`` downstream.
ACCEPTED_FORMS: list[str] = [
    VID,  # bare id
    f"https://www.youtube.com/watch?v={VID}&list=PLabc&index=3&t=42s",
    f"https://youtu.be/{VID}?si=abc123",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/live/{VID}",
    f"https://www.youtube.com/embed/{VID}?autoplay=1",
    f"https://www.youtube-nocookie.com/embed/{VID}",
    f"https://music.youtube.com/watch?v={VID}",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://user:pass@youtube.com/watch?v={VID}",  # credentials ignored
    f"https://youtube.com:8443/watch?v={VID}",  # port ignored
    f"HTTPS://YOUTUBE.COM/watch?v={VID}",  # host case-insensitive
    f"  https://youtu.be/{VID}  ",  # outer whitespace stripped
    f"youtu.be/{VID}",  # scheme-less
]


# ---------------------------------------------------------------------------
# Fixtures — tripwire, authorization bypass, seams
# ---------------------------------------------------------------------------


class EgressViolation(AssertionError):
    """A real connection was attempted inside the input-security guard."""


class _SocketTripwire:
    """Records and refuses every connection attempt at the socket layer.

    Same shape as the egress-boundary guard's tripwire: raise immediately so
    an unmocked dial fails the test, and record too so a broad ``except`` in
    production code cannot swallow the violation (the fixture teardown
    re-asserts an empty record).
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def refuse(self, syscall: str, address: object) -> None:
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        desc = f"{syscall}({host!r})"
        self.attempts.append(desc)
        raise EgressViolation(
            f"input-security tripwire fired: {desc}. Input rejected by "
            "canonicalize must cost zero packets (docs/notes/"
            "input-security.md §The gate's position in the request path)."
        )


@pytest.fixture(autouse=True)
def _no_real_egress(monkeypatch: pytest.MonkeyPatch):
    """Arm the socket tripwire for every test in this module."""
    tripwire = _SocketTripwire()

    def refused_connect(sock, address, *a: object, **k: object):
        tripwire.refuse("socket.connect", address)

    def refused_connect_ex(sock, address, *a: object, **k: object):
        tripwire.refuse("socket.connect_ex", address)

    def refused_create_connection(address, *a: object, **k: object):
        tripwire.refuse("socket.create_connection", address)

    def refused_getaddrinfo(host, port, *a: object, **k: object):
        tripwire.refuse("socket.getaddrinfo", host)

    monkeypatch.setattr(socket.socket, "connect", refused_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refused_connect_ex)
    monkeypatch.setattr(socket, "create_connection", refused_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", refused_getaddrinfo)
    yield tripwire
    assert not tripwire.attempts, (
        f"swallowed connection attempt(s) recorded: {tripwire.attempts} — "
        "production code caught the tripwire's exception, but the attempt "
        "itself breaks the reject-before-network contract."
    )


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """Direct ``mcp.call_tool()`` runs with no HTTP request, so there is no
    token to resolve — bypass the AuthMiddleware gate (test_server pattern)."""
    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    """Roomy deterministic limiter — the suite shares the module-level one."""
    monkeypatch.setattr(
        "ytt.server._rate_limiter",
        SubjectRateLimiter(capacity=500, refill_rate_per_sec=500.0 / 60.0),
    )


@pytest.fixture(autouse=True)
def _boobytrap_seams(monkeypatch):
    """The fetch/Whisper seams must never be reached by a rejected input.

    Tests that need the pipeline to *reach* the fetch boundary install their
    own recording stub on the same seams; reaching the real ones here is a
    contract violation, not a network call.
    """

    async def _no_fetch(*args: Any, **kwargs: Any):
        raise AssertionError(
            "real caption fetch attempted — input-security tests must stub "
            "the seam or reject the input before it"
        )

    async def _no_whisper(*args: Any, **kwargs: Any):
        raise AssertionError("real Whisper job attempted — no network in unit tests")

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _no_fetch)
    monkeypatch.setattr(ytt.whisper, "run_whisper_job", _no_whisper)


# ---------------------------------------------------------------------------
# Leg 1 — the rejection table, offline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("url", "reason"), REJECTED_INPUTS, ids=lambda v: repr(v)[:60])
def test_rejected_inputs_raise_bad_url_with_reason(url, reason):
    """Every documented rejection class fails with stable ``bad_url``."""
    with pytest.raises(YttError) as ei:
        canonicalize(url)
    assert ei.value.error_code == BAD_URL
    assert reason in ei.value.message


@pytest.mark.parametrize(("url", "reason"), REJECTED_INPUTS, ids=lambda v: repr(v)[:60])
def test_rejected_input_reasons_are_stable_on_repeat(url, reason):
    """Rejection is a pure function of the string — no state, no retry path."""
    with pytest.raises(YttError) as first:
        canonicalize(url)
    with pytest.raises(YttError) as second:
        canonicalize(url)
    assert first.value.message == second.value.message
    assert first.value.error_code == second.value.error_code


# ---------------------------------------------------------------------------
# Leg 2 — reject before network, end to end through the tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "reason"), REJECTED_INPUTS, ids=lambda v: repr(v)[:60])
async def test_tool_rejects_input_before_any_seam(url, reason):
    """Drive ``get_youtube_transcript`` with each hostile input.

    The socket tripwire and the fetch/Whisper boobytraps are armed: the only
    way this test passes is if the tool returns the ``bad_url`` error without
    touching a single seam — cache and rate limiter included (the limiter is
    charged only on the cache-miss fetch path, after canonicalize).
    """
    result = await mcp.call_tool("get_youtube_transcript", {"url": url})
    sc = result.structured_content
    assert sc is not None
    assert sc["status"] == "error"
    assert sc["error_code"] == BAD_URL
    assert reason in sc["message"]
    # No downstream key is minted from unvalidated input.
    assert sc["video_id"] == ""


@pytest.mark.asyncio
async def test_rejection_costs_no_rate_limit_token():
    """A rejected input never reaches the limiter's charge.

    A bucket for the (no-HTTP-context) ``anonymous`` subject is created only
    by ``consume`` — on the cache-miss fetch path, after canonicalize. If a
    rejection ever charged quota, the bucket would exist here.
    """
    from ytt import server

    limiter = server._rate_limiter
    assert "anonymous" not in limiter._buckets
    for url, _ in REJECTED_INPUTS[:5]:
        result = await mcp.call_tool("get_youtube_transcript", {"url": url})
        assert result.structured_content["error_code"] == BAD_URL
    assert "anonymous" not in limiter._buckets, (
        "a canonicalize rejection charged the rate limiter — rejection must "
        "precede the cache-miss charge (docs/notes/input-security.md)"
    )


# ---------------------------------------------------------------------------
# Leg 3 — accepted input forwards only the canonical id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ACCEPTED_FORMS, ids=lambda v: v[:48])
async def test_accepted_form_forwards_only_the_id(monkeypatch, form):
    """Every accepted form drives the fetch boundary with the bare id only.

    The stub records what the fetch path received: the canonical id, never
    the caller's URL — no userinfo, no port, no tracking parameters survive.
    """
    seen: list[str] = []

    async def _recording_fetch(video_id, lang, settings, **kwargs):
        seen.append(video_id)
        # UNAVAILABLE (not empty_body — that code triggers the Whisper ASR
        # fallback and would start a job) lands in the plain fetch-error
        # branch, proving the request traveled canonicalize → cache-miss →
        # limiter → fetch seam with the id alone.
        raise YttError(UNAVAILABLE, "recorded; unit suite has no network")

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _recording_fetch)

    result = await mcp.call_tool("get_youtube_transcript", {"url": form})
    sc = result.structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == UNAVAILABLE
    assert sc["video_id"] == VID
    assert seen == [VID]


# ---------------------------------------------------------------------------
# Leg 4 — only the canonical URL ever reaches yt-dlp
# ---------------------------------------------------------------------------


class _RecordingYoutubeDL:
    """Stand-in that records the URL ``extract_info`` was asked for, then
    stops the pipeline before any network work."""

    urls: list[str] = []
    opts_seen: list[dict] = []

    def __init__(self, opts: dict):
        self.opts = opts
        _RecordingYoutubeDL.opts_seen.append(opts)

    def __enter__(self) -> "_RecordingYoutubeDL":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> dict:
        _RecordingYoutubeDL.urls.append(url)
        # UNAVAILABLE: a plain fetch error — raised unchanged by the proxy
        # retry and never routed into the Whisper fallback.
        raise YttError(UNAVAILABLE, "recorded; stopping before any network")

    def urlopen(self, url: str) -> Any:
        raise AssertionError(
            f"urlopen({url!r}) reached — the recording stand-in stops at "
            "extract_info; nothing should dial past it in this test"
        )


@pytest.mark.asyncio
async def test_fetch_transcript_dials_only_the_canonical_literal(monkeypatch):
    """``fetch_transcript`` constructs the URL from the validated id alone.

    The real ``fetch_transcript`` runs (to_thread, timeout wrapper, proxy
    retry semantics) with only ``YoutubeDL`` replaced: the one URL yt-dlp
    would receive is the literal template over the id — whatever string the
    caller originally typed is gone by here.
    """
    monkeypatch.setattr(ytt.fetch.yt_dlp, "YoutubeDL", _RecordingYoutubeDL)

    with pytest.raises(YttError) as ei:
        await fetch_transcript(VID, None, Settings())

    assert ei.value.error_code == UNAVAILABLE
    assert _RecordingYoutubeDL.urls == [CANONICAL_TEMPLATE.format(video_id=VID)]


@pytest.mark.parametrize(
    "rel",
    ["ytt/fetch.py", "ytt/whisper.py", "ytt/canary.py"],
)
def test_every_ydl_call_site_shares_the_literal_template(rel):
    """All three YouTube-bound call sites embed the id via one literal.

    A call site that built a URL from anything else (the caller's string, a
    config value, user data) would silently widen the output invariant —
    the static leg makes that a test failure here, not a production SSRF.
    """
    src = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert 'f"https://www.youtube.com/watch?v={video_id}"' in src, (
        f"{rel} no longer builds its yt-dlp URL from the canonical literal "
        "template — docs/notes/input-security.md §The output invariant"
    )


# ---------------------------------------------------------------------------
# Leg 5 — arbitrary video ids: shape is provable offline, existence is not
# ---------------------------------------------------------------------------


@given(vid=st.text(alphabet=_ID_ALPHABET, min_size=11, max_size=11))
def test_any_shape_valid_id_is_accepted_verbatim(vid):
    """No existence probe: a shape-valid id maps to itself, no network."""
    assert canonicalize(vid) == vid
    # Invariant 3 — canonicalization is idempotent on its own output.
    assert canonicalize(canonicalize(vid)) == vid


@given(
    vid=st.text(alphabet=_ID_ALPHABET, min_size=11, max_size=11),
    extra=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=5),
)
def test_wrong_length_ids_are_rejected(vid, extra):
    """10 chars or 13 — anything off exactly 11 is ``bad_url``, never a
    best-effort trim."""
    with pytest.raises(YttError) as over:
        canonicalize(vid + extra)
    assert over.value.error_code == BAD_URL
    with pytest.raises(YttError) as under:
        canonicalize(vid[:-1])
    assert under.value.error_code == BAD_URL


@given(
    vid=st.text(alphabet=_ID_ALPHABET, min_size=11, max_size=11),
    bad=st.text(min_size=1, max_size=4).filter(lambda s: not _ID_ONLY_RE.fullmatch(s)),
)
def test_id_slot_with_off_alphabet_characters_is_rejected(vid, bad):
    """Non-``[A-Za-z0-9_-]`` material in the id slot never yields an id.

    The patch sits mid-string so surrounding whitespace stripping cannot
    rescue it; whatever the parse then sees (host, path, garbage), the
    outcome is a stable ``bad_url`` — not another exception type, and (per
    the tripwire) no packet.
    """
    with pytest.raises(YttError) as ei:
        canonicalize(vid[:5] + bad + vid[5:])
    assert ei.value.error_code == BAD_URL


# ---------------------------------------------------------------------------
# Leg 6 — get_transcript_job's id is an opaque key, never a URL ingredient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bogus",
    [
        "",
        "../../etc/passwd",
        "dQw4w9WgXcQ'; DROP TABLE jobs;--",
        VID,  # shape-valid but no such job
        "x" * 500,
    ],
    ids=["empty", "traversal", "injection", "valid-but-unknown", "overlong"],
)
async def test_get_transcript_job_bogus_id_is_not_found(bogus):
    """A fabricated job id misses the registry to ``not_found``.

    No canonicalize, no URL construction, no fetch seam — the boobytraps and
    tripwire stay silent because the id is only ever a dict key.
    """
    result = await mcp.call_tool("get_transcript_job", {"video_id": bogus})
    sc = result.structured_content
    assert sc is not None
    assert sc["status"] == "error"
    assert sc["error_code"] == "not_found"
