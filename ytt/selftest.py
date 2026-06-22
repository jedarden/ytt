"""Egress self-test + sub discovery (plan: §Data Models / EgressReport).

Probes ``ipinfo.io`` for ip/asn/org and derives ``is_residential`` from the
seeded ``DATACENTER_ASNS``/``DATACENTER_ORG_PATTERNS`` sets.  The
``--show-sub`` flag reads the last decoded token ``sub`` from
``/tmp/ytt_last_sub`` for allowlist setup.

Plan §Security & Authorization:
    DATACENTER_ASNS and DATACENTER_ORG_PATTERNS are seeded maintenance points;
    update on canary false-positives.

Plan §EgressReport:
    ``is_residential = asn not in DATACENTER_ASNS
                       and not any(p in org.upper() for p in DATACENTER_ORG_PATTERNS)``

The ``/tmp/ytt_last_sub`` file is written by :mod:`ytt.authz` (``write_last_sub``)
on first successful auth; mode 0600; not logged to stdout (plan §Security —
"Sub discovery mechanism not blocked by the redaction filter").
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

from ytt.models import EgressReport

# ---------------------------------------------------------------------------
# Known-datacenter AS numbers (seeded — plan §EgressReport)
# Update on canary false-positives.
# ---------------------------------------------------------------------------

DATACENTER_ASNS: frozenset[str] = frozenset(
    {
        "AS16509",   # Amazon AWS
        "AS14618",   # Amazon AWS (alternate range)
        "AS15169",   # Google Cloud Platform
        "AS396982",  # Google Cloud Platform (alternate)
        "AS8075",    # Microsoft Azure
        "AS8069",    # Microsoft Azure (alternate)
        "AS14061",   # DigitalOcean
        "AS63949",   # Linode / Akamai
        "AS13335",   # Cloudflare
        "AS20940",   # Akamai
        "AS16276",   # OVH SAS
        "AS14907",   # Wikimedia
        "AS36351",   # SoftLayer / IBM Cloud
        "AS36692",   # OpenDNS / Cisco
        "AS32934",   # Meta / Facebook
        "AS15133",   # Edgecast / Verizon Digital Media
        "AS54113",   # Fastly
        "AS2906",    # Netflix
        "AS19551",   # Incapsula / Imperva
        "AS46606",   # Unified Layer / Bluehost
        "AS22576",   # Rackspace
        "AS27357",   # Rackspace
        "AS10532",   # Rackspace (more ranges)
    }
)

#: Uppercase string fragments matched against the ``org`` field.
DATACENTER_ORG_PATTERNS: tuple[str, ...] = (
    "AMAZON",
    "AWS",
    "GOOGLE",
    "MICROSOFT",
    "AZURE",
    "DIGITALOCEAN",
    "LINODE",
    "CLOUDFLARE",
    "AKAMAI",
    "OVH",
    "IBM CLOUD",
    "SOFTLAYER",
    "RACKSPACE",
    "FASTLY",
    "NETLIFY",
    "HETZNER",
    "VULTR",
    "SCALEWAY",
    "LEASEWEB",
    "CHOOPA",   # Vultr alias
    "HOSTWINDS",
)

# ---------------------------------------------------------------------------
# is_residential derivation (plan: §Data Models / EgressReport)
# ---------------------------------------------------------------------------

_LAST_SUB_PATH = "/tmp/ytt_last_sub"


def derive_is_residential(asn: str | None, org: str | None) -> bool:
    """Return ``True`` if the egress IP is classified as residential.

    Plan §EgressReport:
        ``is_residential = asn not in DATACENTER_ASNS
                           and not any(p in org.upper() for p in DATACENTER_ORG_PATTERNS)``

    Both ``asn`` and ``org`` may be ``None`` (e.g. private/RFC-1918 IP) — in that
    case we default to ``True`` (assume residential if no signal contradicts it).
    """
    if asn is not None and asn.upper() in DATACENTER_ASNS:
        return False
    if org is not None:
        org_upper = org.upper()
        if any(pattern in org_upper for pattern in DATACENTER_ORG_PATTERNS):
            return False
    return True


# ---------------------------------------------------------------------------
# Egress probe (plan §Observability / EgressReport + /admin/egress)
# ---------------------------------------------------------------------------

_IPINFO_URL = "https://ipinfo.io/json"

# Timeout for the ipinfo.io HTTP call (integration-gated — network required).
_PROBE_TIMEOUT_SEC = 10.0


def probe_egress(proxy_url: str | None = None) -> EgressReport:
    """Probe ``ipinfo.io/json`` and return a populated ``EgressReport``.

    Plan §Observability — startup egress log + ``/admin/egress`` handler.

    ``ipinfo.io`` returns a JSON body with at least ``ip`` and optionally
    ``org`` (format: ``"AS12345 Company Name"``).  We split the org field
    to extract the ASN.

    This function is **synchronous** (blocking) so it can run at startup before
    the event loop starts, or be called via ``asyncio.to_thread`` from async
    contexts.  Raises ``httpx.HTTPError`` on network failure (integration only).
    """
    kwargs: dict[str, Any] = {"timeout": _PROBE_TIMEOUT_SEC}
    via_proxy = False

    if proxy_url:
        kwargs["proxies"] = proxy_url
        via_proxy = True

    with httpx.Client(**kwargs) as client:
        resp = client.get(_IPINFO_URL)
        resp.raise_for_status()
        data = resp.json()

    ip: str = data.get("ip", "unknown")
    org_raw: str | None = data.get("org")

    # org field from ipinfo.io: "AS12345 SomeName"
    asn: str | None = None
    org: str | None = None
    if org_raw:
        parts = org_raw.split(" ", 1)
        if parts[0].upper().startswith("AS"):
            asn = parts[0].upper()
            org = parts[1] if len(parts) > 1 else None
        else:
            org = org_raw

    is_res = derive_is_residential(asn, org)

    return EgressReport(
        ip=ip,
        asn=asn,
        org=org,
        via_proxy=via_proxy,
        is_residential=is_res,
    )


# ---------------------------------------------------------------------------
# run_selftest — CLI entry point (plan §Tools / ytt selftest)
# ---------------------------------------------------------------------------

def run_selftest(show_sub: bool = False) -> int:
    """Run the egress probe (or print the last token sub).

    Called by ``ytt selftest`` / ``ytt selftest --show-sub``.

    Returns 0 on success, 1 on failure.

    Plan §Security — "Sub discovery mechanism: add a ``ytt selftest --show-sub``
    CLI command that reads the last decoded token sub from a temporary file
    (``/tmp/ytt_last_sub``, written once on first successful auth, mode 0600,
    not logged to stdout)."
    """
    if show_sub:
        return _show_sub()
    return _run_egress_probe()


def _show_sub() -> int:
    """Print the last decoded ``sub`` claim from ``/tmp/ytt_last_sub``."""
    try:
        with open(_LAST_SUB_PATH) as fh:
            sub = fh.read().strip()
    except FileNotFoundError:
        print(
            "No sub claim on file yet.  Make one authenticated tool call first,\n"
            "then run `ytt selftest --show-sub` again.",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        print(f"Cannot read {_LAST_SUB_PATH}: {e}", file=sys.stderr)
        return 1

    if not sub:
        print("Sub file is empty — no authenticated calls recorded yet.", file=sys.stderr)
        return 1

    print(sub)
    print(
        f"\nAdd this value to YTT_ALLOWED_SUBJECTS to permit this user.",
        file=sys.stderr,
    )
    return 0


def _run_egress_probe() -> int:
    """Probe ipinfo.io and print the result as JSON; update the metric."""
    try:
        from ytt.observability import ytt_egress_is_residential
        report = probe_egress()
        ytt_egress_is_residential.set(1 if report.is_residential else 0)
        output = {
            "ip": report.ip,
            "asn": report.asn,
            "org": report.org,
            "via_proxy": report.via_proxy,
            "is_residential": report.is_residential,
        }
        print(json.dumps(output, indent=2))
        if not report.is_residential:
            print(
                "\nWARNING: egress IP is classified as a datacenter/cloud IP. "
                "YouTube may block fetches from this address.\n"
                "Set YTT_PROXY_URL to a residential proxy to work around this.",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception as exc:  # pragma: no cover — integration path
        print(f"Egress probe failed: {exc}", file=sys.stderr)
        return 1
