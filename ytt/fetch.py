"""Fetch core — yt-dlp caption/metadata extraction (plan: Fetch core).

In-process via the yt-dlp Python API (Unlicense).  Handles:

- No-cookies enforcement: ``cookiefile=None`` / ``cookiesfrombrowser=None`` are
  always explicit in the options dict so no yt-dlp config file in the image can
  silently activate cookie extraction (plan §Security, standing constraint).
- Player-client override ``tv/web_embedded/mweb``: avoids the PoToken
  requirement (plan §Fetch core step 1).
- json3 caption extraction, rolling-caption dedup (delegated to
  :mod:`ytt.parse_json3`), metadata, language selection.
- Error taxonomy: stable ``error_code`` constants mapped from yt-dlp seed
  strings (plan §Error taxonomy).  ``ip_blocked`` triggers an async proxy retry
  when ``YTT_PROXY_URL`` is set (plan §ip_blocked).
- Async-safe: the blocking ``extract_info`` call runs in a thread via
  ``asyncio.to_thread``, wrapped in ``asyncio.wait_for`` with the configured
  timeout (plan §Concurrency, ``YTT_EXTRACT_TIMEOUT_SEC``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yt_dlp
import yt_dlp.utils

from ytt import errors
from ytt.errors import YttError
from ytt.models import Segment
from ytt.parse_json3 import parse_json3

if TYPE_CHECKING:
    from ytt.config import Settings


# ---------------------------------------------------------------------------
# Production yt-dlp options (plan §Security + §Fetch core)
# ---------------------------------------------------------------------------

#: No-cookies enforcement — MUST be present and falsy in every YDL opts dict.
#: Explicit ``None`` blocks any yt-dlp config file from activating cookie extraction.
YDL_NO_COOKIES: dict = {
    "cookiefile": None,
    "cookiesfrombrowser": None,
}

#: Player-client list that avoids the PoToken requirement (plan §Fetch core step 1).
YDL_EXTRACTOR_ARGS: dict = {
    "extractor_args": {"youtube": {"player_client": ["tv", "web_embedded", "mweb"]}},
}

#: Caption extraction flags.
YDL_CAPTION_FLAGS: dict = {
    "skip_download": True,
    "writesubtitles": True,
    "writeautomaticsub": True,
    "subtitlesformat": "json3",
    # YouTube now applies Widevine/PlayReady DRM (via SABR streaming) to many
    # ordinary uploads. By default yt-dlp ABORTS extraction with "This video is
    # DRM protected" (see yt_dlp/extractor/youtube/_video.py: it calls
    # report_drm() unless allow_unplayable_formats is set). That abort also
    # kills CAPTION retrieval — but caption tracks are NOT DRM-encrypted, only
    # the audio/video formats are, and the caption path never downloads media
    # (skip_download=True). Setting this lets extract_info() proceed past the
    # DRM check and still populate subtitles/automatic_captions, so we can pull
    # the (unencrypted) caption text. Whisper fallback still can't fetch DRM
    # audio, but that path is only reached when a video genuinely has no
    # captions. Without this, EVERY DRM'd video (incl. normal ones like TED
    # talks) falsely reported "no captions" → doomed Whisper job.
    "allow_unplayable_formats": True,
    # Even past the DRM check, extract_info() still runs format SELECTION (yes,
    # even with download=False + skip_download=True), and when every format is
    # DRM/unplayable it raises "Requested format is not available" (raised in
    # YoutubeDL.py only when ignore_no_formats_error is False). We want subtitle
    # tracks, never a media format, so tell yt-dlp to warn-and-continue instead
    # of erroring on format selection — it then returns the info dict with
    # subtitles/automatic_captions populated. yt-dlp's documented "extract
    # metadata even if the video isn't downloadable" path.
    "ignore_no_formats_error": True,
}

#: Base options merged into every YDL() call in the caption path.
#: Unit tests assert ``cookiefile`` and ``cookiesfrombrowser`` are both present
#: and falsy (plan §Security, §No YouTube cookies test).
YDL_BASE_OPTS: dict = {
    **YDL_NO_COOKIES,
    **YDL_EXTRACTOR_ARGS,
    **YDL_CAPTION_FLAGS,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "ignoreerrors": False,
}


# ---------------------------------------------------------------------------
# Error taxonomy — seed string → error_code (plan §Error taxonomy)
# ---------------------------------------------------------------------------
#: Order matters: more specific patterns first.  This list is a maintenance
#: point pinned to ``yt-dlp==2025.5.22``; update on version bumps.
SEED_MAP: list[tuple[str, str]] = [
    ("Private video", errors.PRIVATE),
    ("members-only", errors.MEMBERS_ONLY),
    ("Sign in to confirm your age", errors.AGE_RESTRICTED),
    ("not available in your country", errors.REGION_BLOCKED),
    ("This live event will begin", errors.IS_LIVESTREAM),
    ("is live", errors.IS_LIVESTREAM),
    ("Video unavailable", errors.UNAVAILABLE),
    ("has been removed", errors.UNAVAILABLE),
    ("HTTP Error 429", errors.RATE_LIMITED),
    ("Sign in to confirm you're not a bot", errors.IP_BLOCKED),
    ("HTTP Error 403", errors.IP_BLOCKED),
    ("Did not get any data blocks", errors.IP_BLOCKED),
    ("audio_too_large", errors.TOO_LONG_FOR_ASR),
]


def classify_ydl_error(exc_msg: str) -> str:
    """Map a yt-dlp exception message to a stable :mod:`ytt.errors` error_code.

    Falls back to :data:`~ytt.errors.EMPTY_BODY` for unrecognized messages —
    a distinct metric label so silent breakage is visible (plan §Error taxonomy).
    """
    for seed, code in SEED_MAP:
        if seed in exc_msg:
            return code
    return errors.EMPTY_BODY


# ---------------------------------------------------------------------------
# Language / track utilities (plan §Language selection)
# ---------------------------------------------------------------------------

def _normalize_lang_key(key: str) -> str:
    """Strip ``.auto`` suffix and lowercase a yt-dlp caption dict key."""
    return key.removesuffix(".auto").lower()


def get_available_langs(info: dict) -> list[str]:
    """Return a sorted, deduplicated BCP-47 tag list from all caption tracks.

    Merges keys from ``info['subtitles']`` and ``info['automatic_captions']``,
    strips ``.auto`` suffixes, lowercases, and de-duplicates (plan §Language
    selection — ``available_langs`` format).
    """
    keys: set[str] = set()
    for key in (info.get("subtitles") or {}):
        keys.add(_normalize_lang_key(key))
    for key in (info.get("automatic_captions") or {}):
        keys.add(_normalize_lang_key(key))
    return sorted(keys)


def _find_json3_url(track_list: list[dict] | None) -> str | None:
    """Return the first ``ext='json3'`` URL in a caption format list, or ``None``."""
    for fmt in (track_list or []):
        if fmt.get("ext") == "json3":
            return fmt.get("url")
    return None


def _select_track(
    info: dict,
    lang: str | None,
) -> tuple[str, str, str, str | None]:
    """Select the best json3 caption track URL.

    Language priority (plan §Language selection):
    - ``lang`` given: manual[lang] → auto[lang] → manual[default] → auto[default] → any.
    - ``lang`` omitted: original/default → English → any.

    Returns
    -------
    (url, kind, served_lang, fallback_message)
        ``kind`` is ``"asr"`` for automatic-caption tracks (rolling; must be
        deduped by :func:`~ytt.parse_json3.parse_json3`), or ``""`` for manual
        tracks.  ``fallback_message`` is non-``None`` when the requested language
        was unavailable and a fallback was chosen.
    """
    subtitles: dict[str, list] = info.get("subtitles") or {}
    auto_captions: dict[str, list] = info.get("automatic_captions") or {}

    def try_manual(k: str) -> str | None:
        return _find_json3_url(subtitles.get(k))

    def try_auto(k: str) -> str | None:
        return _find_json3_url(auto_captions.get(k))

    if lang is not None:
        # Primary choices: exact match, manual first
        url = try_manual(lang)
        if url:
            return url, "", lang, None
        url = try_auto(lang)
        if url:
            return url, "asr", lang, None

        # Fallback: default/original language for this video
        default_lang = info.get("language") or ""
        if default_lang and default_lang != lang:
            url = try_manual(default_lang)
            if url:
                return url, "", default_lang, (
                    f"requested {lang!r} unavailable; served {default_lang!r}"
                )
            url = try_auto(default_lang)
            if url:
                return url, "asr", default_lang, (
                    f"requested {lang!r} unavailable; served {default_lang!r}"
                )

        # Fallback: any available manual track
        for k, fmts in subtitles.items():
            url = _find_json3_url(fmts)
            if url:
                served = _normalize_lang_key(k)
                return url, "", served, (
                    f"requested {lang!r} unavailable; served {served!r}"
                )
        # Fallback: any available auto track
        for k, fmts in auto_captions.items():
            url = _find_json3_url(fmts)
            if url:
                served = _normalize_lang_key(k)
                return url, "asr", served, (
                    f"requested {lang!r} unavailable; served {served!r}"
                )

        raise YttError(
            errors.EMPTY_BODY,
            f"No captions available for {lang!r}. "
            f"Available: {get_available_langs(info)}",
        )

    else:
        # lang=None: original/default → English → any
        default_lang = info.get("language") or ""
        candidates = []
        if default_lang:
            candidates.append(default_lang)
        if "en" not in candidates:
            candidates.append("en")

        for candidate in candidates:
            url = try_manual(candidate)
            if url:
                return url, "", candidate, None
        for candidate in candidates:
            url = try_auto(candidate)
            if url:
                return url, "asr", candidate, None

        # any available manual
        for k, fmts in subtitles.items():
            url = _find_json3_url(fmts)
            if url:
                return url, "", _normalize_lang_key(k), None
        # any available auto
        for k, fmts in auto_captions.items():
            url = _find_json3_url(fmts)
            if url:
                return url, "asr", _normalize_lang_key(k), None

        raise YttError(errors.EMPTY_BODY, "No captions available.")


# ---------------------------------------------------------------------------
# FetchResult (internal result envelope)
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    """Result of a successful caption fetch (plan §Fetch core return contract)."""

    segments: list[Segment]
    source: str  # "caption_manual" | "caption_auto"
    served_lang: str
    requested_lang: str | None
    available_langs: list[str]
    title: str | None = None
    channel: str | None = None
    duration_sec: float | None = None
    published: str | None = None
    message: str | None = None  # language-fallback advisory


# ---------------------------------------------------------------------------
# Blocking fetch core (call via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _do_fetch(
    video_id: str,
    lang: str | None,
    settings: "Settings",
    proxy: str | None,
) -> FetchResult:
    """Synchronous yt-dlp extraction; always call via :func:`fetch_transcript`.

    Constructs a ``YoutubeDL`` context with no-cookies, player-client override,
    and optional proxy; extracts info, selects the caption track, fetches the
    json3 body in-process, and delegates to :func:`~ytt.parse_json3.parse_json3`.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts: dict = dict(YDL_BASE_OPTS)
    if proxy:
        opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise YttError(errors.EMPTY_BODY, "yt-dlp returned no info.")

            available = get_available_langs(info)
            track_url, kind, served_lang, fallback_msg = _select_track(info, lang)

            # Fetch json3 body in-process (avoids a second YDL construction)
            resp = ydl.urlopen(track_url)
            raw_bytes = resp.read()
    except YttError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        code = classify_ydl_error(str(exc))
        raise YttError(code, str(exc)) from exc
    except yt_dlp.utils.ExtractorError as exc:
        code = classify_ydl_error(str(exc))
        raise YttError(code, str(exc)) from exc

    raw: dict = json.loads(raw_bytes.decode("utf-8"))
    events: list[dict] = raw.get("events", [])
    segments = parse_json3(events, kind=kind)

    # Metadata (plan §Fetch core step 1)
    title: str | None = info.get("title")
    channel: str | None = info.get("channel") or info.get("uploader")
    raw_dur = info.get("duration")
    duration_sec = float(raw_dur) if raw_dur is not None else None
    published: str | None = info.get("upload_date")  # 'YYYYMMDD' string

    source = "caption_auto" if kind == "asr" else "caption_manual"

    return FetchResult(
        segments=segments,
        source=source,
        served_lang=served_lang,
        requested_lang=lang if (lang is not None and lang != served_lang) else None,
        available_langs=available,
        title=title,
        channel=channel,
        duration_sec=duration_sec,
        published=published,
        message=fallback_msg,
    )


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------

async def fetch_transcript(
    video_id: str,
    lang: str | None,
    settings: "Settings",
) -> FetchResult:
    """Async caption fetch with timeout + ip_blocked proxy retry.

    Wraps :func:`_do_fetch` in ``asyncio.to_thread`` + ``asyncio.wait_for``
    (timeout = ``YTT_EXTRACT_TIMEOUT_SEC``).  On ``ip_blocked`` and
    ``YTT_PROXY_URL`` set, retries once with the proxy (plan §ip_blocked).
    """
    async def _run(proxy: str | None) -> FetchResult:
        return await asyncio.wait_for(
            asyncio.to_thread(_do_fetch, video_id, lang, settings, proxy),
            timeout=float(settings.extract_timeout_sec),
        )

    try:
        return await _run(proxy=None)
    except asyncio.TimeoutError:
        raise YttError(
            errors.RATE_LIMITED,
            f"Extraction timed out after {settings.extract_timeout_sec}s "
            "(possible silent hang; retrying may help).",
        )
    except YttError as exc:
        if exc.error_code == errors.IP_BLOCKED and settings.proxy_url:
            # Retry once through the configured proxy (plan §ip_blocked proxy retry)
            try:
                return await _run(proxy=settings.proxy_url)
            except asyncio.TimeoutError:
                raise YttError(
                    errors.RATE_LIMITED,
                    f"Extraction timed out after {settings.extract_timeout_sec}s "
                    "(proxy retry also timed out).",
                )
            except YttError as retry_exc:
                new_msg = retry_exc.message + " (proxy retry also failed)"
                raise YttError(retry_exc.error_code, new_msg) from retry_exc
        raise
