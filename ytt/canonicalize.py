"""URL -> canonical video_id (plan: URL -> canonical video_id, load-bearing).

Runs **before** any ``extract_info``. Two URL forms of one video must collapse
to one 11-char id, else duplicate fetches + cache misses. Playlist-only /
channel / handle / search inputs are rejected with ``error_code: bad_url``.

Satisfies **Invariant 3**: ``canonicalize(canonicalize(x)) == canonicalize(x)``
(the output is a bare 11-char id, which is itself a valid input that maps to
itself).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from ytt.errors import BAD_URL, YttError

#: The canonical YouTube video id shape.
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Hosts we accept (after stripping www./m./music. prefixes).
_YOUTUBE_HOSTS = {"youtube.com", "youtube-nocookie.com"}
_SHORT_HOST = "youtu.be"

# Path prefixes that carry the id as the next path segment.
_PATH_ID_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/")

# Path prefixes that are definitively NOT a single video -> bad_url.
_REJECT_PREFIXES = ("/channel/", "/c/", "/user/", "/@", "/results", "/playlist", "/feed", "/hashtag/")

_LEADING_SUBDOMAIN_RE = re.compile(r"^(www\.|m\.|music\.|gaming\.)")


def _fail(url: str, why: str) -> "YttError":
    return YttError(BAD_URL, f"Not a single-video YouTube URL ({why}): {url!r}")


def canonicalize(url_or_id: str) -> str:
    """Return the canonical 11-char video id for any accepted YouTube URL/id.

    Raises :class:`ytt.errors.YttError` with ``error_code == "bad_url"`` for
    playlist-only / channel / handle / search inputs or anything unrecognized.
    """
    raw = (url_or_id or "").strip()
    if not raw:
        raise _fail(url_or_id, "empty")

    # Bare 11-char id passes straight through (this is what makes canon idempotent).
    if VIDEO_ID_RE.match(raw):
        return raw

    # Ensure urlparse sees a netloc even for scheme-less inputs like "youtu.be/ID".
    parsed = urlparse(raw if "//" in raw else "https://" + raw)
    host = parsed.netloc.lower()
    # Defensive: never trust embedded credentials in the host.
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    host = _LEADING_SUBDOMAIN_RE.sub("", host)
    path = parsed.path or "/"
    query = parse_qs(parsed.query)

    if host == _SHORT_HOST:
        candidate = path.lstrip("/").split("/", 1)[0]
        return _validate(candidate, url_or_id)

    if host in _YOUTUBE_HOSTS:
        for prefix in _REJECT_PREFIXES:
            if path == prefix or path.startswith(prefix):
                raise _fail(url_or_id, "channel/playlist/search/handle")

        # watch?v=ID (list/t/pp ignored)
        if path in ("/watch", "/watch/"):
            vids = query.get("v")
            if not vids:
                raise _fail(url_or_id, "watch URL has no v= parameter")
            return _validate(vids[0], url_or_id)

        for prefix in _PATH_ID_PREFIXES:
            if path.startswith(prefix):
                candidate = path[len(prefix) :].split("/", 1)[0]
                return _validate(candidate, url_or_id)

        # /attribution_link?u=... or any other youtube.com path we don't map.
        raise _fail(url_or_id, "unrecognized youtube.com path")

    raise _fail(url_or_id, "not a youtube host")


def _validate(candidate: str, original: str) -> str:
    if VIDEO_ID_RE.match(candidate):
        return candidate
    raise _fail(original, f"extracted id {candidate!r} is not 11 valid chars")
