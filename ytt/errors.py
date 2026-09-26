"""Stable error taxonomy (plan: Fetch core / Error taxonomy).

Each ``error_code`` is a stable string surfaced to the model alongside a
verbatim-relayable ``message``. The yt-dlp seed string->code map is implemented
in Phase 2; this module defines the constant set and the mapping entrypoint.
"""

from __future__ import annotations


class YttError(Exception):
    """Carries a stable ``error_code`` plus a verbatim-relayable ``message``.

    The whole error taxonomy raises this; callers map ``error_code``/``message``
    straight into a ``TranscriptResult`` ``status="error"``. The yt-dlp
    string->code mapping is built in Phase 2 (:mod:`ytt.fetch`).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class NoCaptionsError(YttError):
    """``empty_body`` raised specifically because no caption track exists.

    yt-dlp metadata (``extract_info``) was retrieved successfully — the video
    is real and fetchable — it simply has no usable captions, so the caller
    (``ytt.server``) falls back to Whisper ASR. Carries the video's
    ``duration_sec`` when known so the ``MAX_ASR_DURATION_SEC`` cap can be
    enforced **at job creation** — before a job is registered, a quota slot
    charged, or any audio downloaded — rather than only at download time.

    A plain :class:`YttError` with ``error_code=empty_body`` (e.g. "yt-dlp
    returned no info") has ``duration_sec is None`` via :func:`getattr`, so
    consumers never need an isinstance check to stay safe.
    """

    def __init__(self, message: str, duration_sec: float | None = None) -> None:
        super().__init__(EMPTY_BODY, message)
        self.duration_sec = duration_sec


# --- error_code constants (the stable enum) ---------------------------------
BAD_URL = "bad_url"
# A URL extracted from a video's yt-dlp metadata (caption track, media
# format, or a redirect target reached from either) violated the derived-URL
# scheme/host allowlist — docs/notes/derived-url-policy.md. Unlike bad_url
# this is never caller-fixable: the caller's input was fine, the video's
# metadata tried to send our egress somewhere it must not go. Never routed
# into the Whisper ASR fallback — that would download audio from the very
# video whose metadata misbehaved.
BAD_METADATA_URL = "bad_metadata_url"
PRIVATE = "private"
MEMBERS_ONLY = "members_only"
AGE_RESTRICTED = "age_restricted"
REGION_BLOCKED = "region_blocked"
IS_LIVESTREAM = "is_livestream"
UNAVAILABLE = "unavailable"
RATE_LIMITED = "rate_limited"
IP_BLOCKED = "ip_blocked"
EMPTY_BODY = "empty_body"
TOO_LONG_FOR_ASR = "too_long_for_asr"
ASR_FAILED = "asr_failed"
# Logical errors emitted by get_transcript_job (not from yt-dlp string parsing):
NOT_FOUND = "not_found"
CURSOR_STALE = "cursor_stale"
# AuthZ errors:
FORBIDDEN = "forbidden"  # subject not in allowlist (403)

# WhisperJob-internal / metric-only labels (NOT TranscriptResult error_codes):
NO_CAPTIONS_ASR_STARTED = "no_captions_asr_started"
NO_CAPTIONS_ASR_FAILED = "no_captions_asr_failed"

__all__ = ["YttError"] + [name for name in dir() if name.isupper()]
