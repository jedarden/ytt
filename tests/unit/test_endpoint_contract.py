"""HTTP endpoint contract tests (docs/notes/http-endpoints.md).

Drives the real ASGI app (Starlette ``TestClient``, no uvicorn, no network
that the stubs don't provide) and pins the operational contract the docs
specify:

- **Route inventory** — which paths exist, with which methods, and that
  ``/ytt/mcp`` is *not* one of them (the transport is the prefix root).
- **Authentication** — the ``/ytt`` transport 401s every unauthenticated or
  invalid-token request before any protocol handling, with a
  ``resource_metadata`` pointer that actually resolves; ``/admin/egress``
  applies the same 401 + the same allowlist predicate as the tool-call gate
  (case-insensitive, ``@domain``-aware, no ``email_verified`` requirement —
  the reference Authentik hardcodes that claim False, so requiring it made
  the route un-returnable).
- **No transcript work off the transport** — a spy quadruple (caption
  fetch, Whisper get-or-create, Whisper run, cache write) stays silent
  while every operational route, slash variant, 404, and metadata route is
  driven. ``/metrics`` and ``/health`` are unauthenticated by design; the
  contract that keeps that safe is that they are fixed-shape, aggregate-only
  handlers — this module proves the "no work" half of that.
- **Method + path discipline** — GET/HEAD-only custom routes (405
  otherwise), 307 trailing-slash redirects that preserve method and auth,
  case-sensitive matching, no double-slash aliases.

Auth-related test tooling (fake ``AccessToken``, ``verify_token`` patching)
follows ``tests/unit/test_observability.py``; the allowlist bypass for
tool-logic tests lives in ``test_server.py``/``test_authz_tool_gate.py`` and
is deliberately NOT used here — this module tests the gates themselves.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest
from prometheus_client import REGISTRY
from starlette.testclient import TestClient

from ytt.server import build_asgi_app, mcp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CUSTOM_ROUTES = ("/ytt/health", "/ytt/metrics", "/ytt/admin/egress")


def _client(app=None) -> TestClient:
    return TestClient(app or build_asgi_app(), raise_server_exceptions=False)


def _access_token(email: str | None, verified: bool = True):
    """A real FastMCP AccessToken — ``get_access_token()`` type-checks it."""
    from fastmcp.server.auth.auth import AccessToken

    claims = {} if email is None else {"email": email, "email_verified": verified}
    return AccessToken(
        token="faketoken",
        client_id="test-client",
        scopes=[],
        expires_at=None,
        claims=claims,
    )


def _prm_url_from_header(header_value: str) -> str | None:
    m = re.search(r'resource_metadata="([^"]+)"', header_value or "")
    return m.group(1) if m else None


def _expected_prm_url() -> str:
    """The RFC 9728 PRM URL for the test public_url, built independently of
    ``ytt.server._prm_url`` so the test can't pass by construction alone."""
    from ytt.server import _settings_singleton

    parsed = urlparse(_settings_singleton.public_url)
    return (
        f"{parsed.scheme}://{parsed.netloc}"
        f"/.well-known/oauth-protected-resource{parsed.path.rstrip('/')}"
    )


def _install_transcript_spies(monkeypatch):
    """Silent recorders on every transcript-pipeline entry point.

    ``ytt.fetch.fetch_transcript`` and ``ytt.whisper.run_whisper_job`` are
    resolved from their source modules at call time (the tool and
    ``_run_whisper_job_bounded`` import lazily), and the registry/cache are
    patched on the server singletons — the same surfaces the production
    pipeline goes through, so a route that so much as starts a fetch, an
    ASR job, or a cache write lands here.
    """
    import ytt.fetch
    import ytt.whisper
    from ytt import server

    calls: list[str] = []

    def _spy(name: str):
        async def _record(*args, **kwargs):
            calls.append(name)

        return _record

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _spy("fetch_transcript"))
    monkeypatch.setattr(ytt.whisper, "run_whisper_job", _spy("run_whisper_job"))
    monkeypatch.setattr(
        server.whisper_registry, "get_or_create", _spy("get_or_create")
    )
    monkeypatch.setattr(server.transcript_cache, "put", _spy("cache_put"))
    return calls


# ---------------------------------------------------------------------------
# Route inventory
# ---------------------------------------------------------------------------


def test_route_inventory_matches_contract():
    """The mounted route set is exactly the documented one (docs §Route
    inventory) — in particular the MCP transport lives at the prefix root
    and there is no ``/ytt/mcp`` alias."""
    paths: dict[str, set[str]] = {}
    for route in build_asgi_app().router.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        methods = getattr(route, "methods", None) or set()
        paths.setdefault(path, set()).update(methods)

    for path, expected in (
        ("/ytt", {"POST", "DELETE"}),
        ("/ytt/health", {"GET", "HEAD"}),
        ("/ytt/metrics", {"GET", "HEAD"}),
        ("/ytt/admin/egress", {"GET", "HEAD"}),
        ("/.well-known/oauth-protected-resource/ytt", {"GET"}),
        ("/ytt/authorize", {"POST"}),
        ("/ytt/token", {"POST"}),
        ("/ytt/register", {"POST"}),
    ):
        assert expected <= paths.get(path, set()), (
            f"{path}: expected methods {sorted(expected)}, "
            f"mounted: {sorted(paths.get(path, set()))}"
        )

    assert "/ytt/mcp" not in paths, (
        "the transport is mounted at the prefix root (/ytt); a /ytt/mcp "
        "alias would be an undocumented second MCP surface"
    )


# ---------------------------------------------------------------------------
# /ytt — the MCP transport: auth before protocol
# ---------------------------------------------------------------------------

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "contract-test", "version": "0"},
    },
}
_ACCEPT_JSON = {"Accept": "application/json, text/event-stream"}


@pytest.mark.parametrize("auth_header", [None, "Bearer not-a-real-token"])
def test_mcp_transport_401s_before_any_protocol_handling(auth_header):
    """Every unauthenticated/invalid-token request to /ytt is a 401 — MCP
    messages included (auth runs in RequireAuthMiddleware, ahead of JSON-RPC
    parsing, so no tool logic can run for an unauthenticated caller)."""
    headers = {**_ACCEPT_JSON}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    with _client() as client:
        resp = client.post("/ytt", json=_INITIALIZE, headers=headers)
    assert resp.status_code == 401
    challenge = resp.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer"), challenge
    assert _prm_url_from_header(challenge) == _expected_prm_url()


def test_transport_challenge_prm_pointer_resolves():
    """The resource_metadata URL challenged by the transport is the routable
    RFC 9728 document (host root + resource path appended) — a client that
    follows it must land on metadata, not a 404."""
    with _client() as client:
        resp = client.post("/ytt", json=_INITIALIZE, headers=_ACCEPT_JSON)
        assert resp.status_code == 401
        prm_url = _prm_url_from_header(resp.headers["www-authenticate"])
        assert prm_url == _expected_prm_url()

        followed = client.get(urlparse(prm_url).path)
    assert followed.status_code == 200
    assert followed.json().get("resource") is not None


# ---------------------------------------------------------------------------
# /ytt/admin/egress — the same auth as a tool call
# ---------------------------------------------------------------------------


def test_admin_egress_401_points_at_routable_metadata():
    """/admin/egress's 401 must carry the same routable PRM pointer as the
    transport challenge. The route used to emit
    ``{public_url}/.well-known/oauth-protected-resource`` — a prefixed shape
    nothing routes (404), stranding the client exactly when it needs
    re-auth instructions (docs §/ytt/admin/egress, step 1)."""
    with _client() as client:
        resp = client.get("/ytt/admin/egress")
        assert resp.status_code == 401
        prm_url = _prm_url_from_header(resp.headers.get("www-authenticate", ""))
        assert prm_url == _expected_prm_url()
        followed = client.get(urlparse(prm_url).path)
    assert followed.status_code == 200
    assert "resource" in followed.json()


def test_admin_egress_gating_contract(monkeypatch):
    """401 without/rejected token; 403 for authenticated non-allowlisted;
    200 for allowlisted — with the *same* predicate as the tool-call gate:
    an Authentik-shaped token (``email_verified: False``) and an
    ``@domain`` allowlist entry must both admit, because the route promised
    'same auth as tool calls' and the old inline check (raw case-sensitive
    set membership + a truthy email_verified) denied every real production
    subject."""
    from unittest.mock import patch

    from ytt.models import EgressReport
    from ytt.server import _settings_singleton

    report = EgressReport(
        ip="203.0.113.1",
        asn="AS64512",
        org="Home ISP",
        via_proxy=False,
        is_residential=True,
    )

    def _get(email: str | None, verified: bool = True, allowlist: str = "") -> int:
        monkeypatch.setattr(_settings_singleton, "allowed_subjects", allowlist)
        with _client() as client:
            with patch.object(
                mcp.auth,
                "verify_token",
                return_value=_access_token(email, verified=verified),
            ), patch("ytt.selftest.probe_egress", return_value=report):
                headers = {} if email is None else {
                    "Authorization": "Bearer faketoken"
                }
                return client.get("/ytt/admin/egress", headers=headers).status_code

    assert _get(None) == 401
    assert _get("allowed@example.com", allowlist="allowed@example.com") == 200

    # verify_token returning None = IdP rejected the token → 401 (not 403)
    with _client() as client:
        monkeypatch.setattr(
            _settings_singleton, "allowed_subjects", "allowed@example.com"
        )
        with patch.object(mcp.auth, "verify_token", return_value=None):
            assert (
                client.get(
                    "/ytt/admin/egress",
                    headers={"Authorization": "Bearer garbage"},
                ).status_code
                == 401
            )

    monkeypatch.setattr(_settings_singleton, "allowed_subjects", "allowed@example.com")
    with _client() as client:
        with patch.object(
            mcp.auth,
            "verify_token",
            return_value=_access_token("stranger@example.com"),
        ), patch("ytt.selftest.probe_egress", return_value=report):
            assert (
                client.get(
                    "/ytt/admin/egress",
                    headers={"Authorization": "Bearer faketoken"},
                ).status_code
                == 403
            )

    # Authentik always sends email_verified: False (ytt.authz docstring) —
    # the allowlisted subject must still get through.
    assert (
        _get(
            "allowed@example.com",
            verified=False,
            allowlist="allowed@example.com",
        )
        == 200
    )
    # @domain allowlist entries admit the whole domain — same as tool calls.
    assert (
        _get(
            "anyone@example.com",
            verified=False,
            allowlist="@example.com",
        )
        == 200
    )


def test_admin_egress_success_body_shape(monkeypatch):
    """A 200 carries exactly the five egress fields — no proxy URL, no
    credential material, nothing per-subject."""
    from unittest.mock import patch

    from ytt.models import EgressReport
    from ytt.server import _settings_singleton

    monkeypatch.setattr(_settings_singleton, "allowed_subjects", "a@b.co")
    report = EgressReport(
        ip="203.0.113.1",
        asn="AS64512",
        org="Home ISP",
        via_proxy=False,
        is_residential=True,
    )
    with _client() as client:
        with patch.object(
            mcp.auth, "verify_token", return_value=_access_token("a@b.co")
        ), patch("ytt.selftest.probe_egress", return_value=report):
            resp = client.get(
                "/ytt/admin/egress", headers={"Authorization": "Bearer t"}
            )
    assert resp.status_code == 200
    assert set(resp.json()) == {"ip", "asn", "org", "via_proxy", "is_residential"}


def test_admin_egress_side_effects_are_the_complete_list(monkeypatch):
    """The route's entire effect set (docs §/ytt/admin/egress): one egress
    probe, the residential gauge, one last-sub write. Nothing else — in
    particular none of the transcript-pipeline spies below may fire."""
    from unittest.mock import patch

    from ytt import server
    from ytt.models import EgressReport

    monkeypatch.setattr(server._settings_singleton, "allowed_subjects", "a@b.co")
    report = EgressReport(
        ip="203.0.113.1",
        asn="AS64512",
        org="Home ISP",
        via_proxy=False,
        is_residential=True,
    )
    probes, subs = [], []

    def _fake_probe(proxy_url):
        probes.append(proxy_url)
        return report

    monkeypatch.setattr("ytt.selftest.probe_egress", _fake_probe)
    monkeypatch.setattr("ytt.authz.write_last_sub", lambda sub: subs.append(sub))
    spy_calls = _install_transcript_spies(monkeypatch)

    with _client() as client:
        with patch.object(
            mcp.auth, "verify_token", return_value=_access_token("a@b.co")
        ):
            resp = client.get(
                "/ytt/admin/egress", headers={"Authorization": "Bearer t"}
            )
    assert resp.status_code == 200

    assert probes == [server._settings_singleton.proxy_url]
    assert subs == ["a@b.co"]
    assert REGISTRY.get_sample_value("ytt_egress_is_residential") == 1.0
    assert spy_calls == []


def test_admin_egress_probe_failure_502_body_is_redacted(monkeypatch):
    """Probe failure → 502, and the relayed exception text goes through
    ``redact_credentials()`` — a httpx failure quoting the credentialed
    proxy must render with the userinfo stripped (docs/notes/proxy-egress.md)."""
    from unittest.mock import patch

    from ytt import server

    monkeypatch.setattr(server._settings_singleton, "allowed_subjects", "a@b.co")
    with _client() as client:
        with patch.object(
            mcp.auth, "verify_token", return_value=_access_token("a@b.co")
        ), patch(
            "ytt.selftest.probe_egress",
            side_effect=Exception(
                "dial http://alice:s3cret@proxy.example.com:3128 failed"
            ),
        ):
            resp = client.get(
                "/ytt/admin/egress", headers={"Authorization": "Bearer t"}
            )
    assert resp.status_code == 502
    assert "s3cret" not in resp.text and "alice" not in resp.text
    assert "proxy.example.com:3128" in resp.text


# ---------------------------------------------------------------------------
# Method discipline + path normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _CUSTOM_ROUTES)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_custom_routes_accept_get_head_only(path, method):
    """Write verbs are a bare 405 on every custom route — there is no
    write-shaped handler for a request to reach, whatever body it carries."""
    with _client() as client:
        resp = getattr(client, method)(path)
    assert resp.status_code == 405, f"{method.upper()} {path} must be 405"


def test_trailing_slash_redirects_preserve_method_and_auth():
    """Slash variants 307 to the canonical path; the redirect carries the
    method (POST lands in the 405, not a handler) and the auth gate
    (following /admin/egress/ lands in the 401) — a slash can never drop
    either (docs §Trailing slashes)."""
    with _client() as client:
        for path, canonical in (
            ("/ytt/metrics/", "/ytt/metrics"),
            ("/ytt/health/", "/ytt/health"),
            ("/ytt/admin/egress/", "/ytt/admin/egress"),
            ("/ytt/", "/ytt"),
        ):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 307, f"GET {path}"
            assert resp.headers["location"].endswith(canonical), path

            followed = client.get(path, follow_redirects=True)
            # Following the slash variant must land on the *gated* route for
            # both auth-bearing paths: /ytt/admin/egress/ into the route's
            # 401, and /ytt/ into the transport's 401 (GET /ytt is the SSE
            # stream, which RequireAuthMiddleware gates like every other
            # method). A slash preserving auth — never dropping it — is the
            # property under test; only the tokenless-by-design routes
            # (metrics, health) follow through to 200.
            expected = {"/ytt/admin/egress/": 401, "/ytt/": 401}.get(path, 200)
            assert followed.status_code == expected, f"followed GET {path}"

        post_redirect = client.post("/ytt/metrics/", follow_redirects=False)
        assert post_redirect.status_code == 307
        assert client.post("/ytt/metrics/", follow_redirects=True).status_code == 405


def test_path_normalization_contract():
    """Dot segments collapse; double slashes and case variants do not exist
    as aliases; /ytt/mcp is not a route in any method (docs §Trailing
    slashes)."""
    with _client() as client:
        assert client.get("/ytt/./metrics").status_code == 200
        assert client.get("/ytt//metrics").status_code == 404
        assert client.get("/YTT/metrics").status_code == 404
        assert client.get("/ytt/METRICS").status_code == 404
        init = client.post(
            "/ytt/mcp", json=_INITIALIZE, headers=_ACCEPT_JSON
        )
        assert init.status_code == 404


# ---------------------------------------------------------------------------
# Public-safe bodies: health + metrics
# ---------------------------------------------------------------------------


def test_health_body_is_liveness_only():
    """/ytt/health returns exactly {"status": "ok"} — publicly routed, so
    nothing else (version, subjects, config) may leak into it."""
    with _client() as client:
        resp = client.get("/ytt/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_metrics_exposition_is_public_safe(monkeypatch):
    """The exposition body carries only aggregate series with the documented
    bounded label surface — never a subject email, a YouTube URL, or
    transcript material. Mints a real rate-limited series first so the
    subject_hash path is exercised, not just asserted in the abstract."""
    from prometheus_client.parser import text_string_to_metric_families

    from ytt.server import _record_rate_limited

    _record_rate_limited("secret-user@example.com", "get_youtube_transcript")

    with _client() as client:
        resp = client.get("/ytt/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")

    body = resp.text
    assert "secret-user@example.com" not in body

    allowed_labels = {"outcome", "reason", "subject_hash"}
    # ``le`` (histogram buckets) and ``quantile`` (summaries) are reserved
    # structural labels the exposition format itself attaches — the client
    # library owns their (numeric) values, no application code routes data
    # through them. The bounded surface being pinned is the *application*
    # label set.
    structural_labels = {"le", "quantile"}
    ytt_families = [
        f for f in text_string_to_metric_families(body) if f.name.startswith("ytt_")
    ]
    assert ytt_families, "no ytt_* families found — parser or registry broke"
    for family in ytt_families:
        for sample in family.samples:
            unknown = set(sample.labels) - allowed_labels - structural_labels
            assert not unknown, (
                f"{sample.name}: label(s) {sorted(unknown)} outside the "
                f"documented surface {sorted(allowed_labels)} — a "
                "high-cardinality or identity-bearing label cannot ship on "
                "the unauthenticated exposition"
            )
            # A label value is data routed through a public endpoint: it
            # must be a bounded class ("ok", "ip_blocked", a hash prefix…)
            # never a URL, a per-subject identity, or free text.
            for label_value in sample.labels.values():
                assert "@" not in label_value and "http" not in label_value, (
                    f"{sample.name}: label value {label_value!r} looks like "
                    "an identity or URL — not public-safe"
                )


# ---------------------------------------------------------------------------
# The core invariant: no transcript work off the transport
# ---------------------------------------------------------------------------


def test_operational_routes_never_trigger_transcript_work(monkeypatch):
    """Driving every operational route — metrics and health (both slash
    variants), the gated egress route, the transport challenge, unknown
    paths, and the public metadata documents — must leave the transcript
    pipeline (caption fetch, ASR get-or-create, ASR run, cache write)
    completely untouched. /ytt is the only route that can start that work,
    and here it only ever answers 401."""
    spy_calls = _install_transcript_spies(monkeypatch)

    with _client() as client:
        drives: list[object] = [
            client.get("/ytt/metrics"),
            client.get("/ytt/metrics/"),
            client.head("/ytt/metrics"),
            client.get("/ytt/health"),
            client.get("/ytt/health/"),
            client.get("/ytt/admin/egress"),
            client.get("/ytt/admin/egress/"),
            client.get("/ytt"),  # transport GET without a token
            client.post("/ytt", json=_INITIALIZE, headers=_ACCEPT_JSON),
            client.post("/ytt/metrics", json={}),
            client.post("/ytt/health", json={}),
            client.get("/definitely-not-a-route"),
            client.get("/.well-known/oauth-protected-resource/ytt"),
            client.get("/.well-known/oauth-authorization-server/ytt"),
            client.get("/.well-known/openid-configuration/ytt"),
        ]
        for resp in drives:
            assert resp.status_code < 500, f"{resp.request.method} {resp.request.url}"

    assert spy_calls == [], (
        f"transcript pipeline invoked from an operational route: {spy_calls}"
    )
