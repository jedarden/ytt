"""Egress self-test + sub discovery (plan: Data Models / EgressReport).

Probes ``ipinfo.io`` for ip/asn/org and derives ``is_residential`` from the
seeded ``DATACENTER_ASNS``/``DATACENTER_ORG_PATTERNS`` sets. ``--show-sub`` reads
the last decoded token ``sub`` from ``/tmp/ytt_last_sub`` for allowlist setup.
Implemented in Phase 8. Scaffold stub.
"""

from __future__ import annotations


def run_selftest(show_sub: bool = False) -> int:
    """Run the egress probe (or print the last token sub). Implemented in Phase 8."""
    # TODO(phase-8): implement ipinfo.io probe + is_residential derivation and
    # the /tmp/ytt_last_sub --show-sub mechanism.
    raise NotImplementedError("ytt selftest is implemented in Phase 8")
