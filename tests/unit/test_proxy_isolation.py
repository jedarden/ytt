"""Proxy isolation for identity + ASR traffic (``docs/notes/proxy-egress.md``).

:mod:`tests.unit.test_proxy` pins the positive half of the ``YTT_PROXY_URL``
contract — YouTube caption extraction and the Whisper audio download dial
**direct first** and retry **once through the proxy** on ``ip_blocked``. This
module pins the negative half, which until now existed only as a table row in
the contract doc:

- OIDC discovery (``GET config_url``) — **never proxied**
- JWKS (key fetch for token validation) — **never proxied**
- token exchange / refresh against the upstream IdP — **never proxied**
- the Whisper transcription POST (``/v1/audio/transcriptions``) — **never
  proxied** — with every failure path (HTTP error status, connect failure,
  read timeout) surfacing ``asr_failed`` without a proxied retry

The instrumentation is the HTTP-client construction boundary itself: every
``httpx.AsyncClient`` built while a test runs — the production ASR client
that ``run_whisper_job`` constructs internally (no test client injected),
authlib's upstream OAuth client, fastmcp's discovery fetch — is recorded and
asserted to carry **no ``proxy`` kwarg** and to never mention the configured
sentinel ``YTT_PROXY_URL`` anywhere in its construction kwargs. The proxy
value is *configured* in every test here, so a regression that threads it
into any of these clients fails here instead of silently proxying identity
and tailnet traffic through a third party.

``YTT_PROXY_URL`` is not one of the standard proxy environment variables
httpx's ``trust_env`` reads (``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY``), so
the only way it can reach these clients is through ytt's own code. The static
leg below closes the remaining vector the contract names — "never via process
environment (``HTTP_PROXY`` etc.)": the package must not (re)export the proxy
into the process environment, where trust_env would sweep the ASR POST and
OAuth traffic into the proxy wholesale.

JWKS is pinned at its strongest available level: ytt's verifier is configured
symmetric-local (the reference Authentik signs HS256 with the client secret —
see ``ytt/auth.py``), so token validation performs **no HTTP at all**, and a
JWKS fetch that could be proxied cannot exist. Failure paths (expired /
malformed token) are pinned to stay equally network-free.

All tests are offline: no network, no live IdP, no live Whisper service.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ytt.whisper import WhisperJobRegistry, check_model_guard, run_whisper_job

#: The configured sentinel proxy — a *valid* ``YTT_PROXY_URL`` (passes the
#: Settings validator) with credentials, so a leak of the userinfo into any
#: construction kwarg, URL, or error is also caught.
_PROXY = "http://alice:s3cret@proxy-isolation.test:3128"

#: The sentinel ASR (Whisper) base URL — deliberately neither YouTube nor any
#: real service, so a hardcoded ASR endpoint cannot masquerade as the
#: configured one.
_WHISPER_URL = "http://asr-isolation.test:9000"

VIDEO_ID = "dQw4w9WgXcQ"

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "ytt"

# Reuse the end-to-end OAuth harness: the ASGI client fixture (real app,
# lifespan run) and the flow-walking helpers. The upstream-IdP stand-in is
# NOT reused — this module needs a programmable one (failure status) that
# also records every upstream dial.
from tests.unit.test_oauth_end_to_end import (  # noqa: E402
    BASE,
    CLAUDE_REDIRECT,
    client,  # noqa: F401  -- re-exported as a fixture below
    _form_fields,
    _issuer_path,
    _pkce_pair,
    _query_params,
    exchange_code,
    redeem_refresh_token,
    register_client,
    walk_full_flow,
)


@pytest.fixture
def asgi_client(request):
    """The end-to-end suite's ASGI client, re-exported under the name this
    module's tests use for it (a real app per test, lifespan run)."""
    return request.getfixturevalue("client")


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy_env(monkeypatch):
    """Configure ``YTT_PROXY_URL`` for the test and rebuild cached Settings.

    The standard proxy environment variables are *removed*: with them absent,
    httpx's ``trust_env`` cannot route anything anywhere, so the only way the
    sentinel could reach a client is an explicit ``proxy=`` kwarg from ytt's
    own code — exactly what the construction spy asserts against. The settings
    cache is dropped before and after so the sentinel is (and only is) visible
    to the Settings this test's app/jobs build.
    """
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("YTT_PROXY_URL", _PROXY)

    from ytt.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**overrides: object):
    """Real ``Settings`` with test-safe values; ``proxy_url`` rides the env."""
    env = {
        "YTT_ALLOWED_SUBJECTS": "test-sub",
        "YTT_CACHE_BACKEND": "emptydir",
        "YTT_CACHE_DIR": "/tmp",
        "YTT_SCRATCH_DIR": "/tmp",
        "YTT_EXTRACT_TIMEOUT_SEC": "60",
        "YTT_WHISPER_URL": _WHISPER_URL,
        **{f"YTT_{k.upper()}": str(v) for k, v in overrides.items()},
    }
    with patch.dict(os.environ, env, clear=False):
        from ytt.config import Settings

        return Settings()


class _ClientSpy:
    """Records every ``httpx.AsyncClient`` construction under a test.

    ``arm()`` patches ``httpx.AsyncClient.__init__`` (authlib's
    ``AsyncOAuth2Client`` subclasses it — the alias authlib imports *is* this
    module — so the upstream OAuth client is captured too), appending the
    kwargs of each construction. With ``transport_handler`` the spy also
    injects an ``httpx.MockTransport`` into clients that don't bring their
    own, which is what lets the *production* construction paths (e.g. the ASR
    client built inside ``run_whisper_job``) run offline unchanged.

    The isolation assertion is at this boundary because that is where the
    contract lives: ``YTT_PROXY_URL`` may only ever be threaded into a
    yt-dlp ``proxy`` option, never into any identity/ASR HTTP client.
    """

    def __init__(self) -> None:
        self.constructions: list[dict] = []

    def arm(self, monkeypatch: pytest.MonkeyPatch, *, transport_handler=None) -> None:
        real_init = httpx.AsyncClient.__init__

        def spy_init(instance, *args, **kwargs):
            if transport_handler is not None and "transport" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(transport_handler)
            self.constructions.append(kwargs)
            real_init(instance, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", spy_init)

    def assert_no_proxy(self, what: str) -> None:
        """No construction carried a ``proxy`` kwarg or the sentinel URL."""
        assert self.constructions, (
            f"{what}: the construction spy recorded no httpx.AsyncClient — "
            "the instrumentation saw nothing, so this test proves nothing. "
            "If the path legitimately stopped building a client, update the "
            "test; if it stopped being instrumented, re-arm the spy earlier."
        )
        for i, kwargs in enumerate(self.constructions, start=1):
            assert kwargs.get("proxy") is None, (
                f"{what}: httpx client #{i} was constructed with "
                f"proxy={kwargs.get('proxy')!r} — YTT_PROXY_URL must never "
                "reach the identity/ASR HTTP clients (docs/notes/"
                "proxy-egress.md traffic table: OAuth discovery, JWKS, token "
                "calls and the Whisper ASR POST are never proxied)."
            )
            for key, value in kwargs.items():
                assert _PROXY not in str(value), (
                    f"{what}: the configured sentinel YTT_PROXY_URL leaked "
                    f"into httpx client #{i} construction kwarg {key!r} "
                    f"({str(value)[:120]!r}) — proxy credentials must never "
                    "reach the identity/ASR client layer."
                )

    def assert_single_construction(self, what: str) -> None:
        assert len(self.constructions) == 1, (
            f"{what}: expected exactly one httpx.AsyncClient construction, "
            f"got {len(self.constructions)} — a second client here would "
            "mean a retry or re-dial the isolation contract forbids on "
            "these paths."
        )

    def assert_no_construction_proxied(self, what: str) -> None:
        """Like :meth:`assert_no_proxy`, but zero constructions is fine.

        For paths that may legitimately build no client at all (the JWKS
        legs) or whose client count is pinned elsewhere — the isolation
        claim is "if a client was built, it was not proxied".
        """
        for i, kwargs in enumerate(self.constructions, start=1):
            assert kwargs.get("proxy") is None, (
                f"{what}: httpx client #{i} was constructed with "
                f"proxy={kwargs.get('proxy')!r} — YTT_PROXY_URL must never "
                f"reach it ({what})."
            )
            for key, value in kwargs.items():
                assert _PROXY not in str(value), (
                    f"{what}: the configured sentinel YTT_PROXY_URL leaked "
                    f"into httpx client #{i} construction kwarg {key!r}."
                )


@pytest.fixture
def client_spy() -> _ClientSpy:
    return _ClientSpy()


class _AsrFake:
    """In-process stand-in for the Whisper service, programmable per test.

    Records every dial (method + URL) — the no-retry half of the isolation
    proof — and answers ``/v1/models`` for the startup guard and
    ``/v1/audio/transcriptions`` for the job, with a canned status, or raises
    *exc* to simulate a network-level failure (``ConnectError`` /
    ``ReadTimeout``) at the handler, exactly where the real connection pool
    would raise it.
    """

    def __init__(
        self,
        *,
        model: str = "Systran/faster-whisper-small",
        status_code: int = 200,
        exc: Exception | None = None,
    ) -> None:
        self.model = model
        self.status_code = status_code
        self.exc = exc
        self.requests: list[tuple[str, str]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        if self.exc is not None:
            raise self.exc
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": self.model}]})
        return httpx.Response(
            self.status_code,
            json={"text": "hello", "language": "en", "segments": []},
        )


async def _drive_whisper_job(
    settings,
    tmp_path: Path,
    asr: _AsrFake,
    client_spy: _ClientSpy,
    monkeypatch: pytest.MonkeyPatch,
):
    """Run a full ``run_whisper_job`` through the PRODUCTION ASR client.

    yt-dlp is stubbed at the download boundary (the audio leg's proxy
    semantics live in ``tests.unit.test_proxy``); the HTTP side under test
    here — the transcription POST — runs through the client
    ``run_whisper_job`` constructs *itself* (no ``http_client`` injected),
    with the spy substituting only the socket. Returns the final job.
    """
    scratch = Path(settings.scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    audio = scratch / f"{VIDEO_ID}.m4a"
    audio.write_bytes(b"fake audio bytes")

    client_spy.arm(monkeypatch, transport_handler=asr.handle)

    registry = WhisperJobRegistry()
    cache = MagicMock()
    cache.put = AsyncMock(return_value=True)
    job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)
    with patch("ytt.whisper._do_download_audio", return_value=str(audio)):
        await run_whisper_job(
            job, registry, settings, cache, settings.whisper_model
        )
    return await registry.get(VIDEO_ID)


def _mint_id_token(*, exp_offset: int = 3600) -> str:
    """An HS256 id_token exactly as the reference Authentik mints them."""
    from joserfc import jwk, jwt

    from ytt.config import DEFAULT_OIDC_ISSUER, get_settings

    now = int(__import__("time").time())
    key = jwk.import_key(get_settings().oauth_client_secret, "oct")
    return jwt.encode(
        {"alg": "HS256"},
        {
            "iss": DEFAULT_OIDC_ISSUER,
            "aud": get_settings().oauth_client_id,
            "sub": "proxy-isolation@example.com",
            "email": "proxy-isolation@example.com",
            "exp": now + exp_offset,
            "iat": now,
        },
        key,
    )


# ---------------------------------------------------------------------------
# Whisper ASR POST — never proxied, success + failure paths
# ---------------------------------------------------------------------------


class TestWhisperAsrPostNeverProxied:
    """``POST {YTT_WHISPER_URL}/v1/audio/transcriptions`` with the proxy
    configured: one client, no ``proxy`` kwarg, one dial — and every failure
    path lands ``asr_failed`` with still no proxied retry."""

    async def test_success_constructs_client_without_proxy(
        self, proxy_env, tmp_path, client_spy, monkeypatch
    ):
        settings = _settings(scratch_dir=str(tmp_path / "scratch"))
        asr = _AsrFake()

        final = await _drive_whisper_job(settings, tmp_path, asr, client_spy, monkeypatch)

        assert final is not None and final.status == "done", (
            f"ASR job did not complete (status {getattr(final, 'status', None)!r}) "
            "— the isolation assertions below would prove nothing"
        )
        # Exactly one dial: the configured Whisper service, nothing else.
        assert asr.requests == [
            ("POST", f"{_WHISPER_URL}/v1/audio/transcriptions")
        ], f"ASR path made unexpected dials: {asr.requests!r}"
        client_spy.assert_no_proxy("ASR POST (success path)")
        client_spy.assert_single_construction("ASR POST (success path)")

    async def test_http_error_status_is_asr_failed_with_no_proxied_retry(
        self, proxy_env, tmp_path, client_spy, monkeypatch
    ):
        """Upstream 500 → ``asr_failed``; one client, one dial — the ASR leg
        is never swept into the YouTube-path ip_blocked proxy retry."""
        settings = _settings(scratch_dir=str(tmp_path / "scratch"))
        asr = _AsrFake(status_code=500)

        final = await _drive_whisper_job(settings, tmp_path, asr, client_spy, monkeypatch)

        assert final is not None and final.status == "error"
        assert final.error_code == "asr_failed"
        assert len(asr.requests) == 1, (
            f"expected exactly one ASR dial, got {asr.requests!r} — a "
            "second dial would mean a retry, and the ASR POST has none "
            "(never a proxied one)"
        )
        client_spy.assert_no_proxy("ASR POST (500 failure path)")
        client_spy.assert_single_construction("ASR POST (500 failure path)")

    async def test_connect_error_is_asr_failed_with_no_proxied_retry(
        self, proxy_env, tmp_path, client_spy, monkeypatch
    ):
        settings = _settings(scratch_dir=str(tmp_path / "scratch"))
        asr = _AsrFake(exc=httpx.ConnectError("connection refused"))

        final = await _drive_whisper_job(settings, tmp_path, asr, client_spy, monkeypatch)

        assert final is not None and final.status == "error"
        assert final.error_code == "asr_failed"
        assert len(asr.requests) == 1
        client_spy.assert_no_proxy("ASR POST (connect-failure path)")
        client_spy.assert_single_construction("ASR POST (connect-failure path)")

    async def test_read_timeout_is_asr_failed_with_no_proxied_retry(
        self, proxy_env, tmp_path, client_spy, monkeypatch
    ):
        settings = _settings(scratch_dir=str(tmp_path / "scratch"))
        asr = _AsrFake(exc=httpx.ReadTimeout("timed out"))

        final = await _drive_whisper_job(settings, tmp_path, asr, client_spy, monkeypatch)

        assert final is not None and final.status == "error"
        assert final.error_code == "asr_failed"
        assert len(asr.requests) == 1
        client_spy.assert_no_proxy("ASR POST (timeout path)")
        client_spy.assert_single_construction("ASR POST (timeout path)")


class TestModelGuardNeverProxied:
    """The startup ``GET /v1/models`` probe constructs its own client too."""

    async def test_probe_constructs_client_without_proxy(
        self, proxy_env, client_spy, monkeypatch
    ):
        settings = _settings()
        asr = _AsrFake(model=settings.whisper_model)
        client_spy.arm(monkeypatch, transport_handler=asr.handle)

        model = await check_model_guard(settings.whisper_url, settings.whisper_model)

        assert model == settings.whisper_model
        assert asr.requests == [("GET", f"{_WHISPER_URL}/v1/models")]
        client_spy.assert_no_proxy("model guard (success path)")
        client_spy.assert_single_construction("model guard (success path)")

    async def test_probe_failure_is_swallowed_without_a_proxied_retry(
        self, proxy_env, client_spy, monkeypatch
    ):
        """The guard fails open (configured model kept) — one dial, no retry,
        no proxy."""
        settings = _settings()
        asr = _AsrFake(exc=httpx.ConnectError("whisper is down"))
        client_spy.arm(monkeypatch, transport_handler=asr.handle)

        model = await check_model_guard(settings.whisper_url, settings.whisper_model)

        assert model == settings.whisper_model, (
            "the guard must fail open to the configured model, never retry "
            "(through the proxy or otherwise)"
        )
        assert len(asr.requests) == 1
        client_spy.assert_no_proxy("model guard (failure path)")
        client_spy.assert_single_construction("model guard (failure path)")


# ---------------------------------------------------------------------------
# OIDC discovery — never proxied, success + failure paths
# ---------------------------------------------------------------------------

#: Structurally valid discovery document (same shape as the conftest fake —
#: OIDCConfiguration enforces these keys).
_DISCOVERY_DOC = {
    "issuer": "https://sso.ardenone.com/application/o/ytt/",
    "authorization_endpoint": "https://sso.ardenone.com/application/o/authorize/",
    "token_endpoint": "https://sso.ardenone.com/application/o/token/",
    "jwks_uri": "https://sso.ardenone.com/application/o/ytt/jwks/",
    "response_types_supported": ["code"],
    "subject_types_supported": ["public"],
    "id_token_signing_alg_values_supported": ["RS256"],
}


@pytest.fixture
def real_discovery(monkeypatch):
    """Undo the conftest class-level discovery stub for one test.

    ``tests/conftest.py`` permanently replaces ``OIDCProxy.get_oidc_configuration``
    so that importing ``ytt.server`` never dials the IdP. The tests here are
    ABOUT that dial, so this fixture restores the real classmethod (keeping
    the call shape ``__init__`` uses) for its duration.
    """
    from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy

    def _real(self, config_url, strict, timeout_seconds):
        return OIDCConfiguration.get_oidc_configuration(
            config_url, strict=strict, timeout_seconds=timeout_seconds
        )

    monkeypatch.setattr(OIDCProxy, "get_oidc_configuration", _real)


class TestOidcDiscoveryNeverProxied:
    """The ``GET config_url`` discovery fetch fastmcp makes at provider
    construction: no ``proxy`` kwarg, exactly one attempt — a discovery
    failure must fail startup, never retry through the proxy."""

    async def test_build_auth_provider_discovers_without_proxy(
        self, proxy_env, real_discovery, monkeypatch, client_spy
    ):
        from fastmcp.server.auth import oidc_proxy

        from ytt.auth import build_auth_provider

        calls: list[tuple[str, dict]] = []

        def recording_get(url, **kwargs):
            calls.append((str(url), kwargs))
            # fastmcp calls raise_for_status() on the response, so it must
            # carry its request (a bare Response(200) has none).
            return httpx.Response(
                200, json=_DISCOVERY_DOC, request=httpx.Request("GET", str(url))
            )

        monkeypatch.setattr(oidc_proxy.httpx, "get", recording_get)
        # The *settings* this provider is built from carry the sentinel
        # proxy (proxy_env + _settings read the env), so any kwarg threading
        # of settings.proxy_url anywhere in the construction path lands in
        # the spy.
        client_spy.arm(monkeypatch)

        settings = _settings()
        provider = build_auth_provider(settings)

        assert len(calls) == 1, (
            f"expected exactly one discovery fetch, got {calls!r}"
        )
        url, kwargs = calls[0]
        assert url == str(settings.oidc_config_url)
        assert kwargs.get("proxy") is None, (
            f"the OIDC discovery fetch was constructed with "
            f"proxy={kwargs.get('proxy')!r} — identity traffic must never "
            "traverse YTT_PROXY_URL (docs/notes/proxy-egress.md)"
        )
        assert _PROXY not in str(kwargs)
        client_spy.assert_no_construction_proxied(
            "OIDC discovery (success path)"
        )
        # The provider came up for real on the discovered document.
        assert provider.oidc_config.issuer == _DISCOVERY_DOC["issuer"]

    async def test_discovery_failure_fails_fast_without_a_proxied_retry(
        self, proxy_env, real_discovery, monkeypatch, client_spy
    ):
        from fastmcp.server.auth import oidc_proxy

        from ytt.auth import build_auth_provider

        calls: list[tuple[str, dict]] = []

        def failing_get(url, **kwargs):
            calls.append((str(url), kwargs))
            raise httpx.ConnectError(f"connection refused for {url}")

        monkeypatch.setattr(oidc_proxy.httpx, "get", failing_get)
        client_spy.arm(monkeypatch)

        with pytest.raises(httpx.ConnectError):
            build_auth_provider(_settings())

        assert len(calls) == 1, (
            f"discovery failure produced {len(calls)} attempts — startup "
            "must fail fast, never retry (and a retry through the proxy "
            "would be doubly wrong: identity traffic must not traverse "
            "YTT_PROXY_URL at all)"
        )
        assert calls[0][1].get("proxy") is None
        assert _PROXY not in str(calls[0][1])
        client_spy.assert_no_construction_proxied(
            "OIDC discovery (failure path)"
        )


# ---------------------------------------------------------------------------
# Token exchange + refresh — never proxied, success + failure paths
# ---------------------------------------------------------------------------


class _RecordingIdP:
    """Upstream IdP stand-in recording every dial the proxy makes to it.

    Answers both grants with a valid token set (the ``id_token`` a real
    HS256-signed response carries), or — when ``exchange_status`` is set —
    rejects the grant with that status, to pin the failure path: one upstream
    attempt, no proxied retry.
    """

    def __init__(self, *, exchange_status: int = 200) -> None:
        self.idp_code = "proxy-isolation-idp-code"
        self.exchange_status = exchange_status
        self.requests: list[httpx.Request] = []

    def _token_set(self) -> dict:
        from joserfc import jwk, jwt

        from ytt.config import DEFAULT_OIDC_ISSUER, get_settings

        now = int(__import__("time").time())
        key = jwk.import_key(get_settings().oauth_client_secret, "oct")
        id_token = jwt.encode(
            {"alg": "HS256"},
            {
                "iss": DEFAULT_OIDC_ISSUER,
                "aud": get_settings().oauth_client_id,
                "sub": "proxy-isolation@example.com",
                "email": "proxy-isolation@example.com",
                "exp": now + 3600,
                "iat": now,
            },
            key,
        )
        return {
            "access_token": "upstream-access-token",
            "id_token": id_token,
            "refresh_token": "upstream-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid email offline_access",
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        grant = _form_fields(request).get("grant_type", "")
        if (
            grant in ("authorization_code", "refresh_token")
            and self.exchange_status == 200
        ):
            return httpx.Response(200, json=self._token_set())
        return httpx.Response(
            self.exchange_status, json={"error": "invalid_grant"}
        )


@pytest.fixture
def mock_idp(monkeypatch, request) -> _RecordingIdP:
    """Route the OAuthProxy's upstream authlib client through the recorder.

    Same injection point as ``test_oauth_end_to_end``'s fixture (the
    ``oauth_proxy.proxy`` module attribute, resolved per construction), with
    the exchange status parametrizable via ``indirect`` for failure paths.
    """
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    from fastmcp.server.auth.oauth_proxy import proxy as oauth_proxy_module

    idp = _RecordingIdP(
        exchange_status=getattr(request, "param", 200) or 200
    )

    class _MockTransportClient(AsyncOAuth2Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(idp.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(oauth_proxy_module, "AsyncOAuth2Client", _MockTransportClient)
    return idp


#: The upstream token endpoint — tests/conftest.py's fake OIDC config. Every
#: dial the OAuthProxy makes upstream must land here and nowhere else.
_UPSTREAM_TOKEN_ENDPOINT = "https://sso.ardenone.com/application/o/token/"


class TestTokenExchangeNeverProxied:
    """The authorization-code exchange and the transparent refresh — the two
    upstream token calls the proxy can initiate — carry no proxy anywhere."""

    async def test_code_exchange_and_refresh_construct_clients_without_proxy(
        self, proxy_env, mock_idp, asgi_client, client_spy, monkeypatch
    ):
        from ytt.config import get_settings

        # Arm before the flow: both upstream clients are built per dial.
        client_spy.arm(monkeypatch)

        client_id, code, verifier = walk_full_flow(asgi_client, mock_idp)

        resp = exchange_code(asgi_client, client_id, code, verifier)
        assert resp.status_code == 200, resp.text[:300]
        tokens = resp.json()
        assert tokens.get("refresh_token"), (
            "no refresh token issued — the refresh leg below proves nothing"
        )

        refresh = redeem_refresh_token(asgi_client, client_id, tokens["refresh_token"])
        assert refresh.status_code == 200, refresh.text[:300]

        # Both grants actually went upstream — to the IdP, over a client
        # that carried no proxy.
        upstream_grants = [
            r for r in mock_idp.requests
            if str(r.url) == _UPSTREAM_TOKEN_ENDPOINT
        ]
        assert len(upstream_grants) == 2, (
            f"expected the code exchange + one refresh upstream "
            f"({[str(r.url) for r in mock_idp.requests]!r})"
        )
        client_spy.assert_no_proxy("token exchange + refresh")
        assert get_settings().proxy_url == _PROXY, (
            "test setup broke: the proxy sentinel was not configured, so "
            "these assertions prove nothing"
        )

    @pytest.mark.parametrize("mock_idp", [400], indirect=True)
    async def test_exchange_failure_makes_one_upstream_attempt_never_proxied(
        self, proxy_env, mock_idp, asgi_client, client_spy, monkeypatch
    ):
        """Upstream rejects the code with 400. The exchange happens eagerly
        at ``/auth/callback`` in this fastmcp (not at POST /token), so the
        rejection surfaces there: ytt errors, and there is exactly ONE
        upstream attempt from a proxy-free client — no proxied retry, no
        loop."""
        client_spy.arm(monkeypatch)

        # walk_full_flow asserts the callback's success 302 — but here the
        # callback IS the failing dial, so walk the same grant by hand up
        # to that point.
        client_id = register_client(asgi_client, CLAUDE_REDIRECT)["client_id"]
        verifier, challenge = _pkce_pair()

        r_authorize = asgi_client.get(
            f"{BASE}{_issuer_path()}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLAUDE_REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "proxy-isolation-failure",
                "scope": "openid email offline_access",
            },
        )
        assert r_authorize.status_code == 302, r_authorize.text[:300]
        consent_url = r_authorize.headers["location"]
        r_consent = asgi_client.get(consent_url)
        assert r_consent.status_code == 200, r_consent.text[:300]
        csrf = re.search(
            r'name="csrf_token" value="([^"]+)"', r_consent.text
        ).group(1)
        r_approval = asgi_client.post(
            f"{BASE}{_issuer_path()}/consent",
            data={
                "txn_id": _query_params(consent_url)["txn_id"],
                "csrf_token": csrf,
                "action": "approve",
            },
        )
        assert r_approval.status_code == 302, r_approval.text[:300]

        # The upstream leg — where the IdP's rejection lands.
        upstream_query = _query_params(r_approval.headers["location"])
        r_callback = asgi_client.get(
            f"{BASE}{_issuer_path()}/auth/callback",
            params={"code": mock_idp.idp_code, "state": upstream_query["state"]},
        )
        assert r_callback.status_code >= 400, (
            f"expected the upstream rejection to surface as an error at "
            f"the callback, got {r_callback.status_code}"
        )
        # And the client-facing grant is dead too: no code was issued, so
        # a /token redemption yields nothing (and dials nowhere).
        resp = exchange_code(asgi_client, client_id, "never-issued", verifier)
        assert resp.status_code >= 400, resp.text[:300]

        upstream_grants = [
            r for r in mock_idp.requests
            if str(r.url) == _UPSTREAM_TOKEN_ENDPOINT
        ]
        assert len(upstream_grants) == 1, (
            f"expected exactly one upstream attempt on the failure path, "
            f"got {len(upstream_grants)} — a retry (proxied or not) is not "
            "part of the contract"
        )
        client_spy.assert_no_proxy("token exchange failure path")


# ---------------------------------------------------------------------------
# JWKS — no fetch exists to proxy (symmetric-local verification)
# ---------------------------------------------------------------------------


class TestJwksNeverProxied:
    """ytt verifies upstream id tokens with the client secret (HS256) —
    ``build_auth_provider`` passes ``public_key``, never ``jwks_uri`` — so
    token validation performs no HTTP at all, and a JWKS fetch that could be
    proxied cannot exist. Pinned against real (issued-shaped) and failing
    tokens: neither may construct a single client."""

    @staticmethod
    def _verifier(settings):
        from ytt.auth import UpstreamIdTokenVerifier, build_auth_provider

        provider = build_auth_provider(settings)
        # fastmcp 3.4.x stores the constructor kwarg under a private name
        # (oauth_proxy/proxy.py: ``self._token_validator = token_verifier``).
        # Reading the private attribute deliberately: if fastmcp renames it,
        # this fails LOUDLY here instead of quietly vacating the assertion.
        verifier = provider._token_validator
        assert isinstance(verifier, UpstreamIdTokenVerifier), (
            "the provider's token verifier is no longer ytt's "
            "UpstreamIdTokenVerifier — re-pin this module's JWKS legs "
            "against whatever replaced it"
        )
        return verifier

    async def test_verifier_is_symmetric_local_and_never_fetches(
        self, proxy_env, client_spy, monkeypatch
    ):
        settings = _settings()
        verifier = self._verifier(settings)

        assert verifier.jwks_uri is None, (
            "the token verifier grew a JWKS URI — key fetching would then "
            "ride the HTTP client layer and MUST be pinned proxy-free here "
            "before shipping (docs/notes/proxy-egress.md: JWKS is never "
            "proxied)"
        )
        assert verifier.public_key == settings.oauth_client_secret

        client_spy.arm(monkeypatch)

        access = await verifier.load_access_token(_mint_id_token())
        assert access is not None, "a valid HS256 id_token must verify"

        assert client_spy.constructions == [], (
            "token validation constructed an HTTP client — a JWKS (or any) "
            "fetch appeared on the validation path; nothing on it may touch "
            "the network, proxied or not"
        )

    async def test_expired_token_rejection_is_equally_network_free(
        self, proxy_env, client_spy, monkeypatch
    ):
        """Failure path: rejection must not fall back to any network call."""
        verifier = self._verifier(_settings())
        client_spy.arm(monkeypatch)

        rejected = await verifier.load_access_token(_mint_id_token(exp_offset=-3600))
        assert rejected is None, "an expired id_token must be rejected"

        assert client_spy.constructions == []


# ---------------------------------------------------------------------------
# Static leg — the proxy must never be exported into the process environment
# ---------------------------------------------------------------------------


class TestProxyNeverViaProcessEnvironment:
    """``docs/notes/proxy-egress.md``: "The proxy value is threaded as yt-dlp's
    ``proxy`` option (per-``YoutubeDL`` construction), never via process
    environment (``HTTP_PROXY`` etc.) — that would sweep the ASR POST and
    OAuth traffic into the proxy." An ``os.environ`` write (or ``os.putenv``)
    touching a proxy variable is exactly that mistake; the runtime tests
    above cannot see it because ``trust_env`` pickup bypasses the
    construction kwargs they spy on."""

    def test_package_never_writes_proxy_environment_variables(self) -> None:
        violations: list[str] = []
        for source in sorted(PACKAGE_DIR.glob("*.py")):
            text = source.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(source))
            for node in ast.walk(tree):
                segment = ast.get_source_segment(text, node) or ""
                if "proxy" not in segment.lower():
                    continue
                # os.environ[...] = / += — a Subscript write over `environ`
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                    for target in targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and "environ" in ast.unparse(target.value)
                        ):
                            violations.append(f"{source.name}: {segment}")
                # os.environ.setdefault(...) / os.environ.update(...) / os.putenv(...)
                if isinstance(node, ast.Call):
                    func = ast.unparse(node.func)
                    touches_environ = "environ" in func
                    is_putenv = func.endswith("putenv")
                    if touches_environ or is_putenv:
                        violations.append(f"{source.name}: {segment}")

        assert not violations, (
            "ytt/ writes a proxy variable into the process environment:\n"
            "  - " + "\n  - ".join(violations) + "\n"
            "httpx's trust_env would then sweep the ASR POST and OAuth "
            "traffic into that proxy (docs/notes/proxy-egress.md: the proxy "
            "is threaded as yt-dlp's proxy option, never via the process "
            "environment). Configure YTT_PROXY_URL for Settings to read; "
            "let fetch.run_with_proxy_retry own the only dial through it."
        )
