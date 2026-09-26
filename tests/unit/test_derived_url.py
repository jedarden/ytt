"""Derived-URL SSRF policy contract (docs/notes/derived-url-policy.md).

Caller input is closed off by ``test_input_security_contract.py`` — the
submitted string is canonicalized, never dialed. This module pins the second
surface: URLs yt-dlp extracts from a video's *metadata* (the caption track,
the media formats, and every redirect target reached from either). Those URLs
are followed, not authored, so they are held to an allowlist rather than a
canonical form:

- **The policy** (``ytt.derived_url.validate_derived_url``) — https-only, a
  YouTube-controlled host allowlist (suffix-anchored plus one exact host), no
  userinfo, no explicit port, parse-refusal fails closed. The regression set
  the bead asks for: external, localhost, private-network (loopback / RFC1918
  / link-local / cloud-metadata, including decimal/hex/octal IP spellings),
  and malformed metadata URLs all reject with stable ``bad_metadata_url``.
- **Enforcement layers** — caption-path pre-dial validation, audio-path
  pre-download audit, and the process-wide network gates installed at
  ``ytt.fetch`` import (``YoutubeDL.urlopen`` plus both request backends'
  redirect decision points).
- **Rejection behavior** — ``bad_metadata_url``, never ``bad_url`` (nothing
  for the caller to fix) and never ``empty_body`` (that code triggers the
  Whisper ASR fallback, which would download audio from the very video whose
  metadata misbehaved): the server surfaces it as a plain fetch error and
  starts no job.

All tests are offline: no network, no DNS, no extraction.
"""

from __future__ import annotations

import socket
import urllib.request as urllib_request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp
from yt_dlp.networking import Request as YtdlpRequest
from yt_dlp.networking.exceptions import RequestError

import ytt.fetch
import ytt.whisper
from ytt.config import Settings
from ytt.derived_url import (
    DERIVED_URL_EXACT_HOSTS,
    DERIVED_URL_HOST_SUFFIXES,
    DERIVED_URL_SCHEMES,
    POLICY_VIOLATION_MARK,
    _iter_audio_info_urls,
    _make_guarded_rebuild_method,
    _make_guarded_urlopen,
    _originals,
    audit_audio_info_urls,
    host_is_allowed,
    install,
    validate_derived_url,
)
from ytt.errors import BAD_METADATA_URL, YttError
from ytt.fetch import SEED_MAP, classify_ydl_error, fetch_transcript
from ytt.whisper import _do_download_audio

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Dedicated id for the e2e legs — deliberately not one any other test module
#: may have put in the transcript cache or the Whisper registry.
VID = "dRlMetA0001"

#: The one legitimately-shaped caption track URL form (www.youtube.com).
TRACK_URL = f"https://www.youtube.com/api/timedtext?v={VID}&lang=en&fmt=json3"

#: The canonical off-policy target every leg agrees on: cloud metadata.
EVIL = "https://169.254.169.254/latest/meta-data/"


# ---------------------------------------------------------------------------
# Fixtures — offline enforcement
# ---------------------------------------------------------------------------


class EgressViolation(AssertionError):
    """A real connection was attempted inside the derived-URL tests."""


@pytest.fixture(autouse=True)
def _no_real_egress(monkeypatch: pytest.MonkeyPatch):
    """Fail loudly (and at teardown, even if swallowed) on any real dial."""
    attempts: list[str] = []

    def _refuse(name: str):
        def handler(*a: Any, **k: Any):
            desc = f"{name}({a[1] if len(a) > 1 else a[0]!r})"
            attempts.append(desc)
            raise EgressViolation(desc)

        return handler

    monkeypatch.setattr(socket.socket, "connect", _refuse("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", _refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _refuse("getaddrinfo"))
    yield
    assert not attempts, f"swallowed connection attempt(s): {attempts}"


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """Direct ``mcp.call_tool()`` runs with no HTTP request context."""
    from ytt.server import mcp

    from fastmcp.server.middleware.authorization import AuthMiddleware

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    """Roomy deterministic limiter — the suite shares the module-level one."""
    from ytt.ratelimit import SubjectRateLimiter

    monkeypatch.setattr(
        "ytt.server._rate_limiter",
        SubjectRateLimiter(capacity=500, refill_rate_per_sec=500.0 / 60.0),
    )


# ---------------------------------------------------------------------------
# Leg A — the policy accepts real YouTube-controlled URL shapes
# ---------------------------------------------------------------------------

ALLOWED_DERIVED_URLS = [
    # caption track: timedtext on www.youtube.com
    TRACK_URL,
    # media format on a googlevideo CDN shard (the audio path's real target)
    "https://rr3---sn-nx57ynsk.googlevideo.com/videoplayback?ip=203.0.113.7&signature=x",
    # HLS manifest host + path shapes
    "https://manifest.googlevideo.com/api/manifest/hls_playlist/id/x/master.m3u8",
    # subdomains of the suffix set: consent interstitials, thumbnails
    "https://consent.youtube.com/m?continue=x",
    "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    # the exact InnerTube host the pinned player clients talk to
    "https://youtubei.googleapis.com/youtubei/v1/player?key=x",
    # bare suffix equality and the nocookie domain
    "https://youtube.com/",
    "https://youtube-nocookie.com/embed/dQw4w9WgXcQ",
    # scheme/host case-insensitivity (as urlparse reports them, lowercased)
    "HTTPS://WWW.YOUTUBE.COM/api/timedtext?lang=en",
]


@pytest.mark.parametrize("url", ALLOWED_DERIVED_URLS, ids=lambda v: v[:58])
def test_allowed_derived_urls_pass_through_unchanged(url):
    """Accepted URLs are returned verbatim — validated, never rewritten.

    What was checked must be exactly what gets dialed; a rewriting validator
    would silently widen the gap between the audit and the dial.
    """
    assert validate_derived_url(url, what="probe") is url


def test_policy_is_idempotent():
    """validate(validate(x)) == validate(x) — the layers may re-check."""
    once = validate_derived_url(TRACK_URL)
    assert validate_derived_url(once) is once


def test_allowlist_shape_is_pinned():
    """The allowlist data is exactly the documented set.

    Exact (not subset) equality: widening the host/scheme set is a code
    review event that must update this test and the policy doc, not a quiet
    constant edit — there is deliberately no configuration knob.
    """
    assert DERIVED_URL_SCHEMES == frozenset({"https"})
    assert DERIVED_URL_HOST_SUFFIXES == frozenset(
        {"youtube.com", "youtube-nocookie.com", "googlevideo.com", "ytimg.com"}
    )
    assert DERIVED_URL_EXACT_HOSTS == frozenset({"youtubei.googleapis.com"})


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("www.youtube.com", True),
        ("youtube.com", True),
        ("rr3---sn-nx57ynsk.googlevideo.com", True),
        ("ytimg.com", True),
        ("i.ytimg.com", True),
        ("youtubei.googleapis.com", True),  # exact host
        ("googleapis.com", False),  # the broad domain is NOT allowlisted
        ("youtube.com.evil.com", False),  # suffix must be dot-anchored
        ("evilmoogle.com", False),  # substring, not suffix
        ("youtube.com.", False),  # trailing dot is a different host
        ("xn--utube-9re.com", False),
        ("", False),
        (None, False),
    ],
    ids=str,
)
def test_host_is_allowed_suffix_anchoring(host, allowed):
    assert host_is_allowed(host) is allowed


# ---------------------------------------------------------------------------
# Leg B — the rejection table: external, localhost, private, malformed
# ---------------------------------------------------------------------------

REJECTED_DERIVED_URLS: list[tuple[object, str]] = [
    # -- localhost / loopback -------------------------------------------------
    ("https://localhost/api/timedtext?v=x", "host 'localhost' not allowed"),
    ("https://LOCALHOST/api/timedtext?v=x", "host 'localhost' not allowed"),
    ("https://127.0.0.1/videoplayback?x=1", "host '127.0.0.1' not allowed"),
    ("https://127.255.255.254/videoplayback", "host '127.255.255.254' not allowed"),
    ("https://0.0.0.0/videoplayback", "host '0.0.0.0' not allowed"),
    ("https://[::1]/api/timedtext", "host '::1' not allowed"),
    ("https://[::ffff:127.0.0.1]/videoplayback", "host '::ffff:127.0.0.1' not allowed"),
    # -- private networks (RFC1918) -------------------------------------------
    ("https://10.1.2.3/videoplayback", "host '10.1.2.3' not allowed"),
    ("https://172.16.0.9/videoplayback", "host '172.16.0.9' not allowed"),
    ("https://192.168.1.10/videoplayback", "host '192.168.1.10' not allowed"),
    # -- link-local / cloud metadata ------------------------------------------
    ("https://169.254.169.254/latest/meta-data/", "host '169.254.169.254' not allowed"),
    ("https://169.254.170.2/v2/credentials", "host '169.254.170.2' not allowed"),
    (
        "https://metadata.google.internal/computeMetadata/v1/instance/name",
        "host 'metadata.google.internal' not allowed",
    ),
    # -- loopback in non-dotted spellings (an allowlist ignores spelling) ------
    ("https://2130706433/videoplayback", "host '2130706433' not allowed"),
    ("https://0x7f000001/videoplayback", "host '0x7f000001' not allowed"),
    ("https://017700000001/videoplayback", "host '017700000001' not allowed"),
    # -- external, non-YouTube -------------------------------------------------
    ("https://evil.com/videoplayback?x=1", "host 'evil.com' not allowed"),
    ("https://example.com/api/timedtext", "host 'example.com' not allowed"),
    # -- lookalikes / suffix anchoring ------------------------------------------
    ("https://youtube.com.evil.com/watch", "host 'youtube.com.evil.com' not allowed"),
    (
        "https://rr3---sn-x.googlevideo.com.evil.com/videoplayback",
        "host 'rr3---sn-x.googlevideo.com.evil.com' not allowed",
    ),
    ("https://evilmoogle.com/x", "host 'evilmoogle.com' not allowed"),
    ("https://youtube.com./api/timedtext", "host 'youtube.com.' not allowed"),
    (
        "https://youtubei.googleapis.com.evil.com/youtubei/v1/player",
        "host 'youtubei.googleapis.com.evil.com' not allowed",
    ),
    ("https://notyoutube.com/x", "host 'notyoutube.com' not allowed"),
    # -- scheme: https-only (downgrades and exotic schemes are violations) ------
    (
        "http://rr3---sn-nx57ynsk.googlevideo.com/videoplayback",
        "scheme 'http' not allowed",
    ),
    ("ftp://www.youtube.com/api/timedtext", "scheme 'ftp' not allowed"),
    ("file:///etc/passwd", "scheme 'file' not allowed"),
    ("data:text/html,<h1>x</h1>", "scheme 'data' not allowed"),
    ("about:blank", "scheme 'about' not allowed"),
    ("javascript:alert(1)", "scheme 'javascript' not allowed"),
    # -- userinfo: derived URLs never carry credentials --------------------------
    (
        "https://user:pass@rr3---sn-x.googlevideo.com/videoplayback",
        "userinfo (user:pass@) not allowed",
    ),
    ("https://user@www.youtube.com/api/timedtext", "userinfo"),
    # userinfo smuggling resolves in the safe direction: a credential-bearing
    # derived URL is rejected outright even when the host side is allowlisted
    ("https://youtube.com@evil.com/watch", "userinfo (user:pass@) not allowed"),
    ("https://evil.com@www.youtube.com/api/timedtext", "userinfo"),
    # -- explicit ports: YouTube never serves one ---------------------------------
    ("https://www.youtube.com:8443/api/timedtext", "explicit port :8443 not allowed"),
    ("https://rr3---sn-x.googlevideo.com:443/videoplayback", "explicit port :443 not allowed"),
    ("https://[::1]:443/x", "explicit port :443 not allowed"),
    # -- malformed: the parser refuses, the policy fails closed -------------------
    ("https://[::1/api/timedtext", "malformed URL structure"),
    ("https://youtube.com:port/x", "malformed URL structure"),
    ("https://youtube.com:99999/x", "malformed URL structure"),
    # -- empty / non-string --------------------------------------------------------
    ("", "empty URL"),
    ("   ", "empty URL"),
    (None, "empty URL"),
    (b"https://www.youtube.com/api/timedtext", "empty URL"),
    (12345, "empty URL"),
]


@pytest.mark.parametrize(("url", "reason"), REJECTED_DERIVED_URLS, ids=lambda v: repr(v)[:52])
def test_rejected_derived_urls_raise_bad_metadata_url(url, reason):
    """Every documented rejection class fails with stable ``bad_metadata_url``."""
    with pytest.raises(YttError) as ei:
        validate_derived_url(url, what="probe")
    assert ei.value.error_code == BAD_METADATA_URL
    assert reason in ei.value.message
    # The marker ties any pass through yt-dlp back to this code (leg H).
    assert POLICY_VIOLATION_MARK in ei.value.message


def test_rejection_message_shape():
    """marker + surface + reason + redacted-verbatim URL, in that order."""
    url = "https://127.0.0.1/videoplayback?x=1"
    with pytest.raises(YttError) as ei:
        validate_derived_url(url, what="audio download")
    m = ei.value.message
    assert m.startswith(f"{POLICY_VIOLATION_MARK}: ")
    assert "audio download" in m
    assert "host '127.0.0.1' not allowed" in m
    assert repr(url) in m  # quoted redacted, matching the input gate's convention


def test_rejection_is_a_pure_function_of_the_string():
    """No state, no retry path — the same URL rejects identically forever."""
    with pytest.raises(YttError) as first:
        validate_derived_url(EVIL, what="probe")
    with pytest.raises(YttError) as second:
        validate_derived_url(EVIL, what="probe")
    assert first.value.message == second.value.message
    assert first.value.error_code == second.value.error_code


# ---------------------------------------------------------------------------
# Leg C — the audio-path audit sweeps every URL the downloader resolves
# ---------------------------------------------------------------------------

BENIGN_FMT_URL = "https://rr3---sn-nx57ynsk.googlevideo.com/videoplayback?x=1"


def _info_with_evil(where: str) -> dict:
    """An otherwise-benign audio info dict with EVIL planted at *where*."""
    fmt: dict = {"format_id": "140", "ext": "m4a", "url": BENIGN_FMT_URL}
    if where == "format_url":
        fmt["url"] = EVIL
    elif where == "manifest_url":
        fmt["manifest_url"] = EVIL
    elif where == "fragment_base_url":
        fmt["fragment_base_url"] = EVIL
    elif where == "fragment_url":
        fmt["fragments"] = [{"url": EVIL}]
    elif where == "top_level_url":
        return {"url": EVIL, "duration": 600, "formats": [fmt]}
    elif where == "top_level_manifest":
        return {"manifest_url": EVIL, "duration": 600, "formats": [fmt]}
    return {"duration": 600, "formats": [fmt]}


@pytest.mark.parametrize(
    "where",
    [
        "format_url",
        "manifest_url",
        "fragment_base_url",
        "fragment_url",
        "top_level_url",
        "top_level_manifest",
    ],
)
def test_audit_rejects_evil_url_at_every_resolved_position(where):
    """The audit is keyed by what the downloader dials, not by one field."""
    with pytest.raises(YttError) as ei:
        audit_audio_info_urls(_info_with_evil(where), what="audio download")
    assert ei.value.error_code == BAD_METADATA_URL
    assert POLICY_VIOLATION_MARK in ei.value.message


def test_audit_passes_a_benign_info_dict():
    info = {
        "duration": 600,
        "formats": [
            {
                "format_id": "140",
                "ext": "m4a",
                "url": BENIGN_FMT_URL,
                "manifest_url": "https://manifest.googlevideo.com/api/manifest/dash/id/x",
                "fragment_base_url": "https://rr3---sn-x.googlevideo.com/videoplayback/",
                "fragments": [{"url": "https://rr3---sn-x.googlevideo.com/seg-r1"}],
            }
        ],
    }
    audit_audio_info_urls(info, what="audio download")  # must not raise


def test_audit_skips_bare_relative_fragments():
    """A relative fragment resolves against the validated base — same host.

    It cannot name a different origin, so it is not a dial decision and the
    audit does not fail on it.
    """
    info = {
        "formats": [
            {
                "url": BENIGN_FMT_URL,
                "fragments": [{"url": "seg-r1"}, {"url": "../seg-r2"}],
            }
        ]
    }
    assert list(_iter_audio_info_urls(info)) == [BENIGN_FMT_URL]
    audit_audio_info_urls(info, what="audio download")  # must not raise


def test_audit_catches_protocol_relative_fragments():
    """``//host/…`` changes host exactly like an absolute URL — audited.

    It is audited in its https form (the only scheme the policy allows), so
    a protocol-relative link-local target dies here rather than at urljoin.
    """
    info = {
        "formats": [
            {"url": BENIGN_FMT_URL, "fragments": [{"url": "//169.254.169.254/seg"}]}
        ]
    }
    assert list(_iter_audio_info_urls(info)) == [
        BENIGN_FMT_URL,
        "https://169.254.169.254/seg",
    ]
    with pytest.raises(YttError) as ei:
        audit_audio_info_urls(info, what="audio download")
    assert ei.value.error_code == BAD_METADATA_URL


def test_audit_ignores_non_dict_formats_and_non_string_urls():
    """Metadata shapes yt-dlp does not promise not to produce are tolerated —
    and tolerated in the safe direction (skipped, never trusted)."""
    info = {"formats": ["not-a-dict", None, {"url": 123}, {"url": EVIL}]}
    with pytest.raises(YttError) as ei:
        audit_audio_info_urls(info, what="audio download")
    assert ei.value.error_code == BAD_METADATA_URL


# ---------------------------------------------------------------------------
# Leg D — the process-wide gates: armed hooks, idempotence, wrapper semantics
# ---------------------------------------------------------------------------


def _requests_backend_available() -> bool:
    try:
        import requests  # noqa: F401
    except ImportError:
        return False
    return True


def test_import_of_ytt_fetch_arms_the_gates():
    """``ytt.fetch`` import installed the guards — production's armed state."""
    from yt_dlp.networking import _urllib

    assert yt_dlp.YoutubeDL.urlopen is not _originals["urlopen"]
    assert (
        _urllib.RedirectHandler.redirect_request is not _originals["redirect_request"]
    )
    if _requests_backend_available():
        from yt_dlp.networking import _requests

        assert (
            _requests.RequestsSession.rebuild_method
            is not _originals["rebuild_method"]
        )
    else:
        # requests absent (this project's shape): the requests-backend gate
        # is deliberately skipped, the urllib gates carry the surface.
        assert "rebuild_method" not in _originals


def test_install_is_idempotent():
    """A second install() wraps nothing twice."""
    before_urlopen = yt_dlp.YoutubeDL.urlopen
    before_originals = dict(_originals)
    install()
    assert yt_dlp.YoutubeDL.urlopen is before_urlopen
    assert _originals == before_originals


def test_urlopen_gate_passes_allowlisted_dials_through():
    """Factory semantics: allowlisted str and Request dials reach the backend."""
    dialed: list[Any] = []

    def original(ydl, req):  # type: ignore[no-untyped-def]
        dialed.append(req)
        return "sentinel"

    gate = _make_guarded_urlopen(original)
    as_request = YtdlpRequest(TRACK_URL)
    assert gate(None, TRACK_URL) == "sentinel"
    assert gate(None, as_request) == "sentinel"
    # The exact objects were forwarded — nothing re-wrapped, nothing dropped.
    assert dialed[0] == TRACK_URL
    assert dialed[1] is as_request


def test_urlopen_gate_rejects_off_policy_dials_before_the_backend():
    """str and Request shapes both die pre-dial, as RequestError + marker."""
    dialed: list[Any] = []

    def original(ydl, req):  # type: ignore[no-untyped-def]
        dialed.append(req)  # pragma: no cover — must never run
        return "sentinel"

    gate = _make_guarded_urlopen(original)
    for req in (EVIL, YtdlpRequest(EVIL), YtdlpRequest("https://127.0.0.1/x")):
        with pytest.raises(RequestError) as ei:
            gate(None, req)
        assert POLICY_VIOLATION_MARK in str(ei.value)
        assert isinstance(ei.value.__cause__, YttError)
        assert ei.value.__cause__.error_code == BAD_METADATA_URL
    assert dialed == []  # the backend never saw the dial


def test_urllib_redirect_gate_rejects_the_hop():
    """The urllib backend's redirect decision validates the new location."""
    from yt_dlp.networking import _urllib

    handler = _urllib.RedirectHandler()
    with pytest.raises(RequestError) as ei:
        handler.redirect_request(
            req=None, fp=None, code=302, msg="Found", headers={}, newurl=EVIL
        )
    assert POLICY_VIOLATION_MARK in str(ei.value)
    assert ei.value.__cause__.error_code == BAD_METADATA_URL


def test_urllib_redirect_gate_allows_an_allowlisted_hop():
    """An allowlisted target passes the gate and the original handler runs —
    which, for an https target, just builds the next Request (offline)."""
    from yt_dlp.networking import _urllib

    handler = _urllib.RedirectHandler()
    req = urllib_request.Request(TRACK_URL)
    out = handler.redirect_request(
        req=req,
        fp=None,
        code=302,
        msg="Found",
        headers={},
        newurl=BENIGN_FMT_URL,
    )
    assert isinstance(out, urllib_request.Request)
    assert out.full_url == BENIGN_FMT_URL


def test_requests_redirect_gate_runs_original_first_then_validates():
    """rebuild_method's hop is BUILT first, then gated — the gate sees the
    final next-hop URL the backend is about to send."""
    built: list[str] = []

    def original(session, prepared_request, response):  # type: ignore[no-untyped-def]
        built.append(prepared_request.url)

    gate = _make_guarded_rebuild_method(original)
    prepared = MagicMock()
    prepared.url = EVIL
    with pytest.raises(RequestError) as ei:
        gate(None, prepared, None)
    assert built == [EVIL]  # the hop was rebuilt, then refused before sending
    assert POLICY_VIOLATION_MARK in str(ei.value)


def test_real_ydl_instance_rejects_off_policy_dial_before_any_socket():
    """The class yt-dlp actually instantiates is gated — a plain dial of a
    cloud-metadata URL dies as RequestError without a socket (the module
    tripwire proves the 'before any socket' half)."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        with pytest.raises(RequestError) as ei:
            ydl.urlopen(EVIL)
    assert POLICY_VIOLATION_MARK in str(ei.value)


# ---------------------------------------------------------------------------
# Leg E — caption path: the track URL is validated before it is dialed
# ---------------------------------------------------------------------------


def _install_fake_fetch_ydl(monkeypatch, *, info: dict, json3_bytes: bytes | None):
    """Recording ``YoutubeDL`` stand-in for ``ytt.fetch`` (input-security
    pattern): records extract/urlopen targets, fakes the json3 body."""
    extract_urls: list[str] = []
    urlopen_urls: list[str] = []

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    class _FakeYDL:
        def __init__(self, opts: dict) -> None:
            self.opts = opts

        def __enter__(self) -> "_FakeYDL":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def extract_info(self, url: str, download: bool = False) -> dict:
            extract_urls.append(url)
            return info

        def urlopen(self, url: str) -> Any:
            urlopen_urls.append(url)
            assert json3_bytes is not None, f"unexpected dial: {url!r}"
            return _FakeResponse(json3_bytes)

    monkeypatch.setattr(ytt.fetch.yt_dlp, "YoutubeDL", _FakeYDL)
    return extract_urls, urlopen_urls


def _caption_info(track_url: str) -> dict:
    return {
        "id": VID,
        "title": "Metadata Policy Fixture",
        "duration": 120,
        "language": "en",
        "subtitles": {"en": [{"ext": "json3", "url": track_url}]},
        "automatic_captions": {},
    }


async def test_evil_caption_track_url_rejected_before_any_dial(monkeypatch):
    """A lying extractor answer pointing the timedtext URL off-policy is a
    pre-dial ``bad_metadata_url`` — extraction happened, nothing was dialed."""
    extract_urls, urlopen_urls = _install_fake_fetch_ydl(
        monkeypatch, info=_caption_info(EVIL), json3_bytes=None
    )
    with pytest.raises(YttError) as ei:
        await fetch_transcript(VID, "en", Settings())
    assert ei.value.error_code == BAD_METADATA_URL
    assert POLICY_VIOLATION_MARK in ei.value.message
    assert "caption track" in ei.value.message
    assert urlopen_urls == []  # the rejection preceded the dial
    assert extract_urls == [f"https://www.youtube.com/watch?v={VID}"]


async def test_benign_caption_track_url_is_dialed_exactly_as_validated(monkeypatch):
    """The happy path is untouched: the validated URL is the dialed URL."""
    extract_urls, urlopen_urls = _install_fake_fetch_ydl(
        monkeypatch, info=_caption_info(TRACK_URL), json3_bytes=b'{"events":[]}'
    )
    result = await fetch_transcript(VID, "en", Settings())
    assert urlopen_urls == [TRACK_URL]
    assert result.served_lang == "en"
    assert result.source == "caption_manual"


async def test_gate_error_mid_json3_fetch_classifies_to_bad_metadata_url(monkeypatch):
    """A redirect-hop rejection inside the json3 fetch surfaces as a yt-dlp
    RequestError (the gates' translation) and the marker maps it — through
    the real classify path — to ``bad_metadata_url``, never ``empty_body``."""

    class _FakeYDL:
        def __init__(self, opts: dict) -> None:
            pass

        def __enter__(self) -> "_FakeYDL":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def extract_info(self, url: str, download: bool = False) -> dict:
            return _caption_info(TRACK_URL)

        def urlopen(self, url: str) -> Any:
            raise RequestError(
                f"{POLICY_VIOLATION_MARK}: redirect target URL is not on the "
                f"allowed scheme/host policy (host '169.254.169.254' not "
                f"allowed): {EVIL!r}"
            )

    monkeypatch.setattr(ytt.fetch.yt_dlp, "YoutubeDL", _FakeYDL)
    with pytest.raises(YttError) as ei:
        await fetch_transcript(VID, "en", Settings())
    assert ei.value.error_code == BAD_METADATA_URL


# ---------------------------------------------------------------------------
# Leg F — audio path: the audit runs before any download starts
# ---------------------------------------------------------------------------


def _audio_stub(info: dict, download_side_effect: Exception | None = None):
    """Mock YoutubeDL context manager for the audio path (test_whisper shape)."""
    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = info
    if download_side_effect is not None:
        mock_ydl.download.side_effect = download_side_effect
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    return mock_ctx, mock_ydl


def test_evil_audio_format_url_rejected_before_any_download(tmp_path):
    """A lying format URL pointing at cloud metadata is refused pre-download —
    ``ydl.download`` is never invoked, no bytes reach scratch."""
    stub, mock_ydl = _audio_stub(_info_with_evil("format_url"))
    with patch(
        "ytt.whisper.yt_dlp.YoutubeDL",
        side_effect=lambda opts: stub,
    ):
        with pytest.raises(YttError) as ei:
            _do_download_audio(
                VID, str(tmp_path / "scratch"), 500 * 1024 * 1024,
                max_asr_duration_sec=1200,
            )
    assert ei.value.error_code == BAD_METADATA_URL
    assert POLICY_VIOLATION_MARK in ei.value.message
    assert "audio download" in ei.value.message
    mock_ydl.download.assert_not_called()
    scratch = tmp_path / "scratch"
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_benign_audio_info_downloads(tmp_path):
    """The happy path is untouched: a benign info dict downloads."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / f"{VID}.m4a").write_bytes(b"audio")
    benign = {"duration": 600, "formats": [_info_with_evil("format_url")["formats"][0]]}
    benign["formats"][0]["url"] = BENIGN_FMT_URL
    stub, mock_ydl = _audio_stub(benign)
    with patch(
        "ytt.whisper.yt_dlp.YoutubeDL",
        side_effect=lambda opts: stub,
    ):
        out = _do_download_audio(
            VID, str(scratch), 500 * 1024 * 1024, max_asr_duration_sec=1200
        )
    assert out == str(scratch / f"{VID}.m4a")
    mock_ydl.download.assert_called_once()


def test_audio_gate_error_mid_download_classifies_to_bad_metadata_url(tmp_path):
    """A redirect-hop rejection during the download proper — DownloadError
    carrying the marker — classifies to ``bad_metadata_url``."""
    benign = {"duration": 600, "formats": [{"format_id": "140", "ext": "m4a",
                                            "url": BENIGN_FMT_URL}]}
    stub, _ = _audio_stub(
        benign,
        download_side_effect=yt_dlp.utils.DownloadError(
            f"ERROR: {POLICY_VIOLATION_MARK}: redirect target URL is not on "
            f"the allowed scheme/host policy (host not allowed): {EVIL!r}"
        ),
    )
    with patch(
        "ytt.whisper.yt_dlp.YoutubeDL",
        side_effect=lambda opts: stub,
    ):
        with pytest.raises(YttError) as ei:
            _do_download_audio(
                VID, str(tmp_path / "scratch"), 500 * 1024 * 1024,
                max_asr_duration_sec=1200,
            )
    assert ei.value.error_code == BAD_METADATA_URL


# ---------------------------------------------------------------------------
# Leg G — the server surfaces bad_metadata_url WITHOUT the Whisper fallback
# ---------------------------------------------------------------------------


async def test_server_surfaces_bad_metadata_url_without_whisper_fallback(monkeypatch):
    """``empty_body`` is the ONLY code that starts a Whisper job.

    A metadata-policy violation must take the plain-error branch: no ASR job,
    no quota charge, no audio download — downloading audio from the very
    video whose metadata misbehaved is the one response we must never have.
    """
    from ytt.server import mcp, whisper_registry

    async def _policy_violation_fetch(video_id, lang, settings, **kwargs):  # type: ignore[no-untyped-def]
        raise YttError(
            BAD_METADATA_URL,
            f"{POLICY_VIOLATION_MARK}: caption track URL is not on the allowed "
            f"scheme/host policy (host '169.254.169.254' not allowed): {EVIL!r}",
        )

    async def _no_whisper(*args: Any, **kwargs: Any):
        raise AssertionError("Whisper fallback entered for bad_metadata_url")

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _policy_violation_fetch)
    monkeypatch.setattr(ytt.whisper, "run_whisper_job", _no_whisper)

    result = await mcp.call_tool(
        "get_youtube_transcript", {"url": f"https://www.youtube.com/watch?v={VID}"}
    )
    sc = result.structured_content
    assert sc is not None
    assert sc["status"] == "error"
    assert sc["error_code"] == BAD_METADATA_URL
    assert sc["video_id"] == VID  # a real id, not the empty rejected-input id
    assert POLICY_VIOLATION_MARK in sc["message"]
    # …and nothing was minted behind the error.
    assert await whisper_registry.get(VID) is None


# ---------------------------------------------------------------------------
# Leg H — classification: the marker maps to bad_metadata_url, first
# ---------------------------------------------------------------------------


def test_marker_classifies_to_bad_metadata_url():
    msg = (
        "ERROR: unable to download video data: "
        f"{POLICY_VIOLATION_MARK}: redirect target URL is not on the allowed "
        f"scheme/host policy (host not allowed): {EVIL!r}"
    )
    assert classify_ydl_error(msg) == BAD_METADATA_URL


def test_marker_takes_priority_over_every_generic_seed():
    """A violating response may quote seed strings of its own — the marker is
    pinned FIRST in SEED_MAP so the security code always wins."""
    msg = f"{POLICY_VIOLATION_MARK}: ... Private video Sign in to confirm your age"
    assert classify_ydl_error(msg) == BAD_METADATA_URL
    assert SEED_MAP[0] == (POLICY_VIOLATION_MARK, BAD_METADATA_URL)


# ---------------------------------------------------------------------------
# Leg I — the spec doc and the enforcement call sites cannot drift apart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "needle"),
    [
        ("ytt/fetch.py", 'validate_derived_url(track_url, what="caption track")'),
        ("ytt/whisper.py", 'audit_audio_info_urls(info, what="audio download")'),
        ("ytt/fetch.py", "install()"),
    ],
)
def test_enforcement_call_sites_exist(rel, needle):
    """A call site losing its gate is a silent unguarding — make it a test
    failure here instead."""
    src = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert needle in src, f"{rel} no longer enforces the derived-URL policy"


def test_policy_doc_names_the_contract():
    """The spec (docs/notes/derived-url-policy.md) pins code, marker, and the
    rejection behavior this module enforces — and both directions of the
    cross-reference to the input gate exist."""
    doc = (REPO_ROOT / "docs/notes/derived-url-policy.md").read_text(encoding="utf-8")
    assert "bad_metadata_url" in doc
    assert POLICY_VIOLATION_MARK in doc
    assert "empty_body" in doc  # the not-a-Whisper-trigger clause
    assert "169.254.169.254" in doc  # the SSRF target set is named, not implied
    assert "derived-url-policy.md" in (
        REPO_ROOT / "docs/notes/input-security.md"
    ).read_text(encoding="utf-8")
    assert "docs/notes/derived-url-policy.md" in (
        REPO_ROOT / "ytt/errors.py"
    ).read_text(encoding="utf-8")
