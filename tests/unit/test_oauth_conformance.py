"""Automated MCP OAuth discovery + audience/path conformance suite.

Encodes the server-exposure contract from ``docs/research/mcp-oauth-authentication.md``
(§4.1 WWW-Authenticate, §4.2 Protected Resource Metadata RFC 9728, §4.3
Authorization Server Metadata RFC 8414) and the authorization model from
``docs/notes/auth.md``, exercised against the real ASGI app the way an MCP
client walks it — no network, no live Authentik (unit-suite conftest patches
the OIDC discovery fetch).

Coverage map (each class cites the requirement it pins):

- :class:`TestUnauthenticatedChallenge` — every protected surface answers an
  unauthenticated request with **401 + a ``Bearer`` WWW-Authenticate challenge
  carrying ``resource_metadata``** (research §4.1 "the single most important
  interop detail"), swept across the MCP transport AND every registered tool.
- :class:`TestProtectedResourceMetadata` — RFC 9728 document conformance at
  the path-aware well-known URL: path-bearing ``resource``, non-empty
  ``authorization_servers``, ``bearer_methods_supported: header``, scopes.
- :class:`TestAuthorizationServerMetadata` — RFC 8414 conformance: issuer =
  the path-bearing public URL, endpoints reachable under that issuer path,
  authorization-code-only response types, S256-only PKCE, grant types.
- :class:`TestPKCEFlow` — OAuth 2.1: authorization requests **require** a
  S256 ``code_challenge`` (``plain`` rejected, absent rejected), and an
  approved consent redirects upstream with a fresh S256 challenge (the proxy
  re-challenges for the upstream leg per ``forward_pkce=True``).
- :class:`TestDCRRestrictedToClaudeRedirects` — the shipped form of
  docs/notes/auth.md's "DCR disabled for personal v1": registration completes
  (OAuthProxy requires it) but the authorize endpoint enforces the
  Claude-only redirect allowlist, so a self-registered third-party redirect
  URI can never complete an authorization — the open-redirect/relay guard.
- :class:`TestPathBearingAudienceBinding` — RFC 8707: the token audience, the
  resource identifier and the issuer are all the **path-bearing** public URL
  (never the bare origin), and the upstream id-token verifier rejects wrong
  audience / wrong issuer (confused-deputy replay guard).
- :class:`TestSubjectAllowlistCoverage` — AuthZ (``YTT_ALLOWED_SUBJECTS``)
  gates every registered tool with no token present (fail-closed), and the
  public/protected route split is exactly as documented.
- :class:`TestDiscoveryChain` — the full client algorithm of research §2:
  401 challenge → fetch the challenged ``resource_metadata`` URL → PRM →
  authorization server → RFC 8414 path-inserted AS metadata → issuer match.
- :class:`TestInvalidSignatureRejection`, :class:`TestTemporalClaimEnforcement`,
  :class:`TestUpstreamSecretRotation`, :class:`TestYTTSignedTokenRotation`,
  :class:`TestJWKSPathFailClosed` and :class:`TestDiscoveryOutageFailClosed` —
  the token-validation failure modes (bead ytt-3da4d7d5, runbook in
  ``docs/notes/auth.md`` § "Key rotation and token-validation failures"):
  every signature failure fails closed, ``exp`` is mandatory and ``nbf`` is
  honored on the upstream id_token, both signing keys rotate instantly with
  no dual-key acceptance window, the JWKS path (the default for an RS256
  IdP) fails closed on outages/empty key sets and serves cached keys for at
  most its 1h TTL, and an IdP discovery outage at construction blocks
  startup while post-startup validation stays fully offline.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from ytt.config import get_settings

# ===========================================================================
# Helpers
# ===========================================================================

#: Absolute https base for OAuth-flow requests. The consent CSRF cookie is
#: ``Secure`` (base_url is https), so the consent GET and POST must both use
#: absolute https URLs for the TestClient cookie jar to carry it.
BASE = "https://mcp.ardenone.com"

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


#: RFC 8414 §3.1 — for an issuer URL with a path component, the well-known
#: suffix is inserted between the host and the path
#: (https://host/.well-known/oauth-authorization-server/<issuer-path>).
def _well_known_url(issuer: str, suffix: str) -> str:
    parsed = urlparse(issuer)
    return f"{parsed.scheme}://{parsed.netloc}{suffix}{parsed.path.rstrip('/')}"


def _issuer_path() -> str:
    """Path component of the configured path-bearing public URL (e.g. '/ytt')."""
    return urlparse(get_settings().public_url).path.rstrip("/")


def _parse_www_authenticate(header: str) -> tuple[str, dict[str, str]]:
    """Split a WWW-Authenticate header into ``(scheme, {param: value})``.

    The challenge's ``error_description`` contains commas inside a
    quoted-string, so a naive comma split would shred the params (RFC 9110
    §11.6.1 quote-aware scanning).
    """
    scheme, _, rest = header.partition(" ")
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in rest:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))

    params: dict[str, str] = {}
    for part in parts:
        k, _, v = part.strip().partition("=")
        if k:
            params[k.strip()] = v.strip().strip('"')
    return scheme.strip(), params


def _pkce_pair() -> tuple[str, str]:
    """RFC 7636 §4.1–4.2 — S256 (verifier, challenge) pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


@pytest.fixture
def client():
    """TestClient over the real ASGI app; redirects NOT followed so the
    conformance assertions can inspect each hop of the OAuth flows."""
    from ytt.server import build_asgi_app

    return TestClient(
        build_asgi_app(), raise_server_exceptions=False, follow_redirects=False
    )


def register_client(client: TestClient, *redirect_uris: str) -> dict:
    """DCR-register a client and return the registration response JSON."""
    resp = client.post(
        f"{BASE}{_issuer_path()}/register",
        json={
            "client_name": "oauth-conformance-suite",
            "redirect_uris": list(redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _list_tools_bypassing_auth() -> list:
    """All registered tools with AuthMiddleware temporarily bypassed.

    The middleware denies unauthenticated ``list_tools`` at the protocol
    layer, which would otherwise hide the registration these sweeps are
    meant to cover. The bypass is flipped back in a ``finally`` — with no
    ``await`` between flip and restore, no other test can observe it.
    """
    from fastmcp.server.middleware.authorization import AuthMiddleware

    from ytt.server import mcp

    bypassed: list[tuple[AuthMiddleware, object]] = []
    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            bypassed.append((mw, mw.auth))
            mw.auth = lambda ctx: True
    try:
        return await mcp.list_tools()
    finally:
        for mw, auth in bypassed:
            mw.auth = auth


def registered_tool_objects() -> list:
    """Registered Tool objects (sync bridge — the registry is async; only
    call from a sync test, which owns a fresh event loop)."""
    import asyncio

    return asyncio.run(_list_tools_bypassing_auth())


def _probe_args(tool) -> dict:
    """Minimal valid arguments for *tool*, built from its own schema so the
    sweep keeps working when tools are added or their signatures change."""
    schema = getattr(tool, "parameters", None) or {}
    props = schema.get("properties", {}) or {}
    out: dict = {}
    for name in schema.get("required", []) or []:
        spec = props.get(name, {})
        if spec.get("type") in ("integer", "number"):
            out[name] = 1
        elif spec.get("type") == "boolean":
            out[name] = False
        else:
            out[name] = "probe"
    return out


def _full_consent_approval(client: TestClient, authorize_params: dict):
    """Walk authorize → consent page → approve; return the approval response.

    The caller inspects the response (a 302 to the upstream AS on success).
    Raises with context if any hop misbehaves.
    """
    r_authorize = client.get(
        f"{BASE}{_issuer_path()}/authorize", params=authorize_params
    )
    assert r_authorize.status_code == 302, (
        f"authorize: {r_authorize.status_code} {r_authorize.text[:200]}"
    )
    consent_url = r_authorize.headers["location"]
    assert consent_url.startswith(f"{BASE}{_issuer_path()}/consent"), consent_url

    r_consent = client.get(consent_url)
    assert r_consent.status_code == 200, (
        f"consent GET: {r_consent.status_code} {r_consent.text[:200]}"
    )
    match = re.search(r'name="csrf_token" value="([^"]+)"', r_consent.text)
    assert match, "consent page carries no csrf_token form field"
    txn_id = consent_url.split("txn_id=")[1]

    return client.post(
        f"{BASE}{_issuer_path()}/consent",
        data={
            "txn_id": txn_id,
            "csrf_token": match.group(1),
            "action": "approve",
        },
    )


# ===========================================================================
# 401 + WWW-Authenticate challenge (research §4.1, RFC 6750 §3, RFC 9728 §5.1)
# ===========================================================================


class TestUnauthenticatedChallenge:
    """Every protected surface answers an unauthenticated request with
    401 + ``WWW-Authenticate: Bearer ... resource_metadata="..."``."""

    def test_mcp_transport_401_with_challenge(self, client):
        resp = client.post(
            _issuer_path(), json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        )
        assert resp.status_code == 401
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    def test_challenge_scheme_is_bearer_with_resource_metadata(self, client):
        """The challenge must be ``Bearer`` scheme and carry the RFC 9728
        ``resource_metadata`` param — research §4.1: without it the hosted
        claude.ai connector fails to add the server at all."""
        resp = client.post(
            _issuer_path(), json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        )
        scheme, params = _parse_www_authenticate(resp.headers["www-authenticate"])
        assert scheme == "Bearer"
        expected_prm = _well_known_url(
            get_settings().public_url, "/.well-known/oauth-protected-resource"
        )
        assert params.get("resource_metadata") == expected_prm

    def test_challenged_resource_metadata_url_resolves(self, client):
        """The resource_metadata URL in the challenge is the URL the client
        will actually GET next — it must resolve to the PRM document."""
        resp = client.post(
            _issuer_path(), json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        )
        _, params = _parse_www_authenticate(resp.headers["www-authenticate"])
        prm_url = params["resource_metadata"]
        doc = client.get(prm_url)
        assert doc.status_code == 200
        assert "resource" in doc.json()

    def test_invalid_bearer_token_still_challenges(self, client):
        """A garbage bearer token gets the same 401 challenge (RFC 6750 §3 —
        invalid_token error, resource_metadata still advertised)."""
        resp = client.post(
            _issuer_path(),
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401
        scheme, params = _parse_www_authenticate(resp.headers["www-authenticate"])
        assert scheme == "Bearer"
        assert "resource_metadata" in params
        assert params.get("error") == "invalid_token"

    def test_every_registered_tool_401s_unauthenticated(self, client):
        """Sweep: an unauthenticated ``tools/call`` for EVERY registered tool
        is rejected with 401 + challenge before any tool logic runs — the
        auth gate is transport-level, so a future tool cannot accidentally
        ship ungated."""
        tools = {t.name: t for t in registered_tool_objects()}
        assert tools, "no tools registered — sweep would be vacuous"
        for name in sorted(tools):
            resp = client.post(
                _issuer_path(),
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": _probe_args(tools[name])},
                    "id": 1,
                },
            )
            assert resp.status_code == 401, f"tool {name!r} not 401-gated"
            scheme, params = _parse_www_authenticate(resp.headers["www-authenticate"])
            assert scheme == "Bearer", f"tool {name!r}: challenge not Bearer"
            assert "resource_metadata" in params, f"tool {name!r}: no resource_metadata"

    def test_admin_egress_401_unauthenticated(self, client):
        """The auth-gated /admin/egress diagnostic route also 401s with no
        token (plan §Security)."""
        resp = client.get(f"{_issuer_path()}/admin/egress")
        assert resp.status_code == 401


# ===========================================================================
# Protected Resource Metadata (RFC 9728)
# ===========================================================================


class TestProtectedResourceMetadata:
    """The PRM document at the path-aware well-known URL."""

    @pytest.fixture
    def prm(self, client):
        resp = client.get(
            _well_known_url(
                get_settings().public_url, "/.well-known/oauth-protected-resource"
            )
        )
        assert resp.status_code == 200
        return resp.json()

    def test_resource_is_the_path_bearing_public_url(self, prm):
        """RFC 9728 §4 + RFC 8707: ``resource`` is the canonical URI — the
        full path-bearing public URL, no trailing slash, never the bare
        origin."""
        assert prm["resource"] == get_settings().public_url
        assert urlparse(prm["resource"]).path == _issuer_path()

    def test_authorization_servers_non_empty_and_https(self, prm):
        servers = prm.get("authorization_servers")
        assert isinstance(servers, list) and servers
        for server in servers:
            assert urlparse(str(server)).scheme == "https"

    def test_authorization_server_is_the_ytt_issuer(self, prm):
        """ytt co-hosts its AS with the resource (OAuthProxy model) — the
        advertised AS must be the path-bearing ytt issuer, so clients walk
        discovery here and not at some foreign AS."""
        assert prm["authorization_servers"] == [get_settings().public_url]

    def test_bearer_methods_supported_header(self, prm):
        assert "header" in prm.get("bearer_methods_supported", [])

    def test_scopes_supported_advertise_refresh_enabling_set(self, prm):
        """openid + email are the identity scopes; offline_access is
        deliberately advertised so clients request a refresh-capable grant —
        without it every ~5-minute Authentik expiry forced a full re-auth
        (0.2.13 fix, ytt/auth.py)."""
        for scope in ("openid", "email", "offline_access"):
            assert scope in prm.get("scopes_supported", [])


# ===========================================================================
# Authorization Server Metadata (RFC 8414)
# ===========================================================================


class TestAuthorizationServerMetadata:
    """The AS metadata document at the RFC 8414 path-inserted URL."""

    @pytest.fixture
    def as_meta(self, client):
        resp = client.get(
            _well_known_url(
                get_settings().public_url, "/.well-known/oauth-authorization-server"
            )
        )
        assert resp.status_code == 200
        return resp.json()

    def test_issuer_equals_path_bearing_public_url(self, as_meta):
        """RFC 8414 §2: the issuer must be the URL the client discovered the
        metadata from (modulo well-known insertion) — here the path-bearing
        public URL, byte-identical, no trailing slash."""
        assert as_meta["issuer"] == get_settings().public_url

    @pytest.mark.parametrize(
        "field",
        [
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
        ],
    )
    def test_endpoints_live_under_the_issuer_path(self, as_meta, field):
        """ytt's IngressRoute only forwards PathPrefix('/ytt') — every
        advertised endpoint URL must carry the issuer path or clients get 404
        walking the advertised URLs (the 2026-07-16 production DCR failure
        was exactly this class of bug)."""
        url = urlparse(str(as_meta[field]))
        assert url.scheme == "https"
        assert url.path.startswith(_issuer_path() + "/")

    def test_revocation_endpoint_if_advertised_lives_under_issuer_path(self, as_meta):
        """RFC 7009 revocation is optional, so its presence follows the
        upstream IdP's discovery document (the unit-suite fake OIDC config
        omits it; live Authentik advertises it). Advertised or not, an
        issuer-path-bearing value is required for reachability."""
        if "revocation_endpoint" not in as_meta:
            pytest.skip("no revocation_endpoint in AS metadata (upstream omits it)")
        url = urlparse(str(as_meta["revocation_endpoint"]))
        assert url.path.startswith(_issuer_path() + "/")

    def test_response_types_authorization_code_only(self, as_meta):
        """OAuth 2.1: authorization code only — the implicit flow must not be
        advertised."""
        types = as_meta.get("response_types_supported", [])
        assert "code" in types
        assert "token" not in types

    def test_grant_types_include_code_and_refresh(self, as_meta):
        grants = as_meta.get("grant_types_supported", [])
        assert "authorization_code" in grants
        assert "refresh_token" in grants

    def test_pkce_s256_supported_and_plain_forbidden(self, as_meta):
        """RFC 7636 + OAuth 2.1: S256 advertised; ``plain`` must never be
        (it fails the 'PKCE binds via a one-way derivation' property that
        makes PKCE worth requiring)."""
        methods = as_meta.get("code_challenge_methods_supported", [])
        assert "S256" in methods
        assert "plain" not in methods

    def test_token_endpoint_auth_methods_present(self, as_meta):
        assert as_meta.get("token_endpoint_auth_methods_supported")

    def test_scopes_supported_matches_prm_offer(self, as_meta):
        for scope in ("openid", "email", "offline_access"):
            assert scope in as_meta.get("scopes_supported", [])

    def test_oidc_alias_served_at_path_inserted_url(self, client):
        """The OIDC-discovery alias (RFC 8414 §5 fallback for OIDC-aware
        clients) must also be mounted at the path-inserted location."""
        resp = client.get(
            _well_known_url(
                get_settings().public_url, "/.well-known/openid-configuration"
            )
        )
        assert resp.status_code == 200
        assert resp.json()["issuer"] == get_settings().public_url


# ===========================================================================
# PKCE flow (OAuth 2.1 / RFC 7636) — driven end-to-end, no network
# ===========================================================================


class TestPKCEFlow:
    """The authorization-code + PKCE contract at the /authorize endpoint."""

    def _authorize_params(self, client_id: str, **overrides) -> dict:
        _, challenge = _pkce_pair()
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "conformance-state",
            "scope": "openid email offline_access",
        }
        params.update(overrides)
        return params

    def test_approved_flow_redirects_upstream_with_s256_challenge(self, client):
        """Full offline walk: DCR → authorize → consent approval. The final
        302 must target the upstream IdP's authorization endpoint with an
        S256 code_challenge (forward_pkce=True — the proxy re-challenges for
        its own upstream leg) and carry response_type=code + a state."""
        client_id = register_client(client, CLAUDE_REDIRECT)["client_id"]
        approval = _full_consent_approval(client, self._authorize_params(client_id))
        assert approval.status_code == 302
        upstream = urlparse(approval.headers["location"])
        query = {k: v[0] for k, v in parse_qs(upstream.query).items()}
        assert upstream.scheme == "https"
        # Upstream is the Authentik authorize endpoint, not some echo of ours
        assert upstream.path.endswith("/application/o/authorize/")
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        # RFC 7636 §4.2: base64url(SHA-256) — 43 chars, no padding
        assert len(query["code_challenge"]) == 43
        assert "state" in query
        assert "code" not in query  # nothing is granted pre-authentication

    @staticmethod
    def _error_redirect_params(resp) -> dict:
        """Parse the query of a 302 error redirect back to the caller."""
        return {
            k: v[0]
            for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()
        }

    def test_missing_code_challenge_rejected(self, client):
        """PKCE is mandatory (OAuth 2.1) — an authorize request without a
        code_challenge is bounced to the caller with invalid_request, never
        admitted toward the consent/upstream legs."""
        client_id = register_client(client, CLAUDE_REDIRECT)["client_id"]
        params = self._authorize_params(client_id)
        del params["code_challenge"], params["code_challenge_method"]
        resp = client.get(f"{_issuer_path()}/authorize", params=params)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith(CLAUDE_REDIRECT)
        error_query = self._error_redirect_params(resp)
        assert error_query["error"] == "invalid_request"
        assert "code" not in error_query  # no authorization code is granted

    def test_plain_code_challenge_method_rejected(self, client):
        """``plain`` must be rejected — S256 only (see AS-metadata test)."""
        client_id = register_client(client, CLAUDE_REDIRECT)["client_id"]
        resp = client.get(
            f"{_issuer_path()}/authorize",
            params=self._authorize_params(client_id, code_challenge_method="plain"),
        )
        assert resp.status_code == 302
        error_query = self._error_redirect_params(resp)
        assert error_query["error"] == "invalid_request"
        assert "code" not in error_query

    def test_unknown_client_rejected_with_invalid_request(self, client):
        _, challenge = _pkce_pair()
        resp = client.get(
            f"{_issuer_path()}/authorize",
            params={
                "response_type": "code",
                "client_id": "never-registered",
                "redirect_uri": CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_request"


# ===========================================================================
# DCR restricted to Claude's redirect URIs (docs/notes/auth.md)
# ===========================================================================


class TestDCRRestrictedToClaudeRedirects:
    """docs/notes/auth.md: "Dynamic Client Registration disabled for personal
    v1 (DCR lets anyone register)."

    Shipped model (ytt/auth.py): OAuthProxy requires an open registration
    endpoint, so the control is enforced at the *authorization* step —
    ``allowed_client_redirect_uris`` is Claude's two callbacks, and an
    authorize request for any other registered redirect URI is refused with
    400 invalid_request (never a redirect). These tests pin that enforcement
    point: a self-registered third-party (or loopback-http) redirect URI must
    be unable to complete an authorization, which is what keeps ytt's AS from
    acting as an open relay to arbitrary redirect targets.
    """

    @pytest.mark.parametrize(
        "evil_redirect",
        [
            "https://evil.example.com/callback",  # third-party collector
            "http://127.0.0.1:0/callback",  # loopback (public-client) variant
        ],
    )
    def test_foreign_redirect_uri_cannot_complete_authorization(
        self, client, evil_redirect
    ):
        registration = register_client(client, CLAUDE_REDIRECT, evil_redirect)
        assert registration["client_id"]
        assert registration["redirect_uris"] == [CLAUDE_REDIRECT, evil_redirect]

        _, challenge = _pkce_pair()
        resp = client.get(
            f"{_issuer_path()}/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": evil_redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "x",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"
        # No redirect may leave for the unregistered URI (open-redirect guard)
        assert "location" not in {k.lower() for k in resp.headers}

    def test_claude_redirect_uri_completes_authorization(self, client):
        """The allowlisted URI still walks the full flow — the restriction is
        scoped, not a blanket authorization lockout."""
        client_id = register_client(client, CLAUDE_REDIRECT)["client_id"]
        _, challenge = _pkce_pair()
        resp = client.get(
            f"{_issuer_path()}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "x",
            },
        )
        assert resp.status_code == 302
        assert f"{_issuer_path()}/consent" in resp.headers["location"]

    def test_registration_response_shape(self, client):
        """DCR response: a client_id is issued, the authorization-code grant
        is what's registered, and the scope offer includes the
        refresh-enabling set."""
        registration = register_client(client, CLAUDE_REDIRECT)
        assert registration["client_id"]
        assert "authorization_code" in registration["grant_types"]
        assert "refresh_token" in registration["grant_types"]
        for scope in ("openid", "email", "offline_access"):
            assert scope in registration.get("scope", "")


# ===========================================================================
# Path-bearing audience binding (RFC 8707) + issuer validation
# ===========================================================================


class TestPathBearingAudienceBinding:
    """Audience-bound token validation: tokens must be issued *for this
    server* (the path-bearing URL), and verifier must reject foreign
    audience/issuer (confused-deputy replay, research §5 "Token-type
    caveat")."""

    @pytest.fixture
    def provider(self):
        from ytt.auth import build_auth_provider
        from ytt.config import Settings

        return build_auth_provider(
            Settings(
                public_url="https://mcp.example.com/ytt",
                oauth_client_id="test-client-id",
                oauth_client_secret="test-client-secret",
            )
        )

    def test_fastmcp_token_audience_is_path_bearing_resource(self, provider):
        """FastMCP-issued access tokens are bound to the full resource URL —
        get_routes()/set_mcp_path() derives it from the path-bearing
        base_url (pinned fastmcp behavior the production config relies on)."""
        provider.get_routes(mcp_path="/ytt")  # triggers set_mcp_path()
        assert provider._jwt_issuer.issuer == "https://mcp.example.com/ytt"
        assert provider._jwt_issuer.audience == "https://mcp.example.com/ytt"

    def test_upstream_verifier_issuer_is_the_authentik_issuer(self, provider):
        """The id-token verifier is pinned to Authentik's per-application
        issuer (trailing slash included — OIDC discovery value), not to the
        resource URL, and its audience is the OIDC-mandated client_id
        (OIDC Core §2: id_token aud is the client, not the resource)."""
        from ytt.auth import AUTHENTIK_ISSUER

        verifier = provider._token_validator
        assert verifier.issuer == AUTHENTIK_ISSUER
        assert verifier.audience == "test-client-id"

    @staticmethod
    def _hs256_token(secret: str, claims: dict) -> str:
        from joserfc import jwk, jwt

        key = jwk.import_key(secret, "oct")
        return jwt.encode({"alg": "HS256"}, claims, key)

    def _claims(self, iss: str, aud: str) -> dict:
        now = int(time.time())
        return {
            "iss": iss,
            "aud": aud,
            "sub": "me@example.com",
            "email": "me@example.com",
            "exp": now + 3600,
            "iat": now,
        }

    @pytest.mark.asyncio
    async def test_hs256_id_token_accepted(self, provider):
        """The production token shape — Authentik HS256 id_token keyed by the
        client secret (see ytt/auth.py for why HS256 here is correct) — with
        matching iss and aud verifies to an AccessToken carrying the email
        the allowlist consumes."""
        verified = await provider._token_validator.verify_token(
            self._hs256_token(
                "test-client-secret",
                self._claims(
                    iss="https://sso.ardenone.com/application/o/ytt/",
                    aud="test-client-id",
                ),
            )
        )
        assert verified is not None
        assert verified.claims["email"] == "me@example.com"

    @pytest.mark.asyncio
    async def test_wrong_issuer_rejected(self, provider):
        """Issuer validation: an otherwise-valid token from a different
        issuer (e.g. another Authentik application — even another slug on the
        same host) must not verify."""
        verified = await provider._token_validator.verify_token(
            self._hs256_token(
                "test-client-secret",
                self._claims(
                    iss="https://sso.ardenone.com/application/o/someone-else/",
                    aud="test-client-id",
                ),
            )
        )
        assert verified is None

    @pytest.mark.asyncio
    async def test_wrong_audience_rejected(self, provider):
        """A token minted for a *different* application's client_id must not
        verify — the confused-deputy guard."""
        verified = await provider._token_validator.verify_token(
            self._hs256_token(
                "test-client-secret",
                self._claims(
                    iss="https://sso.ardenone.com/application/o/ytt/",
                    aud="ibkr-mcp-client-id",
                ),
            )
        )
        assert verified is None

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, provider):
        now = int(time.time())
        claims = self._claims(
            iss="https://sso.ardenone.com/application/o/ytt/", aud="test-client-id"
        )
        claims["exp"] = now - 60
        verified = await provider._token_validator.verify_token(
            self._hs256_token("test-client-secret", claims)
        )
        assert verified is None


# ===========================================================================
# Token-validation failures: signatures, temporal claims, key rotation,
# discovery/JWKS outages (docs/notes/auth.md § "Key rotation and
# token-validation failures")
# ===========================================================================


def _sign_hs256(secret: str, claims: dict) -> str:
    """Sign *claims* as an HS256 token keyed by *secret* — the shape the
    reference Authentik issues (see ytt/auth.py for why HS256 is correct)."""
    from joserfc import jwk, jwt

    key = jwk.import_key(secret, "oct")
    return jwt.encode({"alg": "HS256"}, claims, key)


def _id_token_claims(verifier, **overrides) -> dict:
    """Well-formed upstream id_token claims bound to *verifier*'s own
    iss/aud. An override of ``None`` removes the claim entirely (used to
    build malformed tokens)."""
    now = int(time.time())
    claims: dict = {
        "iss": verifier.issuer,
        "aud": verifier.audience,
        "sub": "me@example.com",
        "email": "me@example.com",
        "exp": now + 3600,
        "iat": now,
    }
    for name, value in overrides.items():
        if value is None:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


@pytest.fixture
def upstream_pair():
    """The provider built the production way plus its upstream id-token
    verifier (the ytt-built HS256 one, ``provider._token_validator``)."""
    from ytt.auth import build_auth_provider
    from ytt.config import Settings

    provider = build_auth_provider(
        Settings(
            public_url="https://mcp.example.com/ytt",
            oauth_client_id="test-client-id",
            oauth_client_secret="test-client-secret",
        )
    )
    return provider, provider._token_validator


class TestInvalidSignatureRejection:
    """Any token whose signature does not verify is rejected — ``None``
    (→ 401 invalid_token), never an exception escaping to the caller and
    never a partial accept. The signature check is the boundary: everything
    else in this section only bites on tokens that pass it."""

    @pytest.fixture
    def verifier(self, upstream_pair):
        return upstream_pair[1]

    @pytest.mark.asyncio
    async def test_attacker_signed_token_rejected(self, verifier):
        """Perfect claims, wrong key: a token signed with an attacker-chosen
        secret fails the MAC check even though iss/aud/exp all match."""
        assert (
            await verifier.verify_token(
                _sign_hs256("attacker-chosen-secret", _id_token_claims(verifier))
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_tampered_payload_rejected(self, verifier):
        """Re-encoding the payload with a different subject but the original
        signature is rejected — claims are not trusted without a re-sign."""
        import base64 as b64
        import json as jsonlib

        token = _sign_hs256("test-client-secret", _id_token_claims(verifier))
        header_b64, payload_b64, sig_b64 = token.split(".")
        payload = jsonlib.loads(b64.urlsafe_b64decode(payload_b64 + "=="))
        payload["sub"] = "attacker@example.com"
        payload["email"] = "attacker@example.com"
        forged_payload = (
            b64.urlsafe_b64encode(jsonlib.dumps(payload).encode())
            .rstrip(b"=")
            .decode()
        )
        assert (
            await verifier.verify_token(f"{header_b64}.{forged_payload}.{sig_b64}")
            is None
        )

    @pytest.mark.asyncio
    async def test_alg_none_rejected(self, verifier):
        """``alg: none`` (unsigned JWT) is not accepted regardless of the
        configured algorithm — the verifier's JWS registry pins HS256."""
        import base64 as b64
        import json as jsonlib

        def seg(obj) -> str:
            return (
                b64.urlsafe_b64encode(jsonlib.dumps(obj).encode())
                .rstrip(b"=")
                .decode()
            )

        unsigned = f'{seg({"alg": "none", "typ": "JWT"})}.{seg(_id_token_claims(verifier))}.'
        assert await verifier.verify_token(unsigned) is None

    @pytest.mark.asyncio
    async def test_malformed_tokens_rejected_not_raised(self, verifier):
        """Garbage inputs — not a JWT, empty string, missing signature
        segment — return ``None`` like any other rejection instead of
        raising through the auth middleware (a 500 on a malformed bearer
        would itself be a failure mode)."""
        for junk in ("not-a-jwt-at-all", "", "only.two"):
            assert await verifier.verify_token(junk) is None, f"junk={junk!r}"


class TestTemporalClaimEnforcement:
    """``exp`` is mandatory and ``nbf`` is honored on the upstream
    id_token. FastMCP's ``JWTVerifier`` checks ``exp`` only when present
    and never looks at ``nbf`` (pinned against 3.4.2); ytt's
    ``UpstreamIdTokenVerifier`` (ytt/auth.py) adds both checks (OIDC Core
    §2, RFC 7519 §4.1.5). These pins are the fail-closed answer to "what
    does ytt do with an expired, not-yet-valid, or never-expiring token":
    the first two die at FastMCP/JWT level, and the last is rejected here
    rather than granted an implicit infinite lifetime."""

    @pytest.fixture
    def verifier(self, upstream_pair):
        return upstream_pair[1]

    @pytest.mark.asyncio
    async def test_missing_exp_rejected(self, verifier):
        """A signed token with no ``exp`` claim is malformed (OIDC Core §2
        requires exp) and must not verify — otherwise a misbehaving IdP
        could mint a token ytt would honor forever."""
        assert (
            await verifier.verify_token(
                _sign_hs256("test-client-secret", _id_token_claims(verifier, exp=None))
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_not_yet_valid_beyond_leeway_rejected(self, verifier):
        """``nbf`` more than the leeway window in the future: not accepted."""
        from ytt.auth import NBF_LEEWAY_SECONDS

        now = int(time.time())
        token = _sign_hs256(
            "test-client-secret",
            _id_token_claims(verifier, nbf=now + NBF_LEEWAY_SECONDS + 60),
        )
        assert await verifier.verify_token(token) is None

    @pytest.mark.asyncio
    async def test_not_yet_valid_at_leeway_boundary_accepted(self, verifier):
        """The leeway boundary itself is inclusive (reject strictly beyond
        ``now + NBF_LEEWAY_SECONDS``) — a token validated within a second of
        issuance is never bounced for a rounding disagreement."""
        from ytt.auth import NBF_LEEWAY_SECONDS

        now = int(time.time())
        token = _sign_hs256(
            "test-client-secret", _id_token_claims(verifier, nbf=now + NBF_LEEWAY_SECONDS)
        )
        assert await verifier.verify_token(token) is not None

    @pytest.mark.asyncio
    async def test_past_nbf_and_absent_nbf_accepted(self, verifier):
        """``nbf`` in the past, and its absence, are both ordinary valid
        tokens — the check only refuses future-valid tokens."""
        now = int(time.time())
        with_past_nbf = _sign_hs256(
            "test-client-secret", _id_token_claims(verifier, nbf=now - 3600)
        )
        without_nbf = _sign_hs256("test-client-secret", _id_token_claims(verifier))
        assert await verifier.verify_token(with_past_nbf) is not None
        assert await verifier.verify_token(without_nbf) is not None


class TestUpstreamSecretRotation:
    """Rotating the upstream IdP client secret is the symmetric analog of a
    JWKS key rotation — and it is *immediate*. The verifier holds exactly
    one key (the configured secret): once ytt is redeployed with the new
    ``YTT_OAUTH_CLIENT_SECRET`` every old-signed token fails verification,
    with no dual-key acceptance window and no cache to flush. Operational
    consequences (sessions re-auth on their next upstream refresh) are in
    docs/notes/auth.md § "Key rotation and token-validation failures"."""

    @pytest.fixture
    def verifier(self, upstream_pair):
        return upstream_pair[1]

    @staticmethod
    def _rotated_verifier(verifier):
        from ytt.auth import UpstreamIdTokenVerifier

        return UpstreamIdTokenVerifier(
            public_key="rotated-client-secret",
            algorithm="HS256",
            issuer=verifier.issuer,
            audience=verifier.audience,
        )

    @pytest.mark.asyncio
    async def test_old_secret_tokens_die_at_rotation(self, verifier):
        """The same pre-rotation token verifies under the old secret's
        verifier and is rejected once the verifier holds the new secret."""
        token = _sign_hs256("test-client-secret", _id_token_claims(verifier))
        assert await verifier.verify_token(token) is not None
        assert await self._rotated_verifier(verifier).verify_token(token) is None

    @pytest.mark.asyncio
    async def test_new_secret_tokens_accepted_after_rotation(self, verifier):
        """Tokens the IdP signs with the rotated secret verify under the
        redeployed verifier — rotation is a flip, not a migration."""
        post = _sign_hs256(
            "rotated-client-secret", _id_token_claims(self._rotated_verifier(verifier))
        )
        assert await self._rotated_verifier(verifier).verify_token(post) is not None

    def test_verifier_holds_exactly_one_static_key(self, verifier):
        """The key is the settings value itself — static, offline, nothing
        to refresh from the IdP — which is why rotation cannot leave a
        stale-key window behind."""
        from ytt.auth import UpstreamIdTokenVerifier

        assert verifier.jwks_uri is None
        assert verifier.public_key == "test-client-secret"
        assert isinstance(verifier, UpstreamIdTokenVerifier)


class TestYTTSignedTokenRotation:
    """The tokens Claude actually presents are FastMCP-issued JWTs signed
    with ``YTT_JWT_SIGNING_SECRET`` — an independent second key, held only
    by ytt. Rotating it instantly invalidates every access AND refresh
    token (the "log everyone out now" lever); upstream-secret rotation can
    neither forge nor invalidate these tokens."""

    @pytest.fixture
    def issuer(self, upstream_pair):
        provider = upstream_pair[0]
        provider.get_routes(mcp_path="/ytt")  # materializes _jwt_issuer
        return provider._jwt_issuer

    def test_issued_token_verifies_locally(self, issuer):
        token = issuer.issue_access_token(
            client_id="test-client-id", scopes=["openid"], jti="jti-rotation-1"
        )
        payload = issuer.verify_token(token)  # raises JoseError if invalid
        assert payload["iss"] == issuer.issuer

    def test_rotated_signing_key_rejects_issued_tokens(self, issuer):
        """A verifier holding the rotated key rejects every token the old
        key signed — the whole fleet is logged out at once, fail closed."""
        from fastmcp.server.auth.jwt_issuer import JWTIssuer
        from joserfc.errors import JoseError

        token = issuer.issue_access_token(
            client_id="test-client-id", scopes=["openid"], jti="jti-rotation-2"
        )
        rotated = JWTIssuer(
            issuer=issuer.issuer,
            audience=issuer.audience,
            signing_key="rotated-ytt-signing-secret",
        )
        with pytest.raises(JoseError):
            rotated.verify_token(token)

    def test_expired_and_malformed_bearers_raise_jose_error(self, issuer):
        """Expired and malformed bearer tokens raise ``JoseError`` — the
        request path converts that to 401 invalid_token, never a 500."""
        from joserfc.errors import DecodeError, JoseError

        expired = issuer.issue_access_token(
            client_id="test-client-id", scopes=[], jti="jti-exp", expires_in=-10
        )
        with pytest.raises(JoseError):
            issuer.verify_token(expired)
        with pytest.raises(DecodeError):
            issuer.verify_token("garbage-not-a-jwt")

    def test_issued_tokens_carry_exp_and_no_nbf(self, issuer):
        """FastMCP-issued tokens carry ``exp`` (they die naturally) and no
        ``nbf`` — so the verifier's missing nbf check is moot on this leg;
        the nbf-enforcing ``UpstreamIdTokenVerifier`` covers the upstream
        id_token leg instead."""
        token = issuer.issue_access_token(
            client_id="test-client-id", scopes=["openid"], jti="jti-shape-1"
        )
        import base64 as b64
        import json as jsonlib

        payload_b64 = token.split(".")[1]
        claims = jsonlib.loads(b64.urlsafe_b64decode(payload_b64 + "=="))
        assert "exp" in claims
        assert "nbf" not in claims

    @pytest.mark.asyncio
    async def test_client_secret_rotation_rotates_the_derived_signing_key_too(
        self, upstream_pair
    ):
        """The reference deployment leaves ``YTT_JWT_SIGNING_SECRET``
        unset, and OAuthProxy then derives the FastMCP signing key from
        the upstream client secret (HKDF, fixed salt — deterministic
        across restarts). Rotating ``YTT_OAUTH_CLIENT_SECRET`` therefore
        rotates BOTH key families at once: every issued token dies at the
        signature check, not just upstream-signed ones. Same secret →
        interchangeable tokens (the property that makes restarts safe);
        rotated secret → every pre-rotation token rejected."""
        from joserfc.errors import JoseError

        from ytt.auth import build_auth_provider
        from ytt.config import Settings

        def build(secret: str):
            provider = build_auth_provider(
                Settings(
                    public_url="https://mcp.example.com/ytt",
                    oauth_client_id="test-client-id",
                    oauth_client_secret=secret,
                )
            )
            provider.get_routes(mcp_path="/ytt")
            return provider._jwt_issuer

        _, verifier = upstream_pair
        same_secret_issuer = build("test-client-secret")
        rotated_secret_issuer = build("rotated-client-secret")

        token = same_secret_issuer.issue_access_token(
            client_id="test-client-id", scopes=["openid"], jti="jti-derived-1"
        )
        # Same client secret → same derived key → token verifies on a
        # freshly-built provider (restart stability).
        assert same_secret_issuer.verify_token(token)["jti"] == "jti-derived-1"
        # Rotated client secret → different derived key → the pre-rotation
        # token is rejected at the signature check.
        with pytest.raises(JoseError):
            rotated_secret_issuer.verify_token(token)
        # The upstream verifier is a different object with the same secret
        # — the two key families are independent derivations, not one key.
        assert verifier.public_key == "test-client-secret"
        assert verifier.public_key != same_secret_issuer._jwt_key


class TestJWKSPathFailClosed:
    """The JWKS-based verifier — what ``OIDCProxy.get_token_verifier()``
    auto-builds by default, and what any RS256 IdP would require — fails
    closed at every step, and its one-hour cache is the *only* window in
    which a rotated-out key keeps working. The reference Authentik
    publishes an empty JWKS (that is why ytt overrides with symmetric
    verification, see ytt/auth.py); these pins prove that losing that
    override would 401 everything (fail closed), never accept an
    unverifiable token."""

    @staticmethod
    def _jwks_verifier():
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        return JWTVerifier(
            jwks_uri="https://sso.ardenone.com/application/o/ytt/jwks/",
            issuer="https://sso.ardenone.com/application/o/ytt/",
            audience="test-client-id",
            algorithm="RS256",
        )

    @staticmethod
    def _material(kid: str, verifier) -> tuple[str, dict]:
        """An RS256 token and the JWKS document publishing its key — one
        generated key shared by both, the way a real IdP publishes the
        public half of the key it signs with."""
        import time as time_mod

        from joserfc import jwk, jwt

        key = jwk.RSAKey.generate_key(parameters={"kid": kid})
        token = jwt.encode(
            {"alg": "RS256", "kid": kid},
            {
                "iss": verifier.issuer,
                "aud": verifier.audience,
                "sub": "me@example.com",
                "exp": int(time_mod.time()) + 3600,
            },
            key,
        )
        return token, {"keys": [key.as_dict(private=False)]}

    @pytest.mark.asyncio
    async def test_empty_jwks_rejects(self):
        """The live reference-IdP shape — ``{}``, no keys — rejects every
        token: the default JWKS path on this IdP is fail-closed-forever,
        which is precisely why the symmetric override exists."""
        from unittest.mock import AsyncMock, patch

        verifier = self._jwks_verifier()
        token, _ = self._material("kid-a", verifier)
        with patch.object(
            verifier, "_fetch_jwks", AsyncMock(return_value={"keys": []})
        ):
            assert await verifier.verify_token(token) is None

    @pytest.mark.asyncio
    async def test_jwks_outage_rejects_without_cache(self):
        """A JWKS fetch outage with a cold cache rejects the token — a
        verification key is never conjured from nothing."""
        import httpx
        from unittest.mock import AsyncMock, patch

        verifier = self._jwks_verifier()
        token, _ = self._material("kid-a", verifier)
        with patch.object(
            verifier,
            "_fetch_jwks",
            AsyncMock(side_effect=httpx.ConnectError("jwks down")),
        ):
            assert await verifier.verify_token(token) is None

    @pytest.mark.asyncio
    async def test_cached_key_served_within_ttl_through_outage(self):
        """Once a key is cached, an IdP outage does NOT revoke it: tokens
        under the cached kid keep verifying for the cache TTL. This is the
        "uses cached keys" answer — bounded staleness, not immediate
        lockout and not unbounded trust."""
        import httpx
        from unittest.mock import AsyncMock, patch

        verifier = self._jwks_verifier()
        token, doc_a = self._material("kid-a", verifier)

        with patch.object(verifier, "_fetch_jwks", AsyncMock(return_value=doc_a)):
            assert await verifier.verify_token(token) is not None  # warms cache

        with patch.object(
            verifier,
            "_fetch_jwks",
            AsyncMock(side_effect=httpx.ConnectError("jwks down")),
        ):
            # cached kid still verifies through the outage...
            assert await verifier.verify_token(token) is not None
            # ...but an unknown kid cannot be resolved and is rejected.
            stranger, _ = self._material("kid-never-published", verifier)
            assert await verifier.verify_token(stranger) is None

    def test_cache_ttl_is_one_hour(self):
        """The documented rotation-staleness bound: a key removed from the
        live JWKS keeps validating for at most this long. If FastMCP ever
        changes the TTL, this pin forces the runbook number to be revisited
        (docs/notes/auth.md § Key rotation)."""
        assert self._jwks_verifier()._cache_ttl == 3600

    @pytest.mark.asyncio
    async def test_rotated_out_key_rejected_once_cache_expires(self):
        """After the JWKS rotates kid-a → kid-b: within the TTL the old key
        still verifies (cached); once the cache expires the old kid's
        tokens are rejected and the new kid's tokens verify."""
        from unittest.mock import AsyncMock, patch

        verifier = self._jwks_verifier()
        old_token, doc_a = self._material("kid-a", verifier)
        new_token, doc_b = self._material("kid-b", verifier)

        with patch.object(verifier, "_fetch_jwks", AsyncMock(return_value=doc_a)):
            assert await verifier.verify_token(old_token) is not None

        # Cross the 1h TTL without sleeping it: reaching into the private
        # cache timestamp is the honest way to test time here.
        verifier._jwks_cache_time = 0.0

        with patch.object(verifier, "_fetch_jwks", AsyncMock(return_value=doc_b)):
            assert await verifier.verify_token(old_token) is None
            assert await verifier.verify_token(new_token) is not None


class TestDiscoveryOutageFailClosed:
    """The IdP discovery document is fetched exactly once — eagerly, at
    provider construction, i.e. at server startup. An outage there prevents
    startup: no server comes up at all (fail closed — an unreachable IdP
    can never produce an unauthenticated ytt). After startup, validation is
    fully offline (symmetric keys from settings), so a *later* IdP outage
    degrades only new logins; existing sessions keep validating."""

    def test_unreachable_discovery_fails_startup(self):
        """If the config URL cannot be fetched at construction, provider
        construction raises and the server never binds."""
        from unittest.mock import patch

        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        from ytt.auth import build_auth_provider
        from ytt.config import Settings

        settings = Settings(
            public_url="https://mcp.example.com/ytt",
            oauth_client_id="test-client-id",
            oauth_client_secret="test-client-secret",
        )
        with patch.object(
            OIDCProxy,
            "get_oidc_configuration",
            side_effect=RuntimeError("simulated IdP discovery outage"),
        ):
            with pytest.raises(RuntimeError, match="discovery outage"):
                build_auth_provider(settings)

    def test_post_startup_validation_is_offline(self, upstream_pair):
        """Structural pin for the no-runtime-discovery property: the
        upstream verifier holds a static symmetric key (no ``jwks_uri``)
        and the client-facing verifier signs/validates with a local key —
        token validation performs zero network I/O against the IdP."""
        provider, verifier = upstream_pair
        assert verifier.jwks_uri is None
        assert verifier.public_key == "test-client-secret"
        provider.get_routes(mcp_path="/ytt")
        assert provider._jwt_issuer is not None


# ===========================================================================
# Subject allowlist coverage across every protected tool
# ===========================================================================


class TestSubjectAllowlistCoverage:
    """AuthZ (``YTT_ALLOWED_SUBJECTS``) covers every tool, fail-closed, with
    no token present — and the public/protected route split is exactly the
    documented one."""

    @pytest.mark.asyncio
    async def test_every_registered_tool_denied_without_token(self):
        """The AuthMiddleware gate (AuthZ enforcement point wired in
        ytt/server.py) denies every registered tool when no token resolves —
        swept over the live registry so a newly added tool is automatically
        covered."""
        from fastmcp import Client

        from ytt.server import mcp

        all_tools = await _list_tools_bypassing_auth()
        names = sorted(t.name for t in all_tools)
        assert names, "no tools registered — sweep would be vacuous"
        tools = {t.name: t for t in all_tools}
        async with Client(mcp) as client:
            for name in names:
                try:
                    result = await client.call_tool(name, _probe_args(tools[name]))
                except Exception as exc:
                    assert "uthorization" in str(exc), (
                        f"tool {name!r} failed for an unexpected reason: {exc}"
                    )
                else:
                    pytest.fail(
                        f"tool {name!r} executed without a token: "
                        f"is_error={result.is_error}"
                    )

    def test_public_health_and_metrics_stay_open(self, client):
        """The probe surface stays unauthenticated (k8s liveness/ServiceMonitor
        target the ClusterIP directly) — guards against an over-broad auth
        change breaking probes."""
        assert client.get(f"{_issuer_path()}/health").status_code == 200
        assert client.get(f"{_issuer_path()}/metrics").status_code == 200


# ===========================================================================
# Full discovery chain (research §2 — the algorithm a client actually walks)
# ===========================================================================


class TestDiscoveryChain:
    """Walk the chain exactly as an MCP client does, with no prior knowledge
    of ytt's URLs beyond the connector URL:

    POST <connector> (401) → WWW-Authenticate resource_metadata URL →
    PRM document → authorization_servers[0] → RFC 8414 path-inserted AS
    metadata → issuer round-trip.
    """

    def test_full_client_discovery_walk(self, client):
        # 1. Unauthenticated probe → 401 challenge
        first = client.post(
            _issuer_path(), json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        )
        assert first.status_code == 401
        _, params = _parse_www_authenticate(first.headers["www-authenticate"])

        # 2. Fetch the challenged resource_metadata URL
        prm = client.get(params["resource_metadata"])
        assert prm.status_code == 200
        prm_doc = prm.json()

        # 3. resource == the audience this server expects (RFC 8707)
        assert prm_doc["resource"] == get_settings().public_url

        # 4. Pick the first advertised authorization server
        issuer = prm_doc["authorization_servers"][0]

        # 5. RFC 8414 path-inserted AS metadata (well-known inserted between
        #    host and path), and its issuer must round-trip to the same URL
        as_url = _well_known_url(issuer, "/.well-known/oauth-authorization-server")
        as_meta = client.get(as_url)
        assert as_meta.status_code == 200, f"AS metadata unreachable at {as_url}"
        assert as_meta.json()["issuer"] == issuer

        # 6. The advertised endpoints must be walkable URLs under the issuer
        for field in (
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
        ):
            advertised = urlparse(str(as_meta.json()[field]))
            issuer_parsed = urlparse(issuer)
            assert advertised.scheme == issuer_parsed.scheme
            assert advertised.netloc == issuer_parsed.netloc
            assert advertised.path.startswith(issuer_parsed.path + "/")
