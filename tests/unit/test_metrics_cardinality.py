"""Public-metrics cardinality contract — the bounded label-set regression.

``docs/notes/http-endpoints.md`` ("Visibility model") makes *every
unauthenticated response body safe to expose publicly* the load-bearing
argument for the prefix-wide IngressRoute rule, and states that
``/ytt/metrics`` "carries only aggregate series with a bounded label set".
This module pins that guarantee endpoint-wide (bead ``ytt-bce185f2``) by
scraping both producers of ``ytt_*`` series and holding every exported
family to the documented surface:

- **(a) No label key drawn from an unbounded per-request identifier.**  Each
  family carries exactly its documented label keys — a future ``video_id``,
  OAuth ``subject``, Whisper ``job_id`` or proxy ``url`` label fails here,
  before it ships transcript-activity metadata to the public internet.  The
  per-family allowlist is the hard bound; the identifier-shape check behind
  it is a second, deliberately separate gate, so loosening the allowlist for
  a new label still fails until the identifier exemption is written down.
- **(b) Only the documented aggregate families.**  A brand-new ``ytt_*``
  family cannot appear on a public scrape (or on the canary's in-cluster
  one) without an entry in :data:`YTT_DOCUMENTED_SURFACE` — the exposition
  and the docs cannot drift apart silently.

Two scrapes, one per deployment that serves ``ytt_*`` series:

1. the real ASGI app's unauthenticated ``GET /ytt/metrics`` — the body the
   public internet can read (Starlette ``TestClient``, as
   ``tests/unit/test_endpoint_contract.py``, whose flat label-key allowlist
   this module tightens per family and extends with value shapes);
2. a fresh interpreter shaped like the canary Deployment (``import
   ytt.canary`` and nothing else) — the process whose ``:8081`` registry the
   ServiceMonitor scrapes.  In-process scraping cannot isolate that
   registry: the pytest process has usually imported both modules into its
   own default registry already, so a subprocess is the only faithful
   "canary registry".

The reserved structural labels the exposition format itself attaches
(``le`` on histogram buckets, ``quantile`` on summaries) are library-owned
numeric bucket boundaries, exempt here exactly as the docs promise — the
bounded surface being pinned is the *application* label set.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# The documented surface (docs/notes/http-endpoints.md — /ytt/metrics + canary)
# ---------------------------------------------------------------------------

#: Every ``ytt_*`` family a public exposition may carry, keyed by registry
#: family name (prometheus_client strips the counter ``_total`` suffix — the
#: same convention as ``test_observability.test_metrics_exist``) → the exact
#: label keys that family may carry.  A family or key outside this table is a
#: test failure, not a docs update: the public-safe claim is load-bearing.
YTT_DOCUMENTED_SURFACE: dict[str, frozenset[str]] = {
    # --- ytt.observability (registered in both processes) -------------------
    "ytt_fetch_blocks": frozenset({"outcome"}),
    "ytt_fetch_empty_body": frozenset(),
    "ytt_whisper_errors": frozenset({"reason"}),
    "ytt_whisper_job_seconds": frozenset(),
    "ytt_cache_bytes": frozenset(),
    "ytt_cache_evictions": frozenset(),
    "ytt_queue_depth": frozenset(),
    "ytt_rate_limited": frozenset({"subject_hash"}),
    "ytt_egress_is_residential": frozenset(),
    "ytt_canary_last_success_timestamp_seconds": frozenset(),
    "ytt_canary_failures": frozenset(),
    # --- ytt.canary (registered in the canary Deployment's process only) ---
    "ytt_canary_probe_last_success_timestamp_seconds": frozenset({"probe"}),
    "ytt_canary_probes": frozenset({"probe", "outcome"}),
}

#: Families only ``ytt.canary`` registers.  Optional on the main server's
#: scrape (present iff that process imported ``ytt.canary`` — production
#: never does; a pytest run that collected any canary test module always
#: does), required on the canary's own.
CANARY_ONLY_FAMILIES = frozenset(
    {
        "ytt_canary_probe_last_success_timestamp_seconds",
        "ytt_canary_probes",
    }
)

#: Families the main server's ``/ytt/metrics`` must export — registered at
#: import of ``ytt.observability``, which ``ytt.server`` imports
#: unconditionally.  Dropping one is a contract change, not an accident.
SERVER_REQUIRED_FAMILIES = frozenset(YTT_DOCUMENTED_SURFACE) - CANARY_ONLY_FAMILIES

#: Families the canary's ``:8081`` registry must export: that process imports
#: ``ytt.observability`` (the shared surface) *and* registers its own pair.
CANARY_REQUIRED_FAMILIES = frozenset(YTT_DOCUMENTED_SURFACE)

#: The default registry's non-``ytt_*`` exports are prometheus_client's own
#: process/python gauges — library-owned, label-bounded by the library.  A
#: family outside these namespaces has no business on a public exposition.
LIBRARY_FAMILY_PREFIXES = ("python_", "process_")

#: Structural labels the exposition format itself attaches (histogram bucket
#: upper bounds, summary quantiles) — numeric, library-owned, exempt exactly
#: as docs/notes/http-endpoints.md promises.
_STRUCTURAL_LABELS = frozenset({"le", "quantile"})

#: Substrings that mark a label key as an unbounded per-request identifier —
#: the failure modes this regression exists for (video id, OAuth subject,
#: Whisper job id, URL).  Deliberately separate from the per-family allowlist
#: above: documenting a new label requires editing the allowlist, and
#: documenting an identifier-shaped one must additionally get past this gate.
_IDENTIFIER_KEY_TOKENS = ("video", "subject", "email", "job", "url", "proxy")

#: Keys that are identifiers outright (exact match).
_IDENTIFIER_KEY_EXACT = frozenset({"id", "sub", "path", "transcript"})

#: A label value is data routed through a public endpoint: a bounded
#: vocabulary token (``ok``, ``ip_blocked``, ``via_proxy``…), never free
#: text, a URL, or an email.  (``subject_hash`` has its own shape below.)
_BOUNDED_VALUE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

#: ``ytt_rate_limited_total{subject_hash}`` carries the first 8 hex chars of
#: sha256(subject) — the only subject representation allowed public.
_SUBJECT_HASH_RE = re.compile(r"[0-9a-f]{8}")

#: The canary Deployment's whole ytt import surface is ``ytt.canary`` (which
#: transitively registers ``ytt.observability``); its ``:8081`` scrape is
#: ``generate_latest`` of the resulting default registry.
_CANARY_SCRAPE_SNIPPET = (
    "import ytt.canary  # the canary Deployment's whole ytt import surface\n"
    "from prometheus_client import REGISTRY, generate_latest\n"
    "print(generate_latest(REGISTRY).decode(), end='')\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalized_family(parsed_name: str) -> str:
    """Registry family name of a parsed exposition family.

    prometheus_client mirrors every counter/histogram's OpenMetrics creation
    timestamp into the classic text format as a separate ``<name>_created``
    gauge family; that companion normalizes onto the registry name (counters
    arrive from the parser already sans their ``_total``).
    """
    if parsed_name.endswith("_created"):
        return parsed_name[: -len("_created")]
    return parsed_name


def _identifier_shaped(label_key: str) -> bool:
    lowered = label_key.lower()
    if lowered == "subject_hash":
        return False  # the sanctioned sha256-prefix subject representation
    return lowered in _IDENTIFIER_KEY_EXACT or any(
        token in lowered for token in _IDENTIFIER_KEY_TOKENS
    )


def _assert_bounded_public_surface(
    body: str, *, required_families: frozenset[str], context: str
) -> None:
    """Hold one exposition body to the documented aggregate surface.

    For every family the body carries: documented (or a prometheus_client
    library default), each sample labelled with exactly that family's
    documented keys, and every label value a bounded vocabulary token (an
    8-hex ``subject_hash`` where subjects appear at all).  Also asserts no
    required family went missing.  Violations accumulate so one run reports
    every problem instead of the first.
    """
    problems: list[str] = []
    seen: set[str] = set()

    for family in text_string_to_metric_families(body):
        name = _normalized_family(family.name)
        if not name.startswith("ytt_"):
            if not name.startswith(LIBRARY_FAMILY_PREFIXES):
                problems.append(
                    f"{context}: family {name!r} is neither a documented "
                    f"ytt_* aggregate nor a prometheus_client "
                    f"{list(LIBRARY_FAMILY_PREFIXES)} default — unexplained "
                    "surface on a public exposition"
                )
            continue

        seen.add(name)
        allowed_labels = YTT_DOCUMENTED_SURFACE.get(name)
        if allowed_labels is None:
            problems.append(
                f"{context}: undocumented ytt_* family {name!r} — a new "
                "family cannot ship on a public exposition without a "
                "YTT_DOCUMENTED_SURFACE entry (docs/notes/http-endpoints.md)"
            )
            continue

        for sample in family.samples:
            labels = sample.labels
            unknown = set(labels) - allowed_labels - _STRUCTURAL_LABELS
            if unknown:
                problems.append(
                    f"{context}: {sample.name} carries label(s) "
                    f"{sorted(unknown)} outside {name}'s documented surface "
                    f"({sorted(allowed_labels) or 'no labels'}) — a "
                    "high-cardinality or identity-bearing label cannot ship "
                    "publicly"
                )
            for key, value in labels.items():
                if key in _STRUCTURAL_LABELS:
                    continue
                if _identifier_shaped(key):
                    problems.append(
                        f"{context}: {sample.name} label {key!r} is drawn "
                        "from an unbounded per-request identifier (video id, "
                        "OAuth subject, job id, URL) — subjects may appear "
                        "only as subject_hash, never as a raw identifier"
                    )
                if key == "subject_hash":
                    if not _SUBJECT_HASH_RE.fullmatch(value):
                        problems.append(
                            f"{context}: {sample.name} "
                            f"subject_hash={value!r} is not the documented "
                            "8-hex sha256 prefix — a wider subject "
                            "representation cannot ship publicly"
                        )
                elif not _BOUNDED_VALUE_RE.fullmatch(value):
                    problems.append(
                        f"{context}: {sample.name} label {key}={value!r} is "
                        "not a bounded vocabulary token — free text, a URL "
                        "or an email is not public-safe"
                    )

    missing = required_families - seen
    if missing:
        problems.append(
            f"{context}: documented families missing from the scrape "
            f"({sorted(missing)}) — dropping a public series is a contract "
            "change, not an accident"
        )

    assert not problems, "\n".join(problems)


def _canary_registry_scrape() -> str:
    """``generate_latest`` from a fresh interpreter shaped like the canary.

    The canary Deployment's process is ``import ytt.canary`` (plus its
    transitive ``ytt.observability``) and nothing else; the ServiceMonitor's
    ``:8081`` scrape is ``generate_latest`` of exactly that default
    registry.  A subprocess is the only faithful way to observe it from the
    suite — this pytest process may already hold both modules' registrations
    in its own default registry.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CANARY_SCRAPE_SNIPPET],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        "the canary-shaped interpreter failed to produce a registry scrape "
        f"(exit {proc.returncode}):\n{proc.stderr[-2000:]}"
    )
    return proc.stdout


# ---------------------------------------------------------------------------
# The two scrapes
# ---------------------------------------------------------------------------


def test_public_metrics_scrape_is_bounded_to_the_documented_surface():
    """The unauthenticated ``GET /ytt/metrics`` body — the one the
    prefix-wide IngressRoute rule hands to the whole internet — carries only
    the documented aggregate families, each with exactly its documented
    label keys and bounded label values."""
    from ytt.server import _record_rate_limited, build_asgi_app

    # Mint a real per-subject series first so the subject_hash path is
    # exercised, not just asserted in the abstract — the hash may ship, the
    # subject itself never may.
    marker_subject = "cardinality-probe@example.com"
    _record_rate_limited(marker_subject, "get_youtube_transcript")

    with TestClient(build_asgi_app(), raise_server_exceptions=True) as client:
        response = client.get("/ytt/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")
    assert marker_subject not in response.text

    _assert_bounded_public_surface(
        response.text,
        required_families=SERVER_REQUIRED_FAMILIES,
        context="GET /ytt/metrics",
    )


def test_canary_registry_scrape_is_bounded_to_the_documented_surface():
    """The canary Deployment's ``:8081`` registry — the shared process-wide
    registry plus its own per-path pair — exposes exactly the documented
    families, so the in-cluster-only endpoint obeys the same public-safe
    bound as the public ``/ytt/metrics`` body
    (docs/notes/http-endpoints.md, "Canary /metrics")."""
    _assert_bounded_public_surface(
        _canary_registry_scrape(),
        required_families=CANARY_REQUIRED_FAMILIES,
        context="canary :8081 registry (fresh import of ytt.canary)",
    )
