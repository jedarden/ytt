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


# --- error_code constants (the stable enum) ---------------------------------
BAD_URL = "bad_url"
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

# WhisperJob-internal / metric-only labels (NOT TranscriptResult error_codes):
NO_CAPTIONS_ASR_STARTED = "no_captions_asr_started"
NO_CAPTIONS_ASR_FAILED = "no_captions_asr_failed"

__all__ = ["YttError"] + [name for name in dir() if name.isupper()]
