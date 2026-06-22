"""Standalone residential-egress canary (plan: §Observability / Canary).

A long-running probe that calls yt-dlp directly (same code path as
:mod:`ytt.fetch`, NOT via the HTTP tool endpoint — bypasses OAuth) against a
fixed internal video list, exposing ``ytt_canary_*`` metrics on a Prometheus
``/metrics`` endpoint.  Runs as its own Deployment (no K8s Jobs — plan
§Infrastructure constraint).

Metrics emitted (plan §Observability / Canary):

    ytt_canary_last_success_timestamp_seconds  — Gauge (updated on each success)
    ytt_canary_failures_total                  — Counter (incremented on failure)

PrometheusRule: fire ``YttCanaryFailed`` if
    ``time() - ytt_canary_last_success_timestamp_seconds > 1800``
(3 consecutive 10-min probes missed).

The canary Deployment is separate from the main ytt server; it has its own
``/metrics`` port (8081 by default) scraped by a ``ServiceMonitor`` referencing
``app=ytt-canary``.

Usage (within the canary Deployment):
    CMD ["ytt", "canary"]   — or directly: python -m ytt.canary
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from prometheus_client import REGISTRY, start_http_server

from ytt.observability import (
    ytt_canary_failures_total,
    ytt_canary_last_success_timestamp_seconds,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixed internal video list (plan: §Canary — "fixed internal video list")
# These videos must be stable (old, non-livestream, always-captioned).
# Update if a video is deleted or region-blocked.
# ---------------------------------------------------------------------------

CANARY_VIDEO_IDS: tuple[str, ...] = (
    "jNQXAC9IVRw",  # "Me at the zoo" — YouTube's first video (very stable)
    "dQw4w9WgXcQ",  # Rick Astley "Never Gonna Give You Up" (very stable)
)

# ---------------------------------------------------------------------------
# Probe function
# ---------------------------------------------------------------------------


def _probe_one(video_id: str, settings: Any) -> bool:
    """Run a single yt-dlp caption probe against ``video_id``.

    Returns ``True`` on success (captions extracted), ``False`` on any error.
    Intentionally synchronous — called via ``asyncio.to_thread`` from the probe loop.
    """
    try:
        from ytt.fetch import YDL_BASE_OPTS, get_available_langs
        import yt_dlp

        # Reuse the same yt-dlp options as the main fetch path (plan §Canary:
        # "calls yt-dlp directly (same code path as fetch.py)").
        opts = dict(YDL_BASE_OPTS)
        opts.update(
            {
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitlesformat": "json3",
                "quiet": True,
                "no_warnings": True,
            }
        )

        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Success if at least one caption track exists.
        if info:
            subs = info.get("subtitles") or {}
            auto = info.get("automatic_captions") or {}
            if subs or auto:
                return True

        log.warning("Canary: no captions found for %s", video_id)
        return False

    except Exception as exc:  # pragma: no cover — integration path
        log.error("Canary probe error for %s: %s", video_id, exc)
        return False


# ---------------------------------------------------------------------------
# Probe loop
# ---------------------------------------------------------------------------


async def run_probe_loop(interval_sec: int = 600) -> None:
    """Async probe loop — runs forever (plan: long-running Deployment).

    Every ``interval_sec`` seconds, probe each video in ``CANARY_VIDEO_IDS``.
    On any success: update ``ytt_canary_last_success_timestamp_seconds``.
    On all failures: increment ``ytt_canary_failures_total``.
    """
    from ytt.config import get_settings

    settings = get_settings()

    log.info(
        "Canary probe loop starting (interval=%ds, videos=%s)",
        interval_sec,
        CANARY_VIDEO_IDS,
    )

    while True:
        success = False
        for video_id in CANARY_VIDEO_IDS:
            ok = await asyncio.to_thread(_probe_one, video_id, settings)
            if ok:
                ytt_canary_last_success_timestamp_seconds.set(time.time())
                success = True
                log.info("Canary probe succeeded for %s", video_id)
                break

        if not success:
            ytt_canary_failures_total.inc()
            log.error("Canary probe failed for all videos: %s", CANARY_VIDEO_IDS)

        await asyncio.sleep(interval_sec)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Start the canary probe loop with a Prometheus metrics server on :8081."""
    import logging as _logging

    from ytt.config import get_settings

    _logging.basicConfig(level=_logging.INFO)

    settings = get_settings()
    interval = settings.canary_interval_sec

    # Start the prometheus metrics HTTP server on a dedicated port (8081).
    # The canary Deployment's ServiceMonitor scrapes this port.
    start_http_server(8081, registry=REGISTRY)
    log.info("Canary metrics server started on :8081")

    asyncio.run(run_probe_loop(interval_sec=interval))
    return 0  # pragma: no cover
