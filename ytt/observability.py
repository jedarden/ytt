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
        authorization, transcript, audio_path, proxy_url, allowed_subjects,
        client_secret, oauth_client_secret, jwt_signing_secret,
        jwt_signing_key.
    Blocked URL values: any field whose string value contains a credential-
        bearing URL (``user:pass@host`` — Webshare etc.) is sanitized via
        :func:`redact_credentials` — userinfo stripped, host:port kept —
        whether the URL is the whole value or embedded in a sentence.

Free-text error messages are NOT structured fields and bypass this processor —
upstream exception strings that may quote the credentialed proxy URL must be
passed through :func:`redact_credentials` before they become a
``YttError`` message or a log argument.

Usage
-----
Import this module once at startup; call ``configure_logging()`` before any log
calls.  Use ``get_logger(__name__)`` (structlog) in each module.
"""

from __future__ import annotations

import re
from typing import Any

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

#: Canonical fetch-outcome label values, pre-registered below so the counter
#: exports a 0 series per outcome from process startup. Mirrors the stable
#: taxonomy in :mod:`ytt.errors` (``ip_blocked``, ``no_captions_asr_started``).
FETCH_BLOCK_OUTCOMES: tuple[str, ...] = (
    "ok",
    "ip_blocked",
    "no_captions_asr_started",
)

# prometheus_client exports no series for a labelled Counter until its first
# child exists — so a fresh process carried no ``ytt_fetch_blocks_total``
# series at all, and "never blocked" was indistinguishable from "metric not
# registered" on the canary pod (docs/notes/canary-first-fetch.md).
# Pre-create one child per canonical outcome: ``.labels()`` instantiates the
# child at 0 and records no fetch event.
for _fetch_outcome in FETCH_BLOCK_OUTCOMES:
    ytt_fetch_blocks_total.labels(outcome=_fetch_outcome)

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

#: Field names (lowercase) whose values are always redacted. The secret-named
#: entries cover the OAuth configuration credential pair's shapes
#: (``client_secret``/``oauth_client_secret`` — ``YTT_OAUTH_CLIENT_SECRET`` —
#: plus the FastMCP signing key under ``jwt_signing_secret``/``jwt_signing_key``):
#: a plain secret string matches no URL pattern, so only the *name* rule can
#: stop it from reaching a rendered log event.
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
        "client_secret",
        "oauth_client_secret",
        "jwt_signing_secret",
        "jwt_signing_key",
    }
)

#: Regex that matches credential-bearing URLs (contains ``user:pass@``).
_CREDENTIAL_URL_RE = re.compile(r"https?://[^@\s]+@")

#: A credential-bearing URL embedded in free text: ``scheme://user[:pass]@host…``.
#: The userinfo run may not contain whitespace, ``/`` or ``@``; the host run
#: stops at whitespace or a common closer so surrounding punctuation in a
#: sentence survives. Matches any scheme — yt-dlp error strings quote the
#: configured proxy verbatim, and that proxy may be socks-shaped in future.
_CRED_URL_IN_TEXT_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)"
    r"(?P<userinfo>[^\s/@]+)"
    r"@"
    r"(?P<hostport>[^\s,;)\]]*)"
)


def redact_credentials(text: str) -> str:
    """Strip ``user:password@`` userinfo from any URL embedded in free text.

    yt-dlp exception strings (and httpx's) can quote the configured proxy URL
    verbatim — e.g. ``Unable to communicate with proxy
    http://user:pass@proxy.example.com:3128``. Those strings become
    :class:`~ytt.errors.YttError` messages, which are verbatim-relayable to
    MCP clients *and* logged; plan §Observability requires that Webshare
    credentials never reach logs, error messages, or the ``ip_blocked`` retry
    note. The structlog redaction filter only sees structured *fields*, so
    every code path that turns an upstream exception into a message string
    must run it through this helper first (``ytt.fetch``, ``ytt.whisper``,
    ``ytt.canary``, ``ytt.server``).

    ``"dial http://alice:secret@proxy.example.com:3128 failed"`` →
    ``"dial http://proxy.example.com:3128 failed"``. Text without a
    credential-bearing URL is returned unchanged.
    """
    if "@" not in text:
        return text
    return _CRED_URL_IN_TEXT_RE.sub(
        lambda m: f"{m.group('scheme')}{m.group('hostport')}", text
    )


def redaction_processor(
    logger: Any, method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor that redacts sensitive fields before rendering.

    Plan §Observability / Diagnostic hygiene:
    - Blocked field names: sub, email, token, secret, key, authorization,
      transcript, audio_path, proxy_url, allowed_subjects.
    - Any string value containing a credential-bearing URL is sanitized via
      :func:`redact_credentials` — the same free-text rule the fetch/whisper/
      canary boundaries apply, so a log argument quoting the configured
      proxy verbatim (``error=str(exc)`` on the startup egress probe and
      Whisper cleanup paths) renders with the userinfo stripped, whether the
      URL is the whole value or embedded in a sentence. Host and port stay
      for diagnosability.
    """
    for key in list(event_dict.keys()):
        if key.lower() in _REDACTED_FIELD_NAMES:
            event_dict[key] = "<redacted>"
            continue
        val = event_dict[key]
        if isinstance(val, str) and "@" in val and _CREDENTIAL_URL_RE.search(val):
            event_dict[key] = redact_credentials(val)
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

    ``redaction_processor`` runs **last** among the value-shaping processors —
    after ``format_exc_info``/``StackInfoRenderer`` — so the ``exception`` and
    ``stack_trace`` fields those produce (raw formatted tracebacks, whose
    exception lines can quote a credential-bearing ``YTT_WHISPER_URL`` or
    proxy verbatim) are scanned and sanitized too. Ahead of them it would
    only ever see the not-yet-rendered ``exc_info`` object and miss the leak.
    """
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redaction_processor,
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
