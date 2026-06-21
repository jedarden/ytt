"""Stable error taxonomy (plan: Fetch core / Error taxonomy).

Each ``error_code`` is a stable string surfaced to the model alongside a
verbatim-relayable ``message``. The yt-dlp seed string->code map is implemented
in Phase 2; this module defines the constant set and the mapping entrypoint.
"""

from __future__ import annotations

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

__all__ = [name for name in dir() if name.isupper()]
