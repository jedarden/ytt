"""Regression tests for unauthenticated OAuth endpoint abuse bounds."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.stores.filetree import FileTreeStore
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import AnyUrl, OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.requests import Request

from ytt.auth import CLAUDE_REDIRECT_URIS, UpstreamIdTokenVerifier, YttOIDCProvider
from ytt.oauth_abuse import (
    OAUTH_CLIENT_COLLECTION,
    REGISTRATION_INDEX_COLLECTION,
    AuthorizationThrottle,
    RegistrationThrottle,
    TokenThrottle,
    enumerate_client_ids,
    registration_source_address,
)


def _request(peer: str, x_forwarded_for: str | None = None) -> Request:
    headers = []
    if x_forwarded_for is not None:
        headers.append((b"x-forwarded-for", x_forwarded_for.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/register",
        "raw_path": b"/register",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 443),
        "server": ("example.test", 443),
    }
    return Request(scope)


def _provider(
    store: FileTreeStore,
    *,
    cap: int = 256,
    ttl: int = 30,
    rate: int = 10,
    authorize_client_rate: int = 30,
    authorize_ip_rate: int = 120,
    token_client_rate: int = 30,
    token_ip_rate: int = 120,
    now=lambda: 1_000.0,
    throttle_clock=None,
) -> YttOIDCProvider:
    provider = YttOIDCProvider(
        config_url="https://idp.example.test/.well-known/openid-configuration",
        client_id="ytt-client",
        client_secret="unit-test-secret",
        base_url="https://mcp.example.test/ytt",
        resource_base_url="https://mcp.example.test",
        issuer_url="https://mcp.example.test/ytt",
        allowed_client_redirect_uris=CLAUDE_REDIRECT_URIS,
        token_verifier=UpstreamIdTokenVerifier(
            public_key="unit-test-secret",
            algorithm="HS256",
            issuer="https://idp.example.test/",
            audience="ytt-client",
        ),
        verify_id_token=True,
        client_storage=store,
        registration_rate_per_min=rate,
        authorize_rate_per_client_per_min=authorize_client_rate,
        authorize_rate_per_ip_per_min=authorize_ip_rate,
        token_rate_per_client_per_min=token_client_rate,
        token_rate_per_ip_per_min=token_ip_rate,
        trusted_proxy_count=2,
        registration_cap=cap,
        registration_ttl_sec=ttl,
        registration_clock=now,
        throttle_clock=throttle_clock or time.monotonic,
    )
    # Mirror build_auth_provider's scope wiring so the full grant walk below
    # is admitted exactly as the production provider would admit it.
    provider.required_scopes = ["openid", "email", "offline_access"]
    provider.update_default_scopes(["openid", "email", "offline_access"])
    return provider


def _client(client_id: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(CLAUDE_REDIRECT_URIS[0])],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def test_registration_throttle_enforces_sliding_window_and_isolates_sources():
    now = [100.0]
    throttle = RegistrationThrottle(limit=2, window_seconds=60, clock=lambda: now[0])

    assert throttle.retry_after("192.0.2.1") is None
    assert throttle.retry_after("192.0.2.1") is None
    assert throttle.retry_after("192.0.2.1") == 60
    assert throttle.retry_after("192.0.2.2") is None

    now[0] = 159.9
    assert throttle.retry_after("192.0.2.1") == 1
    now[0] = 160.0
    assert throttle.retry_after("192.0.2.1") is None


def test_registration_source_uses_configured_trusted_hop_or_peer_fallback():
    request = _request(
        "10.0.0.5",
        "198.51.100.99, 203.0.113.8, 10.0.0.4",
    )
    trusted = {
        "trusted_proxy_count": 2,
        "trusted_proxy_cidrs": "10.0.0.0/8",
    }
    assert registration_source_address(request, **trusted) == "203.0.113.8"
    assert (
        registration_source_address(_request("10.0.0.5", "198.51.100.99"), **trusted)
        == "10.0.0.5"
    )
    assert (
        registration_source_address(
            request, trusted_proxy_count=0, trusted_proxy_cidrs="10.0.0.0/8"
        )
        == "10.0.0.5"
    )
    assert (
        registration_source_address(
            _request("10.0.0.5", "198.51.100.99, not-an-ip"), **trusted
        )
        == "10.0.0.5"
    )
    assert (
        registration_source_address(
            _request("203.0.113.40", "198.51.100.99, 203.0.113.8"), **trusted
        )
        == "203.0.113.40"
    )


def test_trusted_proxy_cidr_configuration_rejects_malformed_networks():
    from pydantic import ValidationError

    from ytt.config import Settings

    with pytest.raises(ValidationError, match="TRUSTED_PROXY_CIDRS"):
        Settings(
            public_url="https://mcp.example.test/ytt",
            oauth_trusted_proxy_cidrs="10.0.0.0/8,broken",
        )


def test_register_route_returns_429_at_threshold_across_both_paths(tmp_path: Path):
    provider = _provider(FileTreeStore(data_directory=tmp_path), rate=1)
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))

    with TestClient(app) as client:
        client.options(
            "/register",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
            },
        )
        first = client.post("/register", json={})
        second = client.post(
            "/ytt/register", json={}, headers={"Origin": "https://claude.ai"}
        )

    assert first.status_code != 429
    assert second.status_code == 429
    assert second.json()["error"] == "rate_limited"
    assert int(second.headers["Retry-After"]) >= 1


def test_corrupt_persisted_index_fails_closed(tmp_path):
    class CorruptIndexStore(FileTreeStore):
        async def get(self, key, *, collection=None):
            if collection == REGISTRATION_INDEX_COLLECTION:
                return {"client": float("nan")}
            return await super().get(key, collection=collection)

    provider = _provider(CorruptIndexStore(data_directory=tmp_path), cap=1)
    with pytest.raises(RegistrationError) as error:
        asyncio.run(provider.register_client(_client("must-not-be-stored")))

    assert error.value.error == "temporarily_unavailable"
    assert asyncio.run(provider._client_store.get(key="must-not-be-stored")) is None


def test_client_store_writes_without_register_cannot_bypass_cap(tmp_path):
    provider = _provider(FileTreeStore(data_directory=tmp_path), cap=1)

    def record(client_id):
        return ProxyDCRClient(
            client_id=client_id,
            client_secret=None,
            redirect_uris=[AnyUrl(CLAUDE_REDIRECT_URIS[0])],
            grant_types=["authorization_code", "refresh_token"],
            scope="openid email",
            token_endpoint_auth_method="none",
            allowed_redirect_uri_patterns=list(CLAUDE_REDIRECT_URIS),
        )

    asyncio.run(
        provider._client_store.put(
            key="metadata-client", value=record("metadata-client")
        )
    )
    with pytest.raises(RegistrationError):
        asyncio.run(
            provider._client_store.put(
                key="second-client", value=record("second-client")
            )
        )
    assert asyncio.run(provider._client_store.get(key="metadata-client")) is not None
    assert asyncio.run(provider._client_store.get(key="second-client")) is None


def test_registration_cap_rejects_without_evicting_and_survives_restart(tmp_path):
    store_path = tmp_path / "oauth"
    provider = _provider(FileTreeStore(data_directory=store_path), cap=1)
    asyncio.run(provider.register_client(_client("first-client")))

    restarted = _provider(FileTreeStore(data_directory=store_path), cap=1)
    with pytest.raises(RegistrationError) as error:
        asyncio.run(restarted.register_client(_client("second-client")))
    assert error.value.error == "invalid_client_metadata"
    assert asyncio.run(restarted._client_store.get(key="first-client")) is not None
    assert asyncio.run(restarted._client_store.get(key="second-client")) is None


def test_expired_rows_are_deleted_before_capacity_check(tmp_path):
    now = [1_000.0]
    provider = _provider(
        FileTreeStore(data_directory=tmp_path), cap=1, ttl=30, now=lambda: now[0]
    )
    asyncio.run(provider.register_client(_client("expires-first")))
    assert asyncio.run(provider._client_store.get(key="expires-first")) is not None

    now[0] = 1_031.0
    asyncio.run(provider.register_client(_client("after-expiry")))
    assert asyncio.run(provider._client_store.get(key="expires-first")) is None
    assert asyncio.run(provider._client_store.get(key="after-expiry")) is not None
    assert "expires-first" not in asyncio.run(
        enumerate_client_ids(provider._client_storage)
    )


def test_legacy_client_rows_are_counted_and_given_a_finite_ttl(tmp_path):
    store = FileTreeStore(data_directory=tmp_path)
    legacy_store = PydanticAdapter[ProxyDCRClient](
        key_value=store,
        pydantic_model=ProxyDCRClient,
        default_collection=OAUTH_CLIENT_COLLECTION,
        raise_on_validation_error=True,
    )
    legacy = ProxyDCRClient(
        client_id="legacy-client",
        client_secret=None,
        redirect_uris=[AnyUrl(CLAUDE_REDIRECT_URIS[0])],
        grant_types=["authorization_code", "refresh_token"],
        scope="openid email",
        token_endpoint_auth_method="none",
        allowed_redirect_uri_patterns=list(CLAUDE_REDIRECT_URIS),
    )
    asyncio.run(legacy_store.put(key="legacy-client", value=legacy))

    provider = _provider(store, cap=1, ttl=30)
    with pytest.raises(RegistrationError):
        asyncio.run(provider.register_client(_client("new-client")))

    stored, ttl = asyncio.run(provider._client_store.ttl(key="legacy-client"))
    assert stored is not None
    assert ttl is not None and 0 < ttl <= 30


def test_concurrent_registrations_cannot_cross_the_persisted_cap(tmp_path):
    provider = _provider(FileTreeStore(data_directory=tmp_path), cap=3)

    async def register_all():
        return await asyncio.gather(
            *(
                provider.register_client(_client(f"client-{index}"))
                for index in range(8)
            ),
            return_exceptions=True,
        )

    results = asyncio.run(register_all())
    assert sum(result is None for result in results) == 3
    assert sum(isinstance(result, RegistrationError) for result in results) == 5
    keys = asyncio.run(enumerate_client_ids(provider._client_storage))
    assert len(keys) == 3


# ---------------------------------------------------------------------------
# /authorize initiation throttle
# ---------------------------------------------------------------------------

CODE_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(b"unit-test-verifier").digest())
    .rstrip(b"=")
    .decode()
)


def _authorize_params(client_id: str) -> dict[str, str]:
    return {
        "client_id": client_id,
        "redirect_uri": CLAUDE_REDIRECT_URIS[0],
        "response_type": "code",
        "code_challenge": CODE_CHALLENGE,
        "code_challenge_method": "S256",
        "state": "st",
    }


async def _stored_keys(provider: YttOIDCProvider, collection: str) -> list[str]:
    return await enumerate_client_ids(provider._client_storage, collection=collection)


def _with_trusted_peer(app):
    """Pin the socket peer inside the default trusted proxy CIDR.

    Starlette's TestClient reports the peer as the non-IP hostname
    ``testclient``, which the source rule (correctly) refuses to trust for
    X-Forwarded-For. Pinning the peer to the trusted pod network lets tests
    key sources by their forwarded visitor hop exactly as the ingress does.
    """

    async def shim(scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = ("10.42.0.5", 4321)
        await app(scope, receive, send)

    return shim


def test_authorization_throttle_window_keying_and_reset():
    now = [100.0]
    throttle = AuthorizationThrottle(
        per_client_limit=2,
        per_ip_limit=3,
        window_seconds=60,
        clock=lambda: now[0],
    )

    # Allowed up to the per-(source, client) limit.
    assert throttle.retry_after("192.0.2.1", "client-a") is None
    assert throttle.retry_after("192.0.2.1", "client-a") is None
    assert throttle.retry_after("192.0.2.1", "client-a") == 60

    # Client keying: a different client from the same IP has its own budget.
    assert throttle.retry_after("192.0.2.1", "client-b") is None
    # IP keying: the same client from a different IP has its own budget.
    assert throttle.retry_after("192.0.2.2", "client-a") is None

    # Per-IP ceiling: the IP has had 3 admitted (2x client-a, 1x client-b),
    # so even a brand-new client is denied at the IP bound.
    assert throttle.retry_after("192.0.2.1", "client-c") == 60

    # Denied attempts do not extend the window: the reported wait shrinks.
    now[0] = 159.5
    assert throttle.retry_after("192.0.2.1", "client-a") == 1

    # Window reset: past the first admitted attempt the budget returns.
    now[0] = 160.0
    assert throttle.retry_after("192.0.2.1", "client-a") is None


def test_authorization_throttle_zero_limit_denies_all():
    throttle = AuthorizationThrottle(
        per_client_limit=0, per_ip_limit=10, window_seconds=60
    )
    assert throttle.retry_after("192.0.2.1", "client-a") == 60


def test_authorization_throttle_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        AuthorizationThrottle(per_client_limit=-1, per_ip_limit=10)


def test_authorize_route_429_writes_no_state_and_issues_no_code(tmp_path: Path):
    provider = _provider(
        FileTreeStore(data_directory=tmp_path), authorize_client_rate=1
    )
    asyncio.run(provider.register_client(_client("authz-client")))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))

    with TestClient(app) as client:
        allowed = client.get(
            "/authorize",
            params=_authorize_params("authz-client"),
            follow_redirects=False,
        )
        throttled = client.get("/authorize", params=_authorize_params("authz-client"))

    # The allowed initiation reached the handler: it redirected (302) and
    # wrote exactly one OAuth transaction.
    assert allowed.status_code == 302
    transactions_after_allowed = asyncio.run(
        _stored_keys(provider, "mcp-oauth-transactions")
    )
    assert len(transactions_after_allowed) == 1

    # The throttled initiation never reached the handler: direct 429 with an
    # OAuth-shaped error body and Retry-After, no new transaction, no code.
    assert throttled.status_code == 429
    assert throttled.json()["error"] == "rate_limited"
    assert int(throttled.headers["Retry-After"]) >= 1
    assert (
        asyncio.run(_stored_keys(provider, "mcp-oauth-transactions"))
        == transactions_after_allowed
    )
    assert asyncio.run(_stored_keys(provider, "mcp-authorization-codes")) == []


def test_authorize_route_keys_by_client_ip_and_shared_across_paths(tmp_path: Path):
    provider = _provider(
        FileTreeStore(data_directory=tmp_path),
        authorize_client_rate=1,
        authorize_ip_rate=100,
    )
    asyncio.run(provider.register_client(_client("authz-client")))
    asyncio.run(provider.register_client(_client("authz-client-2")))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))

    def source_headers(visitor_ip: str) -> dict[str, str]:
        # _provider uses trusted_proxy_count=2: the source key is the second
        # address from the right of X-Forwarded-For, i.e. the visitor as the
        # ingress chain records it, with one trusted hop appended after it.
        return {"X-Forwarded-For": f"{visitor_ip}, 10.42.0.6"}

    with TestClient(_with_trusted_peer(app)) as client:
        first = client.get(
            "/authorize",
            params=_authorize_params("authz-client"),
            headers=source_headers("198.51.100.10"),
            follow_redirects=False,
        )
        # Same client, different source IP: separate per-client budget.
        other_ip = client.get(
            "/authorize",
            params=_authorize_params("authz-client"),
            headers=source_headers("198.51.100.11"),
            follow_redirects=False,
        )
        # Different client, same source IP: separate per-client budget.
        other_client = client.get(
            "/authorize",
            params=_authorize_params("authz-client-2"),
            headers=source_headers("198.51.100.10"),
            follow_redirects=False,
        )
        # Same client, same source IP, other path spelling: the budget is
        # shared between bare and path-inserted routes.
        same_key_other_path = client.get(
            "/ytt/authorize",
            params=_authorize_params("authz-client"),
            headers=source_headers("198.51.100.10"),
        )

    assert first.status_code == 302
    assert other_ip.status_code == 302
    assert other_client.status_code == 302
    assert same_key_other_path.status_code == 429
    assert same_key_other_path.json()["error"] == "rate_limited"


def test_authorize_route_per_ip_ceiling_binds_client_rotation(tmp_path: Path):
    provider = _provider(
        FileTreeStore(data_directory=tmp_path),
        authorize_client_rate=10,
        authorize_ip_rate=1,
    )
    for name in ("authz-client", "authz-client-2"):
        asyncio.run(provider.register_client(_client(name)))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))
    headers = {"X-Forwarded-For": "198.51.100.10, 10.42.0.6"}

    with TestClient(_with_trusted_peer(app)) as client:
        first = client.get(
            "/authorize",
            params=_authorize_params("authz-client"),
            headers=headers,
            follow_redirects=False,
        )
        rotated = client.get(
            "/authorize", params=_authorize_params("authz-client-2"), headers=headers
        )

    assert first.status_code == 302
    # A fresh client_id does not escape the per-source-IP ceiling.
    assert rotated.status_code == 429
    assert rotated.json()["error"] == "rate_limited"


def test_authorize_route_reads_client_id_from_post_form_body(tmp_path: Path):
    provider = _provider(
        FileTreeStore(data_directory=tmp_path), authorize_client_rate=1
    )
    asyncio.run(provider.register_client(_client("authz-client")))
    asyncio.run(provider.register_client(_client("authz-client-2")))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))
    headers = {"X-Forwarded-For": "198.51.100.10, 10.42.0.6"}

    with TestClient(_with_trusted_peer(app)) as client:
        first = client.post(
            "/authorize",
            data=_authorize_params("authz-client"),
            headers=headers,
            follow_redirects=False,
        )
        second = client.post(
            "/authorize", data=_authorize_params("authz-client"), headers=headers
        )
        other_client = client.post(
            "/authorize",
            data=_authorize_params("authz-client-2"),
            headers=headers,
            follow_redirects=False,
        )

    # The body client_id keys the budget, and the replayed body still
    # reaches the handler (302, not a body-starved 400).
    assert first.status_code == 302
    assert second.status_code == 429
    assert other_client.status_code == 302


# ---------------------------------------------------------------------------
# /token redemption throttle
# ---------------------------------------------------------------------------

TOKEN_BASE = "https://mcp.example.test"


def _token_form(client_id: str, **overrides: str) -> dict[str, str]:
    form = {
        "grant_type": "authorization_code",
        "code": "never-issued-code",
        "redirect_uri": CLAUDE_REDIRECT_URIS[0],
        "code_verifier": "unit-test-verifier",
        "client_id": client_id,
    }
    form.update(overrides)
    return form


def test_token_throttle_window_keying_and_reset():
    now = [100.0]
    throttle = TokenThrottle(
        per_client_limit=2,
        per_ip_limit=3,
        window_seconds=60,
        clock=lambda: now[0],
    )

    assert throttle.retry_after("192.0.2.1", "client-a") is None
    assert throttle.retry_after("192.0.2.1", "client-a") is None
    assert throttle.retry_after("192.0.2.1", "client-a") == 60

    # Client keying: another client from the same IP has its own budget.
    assert throttle.retry_after("192.0.2.1", "client-b") is None
    # IP keying: the same client from another IP has its own budget.
    assert throttle.retry_after("192.0.2.2", "client-a") is None

    # Per-IP ceiling: 3 admitted from 192.0.2.1 — a brand-new client is
    # denied at the IP bound, so client_id rotation cannot escape it.
    assert throttle.retry_after("192.0.2.1", "client-c") == 60

    # Denied attempts do not extend the window: the reported wait shrinks.
    now[0] = 159.5
    assert throttle.retry_after("192.0.2.1", "client-a") == 1

    # Window reset: past the first admitted attempt the budget returns.
    now[0] = 160.0
    assert throttle.retry_after("192.0.2.1", "client-a") is None


def test_token_throttle_zero_limit_denies_all():
    throttle = TokenThrottle(per_client_limit=0, per_ip_limit=10, window_seconds=60)
    assert throttle.retry_after("192.0.2.1", "client-a") == 60


def test_token_throttle_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        TokenThrottle(per_client_limit=10, per_ip_limit=-1)


class _MockUpstreamIdP:
    """Authentik stand-in for the isolated provider's upstream exchange.

    Answers the OAuthProxy's outbound authorization_code call with a token
    set whose ``id_token`` is a real HS256 JWT matching the isolated
    provider's verifier (issuer/audience/secret from ``_provider``), and
    records every request so tests can prove a throttled attempt never
    reached the exchange at all.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        from joserfc import jwk, jwt

        self.requests.append(request)
        now = int(time.time())
        id_token = jwt.encode(
            {"alg": "HS256"},
            {
                "iss": "https://idp.example.test/",
                "aud": "ytt-client",
                "sub": "transcript-fan@example.com",
                "email": "transcript-fan@example.com",
                "exp": now + 3600,
                "iat": now,
            },
            jwk.import_key("unit-test-secret", "oct"),
        )
        return httpx.Response(
            200,
            json={
                "access_token": "upstream-access-token",
                "id_token": id_token,
                "refresh_token": "upstream-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid email offline_access",
            },
        )

    def patch(self):
        """Patch the proxy's authlib client class onto the mock transport."""
        from authlib.integrations.httpx_client import AsyncOAuth2Client
        from fastmcp.server.auth.oauth_proxy import proxy as oauth_proxy_module

        idp = self

        class _MockTransportClient(AsyncOAuth2Client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(idp.handle)
                super().__init__(*args, **kwargs)

        return patch.object(
            oauth_proxy_module, "AsyncOAuth2Client", _MockTransportClient
        )


def _query_params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _walk_grant(client: TestClient, client_id: str) -> tuple[str, str]:
    """Drive authorize → consent → IdP callback for ``client_id``.

    Returns ``(client-facing code, PKCE verifier)`` — what a real MCP client
    holds when it redeems the code at POST /token.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    r_authorize = client.get(
        f"{TOKEN_BASE}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_REDIRECT_URIS[0],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "token-throttle-state",
            "scope": "openid email offline_access",
        },
    )
    assert r_authorize.status_code == 302, r_authorize.text[:300]
    consent_url = r_authorize.headers["location"]
    assert consent_url.startswith(f"{TOKEN_BASE}/ytt/consent"), consent_url

    r_consent = client.get(consent_url)
    assert r_consent.status_code == 200, r_consent.text[:300]
    match = re.search(r'name="csrf_token" value="([^"]+)"', r_consent.text)
    assert match, "consent page carries no csrf_token form field"
    r_approval = client.post(
        f"{TOKEN_BASE}/ytt/consent",
        data={
            "txn_id": _query_params(consent_url)["txn_id"],
            "csrf_token": match.group(1),
            "action": "approve",
        },
    )
    assert r_approval.status_code == 302, r_approval.text[:300]

    r_callback = client.get(
        f"{TOKEN_BASE}/auth/callback",
        params={
            "code": "mock-idp-code",
            "state": _query_params(r_approval.headers["location"])["state"],
        },
    )
    assert r_callback.status_code == 302, r_callback.text[:300]
    assert r_callback.headers["location"].startswith(CLAUDE_REDIRECT_URIS[0])
    code = _query_params(r_callback.headers["location"])["code"]
    assert code
    return code, verifier


def test_token_route_allows_valid_exchange_then_rejects_without_issuing(
    tmp_path: Path,
):
    """The complete grant still succeeds under the throttle, and the
    request past the ceiling is answered before the handler — no token in
    the body, no upstream exchange, OAuth-shaped 429 with Retry-After."""
    provider = _provider(
        FileTreeStore(data_directory=tmp_path),
        token_client_rate=2,
        token_ip_rate=100,
    )
    asyncio.run(provider.register_client(_client("tok-client")))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))
    idp = _MockUpstreamIdP()

    with idp.patch(), TestClient(app, follow_redirects=False) as client:
        code, verifier = _walk_grant(client, "tok-client")
        assert len(idp.requests) == 1, "the grant walk exchanged upstream once"

        # Allowed exchange #1: the real code redeems for actual tokens —
        # and the replayed form body reached the handler intact.
        allowed = client.post(
            f"{TOKEN_BASE}/token",
            data=_token_form("tok-client", code=code, code_verifier=verifier),
        )
        assert allowed.status_code == 200, allowed.text[:300]
        assert allowed.json()["access_token"]
        assert allowed.json()["refresh_token"]
        # The redemption minted FastMCP tokens without another upstream call.
        upstream_after_allowed = len(idp.requests)

        # Allowed attempt #2 (fabricated code): the handler answers
        # invalid_grant — the throttle admitted it, and it counts.
        second = client.post(f"{TOKEN_BASE}/token", data=_token_form("tok-client"))
        assert second.status_code == 401
        assert second.json()["error"] == "invalid_grant"

        # Attempt #3 exceeds the per-(IP, client) ceiling: direct 429 before
        # the handler, with an OAuth-shaped body and no token fields.
        throttled = client.post(f"{TOKEN_BASE}/token", data=_token_form("tok-client"))
        assert throttled.status_code == 429
        body = throttled.json()
        assert body["error"] == "rate_limited"
        assert body["error_description"]
        assert int(throttled.headers["Retry-After"]) >= 1
        assert throttled.headers["cache-control"] == "no-store"
        assert "access_token" not in body
        assert "refresh_token" not in body
        # No token was issued: the exchange never ran upstream.
        assert len(idp.requests) == upstream_after_allowed

        # Both route spellings share one budget; CORS preflights stay free.
        other_path = client.post(
            f"{TOKEN_BASE}/ytt/token", data=_token_form("tok-client")
        )
        assert other_path.status_code == 429
        assert other_path.json()["error"] == "rate_limited"
        preflight = client.options(f"{TOKEN_BASE}/token")
        assert preflight.status_code != 429


def test_token_route_keys_by_client_and_source_ip(tmp_path: Path):
    provider = _provider(
        FileTreeStore(data_directory=tmp_path),
        token_client_rate=1,
        token_ip_rate=100,
    )
    for name in ("tok-client", "tok-client-2"):
        asyncio.run(provider.register_client(_client(name)))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))

    def source_headers(visitor_ip: str) -> dict[str, str]:
        # _provider uses trusted_proxy_count=2: the source key is the second
        # address from the right of X-Forwarded-For.
        return {"X-Forwarded-For": f"{visitor_ip}, 10.42.0.6"}

    with TestClient(_with_trusted_peer(app)) as client:
        # Same (IP, client): admitted once...
        first = client.post(
            f"{TOKEN_BASE}/token",
            data=_token_form("tok-client"),
            headers=source_headers("198.51.100.10"),
        )
        # ...then denied.
        repeat = client.post(
            f"{TOKEN_BASE}/token",
            data=_token_form("tok-client"),
            headers=source_headers("198.51.100.10"),
        )
        # Different source IP, same client: its own per-client budget.
        other_ip = client.post(
            f"{TOKEN_BASE}/token",
            data=_token_form("tok-client"),
            headers=source_headers("198.51.100.11"),
        )
        # Different client, same source IP: its own per-client budget.
        other_client = client.post(
            f"{TOKEN_BASE}/token",
            data=_token_form("tok-client-2"),
            headers=source_headers("198.51.100.10"),
        )

    # Admitted attempts reached the handler (its invalid_grant, not the
    # throttle's rate_limited); only the repeated key was denied.
    assert first.status_code == 401
    assert first.json()["error"] == "invalid_grant"
    assert repeat.status_code == 429
    assert repeat.json()["error"] == "rate_limited"
    assert other_ip.status_code == 401
    assert other_client.status_code == 401


def test_token_route_per_ip_ceiling_binds_client_rotation(tmp_path: Path):
    provider = _provider(
        FileTreeStore(data_directory=tmp_path),
        token_client_rate=100,
        token_ip_rate=1,
    )
    for name in ("tok-client", "tok-client-2"):
        asyncio.run(provider.register_client(_client(name)))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))
    headers = {"X-Forwarded-For": "198.51.100.10, 10.42.0.6"}

    with TestClient(_with_trusted_peer(app)) as client:
        first = client.post(
            f"{TOKEN_BASE}/token", data=_token_form("tok-client"), headers=headers
        )
        rotated = client.post(
            f"{TOKEN_BASE}/token", data=_token_form("tok-client-2"), headers=headers
        )

    assert first.status_code == 401
    # A fresh client_id does not escape the per-source-IP ceiling.
    assert rotated.status_code == 429
    assert rotated.json()["error"] == "rate_limited"


def test_token_route_window_resets(tmp_path: Path):
    """The rolling window admits again once the recorded attempt ages out —
    a denial is not a blacklist."""
    clock = [1_000.0]
    provider = _provider(
        FileTreeStore(data_directory=tmp_path),
        token_client_rate=1,
        token_ip_rate=100,
        throttle_clock=lambda: clock[0],
    )
    asyncio.run(provider.register_client(_client("tok-client")))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))

    with TestClient(app) as client:
        first = client.post(f"{TOKEN_BASE}/token", data=_token_form("tok-client"))
        denied = client.post(f"{TOKEN_BASE}/token", data=_token_form("tok-client"))
        clock[0] += 60.0  # the strict window has fully rolled
        reset = client.post(f"{TOKEN_BASE}/token", data=_token_form("tok-client"))

    assert first.status_code == 401
    assert denied.status_code == 429
    assert reset.status_code == 401, "the window did not reset after 60s"


def test_token_route_reads_client_id_from_basic_auth(tmp_path: Path):
    """RFC 6749 §2.3.1 Basic-authenticated requests (client_id as the
    userinfo, no body client_id) key under the claimed client."""
    provider = _provider(
        FileTreeStore(data_directory=tmp_path),
        token_client_rate=1,
        token_ip_rate=100,
    )
    asyncio.run(provider.register_client(_client("tok-client")))
    app = Starlette(routes=provider.get_routes(mcp_path="/ytt"))

    def basic_form() -> dict[str, str]:
        form = _token_form("tok-client")
        del form["client_id"]
        return form

    def basic_headers() -> dict[str, str]:
        credentials = base64.b64encode(b"tok-client:guessedin-secret").decode()
        return {"Authorization": f"Basic {credentials}"}

    with TestClient(app) as client:
        first = client.post(
            f"{TOKEN_BASE}/token", data=basic_form(), headers=basic_headers()
        )
        second = client.post(
            f"{TOKEN_BASE}/token", data=basic_form(), headers=basic_headers()
        )

    assert first.status_code == 401
    assert first.json()["error"] == "invalid_client"
    assert second.status_code == 429
    assert second.json()["error"] == "rate_limited"
