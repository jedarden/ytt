"""End-to-end OAuth 2.1 authorization-code + token-validation suite.

Where :mod:`tests.unit.test_oauth_conformance` pins each surface in
isolation (metadata documents, the authorize leg, verifier-level unit
checks), this module walks the **complete** authorization-code grant the
way Claude's connector does it and then uses the *actually issued* token
on the MCP transport:

    DCR → /authorize (S256 PKCE) → consent approve → upstream IdP
    (mocked) → /auth/callback → client code → POST /token
    → FastMCP-issued JWT → Bearer on POST /ytt

``docs/notes/auth.md`` requirements pinned here that no other module
covers end-to-end:

- **Token issuance**: the /token leg succeeds only with the PKCE verifier
  bound at /authorize (wrong verifier → ``invalid_grant``), the code is
  single-use and redirect-bound, and the issued access token is a
  FastMCP JWT whose ``aud``/``iss`` are the **path-bearing** resource URL
  (RFC 8707 audience binding) with the configured one-week lifetime.
- **Audience-bound bearer validation on MCP requests**: a bearer token is
  admitted on ``POST /ytt`` only when the FastMCP JWT's issuer, audience,
  signature, expiry and token-use all validate *and* the token was
  actually issued by this AS (the JTI reference must resolve) — every
  other case 401s with ``invalid_token``.
- **401 vs 403**: transport auth (401 + challenge) is a different layer
  from the subject allowlist — a validly-authenticated caller whose
  ``email`` the allowlist does not admit is denied by AuthZ (403 on
  /admin/egress, empty tool list on the MCP transport), never a 401.

No network: the upstream IdP's token endpoint is answered by an
``httpx.MockTransport`` injected into the OAuthProxy's authlib client,
and the IdP's browser legs (authorize redirect back to
``/auth/callback``) are simulated with the same TestClient. The
``conftest`` fake OIDC discovery document defines the upstream URLs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from ytt.config import get_settings

# ===========================================================================
# Constants + helpers
# ===========================================================================

BASE = "https://mcp.ardenone.com"

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"

#: The email the mocked upstream IdP authenticates. Not in any test
#: allowlist; tests that exercise the admitted path add it explicitly via
#: :func:`_allowlist_for_test`.
SUBJECT = "transcript-fan@example.com"

#: fastmcp_access_token_expiry_seconds in ytt/auth.py — the one-week
#: client-facing TTL, decoupled from Authentik's 5-minute upstream TTL.
ONE_WEEK_SECONDS = 7 * 24 * 3600


def _issuer_path() -> str:
    return urlparse(get_settings().public_url).path.rstrip("/")


def _pkce_pair() -> tuple[str, str]:
    """RFC 7636 §4.1–4.2 — S256 (verifier, challenge) pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _query_params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _form_fields(request: httpx.Request) -> dict[str, str]:
    """Parse a urlencoded request body (authlib's token POSTs)."""
    return dict(
        pair.split("=", 1) for pair in request.content.decode().split("&")
    )


@pytest.fixture
def allowlist(monkeypatch):
    """Point the request-time subject allowlist at *subjects* for one test.

    ``check_subject_auth`` reads ``get_settings()`` per request, so the
    cached settings must be dropped after the env change — and the cache
    dropped again at teardown so the next test rebuilds settings from the
    restored environment. Fixture teardown runs before ``monkeypatch``'s
    env restore (a fixture finalizes before the dependencies it asked
    for), so clearing here leaves no window in which the patched env can
    be re-cached.
    """

    def _allow(*subjects: str) -> None:
        monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", ",".join(subjects))
        get_settings.cache_clear()

    yield _allow
    get_settings.cache_clear()


# ===========================================================================
# Upstream IdP mock
# ===========================================================================


class _MockUpstreamIdP:
    """Authentik stand-in answering the proxy's outbound token requests.

    Serves both grants the proxy can initiate against the upstream token
    endpoint from the conftest fake OIDC config
    (``https://sso.ardenone.com/application/o/token/``):

    - ``authorization_code`` — a token set whose ``id_token`` is a real
      HS256 JWT signed with the OAuth client secret (this IdP's signing
      mode — see ``ytt/auth.py`` for why HS256 is correct here) carrying
      the issuer / audience / email the production id token has;
    - ``refresh_token`` — a rotated upstream token set.

    Records every request so tests can assert what the proxy actually
    sent upstream (e.g. that the proxy's own PKCE verifier rode along).
    """

    def __init__(self, idp_code: str = "mock-idp-code") -> None:
        self.idp_code = idp_code
        self.requests: list[httpx.Request] = []

    def _id_token(self) -> str:
        from joserfc import jwk, jwt

        from ytt.config import DEFAULT_OIDC_ISSUER

        now = int(time.time())
        key = jwk.import_key(get_settings().oauth_client_secret, "oct")
        return jwt.encode(
            {"alg": "HS256"},
            {
                "iss": DEFAULT_OIDC_ISSUER,
                "aud": get_settings().oauth_client_id,
                "sub": SUBJECT,
                "email": SUBJECT,
                "exp": now + 3600,
                "iat": now,
            },
            key,
        )

    def _token_set(self) -> dict:
        return {
            "access_token": "upstream-access-token",
            "id_token": self._id_token(),
            "refresh_token": "upstream-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid email offline_access",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        grant = _form_fields(request).get("grant_type", "")
        if grant in ("authorization_code", "refresh_token"):
            return httpx.Response(200, json=self._token_set())
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


@pytest.fixture
def mock_idp(monkeypatch) -> _MockUpstreamIdP:
    """Route the OAuthProxy's upstream authlib client through a MockTransport.

    ``AsyncOAuth2Client`` is resolved from the ``oauth_proxy.proxy`` module
    namespace at client-construction time (i.e. per request), so patching
    the module attribute covers the code exchange and any later transparent
    refresh regardless of when the app under test was built.
    """
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    from fastmcp.server.auth.oauth_proxy import proxy as oauth_proxy_module

    idp = _MockUpstreamIdP()

    class _MockTransportClient(AsyncOAuth2Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(idp.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(oauth_proxy_module, "AsyncOAuth2Client", _MockTransportClient)
    return idp


# ===========================================================================
# ASGI client + flow-walking helpers
# ===========================================================================


@pytest.fixture
def client():
    """TestClient over the real ASGI app; redirects NOT followed so each hop
    of the OAuth flow can be inspected and driven explicitly. The context
    manager runs the app's lifespan, which initializes FastMCP's streamable
    HTTP session manager — without it any request that gets *past* bearer
    auth 500s instead of reaching the MCP transport (a 401 at the middleware
    never gets that far, which is why the unauthenticated-challenge tests
    would mask the omission)."""
    from ytt.server import build_asgi_app

    with TestClient(
        build_asgi_app(), raise_server_exceptions=False, follow_redirects=False
    ) as test_client:
        yield test_client


def register_client(client: TestClient, *redirect_uris: str) -> dict:
    """DCR-register a client and return the registration response JSON."""
    resp = client.post(
        f"{BASE}{_issuer_path()}/register",
        json={
            "client_name": "oauth-end-to-end-suite",
            "redirect_uris": list(redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def walk_full_flow(
    client: TestClient, mock_idp: _MockUpstreamIdP, *, state: str = "e2e-state"
) -> tuple[str, str, str]:
    """Walk the complete grant up to (not including) POST /token.

    DCR → /authorize (S256 PKCE) → consent GET+approve → upstream redirect
    (simulated: the IdP "returns" to /auth/callback with its code) →
    redirect carrying ytt's client-facing authorization code.

    Returns ``(client_id, client_code, code_verifier)`` — everything a real
    MCP client holds when it redeems the code at POST /token.
    """
    client_id = register_client(client, CLAUDE_REDIRECT)["client_id"]
    verifier, challenge = _pkce_pair()

    # 1. /authorize with S256 PKCE → consent page
    r_authorize = client.get(
        f"{BASE}{_issuer_path()}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "openid email offline_access",
        },
    )
    assert r_authorize.status_code == 302, r_authorize.text[:300]
    consent_url = r_authorize.headers["location"]
    assert consent_url.startswith(f"{BASE}{_issuer_path()}/consent"), consent_url

    # 2. Consent: GET the page (CSRF token), then approve
    r_consent = client.get(consent_url)
    assert r_consent.status_code == 200, r_consent.text[:300]
    match = re.search(r'name="csrf_token" value="([^"]+)"', r_consent.text)
    assert match, "consent page carries no csrf_token form field"
    r_approval = client.post(
        f"{BASE}{_issuer_path()}/consent",
        data={
            "txn_id": _query_params(consent_url)["txn_id"],
            "csrf_token": match.group(1),
            "action": "approve",
        },
    )
    assert r_approval.status_code == 302, r_approval.text[:300]

    # 3. Upstream IdP leg — in production a browser round-trip to Authentik
    #    and back; here the proxy's upstream-redirect `state` is the txn id,
    #    and the IdP "returns" with its own code via /auth/callback. The
    #    consent-binding cookie from step 2 must ride along (same client).
    upstream_query = _query_params(r_approval.headers["location"])
    r_callback = client.get(
        f"{BASE}{_issuer_path()}/auth/callback",
        params={"code": mock_idp.idp_code, "state": upstream_query["state"]},
    )
    assert r_callback.status_code == 302, r_callback.text[:300]
    assert r_callback.headers["location"].startswith(CLAUDE_REDIRECT), (
        r_callback.headers["location"]
    )

    # 4. The redirect to Claude's callback carries OUR authorization code —
    #    and must round-trip the caller's original state.
    client_query = _query_params(r_callback.headers["location"])
    assert client_query["state"] == state, "caller state lost at the callback"
    assert client_query["code"]
    return client_id, client_query["code"], verifier


def exchange_code(
    client: TestClient,
    client_id: str,
    code: str,
    verifier: str,
    *,
    redirect_uri: str = CLAUDE_REDIRECT,
) -> httpx.Response:
    """POST /token — redeem an authorization code for ytt-issued tokens."""
    return client.post(
        f"{BASE}{_issuer_path()}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )


def redeem_refresh_token(
    client: TestClient, client_id: str, refresh_token: str
) -> httpx.Response:
    """POST /token with grant_type=refresh_token."""
    return client.post(
        f"{BASE}{_issuer_path()}/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


#: The streamable-HTTP transport refuses any request whose Accept header
#: does not offer both types (406 before any protocol handling).
_ACCEPT_JSON = {"Accept": "application/json, text/event-stream"}


def mcp_json(resp: httpx.Response) -> dict:
    """The JSON-RPC payload carried by an MCP transport reply.

    The streamable-HTTP transport may answer a POST with application/json
    or with SSE framing (the spec lets the server choose; this client
    offers both Accept types and gets SSE), where the JSON-RPC response
    rides in ``data:`` lines.
    """
    if resp.headers["content-type"].startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[len("data:"):].strip())
                if isinstance(payload, dict) and (
                    "result" in payload or "error" in payload
                ):
                    return payload
        raise AssertionError(f"no JSON-RPC response in SSE body: {resp.text[:300]}")
    return resp.json()


def mcp_request(
    client: TestClient,
    token: str | None,
    method: str,
    session_id: str | None = None,
    **params,
) -> httpx.Response:
    """POST a JSON-RPC message to the MCP transport with an optional bearer."""
    headers = {**_ACCEPT_JSON}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    return client.post(
        _issuer_path(),
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers=headers,
    )


def open_mcp_session(client: TestClient, token: str) -> str:
    """Run the MCP initialize handshake and return the session id.

    A real MCP client cannot skip this: the streamable-HTTP transport only
    assigns an ``mcp-session-id`` after ``initialize`` +
    ``notifications/initialized``, and later requests without it are a
    protocol error — so the bearer-validation tests below talk to the
    transport the way Claude actually does, not with a bare tools/list.
    """
    resp = mcp_request(
        client,
        token,
        "initialize",
        protocolVersion="2025-06-18",
        capabilities={},
        clientInfo={"name": "oauth-end-to-end-suite", "version": "0"},
    )
    assert resp.status_code == 200, resp.text[:300]
    session_id = resp.headers["mcp-session-id"]
    initialized = client.post(
        _issuer_path(),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={
            **_ACCEPT_JSON,
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session_id,
        },
    )
    assert initialized.status_code == 202, initialized.text[:300]
    return session_id


# ===========================================================================
# Issued-token variants for the MCP bearer validator
# ===========================================================================


def _mint_bearer_variants() -> dict[str, str]:
    """Well-formed JWT bearer variants, each wrong in exactly one respect —
    the input space of the MCP transport's token validator.

    The signing key is derived deterministically from the OAuth client
    secret (``jwt_signing_secret`` is unset in the unit environment), so a
    provider rebuilt from the cached settings carries the *same* key as the
    one inside the app under test — which is what makes each variant an
    isolated, single-property failure.
    """
    from joserfc import jwk, jwt

    from fastmcp.server.auth.jwt_issuer import JWTIssuer

    from ytt.auth import build_auth_provider

    provider = build_auth_provider(get_settings())
    # The JWT issuer is created by set_mcp_path(), via get_routes() — the
    # same call the app build makes (tests/unit/test_oauth_conformance.py
    # pins this). Without it jwt_issuer raises "not initialized".
    provider.get_routes(mcp_path=_issuer_path())
    issuer = str(provider.jwt_issuer.issuer)
    audience = str(provider.jwt_issuer.audience)
    signing_key = provider.jwt_issuer._signing_key

    def _encode(claims: dict, key) -> str:
        return jwt.encode({"alg": "HS256"}, claims, jwk.import_key(key, "oct"))

    # Bound to the bare origin instead of the path-bearing resource URL —
    # the RFC 8707 confused-deputy shape (a token minted for some *other*
    # resource on the same host).
    origin_audience = f"{urlparse(audience).scheme}://{urlparse(audience).netloc}"

    return {
        # Signed by this AS for the right audience, but never issued: the
        # JTI reference does not resolve server-side (reference-token pin).
        "unknown_jti": provider.jwt_issuer.issue_access_token(
            client_id="some-client", scopes=["openid"], jti="never-issued-jti"
        ),
        "wrong_audience": JWTIssuer(
            issuer=issuer, audience=origin_audience, signing_key=signing_key
        ).issue_access_token(client_id="some-client", scopes=["openid"], jti="x"),
        # The mirror image: bound to this resource and signed by this AS's
        # own key, but minted in the name of a *different* authorization
        # server — the iss claim does not name this issuer, so possession
        # of the key proves nothing about where the token came from.
        "wrong_issuer": _encode(
            {
                "iss": "https://evil.example.com",
                "aud": audience,
                "client_id": "some-client",
                "scope": "openid",
                "exp": int(time.time()) + 600,
                "iat": int(time.time()),
                "jti": "x",
            },
            signing_key,
        ),
        # Correct audience, expired one minute ago (research §5: "reject
        # expired tokens with 401").
        "expired": provider.jwt_issuer.issue_access_token(
            client_id="some-client", scopes=["openid"], jti="x", expires_in=-60
        ),
        # Right claims, signed by someone else.
        "wrong_signature": _encode(
            {
                "iss": issuer,
                "aud": audience,
                "client_id": "some-client",
                "scope": "openid",
                "exp": int(time.time()) + 600,
                "iat": int(time.time()),
                "jti": "x",
            },
            b"attacker-key-at-least-12ch!",
        ),
        # The refresh reference presented where an access token belongs.
        "refresh_as_access": provider.jwt_issuer.issue_refresh_token(
            client_id="some-client", scopes=["openid"], jti="x", expires_in=600
        ),
    }


# ===========================================================================
# The full authorization-code grant → token issuance
# ===========================================================================


class TestAuthorizationCodeEndToEnd:
    """DCR → authorize (S256 PKCE) → consent → IdP callback → code →
    POST /token → FastMCP-issued JWT — the complete OAuth 2.1 grant."""

    def test_full_flow_issues_fastmcp_tokens(self, client, mock_idp):
        """The complete flow ends in a token response — and the issued
        access token is a FastMCP JWT bound to the path-bearing resource URL
        with the configured one-week lifetime."""
        client_id, code, verifier = walk_full_flow(client, mock_idp)

        # The upstream exchange really happened, and the proxy's own PKCE
        # verifier was forwarded (forward_pkce=True).
        assert len(mock_idp.requests) == 1
        upstream_form = _form_fields(mock_idp.requests[0])
        assert upstream_form["code"] == mock_idp.idp_code
        assert upstream_form["code_verifier"]

        resp = exchange_code(client, client_id, code, verifier)
        assert resp.status_code == 200, resp.text[:300]

        body = resp.json()
        assert body["token_type"] == "Bearer"
        # One-week client-facing TTL — valid only because the upstream
        # response carried a refresh token (the clamp in ytt/auth.py).
        assert body["expires_in"] == ONE_WEEK_SECONDS
        # offline_access in the granted scope ⇒ a refresh token was issued
        # (the 0.2.13 fix chain in ytt/auth.py).
        for scope in ("openid", "email", "offline_access"):
            assert scope in body["scope"]
        assert body["refresh_token"]

        # The issued access token validates against the real provider's
        # issuer — signature, expiry, issuer and audience all checked —
        # and carries exactly the documented RFC 8707 binding.
        from ytt.auth import build_auth_provider

        provider = build_auth_provider(get_settings())
        provider.get_routes(mcp_path=_issuer_path())

        payload = provider.jwt_issuer.verify_token(body["access_token"])
        assert payload["aud"] == get_settings().public_url
        assert payload["iss"] == get_settings().public_url
        assert payload["exp"] - payload["iat"] == ONE_WEEK_SECONDS
        assert payload["client_id"] == client_id
        assert payload["jti"]
        assert payload["scope"] == body["scope"]

        # The refresh reference is a distinct, correctly-typed token.
        refresh_payload = provider.jwt_issuer.verify_token(
            body["refresh_token"], expected_token_use="refresh"
        )
        assert refresh_payload["token_use"] == "refresh"
        assert refresh_payload["client_id"] == client_id

    def test_pkce_verifier_is_enforced_at_the_token_endpoint(self, client, mock_idp):
        """The S256 challenge bound at /authorize must be redeemed with the
        matching verifier: a wrong verifier is invalid_grant, and the code
        itself remains redeemable by the legitimate verifier."""
        client_id, code, verifier = walk_full_flow(client, mock_idp)

        wrong_verifier = _pkce_pair()[0]
        assert wrong_verifier != verifier
        resp = exchange_code(client, client_id, code, wrong_verifier)
        # 401, not RFC 6749 §5.2's 400: the MCP-spec transform in FastMCP's
        # TokenHandler rewrites invalid_grant to 401 ("Invalid or expired
        # tokens MUST receive a HTTP 401 response"). It is still an
        # invalid_grant body — the OAuth error code is what clients act on.
        assert resp.status_code == 401, resp.text[:300]
        assert resp.json()["error"] == "invalid_grant"
        assert resp.headers["cache-control"] == "no-store"

        # The rejection was the verifier's doing — the right one succeeds.
        ok = exchange_code(client, client_id, code, verifier)
        assert ok.status_code == 200, ok.text[:300]

    def test_authorization_code_is_single_use(self, client, mock_idp):
        """RFC 6749 §4.1.2: the code redeems once; replay is rejected and
        mints nothing."""
        client_id, code, verifier = walk_full_flow(client, mock_idp)

        first = exchange_code(client, client_id, code, verifier)
        assert first.status_code == 200, first.text[:300]

        replay = exchange_code(client, client_id, code, verifier)
        # 401 invalid_grant — the MCP-spec transform (see the PKCE test).
        assert replay.status_code == 401
        assert replay.json()["error"] == "invalid_grant"
        assert "access_token" not in replay.json()

    def test_code_is_bound_to_the_authorized_redirect_uri(self, client, mock_idp):
        """A code redeemed with a redirect_uri other than the one used at
        /authorize is invalid_grant — even with the correct verifier (the
        other Claude callback is a *registered* URI, so this pins the
        binding, not the allowlist)."""
        client_id, code, verifier = walk_full_flow(client, mock_idp)

        resp = exchange_code(
            client,
            client_id,
            code,
            verifier,
            redirect_uri="https://claude.com/api/mcp/auth_callback",
        )
        # Redirect-binding mismatch is invalid_request (RFC 6749 §10.6
        # shape implemented in the token handler), and unlike invalid_grant
        # it stays at 400 — the MCP-spec 401 transform is scoped to
        # invalid_grant only.
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"


@pytest.fixture(scope="module")
def bearer_variants() -> dict[str, str]:
    """The wrong-token input space, minted once — building the provider is
    the expensive part, and none of the variants touch per-test state."""
    return _mint_bearer_variants()


# ===========================================================================
# Rejected grant attempts — non-PKCE and foreign-redirect DCR
# ===========================================================================


class TestRejectedGrantsNeverMintTokens:
    """tests/unit/test_oauth_conformance.py pins the /authorize-leg
    rejections in isolation (missing/plain challenge, foreign redirect
    URI); this class walks the same rejections as *complete grant
    attempts* to their shared terminus — nothing is ever issued, and the
    token leg itself yields nothing. These are the two refusal classes
    docs/notes/auth.md calls out: non-PKCE (OAuth 2.1 mandates S256) and
    DCR clients whose redirect URI is not one of Claude's ("DCR lets
    anyone register" — the proxy's registration facade accepts the
    client, the authorization step refuses the grant)."""

    @staticmethod
    def _redeem_fabricated_code(client: TestClient, redirect_uri: str) -> httpx.Response:
        """POST /token for a code that was never issued — the leg a client
        would still have to run if it ignored the authorize rejection."""
        client_id = register_client(client, CLAUDE_REDIRECT)["client_id"]
        return exchange_code(
            client, client_id, "never-issued-code", _pkce_pair()[0],
            redirect_uri=redirect_uri,
        )

    @pytest.mark.parametrize("challenge_mode", ["absent", "plain"])
    def test_non_pkce_grant_never_yields_a_token(self, client, challenge_mode):
        """An /authorize attempt without S256 PKCE dies at the first hop —
        the caller's redirect carries invalid_request and no code — and the
        token endpoint confirms the vault stays shut: even a fabricated
        redemption is rejected."""
        client_id = register_client(client, CLAUDE_REDIRECT)["client_id"]
        verifier, _ = _pkce_pair()
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_REDIRECT,
            "state": "no-pkce",
            "scope": "openid email offline_access",
        }
        if challenge_mode == "absent":
            pass  # RFC 7636 parameters omitted entirely
        else:  # the pre-OAuth-2.1 `plain` downgrade
            params["code_challenge"] = verifier
            params["code_challenge_method"] = "plain"

        resp = client.get(f"{BASE}{_issuer_path()}/authorize", params=params)
        assert resp.status_code == 302, resp.text[:300]
        assert resp.headers["location"].startswith(CLAUDE_REDIRECT)
        error_query = _query_params(resp.headers["location"])
        assert error_query["error"] == "invalid_request"
        assert "code" not in error_query, "a challenge-less grant was issued a code"

        # Nothing was issued, so there is nothing to redeem — and the token
        # endpoint does not mint on faith either.
        token_resp = self._redeem_fabricated_code(client, CLAUDE_REDIRECT)
        assert token_resp.status_code == 401, token_resp.text[:300]
        assert token_resp.json()["error"] == "invalid_grant"

    def test_dcr_client_with_foreign_redirect_never_yields_a_token(
        self, client
    ):
        """The DCR attempt that *is* accepted (registration succeeds — it is
        the proxy's client-facing facade) still cannot complete a grant for
        a non-Claude redirect URI: /authorize refuses with 400 and no
        redirect, and the token leg mints nothing. Registration alone buys
        nothing — the subject allowlist behind a valid token is the next
        gate, and no token is reachable from here."""
        evil_redirect = "https://evil.example.com/callback"
        registration = register_client(client, CLAUDE_REDIRECT, evil_redirect)
        assert registration["redirect_uris"] == [CLAUDE_REDIRECT, evil_redirect]

        _, challenge = _pkce_pair()
        resp = client.get(
            f"{BASE}{_issuer_path()}/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": evil_redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "dcr-evil",
            },
        )
        assert resp.status_code == 400, resp.text[:300]
        assert resp.json()["error"] == "invalid_request"
        # No redirect may leave for the unregistered URI (open-redirect guard)
        assert "location" not in {k.lower() for k in resp.headers}

        token_resp = self._redeem_fabricated_code(client, evil_redirect)
        assert token_resp.status_code == 401, token_resp.text[:300]
        assert token_resp.json()["error"] == "invalid_grant"


# ===========================================================================
# Audience-bound bearer validation on the MCP transport
# ===========================================================================


class TestBearerValidationOnMCPRequests:
    """Every way a bearer token can be wrong — audience, signature, expiry,
    token-use, or never-issued reference — must 401 with ``invalid_token``
    on the MCP transport; only a token actually issued by this AS for this
    resource gets past transport auth."""

    def test_full_flow_token_is_admitted_on_mcp(self, client, mock_idp, allowlist):
        """The happy path: a token from the real issuance flow authenticates
        on POST /ytt — with its subject allowlisted, tools/list answers with
        the real tool set (past transport auth AND past AuthZ)."""
        client_id, code, verifier = walk_full_flow(client, mock_idp)
        tokens = exchange_code(client, client_id, code, verifier).json()

        allowlist(SUBJECT)
        session_id = open_mcp_session(client, tokens["access_token"])
        resp = mcp_request(
            client, tokens["access_token"], "tools/list", session_id=session_id
        )
        assert resp.status_code == 200, resp.text[:300]
        assert mcp_json(resp)["result"]["tools"], "allowlisted caller saw no tools"

    def test_issued_token_denied_for_non_allowlisted_subject(
        self, client, mock_idp, allowlist
    ):
        """AuthN ≠ AuthZ: the same legitimately-issued token, held by a
        subject the allowlist does not admit, gets an *authenticated-shaped*
        denial — tools are filtered to none, never a 401 (the transport
        recognized the token; the allowlist denied the caller)."""
        client_id, code, verifier = walk_full_flow(client, mock_idp)
        tokens = exchange_code(client, client_id, code, verifier).json()

        allowlist("someone-else@example.com")
        session_id = open_mcp_session(client, tokens["access_token"])
        resp = mcp_request(
            client, tokens["access_token"], "tools/list", session_id=session_id
        )
        assert resp.status_code == 200, resp.text[:300]
        assert mcp_json(resp)["result"]["tools"] == [], (
            "non-allowlisted subject must see no tools"
        )

    @pytest.mark.parametrize(
        "variant",
        ["unknown_jti", "wrong_audience", "wrong_issuer", "expired",
         "wrong_signature", "refresh_as_access"],
    )
    def test_wrong_tokens_401_with_invalid_token(
        self, client, bearer_variants, variant
    ):
        """Well-formed but wrong tokens are all transport-401s carrying the
        ``invalid_token`` challenge — issuer, audience binding (RFC 8707),
        signature, expiry and access/refresh separation are each enforced
        on the live MCP transport, never silently admitted."""
        resp = mcp_request(client, bearer_variants[variant], "tools/list")
        assert resp.status_code == 401, (
            f"{variant}: expected 401, got {resp.status_code} {resp.text[:200]}"
        )
        challenge = resp.headers["www-authenticate"]
        assert challenge.startswith("Bearer")
        assert "invalid_token" in challenge
        assert "resource_metadata" in challenge


# ===========================================================================
# 401 vs 403 — the documented split
# ===========================================================================


class TestUnauthorizedVsForbidden:
    """docs/research/mcp-oauth-authentication.md §5: "401 — missing/invalid/
    expired token; 403 — valid token but insufficient scope/permission."

    The two layers must never blur: transport auth answers identity
    failures with 401 + challenge; the subject allowlist answers
    authorization failures with 403 (HTTP surface) / denial (MCP surface)
    for a caller whose token already validated."""

    def test_missing_token_is_401_not_403(self, client):
        resp = mcp_request(client, None, "tools/list")
        assert resp.status_code == 401
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    def test_valid_token_non_allowlisted_admin_egress_is_403(
        self, client, mock_idp
    ):
        """/admin/egress with a *valid* token: the caller is authenticated,
        so failure is authorization — 403 with the allowlist message, never
        a 401 and never a WWW-Authenticate re-challenge."""
        from ytt.authz import subject_allowed

        client_id, code, verifier = walk_full_flow(client, mock_idp)
        tokens = exchange_code(client, client_id, code, verifier).json()

        # This route captures Settings at app-build time, so the guarantee
        # comes from the unit environment itself: the allowlist admits
        # nobody (fail-closed), making this validly-authenticated subject
        # exactly the 403 case. Asserted before the request so a deviant
        # environment fails here instead of reaching the network probe.
        assert not subject_allowed(SUBJECT, get_settings().allowed_subjects_set)

        resp = client.get(
            f"{BASE}{_issuer_path()}/admin/egress",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert resp.status_code == 403, resp.text[:300]
        assert "allowlist" in resp.json()["error"]
        assert "www-authenticate" not in {k.lower() for k in resp.headers}

    def test_admin_egress_without_token_is_401_not_403(self, client):
        """Same route, no token — identity failure, so 401 + challenge (a
        403 would leak that the route exists and is merely 'denied')."""
        resp = client.get(f"{BASE}{_issuer_path()}/admin/egress")
        assert resp.status_code == 401
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    def test_allowlisted_subject_passes_both_layers(
        self, client, mock_idp, allowlist
    ):
        """End state: issued token + allowlisted subject reaches the tools —
        the two-layer model composing correctly on one request."""
        client_id, code, verifier = walk_full_flow(client, mock_idp)
        tokens = exchange_code(client, client_id, code, verifier).json()

        allowlist(SUBJECT)
        session_id = open_mcp_session(client, tokens["access_token"])
        resp = mcp_request(
            client, tokens["access_token"], "tools/list", session_id=session_id
        )
        assert resp.status_code == 200
        assert mcp_json(resp)["result"]["tools"]


# ===========================================================================
# Refresh rotation (the offline_access chain documented in ytt/auth.py)
# ===========================================================================


class TestRefreshRotation:
    """The issued refresh token redeems silently at /token for a fresh
    access token that validates on the MCP transport — the mechanism that
    keeps Claude from re-driving the full browser flow weekly."""

    def test_refresh_grant_rotates_to_a_working_access_token(
        self, client, mock_idp, allowlist
    ):
        client_id, code, verifier = walk_full_flow(client, mock_idp)
        tokens = exchange_code(client, client_id, code, verifier).json()
        refresh_token = tokens["refresh_token"]
        assert refresh_token

        resp = redeem_refresh_token(client, client_id, refresh_token)
        assert resp.status_code == 200, resp.text[:300]
        rotated = resp.json()
        assert rotated["access_token"]
        assert rotated["access_token"] != tokens["access_token"]

        # The rotated token really authenticates (allowlisted subject).
        allowlist(SUBJECT)
        session_id = open_mcp_session(client, rotated["access_token"])
        listed = mcp_request(
            client, rotated["access_token"], "tools/list", session_id=session_id
        )
        assert listed.status_code == 200, listed.text[:300]
        assert mcp_json(listed)["result"]["tools"]

        # The old refresh token was rotated away — reuse is invalid_grant
        # at 401 (the MCP-spec transform; see the PKCE-verifier test).
        replay = redeem_refresh_token(client, client_id, refresh_token)
        assert replay.status_code == 401, replay.text[:300]
        assert replay.json()["error"] == "invalid_grant"
