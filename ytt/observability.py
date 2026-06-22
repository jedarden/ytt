"""Observability — metrics + structured logging (plan: §Observability).

Prometheus metrics (the exact label sets from the plan), structlog JSON
to stdout with a redaction filter (tokens / subject list / transcript bodies /
credential-bearing URLs never logged).

Metrics (all registered against the default CollectorRegistry):

    ytt_fetch_blocks_total{outcome}      — Counter
    ytt_fetch_empty_body_total           — Counter
    ytt_whisper_errors_total{reason}     — Counter
    ytt_whisper_job_seconds              — Histogram
    ytt_cache_bytes                      — Gauge
    ytt_cache_evictions_total            — Counter
    ytt_queue_depth                      — Gauge
    ytt_rate_limited_total{subject_hash} — Counter
    ytt_egress_is_residential            — Gauge

Logging:

The ``configure_logging()`` call sets up structlog for JSON-to-stdout output.
The ``RedactionProcessor`` removes sensitive fields before any log record reaches
a renderer:

    Blocked field names (case-insensitive): sub, email, token, secret, key,
        authorization, transcript, audio_path, proxy_url, allowed_subjects.
    Blocked URL values: any field whose string value contains ``@`` (credential-
        bearing URL — Webshare etc.) is sanitized to ``<redacted-url>``.

Usage
-----
Import this module once at startup; call ``configure_logging()`` before any log
calls.  Use ``get_logger(__name__)`` (structlog) in each module.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import structlog
from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Prometheus metrics (plan: §Observability)
# ---------------------------------------------------------------------------

#: Outcome label on fetch events (error_code or "ok").
ytt_fetch_blocks_total = Counter(
    "ytt_fetch_blocks_total",
    "Total yt-dlp caption fetch attempts, labelled by outcome.",
    ["outcome"],
)

#: Separate counter for empty/unrecognized bodies (distinct metric so silent
#: breakage is visible — plan §Fetch core error taxonomy).
ytt_fetch_empty_body_total = Counter(
    "ytt_fetch_empty_body_total",
    "Total empty-body (unrecognized yt-dlp error) fetch events.",
)

#: Whisper transcription errors.
ytt_whisper_errors_total = Counter(
    "ytt_whisper_errors_total",
    "Total Whisper ASR errors, labelled by reason (error_code).",
    ["reason"],
)

#: Histogram of completed Whisper job wall-clock durations (seconds).
ytt_whisper_job_seconds = Histogram(
    "ytt_whisper_job_seconds",
    "Wall-clock seconds per completed Whisper ASR job.",
    buckets=[30, 60, 120, 300, 600, 1200, 1800, 2880],
)

#: Current total bytes in the transcript cache.
ytt_cache_bytes = Gauge(
    "ytt_cache_bytes",
    "Total bytes currently stored in the transcript cache.",
)

#: Cumulative LRU evictions.
ytt_cache_evictions_total = Counter(
    "ytt_cache_evictions_total",
    "Total cache units evicted (whole txt+json pairs).",
)

#: Current depth of the bounded fetch queue (waiting requests).
ytt_queue_depth = Gauge(
    "ytt_queue_depth",
    "Current number of requests waiting in the bounded fetch queue.",
)

#: Per-subject rate-limit events — subject hash (first 8 chars of sha256).
ytt_rate_limited_total = Counter(
    "ytt_rate_limited_total",
    "Total requests rejected by the per-subject rate limiter, by subject hash.",
    ["subject_hash"],
)

#: 1 when the egress IP is non-datacenter (residential), 0 otherwise.
ytt_egress_is_residential = Gauge(
    "ytt_egress_is_residential",
    "1 if the current egress IP is classified as residential, 0 if datacenter.",
)

# ---------------------------------------------------------------------------
# Canary metrics (consumed by the standalone canary Deployment — plan §Canary)
# ---------------------------------------------------------------------------

#: Unix timestamp of the last successful canary probe.
ytt_canary_last_success_timestamp_seconds = Gauge(
    "ytt_canary_last_success_timestamp_seconds",
    "Unix timestamp of the last successful canary yt-dlp probe.",
)

#: Cumulative canary probe failures.
ytt_canary_failures_total = Counter(
    "ytt_canary_failures_total",
    "Total canary yt-dlp probe failures.",
)

# ---------------------------------------------------------------------------
# Structlog redaction filter
# ---------------------------------------------------------------------------

#: Field names (lowercase) whose values are always redacted.
_REDACTED_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "sub",
        "email",
        "token",
        "secret",
        "key",
        "authorization",
        "transcript",
        "audio_path",
        "proxy_url",
        "allowed_subjects",
    }
)

#: Regex that matches credential-bearing URLs (contains ``user:pass@``).
_CREDENTIAL_URL_RE = re.compile(r"https?://[^@\s]+@")


def _sanitize_url(value: str) -> str:
    """Strip credentials from a URL string.

    ``http://user:pass@host:1234/path`` → ``http://host:1234/path``.
    Returns the original value unchanged if it is not a credential-bearing URL.
    """
    if "@" not in value:
        return value
    try:
        parsed = urlparse(value)
        # Replace netloc with host only (drop username:password)
        sanitized = urlunparse(parsed._replace(netloc=parsed.hostname or parsed.netloc))
        return sanitized
    except Exception:
        return "<redacted-url>"


def redaction_processor(
    logger: Any, method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor that redacts sensitive fields before rendering.

    Plan §Observability / Diagnostic hygiene:
    - Blocked field names: sub, email, token, secret, key, authorization,
      transcript, audio_path, proxy_url, allowed_subjects.
    - Any string value containing '@' is sanitized (credential-bearing URL).
    """
    for key in list(event_dict.keys()):
        if key.lower() in _REDACTED_FIELD_NAMES:
            event_dict[key] = "<redacted>"
            continue
        val = event_dict[key]
        if isinstance(val, str) and "@" in val and _CREDENTIAL_URL_RE.search(val):
            event_dict[key] = _sanitize_url(val)
    return event_dict


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    """Configure structlog for JSON-to-stdout output with redaction.

    Call once at startup (before any log calls).  Subsequent calls are
    idempotent (structlog checks if it is already configured).

    Uses ``PrintLoggerFactory`` (direct stdout, no stdlib intermediary) so the
    redaction processor runs unconditionally on all structured log events.
    ``structlog.stdlib.add_logger_name`` is intentionally excluded — it expects
    a stdlib ``logging.Logger`` and would fail with ``PrintLogger``.  The
    logger name is instead bound at get-logger time via ``structlog.get_logger(name)``.
    """
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redaction_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),  # pass all levels
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,  # allow reconfiguration in tests
    )


def get_logger(name: str) -> Any:
    """Return a structlog logger bound with ``name``.

    Convenience wrapper so modules can do ``log = observability.get_logger(__name__)``.
    """
    return structlog.get_logger(name)
