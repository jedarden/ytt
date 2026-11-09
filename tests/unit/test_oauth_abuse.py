"""Regression tests for unauthenticated OAuth dynamic registration bounds."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    RegistrationThrottle,
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
    now=lambda: 1_000.0,
) -> YttOIDCProvider:
    return YttOIDCProvider(
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
        trusted_proxy_count=2,
        registration_cap=cap,
        registration_ttl_sec=ttl,
        registration_clock=now,
    )


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
