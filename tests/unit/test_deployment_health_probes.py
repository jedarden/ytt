"""Deployment-level health-probe verification (bead ytt-638b8e2e).

The Kubernetes liveness/readiness probes (``deploy/k8s/ardenone-cluster/ytt/
deployment.yml``), the Docker HEALTHCHECK (``Dockerfile``), and the health
route the code registers are three artifacts that must agree on one URL —
the unauthenticated ``{path_prefix}health`` route on the one port
``ytt serve`` binds — and nothing else keeps them in sync.  The manifest is
hand-maintained (and mirrored byte-for-byte into declarative-config, guarded
by ``test_deploy_parity.py``), the image is built from the Dockerfile, and
the route path is derived at runtime from ``Settings.route("health")``.
Drift between the three is invisible to every other test yet fatal at deploy
time: a probe that 404s (prefix moved), 405s (verb narrowed), or points at a
port nothing binds leaves the single replica permanently not-Ready or
CrashLooping — and because the readiness probe gates the Service endpoints,
its target is also the difference between failing closed (no traffic) and
open (liveness killing a pod readiness still admits).

Three legs:

1. **Manifest geometry** — every ``ytt serve`` container's probes are
   ``httpGet`` on the exact health path built from the *manifest's own*
   ``YTT_PATH_PREFIX`` via the same ``join_path`` the code uses, on the port
   the code binds, with readiness gating no later than liveness; the canary
   container (which runs ``ytt canary`` — no HTTP server on 8080) must not
   probe the health route.
2. **Probe-surface smoke** — through the real ASGI app, the requests a
   kubelet and a Docker daemon actually send answer 200 unauthenticated:
   GET (k8s ``httpGet`` probes) and HEAD (HEAD-throwing load balancers and
   uptime checks — Starlette auto-derives HEAD from the GET-only route
   registration; the body must be dropped while the headers survive).
   POST must 405: an incorrectly configured probe fails closed instead of
   silently passing.
3. **Required-startup-configuration failures** — the documented fail-closed
   env set.  A container missing ``YTT_OAUTH_CLIENT_ID`` /
   ``YTT_OAUTH_CLIENT_SECRET`` / ``YTT_PUBLIC_URL``, or carrying a malformed
   one (public-URL scheme, path-prefix slash rule), must exit 1 *before
   binding*: the process ending nonzero under the image's ``CMD`` is what
   makes kubelet's probes fail and the pod CrashLoop instead of serving a
   half-configured server.  (The docker-based image smoke,
   ``tests/image/test_image_smoke.py``, asserts the same posture against the
   *built* image; this leg pins it where no daemon is needed — the unit
   gate.)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest  # via fastmcp (runtime dependency) — always present in the venv
import yaml
from starlette.testclient import TestClient

from ytt.config import Settings, get_settings, join_path
from ytt.server import build_asgi_app

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_K8S = REPO_ROOT / "deploy" / "k8s"
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
SERVER_SOURCE = (REPO_ROOT / "ytt" / "server.py").read_text(encoding="utf-8")

#: The one port ``ytt serve`` binds. Pinned against the code itself below —
#: if this moves, the manifest probes, containerPort, Service, Dockerfile
#: EXPOSE, and HEALTHCHECK must all move in the same commit.
SERVED_PORT = 8080


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _k8s_documents():
    for path in sorted(DEPLOY_K8S.rglob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict):
                yield path, doc


def _containers_of_kind(kind: str):
    for path, doc in _k8s_documents():
        if doc.get("kind") != kind:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            yield path, doc, container


def _deployments():
    return [(path, doc) for path, doc in _k8s_documents() if doc.get("kind") == "Deployment"]


def _container_command(container) -> list[str]:
    return list(container.get("command") or [])


def _serve_containers():
    """The containers running the MCP server (image CMD re-declared)."""
    return [
        (path, doc, container)
        for path, doc, container in _containers_of_kind("Deployment")
        if _container_command(container) == ["ytt", "serve"]
    ]


def _container_env(container) -> dict[str, str]:
    """Literal env values on a container (``valueFrom`` refs are skipped —
    they carry no probe-relevant path config)."""
    return {
        entry["name"]: entry["value"]
        for entry in container.get("env", [])
        if "value" in entry
    }


def _probe(container, name: str) -> dict:
    probe = container.get(name)
    assert probe is not None, (
        f"{container['name']}: no {name} — the single-replica server has no "
        f"{name.replace('Probe', '')} signal; kubelet can neither restart it "
        "nor gate traffic on actual health"
    )
    return probe


def _expected_health_path(container) -> str:
    """The health path the *rendered* container will serve.

    Derived from the manifest's own ``YTT_PATH_PREFIX`` via the same
    ``join_path`` the code uses (``Settings.route``), so a prefix change in
    either place alone fails here instead of in a CrashLoop.
    """
    env = _container_env(container)
    prefix = env.get("YTT_PATH_PREFIX")
    assert prefix is not None, (
        "deployment.yml does not set YTT_PATH_PREFIX — the probes below are "
        "then checked against the image default only, and the manifest no "
        "longer states the prefix the public route is served under"
    )
    return join_path(prefix, "health")


# ---------------------------------------------------------------------------
# 1. Manifest geometry — the probes point where the code actually serves
# ---------------------------------------------------------------------------


def test_exactly_one_serve_container_under_deploy_k8s():
    serve = _serve_containers()
    assert len(serve) == 1, (
        f"expected exactly one 'ytt serve' container under {DEPLOY_K8S}, "
        f"found {len(serve)}: {[str(p) for p, _, _ in serve]} — a second one "
        "must be added to the probe assertions in this module"
    )


def test_probes_are_http_get_against_the_health_route(monkeypatch):
    """Both probes are httpGet (not tcpSocket/exec) on the exact health path.

    tcpSocket passes while the route is broken (a half-bound uvicorn accepts
    connections before it serves); exec probes bypass the endpoint this
    module verifies; and the path must equal ``Settings.route("health")``
    computed with the manifest's own prefix — the drift that would otherwise
    404 every probe.
    """
    _, _, container = _serve_containers()[0]
    expected = _expected_health_path(container)

    monkeypatch.setenv("YTT_PATH_PREFIX", _container_env(container)["YTT_PATH_PREFIX"])
    assert Settings().route("health") == expected, (
        f"the code's health route for the manifest prefix is "
        f"{Settings().route('health')!r}, not the probed {expected!r}"
    )
    assert expected == "/ytt/health", (
        f"probe target moved to {expected!r} — update deploy/DEPLOY-CHECKLIST.md "
        "§5 and the Dockerfile HEALTHCHECK in the same change"
    )

    for name in ("livenessProbe", "readinessProbe"):
        probe = _probe(container, name)
        assert "httpGet" in probe, f"{name}: not an httpGet probe"
        assert "exec" not in probe and "tcpSocket" not in probe, (
            f"{name}: exec/tcpSocket probes bypass the unauthenticated health "
            "route this module pins"
        )
        assert probe["httpGet"]["path"] == expected, (
            f"{name}: path {probe['httpGet']['path']!r} != the rendered health "
            f"route {expected!r} — every probe on this pod 404s"
        )


def test_probe_ports_match_the_bound_port_everywhere():
    """Probe port == containerPort == Service target == EXPOSE == serve()'s bind.

    ``serve()`` binds ``SERVED_PORT`` in code; the manifest and Dockerfile
    must agree with that *specific* value, so it is pinned against the source
    here rather than trusted.
    """
    assert "port=8080" in SERVER_SOURCE, (
        "server.serve() no longer binds 8080 — update SERVED_PORT here and, "
        "in the same commit, the deployment probes/containerPort, the "
        "Service ports, the Dockerfile EXPOSE and HEALTHCHECK"
    )

    _, _, container = _serve_containers()[0]
    declared = {
        p["containerPort"]
        for p in container.get("ports", [])
        if p.get("name") == "http"
    }
    assert declared == {SERVED_PORT}, (
        f"container 'http' port is {declared}, serve() binds {SERVED_PORT}"
    )

    for name in ("livenessProbe", "readinessProbe"):
        port = _probe(container, name)["httpGet"]["port"]
        assert port == SERVED_PORT, (
            f"{name}: probes port {port}, serve() binds {SERVED_PORT} — "
            "connection-refused on every probe"
        )

    services = [
        (path, doc)
        for path, doc in _k8s_documents()
        if doc.get("kind") == "Service" and doc["metadata"]["name"] == "ytt"
    ]
    assert services, "no Service named 'ytt' — the readiness gate has no endpoints to gate"
    for port_spec in services[0][1]["spec"]["ports"]:
        assert port_spec["targetPort"] == SERVED_PORT and port_spec["port"] == SERVED_PORT, (
            f"Service port spec {port_spec} does not target {SERVED_PORT} — "
            "endpoints would route around the port the probes verify"
        )

    assert re.search(rf"(?m)^EXPOSE\s+{SERVED_PORT}\s*$", DOCKERFILE), (
        f"Dockerfile does not EXPOSE {SERVED_PORT}"
    )


def test_readiness_gates_no_later_than_liveness():
    """Readiness must notice failure before liveness kills the pod.

    A liveness that fires first turns a slow/hung server into a restart loop
    (CrashLoopBackOff) instead of the intended degraded state (pod alive,
    removed from the Service endpoints). The manifest's 5s/10s vs 10s/30s
    shape is the contract.
    """
    _, _, container = _serve_containers()[0]
    live = _probe(container, "livenessProbe")
    ready = _probe(container, "readinessProbe")
    assert ready["initialDelaySeconds"] <= live["initialDelaySeconds"], (
        "liveness starts gating before readiness — a slow boot gets killed "
        "instead of merely being held out of the endpoints"
    )
    assert ready["periodSeconds"] <= live["periodSeconds"], (
        "readiness polls slower than liveness — traffic is drained/restored "
        "on a coarser clock than restart decisions"
    )
    for name in ("livenessProbe", "readinessProbe"):
        probe = _probe(container, name)
        assert probe.get("periodSeconds", 10) >= 1 and probe.get("failureThreshold", 3) >= 1, (
            f"{name}: degenerate period/failureThreshold"
        )


def test_canary_container_does_not_probe_the_health_route():
    """The canary runs `ytt canary` — no HTTP server on 8080 — so a copy-pasted
    /ytt/health probe would never answer and permanently CrashLoop the pod."""
    health_path = _expected_health_path(_serve_containers()[0][2])
    for path, _, container in _containers_of_kind("Deployment"):
        if _container_command(container) == ["ytt", "serve"]:
            continue
        for name in ("livenessProbe", "readinessProbe"):
            probe = container.get(name)
            if probe is None:
                continue
            probed = probe.get("httpGet", {}).get("path", "")
            assert probed != health_path, (
                f"{path.name}:{container['name']}: {name} targets {health_path!r} "
                f"but this container runs {_container_command(container)} — it "
                "serves no HTTP health route; probe the surface it actually "
                "exposes (see the manifest's own comment)"
            )


def test_dockerfile_healthcheck_targets_the_same_route():
    """The container-level probe (docker run / compose; k8s ignores it) hits
    the same unauthenticated health route, prefix-aware, on the bound port —
    and the manifest re-declares the image's exact CMD.

    The HEALTHCHECK's ``python -c`` URL expression is parsed and *evaluated*
    rather than grepped for keywords: host:port, the documented default
    prefix, and the route segment must compose (via ``join_path``) to the
    same route under both the default and the manifest's own prefix — the
    manifest's env is what the container actually runs with, so it feeds
    both this URL and the code's route construction inside the container.
    """
    assert 'CMD ["ytt", "serve"]' in DOCKERFILE, "runtime CMD is not the server"

    match = re.search(r"(?ms)^HEALTHCHECK.*?(?=^[A-Z]|\Z)", DOCKERFILE)
    assert match, (
        "Dockerfile has no HEALTHCHECK — docker run / compose self-hosters "
        "get no health status (`docker ps`) and no container-level restart "
        "signal outside k8s"
    )
    healthcheck = match.group(0)

    cmd_match = re.search(r'python -c\s+"([^"]+)"', healthcheck)
    assert cmd_match, "HEALTHCHECK CMD is not a double-quoted python -c expression"
    probe_src = cmd_match.group(1)

    # GET and unauthenticated by construction: urlopen issues a plain GET
    # with no credentials — the same request shape the kubelet probes send
    # (driven against the app above). A HEAD or an Authorization header here
    # would make docker and kubelet probe different surfaces.
    assert "urlopen" in probe_src, (
        "HEALTHCHECK no longer probes via urlopen (GET, unauthenticated) — "
        "it must send the same request the k8s httpGet probes do"
    )
    assert "Authorization" not in probe_src and "token" not in probe_src, (
        "HEALTHCHECK must stay unauthenticated — /ytt/health takes no auth"
    )

    url_parts = re.search(
        r"urlopen\(\s*'([^']*)'\s*\+\s*os\.environ\.get\("
        r"\s*'YTT_PATH_PREFIX'\s*,\s*'([^']*)'\s*\)\s*\+\s*'([^']*)'",
        probe_src,
    )
    assert url_parts, (
        "HEALTHCHECK must build the URL as 'http://127.0.0.1:<port>' + "
        "os.environ.get('YTT_PATH_PREFIX', <default>) + 'health' — a hardcoded "
        "path breaks non-default-prefix deployments, an env-less one breaks "
        "self-hosters relying on the documented default"
    )
    host, default_prefix, segment = url_parts.groups()
    assert host == f"http://127.0.0.1:{SERVED_PORT}", (
        f"HEALTHCHECK dials {host!r}, serve() binds {SERVED_PORT}"
    )
    assert join_path(default_prefix, segment) == "/ytt/health", (
        f"HEALTHCHECK's default prefix resolves to "
        f"{join_path(default_prefix, segment)!r}, not the documented /ytt/health"
    )

    _, _, container = _serve_containers()[0]
    manifest_prefix = _container_env(container)["YTT_PATH_PREFIX"]
    expected = _expected_health_path(container)
    assert join_path(manifest_prefix, segment) == expected, (
        f"HEALTHCHECK under the manifest's prefix probes "
        f"{join_path(manifest_prefix, segment)!r} while the k8s probes target "
        f"{expected!r} — docker and kubelet would disagree about health"
    )


# ---------------------------------------------------------------------------
# 2. Probe-surface smoke — the requests the kubelet/docker daemon send
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    """The real ASGI app, built against a freshly-read Settings.

    ``get_settings`` is ``lru_cache``d process-wide, and this module may run
    after others in the same pytest process — clear the cache on both ends so
    the smoke legs exercise the app the ambient (conftest-defaulted) env
    actually configures, never a Settings some earlier module left behind.
    """
    get_settings.cache_clear()
    yield build_asgi_app()
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_liveness_readiness_probe_succeeds_unauthenticated(client):
    """The exact request both k8s probes send — GET, no Authorization header —
    returns 200 {"status": "ok"} on the probed path."""
    _, _, container = _serve_containers()[0]
    probe_path = _expected_health_path(container)

    resp = client.get(probe_path, headers={"Authorization": ""})
    assert resp.status_code == 200, f"probe GET {probe_path} -> {resp.status_code}"
    assert resp.json() == {"status": "ok"}
    assert resp.headers["content-type"].startswith("application/json")


def test_head_probe_succeeds_with_dropped_body(client):
    """HEAD on the probed path: 200, empty body, GET's content-length intact.

    Load balancers and uptime checks commonly probe with HEAD; Starlette
    auto-allows it for the GET-registered route.  A 405 or a kept body here
    would fail exactly the tooling that never sends GET.
    """
    _, _, container = _serve_containers()[0]
    probe_path = _expected_health_path(container)

    get_body = client.get(probe_path).text
    head = client.head(probe_path)
    assert head.status_code == 200, f"HEAD {probe_path} -> {head.status_code}"
    assert head.content == b"", f"HEAD must not carry a body, got {head.content!r}"
    assert head.headers.get("content-length") == str(len(get_body)), (
        "HEAD dropped the content-length the GET body implies — proxy/probe "
        "clients that size the response from HEAD see a different endpoint"
    )


def test_wrong_verb_fails_closed(client):
    """POST on the probed path must 405 — a misconfigured probe fails instead
    of silently passing against a route that does not implement the verb."""
    _, _, container = _serve_containers()[0]
    assert client.post(_expected_health_path(container), json={}).status_code == 405


# ---------------------------------------------------------------------------
# 3. Required-startup-configuration failures — probes must fail closed
# ---------------------------------------------------------------------------

#: The documented minimal-valid env (image smoke's fail-closed harness shape).
#: Each case removes or corrupts one variable; the server must exit 1 before
#: binding — under the image CMD that is a dead container, so kubelet's probes
#: fail and the pod CrashLoops instead of serving half-configured OAuth.
_VALID_STARTUP_ENV = {
    "YTT_PUBLIC_URL": "https://ytt.example.com/ytt",
    "YTT_PATH_PREFIX": "/ytt/",
    "YTT_OAUTH_CLIENT_ID": "smoke-client",
    "YTT_OAUTH_CLIENT_SECRET": "smoke-secret",
}

#: (case id, env overrides — None deletes the variable, expected message
#: fragment or None for "any").  Messages are the exact documented strings
#: the fail-closed checks raise; the missing-secret case is enforced by
#: fastmcp's token-verifier construction, so only the exit code is pinned.
_FAIL_CLOSED_CASES = [
    (
        "missing-oauth-client-id",
        {"YTT_OAUTH_CLIENT_ID": None},
        "YTT_OAUTH_CLIENT_ID is required",
    ),
    ("missing-oauth-client-secret", {"YTT_OAUTH_CLIENT_SECRET": None}, None),
    (
        "missing-public-url",
        {"YTT_PUBLIC_URL": None},
        "YTT_PUBLIC_URL is required",
    ),
    (
        "malformed-public-url",
        {"YTT_PUBLIC_URL": "mcp.example.com/ytt"},
        "YTT_PUBLIC_URL must use http:// or https://",
    ),
    (
        "malformed-path-prefix",
        {"YTT_PATH_PREFIX": "ytt"},
        "YTT_PATH_PREFIX must end with '/'",
    ),
]


def _startup_env(overrides: dict[str, str | None]) -> dict[str, str]:
    """The test host's env with every YTT_* stripped, then the documented
    minimal-valid set applied — so the case under test is the *only* config
    difference, never ambient host state."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YTT_")}
    env.update(_VALID_STARTUP_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


@pytest.mark.parametrize("case_id,overrides,expected_message", _FAIL_CLOSED_CASES)
def test_required_startup_config_failure_exits_before_binding(
    case_id: str, overrides: dict[str, str | None], expected_message: str | None
):
    """`python -m ytt serve` (the image CMD's entrypoint) exits 1, printing the
    documented error, without ever binding — a probe can only fail here."""
    run = subprocess.run(
        [sys.executable, "-m", "ytt", "serve"],
        env=_startup_env(overrides),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = run.stdout + run.stderr
    assert run.returncode == 1, (
        f"[{case_id}] expected exit 1 before binding, got {run.returncode}:\n"
        f"{output[-2000:]}"
    )
    if expected_message is not None:
        assert expected_message in output, (
            f"[{case_id}] exit was 1 but the documented error is absent:\n"
            f"{output[-2000:]}"
        )
