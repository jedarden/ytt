"""Per-subject rate limiting + bounded queue (plan: Concurrency / rate limit).

Hand-rolled in-process token bucket (``YTT_RATE_LIMIT_PER_MIN``) + per-subject
Whisper quota (``YTT_WHISPER_JOBS_PER_HOUR``); bounded queue in front of the
fetch semaphore returns 429 + Retry-After when full. Cache hits do NOT consume
the bucket. Implemented in Phase 3/5. Scaffold stub.
"""

from __future__ import annotations
