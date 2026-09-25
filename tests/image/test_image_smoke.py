"""Built-image self-hosting smoke test (bead ytt-cf8157a2).

Drives the documented Docker quick start (README "Quick start (self-hosted)",
docs/usage/self-hosting.md) against a *built* ytt image and asserts the three
behaviors the docs promise:

1. **Fail closed** — no OAuth client configuration → the server exits 1 and
   never binds (``YTT_OAUTH_CLIENT_ID`` / ``YTT_OAUTH_CLIENT_SECRET`` are
   startup-required; README: "the server exits 1 without it"). Same for
   ``YTT_PUBLIC_URL`` — startup-required with **no** fallback (bead
   ``ytt-a1fbc575``): unset or malformed, the server must exit 1 rather
   than emit OAuth metadata targeting the reference deployment.
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
  * the ``cryptography`` package (already in the project's dependency graph —
    it signs the stub IdP's self-signed certificate)

Network mode (``YTT_SMOKE_NETWORK``, default ``bridge``): the booted container
must reach the stub IdP on the test host. ``bridge`` uses the documented
``-p`` publish shape plus ``--add-host=host.docker.internal:host-gateway``.
Hosts whose firewall blocks container→host traffic over the docker bridge
(NixOS with a default nftables ruleset, among others) make that path time out;
set ``YTT_SMOKE_NETWORK=host`` to share the host network namespace instead —
the server's port is hardcoded to 8080 (``server.serve``), so host mode
requires host port 8080 to be free.

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

#: The server's port is hardcoded in ``server.serve`` — in ``host`` network
#: mode this is also the host port, so it must be free.
_SERVER_PORT = 8080


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

    def __init__(self, workdir: Path, network_mode: str) -> None:
        self.cert = workdir / "idp-cert.pem"
        self.key = workdir / "idp-key.pem"
        self._generate_self_signed_cert()
        # The container reads the cert as uid 10001 — don't inherit a
        # restrictive host umask on the bind-mounted file.
        self.cert.chmod(0o644)
        self.key.chmod(0o600)

        with socket.socket() as s:
            s.bind(("0.0.0.0", 0))
            self.port = s.getsockname()[1]
        # The issuer URL as the container itself resolves it, per network mode.
        host = "127.0.0.1" if network_mode == "host" else "host.docker.internal"
        self.issuer = f"https://{host}:{self.port}/"
        # https is mandatory: Settings rejects an http OIDC issuer outright
        # (OIDC Core §3.1.2.1), so the stub must serve TLS.
        self._srv = HTTPServer(("0.0.0.0", self.port), self._handler())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self._srv.socket = ctx.wrap_socket(self._srv.socket, server_side=True)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def _generate_self_signed_cert(self) -> None:
        """Sign a throwaway cert covering both dialable identities: the
        host-gateway hostname (bridge mode) and 127.0.0.1 (host mode)."""
        # Imported lazily so a venv without the dep skips instead of erroring
        # at collection; cryptography ships with the project's dependency
        # graph (fastmcp/mcp), so real environments always have it.
        try:
            import datetime
            import ipaddress

            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError as exc:
            pytest.skip(f"cryptography unavailable — skipping smoke test: {exc}")

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "ytt-smoke-stub-idp")]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("host.docker.internal"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        doc = json.dumps(
            {
                "issuer": self.issuer,
                "authorization_endpoint": self.issuer + "authorize/",
                "token_endpoint": self.issuer + "token/",
                "jwks_uri": self.issuer + "jwks/",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["HS256"],
            }
        ).encode()
        body = doc

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _boot_container(
    image: str,
    network_mode: str,
    idp: _StubIdp,
    env: dict[str, str],
    name: str,
    publish_port: int,
) -> tuple[bool, str, str]:
    """`docker run` the minimal documented quick start and wait for health.

    Returns ``(healthy, base_url, failure_logs)``. The container is left
    running on success; on failure it is removed and its logs returned
    (no ``--rm``: the daemon would eat the logs before we can read them).
    """
    if network_mode == "host":
        base_url = f"http://127.0.0.1:{_SERVER_PORT}/ytt"
        run_args: list[str] = ["--network", "host"]
    else:
        base_url = f"http://127.0.0.1:{publish_port}/ytt"
        run_args = [
            "-p",
            f"127.0.0.1:{publish_port}:{_SERVER_PORT}",
            "--add-host=host.docker.internal:host-gateway",
        ]

    run = _docker(
        "run",
        "-d",
        "--name",
        name,
        *run_args,
        "-v",
        f"{idp.cert}:/ytt-smoke-idp-cert.pem:ro",
        *[f"-e{k}={v}" for k, v in env.items()],
        image,
    )
    if run.returncode != 0:
        return False, base_url, f"docker run failed:\n{run.stderr[-1000:]}"

    deadline = time.monotonic() + BOOT_TIMEOUT_SEC
    last_err = ""
    while time.monotonic() < deadline:
        # Exit early if the container already died — with no --rm we can
        # still read its logs below instead of staring at connection refusals.
        state = _docker("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name)
        if state.returncode != 0 or state.stdout.strip().startswith("False"):
            break
        try:
            r = httpx.get(f"{base_url}/health", timeout=2.0)
            if r.status_code == 200:
                return True, base_url, ""
            last_err = f"health {r.status_code}"
        except httpx.HTTPError as exc:
            last_err = str(exc)[:300]
        time.sleep(1.0)

    logs = _container_logs(name)
    _docker("rm", "-f", name)
    return (
        False,
        base_url,
        (
            f"container did not become healthy within {BOOT_TIMEOUT_SEC:.0f}s "
            f"(last error: {last_err})\ncontainer logs:\n{logs}"
        ),
    )


@pytest.fixture(scope="module")
def network_mode(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Bridge (default) or host — see the module docstring."""
    mode = os.environ.get("YTT_SMOKE_NETWORK", "bridge")
    if mode not in ("bridge", "host"):
        pytest.fail(f"YTT_SMOKE_NETWORK must be 'bridge' or 'host', got {mode!r}")
    if mode == "host":
        # serve() binds 0.0.0.0:8080 unconditionally — fail loudly if taken.
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", _SERVER_PORT)) == 0:
                pytest.fail(
                    f"YTT_SMOKE_NETWORK=host needs host port {_SERVER_PORT} free"
                )
    return mode


@pytest.fixture(scope="module")
def booted(image: str, network_mode: str, tmp_path_factory: pytest.TempPathFactory):
    """Boot the built image with the minimal documented configuration.

    Mirrors the README quick start `docker run -e ...` list; the only
    non-README env is SSL_CERT_FILE, which teaches the container's discovery
    fetch to trust this test's own stub IdP (harness plumbing, not a
    documented ytt setting). The OAuth client pair is generated per run and
    enters the container solely via `-e` — runtime injection only.
    """
    workdir = tmp_path_factory.mktemp("ytt-image-smoke")
    idp = _StubIdp(workdir, network_mode)
    client_id = "smoke-client-" + secrets.token_hex(4)
    client_secret = secrets.token_urlsafe(24)
    public_url = (
        f"http://127.0.0.1:{_SERVER_PORT}/ytt"
        if network_mode == "host"
        else f"http://127.0.0.1:{_free_port()}/ytt"
    )

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

    name = "ytt-smoke-" + secrets.token_hex(4)
    healthy, base_url, failure = _boot_container(
        image,
        network_mode,
        idp,
        env,
        name,
        publish_port=int(public_url.rsplit(":", 1)[1].split("/", 1)[0]),
    )
    if not healthy:
        idp.stop()
        hint = (
            " — if this host firewalls container→host traffic over the docker "
            "bridge, retry with YTT_SMOKE_NETWORK=host"
            if network_mode == "bridge"
            else ""
        )
        pytest.fail(failure + hint)

    booted_container = BootedContainer(base_url, name, client_secret)
    try:
        yield booted_container
    finally:
        _docker("rm", "-f", name)
        idp.stop()


# ---------------------------------------------------------------------------
# 1. fail closed without OAuth client configuration
# ---------------------------------------------------------------------------


def _fail_closed_run(
    image: str,
    missing: str | None = None,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """`docker run` the quick start with one variable removed and/or values
    overridden — the harness for every startup-required fail-closed check."""
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
    if missing is not None:
        base.pop(missing)
    base.update(overrides or {})
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


#: Distinctive fake credential values for the blank-value cases (bead
#: ytt-62be628f). A blank (empty string) variable is what a manifest ``env``
#: line interpolating an unset Secret key produces — it must exit 1 exactly
#: like a missing one. The *other* half of the pair carries the canary, so
#: asserting its absence from the container's output proves the fail-closed
#: path never echoes a working credential value into pod logs.
_CANARY_CLIENT_SECRET = "canary-client-secret-leak-probe-7b29cd"
_CANARY_CLIENT_ID = "canary-client-id-leak-probe-3f41a9"


def test_blank_oauth_client_id_fails_closed(image: str):
    """A blank YTT_OAUTH_CLIENT_ID must exit 1 like a missing one, printing
    the documented error, and must not leak the configured secret value."""
    run = _fail_closed_run(
        image,
        overrides={
            "YTT_OAUTH_CLIENT_ID": "",
            "YTT_OAUTH_CLIENT_SECRET": _CANARY_CLIENT_SECRET,
        },
    )
    output = run.stdout + run.stderr
    assert run.returncode == 1, (
        f"expected exit 1, got {run.returncode}:\n{output[-2000:]}"
    )
    assert "YTT_OAUTH_CLIENT_ID is required" in output, (
        f"exit was 1 but the documented missing-client-id error is absent:\n{output[-2000:]}"
    )
    assert _CANARY_CLIENT_SECRET not in output, (
        f"blank client id rejected, but the configured secret value leaked "
        f"into the container output:\n{output[-2000:]}"
    )


def test_blank_oauth_client_secret_fails_closed(image: str):
    """A blank YTT_OAUTH_CLIENT_SECRET must exit 1 like a missing one, and
    must not leak the configured client id value."""
    run = _fail_closed_run(
        image,
        overrides={
            "YTT_OAUTH_CLIENT_ID": _CANARY_CLIENT_ID,
            "YTT_OAUTH_CLIENT_SECRET": "",
        },
    )
    output = run.stdout + run.stderr
    assert run.returncode == 1, (
        f"expected exit 1, got {run.returncode}:\n{output[-2000:]}"
    )
    assert _CANARY_CLIENT_ID not in output, (
        f"blank client secret rejected, but the configured client id value "
        f"leaked into the container output:\n{output[-2000:]}"
    )


def test_missing_public_url_fails_closed(image: str):
    """Without YTT_PUBLIC_URL the server must exit 1 — there is no fallback
    to the reference deployment (bead ytt-a1fbc575): the OAuth
    audience/resource/issuer and every emitted RFC 9728 metadata document
    derive from this value, so a silent default would mistarget them."""
    run = _fail_closed_run(image, "YTT_PUBLIC_URL")
    output = run.stdout + run.stderr
    assert run.returncode == 1, (
        f"expected exit 1, got {run.returncode}:\n{output[-2000:]}"
    )
    assert "YTT_PUBLIC_URL is required" in output, (
        f"exit was 1 but the documented missing-public-url error is absent:\n{output[-2000:]}"
    )


def test_malformed_public_url_fails_closed(image: str):
    """A malformed YTT_PUBLIC_URL must also exit 1 — the value is validated
    when present, not merely required when absent."""
    run = _fail_closed_run(
        image, overrides={"YTT_PUBLIC_URL": "mcp.example.com/ytt"}
    )
    output = run.stdout + run.stderr
    assert run.returncode == 1, (
        f"expected exit 1, got {run.returncode}:\n{output[-2000:]}"
    )
    assert "YTT_PUBLIC_URL must use http:// or https://" in output, (
        f"exit was 1 but the malformed-public-url error is absent:\n{output[-2000:]}"
    )


# ---------------------------------------------------------------------------
# 2. minimal valid config boots; documented endpoints respond
# ---------------------------------------------------------------------------


def test_quick_start_health(booted: BootedContainer):
    """self-hosting.md "Smoke testing": /ytt/health → {"status": "ok"}."""
    r = httpx.get(f"{booted.base_url}/health", timeout=10.0)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_quick_start_mcp_transport_requires_auth(booted: BootedContainer):
    """The MCP transport mounted at /ytt must reject anonymous clients with the
    documented 401 + WWW-Authenticate resource_metadata pointer (README
    "Auth required"; server.py's challenge shape)."""
    r = httpx.get(booted.base_url, timeout=10.0)
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
