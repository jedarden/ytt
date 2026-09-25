"""Standalone residential-egress canary (plan: §Observability / Canary).

A long-running probe that calls yt-dlp directly (same code path as
:mod:`ytt.fetch`, NOT via the HTTP tool endpoint — bypasses OAuth) against a
fixed internal video list, exposing ``ytt_canary_*`` metrics on a Prometheus
``/metrics`` endpoint.  Runs as its own Deployment (no K8s Jobs — plan
§Infrastructure constraint).

Metrics emitted (plan §Observability / Canary; per-path pair per
deploy/CANARY-MONITORING-RUNBOOK.md §1):

    ytt_canary_last_success_timestamp_seconds  — Gauge (set when ANY path succeeds)
    ytt_canary_failures_total                  — Counter (incremented when NO path succeeds)
    ytt_canary_probe_last_success_timestamp_seconds — Gauge{probe} (per path)
    ytt_canary_probes_total                    — Counter{probe, outcome} (per path)

The per-path pair is what makes a persistent failure *attributable*:
``probe=direct|via_proxy`` (the same vocabulary as the gate,
``ytt.canary_gate.PROBE_ORDER``) distinguishes "native egress blocked, users
still served via the proxy fallback" (``YttCanaryDirectBlocked``) from "the
fallback is broken while direct is healthy" (``YttCanaryFallbackBroken``);
``YttCanaryProbeFlapping`` catches the case both staleness gauges miss — a
path failing every other cycle while its gauge keeps refreshing.  The
via_proxy series exists only while ``YTT_PROXY_URL`` is configured —
its absence means "this canary does not probe via proxy", never "the proxy
is broken".  Continuous monitoring and the alert-response procedure are
defined in ``deploy/CANARY-MONITORING-RUNBOOK.md``; the canonical
alert-rule definitions live in its §2 (drift-guarded against both this
module's metrics and the applied ``prometheusrule.yml`` by
``tests/unit/test_canary_monitoring.py``).

Freshness gauges initialize to **loop-start time**, not 0: a pod restart
cannot fire the staleness alerts before the first probe completes, and
``time() - gauge`` is bounded by process age (a gauge older than the pod
can never be observed).

PrometheusRule: fire ``YttCanaryFailed`` if
    ``time() - ytt_canary_last_success_timestamp_seconds > 1800``
(3 consecutive 10-min probes missed on every path — fetches down for users).

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

from prometheus_client import REGISTRY, Counter, Gauge, start_http_server

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
# Per-path probe metrics (CANARY-MONITORING-RUNBOOK §1 — continuous
# freshness/failure monitoring)
# ---------------------------------------------------------------------------

#: Probe-path label values — one vocabulary across the one-shot canary, the
#: gate and the continuous metrics, so an alert, a remediation directive and
#: a log line always name a path the same way.  Pinned equal to
#: ``ytt.canary_gate.PROBE_ORDER`` by tests/unit/test_canary_monitoring.py.
PROBE_DIRECT = "direct"
PROBE_VIA_PROXY = "via_proxy"
PROBE_LABELS: tuple[str, ...] = (PROBE_DIRECT, PROBE_VIA_PROXY)

# These two live here rather than in ytt.observability (the home of the
# overall pair above) because only this module's probe loop emits them: the
# canary process registers them, the server process never does, and a
# missing series on the server scrape means exactly that.
ytt_canary_probe_last_success_timestamp_seconds = Gauge(
    "ytt_canary_probe_last_success_timestamp_seconds",
    "Unix timestamp of the last successful canary probe by path "
    '(probe="direct"|"via_proxy"); initialized to probe-loop start time.',
    ["probe"],
)

#: Canonical outcome labels for :data:`ytt_canary_probes_total`, pre-registered
#: as zero children at loop start (same rationale as
#: ``ytt.observability.FETCH_BLOCK_OUTCOMES``: an absent series must never be
#: readable as "metric not registered").  Non-canonical outcomes still get
#: their own child on first occurrence — Counter children auto-create.
CANARY_PROBE_OUTCOMES: tuple[str, ...] = (
    "ok",
    "ip_blocked",
    "empty_body",
    "rate_limited",
    "unavailable",
)

ytt_canary_probes_total = Counter(
    "ytt_canary_probes_total",
    "Canary probe-ladder terminations by path and outcome (one per path per "
    'cycle; outcome="ok" or a stable ytt.errors error_code).',
    ["probe", "outcome"],
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


def _probe_ladder_once(proxy: str | None = None) -> dict:
    """Walk the fixed video ladder once and return the terminating probe.

    Probes :data:`CANARY_VIDEO_IDS` in order and stops at the first success
    (the plan §Canary fallback ladder — one good fetch per cycle, no wasted
    residential bandwidth).  Returns the terminating :func:`probe_once_detail`
    report with the video that produced it added as ``video_id``, so the
    ladder's result carries its own ``outcome`` for the per-path metrics and
    the logs.  Never returns an empty dict: the ladder has at least one video.

    ``proxy`` is the continuous counterpart of ``ytt canary --once
    --via-proxy``: pass ``YTT_PROXY_URL`` to measure the fallback path from
    the same process and cadence that measures the direct one.
    """
    result: dict = {}
    for video_id in CANARY_VIDEO_IDS:
        result = dict(probe_once_detail(video_id, proxy=proxy), video_id=video_id)
        if result["ok"]:
            break
    return result


def _record_probe(probe: str, detail: dict) -> bool:
    """Record one path's ladder result in the per-path metrics.

    Every termination increments ``ytt_canary_probes_total{probe, outcome}``;
    a success also stamps the per-path freshness gauge.  Returns the detail's
    ``ok`` so the caller can fold paths into the overall cycle verdict.
    """
    ytt_canary_probes_total.labels(probe=probe, outcome=detail["outcome"]).inc()
    if detail["ok"]:
        ytt_canary_probe_last_success_timestamp_seconds.labels(probe=probe).set(
            time.time()
        )
    return bool(detail["ok"])


def _log_probe(probe: str, detail: dict) -> None:
    if detail["ok"]:
        log.info("Canary probe succeeded for %s (probe=%s)", detail["video_id"], probe)
    else:
        log.warning(
            "Canary probe failed for %s (probe=%s, outcome=%s)",
            detail["video_id"],
            probe,
            detail["outcome"],
        )


# ---------------------------------------------------------------------------
# Probe loop
# ---------------------------------------------------------------------------


async def run_probe_loop(interval_sec: int = 600) -> None:
    """Async probe loop — runs forever (plan: long-running Deployment).

    Every ``interval_sec`` seconds, walk the fallback ladder **directly**;
    when ``YTT_PROXY_URL`` is configured, walk it **through the proxy** too —
    the continuous counterpart of the gate's two probes (RUNBOOK §3.1,
    CANARY-MONITORING-RUNBOOK §1), so a persistent failure is attributable
    to a path, not just "the canary".

    Metrics per cycle (CANARY-MONITORING-RUNBOOK §1):

    * ``ytt_canary_probes_total{probe, outcome}`` — one increment per path
    * ``ytt_canary_probe_last_success_timestamp_seconds{probe}`` — stamped
      per path on success
    * ``ytt_canary_last_success_timestamp_seconds`` — stamped when ANY path
      succeeded; this is the gauge ``YttCanaryFailed`` watches, so stale
      means *neither* path has worked for the window — fetches down
    * ``ytt_canary_failures_total`` — incremented when NO path succeeded
    """
    from ytt.config import get_settings

    settings = get_settings()
    proxy_url = settings.proxy_url

    # Boot-initialize freshness to loop start (not 0) and pre-register the
    # zero counter children for every path this loop will actually probe —
    # see the module docstring and CANARY-MONITORING-RUNBOOK §1 for why.
    boot = time.time()
    ytt_canary_last_success_timestamp_seconds.set(boot)
    probed_paths = [PROBE_DIRECT] + ([PROBE_VIA_PROXY] if proxy_url else [])
    for probe in probed_paths:
        ytt_canary_probe_last_success_timestamp_seconds.labels(probe=probe).set(boot)
        for outcome in CANARY_PROBE_OUTCOMES:
            ytt_canary_probes_total.labels(probe=probe, outcome=outcome)

    log.info(
        "Canary probe loop starting (interval=%ds, videos=%s, proxy_probes=%s)",
        interval_sec,
        CANARY_VIDEO_IDS,
        bool(proxy_url),
    )

    while True:
        direct = await asyncio.to_thread(_probe_ladder_once)
        any_ok = _record_probe(PROBE_DIRECT, direct)
        _log_probe(PROBE_DIRECT, direct)
        if proxy_url:
            via = await asyncio.to_thread(_probe_ladder_once, proxy_url)
            any_ok = _record_probe(PROBE_VIA_PROXY, via) or any_ok
            _log_probe(PROBE_VIA_PROXY, via)

        if any_ok:
            ytt_canary_last_success_timestamp_seconds.set(time.time())
        else:
            ytt_canary_failures_total.inc()
            log.error(
                "Canary probe failed on every path (videos=%s)", CANARY_VIDEO_IDS
            )

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
