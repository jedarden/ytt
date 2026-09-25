"""Manifest-level assertions for the IngressRoute visibility model
(docs/notes/http-endpoints.md §Visibility model).

What is reachable from the public internet is decided by the Traefik
IngressRoute (``deploy/k8s/ardenone-cluster/ytt/ingressroute.yml``), not by
the app.  The documented model is:

* one broad **public** rule — ``Host(mcp.ardenone.com) && PathPrefix(/ytt)``
  at Traefik's default priority — carries *every* path under the prefix,
  ``/ytt/metrics`` and ``/ytt/health`` included;
* three **priority-1000** rules carry the path-inserted
  ``/.well-known/{oauth-protected-resource,oauth-authorization-server,
  openid-configuration}/ytt`` metadata routes (priority 1000 against ibkr's
  broad ``/.well-known`` rule, which auto-computes ~80 from rule length —
  a wide, stable margin, manifest header / plan §Deployment);
* the canary's metrics Service (``ytt-canary`` :8081) is **in-cluster
  only**: a ClusterIP fronted by the ServiceMonitor, with no IngressRoute
  rule at all.

The design consequence is the invariant this file keeps deliberate: because
the prefix rule routes everything, *every unauthenticated response body must
be safe to expose publicly* — which is why ``/ytt/metrics`` carries only
aggregate series with a bounded label set and ``/ytt/health`` a fixed body.

Nothing else holds the manifest side: the endpoint-contract suite
(``test_endpoint_contract.py``) drives the ASGI app and stays green if the
route manifest drifts — a new rule exposing an internal path, a lost
``.well-known`` priority, or a rule added for ``ytt-canary``.  Asserted here
(in the style of ``test_single_replica.py``) so drift fails the gate; a
legitimate change is a deliberate docs + manifest + test edit.  The mirror
itself is pinned to the applied manifests by ``test_deploy_parity.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # via fastmcp (runtime dependency) — always present in the venv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = REPO_ROOT / "deploy" / "k8s"

# The documented route set, byte-exact as written in the manifest — the only
# routers the visibility model admits.
PUBLIC_RULE_MATCH = "Host(`mcp.ardenone.com`) && PathPrefix(`/ytt`)"
WELLKNOWN_RULE_MATCHES = (
    "Host(`mcp.ardenone.com`) && PathPrefix(`/.well-known/oauth-protected-resource/ytt`)",
    "Host(`mcp.ardenone.com`) && PathPrefix(`/.well-known/oauth-authorization-server/ytt`)",
    "Host(`mcp.ardenone.com`) && PathPrefix(`/.well-known/openid-configuration/ytt`)",
)

# The .well-known margin over ibkr's auto-computed ~80 (manifest header).
WELLKNOWN_PRIORITY = 1000

# Every documented rule fronts the main server Service — never ytt-canary.
MAIN_SERVICE_NAME = "ytt"
MAIN_SERVICE_PORT = 8080
CANARY_SERVICE_NAME = "ytt-canary"
CANARY_METRICS_PORT = 8081  # the canary metrics port, by Service contract


def _k8s_yaml_documents():
    # The mirror historically uses both extensions (*.yml applied manifests,
    # *.yaml iad-ci artifacts); scanning only one could let the guard pass
    # vacuously over no IngressRoute documents.
    for path in sorted(DEPLOY_ROOT.rglob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict):
                yield path, doc


def _ingressroutes():
    for path, doc in _k8s_yaml_documents():
        if doc.get("kind") == "IngressRoute":
            yield path, doc


def _routes():
    """Yield (path, match, route) for every rule of every IngressRoute.

    Scanning *all* IngressRoutes under the mirror (not just the ``ytt``
    named one) is deliberate: a second IngressRoute carrying rules for the
    same host is exactly the drift class — a new rule is a new public
    surface wherever the document it lives in is named.
    """
    for path, doc in _ingressroutes():
        for route in doc.get("spec", {}).get("routes") or []:
            yield path, route.get("match", ""), route


def _route_service(route):
    """The single (name, port) a rule fronts, or None if it isn't exactly one."""
    services = route.get("services") or []
    if len(services) != 1:
        return None
    return (services[0].get("name"), services[0].get("port"))


def _path_prefixes(match: str) -> list[str]:
    return re.findall(r"PathPrefix\(`([^`]*)`\)", match)


def _covers(prefix: str, path: str) -> bool:
    # Traefik PathPrefix is segment-aware: PathPrefix(`/yt`) does not match
    # `/ytt/metrics` — mirror that so a lookalike prefix isn't a carrier.
    return prefix == "/" or path == prefix or path.startswith(prefix.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# The route set: exactly the documented model, all fronting the main server
# ---------------------------------------------------------------------------


def test_ingressroute_mirror_is_nonempty():
    names = [doc["metadata"]["name"] for _, doc in _ingressroutes()]
    assert names, (
        f"no IngressRoute documents found under {DEPLOY_ROOT} — the glob or "
        "repo layout changed; fix the scan before trusting these assertions"
    )


def test_routes_are_exactly_the_documented_set():
    documented = {PUBLIC_RULE_MATCH, *WELLKNOWN_RULE_MATCHES}
    actual = []
    for path, match, route in _routes():
        actual.append(match)
        found = _route_service(route)
        assert found == (MAIN_SERVICE_NAME, MAIN_SERVICE_PORT), (
            f"{path.relative_to(REPO_ROOT)}: rule {match!r} fronts "
            f"{found!r}, not ({MAIN_SERVICE_NAME!r}, {MAIN_SERVICE_PORT!r}) "
            "— every documented rule fronts the main server Service; a rule "
            "fronting anything else changes the visibility model"
        )
    unexpected = sorted(set(actual) - documented)
    missing = sorted(documented - set(actual))
    assert not unexpected and not missing and len(actual) == len(documented), (
        "the IngressRoute route set drifted from the documented visibility "
        f"model (docs/notes/http-endpoints.md §Visibility model): unexpected "
        f"rules {unexpected}, missing rules {missing} (a duplicated match "
        "lands here too). A new rule is a new public surface — if the change "
        "is deliberate, update that doc and this test in the same commit."
    )


def test_wellknown_rules_carry_priority_1000():
    by_match: dict[str, tuple[Path, dict]] = {}
    for path, match, route in _routes():
        by_match.setdefault(match, (path, route))
    for match in WELLKNOWN_RULE_MATCHES:
        assert match in by_match, (
            f"the documented .well-known rule {match!r} is missing — the "
            "OAuth metadata request would fall through to ibkr's broad "
            "PathPrefix(`/.well-known`) rule and 401 against the wrong "
            "authorization server (manifest header, openid-configuration leg)"
        )
        path, route = by_match[match]
        assert route.get("priority") == WELLKNOWN_PRIORITY, (
            f"{path.relative_to(REPO_ROOT)}: rule {match!r} carries priority "
            f"{route.get('priority')!r}, not {WELLKNOWN_PRIORITY} — ibkr's "
            "broad PathPrefix(`/.well-known`) rule auto-computes ~80 from "
            "rule length, and a lost explicit priority lets it win the "
            "overlap"
        )


# ---------------------------------------------------------------------------
# The public prefix rule carries everything — /ytt/metrics included
# ---------------------------------------------------------------------------


def test_public_prefix_rule_is_the_sole_carrier_of_metrics():
    """``/ytt/metrics`` is publicly routed, and solely by the broad prefix
    rule. A dedicated ``/ytt/metrics`` router — narrowing, gating, or
    re-homing the scrape path — is the drift that would make the
    public-safety invariant accidental: as long as the prefix rule is the
    sole carrier, *every* unauthenticated body under the prefix must stay
    public-safe (docs/notes/http-endpoints.md §Visibility model)."""
    metrics_path = "/ytt/metrics"
    carriers = [
        (match, path, route)
        for path, match, route in _routes()
        if any(_covers(p, metrics_path) for p in _path_prefixes(match))
    ]
    assert [match for match, _, _ in carriers] == [PUBLIC_RULE_MATCH], (
        f"rules that can serve {metrics_path!r}: "
        f"{[match for match, _, _ in carriers]!r} — the documented model "
        f"routes it solely via the public prefix rule {PUBLIC_RULE_MATCH!r}"
    )
    _, path, route = carriers[0]
    assert route.get("priority") is None, (
        f"{path.relative_to(REPO_ROOT)}: the public prefix rule pins "
        f"priority {route.get('priority')!r}; the documented model keeps it "
        "at Traefik's default — the explicit margin belongs to the "
        ".well-known rules alone. Change the doc and this test together if "
        "that is deliberate."
    )
    assert _route_service(route) == (MAIN_SERVICE_NAME, MAIN_SERVICE_PORT), (
        f"{path.relative_to(REPO_ROOT)}: the public prefix rule must front "
        f"({MAIN_SERVICE_NAME!r}, {MAIN_SERVICE_PORT!r}) — it is the router "
        "that makes the whole prefix (transport, health, metrics, admin "
        "gate) reachable, so it must land on the main server Service"
    )


# ---------------------------------------------------------------------------
# The canary stays in-cluster: no rule anywhere fronts its Service
# ---------------------------------------------------------------------------


def test_no_rule_selects_the_canary_service():
    """The canary metrics port is in-cluster *by construction*: the ClusterIP
    Service ``ytt-canary`` (:8081) has no IngressRoute rule, so nothing
    off-cluster can reach it — network visibility is the contract there, not
    application auth (docs/notes/http-endpoints.md §Canary)."""
    offenders = []
    for path, match, route in _routes():
        for svc in route.get("services") or []:
            if (
                svc.get("name") == CANARY_SERVICE_NAME
                or svc.get("port") == CANARY_METRICS_PORT
            ):
                offenders.append(
                    (
                        str(path.relative_to(REPO_ROOT)),
                        match,
                        svc.get("name"),
                        svc.get("port"),
                    )
                )
    assert not offenders, (
        f"IngressRoute rules front the canary Service: {offenders} — "
        f"{CANARY_SERVICE_NAME}:{CANARY_METRICS_PORT} is in-cluster only "
        "(ClusterIP + ServiceMonitor, no IngressRoute rule). A public canary "
        "surface needs a deliberate docs + manifest + test change, and its "
        "bodies would then fall under the public-safety invariant."
    )
