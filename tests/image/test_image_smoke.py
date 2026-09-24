"""Built-image self-hosting smoke test (bead ytt-cf8157a2).

Drives the documented Docker quick start (README "Quick start (self-hosted)",
docs/usage/self-hosting.md) against a *built* ytt image and asserts the three
behaviors the docs promise:

1. **Fail closed** — no OAuth client configuration → the server exits 1 and
   never binds (``YTT_OAUTH_CLIENT_ID`` / ``YTT_OAUTH_CLIENT_SECRET`` are
   startup-required; README: "the server exits 1 without it").
2. **Boots with the minimal documented config** — health, the mounted ``/ytt/``
   MCP transport, and the root-level OAuth discovery documents respond exactly
   as documented (self-hosting.md "OAuth discovery" + "Smoke testing").
3. **Credentials are runtime-only** — the OAuth client secret passed at
   ``docker run`` time appears nowhere in the image's own configuration.

Everything runs through the ``docker`` CLI (``DOCKER_HOST`` is honored, so a
remote daemon works), and the upstream IdP is a **stub served by this module**
over TLS: FastMCP's ``OIDCProxy`` performs a live discovery fetch at startup
(tests/conftest.py docstring), so a boot test needs *some* reachable HTTPS
issuer, and the smoke test must not depend on the reference Authentik or any
other real IdP. The client id/secret are throwaway values generated per run —
no real credential ever enters this file, the image, or the daemon's config.

Prerequisites (everything else skips cleanly):
  * a reachable docker daemon (``docker info``)
  * a built image ref: ``YTT_SMOKE_IMAGE`` (default: the ``VERSION``-pinned
    published tag ``ronaldraygun/ytt:X.Y.Z`` — a private Hub repo, so either
    ``docker login`` first or point ``YTT_SMOKE_IMAGE`` at a local build)
  * ``openssl`` on the test host (self-signed stub-IdP certificate)

Run:  ``YTT_SMOKE_IMAGE=<ref> uv run pytest tests/image -m integration``
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

#: Longest wait for the booted container to answer /ytt/health. Covers the
#: startup egress probe (selftest._PROBE_TIMEOUT_SEC = 10 s, tolerated to fail)
#: plus uvicorn bind on a cold daemon.
BOOT_TIMEOUT_SEC = 90.0

_DOCKER_TIMEOUT_SEC = 120


# ---------------------------------------------------------------------------
# docker plumbing
# ---------------------------------------------------------------------------


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a docker CLI command, raising a clear skip if no daemon answers."""
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        pytest.skip("docker CLI not installed — skipping built-image smoke test")
    except subprocess.TimeoutExpired:
        pytest.skip(f"docker {' '.join(args[:2])} timed out — skipping smoke test")


def _container_logs(name: str) -> str:
    logs = _docker("logs", name)
    return (logs.stdout + logs.stderr)[-4000:]


@pytest.fixture(scope="module")
def image() -> str:
    """The built image under test, present locally.

    Defaults to the published tag pinned to this tree's VERSION (YTT_SMOKE_IMAGE
    overrides) — the same ref the README quick start and self-hosting compose
    example pin — so the default run verifies the documented quick start
    *literally*.
    """
    ref = os.environ.get(
        "YTT_SMOKE_IMAGE",
        f"ronaldraygun/ytt:{(Path(__file__).parents[2] / 'VERSION').read_text().strip()}",
    )
    if _docker("image", "inspect", ref).returncode != 0:
        pull = _docker("pull", ref)
        if pull.returncode != 0:
            pytest.fail(
                f"image {ref} not available locally and pull failed:\n"
                f"{pull.stderr[-1000:]}\n"
                "(ronaldraygun/* is a private Docker Hub repo — docker login "
                "first, or set YTT_SMOKE_IMAGE to a locally built tag)"
            )
    return ref


@pytest.fixture(scope="module", autouse=True)
def require_docker_daemon():
    """Skip the whole module when no docker daemon is reachable.

    The default unit gate (``-m "not integration"``) never collects this
    module; this guard additionally keeps an explicit ``-m integration`` run
    honest on hosts (kaniko builds, clean extractions) with no runtime.
    """
    info = _docker("info", "--format", "{{.ServerVersion}}")
    if info.returncode != 0:
        pytest.skip(
            f"no reachable docker daemon — skipping smoke test: {info.stderr[-300:]}"
        )


# ---------------------------------------------------------------------------
# stub OIDC IdP (startup discovery only)
# ---------------------------------------------------------------------------


class _StubIdp:
    """Minimal HTTPS OIDC discovery endpoint for the container to boot against.

    FastMCP's strict OIDCConfiguration requires issuer, authorization_endpoint,
    token_endpoint, jwks_uri, response_types_supported, subject_types_supported
    and id_token_signing_alg_values_supported — nothing else is fetched at
    startup (the HS256-by-client-secret verifier never touches the JWKS).
    """

    def __init__(self, workdir: Path) -> None:
        self.cert = workdir / "idp-cert.pem"
        self.key = workdir / "idp-key.pem"
        # The container dials the host via host.docker.internal (see the
        # docker run flags below); the SAN must match that hostname.
        gen = subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(self.key),
                "-out",
                str(self.cert),
                "-days",
                "3",
                "-nodes",
                "-subj",
                "/CN=ytt-smoke-stub-idp",
                "-addext",
                "subjectAltName=DNS:host.docker.internal,IP:127.0.0.1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if gen.returncode != 0:
            pytest.skip(
                f"openssl unavailable/failed — skipping smoke test: {gen.stderr[-300:]}"
            )
        # The container reads the cert as uid 10001 — don't inherit a
        # restrictive host umask on the bind-mounted file.
        self.cert.chmod(0o644)

        with socket.socket() as s:
            s.bind(("0.0.0.0", 0))
            self.port = s.getsockname()[1]
        # https is mandatory: Settings rejects an http OIDC issuer outright
        # (OIDC Core §3.1.2.1), so the stub must serve TLS.
        self.issuer = f"https://host.docker.internal:{self.port}/"
        self._srv = HTTPServer(("0.0.0.0", self.port), self._handler())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self._srv.socket = ctx.wrap_socket(self._srv.socket, server_side=True)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        doc = {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + "authorize/",
            "token_endpoint": self.issuer + "token/",
            "jwks_uri": self.issuer + "jwks/",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["HS256"],
        }
        body = json.dumps(doc).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        return Handler

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


# ---------------------------------------------------------------------------
# booted container (documented quick start, minimal valid config)
# ---------------------------------------------------------------------------


class BootedContainer:
    """The running quick-start container plus the values it was given."""

    def __init__(self, base_url: str, name: str, client_secret: str) -> None:
        self.base_url = base_url
        self.name = name
        self.client_secret = client_secret


@pytest.fixture(scope="module")
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def booted(image: str, _free_port: int, tmp_path_factory: pytest.TempPathFactory):
    """Boot the built image with the minimal documented configuration.

    Mirrors the README quick start `docker run -e ...` list; the only
    non-README env is SSL_CERT_FILE, which teaches the container's discovery
    fetch to trust this test's own stub IdP (harness plumbing, not a
    documented ytt setting). The OAuth client pair is generated per run and
    enters the container solely via `-e` — runtime injection only.
    """
    workdir = tmp_path_factory.mktemp("ytt-image-smoke")
    idp = _StubIdp(workdir)
    client_id = "smoke-client-" + secrets.token_hex(4)
    client_secret = secrets.token_urlsafe(24)
    public_url = f"http://127.0.0.1:{_free_port}/ytt"
    name = "ytt-smoke-" + secrets.token_hex(4)

    env = {
        "YTT_PUBLIC_URL": public_url,
        "YTT_PATH_PREFIX": "/ytt/",
        "YTT_ALLOWED_SUBJECTS": "smoke-subject",
        "YTT_OAUTH_CLIENT_ID": client_id,
        "YTT_OAUTH_CLIENT_SECRET": client_secret,
        # Documented BYO-IdP override (self-hosting.md); here: the stub above.
        "YTT_OIDC_ISSUER": idp.issuer,
        # Documented no-Whisper mode: an unreachable endpoint disables ASR.
        "YTT_WHISPER_URL": "http://127.0.0.1:9",
        # Harness plumbing: trust the stub IdP's self-signed certificate.
        "SSL_CERT_FILE": "/ytt-smoke-idp-cert.pem",
    }

    run = _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{_free_port}:8080",
        "--add-host=host.docker.internal:host-gateway",
        "-v",
        f"{idp.cert}:/ytt-smoke-idp-cert.pem:ro",
        *[f"-e{k}={v}" for k, v in env.items()],
        image,
    )
    if run.returncode != 0:
        idp.stop()
        pytest.fail(f"docker run failed:\n{run.stderr[-1000:]}")

    base_url = public_url
    booted_container = BootedContainer(base_url, name, client_secret)
    try:
        deadline = time.monotonic() + BOOT_TIMEOUT_SEC
        last_err = ""
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{base_url}/ytt/health", timeout=2.0)
                if r.status_code == 200:
                    break
                last_err = f"health {r.status_code}"
            except httpx.HTTPError as exc:
                last_err = str(exc)[:300]
            time.sleep(1.0)
        else:
            logs = _container_logs(name)
            _docker("rm", "-f", name)
            pytest.fail(
                f"container did not become healthy within {BOOT_TIMEOUT_SEC:.0f}s "
                f"(last error: {last_err})\ncontainer logs:\n{logs}"
            )
        yield booted_container
    finally:
        _docker("rm", "-f", name)
        idp.stop()


# ---------------------------------------------------------------------------
# 1. fail closed without OAuth client configuration
# ---------------------------------------------------------------------------


def _fail_closed_run(image: str, missing: str) -> subprocess.CompletedProcess[str]:
    """`docker run` the quick start with one OAuth client variable removed."""
    base = {
        "YTT_PUBLIC_URL": "http://127.0.0.1:18080/ytt",
        "YTT_PATH_PREFIX": "/ytt/",
        "YTT_ALLOWED_SUBJECTS": "smoke-subject",
        "YTT_OAUTH_CLIENT_ID": "smoke-client",
        "YTT_OAUTH_CLIENT_SECRET": "smoke-secret",
        # The fail-closed checks raise before the discovery fetch, so no IdP
        # (stub or real) is needed here — the issuer only has to be https.
        "YTT_OIDC_ISSUER": "https://stub-idp.invalid/application/o/ytt/",
    }
    base.pop(missing)
    return _docker(
        "run",
        "--rm",
        *[f"-e{k}={v}" for k, v in base.items()],
        image,
    )


def test_missing_oauth_client_id_fails_closed(image: str):
    """Without YTT_OAUTH_CLIENT_ID the server must exit 1 (README: fail closed)."""
    run = _fail_closed_run(image, "YTT_OAUTH_CLIENT_ID")
    output = run.stdout + run.stderr
    assert run.returncode == 1, (
        f"expected exit 1, got {run.returncode}:\n{output[-2000:]}"
    )
    assert "YTT_OAUTH_CLIENT_ID is required" in output, (
        f"exit was 1 but the documented missing-client-id error is absent:\n{output[-2000:]}"
    )


def test_missing_oauth_client_secret_fails_closed(image: str):
    """Without YTT_OAUTH_CLIENT_SECRET the server must exit 1 (the pair is
    startup-required — the secret keys the HS256 token verifier)."""
    run = _fail_closed_run(image, "YTT_OAUTH_CLIENT_SECRET")
    output = run.stdout + run.stderr
    assert run.returncode == 1, (
        f"expected exit 1, got {run.returncode}:\n{output[-2000:]}"
    )


# ---------------------------------------------------------------------------
# 2. minimal valid config boots; documented endpoints respond
# ---------------------------------------------------------------------------


def test_quick_start_health(booted: BootedContainer):
    """self-hosting.md "Smoke testing": /ytt/health → {"status": "ok"}."""
    r = httpx.get(f"{booted.base_url}/ytt/health", timeout=10.0)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_quick_start_mcp_transport_requires_auth(booted: BootedContainer):
    """The MCP transport mounted at /ytt must reject anonymous clients with the
    documented 401 + WWW-Authenticate resource_metadata pointer (README
    "Auth required"; server.py's challenge shape)."""
    r = httpx.get(f"{booted.base_url}/ytt", timeout=10.0)
    assert r.status_code == 401, f"expected 401 on the MCP mount, got {r.status_code}"
    www_auth = r.headers.get("www-authenticate", "")
    expected_metadata = (
        f"{booted.base_url.rsplit('/', 1)[0]}/.well-known/oauth-protected-resource/ytt"
    )
    assert www_auth.startswith("Bearer"), www_auth
    assert 'resource_metadata="' + expected_metadata + '"' in www_auth, (
        f"resource_metadata pointer {expected_metadata!r} missing from: {www_auth}"
    )


def test_quick_start_oauth_protected_resource_metadata(booted: BootedContainer):
    """self-hosting.md: PRM at /.well-known/oauth-protected-resource/ytt with
    resource == YTT_PUBLIC_URL exactly."""
    r = httpx.get(
        f"{booted.base_url.rsplit('/', 1)[0]}/.well-known/oauth-protected-resource/ytt",
        timeout=10.0,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == booted.base_url
    assert body["authorization_servers"] == [booted.base_url]


def test_quick_start_oauth_authorization_server_metadata(booted: BootedContainer):
    """self-hosting.md: AS metadata at /.well-known/oauth-authorization-server/ytt
    with issuer == YTT_PUBLIC_URL exactly."""
    r = httpx.get(
        f"{booted.base_url.rsplit('/', 1)[0]}/.well-known/oauth-authorization-server/ytt",
        timeout=10.0,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["issuer"] == booted.base_url
    assert body["authorization_endpoint"].startswith(booted.base_url)


# ---------------------------------------------------------------------------
# 3. credentials stay runtime-injected
# ---------------------------------------------------------------------------


def test_runtime_secret_absent_from_image_config(image: str, booted: BootedContainer):
    """The OAuth client secret must exist only in the container's runtime env.

    The image itself is generic (Dockerfile: "NO ardenone specifics are baked
    in") — assert the secret passed via -e never appears in the image's own
    configuration, i.e. the only injection path is runtime env.
    """
    inspect = _docker("image", "inspect", "--format", "{{json .Config.Env}}", image)
    assert inspect.returncode == 0, inspect.stderr
    image_env = json.loads(inspect.stdout)
    leaked = [entry for entry in image_env if booted.client_secret in entry]
    assert not leaked, f"runtime client secret leaked into image config: {leaked}"
