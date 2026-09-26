"""Secret-redaction regression: ``YTT_OAUTH_CLIENT_SECRET`` and credentials
embedded in ``YTT_WHISPER_URL`` never reach an operator- or client-visible
surface.

The two configuration inputs this module covers can both carry a credential:

- ``YTT_OAUTH_CLIENT_SECRET`` is a plain secret string — it keys the HS256
  upstream id-token verifier (``ytt.auth``) and is never printable-safe, so
  no surface may echo it, whatever its shape;
- ``YTT_WHISPER_URL`` is a URL and may embed basic-auth userinfo
  (``http://user:pass@whisper.internal:8000``) exactly the way
  ``YTT_PROXY_URL`` can — the value-scan rule ``ytt.observability`` already
  applies to log fields.

Surfaces pinned (the leak matrix, docs/notes/auth.md § Credential redaction
guarantee):

1. **Startup validation errors** — pydantic echoes the offending input in
   every rendered ``ValidationError``, and for a ``mode="after"`` *model*
   validator failure (invariant 7, burst resolution) that echo is the whole
   input mapping, secrets included. The rendered error is what a
   CrashLooping pod prints: the one place a misconfigured secret would land
   in plaintext. ``Settings.__init__`` rebuilds the error with secret-named
   inputs ``<redacted>`` and credential-bearing URL inputs userinfo-stripped
   (``ytt.config._redacted_validation_error``).
2. **Logs** — the structlog pipeline (field-name blocklist, credential-URL
   value scan) must also cover the ``exception``/``stack_trace`` fields
   ``format_exc_info`` produces, so ``log.exception`` on a URL-quoting
   failure renders sanitized.
3. **Transcript-job failures / exception responses** — a ``WhisperJob``'s
   ``message`` is verbatim-relayable (``get_transcript_job`` returns it to
   the MCP client), so every exception string that becomes one must pass
   :func:`ytt.observability.redact_credentials` — including the httpx
   whisper-service handlers and the job's catch-all, where
   ``httpx.InvalidURL``-shaped errors (URL-quoting, but plain ``ValueError``s)
   land.
4. **Metrics** — error paths bump counters with static taxonomy labels only;
   the rendered exposition must stay canary-free.
5. **OAuth failures** — every rejection shape of the upstream id-token
   verifier (bad signature, expired, ``exp``-less, ``nbf``-future, garbage)
   returns ``None`` without ever echoing the verifying key, in its return
   value or its logs; provider construction logs nothing either.

Scoped separately from the proxy-credential redaction contract
(``tests/unit/test_proxy.py`` — covers ``YTT_PROXY_URL`` quoting by yt-dlp
and the egress probe) and from the startup fail-closed credential gates
(``tests/unit/test_oauth_startup_fail_closed.py`` — pins that a missing or
blank credential pair fails startup). This module's canaries are *valid*
configuration values, so any appearance in a rendered surface is a genuine
leak, not a formatting artifact. Never replace them with a real credential.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import jwt as pyjwt

from ytt import errors
from ytt.auth import UpstreamIdTokenVerifier, build_auth_provider
from ytt.config import DEFAULT_OIDC_ISSUER, Settings
from ytt.models import WhisperJob
from ytt.observability import configure_logging, get_logger
from ytt.whisper import WhisperJobRegistry, run_whisper_job

# ---------------------------------------------------------------------------
# Canaries — valid, distinctive, never real credentials
# ---------------------------------------------------------------------------

#: ≥32 bytes so pyjwt raises no InsecureKeyLengthWarning while signing.
_CANARY_CLIENT_ID = "canary-client-id-redaction-4e82b6"
_CANARY_CLIENT_SECRET = "canary-oauth-client-secret-redaction-c91f42ab37d6"
_CANARY_JWT_SIGNING_KEY = "canary-jwt-signing-key-redaction-77d0e5a19c43"

_CANARY_WHISPER_USER = "whisper-alice-redaction"
_CANARY_WHISPER_PASS = "whisper-hunter2-redaction-c3b1"
_CANARY_WHISPER_USERINFO = f"{_CANARY_WHISPER_USER}:{_CANARY_WHISPER_PASS}"
_CANARY_WHISPER_HOSTPORT = "whisper.internal.example:8000"
_CANARY_WHISPER_URL = f"http://{_CANARY_WHISPER_USERINFO}@{_CANARY_WHISPER_HOSTPORT}"

#: Everything a rendered surface must NOT contain. The userinfo pair and the
#: password alone are the Whisper-URL credentials; the two key strings are the
#: OAuth secrets. (The username alone is not a secret; redaction strips it
#: anyway as part of the userinfo.)
_LEAK_MARKERS = (
    _CANARY_CLIENT_SECRET,
    _CANARY_JWT_SIGNING_KEY,
    _CANARY_WHISPER_USERINFO,
    _CANARY_WHISPER_PASS,
)

_VALID_PUBLIC_URL = "https://ytt.example.com/ytt"


def _assert_secret_free(rendered: str, context: str) -> None:
    """Fail with the offending surface when any canary value appears in it."""
    for marker in _LEAK_MARKERS:
        assert marker not in rendered, (
            f"{context}: credential value {marker!r} leaked into:\n{rendered[:2000]}"
        )


# ---------------------------------------------------------------------------
# 1. Startup validation errors (Settings construction)
# ---------------------------------------------------------------------------


class TestStartupValidationErrors:
    """A Settings construction failure never echoes a credential value."""

    @staticmethod
    def _poisoned(**overrides: Any) -> dict[str, Any]:
        """Valid config with every credential input set to its canary."""
        kwargs: dict[str, Any] = {
            "public_url": _VALID_PUBLIC_URL,
            "oauth_client_secret": _CANARY_CLIENT_SECRET,
            "jwt_signing_secret": _CANARY_JWT_SIGNING_KEY,
            "whisper_url": _CANARY_WHISPER_URL,
        }
        kwargs.update(overrides)
        return kwargs

    def test_canaries_are_valid_config(self):
        """Control: the poisoned values construct fine and round-trip — every
        raise below is attributable to the injected config fault alone, so a
        leak assertion on the rendered error is about a *working* credential,
        not a rejected-junk artifact."""
        s = Settings(**self._poisoned())
        assert s.oauth_client_secret == _CANARY_CLIENT_SECRET
        assert s.whisper_url == _CANARY_WHISPER_URL

    @pytest.mark.parametrize(
        "fault,message_fragment",
        [
            # Model-level (mode="after") failures — pre-fix, pydantic rendered
            # these with the WHOLE input dict echoed, secrets included.
            (
                {"max_asr_duration_sec": 1200, "whisper_timeout_sec": 100},
                "Invariant 7",
            ),
            (
                {"rate_limit_per_min": 0, "rate_limit_burst": 5},
                "deny-all",
            ),
            # Field-level failures — pydantic echoes the failing field's value.
            ({"public_url": ""}, "YTT_PUBLIC_URL is required"),
            ({"public_url": "has space"}, "YTT_PUBLIC_URL contains whitespace"),
            ({"path_prefix": "ytt"}, "YTT_PATH_PREFIX must end with"),
            ({"rate_limit_per_min": -1}, "must be >= 0"),
            (
                {"oidc_issuer": "http://idp.example.com/realms/ytt"},
                "YTT_OIDC_ISSUER must use https://",
            ),
        ],
        ids=[
            "invariant7",
            "burst-contradiction",
            "public-url-missing",
            "public-url-whitespace",
            "path-prefix",
            "negative-limit",
            "oidc-issuer-scheme",
        ],
    )
    def test_validation_error_is_secret_free(self, fault: dict, message_fragment: str):
        """The rendered ValidationError names the faulty variable and keeps
        its operator-facing message, but carries neither OAuth secret nor
        Whisper-URL credential — in str, repr, or any error's echoed input."""
        with pytest.raises(Exception) as exc_info:
            Settings(**self._poisoned(**fault))

        rendered = f"{exc_info.value}\n{exc_info.value!r}"
        assert message_fragment in rendered, (
            f"operator-facing message lost:\n{rendered[:2000]}"
        )
        _assert_secret_free(rendered, "Settings validation error")

        for err in exc_info.value.errors():
            _assert_secret_free(
                json.dumps(err, default=str), "pydantic error detail echo"
            )

    def test_get_settings_cached_path_unchanged(self, monkeypatch):
        """get_settings() construction (the serve() path) gets the same
        redaction for free — it is a plain Settings() call."""
        from ytt.config import get_settings

        monkeypatch.setenv("YTT_PUBLIC_URL", _VALID_PUBLIC_URL)
        monkeypatch.setenv("YTT_OAUTH_CLIENT_SECRET", _CANARY_CLIENT_SECRET)
        monkeypatch.setenv("YTT_WHISPER_URL", _CANARY_WHISPER_URL)
        monkeypatch.setenv("YTT_MAX_ASR_DURATION_SEC", "1200")
        monkeypatch.setenv("YTT_WHISPER_TIMEOUT_SEC", "100")
        get_settings.cache_clear()
        try:
            with pytest.raises(Exception) as exc_info:
                get_settings()
            _assert_secret_free(str(exc_info.value), "get_settings validation error")
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. Logs (the real structlog pipeline, JSON renderer)
# ---------------------------------------------------------------------------


def _log_events(capsys) -> list[dict]:
    """Parse every JSON log line printed since the last readouterr()."""
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


@pytest.fixture()
def json_logging():
    """The production pipeline (JSON renderer, redaction last)."""
    configure_logging()
    yield


class TestLogRedaction:
    """The configured structlog pipeline keeps both credential classes out of
    every rendered event — including exception tracebacks."""

    def test_startup_event_with_credentialed_whisper_url(self, json_logging, capfd):
        """The ``Server startup`` event shape serve() logs carries
        ``whisper_url`` verbatim — userinfo must be stripped while host:port
        stays for diagnosability."""
        log = get_logger("test.redaction")
        log.info(
            "Server startup",
            public_url=_VALID_PUBLIC_URL,
            whisper_url=_CANARY_WHISPER_URL,
            whisper_model="large-v3-turbo",
        )
        (event,) = _log_events(capfd)
        _assert_secret_free(json.dumps(event), "startup log event")
        assert event["whisper_url"] == f"http://{_CANARY_WHISPER_HOSTPORT}"

    @pytest.mark.parametrize(
        "field",
        ["client_secret", "oauth_client_secret", "jwt_signing_secret", "secret"],
    )
    def test_secret_named_fields_render_redacted(
        self, json_logging, capfd, field: str
    ):
        """A plain secret string matches no URL pattern — only the field-name
        rule can stop it. The OAuth secret's plausible log-argument names are
        all blocked."""
        log = get_logger("test.redaction")
        log.info("debug dump", **{field: _CANARY_CLIENT_SECRET})
        (event,) = _log_events(capfd)
        assert event[field] == "<redacted>"

    def test_exception_traceback_is_sanitized(self, json_logging, capfd):
        """``log.exception`` renders the formatted traceback into the event —
        its exception line can quote the credentialed ``YTT_WHISPER_URL``
        verbatim, so the pipeline must sanitize the rendered ``exception``
        field too (redaction runs after format_exc_info)."""
        log = get_logger("test.redaction")
        try:
            raise ValueError(
                f"whisper dial failed for {_CANARY_WHISPER_URL}/v1/audio/transcriptions"
            )
        except ValueError:
            log.exception("whisper_job_unexpected_error", video_id="leakprobe01")
        (event,) = _log_events(capfd)
        rendered = json.dumps(event)
        _assert_secret_free(rendered, "exception traceback log event")
        assert "exception" in event
        assert _CANARY_WHISPER_HOSTPORT in event["exception"]


# ---------------------------------------------------------------------------
# 3. Transcript-job failures → verbatim-relayable job message
# ---------------------------------------------------------------------------

_VIDEO_ID = "leakprobe01"


def _whisper_settings(scratch: Path) -> MagicMock:
    """Job-drive settings with the credentialed ``YTT_WHISPER_URL``."""
    s = MagicMock()
    s.scratch_dir = str(scratch)
    s.whisper_url = _CANARY_WHISPER_URL
    s.whisper_timeout_sec = 100
    s.max_asr_duration_sec = 1200
    s.whisper_realtime_factor = 1.2
    s.max_audio_bytes = 500 * 1024 * 1024
    s.proxy_url = None
    return s


def _url_quoting_message() -> str:
    return (
        f"dial {_CANARY_WHISPER_URL}/v1/audio/transcriptions failed: "
        "connection refused"
    )


def _status_error() -> httpx.HTTPStatusError:
    """HTTPStatusError whose str() embeds the full credentialed request URL
    (verified against httpx 0.28) with a benign upstream body."""
    return httpx.HTTPStatusError(
        "Server error '500 Internal Server Error' for url "
        f"'{_CANARY_WHISPER_URL}/v1/audio/transcriptions'",
        request=httpx.Request("POST", _CANARY_WHISPER_URL),
        response=httpx.Response(500, text="upstream exploded"),
    )


async def _drive_job_to_error(
    post_side_effect: Any, tmp_path: Path
) -> WhisperJob:
    """Run one real run_whisper_job with a poisoned whisper URL and
    *post_side_effect* as the transcription POST; return the terminal job."""
    registry = WhisperJobRegistry()
    settings = _whisper_settings(tmp_path)
    job, _ = await registry.get_or_create(_VIDEO_ID, 50.0, settings, owner="anonymous")

    audio_file = Path(settings.scratch_dir) / f"{_VIDEO_ID}.mp3"
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.write_bytes(b"fake audio")

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=post_side_effect)

    with patch("ytt.whisper._do_download_audio", return_value=str(audio_file)):
        await run_whisper_job(
            job, registry, settings, AsyncMock(), "large-v3-turbo",
            http_client=client,
        )

    final = await registry.get(_VIDEO_ID)
    assert final is not None and final.status == "error"
    return final


# httpx.InvalidURL is a plain ValueError (NOT a RequestError), so it reaches
# the job's catch-all — and URL-parse failures quote the offending input.
def _invalid_url_error() -> httpx.InvalidURL:
    return httpx.InvalidURL(
        f"Invalid URL '{_CANARY_WHISPER_URL}/v1/audio/transcriptions'"
    )


_FAILURE_SHAPES: list[tuple[str, Callable[[], Exception]]] = [
    # httpx RequestError whose str quotes the credentialed URL (httpx's own
    # message shape could grow this any release).
    ("request-error", lambda: httpx.ConnectError(_url_quoting_message())),
    # Timeout handler quoting the URL.
    ("timeout-error", lambda: httpx.ConnectTimeout(_url_quoting_message())),
    # The status handler quotes only the response body, never str(exc) — but
    # the redaction boundary must hold regardless of handler internals.
    ("http-status-error", _status_error),
    ("invalid-url-catch-all", _invalid_url_error),
    # Any other exception the catch-all sees.
    ("generic-catch-all", lambda: ValueError(f"unexpected dial {_CANARY_WHISPER_URL}")),
]


class TestTranscriptJobFailureMessages:
    """Every path that writes a WhisperJob failure message redacts the
    credential-bearing URL first — the message is verbatim-relayable."""

    @pytest.mark.parametrize("case,raiser", _FAILURE_SHAPES)
    async def test_job_message_is_secret_free(
        self, case: str, raiser, tmp_path: Path
    ):
        job = await _drive_job_to_error(raiser(), tmp_path)
        assert job.error_code == errors.ASR_FAILED
        _assert_secret_free(job.message or "", f"job message ({case})")

    async def test_status_error_keeps_response_shape(self, tmp_path: Path):
        """The HTTPStatusError handler keeps its diagnosable shape (status
        code + upstream body) — redaction must not blank the message."""
        job = await _drive_job_to_error(_status_error(), tmp_path)
        assert "500" in (job.message or "")
        assert "upstream exploded" in (job.message or "")


class TestTranscriptJobToolResponse:
    """End to end: a failed job polled through the real ``get_transcript_job``
    tool — the exception-response surface MCP clients actually see."""

    async def test_poll_response_is_secret_free(self, tmp_path: Path, monkeypatch):
        import ytt.server

        # Business-logic surface, not auth (same posture as test_server.py's
        # _bypass_authz fixture): direct call_tool has no HTTP request context.
        from fastmcp.server.middleware.authorization import AuthMiddleware

        for mw in ytt.server.mcp.middleware:
            if isinstance(mw, AuthMiddleware):
                monkeypatch.setattr(mw, "auth", lambda ctx: True)

        registry = ytt.server.whisper_registry
        settings = _whisper_settings(tmp_path)
        job, _ = await registry.get_or_create(_VIDEO_ID, 50.0, settings, owner="anonymous")

        audio_file = Path(settings.scratch_dir) / f"{_VIDEO_ID}.mp3"
        audio_file.parent.mkdir(parents=True, exist_ok=True)
        audio_file.write_bytes(b"fake audio")

        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=ValueError(_url_quoting_message()))

        try:
            with patch(
                "ytt.whisper._do_download_audio", return_value=str(audio_file)
            ):
                await run_whisper_job(
                    job, registry, settings, AsyncMock(), "large-v3-turbo",
                    http_client=client,
                )

            result = await ytt.server.mcp.call_tool(
                "get_transcript_job", {"video_id": _VIDEO_ID}
            )
            assert result.structured_content["status"] == "error"
            _assert_secret_free(
                json.dumps(result.structured_content, default=str),
                "get_transcript_job response",
            )
        finally:
            await registry.remove(_VIDEO_ID)


# ---------------------------------------------------------------------------
# 4. Metrics
# ---------------------------------------------------------------------------


class TestMetricsStaySecretFree:
    """Error paths export static taxonomy labels only — the Prometheus
    exposition never carries a credential value or a config-derived label."""

    @pytest.mark.parametrize("case,raiser", _FAILURE_SHAPES)
    async def test_exposition_after_failed_job_is_secret_free(
        self, case: str, raiser, tmp_path: Path
    ):
        from prometheus_client import REGISTRY, generate_latest

        await _drive_job_to_error(raiser(), tmp_path)
        exposition = generate_latest(REGISTRY).decode()
        _assert_secret_free(exposition, f"metrics exposition ({case})")
        # Not just the credential — the whisper host itself must never become
        # a label value (a config value in a label is a cardinality leak even
        # when the userinfo was stripped).
        assert _CANARY_WHISPER_HOSTPORT not in exposition, (
            f"metrics exposition ({case}): config-derived label value leaked"
        )


# ---------------------------------------------------------------------------
# 5. OAuth failure surfaces
# ---------------------------------------------------------------------------


def _verifier() -> UpstreamIdTokenVerifier:
    """The real verifier keyed by the canary secret (the build_auth_provider
    wiring, minus the provider)."""
    settings = Settings(
        public_url=_VALID_PUBLIC_URL,
        oauth_client_id=_CANARY_CLIENT_ID,
        oauth_client_secret=_CANARY_CLIENT_SECRET,
    )
    return UpstreamIdTokenVerifier(
        public_key=settings.oauth_client_secret,
        algorithm="HS256",
        issuer=settings.oidc_issuer,
        audience=settings.oauth_client_id,
    )


def _id_token(key: str, **claim_overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": DEFAULT_OIDC_ISSUER,
        "aud": _CANARY_CLIENT_ID,
        "sub": "redaction-probe-user",
        "email": "redaction-probe-user@example.com",
        "exp": now + 300,
        "iat": now,
        "nbf": now,
    }
    claims.update(claim_overrides)
    # An explicit None drop is how a case removes a claim entirely
    # (no-exp-claim); pyjwt would otherwise embed a null claim.
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, key, algorithm="HS256")


class TestOAuthFailureSurfaces:
    """Every upstream id-token rejection returns None and never surfaces the
    verifying key (the ``YTT_OAUTH_CLIENT_SECRET`` value) — in the return
    value, an exception, or a log line (structlog's stdout and fastmcp's
    stderr both captured)."""

    async def test_control_canary_key_verifies_a_real_token(self):
        """Control: a token signed WITH the canary secret verifies — the
        canary is the live key, so the rejections below are meaningful."""
        token = _id_token(_CANARY_CLIENT_SECRET)
        assert await _verifier().load_access_token(token) is not None

    @pytest.mark.parametrize(
        "case,token_factory",
        [
            (
                "wrong-signing-key",
                lambda: _id_token("some-other-idp-key-0123456789abcdef"),
            ),
            (
                "expired",
                lambda: _id_token(_CANARY_CLIENT_SECRET, exp=int(time.time()) - 10),
            ),
            ("no-exp-claim", lambda: _id_token(_CANARY_CLIENT_SECRET, exp=None)),
            (
                "nbf-in-future",
                lambda: _id_token(_CANARY_CLIENT_SECRET, nbf=int(time.time()) + 3600),
            ),
            ("garbage-token", lambda: "not-a-jwt"),
        ],
    )
    async def test_rejection_is_secret_free(
        self, json_logging, capfd, case: str, token_factory
    ):
        verifier = _verifier()
        assert await verifier.load_access_token(token_factory()) is None
        captured = capfd.readouterr()
        _assert_secret_free(captured.out, f"verifier rejection stdout ({case})")
        _assert_secret_free(captured.err, f"verifier rejection stderr ({case})")

    def test_provider_construction_logs_nothing_secret(self, json_logging, capfd):
        """build_auth_provider with the canary pair constructs the real
        provider and its construction path emits no credential value."""
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        provider = build_auth_provider(
            Settings(
                public_url=_VALID_PUBLIC_URL,
                oauth_client_id=_CANARY_CLIENT_ID,
                oauth_client_secret=_CANARY_CLIENT_SECRET,
                jwt_signing_secret=_CANARY_JWT_SIGNING_KEY,
            )
        )
        assert isinstance(provider, OIDCProxy)
        captured = capfd.readouterr()
        _assert_secret_free(captured.out, "provider construction stdout")
        _assert_secret_free(captured.err, "provider construction stderr")
