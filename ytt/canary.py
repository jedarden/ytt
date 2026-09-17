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

One-shot mode (lightweight Proof-Obligation canary):
    ytt canary --once               # egress report + caption fetch, JSON, exit 0/1
    python -m ytt.canary --once     # same, no CLI dependency
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from prometheus_client import REGISTRY, start_http_server

from ytt.observability import (
    redact_credentials,
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


def probe_once_detail(video_id: str, proxy: str | None = None) -> dict:
    """Run a single caption probe against ``video_id`` and classify the outcome.

    Returns a JSON-serializable dict::

        {"ok": bool, "outcome": str, "langs": list[str],
         "duration_sec": float, "via_proxy": bool, "error": str | None}

    ``outcome`` is ``"ok"`` or a stable :mod:`ytt.errors` error_code —
    ``ip_blocked`` when YouTube blocks the fetch, ``empty_body`` for an
    unrecognized failure (plan §Error taxonomy).  Synchronous; network-bound.

    ``via_proxy`` reports whether *this probe* dialed through a proxy —
    ``True`` only when *proxy* is passed (``ytt canary --once --via-proxy``).
    The default (direct) matches how the caption path actually reaches
    YouTube: direct first, proxy only on an ``ip_blocked`` retry.

    This is the fetch half of the one-shot canary (``ytt canary --once``) —
    the lightweight vehicle for proving residential egress from wherever the
    command runs (in-cluster one-shot, ``kubectl exec``, or an Argo step).
    With ``--via-proxy`` it becomes the end-to-end check that the configured
    proxy actually carries YouTube traffic (``docs/notes/proxy-egress.md``).
    """
    started = time.monotonic()
    try:
        from ytt.fetch import YDL_BASE_OPTS, classify_ydl_error, get_available_langs
        import yt_dlp

        # Same yt-dlp options as the main fetch path (plan §Canary:
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
        if proxy:
            opts["proxy"] = proxy

        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

    except Exception as exc:
        duration = round(time.monotonic() - started, 2)
        outcome = classify_ydl_error(str(exc))
        # The error string may quote the (credentialed) proxy URL — sanitize
        # before it lands in the printed/JSON report or the logs.
        error_text = redact_credentials(str(exc))
        log.error(
            "Canary probe error for %s: %s (outcome=%s)", video_id, error_text, outcome
        )
        return {
            "ok": False,
            "outcome": outcome,
            "langs": [],
            "duration_sec": duration,
            "via_proxy": proxy is not None,
            "error": error_text,
        }

    duration = round(time.monotonic() - started, 2)
    from ytt.errors import EMPTY_BODY

    langs = get_available_langs(info or {})
    if langs:
        return {
            "ok": True,
            "outcome": "ok",
            "langs": langs,
            "duration_sec": duration,
            "via_proxy": proxy is not None,
            "error": None,
        }

    log.warning("Canary: no captions found for %s", video_id)
    return {
        "ok": False,
        "outcome": EMPTY_BODY,
        "langs": [],
        "duration_sec": duration,
        "via_proxy": proxy is not None,
        "error": "known-good video returned no caption tracks",
    }


def _probe_one(video_id: str, settings: Any) -> bool:
    """Run a single yt-dlp caption probe against ``video_id``.

    Returns ``True`` on success (captions extracted), ``False`` on any error.
    Intentionally synchronous — called via ``asyncio.to_thread`` from the probe loop.
    """
    return bool(probe_once_detail(video_id)["ok"])


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
# One-shot mode (lightweight canary — bead ytt-58325cdf / plan Proof Obligation)
# ---------------------------------------------------------------------------


def run_once(video_id: str | None = None, *, via_proxy: bool = False) -> dict:
    """Run the canary once: egress report + one caption fetch.

    The lightweight Proof-Obligation canary (plan §Proof Obligations —
    "Residential egress is 'decisive, free'"): fetches captions for one
    known-good video from wherever the command runs and reports ``ok`` vs
    ``ip_blocked``.  Complements the long-running probe loop above; intended
    for one-shot use inside ardenone-cluster (``kubectl exec``, a probe pod,
    or an Argo step) and by self-hosters verifying their egress.

    ``via_proxy=True`` (``ytt canary --once --via-proxy``) runs the caption
    probe **through** ``YTT_PROXY_URL`` — the end-to-end check that the
    configured proxy actually carries YouTube traffic.  The default probes
    direct, matching the caption path's direct-first behavior; the egress
    classification half always dials through the proxy when one is set
    (it classifies the proxy's egress, the effective fallback path).

    Returns a JSON-serializable report with ``verdict`` set to the caption
    fetch ``outcome`` (``"ok"`` or a stable error_code) and ``ran_at`` stamped
    UTC — the report is meant to be pasted somewhere durable as evidence.
    Contains no secrets (error strings are credential-redacted).
    """
    from datetime import datetime, timezone

    from ytt.config import get_settings
    from ytt.observability import redact_credentials
    from ytt.selftest import probe_egress

    settings = get_settings()
    vid = video_id or CANARY_VIDEO_IDS[0]

    # Egress classification is context, not the verdict — if ipinfo.io is
    # unreachable the caption fetch is still the ground truth.
    try:
        report_egress = probe_egress(proxy_url=settings.proxy_url)
        egress: dict = {
            "ip": report_egress.ip,
            "asn": report_egress.asn,
            "org": report_egress.org,
            "via_proxy": report_egress.via_proxy,
            "is_residential": report_egress.is_residential,
        }
    except Exception as exc:
        # httpx failure strings can quote the proxy URL (creds included).
        egress = {
            "error": redact_credentials(str(exc)),
            "via_proxy": settings.proxy_url is not None,
        }

    fetch_report = probe_once_detail(
        vid, proxy=settings.proxy_url if via_proxy else None
    )

    return {
        "mode": "once",
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video_id": vid,
        "egress": egress,
        "caption_fetch": fetch_report,
        "verdict": fetch_report["outcome"],
    }


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


if __name__ == "__main__":
    import sys as _sys

    if "--once" in _sys.argv:
        import json as _json

        _vid: str | None = None
        if "--video-id" in _sys.argv:
            _vid = _sys.argv[_sys.argv.index("--video-id") + 1]
        _report = run_once(
            video_id=_vid, via_proxy="--via-proxy" in _sys.argv
        )
        print(_json.dumps(_report, indent=2))
        _sys.exit(0 if _report["verdict"] == "ok" else 1)

    _sys.exit(main())
