"""Unit tests for the ``YTT_PATH_PREFIX`` startup-validation and
route-joining contract (bead ytt-87812a35).

``docs/notes/http-endpoints.md`` ("Route inventory") documents the prefix
contract but named no pinning test — every other documented contract in the
notes has one. This file is that pin, for the three documented properties:

1. **Startup validation is fail-closed** — a prefix without its trailing
   slash (or without a leading one) fails ``Settings`` construction, so the
   server exits 1 before binding instead of silently misrouting. The join
   the prefix feeds is plain concatenation (:func:`ytt.config.join_path`):
   ``/ytt`` + ``health`` would glue onto ``/ytthealth`` on every route, so
   the value is rejected rather than normalized. The error names the
   variable and the fix — a CrashLooping pod's log must be actionable.

2. **Unset resolves to the default; empty is an error.** An unset
   ``YTT_PATH_PREFIX`` is the documented default ``/ytt/``; an explicitly
   empty value (what a manifest ``env`` line interpolating an unset
   configmap key produces) is a startup error, not a root mount — the same
   unset-interpolation posture as ``YTT_PUBLIC_URL`` and ``YTT_PROXY_URL``.

3. **``Settings.route()`` joins prefix + segment without doubling or
   dropping the boundary slash** for every documented custom route
   (``/ytt/health``, ``/ytt/metrics``, ``/ytt/admin/egress``), and the MCP
   transport mounts at the bare prefix (the trailing slash stripped:
   ``/ytt/`` → ``/ytt``) — which is what keeps ``POST /ytt`` the transport
   and ``/ytt/mcp`` a 404. Pinned at HTTP level against the real ASGI app,
   including a full app rebuild under a non-default prefix: every route
   moves with the prefix, the old spellings 404, and the glue spellings a
   bad prefix would have produced are 404 too.

The entrypoint leg mirrors ``tests/unit/test_oauth_startup_fail_closed.py``:
``python -m ytt serve`` (the image CMD) exits 1 with no uvicorn bind marker
and the documented variable-naming error in its output. No credential value
is in play here, so there is no leak leg — the bad value is echoed by
design (it is the operator's own config, not a secret).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from ytt.config import Settings, get_settings, join_path
from ytt.server import _build_app, build_asgi_app

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The documented default (README/configuration.md/http-endpoints.md).
_DEFAULT_PREFIX = "/ytt/"

#: Every custom-route segment Settings.route() is documented to join, with
#: the mounted path under the default prefix (the http-endpoints.md
#: inventory rows).
_DOCUMENTED_CUSTOM_ROUTES = {
    "health": "/ytt/health",
    "metrics": "/ytt/metrics",
    "admin/egress": "/ytt/admin/egress",
}

#: uvicorn's own bind/serve markers — if either appears, the server got past
#: the prefix gate and served routes on a misrouting mount.
_UVICORN_SERVED_MARKERS = ("Uvicorn running", "Application startup complete")

#: Minimal-valid startup env for the subprocess leg (canary values — never a
#: real credential; the same posture as test_oauth_startup_fail_closed).
_VALID_STARTUP_ENV = {
    "YTT_PUBLIC_URL": "https://mcp.example.com/ytt",
    "YTT_OAUTH_CLIENT_ID": "canary-client-id-path-prefix-probe-1c07e4",
    "YTT_OAUTH_CLIENT_SECRET": "canary-client-secret-path-prefix-probe-1c07e4",
}


# ---------------------------------------------------------------------------
# Leg 1 — startup validation is fail-closed, with a clear error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_prefix,fragment",
    [
        # The documented failure: the join would glue segments onto the
        # mount path's last word (/ytt + health -> /ytthealth).
        ("/ytt", "YTT_PATH_PREFIX must end with '/'"),
        ("ytt/", "YTT_PATH_PREFIX must start with '/'"),
        # Relative + degenerate spellings — same gate.
        ("ytt", "YTT_PATH_PREFIX must end with '/'"),
    ],
)
def test_malformed_prefix_is_rejected_at_construction(bad_prefix, fragment):
    """A prefix missing a boundary slash never constructs — the fail-closed
    startup gate (Settings construction failure -> server exits before
    binding), not a warning and not a normalization."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(path_prefix=bad_prefix)
    assert fragment in str(excinfo.value)


def test_trailing_slash_error_is_actionable():
    """The documented error names the variable and the fix — a CrashLooping
    pod's log must say which env var to correct, not just 'invalid value'."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(path_prefix="/ytt")
    message = str(excinfo.value)
    assert "YTT_PATH_PREFIX must end with '/'" in message
    assert "fix the env var and restart" in message


def test_malformed_prefix_rejected_through_the_env(monkeypatch):
    """The gate sits on the env-fed path too (what a real boot reads), not
    only on direct kwargs construction."""
    monkeypatch.setenv("YTT_PATH_PREFIX", "/ytt")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert "YTT_PATH_PREFIX must end with '/'" in str(excinfo.value)


def test_join_is_plain_concatenation_the_misroute_is_real():
    """Why the gate is fail-closed: the join the prefix feeds does not
    insert a slash, so a missing trailing slash would silently misroute
    every custom route onto the mount path's last word. Pinned as the
    rationale the validator exists for — if the join ever becomes
    slash-normalizing, this pin and the gate must be revisited together."""
    assert join_path("/ytt", "health") == "/ytthealth"
    assert join_path("/ytt", "metrics") == "/yttmetrics"
    assert join_path("/ytt", "admin/egress") == "/yttadmin/egress"


# ---------------------------------------------------------------------------
# Leg 2 — unset resolves to the default; explicitly empty is an error
# ---------------------------------------------------------------------------


def test_unset_resolves_to_the_documented_default(monkeypatch):
    """No ``YTT_PATH_PREFIX`` in the env at all -> the documented default
    ``/ytt/`` (docs previously left this undefined — this test defines it)."""
    monkeypatch.delenv("YTT_PATH_PREFIX", raising=False)
    assert Settings().path_prefix == _DEFAULT_PREFIX


def test_explicitly_empty_is_a_startup_error_not_a_root_mount(monkeypatch):
    """``YTT_PATH_PREFIX=""`` (what a manifest env line interpolating an
    unset key produces) must fail construction like any other slash-less
    value — mounting every route at root would be the silent-misroute
    failure mode wearing a different hat."""
    monkeypatch.setenv("YTT_PATH_PREFIX", "")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert "YTT_PATH_PREFIX must end with '/'" in str(excinfo.value)


def test_valid_nondefault_prefix_is_accepted():
    """The gate rejects malformed values only — a well-formed alternative
    prefix constructs and is honored byte-for-byte."""
    assert Settings(path_prefix="/gateway/").path_prefix == "/gateway/"


# ---------------------------------------------------------------------------
# Leg 3 — route() joins prefix + segment without doubling or dropping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("segment,expected", sorted(_DOCUMENTED_CUSTOM_ROUTES.items()))
def test_route_join_matches_the_documented_inventory(segment, expected):
    """Every custom route in the http-endpoints.md inventory is exactly
    ``Settings.route(segment)`` under the default prefix."""
    assert Settings().route(segment) == expected


@pytest.mark.parametrize("segment,expected", sorted(_DOCUMENTED_CUSTOM_ROUTES.items()))
def test_route_join_moves_with_a_nondefault_prefix(segment, expected):
    """Under a non-default prefix each documented route is the prefix plus
    the segment — the join travels, nothing is pinned to /ytt."""
    settings = Settings(path_prefix="/gateway/")
    assert settings.route(segment) == expected.replace("/ytt/", "/gateway/", 1)


@pytest.mark.parametrize("segment", sorted(_DOCUMENTED_CUSTOM_ROUTES))
def test_route_join_never_doubles_or_drops_the_boundary_slash(segment):
    """Both segment spellings — bare (the way server.py calls it) and
    leading-slash — resolve to the same single-slash route. A dropped
    boundary would glue (``/ytthealth``); a doubled one would 404 the
    documented spelling (``/ytt//health``)."""
    settings = Settings()
    joined = settings.route(segment)
    slashed = settings.route("/" + segment)
    assert joined == slashed
    assert "//" not in joined
    assert joined == f"{_DEFAULT_PREFIX}{segment}"


# ---------------------------------------------------------------------------
# Leg 4 — HTTP level: the real app mounts the contract (default + rebuild)
# ---------------------------------------------------------------------------


def test_default_app_router_carries_the_documented_paths():
    """The ASGI router's route spellings are the Settings-derived ones,
    byte-for-byte: custom routes at route(), the transport at the bare
    prefix (trailing slash stripped), and no ``<prefix>/mcp`` alias."""
    settings = Settings()
    transport_path = settings.path_prefix.rstrip("/")
    paths = {
        route.path
        for route in build_asgi_app().router.routes
        if hasattr(route, "path")
    }
    for segment in _DOCUMENTED_CUSTOM_ROUTES:
        assert settings.route(segment) in paths
    assert transport_path in paths
    assert f"{transport_path}/mcp" not in paths


def test_default_app_transport_is_at_the_bare_prefix_and_mcp_is_a_404():
    """``POST /ytt`` reaches the auth-gated MCP transport (401 challenge,
    not a route miss); ``POST /ytt/mcp`` is a plain 404; the glue spelling a
    slash-less prefix would have produced is a 404 too."""
    client = TestClient(build_asgi_app(), raise_server_exceptions=False)

    unauthenticated = client.post("/ytt")
    assert unauthenticated.status_code == 401
    assert "www-authenticate" in {k.lower() for k in unauthenticated.headers}

    assert client.post("/ytt/mcp").status_code == 404
    assert client.get("/ytthealth").status_code == 404


#: A non-default prefix paired with the issuer URL that must accompany it
#: (self-hosting Step 3: YTT_PUBLIC_URL ends with the prefix stripped of its
#: trailing slash) — the metadata routes derive from the issuer path, so the
#: rebuild pairs them the way a real boot must.
_GATEWAY_PUBLIC_URL = "https://mcp.example.com/gateway"


def _rebuild_under_prefix(monkeypatch, *, prefix: str, public_url: str):
    """Rebuild the full app (tools + custom routes + transport mount) under
    *prefix* — what a fresh process with these env values would serve.

    ``_build_app()`` returns a throwaway FastMCP instance, so the module
    singleton (``ytt.server.mcp``) other tests share is never rebound; the
    settings cache is cleared on both sides so the process-wide cache is
    always reconstructed from the caller's env afterwards.
    """
    monkeypatch.setenv("YTT_PATH_PREFIX", prefix)
    monkeypatch.setenv("YTT_PUBLIC_URL", public_url)
    get_settings.cache_clear()
    try:
        fresh = _build_app()
        settings = get_settings()
        return fresh.http_app(path=settings.path_prefix.rstrip("/") or None)
    finally:
        get_settings.cache_clear()


def test_rebuild_under_nondefault_prefix_serves_the_same_contract(monkeypatch):
    """A full app rebuild under ``YTT_PATH_PREFIX=/gateway/`` moves every
    surface with the prefix: custom routes, the transport (still auth-gated
    at the bare prefix), and the paired metadata path — while the old
    spellings and every glue spelling a bad prefix would have produced are
    404. This is the no-silent-misroute guarantee end-to-end."""
    app = _rebuild_under_prefix(
        monkeypatch, prefix="/gateway/", public_url=_GATEWAY_PUBLIC_URL
    )
    client = TestClient(app, raise_server_exceptions=False)

    # Custom routes moved with the prefix, shapes intact.
    health = client.get("/gateway/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert client.get("/gateway/metrics").status_code == 200
    assert client.get("/gateway/admin/egress").status_code == 401

    # The transport moved to the bare prefix and is still the gated one;
    # the /mcp spelling is a 404 under any prefix (the documented invariant).
    unauthenticated = client.post("/gateway")
    assert unauthenticated.status_code == 401
    assert "www-authenticate" in {k.lower() for k in unauthenticated.headers}
    assert client.post("/gateway/mcp").status_code == 404

    # No silent misroute in either direction: no glue, no old-prefix mount.
    for missing in (
        "/gatewayhealth",  # dropped-slash glue
        "/gatewayytthealth",  # segment glued to the bare prefix
        "/gateway/ytthealth",  # default prefix nested under the new one
        "/ytt/health",  # the old prefix is gone entirely
    ):
        assert client.get(missing).status_code == 404, missing

    # Metadata follows the paired issuer path (public_url ends with the
    # prefix), not the prefix alone and not the old spelling.
    assert (
        client.get("/.well-known/oauth-protected-resource/gateway").status_code == 200
    )


# ---------------------------------------------------------------------------
# Leg 5 — the real entrypoint refuses to boot on a bad prefix
# ---------------------------------------------------------------------------


def test_serve_exits_1_on_a_missing_trailing_slash():
    """``python -m ytt serve`` — the image CMD — exits 1 with the documented
    variable-naming error and never reaches uvicorn, when the env carries
    the documented bad value. This is the 'validated at startup' promise the
    notes make, at the entrypoint a CrashLooping pod actually runs."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YTT_")}
    env.update(_VALID_STARTUP_ENV)
    env["YTT_PATH_PREFIX"] = "/ytt"  # the documented failure: missing slash

    run = subprocess.run(
        [sys.executable, "-m", "ytt", "serve"],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = run.stdout + run.stderr

    assert run.returncode == 1, (
        f"expected exit 1 before serving, got {run.returncode}:\n{output[-2000:]}"
    )
    assert "YTT_PATH_PREFIX must end with '/'" in output, (
        f"exit was 1 but the documented error is absent:\n{output[-2000:]}"
    )
    for marker in _UVICORN_SERVED_MARKERS:
        assert marker not in output, (
            f"uvicorn served routes on a misrouting prefix:\n{output[-2000:]}"
        )
