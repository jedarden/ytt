"""Deployed-ingress smoke test for the documented HTTP surface (bead ytt-734bcd01).

Drives the **actual deployed public route** — DNS, TLS, the Cloudflare tunnel,
Traefik's IngressRoute (``deploy/k8s/ardenone-cluster/ytt/ingressroute.yml``,
``YTT_PATH_PREFIX=/ytt/``) and the running app — and asserts the behaviors
`docs/notes/http-endpoints.md` promises for each family of the public surface:

* **public, unauthenticated:** ``/ytt/health`` (fixed body) and ``/ytt/metrics``
  (Prometheus exposition, bounded label surface);
* **protected, unauthenticated caller:** the MCP transport (``/ytt`` — the only
  route that can trigger transcript work) and ``/ytt/admin/egress`` both 401
  with a ``WWW-Authenticate: Bearer …`` challenge whose ``resource_metadata``
  pointer is *routable* (following it lands on a 200 metadata document);
* **OAuth metadata paths:** the three host-root path-inserted
  ``/.well-known/*/ytt`` documents (RFC 9728 / 8414 / OIDC alias), issuer and
  endpoints byte-derived from the public URL, every advertised endpoint
  fetchable through the proxy;
* **route inventory over the wire:** ``/ytt/mcp`` is *not* a route (404); the
  documented slash/case/dot-segment normalization behavior; the Traefik
  ``ytt-sse``/``ytt-cors`` middlewares observable in response headers;
* **no transcript work:** after driving every unauthorized/unknown probe, the
  work-path series exposed by ``/ytt/metrics`` (``ytt_fetch_blocks_total``,
  ``ytt_fetch_empty_body_total``, ``ytt_whisper_job_seconds_{count,sum}``,
  ``ytt_cache_bytes``) are unchanged and ``ytt_queue_depth`` is 0 — the
  deployed-chain counterpart of the in-process spy assertion in
  ``tests/unit/test_endpoint_contract.py::test_operational_routes_never_trigger_transcript_work``.

What the other suites hold, and what this one adds:

* ``tests/unit/test_endpoint_contract.py`` drives the real ASGI app in-process
  (auth, route inventory, side-effect set) — it cannot see the ingress;
* ``tests/unit/test_ingressroute_visibility.py`` pins the IngressRoute
  *manifest* — it cannot see what Traefik actually does with traffic;
* this module pins the **composed chain**: a lost ``.well-known`` priority, a
  stripped path prefix, a dropped middleware, or an app/ingress disagreement
  all fail here even though every in-process and manifest-level test stays
  green (self-hosting.md "Step 7" is exactly this walk, automated).

**Opt-in, never default.** These tests hit live infrastructure. They skip
unless ``YTT_DEPLOYED_SMOKE_URL`` names the deployment origin (scheme + host,
no path) — so no default gate (``scripts/definition-of-done.sh``, the Docker
build's plain pytest run) ever depends on the deployed cluster being up, and a
run can never silently point at the reference deployment. Run:

    YTT_DEPLOYED_SMOKE_URL=https://mcp.ardenone.com \
      uv run pytest tests/deployed -m deployed -v

``YTT_DEPLOYED_SMOKE_PREFIX`` overrides the path prefix (default ``/ytt``, the
reference ``YTT_PATH_PREFIX``) for driving a different deployment.

**Credentials: none, by design.** The whole protected surface is asserted from
the *unauthenticated* side (401 shape, challenge pointer, no leakage); the
entire value of the no-work leg is that these probes are exactly the requests
an unauthorized caller can make. No bearer token, allowlist subject, or
upstream IdP value is needed or accepted here.

**Known chain divergence (asserted, not hidden):** the app-level contract says
double slashes do not collapse (``/ytt//metrics`` → 404 in-process, pinned by
``test_endpoint_contract.py``). The deployed chain *normalizes* them one hop
upstream of the app (edge/tunnel layer), so ``/ytt//metrics`` resolves onto the
same public route (200). That is safe here — the security property this suite
asserts is that the normalized spelling of a *protected* path still gates
(``/ytt//admin/egress`` → 401, no bypass) — but it is a real app-vs-chain
difference, kept deliberate and asserted as such rather than quietly matching
either side.

**Concurrency caveat:** the no-work leg compares series snapshots taken before
and after its probes. A *legitimate* concurrent user could move them mid-run;
that is not something these probes can cause (every probe is unauthorized or
unknown, and the route inventory bars all of those from the transcript path).
If this leg goes red with ``after > before``, re-run before debugging the
deployment.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark = pytest.mark.deployed

#: Longest wait for any single hop of the chain (edge TLS, tunnel, Traefik,
#: app). Generous — these are live-internet requests from wherever the suite
#: runs — but bounded so a hung hop fails instead of stalling the gate.
REQUEST_TIMEOUT_SEC = 20.0

BASE_URL_VAR = "YTT_DEPLOYED_SMOKE_URL"
PREFIX_VAR = "YTT_DEPLOYED_SMOKE_PREFIX"
DEFAULT_PREFIX = "/ytt"  # the reference deployment's YTT_PATH_PREFIX, no slash

#: Every label the metrics exposition is allowed to carry (http-endpoints.md
#: "bounded label surface"; ``le``/``quantile`` are the exposition library's
#: own structural bucket labels, exempted exactly like the unit contract does).
ALLOWED_METRIC_LABELS = frozenset(
    {"outcome", "reason", "subject_hash", "le", "quantile"}
)

#: Work-path series the no-transcript-work leg pins unchanged. Labeled series
#: are summed across their label values; the rest are single-series.
WORK_SERIES = (
    "ytt_fetch_blocks_total",
    "ytt_fetch_empty_body_total",
    "ytt_whisper_job_seconds_count",
    "ytt_whisper_job_seconds_sum",
    "ytt_cache_bytes",
)

#: Statuses an unauthorized or unknown request may lawfully produce: the 401
#: challenges, the documented 400s (OAuth handler rejects), 404 inventory,
#: 307 normalization redirects, 405 method discipline. *Nothing else* — in
#: particular no 2xx outside the explicitly public reads, which the probe list
#: itself bounds (metrics scrapes and metadata documents only).
_ALLOWED_PROBE_STATUSES = frozenset({200, 307, 400, 401, 404, 405})

_IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_SAMPLE_RE = re.compile(
    r"^(?P<metric>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>\S+)"
    r"(?:\s+\d+)?\s*$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


# ---------------------------------------------------------------------------
# fixtures — the skip-unset guard lives here, so the whole directory is a
# no-op for every default (gate, image build, drive-by pytest) run
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_url() -> str:
    """The deployment origin (scheme + host, no path), from the environment."""
    raw = os.environ.get(BASE_URL_VAR, "").strip()
    if not raw:
        pytest.skip(
            f"deployed-ingress smoke is opt-in: set {BASE_URL_VAR}=<origin> "
            "(e.g. https://mcp.ardenone.com) to drive the live deployment"
        )
    parts = urlsplit(raw)
    assert parts.scheme in ("http", "https") and parts.hostname, (
        f"{BASE_URL_VAR} must be a scheme+host origin, got {raw!r}"
    )
    return f"{parts.scheme}://{parts.netloc}"


@pytest.fixture(scope="module")
def prefix() -> str:
    """The deployed path prefix (default: the reference ``/ytt``)."""
    raw = os.environ.get(PREFIX_VAR, DEFAULT_PREFIX).strip()
    assert raw.startswith("/") and not raw.endswith("/"), (
        f"{PREFIX_VAR} must look like {DEFAULT_PREFIX} (leading slash, no trailing "
        f"slash), got {raw!r}"
    )
    return raw


@pytest.fixture(scope="module")
def client():
    # follow_redirects=False: the 307 normalization behavior is itself part of
    # the documented surface; tests that want the destination re-request it.
    with httpx.Client(follow_redirects=False, timeout=REQUEST_TIMEOUT_SEC) as c:
        c.headers["user-agent"] = "ytt-deployed-ingress-smoke"
        yield c


def _url(base_url: str, prefix: str, path: str) -> str:
    return f"{base_url}{prefix}{path}"


# ---------------------------------------------------------------------------
# unauthorized / unknown probe set — the exact requests an unauthenticated
# caller can make, shared by the no-work leg (and mirrored per-route above it)
# ---------------------------------------------------------------------------


def _unauthorized_and_unknown_probes(base_url: str, prefix: str):
    """``(method, url, json_body | None)`` for the full unauthorized surface.

    The unauthenticated MCP bodies are complete protocol messages — including a
    ``tools/call`` naming transcript work — not empty payloads. That is the
    strongest form of the "auth happens before any protocol handling" claim:
    even a *valid* message requesting a transcript gets nothing but the 401
    challenge, and the metrics leg below proves no fetch started behind it.
    """
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "ytt-deployed-ingress-smoke", "version": "0"},
        },
    }
    tools_call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "get_transcript",
            "arguments": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        },
    }
    accept = {"accept": "application/json, text/event-stream"}
    return [
        ("POST", _url(base_url, prefix, ""), initialize, accept),  # transport
        ("POST", _url(base_url, prefix, ""), tools_call, accept),  # ... asking for work
        ("POST", _url(base_url, prefix, ""), initialize, {}),  # no SSE accept shape
        ("GET", _url(base_url, prefix, ""), None, {}),  # SSE stream, no token
        ("DELETE", _url(base_url, prefix, ""), None, {}),  # session end, no token
        ("GET", _url(base_url, prefix, "/admin/egress"), None, {}),
        ("GET", _url(base_url, prefix, "/admin/egress/"), None, {}),  # slash variant
        ("POST", f"{base_url}{prefix}/mcp", initialize, accept),  # not a route
        ("GET", f"{base_url}{prefix}/mcp", None, {}),
        ("GET", _url(base_url, prefix, "/unknown-path"), None, {}),
        ("GET", f"{base_url}/YTT/health", None, {}),  # case-sensitive routing
        ("GET", f"{base_url}{prefix}//admin/egress", None, {}),  # // normalized
        ("GET", f"{base_url}{prefix}//metrics", None, {}),  # // normalized (public)
        ("GET", f"{base_url}{prefix}/./metrics", None, {}),  # dot-segment (public)
        ("GET", _url(base_url, prefix, "/metrics/"), None, {}),  # 307 variant
        ("GET", f"{base_url}/.well-known/oauth-protected-resource{prefix}", None, {}),
        ("GET", f"{base_url}/.well-known/oauth-authorization-server{prefix}", None, {}),
        ("GET", f"{base_url}/.well-known/openid-configuration{prefix}", None, {}),
    ]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _scrape_samples(client: httpx.Client, metrics_url: str):
    """Parse the exposition into ``{(metric, sorted labels): value}``."""
    resp = client.get(metrics_url)
    assert resp.status_code == 200, f"metrics scrape failed: {resp.status_code}"
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line in resp.text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        assert m is not None, f"unparseable exposition line: {line!r}"
        labels = tuple(sorted(_LABEL_RE.findall(m.group("labels") or "")))
        key = (m.group("metric"), labels)
        assert key not in samples, f"duplicate sample for {key}"
        samples[key] = float(m.group("value"))
    return samples


def _series_total(samples, name: str) -> float:
    """Sum every sample of ``name`` across its label sets (0 if unexposed)."""
    return sum(v for (metric, _labels), v in samples.items() if metric == name)


def _assert_bearer_challenge(resp: httpx.Response, prm_url: str, what: str) -> None:
    assert resp.status_code == 401, (
        f"{what} answered {resp.status_code}, expected the 401 challenge"
    )
    www_auth = resp.headers.get("www-authenticate", "")
    assert www_auth.startswith("Bearer"), (
        f"{what}: challenge is not Bearer: {www_auth!r}"
    )
    assert f'resource_metadata="{prm_url}"' in www_auth, (
        f"{what}: challenge does not point at the deployed PRM URL {prm_url!r}: {www_auth!r}"
    )


# ---------------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------------


def test_health_is_public_with_fixed_body(client, base_url, prefix):
    """``/ytt/health``: no auth, body exactly ``{"status":"ok"}`` (GET and HEAD)."""
    url = _url(base_url, prefix, "/health")
    resp = client.get(url)
    assert resp.status_code == 200
    # "Body is exactly {"status": "ok"}" — no version, no subject count, no
    # configuration detail (public visibility model: this route is routed to
    # the whole internet).
    assert resp.text == '{"status":"ok"}'
    assert resp.headers["content-type"].startswith("application/json")
    head = client.head(url)
    assert head.status_code == 200
    assert head.text == ""


def test_metrics_are_public_aggregates_with_bounded_labels(client, base_url, prefix):
    """``/ytt/metrics``: public exposition, aggregate-only, bounded labels."""
    url = _url(base_url, prefix, "/metrics")
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")

    samples = _scrape_samples(client, url)
    exposed = {metric for metric, _labels in samples}
    # The registry the docs name is live (the fetch counters exist even while
    # all-zero; the cache gauge is always present).
    for name in (
        "ytt_cache_bytes",
        "ytt_egress_is_residential",
        "ytt_fetch_blocks_total",
    ):
        assert name in exposed, f"{name} missing from the exposition"

    # Bounded label surface: no video ID, URL, or subject email may become a
    # label; subjects appear only as subject_hash.
    for (metric, labels), _value in samples.items():
        if not metric.startswith("ytt_"):
            continue
        bad = sorted(k for k, _v in labels if k not in ALLOWED_METRIC_LABELS)
        assert not bad, f"{metric} carries out-of-contract labels {bad}"

    # And no identity-bearing material in the sample lines themselves.
    for line in resp.text.splitlines():
        if not line.startswith("ytt_"):
            continue
        assert "@" not in line, f"subject-looking material in {line!r}"
        assert "http://" not in line and "https://" not in line, f"URL in {line!r}"

    assert client.head(url).status_code == 200


# ---------------------------------------------------------------------------
# protected surface, from the unauthenticated side
# ---------------------------------------------------------------------------


def test_transport_challenges_unauthenticated_callers_with_a_routable_pointer(
    client, base_url, prefix
):
    """``/ytt`` 401s before any protocol handling, pointing at a live PRM URL."""
    prm_url = f"{base_url}/.well-known/oauth-protected-resource{prefix}"
    resp = client.post(
        _url(base_url, prefix, ""),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ytt-deployed-ingress-smoke", "version": "0"},
            },
        },
        headers={"accept": "application/json, text/event-stream"},
    )
    _assert_bearer_challenge(resp, prm_url, "POST /ytt (initialize)")

    # The pointer the challenge advertises must be *routable through the
    # deployed chain* — a client following it must land on the document.
    followed = client.get(prm_url)
    assert followed.status_code == 200, (
        f"WWW-Authenticate points at {prm_url}, which the chain does not serve "
        f"({followed.status_code}) — discovery derails exactly here"
    )
    assert followed.json()["resource"] == f"{base_url}{prefix}"

    # Traefik's ytt-sse middleware (customResponseHeaders on the ytt router)
    # proves the request traversed the ingress chain, not some other route
    # that happens to answer.
    assert resp.headers.get("cache-control") == "no-cache", (
        "the ytt-sse middleware headers are missing on the transport response — "
        "the IngressRoute/middleware wiring drifted"
    )


def test_transport_get_and_delete_are_gated_too(client, base_url, prefix):
    """GET (SSE stream) and DELETE (session end) challenge like every method."""
    prm_url = f"{base_url}/.well-known/oauth-protected-resource{prefix}"
    for method in ("GET", "DELETE"):
        resp = client.request(method, _url(base_url, prefix, ""))
        _assert_bearer_challenge(resp, prm_url, f"{method} {prefix}")

    # The gate must also survive the trailing-slash redirect: /ytt/ -> /ytt
    # (307, same method) and the destination 401s, not 200s.
    resp = client.get(_url(base_url, prefix, "/"))
    assert resp.status_code == 307
    assert resp.headers["location"].endswith(prefix)
    assert client.get(resp.headers["location"]).status_code == 401


def test_admin_egress_challenges_and_leaks_nothing(client, base_url, prefix):
    """``/ytt/admin/egress``: 401 without a token, and the body carries no probe data."""
    prm_url = f"{base_url}/.well-known/oauth-protected-resource{prefix}"
    resp = client.get(_url(base_url, prefix, "/admin/egress"))
    _assert_bearer_challenge(resp, prm_url, "GET /ytt/admin/egress")
    body = resp.text
    assert "error_code" in body, f"expected the app's JSON error shape, got {body!r}"
    # Unauthenticated, the response must leak neither the egress IP nor any
    # ASN/org detail (those require the bearer gate exactly like a tool call).
    assert not _IPV4_RE.search(body), (
        f"egress IP leaked in unauthenticated body: {body!r}"
    )
    assert '"asn"' not in body and '"org"' not in body, (
        f"egress detail leaked: {body!r}"
    )


def test_preflight_reaches_the_deployed_cors_middleware(client, base_url, prefix):
    """OPTIONS on the transport: the Traefik ytt-cors middleware answers.

    middlewares.yml's allowlist (claude.ai origins, Authorization/Content-Type/
    Accept, GET/POST/OPTIONS, max-age 86400) is the OAuth hop's browser-side
    contract; a middleware dropped from the IngressRoute fails here.
    """
    resp = client.options(
        _url(base_url, prefix, ""),
        headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://claude.ai"
    allow_methods = {
        m.strip() for m in resp.headers["access-control-allow-methods"].split(",")
    }
    assert {"GET", "POST", "OPTIONS"} <= allow_methods
    allow_headers = {
        h.strip() for h in resp.headers["access-control-allow-headers"].split(",")
    }
    assert {"Authorization", "Content-Type", "Accept"} <= allow_headers
    assert resp.headers["access-control-max-age"] == "86400"


# ---------------------------------------------------------------------------
# OAuth metadata paths (host root + path insertion)
# ---------------------------------------------------------------------------


def test_protected_resource_metadata_serves_the_deployed_identity(
    client, base_url, prefix
):
    """RFC 9728: ``resource``/``authorization_servers`` equal the public URL byte-for-byte."""
    resp = client.get(f"{base_url}/.well-known/oauth-protected-resource{prefix}")
    assert resp.status_code == 200
    doc = resp.json()
    public_url = f"{base_url}{prefix}"
    assert doc["resource"] == public_url, (
        f"PRM resource {doc['resource']!r} != deployed public URL {public_url!r} — "
        "the metadata is pointing clients at the wrong deployment"
    )
    assert doc["authorization_servers"] == [public_url]
    assert set(doc["scopes_supported"]) >= {"openid", "email", "offline_access"}
    assert doc["bearer_methods_supported"] == ["header"]


def test_as_metadata_and_oidc_alias_derive_every_endpoint_from_the_issuer(
    client, base_url, prefix
):
    """RFC 8414 + OIDC alias: issuer-prefixed endpoints, every one fetchable."""
    issuer = f"{base_url}{prefix}"
    as_resp = client.get(f"{base_url}/.well-known/oauth-authorization-server{prefix}")
    assert as_resp.status_code == 200
    as_doc = as_resp.json()
    assert as_doc["issuer"] == issuer, (
        f"AS metadata issuer {as_doc['issuer']!r} != deployed public URL {issuer!r}"
    )
    for key, tail in (
        ("authorization_endpoint", "/authorize"),
        ("token_endpoint", "/token"),
        ("registration_endpoint", "/register"),
    ):
        assert as_doc[key] == f"{issuer}{tail}", (
            f"{key} must be the issuer-prefixed public path {issuer}{tail}, "
            f"got {as_doc[key]!r}"
        )

    # The OIDC discovery alias serves the same document.
    oidc_resp = client.get(f"{base_url}/.well-known/openid-configuration{prefix}")
    assert oidc_resp.status_code == 200
    assert oidc_resp.json() == as_doc

    # "Every advertised URL must be fetchable through your proxy": a bare GET
    # of the authorization endpoint reaches ytt's own handler (its documented
    # missing-params 400), not some other service on the shared hostname.
    authorize = client.get(as_doc["authorization_endpoint"])
    assert authorize.status_code == 400, (
        f"GET {as_doc['authorization_endpoint']} answered {authorize.status_code} — "
        "expected ytt's OAuth 400; another route may have captured the path"
    )


def test_upstream_callback_route_reaches_ytt(client, base_url, prefix):
    """``{prefix}/auth/callback`` without a code: ytt's own 400, the routing proof.

    self-hosting.md Step 7 — the redirect URI registered on the upstream IdP
    must land on ytt's OAuth handler; 404 (prefix not forwarded) or a 401 from
    a *different* service both mean the chain lost this path.
    """
    resp = client.get(_url(base_url, prefix, "/auth/callback"))
    assert resp.status_code == 400
    assert "oauth" in resp.text.lower(), (
        f"expected ytt's OAuth error page, got {resp.text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# route inventory over the wire
# ---------------------------------------------------------------------------


def test_ytt_mcp_is_not_a_route(client, base_url, prefix):
    """``{prefix}/mcp`` is a 404: the transport mounts at the prefix root itself."""
    for method in ("POST", "GET"):
        resp = client.request(method, f"{base_url}{prefix}/mcp")
        assert resp.status_code == 404, (
            f"{method} {prefix}/mcp answered {resp.status_code}; the inventory has "
            "no such route and anything else means something new is mounted there"
        )


def test_unknown_and_case_mangled_paths_are_404(client, base_url, prefix):
    """No alias routes: unknown paths and case variants stay 404."""
    for url in (
        _url(base_url, prefix, "/unknown-path"),
        f"{base_url}/YTT/health",  # case-sensitive routing
        _url(base_url, prefix, "/ADMIN/egress"),
    ):
        resp = client.get(url)
        assert resp.status_code == 404, (
            f"{url} answered {resp.status_code}, expected 404"
        )


def test_trailing_slash_normalization_redirects_onto_the_canonical_path(
    client, base_url, prefix
):
    """307 (method+body preserving) to the canonical spelling, per the contract."""
    resp = client.get(_url(base_url, prefix, "/metrics/"))
    assert resp.status_code == 307
    # Path asserted, not scheme: the tunnel hands Traefik an http-shaped
    # Location (observed live); following it still lands on the deployment.
    assert resp.headers["location"].endswith(f"{prefix}/metrics")
    assert client.get(resp.headers["location"]).status_code == 200

    resp = client.get(_url(base_url, prefix, "/admin/egress/"))
    assert resp.status_code == 307
    assert resp.headers["location"].endswith(f"{prefix}/admin/egress")
    # The slash cannot drop the gate: the redirect target still challenges.
    assert client.get(resp.headers["location"]).status_code == 401


def test_double_slash_spelling_normalizes_onto_the_same_routes(
    client, base_url, prefix
):
    """The chain collapses ``//`` one hop upstream of the app (known divergence).

    In-process, Starlette 404s ``{prefix}//metrics`` and the unit contract pins
    that; the deployed edge normalizes duplicate slashes before the app sees
    the request, so the doubled spelling resolves onto the *same* canonical
    route. Safe — asserted via the protected twin below — but deliberately
    documented here instead of matching either side silently.
    """
    resp = client.get(f"{base_url}{prefix}//metrics")
    assert resp.status_code == 200
    assert "ytt_" in resp.text, "normalized //metrics is not the metrics exposition"

    # The security property: normalization must not become a gate bypass —
    # the doubled spelling of the protected route still challenges.
    gated = client.get(f"{base_url}{prefix}//admin/egress")
    assert gated.status_code == 401, (
        "the // spelling of the protected route is NOT gated — edge normalization "
        "has become an auth bypass"
    )
    assert gated.headers.get("www-authenticate", "").startswith("Bearer")


def test_dot_segment_spelling_collapses_onto_the_canonical_route(
    client, base_url, prefix
):
    """``{prefix}/./metrics`` → 200 (documented dot-segment collapse)."""
    resp = client.get(f"{base_url}{prefix}/./metrics")
    assert resp.status_code == 200
    assert "ytt_" in resp.text


# ---------------------------------------------------------------------------
# no transcript work from unauthorized or unknown requests
# ---------------------------------------------------------------------------


def test_no_transcript_work_from_unauthorized_or_unknown_requests(
    client, base_url, prefix
):
    """Drive every probe; the work-path series must not move.

    The deployed-chain counterpart of the in-process spy assertion: none of
    these requests — unauthenticated MCP messages (including a ``tools/call``
    naming a transcript), unknown paths, case/normalization variants, metadata
    reads — may reach the fetch/ASR/cache pipeline. Measured through the same
    public metrics the deployment exposes; see the module docstring for the
    concurrent-legitimate-user caveat.
    """
    metrics_url = _url(base_url, prefix, "/metrics")
    before = _scrape_samples(client, metrics_url)
    assert _series_total(before, "ytt_fetch_blocks_total") == 0.0, (
        "precondition: this leg expects a traffic-free window — the fetch counters "
        "are already nonzero, so the before/after comparison would be ambiguous"
    )

    for method, url, body, headers in _unauthorized_and_unknown_probes(
        base_url, prefix
    ):
        resp = client.request(method, url, json=body, headers=headers)
        assert resp.status_code in _ALLOWED_PROBE_STATUSES, (
            f"{method} {url} answered {resp.status_code} — unexpected shape for an "
            "unauthorized/unknown request"
        )
        if resp.status_code == 200:
            assert "/metrics" in url or "well-known" in url, (
                f"{method} {url} answered 200 — an unauthenticated request reached "
                "a route it must not"
            )

    after = _scrape_samples(client, metrics_url)
    for name in (*WORK_SERIES, "ytt_queue_depth"):
        before_v, after_v = _series_total(before, name), _series_total(after, name)
        assert after_v == before_v, (
            f"{name} moved ({before_v} -> {after_v}) while only unauthorized/unknown "
            "requests were in flight — re-check whether a legitimate request raced "
            "the run before suspecting the probes"
        )
