"""Post-deploy canary acceptance gate (``ytt canary --gate``).

The release gate after an image or egress change (bead ytt-026fdbb4;
``deploy/RUNBOOK.md`` §3): prove the running pod can actually reach YouTube
before the release is called done — the one thing a green health probe, the
OAuth metadata checks, and the absence of alerts do *not* prove.

What it runs:

1. ``ytt canary --once`` — the direct caption fetch (the native-egress proof).
2. ``ytt canary --once --via-proxy`` — **only when ``YTT_PROXY_URL`` is
   configured**: the end-to-end proof that the configured proxy carries
   YouTube traffic (``docs/notes/proxy-egress.md``).
3. ``ytt canary --once --browser`` — **only when the browser-primary path is
   configured** (``YTT_BROWSER_WS_URL`` set, ``YTT_FETCH_MODE`` not
   ``ytdlp``): the PO-token proof that the *primary* caption path works —
   fresh context, the player's own caption request, a non-empty timedtext
   body (``docs/notes/browser-fetch.md``).  A gate pass must not be
   reachable with the primary path down: before this probe, a release could
   pass on the yt-dlp fallback alone while every fetch silently degraded
   (open bead ``ytt-1e4c448b`` was exactly that shape).  A browser failure
   reports a real failing outcome, never a skip.

The gate passes only when every probe it ran reports ``outcome=ok``.  It
writes the combined JSON report to an evidence file (default
``/tmp/ytt-canary-evidence/``), prints it on stdout, and exits 0 only on a
full pass.  On failure the report carries a ``remediation`` directive —
rollback vs. escalate, keyed by which probe failed and how — mirroring the
decision table in ``deploy/RUNBOOK.md`` §3.1.

The evidence artifact's full contract — schema, destination, retention,
failure-output shape, and the sensitive-configuration exclusion — is
specified in ``docs/notes/canary-gate-evidence.md`` and drift-guarded by
``tests/unit/test_canary_gate.py`` (TestEvidenceSpecDoc).

Like ``ytt canary --once``, the gate never touches the singleton lock (only
``serve()`` does), so it is safe to exec into the live server pod.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ytt.canary import CANARY_VIDEO_IDS, run_once
from ytt.observability import redact_credentials

log = logging.getLogger(__name__)

#: Outcome synthesized when the gate itself crashes before a probe completes —
#: a tooling/environment failure, never an egress verdict.
GATE_ERROR = "gate_error"

#: Where evidence JSON lands when ``--evidence-dir`` is not given.  ``/tmp``
#: is writable in every context the gate runs (server pod, canary pod,
#: self-hoster's host); retention beyond the pod lifetime is the caller's
#: job — capture stdout into the release record (the durability contract:
#: ``docs/notes/canary-gate-evidence.md`` §4; RUNBOOK §3 step 4).
DEFAULT_EVIDENCE_DIR = "/tmp/ytt-canary-evidence"

_EVIDENCE_FILENAME_PREFIX = "ytt-canary-gate"

#: Probe order — also the order failing probes are reported in.  ``browser``
#: names the browser-primary caption path; it is probed only when the browser
#: path is configured (the same shape as ``via_proxy`` and ``YTT_PROXY_URL``).
PROBE_ORDER: tuple[str, ...] = ("direct", "via_proxy", "browser")


# ---------------------------------------------------------------------------
# Sensitive-configuration exclusion — enforced at the artifact boundary
# ---------------------------------------------------------------------------


def _scrub_secrets(node: Any) -> Any:
    """Return ``node`` with credential-bearing URLs redacted from every string.

    Defense-in-depth at the evidence boundary: ``run_once`` redacts its own
    error strings, but the gate owns the artifact — a future probe field or
    upstream regression that lets the credentialed ``YTT_PROXY_URL`` into a
    report must still never reach the retained JSON or stdout
    (``docs/notes/canary-gate-evidence.md`` §6).  Strings without a
    ``scheme://user:password@host`` URL pass through unchanged, so legitimate
    content (org names, language codes) is never mangled.
    """
    if isinstance(node, str):
        return redact_credentials(node)
    if isinstance(node, dict):
        return {key: _scrub_secrets(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_scrub_secrets(item) for item in node]
    return node


# ---------------------------------------------------------------------------
# Remediation — the rollback/escalation decision table (RUNBOOK §3.1)
# ---------------------------------------------------------------------------


def remediation_for(
    failed_probe: str, outcome: str, *, proxy_configured: bool
) -> str:
    """Return the operator directive for a gate failure.

    Keyed by which probe failed and its stable error code.  Every directive
    names the concrete rollback path (a git revert of the declarative-config
    pin — never ``kubectl rollout undo``) or the escalation target, and points
    at the evidence JSON.  ``deploy/RUNBOOK.md`` §3.1 mirrors this table in
    prose; the two are updated together (drift-guarded by
    ``TestRunbookRemediationMirror`` in ``tests/unit/test_canary_gate.py``).
    """
    lead = (
        "Transient single failures happen — re-run the gate once before "
        "acting. If it repeats: "
    )

    if outcome == GATE_ERROR:
        return (
            f"The gate crashed before completing the {failed_probe} probe — a "
            "tooling/environment failure, not an egress verdict. Do not read "
            "this as a canary result: fix the gate environment and re-run "
            "(check the traceback on stderr); escalate to the maintainers "
            "with the evidence JSON if it persists."
        )

    if failed_probe == "browser":
        # The primary path — its failures get browser-specific directives,
        # never the yt-dlp/extractor wording the direct/via_proxy rows use.
        if outcome == "rate_limited":
            return (
                lead
                + "the browser-primary caption path was rate-limited by "
                "YouTube — the browser pod's shared egress IP is burned or "
                "its quota is exhausted. A rollback will not fix it: "
                "escalate to the egress owner with the evidence JSON, and "
                "decide explicitly whether to keep or revert the tag while "
                "the browser path is throttled (fetches still serve through "
                "the yt-dlp fallback, at reduced caption coverage)."
            )
        if outcome == "empty_body":
            return (
                lead
                + "the browser player's caption request came back with an "
                "empty body — the PO token was missing or rejected (the "
                "detected-as-automation class). If this release touched "
                "the browser launch options, the stealth settings, or "
                "fetch code, revert the tag pin in declarative-config and "
                "push (RUNBOOK §5), then re-gate on the previous tag. If "
                "it did not, the browser Deployment's Chromium or its "
                "launch config drifted — escalate to the maintainers with "
                "the evidence JSON."
            )
        return (
            lead
            + "the browser-primary caption path is down "
            f"({outcome}) — every transcript fetch is silently falling "
            "back to yt-dlp, and the gate no longer calls that a pass. "
            "If this release changed YTT_BROWSER_WS_URL, the browser "
            "Deployment, or its NetworkPolicy, revert that manifest "
            "change in declarative-config and push (RUNBOOK §5), then "
            "re-run the gate. If it did not, the browser server is "
            "unreachable or refusing connections — check the "
            "ytt-browser pod and its egress NetworkPolicy (the "
            "connect-refused shape of bead ytt-1e4c448b) and escalate "
            "to the maintainers with the evidence JSON."
        )

    if outcome == "ip_blocked":
        if failed_probe == "via_proxy":
            return (
                lead
                + "the configured proxy cannot reach YouTube — the fallback "
                "egress path is broken. If this release changed "
                "YTT_PROXY_URL or proxy handling, revert that manifest "
                "change in declarative-config and push (RUNBOOK §5), then "
                "re-run the gate. If it did not, the proxy's residential IP "
                "is burned or its quota is exhausted and a rollback will not "
                "help — escalate to the proxy/egress owner with the evidence "
                "JSON. If the direct probe is failing too, transcript "
                "fetches are down for users — treat it as an incident, not "
                "just a failed gate."
            )
        if proxy_configured:
            return (
                lead
                + "direct egress is blocked by YouTube while the proxy probe "
                "passed — the caption path will degrade to its proxied "
                "fallback (residential bandwidth cost, still serving). This "
                "is an egress event, not a release defect: a rollback will "
                "not fix it. Escalate to the egress owner with the evidence "
                "JSON, and decide explicitly whether to keep or revert the "
                "tag while the direct IP is blocked."
            )
        return (
            lead
            + "native egress is blocked by YouTube and no proxy fallback is "
            "configured. If this release changed fetch code or bumped "
            "yt-dlp, revert the tag pin in declarative-config and push "
            "(RUNBOOK §5), then re-gate on the previous tag. If it did not, "
            "the egress IP itself is burned and a rollback will not help — "
            "escalate to the egress owner with the evidence JSON."
        )

    # Non-egress outcome (empty_body, private, rate_limited, …) on the
    # known-good canary video.
    return (
        lead
        + f"the {failed_probe} caption probe failed with a non-egress "
        "outcome "
        f"({outcome}) on the known-good canary video — this is not an "
        "egress verdict. Most likely a yt-dlp/extractor regression shipped "
        "in the new image: revert the tag pin in declarative-config and "
        "push (RUNBOOK §5), then re-gate on the previous tag. If this "
        "release changed no fetch code, escalate to the maintainers with "
        "the evidence JSON — the fixed canary video list itself may need "
        "updating."
    )


# ---------------------------------------------------------------------------
# Gate report
# ---------------------------------------------------------------------------


def _gate_error_probe(
    video_id: str | None, *, via_proxy: bool = False, exc: Exception
) -> dict:
    """Synthesize a ``run_once``-shaped report after an unexpected crash."""
    return {
        "mode": "once",
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video_id": video_id or CANARY_VIDEO_IDS[0],
        "egress": {},
        "caption_fetch": {
            "ok": False,
            "outcome": GATE_ERROR,
            "langs": [],
            "duration_sec": 0.0,
            "via_proxy": via_proxy,
            "error": redact_credentials(str(exc)),
        },
        "verdict": GATE_ERROR,
    }


def _run_probe(video_id: str | None, *, via_proxy: bool = False, browser: bool = False) -> dict:
    """Run one ``run_once`` probe; a crash becomes a ``gate_error`` report.

    Probes stay ``run_once``-shaped so consumers never branch on missing
    keys, and a gate bug can never take the shape of a passing probe.  The
    report is scrubbed before it is embedded (see :func:`_scrub_secrets`).
    """
    try:
        return _scrub_secrets(
            run_once(video_id=video_id, via_proxy=via_proxy, browser=browser)
        )
    except Exception as exc:  # noqa: BLE001 — the gate reports, not raises
        log.exception(
            "Canary gate: %s probe crashed", _probe_label(via_proxy, browser)
        )
        return _gate_error_probe(video_id, via_proxy=via_proxy, exc=exc)


def _probe_label(via_proxy: bool, browser: bool = False) -> str:
    if browser:
        return "browser"
    return "via_proxy" if via_proxy else "direct"


def _evidence_path(evidence_dir: str | os.PathLike[str], ran: datetime) -> Path:
    """Pick the evidence file's path: timestamped, never overwriting.

    Two gate runs sharing a second (an immediate re-run after a failure)
    would otherwise collide on the same name and silently destroy the first
    run's artifact — the second gets a ``-2``, ``-3``, … suffix instead.
    Sequential runs are the contract; two gates racing in the same second in
    the same directory is not a supported scenario (§3 of the evidence spec).
    """
    candidate = Path(evidence_dir) / "{}-{}.json".format(
        _EVIDENCE_FILENAME_PREFIX, ran.strftime("%Y%m%dT%H%M%SZ")
    )
    sequence = 2
    while candidate.exists():
        candidate = candidate.with_name(f"{candidate.stem}-{sequence}.json")
        sequence += 1
    return candidate


def _write_evidence(report: dict, evidence_dir: str | os.PathLike[str]) -> None:
    """Write the combined report to the evidence file and record its path.

    The write is atomic (temp file + rename): a reader — or a release record
    — never observes a half-written artifact, and a crashed write leaves no
    truncated file that would parse as evidence.  A failed write degrades to
    ``evidence_error`` on the report — the stdout copy is still evidence —
    but never flips a verdict.
    """
    ran = datetime.fromisoformat(report["ran_at"])
    path = _evidence_path(evidence_dir, ran)
    persisted = dict(report, evidence_file=str(path))
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Canary gate: evidence write failed: %s", exc)
        report["evidence_error"] = redact_credentials(str(exc))
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover — cleanup is best-effort
            pass
        return
    report["evidence_file"] = str(path)


def run_gate(
    *,
    video_id: str | None = None,
    evidence_dir: str | os.PathLike[str] | None = None,
) -> dict:
    """Run the post-deploy canary acceptance gate and return its report.

    Runs the direct one-shot canary, plus the ``--via-proxy`` probe when a
    proxy is configured (``YTT_PROXY_URL``), plus the browser-primary probe
    when the browser path is configured (``YTT_BROWSER_WS_URL`` with
    ``YTT_FETCH_MODE`` not ``ytdlp``).  Pass only on a full ``ok``:
    every probe's ``verdict`` must be ``"ok"`` for ``gate == "pass"`` —
    including the browser probe, so a release cannot pass on the yt-dlp
    fallback while the primary path is down.  The returned report is
    JSON-serializable (it is written to the evidence file and printed
    verbatim) and contains no secrets — probe reports carry
    credential-redacted error strings, and the proxy URL itself never
    appears.  Every run writes a fresh timestamped artifact and nothing is
    ever deleted: a re-run appends evidence, it does not replace it.

    Failure reports carry ``remediation`` — the rollback/escalation directive
    from :func:`remediation_for` — and the CLI exits 1.
    """
    from ytt.browser_fetch import browser_fetch_enabled
    from ytt.config import get_settings

    settings = get_settings()
    proxy_configured = settings.proxy_url is not None
    browser_configured = browser_fetch_enabled(settings)
    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    probes: dict[str, dict] = {
        "direct": _run_probe(video_id, via_proxy=False),
    }
    if proxy_configured:
        probes["via_proxy"] = _run_probe(video_id, via_proxy=True)
    if browser_configured:
        probes["browser"] = _run_probe(video_id, browser=True)

    failed_probe = next(
        (name for name in PROBE_ORDER if name in probes and probes[name]["verdict"] != "ok"),
        None,
    )
    verdict = probes[failed_probe]["verdict"] if failed_probe else "ok"
    passed = failed_probe is None

    report: dict[str, Any] = {
        "mode": "gate",
        "ran_at": ran_at,
        "video_id": video_id or CANARY_VIDEO_IDS[0],
        "proxy_configured": proxy_configured,
        "browser_configured": browser_configured,
        "probes": probes,
        "verdict": verdict,
        "gate": "pass" if passed else "fail",
        "failed_probe": failed_probe,
        "remediation": (
            remediation_for(failed_probe, verdict, proxy_configured=proxy_configured)
            if failed_probe
            else None
        ),
        "evidence_file": None,
    }

    _write_evidence(report, evidence_dir or DEFAULT_EVIDENCE_DIR)
    return report
