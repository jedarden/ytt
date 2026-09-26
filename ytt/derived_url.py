"""Derived-URL SSRF policy — the allowlist for yt-dlp-metadata URLs.

Spec: ``docs/notes/derived-url-policy.md`` (companion to
``docs/notes/input-security.md``).

Caller input never reaches the network: ``canonicalize`` reduces it to an
11-char id and every YouTube-bound call site rebuilds a literal watch URL
from it. But the transcript paths do not stop there — they also follow URLs
that yt-dlp extracted from the video's *metadata*:

- the json3 caption-track URL, dialed by ``ytt.fetch._do_fetch`` via
  ``ydl.urlopen``;
- the media/manifest format URLs the audio downloader resolves
  (``ytt.whisper._do_download_audio`` via ``ydl.download``);
- every redirect target reached from either.

Those strings are attacker-influenced to exactly the degree the metadata
response is. A compromised or lying extractor answer could point this
service's egress at localhost, the private network, or a cloud-metadata
endpoint — the same SSRF target set the input gate rejects — and yt-dlp
follows redirects *inside* its request backends, so validating only the
initial URL is not enough: each redirect hop is a fresh dial decided from
response data.

Three layers close that surface (belt, braces, and the buckle):

- :func:`validate_derived_url` — the scheme/host allowlist, the one policy
  every other layer delegates to;
- :func:`audit_audio_info_urls` — sweeps the URL surface of an
  ``extract_info`` info dict (everything the audio downloader is about to
  resolve) before the download starts, so a violation costs zero bytes and
  surfaces as a clean pre-download error rather than a mid-stream abort;
- :func:`install` — the network-layer gate, armed once at
  :mod:`ytt.fetch` import so every yt-dlp instance in the process runs
  guarded. It wraps three hooks: ``yt_dlp.YoutubeDL.urlopen`` (the single
  choke point every extractor and native downloader dial passes through —
  including the *second* metadata extraction ``ydl.download`` performs
  internally, whose result no caller ever sees), plus the two request
  backends' redirect decision points (``RequestsSession.rebuild_method``
  for the ``requests`` backend — the default when ``requests`` is
  installed — and ``RedirectHandler.redirect_request`` for the ``urllib``
  backend), so every hop of every redirect is validated too.

Rejections carry the stable ``bad_metadata_url`` error code
(:mod:`ytt.errors`) — never ``bad_url`` (the caller's input was fine) and
never ``empty_body`` (which would route the request into the Whisper ASR
fallback and download audio from the very video whose metadata misbehaved).
When the violation surfaces through yt-dlp as a network error instead of
directly, the message carries :data:`POLICY_VIOLATION_MARK` so
``classify_ydl_error`` maps it back to ``bad_metadata_url``.

The wrapped hooks are yt-dlp internals, pinned by the ``yt-dlp`` pin in
``pyproject.toml`` like ``SEED_MAP`` — verify them on a yt-dlp version bump
(``tests/unit/test_derived_url.py`` legs D/F fail fast — an AttributeError
at import, or a failed hook identity assertion — if a bump renames any
hook).
"""

from __future__ import annotations

from urllib.parse import urlparse

import yt_dlp

from ytt.errors import BAD_METADATA_URL, YttError
from ytt.observability import redact_credentials

# ---------------------------------------------------------------------------
# The policy, as data
# ---------------------------------------------------------------------------

#: Schemes a derived URL may use. Production egress is TLS-only; a metadata
#: response offering ``http``, ``file``, ``gopher``, ``data``, … is a
#: violation, not a compatibility problem.
DERIVED_URL_SCHEMES: frozenset[str] = frozenset({"https"})

#: Host suffixes a derived URL may point at — the same YouTube-controlled set
#: the egress-boundary guard records as sanctioned for these very paths
#: (``tests/unit/test_egress_boundary.py``: caption bodies come from
#: youtube.com, audio from googlevideo.com). Suffix match is anchored
#: (``rr3---sn-abc.googlevideo.com`` matches; ``evilmoogle.com`` and
#: ``youtube.com.evil.com`` do not).
DERIVED_URL_HOST_SUFFIXES: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtube-nocookie.com",
        "googlevideo.com",
        "ytimg.com",
    }
)

#: YouTube-controlled hosts sharing a broader domain that must NOT be
#: allowlisted wholesale (``googleapis.com`` is far too wide to suffix-match).
DERIVED_URL_EXACT_HOSTS: frozenset[str] = frozenset({"youtubei.googleapis.com"})

#: Seed string that ties a yt-dlp network error raised by any gate here back
#: to :data:`~ytt.errors.BAD_METADATA_URL` in
#: :func:`ytt.fetch.classify_ydl_error`.
POLICY_VIOLATION_MARK = "metadata URL rejected"


def _reject(url: object, what: str, reason: str) -> YttError:
    """Build the stable rejection for a policy violation.

    The offending URL is quoted (redacted) verbatim, matching the input
    gate's convention — tool responses are authenticated-caller content.
    """
    return YttError(
        BAD_METADATA_URL,
        f"{POLICY_VIOLATION_MARK}: {what} URL is not on the allowed "
        f"scheme/host policy ({reason}): {redact_credentials(str(url))!r}",
    )


def host_is_allowed(host: str | None) -> bool:
    """Whether *host* (already lowercase, no userinfo/port) is allowlisted."""
    if not host:
        return False
    if host in DERIVED_URL_EXACT_HOSTS:
        return True
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in DERIVED_URL_HOST_SUFFIXES
    )


def validate_derived_url(url: str, *, what: str = "derived") -> str:
    """Raise :class:`YttError` unless *url* may be dialed; return it unchanged.

    The URL is validated, never rewritten — what was checked is what gets
    dialed. Order of rejection (each reason is asserted by
    ``tests/unit/test_derived_url.py``):

    1. empty / non-string → ``empty URL``;
    2. the URL parser refuses the shape (unbalanced bracket, NFKC host
       delimiters, unparseable port) → ``malformed URL structure`` — fail
       closed, like ``canonicalize``;
    3. scheme not ``https`` → ``scheme not allowed``;
    4. embedded ``user:pass@`` userinfo → rejected outright (derived URLs
       never carry credentials, so smuggling resolves in the safe direction);
    5. explicit port → rejected (YouTube never serves one; a nonstandard
       port on an allowlisted host has no legitimate metadata origin);
    6. host not on the allowlist → ``host not allowed`` — this is where
       localhost, ``127.0.0.1``, ``[::1]``, RFC1918/link-local IP literals,
       cloud-metadata endpoints, lookalikes and trailing-dot hosts die.
    """
    if not isinstance(url, str) or not url.strip():
        raise _reject(url, what, "empty URL")

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname  # lowercase; userinfo/port/brackets stripped
        port = parsed.port  # may raise ValueError on a non-integer port
        username = parsed.username
    except ValueError:
        # urlsplit refuses some shapes outright (see canonicalize) — translate,
        # never leak a raw ValueError out of the policy.
        raise _reject(url, what, "malformed URL structure") from None

    if parsed.scheme.lower() not in DERIVED_URL_SCHEMES:
        raise _reject(url, what, f"scheme {parsed.scheme!r} not allowed")
    if username is not None:
        raise _reject(url, what, "userinfo (user:pass@) not allowed")
    if port is not None:
        raise _reject(url, what, f"explicit port :{port} not allowed")
    if not host_is_allowed(hostname):
        raise _reject(url, what, f"host {hostname!r} not allowed")

    return url


def _iter_audio_info_urls(info: dict):
    """Yield every URL the audio downloader would resolve from *info*.

    The downloader dials the selected format's URL, HLS/DASH manifest URLs,
    fragment base URLs, and absolute per-fragment URLs — all of them are
    metadata-derived and all of them are audited here, before
    ``ydl.download`` is allowed to start. Bare relative paths (no scheme,
    no netloc) resolve against an already-validated base URL and are
    skipped: they cannot name a different host than their base.
    Protocol-relative URLs (``//host/…``) are NOT skipped — joined against
    the base they change the host exactly like an absolute URL — and are
    audited in their https form, the only scheme this policy allows.
    """
    def _collect(candidate: object) -> str | None:
        if not isinstance(candidate, str) or not candidate:
            return None
        if candidate.startswith("//"):
            return f"https:{candidate}"  # protocol-relative → audited as https
        if "://" in candidate:
            return candidate
        return None

    for key in ("url", "manifest_url"):
        found = _collect(info.get(key))
        if found:
            yield found

    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        for key in ("url", "manifest_url", "fragment_base_url"):
            found = _collect(fmt.get(key))
            if found:
                yield found
        for fragment in fmt.get("fragments") or []:
            if isinstance(fragment, dict):
                found = _collect(fragment.get("url"))
                if found:
                    yield found


def audit_audio_info_urls(info: dict, *, what: str = "audio download") -> None:
    """Validate every URL in *info* the audio downloader is about to resolve.

    Called by ``ytt.whisper._do_download_audio`` after ``extract_info`` and
    before ``ydl.download``: the projected-size and duration caps have
    already rejected out-of-bounds videos, so a passing audit means the
    download — if it starts at all — can only dial allowlisted hosts. (The
    network-layer gates from :func:`install` stay armed behind it: they
    cover the download's *second* internal metadata extraction and every
    redirect hop, where this audit structurally cannot.)
    """
    for url in _iter_audio_info_urls(info):
        validate_derived_url(url, what=what)


# ---------------------------------------------------------------------------
# The network-layer gates (armed by install())
# ---------------------------------------------------------------------------

_installed = False
#: The original, unguarded hooks — kept so tests can prove install() wrapped
#: exactly them (and did not double-wrap on a second call).
_originals: dict[str, object] = {}


def _make_guarded_urlopen(original_urlopen):
    """Build the ``YoutubeDL.urlopen`` gate over *original_urlopen*.

    Factory so tests can drive the wrapper against a recording stand-in
    instead of a real network. ``req`` is whatever yt-dlp hands over: a
    ``str``, or a ``yt_dlp.networking.Request`` (the native downloaders
    pass Request objects — ``downloader/http.py``, ``fragment.py``) whose
    ``.url`` is the dial target.
    """
    from yt_dlp.networking.exceptions import RequestError

    def urlopen(ydl, req):  # type: ignore[no-untyped-def]
        target = req if isinstance(req, str) else getattr(req, "url", None)
        try:
            validate_derived_url(target, what="yt-dlp request")
        except YttError as exc:
            raise RequestError(exc.message) from exc
        return original_urlopen(ydl, req)

    return urlopen


def _make_guarded_rebuild_method(original_rebuild):
    """Build the ``requests``-backend redirect gate over *original_rebuild*.

    ``rebuild_method`` runs per redirect hop after the backend has set the
    next-hop URL and before the hop is sent.
    """
    from yt_dlp.networking.exceptions import RequestError

    def rebuild_method(session, prepared_request, response):  # type: ignore[no-untyped-def]
        original_rebuild(session, prepared_request, response)
        try:
            validate_derived_url(prepared_request.url, what="redirect target")
        except YttError as exc:
            raise RequestError(exc.message) from exc

    return rebuild_method


def _make_guarded_redirect_request(original_redirect):
    """Build the ``urllib``-backend redirect gate over *original_redirect*.

    ``redirect_request`` is the backend's redirect decision, receiving the
    raw ``newurl`` the ``Location`` header resolved to.
    """
    from yt_dlp.networking.exceptions import RequestError

    def redirect_request(handler, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        try:
            validate_derived_url(newurl, what="redirect target")
        except YttError as exc:
            raise RequestError(exc.message) from exc
        return original_redirect(handler, req, fp, code, msg, headers, newurl)

    return redirect_request


def install() -> None:
    """Arm the derived-URL gates on this process's yt-dlp (idempotent).

    Wrapped hooks (yt-dlp internals — a maintenance point on version bumps,
    like ``SEED_MAP``):

    - ``yt_dlp.YoutubeDL.urlopen`` — every outbound dial of every extractor
      and native downloader passes here, including the second metadata
      extraction ``ydl.download`` performs internally;
    - ``yt_dlp.networking._requests.RequestsSession.rebuild_method`` — the
      ``requests`` backend's per-hop redirect decision (only armed when
      ``requests`` is installed; this project runs yt-dlp on the ``urllib``
      backend, so absence is the normal production shape, not an error);
    - ``yt_dlp.networking._urllib.RedirectHandler.redirect_request`` — the
      ``urllib`` backend's redirect decision, receiving the raw ``newurl``.

    A violation is raised as ``yt_dlp.networking.exceptions.RequestError``
    (the backends' own exception type) carrying
    :data:`POLICY_VIOLATION_MARK` in the message: it propagates through the
    backends exactly like any other network failure, surfaces as a
    ``DownloadError``/``ExtractorError`` where yt-dlp wraps those, and
    ``classify_ydl_error`` maps the marker to ``bad_metadata_url``.
    """
    global _installed
    if _installed:
        return

    # Private yt_dlp modules on purpose — this is the pinned-internals seam.
    from yt_dlp.networking import _urllib

    # Gate 1 — every dial ( YoutubeDL.urlopen is the single choke point).
    original_urlopen = yt_dlp.YoutubeDL.urlopen
    yt_dlp.YoutubeDL.urlopen = _make_guarded_urlopen(original_urlopen)
    _originals["urlopen"] = original_urlopen

    # Gate 2 — redirect hops inside one dial, urllib backend (always present).
    original_redirect = _urllib.RedirectHandler.redirect_request
    _urllib.RedirectHandler.redirect_request = _make_guarded_redirect_request(
        original_redirect
    )
    _originals["redirect_request"] = original_redirect

    # Gate 3 — redirect hops inside one dial, requests backend. Optional
    # dependency: not installed in this project's venv/image, so the import
    # fails and the urllib gates above carry the redirect surface.
    try:
        from yt_dlp.networking import _requests
    except ImportError:
        pass
    else:
        original_rebuild = _requests.RequestsSession.rebuild_method
        _requests.RequestsSession.rebuild_method = _make_guarded_rebuild_method(
            original_rebuild
        )
        _originals["rebuild_method"] = original_rebuild

    _installed = True
