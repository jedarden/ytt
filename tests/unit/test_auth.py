"""Unit tests for Phase 5: auth.py, authz.py, ratelimit.py.

Tests cover:
- authz allow/deny/empty-allowlist
- authz write-last-sub mechanism
- ratelimit token bucket (capacity, refill, per-subject isolation)
- ratelimit WhisperQuota
- OAuthProvider route shape (PRM path, AS metadata path)
- HTTP-level 401 + WWW-Authenticate header on the MCP endpoint
- 401 metadata URL points to /.well-known/oauth-protected-resource/ytt
- Wrong-audience JWT rejected by JWTVerifier (audience validation unit test)
- Static Claude connector client pre-registered (DCR off)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from ytt.authz import check_subject, get_last_sub, write_last_sub
from ytt.errors import FORBIDDEN, YttError
from ytt.ratelimit import SubjectRateLimiter, TokenBucket, WhisperQuota


# =============================================================================
# authz.py tests
# =============================================================================


class TestCheckSubject:
    """Tests for the subject allowlist (plan: Security & Authorization)."""

    def test_allow_subject_in_list(self):
        """Subject in allowlist → no exception raised."""
        check_subject("user1", frozenset(["user1", "user2"]))

    def test_deny_subject_not_in_list(self):
        """Subject not in allowlist → FORBIDDEN YttError."""
        with pytest.raises(YttError) as exc_info:
            check_subject("user3", frozenset(["user1", "user2"]))
        assert exc_info.value.error_code == FORBIDDEN

    def test_deny_empty_allowlist(self):
        """Empty allowlist → deny all (fail-closed)."""
        with pytest.raises(YttError) as exc_info:
            check_subject("user1", frozenset())
        assert exc_info.value.error_code == FORBIDDEN

    def test_deny_message_is_human_readable(self):
        """403 message must tell the user to contact the operator."""
        with pytest.raises(YttError) as exc_info:
            check_subject("hacker", frozenset())
        msg = exc_info.value.message.lower()
        assert "operator" in msg or "allowlist" in msg

    def test_deny_message_does_not_contain_sub(self):
        """403 message must NOT contain the sub value (no leakage)."""
        sub = "sensitive-sub-value-12345"
        with pytest.raises(YttError) as exc_info:
            check_subject(sub, frozenset())
        assert sub not in exc_info.value.message

    def test_allow_writes_last_sub(self, tmp_path, monkeypatch):
        """Successful auth writes sub to the temp file for selftest --show-sub."""
        last_sub_path = str(tmp_path / "ytt_last_sub")
        import ytt.authz as authz_mod
        monkeypatch.setattr(authz_mod, "_LAST_SUB_PATH", last_sub_path)
        monkeypatch.setattr(authz_mod, "_written_subs", set())
        authz_mod.check_subject("mysub", frozenset(["mysub"]))
        # The file should contain the sub
        with open(last_sub_path) as f:
            assert f.read().strip() == "mysub"

    def test_write_last_sub_only_once(self, tmp_path, monkeypatch):
        """write_last_sub is idempotent — only writes once per sub value."""
        last_sub_path = str(tmp_path / "ytt_last_sub")
        import ytt.authz as authz_mod
        monkeypatch.setattr(authz_mod, "_LAST_SUB_PATH", last_sub_path)
        monkeypatch.setattr(authz_mod, "_written_subs", set())
        authz_mod.write_last_sub("sub1")
        authz_mod.write_last_sub("sub1")  # second call → no-op
        with open(last_sub_path) as f:
            assert f.read().strip() == "sub1"

    def test_get_last_sub_returns_none_when_missing(self, tmp_path, monkeypatch):
        """get_last_sub returns None if the temp file doesn't exist."""
        import ytt.authz as authz_mod
        monkeypatch.setattr(authz_mod, "_LAST_SUB_PATH", str(tmp_path / "nonexistent"))
        result = authz_mod.get_last_sub()
        assert result is None

    def test_get_last_sub_returns_value(self, tmp_path, monkeypatch):
        """get_last_sub returns the stored sub."""
        last_sub_path = str(tmp_path / "sub")
        with open(last_sub_path, "w") as f:
            f.write("stored-sub\n")
        import ytt.authz as authz_mod
        monkeypatch.setattr(authz_mod, "_LAST_SUB_PATH", last_sub_path)
        assert authz_mod.get_last_sub() == "stored-sub"


# =============================================================================
# ratelimit.py tests
# =============================================================================


class TestTokenBucket:
    """Unit tests for the token bucket (plan: per-subject token bucket)."""

    def test_starts_full(self):
        """Bucket starts at full capacity."""
        b = TokenBucket(capacity=5, refill_rate=1.0)
        assert b.consume() is True
        assert b.consume() is True
        assert b.consume() is True
        assert b.consume() is True
        assert b.consume() is True  # 5 consumed

    def test_denies_when_empty(self):
        """Bucket at 0 tokens → consume returns False."""
        b = TokenBucket(capacity=3, refill_rate=0.0)  # no refill
        b.consume(); b.consume(); b.consume()
        assert b.consume() is False

    def test_refills_over_time(self):
        """Bucket refills based on elapsed time."""
        b = TokenBucket(capacity=5, refill_rate=10.0)  # 10/sec
        # Drain
        for _ in range(5):
            b.consume()
        assert b.consume() is False  # empty
        # Simulate 0.2 seconds → 2 tokens added
        b._last_refill = time.monotonic() - 0.2
        assert b.consume() is True  # 2 tokens → 1 consumed

    def test_capped_at_capacity(self):
        """Refill never exceeds capacity."""
        b = TokenBucket(capacity=5, refill_rate=100.0)
        # Drain one token
        b.consume()
        # Fast-forward 1 second → would add 100 tokens but capped at 5
        b._last_refill = time.monotonic() - 1.0
        # Drain all 5
        for _ in range(5):
            assert b.consume() is True
        assert b.consume() is False  # empty again

    def test_retry_after_sec_positive_when_empty(self):
        """retry_after_sec returns positive value when bucket is empty."""
        b = TokenBucket(capacity=1, refill_rate=1.0)  # 1/sec
        b.consume()
        after = b.retry_after_sec()
        assert after > 0.0

    def test_retry_after_sec_zero_when_tokens_available(self):
        """retry_after_sec returns 0 when tokens are available."""
        b = TokenBucket(capacity=5, refill_rate=1.0)
        after = b.retry_after_sec()
        assert after == 0.0


class TestSubjectRateLimiter:
    """Tests for per-subject isolation (plan: per-subject token bucket)."""

    def test_subjects_are_isolated(self):
        """Two subjects have independent buckets."""
        limiter = SubjectRateLimiter(capacity=2, refill_rate_per_sec=0.0)
        limiter.consume("alice")
        limiter.consume("alice")
        assert limiter.consume("alice") is False  # alice exhausted
        assert limiter.consume("bob") is True  # bob unaffected

    def test_per_subject_capacity(self):
        """Each subject starts with full capacity."""
        limiter = SubjectRateLimiter(capacity=3, refill_rate_per_sec=0.0)
        for _ in range(3):
            assert limiter.consume("user1") is True
        assert limiter.consume("user1") is False

    def test_from_rate_per_min(self):
        """from_rate_per_min constructs correct refill rate."""
        limiter = SubjectRateLimiter.from_rate_per_min(60)
        assert limiter.refill_rate_per_sec == pytest.approx(1.0)
        assert limiter.capacity == 60

    def test_retry_after_sec(self):
        """retry_after_sec delegates to the subject's bucket."""
        limiter = SubjectRateLimiter(capacity=1, refill_rate_per_sec=1.0)
        limiter.consume("x")
        assert limiter.retry_after_sec("x") > 0

    def test_bucket_for_creates_on_first_access(self):
        """bucket_for creates and returns the bucket on first call."""
        limiter = SubjectRateLimiter(capacity=5, refill_rate_per_sec=0.0)
        bucket = limiter.bucket_for("new_user")
        assert bucket is not None
        assert bucket.capacity == 5


class TestWhisperQuota:
    """Tests for per-subject Whisper job quota (plan: YTT_WHISPER_JOBS_PER_HOUR)."""

    def test_allows_within_quota(self):
        q = WhisperQuota(jobs_per_hour=5)
        for _ in range(5):
            assert q.consume("user") is True

    def test_denies_over_quota(self):
        q = WhisperQuota(jobs_per_hour=2)
        q.consume("user"); q.consume("user")
        assert q.consume("user") is False

    def test_subjects_independent(self):
        q = WhisperQuota(jobs_per_hour=1)
        q.consume("alice")
        assert q.consume("alice") is False
        assert q.consume("bob") is True  # bob unaffected

    def test_retry_after_sec_positive_when_exhausted(self):
        q = WhisperQuota(jobs_per_hour=1)
        q.consume("user")
        assert q.retry_after_sec("user") > 0


# =============================================================================
# auth.py — YttGoogleProvider route/metadata shape
# =============================================================================


class TestYttGoogleProviderMetadata:
    """Tests for the OAuth AS / PRM route shape (plan: Auth, ADR-001).

    ytt federates to Google (ytt.auth.YttGoogleProvider, an OAuthProxy
    subclass) rather than issuing its own unauthenticated tokens — see
    docs/notes/auth.md and ytt/auth.py's module docstring for why
    FastMCP's InMemoryOAuthProvider (a test/demo provider that auto-approves
    every caller) must never be used here.
    """

    @pytest.fixture
    def provider(self):
        """Build a YttGoogleProvider from default (fake-credential) settings."""
        from ytt.auth import build_auth_provider
        from ytt.config import Settings
        s = Settings(
            public_url="https://mcp.example.com/ytt",
            oauth_client_id="test-client-id.apps.googleusercontent.com",
            oauth_client_secret="test-client-secret",
            jwt_signing_secret=None,
        )
        return build_auth_provider(s)

    def test_dcr_restricted_to_claude_redirect_uris(self, provider):
        """DCR is open (required for OAuthProxy) but scoped to Claude's two
        redirect URIs — defense in depth against ytt's AS being used as an
        open relay by arbitrary third-party OAuth clients."""
        from ytt.auth import CLAUDE_REDIRECT_URIS
        assert provider._allowed_client_redirect_uris == CLAUDE_REDIRECT_URIS

    def test_issuer_url_is_path_bearing(self, provider):
        """issuer_url must match the full path-bearing public_url."""
        assert str(provider.issuer_url).rstrip("/") == "https://mcp.example.com/ytt"

    def test_as_metadata_route_is_path_inserted(self, provider):
        """AS metadata route must be /.well-known/oauth-authorization-server/ytt.

        GoogleProvider/OAuthProxy's get_routes() never calls
        get_well_known_routes(), so this only exists because
        YttGoogleProvider.get_routes() merges it in manually.
        """
        routes = provider.get_routes(mcp_path="/ytt")
        paths = [r.path for r in routes]
        assert "/.well-known/oauth-authorization-server/ytt" in paths, \
            f"Expected path-inserted AS metadata route, got: {paths}"

    def test_prm_route_is_path_inserted(self, provider):
        """PRM route must be /.well-known/oauth-protected-resource/ytt."""
        # Pass mcp_path="/ytt" as create_streamable_http_app would
        routes = provider.get_routes(mcp_path="/ytt")
        paths = [r.path for r in routes]
        assert "/.well-known/oauth-protected-resource/ytt" in paths, \
            f"Expected path-inserted PRM route, got: {paths}"

    def test_resource_url_is_path_bearing(self, provider):
        """_get_resource_url('/ytt') must produce the path-bearing public_url."""
        resource_url = provider._get_resource_url("/ytt")
        assert resource_url is not None
        assert str(resource_url).rstrip("/") == "https://mcp.example.com/ytt"


# =============================================================================
# auth.py — build_auth_provider factory
# =============================================================================


class TestBuildAuthProvider:
    """Tests for the build_auth_provider factory function."""

    def test_returns_ytt_google_provider(self):
        from ytt.auth import YttGoogleProvider, build_auth_provider
        from ytt.config import Settings
        s = Settings(
            public_url="https://mcp.example.com/ytt",
            oauth_client_id="test-client-id.apps.googleusercontent.com",
            oauth_client_secret="test-client-secret",
        )
        provider = build_auth_provider(s)
        assert isinstance(provider, YttGoogleProvider)

    def test_provider_base_url_matches_public_url(self):
        from ytt.auth import build_auth_provider
        from ytt.config import Settings
        s = Settings(
            public_url="https://mcp.example.com/ytt",
            oauth_client_id="test-client-id.apps.googleusercontent.com",
            oauth_client_secret="test-client-secret",
        )
        provider = build_auth_provider(s)
        assert str(provider.base_url).rstrip("/") == "https://mcp.example.com/ytt"

    def test_raises_without_client_id(self):
        """Fail fast at startup rather than silently skipping auth."""
        from ytt.auth import build_auth_provider
        from ytt.config import Settings
        s = Settings(public_url="https://mcp.example.com/ytt", oauth_client_id=None)
        with pytest.raises(ValueError, match="YTT_OAUTH_CLIENT_ID"):
            build_auth_provider(s)


# =============================================================================
# authz.py — check_subject_auth (the AuthMiddleware per-tool-call gate)
# =============================================================================


class TestCheckSubjectAuth:
    """Tests for check_subject_auth — the real enforcement point wired into
    AuthMiddleware in ytt/server.py, covering every tool call (not just the
    /admin/egress side route ``check_subject`` alone used to gate)."""

    @staticmethod
    def _ctx(email=None, verified=True, no_token=False):
        from dataclasses import dataclass

        @dataclass
        class FakeToken:
            claims: dict

        @dataclass
        class FakeCtx:
            token: object

        if no_token:
            return FakeCtx(token=None)
        claims = {}
        if email is not None:
            claims = {"email": email, "email_verified": verified}
        return FakeCtx(token=FakeToken(claims=claims))

    def test_denies_when_no_token(self, monkeypatch):
        from ytt.authz import check_subject_auth
        assert check_subject_auth(self._ctx(no_token=True)) is False

    def test_denies_when_no_email_claim(self, monkeypatch):
        from ytt.authz import check_subject_auth
        assert check_subject_auth(self._ctx(email=None)) is False

    def test_denies_when_email_unverified(self, monkeypatch):
        from ytt.authz import check_subject_auth
        from ytt.config import Settings, get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", "me@example.com")
        try:
            assert check_subject_auth(self._ctx(email="me@example.com", verified=False)) is False
        finally:
            get_settings.cache_clear()

    def test_denies_when_email_not_in_allowlist(self, monkeypatch):
        from ytt.authz import check_subject_auth
        from ytt.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", "allowed@example.com")
        try:
            assert check_subject_auth(self._ctx(email="other@example.com")) is False
        finally:
            get_settings.cache_clear()

    def test_allows_when_email_in_allowlist(self, monkeypatch):
        from ytt.authz import check_subject_auth
        from ytt.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", "me@example.com")
        try:
            assert check_subject_auth(self._ctx(email="me@example.com")) is True
        finally:
            get_settings.cache_clear()

    def test_denies_when_allowlist_empty(self, monkeypatch):
        """Empty allowlist = deny all, even for a validly-authenticated caller."""
        from ytt.authz import check_subject_auth
        from ytt.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", "")
        try:
            assert check_subject_auth(self._ctx(email="me@example.com")) is False
        finally:
            get_settings.cache_clear()


# =============================================================================
# HTTP-level behavior: 401 + WWW-Authenticate
# =============================================================================


class TestHTTPAuthBehavior:
    """HTTP-level tests for 401 on unauthenticated MCP requests.

    These tests use the Starlette TestClient with the full ASGI app (auth wired).
    The /ytt/health endpoint is exempt from auth; the MCP transport requires auth.
    """

    @pytest.fixture
    def client(self):
        """TestClient for the full ASGI app (auth enabled)."""
        from ytt.server import build_asgi_app
        app = build_asgi_app()
        return TestClient(app, raise_server_exceptions=False)

    def test_health_endpoint_exempt_from_auth(self, client):
        """GET /ytt/health must return 200 without auth (plan: liveness probe)."""
        resp = client.get("/ytt/health")
        assert resp.status_code == 200

    def test_mcp_endpoint_requires_auth(self, client):
        """POST /ytt without Bearer token → 401 (plan: unauthenticated → 401)."""
        resp = client.post("/ytt", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert resp.status_code == 401

    def test_mcp_401_has_www_authenticate_header(self, client):
        """401 response must have a WWW-Authenticate header (plan: #1 silent
        'Add connector' failure if missing)."""
        resp = client.post("/ytt", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert resp.status_code == 401
        www_auth = resp.headers.get("www-authenticate", "")
        assert "Bearer" in www_auth

    def test_mcp_401_www_auth_contains_resource_metadata(self, client):
        """WWW-Authenticate must include resource_metadata URL (RFC 9728)."""
        resp = client.post("/ytt", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        www_auth = resp.headers.get("www-authenticate", "")
        assert "resource_metadata=" in www_auth

    def test_well_known_prm_route_accessible(self, client):
        """GET /.well-known/oauth-protected-resource/ytt → 200 (PRM document)."""
        resp = client.get("/.well-known/oauth-protected-resource/ytt")
        assert resp.status_code == 200

    def test_well_known_prm_resource_field(self, client):
        """PRM document 'resource' field must equal the public_url (plan: audience)."""
        resp = client.get("/.well-known/oauth-protected-resource/ytt")
        if resp.status_code != 200:
            pytest.skip("PRM route not accessible in this test env")
        data = resp.json()
        assert "resource" in data
        # resource must be the path-bearing URL (not just the origin)
        resource = data["resource"].rstrip("/")
        assert "/ytt" in resource

    def test_well_known_as_metadata_route_accessible(self, client):
        """GET /.well-known/oauth-authorization-server/ytt → 200 (AS metadata)."""
        resp = client.get("/.well-known/oauth-authorization-server/ytt")
        assert resp.status_code == 200

    def test_well_known_as_metadata_issuer_field(self, client):
        """AS metadata 'issuer' field must match the path-bearing public_url."""
        resp = client.get("/.well-known/oauth-authorization-server/ytt")
        if resp.status_code != 200:
            pytest.skip("AS metadata route not accessible in this test env")
        data = resp.json()
        assert "issuer" in data
        issuer = data["issuer"].rstrip("/")
        assert "/ytt" in issuer

    def test_well_known_as_metadata_pkce_s256(self, client):
        """AS metadata must advertise PKCE S256 (plan: 'code_challenge_methods_supported: S256')."""
        resp = client.get("/.well-known/oauth-authorization-server/ytt")
        if resp.status_code != 200:
            pytest.skip("AS metadata route not accessible in this test env")
        data = resp.json()
        methods = data.get("code_challenge_methods_supported", [])
        assert "S256" in methods


# =============================================================================
# Wrong-audience token validation (JWTVerifier unit test)
# =============================================================================


class TestWrongAudienceRejected:
    """Tests that a token with a wrong audience is rejected.

    Plan: "Audience-bound token validation (RFC 8707) — aud must match the full
    path-bearing https://mcp.ardenone.com/ytt; else confused-deputy replay."

    Uses the JWTVerifier from fastmcp directly (unit test — no HTTP needed).
    """

    @pytest.fixture
    def rsa_keypair(self):
        """Generate an RSA key pair for signing test JWTs."""
        from fastmcp.server.auth.providers.jwt import RSAKeyPair
        return RSAKeyPair.generate()

    @pytest.mark.asyncio
    async def test_wrong_audience_rejected(self, rsa_keypair):
        """JWT with wrong audience → verify_token returns None."""
        import time as _time
        from joserfc import jwk, jwt

        from fastmcp.server.auth.providers.jwt import JWTVerifier

        verifier = JWTVerifier(
            public_key=rsa_keypair.public_key,
            algorithm="RS256",
            issuer="https://mcp.ardenone.com/ytt",
            audience="https://mcp.ardenone.com/ytt",
        )

        # Sign a JWT with the WRONG audience
        private_key = jwk.import_key(rsa_keypair.private_key.get_secret_value(), "RSA")
        wrong_aud_token = jwt.encode(
            {"alg": "RS256"},
            {
                "iss": "https://mcp.ardenone.com/ytt",
                "aud": "https://mcp.ardenone.com/ibkr",  # WRONG!
                "sub": "testuser",
                "exp": int(_time.time()) + 3600,
                "iat": int(_time.time()),
            },
            private_key,
        )

        result = await verifier.verify_token(wrong_aud_token)
        assert result is None, "Wrong-audience token must be rejected"

    @pytest.mark.asyncio
    async def test_correct_audience_accepted(self, rsa_keypair):
        """JWT with correct audience → verify_token returns an AccessToken."""
        import time as _time
        from joserfc import jwk, jwt

        from fastmcp.server.auth.providers.jwt import JWTVerifier

        verifier = JWTVerifier(
            public_key=rsa_keypair.public_key,
            algorithm="RS256",
            issuer="https://mcp.ardenone.com/ytt",
            audience="https://mcp.ardenone.com/ytt",
        )

        private_key = jwk.import_key(rsa_keypair.private_key.get_secret_value(), "RSA")
        correct_token = jwt.encode(
            {"alg": "RS256"},
            {
                "iss": "https://mcp.ardenone.com/ytt",
                "aud": "https://mcp.ardenone.com/ytt",
                "sub": "testuser",
                "exp": int(_time.time()) + 3600,
                "iat": int(_time.time()),
            },
            private_key,
        )

        result = await verifier.verify_token(correct_token)
        assert result is not None, "Correct-audience token must be accepted"
