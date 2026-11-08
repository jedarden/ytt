"""Outage recovery for the browser-primary caption fetch (bead ytt-2ab14314).

``ytt-browser`` is a separate pod, and two things happen to it routinely
(``docs/notes/browser-outage-recovery.md`` is the full specification):

- it **recycles itself every six hours** (``timeout 21600`` loop in
  ``browser-deployment.yml``) — a restart of the browser-server *process*
  inside a long-lived container;
- it can be briefly **unavailable** (pod restarting, not Ready yet) or can
  **crash / be OOM-killed** at any moment.

A fetch that is in flight when the server dies sees its WebSocket transport
drop: Playwright raises (``TargetClosedError`` / connection errors) from
whatever call was pending — the navigation, a player-script evaluate, or the
caption-response wait.  Those exceptions are not
:class:`~ytt.browser_fetch.BrowserInfraError`, so without this layer they
would escape the browser path as raw transport errors and the router in
:mod:`ytt.fetch` would never reach its yt-dlp fallback.

This module wraps :func:`ytt.browser_fetch.browser_fetch_transcript` with the
outage policy (spec: ``docs/notes/browser-outage-recovery.md``):

- **classify** — any non-video, non-infra exception raised by the transport is
  wrapped as ``BrowserInfraError``: ``connection_lost`` when it has the shape
  of a dropped sidecar connection, ``unclassified`` otherwise.  The router's
  contract — video-level errors are ``YttError``/``NoCaptionsError``,
  everything else on this path is ``BrowserInfraError`` — therefore holds for
  every failure, including crashes mid-capture.
- **retry** — failures whose reason says "the sidecar was unavailable or died
  mid-fetch" (``connect``, ``navigate``, ``player_script``,
  ``connection_lost``: the same outage seen at different fetch phases) are
  re-attempted whole, up to :data:`BROWSER_ATTEMPTS` attempts with a short
  escalating backoff.  A retry is a completely fresh fetch (new connect, new
  anonymous context, new page); there is no per-fetch state on the server to
  restore, so a retry is always safe.  A six-hour recycle window (~1 s) is
  bridged by the first retry; a bridged outage is invisible to the caller.
- **fall back** — when the attempts are exhausted (or the reason is not
  outage-shaped) the final ``BrowserInfraError`` propagates, and the router
  falls back to yt-dlp naming the browser failure.

Deliberately **not** retried: ``timeout`` (the whole-fetch budget is already
spent — a retry would only re-burn it), ``empty_body`` / ``rate_limited`` /
``playability_blocked`` (verdicts about this egress/token that a retry seconds
later does not change), ``no_request`` / ``no_player_response`` /
``bad_body`` / ``empty_parse`` (player/video behaviour, not availability) and
``unclassified`` (unknown ⇒ terminal, so bugs fail loudly once, not three
times).  Video-level errors are re-raised untouched on the first attempt.

Metrics note: each attempt is one ``browser_fetch_transcript`` call, so
``ytt_browser_fetch_total`` counts **attempts**.  A bridged outage therefore
shows as ``infra_error +1, ok +1`` with no user-visible failure — retries are
observable in Prometheus and in the ``browser fetch attempt N/3 failed`` log
lines, which is the point: a hide-the-outage policy would hide the drift too.
The canary's browser leg calls ``browser_fetch_transcript`` directly and
measures the *single-attempt* availability on purpose; this policy belongs to
the serving path only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ytt.browser_fetch import BrowserInfraError
from ytt.errors import NoCaptionsError, YttError

if TYPE_CHECKING:  # pragma: no cover
    from ytt.config import Settings

    from ytt.fetch import FetchResult

log = logging.getLogger(__name__)

#: Total attempts for an outage-shaped browser failure: the initial try plus
#: two retries.  Three covers a six-hour recycle (~1 s window), a slow pod
#: restart (~5–10 s with the backoff below) and one crash-then-crash, while
#: bounding the added worst-case wall time to
#: ``(BROWSER_ATTEMPTS - 1) × YTT_BROWSER_TIMEOUT_SEC`` beyond the timeouts
#: the single-attempt path already had.
BROWSER_ATTEMPTS = 3

#: Backoff before retry *n* (1-based) is ``BROWSER_RETRY_BACKOFF_SEC * n``
#: seconds — 1 s before the first retry, 2 s before the second.  Short on
#: purpose: the outage this bridges is the server's own restart, which is
#: over in seconds; anything longer is a real outage the yt-dlp fallback
#: should take over.
BROWSER_RETRY_BACKOFF_SEC = 1.0

#: ``BrowserInfraError.reason`` values that mean "the sidecar was unavailable
#: or died under this fetch" — whichever phase the fetch was in when that
#: happened (connect / navigation / player script / mid-capture).  The whole
#: fetch is re-attempted; see the module docstring for what is excluded and
#: why.
RETRYABLE_REASONS = frozenset(
    {"connect", "navigate", "player_script", "connection_lost"}
)

#: Reason label for an in-flight loss of the browser server (crash or
#: recycle during navigation or caption capture).
CONNECTION_LOST = "connection_lost"
#: Reason label for a non-video exception that is neither a known infra
#: failure nor recognisable as a dropped connection.
UNCLASSIFIED = "unclassified"

#: Message fragments Playwright uses when the sidecar connection dies under
#: an in-flight call (class names carry "Closed"; these cover the shapes
#: whose class name is generic).
_DISCONNECT_MESSAGE_SEEDS = (
    "has been closed",
    "connection closed",
    "connection terminated",
)


def looks_like_disconnect(exc: BaseException) -> bool:
    """Whether *exc* is the shape of a dropped browser-server connection.

    Playwright signals a recycled/crashed sidecar with
    ``TargetClosedError`` / ``ConnectionClosedError`` (or a transport message
    like ``Connection closed``); the stdlib raises :class:`ConnectionError`
    for refused/reset TCP.  Classified by shape, not identity: the test fakes
    are local classes with the same names.
    """
    if isinstance(exc, ConnectionError):
        return True
    if "closed" in type(exc).__name__.lower():
        return True
    text = str(exc).lower()
    return any(seed in text for seed in _DISCONNECT_MESSAGE_SEEDS)


def classify_raw_failure(exc: BaseException) -> str:
    """Reason label for a raw exception that escaped the transport fetch."""
    return CONNECTION_LOST if looks_like_disconnect(exc) else UNCLASSIFIED


async def fetch_with_outage_recovery(
    video_id: str, lang: str | None, settings: "Settings"
) -> "FetchResult":
    """``browser_fetch_transcript`` with the outage policy (module docstring).

    Returns the first successful attempt.  Raises the final
    :class:`~ytt.browser_fetch.BrowserInfraError` when the attempts are
    exhausted or the reason is not outage-shaped, and re-raises video-level
    :class:`~ytt.errors.YttError` / :class:`~ytt.errors.NoCaptionsError`
    untouched.  Never lets a raw transport exception escape.
    """
    from ytt.browser_fetch import browser_fetch_transcript

    attempt = 1
    while True:
        if attempt > 1:
            delay = BROWSER_RETRY_BACKOFF_SEC * (attempt - 1)
            await asyncio.sleep(delay)
        try:
            return await browser_fetch_transcript(video_id, lang, settings)
        except (NoCaptionsError, YttError):
            # Authoritative video-level answer: yt-dlp would only repeat it.
            raise
        except BrowserInfraError as exc:
            failure = exc
        except Exception as exc:
            # A raw Playwright/transport escape: the sidecar died under an
            # in-flight call (or an unexpected bug).  Wrap it so the router's
            # infra contract holds for every failure of this path.
            failure = BrowserInfraError(
                f"browser path died mid-fetch: {type(exc).__name__}: {exc}",
                reason=classify_raw_failure(exc),
            )
            log.warning(
                "browser path raised %s (attempt %d/%d); "
                "classified as infra failure (%s)",
                type(exc).__name__,
                attempt,
                BROWSER_ATTEMPTS,
                failure.reason,
            )
        if failure.reason not in RETRYABLE_REASONS or attempt >= BROWSER_ATTEMPTS:
            raise failure
        delay = BROWSER_RETRY_BACKOFF_SEC * attempt
        log.warning(
            "browser fetch attempt %d/%d failed (%s: %s); retrying in %.1fs",
            attempt,
            BROWSER_ATTEMPTS,
            failure.reason,
            failure,
            delay,
        )
        attempt += 1
