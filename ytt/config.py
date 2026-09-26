"""Configuration loader + startup validations (plan: Configuration table).

A pydantic-settings ``Settings`` model reading the ``YTT_*`` environment with the
exact names and defaults from the plan's Configuration table. Beyond plain
parsing it enforces:

- **Invariant 7** (ETA-timeout safety): ``MAX_ASR_DURATION_SEC × RT_FACTOR <
  WHISPER_TIMEOUT_SEC`` — validated at construction (raises -> server exits 1).
- ``YTT_PATH_PREFIX`` must end with ``/`` (raises if missing).
- ``YTT_PUBLIC_URL`` is required with **no fallback**: the OAuth
  audience/resource/issuer and every emitted RFC 9728 metadata document
  derive from it byte-for-byte, so an unset or empty value raises at
  construction (server exits 1) instead of silently targeting the reference
  deployment the retired baked-in default pointed at. Whitespace-carrying,
  non-http(s), hostname-less, or query/fragment-bearing values are equally
  startup errors (same fail-closed posture as ``YTT_PROXY_URL``); a trailing
  slash is normalized away (RFC 8707 byte-exactness).
- ``YTT_PROXY_URL`` must be a well-formed http(s) proxy URL or unset; an
  empty string is an error, not a silent unset (fail-closed — a manifest
  interpolating a missing secret must not quietly disable the proxy). See
  :func:`Settings._proxy_url_valid` and ``docs/notes/proxy-egress.md``.
- Per-subject limits (``RATE_LIMIT_PER_MIN``, ``RATE_LIMIT_BURST``,
  ``WHISPER_JOBS_PER_HOUR``) must be >= 0: 0 is meaningful (deny-all —
  fail-closed), negatives are config errors. Unset ``RATE_LIMIT_BURST``
  resolves to ``RATE_LIMIT_PER_MIN`` (one full minute of requests up front).
- Malformed limit values (non-integer env strings: ``abc``, ``2.5``, empty)
  are config errors — pydantic rejects them at construction and the server
  exits before binding. There is no lenient fallback and no "unlimited"
  escape value.
- ``RATE_LIMIT_PER_MIN=0`` with an *explicit* positive ``RATE_LIMIT_BURST``
  is rejected: 0 is documented deny-all (no refill), so a one-shot burst
  allowance would silently contradict it. An explicit ``RATE_LIMIT_BURST=0``
  is valid with any rate — capacity 0 denies every fetch (fail-closed).
- ``YTT_OIDC_ISSUER`` / ``YTT_OIDC_CONFIG_URL`` must be well-formed https
  URLs (hostname required; whitespace, query, and fragment rejected — the
  same fail-closed posture as ``YTT_PROXY_URL``, since the issuer is matched
  byte-for-byte against the upstream id token's ``iss`` claim and a typo must
  fail startup, not every login). An unset ``YTT_OIDC_CONFIG_URL`` is derived
  from the issuer per OIDC Discovery §4; the issuer itself is never
  normalized (see :func:`_require_https_url` and
  :meth:`Settings._derive_oidc_config_url`).
- Storage sizing: for ``pvc`` backend, ``statvfs(cache_dir)`` must be >=
  ``cache_max_bytes`` (fail fast); for ``emptydir`` a warning is emitted instead
  (statvfs reports node disk, not the kubelet ``sizeLimit``). This filesystem
  check runs at startup via :meth:`Settings.validate_storage`, not at import.
- **No credential in a validation error**: pydantic echoes the offending
  input in every rendered validation error — for a model-level (``mode=
  "after"``) failure that is the whole input mapping, ``YTT_OAUTH_CLIENT_SECRET``
  included. Construction wraps that echo with secret-name and credential-URL
  redaction (:func:`_redacted_validation_error`) — the same value-never-logs
  posture ``ytt.observability`` enforces at runtime, since the rendered
  ``ValidationError`` is what a CrashLooping pod prints.

Human-readable sizes (``2Gi``, ``500Mi``) are accepted everywhere a byte count
is expected.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BeforeValidator, ValidationError, field_validator, model_validator
from pydantic_core import InitErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict

from ytt.observability import redact_credentials

# --- human-readable size parsing -------------------------------------------

_SIZE_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 10**3,
    "m": 10**6,
    "g": 10**9,
    "t": 10**12,
    "p": 10**15,
    "ki": 2**10,
    "mi": 2**20,
    "gi": 2**30,
    "ti": 2**40,
    "pi": 2**50,
}

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]i?|b)?\s*$", re.IGNORECASE)


def parse_size(value: object) -> int:
    """Parse a human-readable size (``2Gi``, ``500Mi``, ``1048576``, ``1.5g``) to bytes.

    Binary suffixes (``Ki``/``Mi``/``Gi``/``Ti``/``Pi``) are powers of 1024;
    decimal suffixes (``K``/``M``/``G``/``T``/``P``) are powers of 1000; a bare
    number (or ``B``) is bytes. Matches Kubernetes resource-quantity conventions.
    """
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise ValueError(f"invalid size: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    m = _SIZE_RE.match(s)
    if not m:
        raise ValueError(f"invalid size: {value!r}")
    num, unit = m.group(1), (m.group(2) or "").lower()
    if unit == "b":
        unit = ""
    return int(float(num) * _SIZE_UNITS[unit])


Bytes = Annotated[int, BeforeValidator(parse_size)]

#: Schemes accepted for ``YTT_PROXY_URL``. Only ``http``/``https``: yt-dlp would
#: also dial ``socks4/4a/5/5h`` (it bundles a native SOCKS client), but the
#: httpx-based egress probe (:func:`ytt.selftest.probe_egress` — startup,
#: ``/admin/egress``, canary) has no SOCKS adapter in this image, so a SOCKS
#: URL would half-work and half-break. Fail fast at startup instead; most
#: residential proxy providers (Webshare et al.) serve plain HTTP endpoints.
_ALLOWED_PROXY_SCHEMES: frozenset[str] = frozenset({"http", "https"})


# --- upstream OIDC IdP (reference-deployment defaults) -----------------------

#: Issuer of the reference upstream IdP — the org's self-hosted Authentik
#: application for ytt. Baked in as the ``YTT_OIDC_ISSUER`` default so the
#: reference deployment needs no extra env vars; any other deployment points
#: ytt at its own IdP by setting the variable. Compared byte-for-byte against
#: the id token's ``iss`` claim, so it is validated but never normalized
#: (Authentik per-application issuers end with ``/``, Keycloak realm issuers
#: do not — stripping either side breaks token verification).
DEFAULT_OIDC_ISSUER = "https://sso.ardenone.com/application/o/ytt/"

#: The ``YTT_OIDC_CONFIG_URL`` that matches ``DEFAULT_OIDC_ISSUER`` — the
#: OIDC Discovery §4 issuer-relative location of the discovery document,
#: spelled out as a constant so the reference defaults are pinned
#: byte-for-byte and a test can hold the derivation honest.
DEFAULT_OIDC_CONFIG_URL = (
    DEFAULT_OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
)


def _require_https_url(env_name: str, value: str) -> None:
    """Validate an https URL setting in the ``YTT_PROXY_URL`` style (fail fast).

    - empty/whitespace is an error, not a silent default — a manifest
      interpolating a missing value must fail startup, not quietly point ytt
      back at the reference IdP (same fail-closed posture as ``YTT_PROXY_URL``);
    - whitespace anywhere is rejected (copy-paste line wraps are the classic
      way a URL gets split in a manifest);
    - scheme must be https (OIDC Core §3.1.2.1 requires https issuers; ytt is
      a deployed server, not a localhost dev tool);
    - a hostname must be present;
    - query and fragment components are rejected — the issuer is an opaque
      byte-exact comparison value, and an ``iss`` claim never carries either.
    """
    if not value.strip():
        raise ValueError(
            f"{env_name} is empty — unset the variable entirely for the "
            "reference-IdP default; if set, it must be a real URL "
            "(e.g. https://idp.example.com/realms/ytt)"
        )
    if re.search(r"\s", value):
        raise ValueError(
            f"{env_name} contains whitespace: {value!r} — an OIDC URL is a "
            "single token (scheme://host/path); check for a copy-paste line wrap"
        )
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https":
        raise ValueError(
            f"{env_name} must use https:// (OIDC Core §3.1.2.1 requires an "
            f"https issuer) — got scheme {parsed.scheme!r} in {value!r}"
        )
    if not parsed.hostname:
        raise ValueError(
            f"{env_name} has no hostname: {value!r} "
            "(expected e.g. https://idp.example.com/realms/ytt)"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            f"{env_name} must not carry a query or fragment: {value!r} — the "
            "issuer is matched byte-for-byte against the id token's iss claim"
        )


def join_path(prefix: str, route: str) -> str:
    """Join a path prefix with a route segment, collapsing the boundary slash.

    ``join_path("/ytt/", "/health") == "/ytt/health"`` (plan: Path construction
    rule — always strip one leading slash from the route segment).
    """
    return prefix + route.lstrip("/")


# --- secret hygiene on construction errors -----------------------------------

#: Substrings that mark an input field as a credential: its value is replaced
#: with ``<redacted>`` everywhere pydantic would echo it in a validation error.
#: Case-insensitive, matched against the field name.
_SECRET_NAME_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passphrase",
    "api_key",
    "signing_key",
)

#: Placeholder pydantic renders instead of a redacted credential value.
_REDACTED_INPUT = "<redacted>"


def _is_secret_field(name: str) -> bool:
    """True when *name* (a field or dict key) identifies a credential value."""
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_NAME_MARKERS)


def _redact_error_input(value: object, loc: tuple[int | str, ...] = ()) -> object:
    """Sanitize one ``input_value`` pydantic would echo in a validation error.

    - a value under a secret-named key/field becomes ``<redacted>``;
    - any string is passed through :func:`ytt.observability.redact_credentials`
      so a credential-bearing URL (``user:pass@host`` — a ``YTT_WHISPER_URL``
      with embedded basic-auth, a ``YTT_PROXY_URL``) survives only as
      ``scheme://host:port``;
    - dicts and lists are sanitized recursively; anything else is inert.
    """
    if isinstance(value, dict):
        return {
            key: _REDACTED_INPUT
            if isinstance(key, str) and _is_secret_field(key)
            else _redact_error_input(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_error_input(item) for item in value]
    if isinstance(value, str):
        if loc and isinstance(loc[-1], str) and _is_secret_field(loc[-1]):
            return _REDACTED_INPUT
        return redact_credentials(value)
    return value


def _redacted_validation_error(exc: ValidationError) -> ValidationError:
    """Rebuild *exc* with every echoed ``input_value`` sanitized.

    ``pydantic`` renders each validation error with the offending input — for
    a ``mode="after"`` **model** validator failure that is the whole input
    mapping, secrets included (``ValidationError`` is the exception a
    CrashLooping pod prints at startup, i.e. exactly the surface plan
    §Observability forbids leaking credentials onto). The rebuild keeps the
    exception type, title, per-field locations, messages (so operators still
    see which variable is at fault) and ``ctx`` — only the echoed values are
    sanitized via :func:`_redact_error_input`.
    """
    line_errors: list[InitErrorDetails] = []
    for err in exc.errors(include_url=False):
        loc: tuple[int | str, ...] = err.get("loc", ())
        detail: InitErrorDetails = {
            "type": err["type"],
            "loc": loc,
            "input": _redact_error_input(err.get("input"), loc),
        }
        ctx = err.get("ctx")
        if ctx is not None:
            detail["ctx"] = ctx  # type: ignore[typeddict-item]
        line_errors.append(detail)
    return ValidationError.from_exception_data(exc.title, line_errors)


class Settings(BaseSettings):
    """Runtime configuration (plan: Configuration table). Env prefix ``YTT_``."""

    model_config = SettingsConfigDict(
        env_prefix="YTT_",
        env_file=None,
        extra="ignore",
        validate_default=True,
    )

    def __init__(self, **data: object) -> None:
        """Construct with credential-safe validation errors.

        Wraps pydantic's construction so a failure never echoes a credential
        value: ``OAUTH_CLIENT_SECRET`` / ``JWT_SIGNING_SECRET`` (and any
        secret-named input) render as ``<redacted>`` and credential-bearing
        URL values lose their userinfo — see :func:`_redacted_validation_error`.
        The raised exception is still a ``ValidationError`` carrying the same
        locations and messages, so callers matching on either are unaffected.
        """
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _redacted_validation_error(exc) from None

    # --- authz / rate limits ---
    # Comma-separated Google account emails (the "sub" is the Google-verified
    # email — see ytt.auth); empty == deny all (fail-closed).
    allowed_subjects: str = ""
    # Per-subject request rate (token-bucket refill, docs/notes/auth.md).
    # Charged only on the fetch path — cache hits and get_transcript_job
    # polls cost nothing. 0 = deny all fetches for every subject (fail-closed).
    rate_limit_per_min: int = 20
    # Per-subject burst capacity (bucket size). None resolves to
    # rate_limit_per_min (burst == one minute's worth of requests), so
    # YTT_RATE_LIMIT_PER_MIN=0 with no explicit burst denies everything.
    # An explicit 0 is valid with any rate (capacity 0 = deny all); an
    # explicit positive burst under a 0 rate is a config error — the
    # resolution validator rejects it (0 is documented deny-all).
    rate_limit_burst: int | None = None
    # Per-subject Whisper ASR jobs per rolling hour. Charged only when a NEW
    # job starts (joining/polling an existing job is free). 0 = deny all ASR
    # (caption fetches still work) — fail-closed.
    whisper_jobs_per_hour: int = 10

    # --- cache ---
    cache_dir: str = "/cache"
    cache_backend: Literal["pvc", "emptydir"] = "pvc"
    cache_max_bytes: Bytes = "2Gi"  # type: ignore[assignment]
    cache_reconcile_sec: int = 300

    # --- scratch / audio ---
    scratch_dir: str = "/scratch"
    max_audio_bytes: Bytes = "500Mi"  # type: ignore[assignment]

    # --- concurrency ---
    max_concurrent_fetches: int = 4
    max_concurrent_whisper: int = 1
    extract_timeout_sec: int = 60

    # --- whisper ---
    # Model: large-v3-turbo (only model available on whisper-openai service)
    # RT_FACTOR calibrated for CPU: 2.0 (large-v3-turbo is slower than small)
    # Phase 9 will re-calibrate based on actual transcription measurements
    whisper_url: str = "http://whisper-openai.whisper-stt.svc.cluster.local:8000"
    whisper_model: str = "large-v3-turbo"
    whisper_realtime_factor: float = 2.0
    whisper_timeout_sec: int = 2880
    max_asr_duration_sec: int = 1200
    # Global cap on Whisper jobs that are pending (queued behind the
    # max_concurrent_whisper semaphore) or running. Bounds the total queued
    # ASR work one misbehaving (but allowlisted) caller — or a fleet of them —
    # can pile up: the per-subject quota caps each subject's *rate*, this caps
    # the system's *backlog*. A job already in flight can always be joined;
    # the cap only denies *new* jobs (stable error: rate_limited, "queue
    # full"). 0 = deny every new ASR job (fail-closed, same convention as the
    # per-subject limits above).
    max_pending_whisper_jobs: int = 16
    job_ttl_sec: int = 3600

    # --- canary ---
    canary_interval_sec: int = 600

    # --- response shape ---
    inline_char_limit: int = 18000
    chunk_chars: int = 18000

    # --- egress / ingress ---
    proxy_url: str | None = None
    path_prefix: str = "/ytt/"
    # Required — no fallback. The empty-string default is a sentinel:
    # validate_default=True routes it through the field validator, which
    # raises, so construction fails whenever the env var is unset. The OAuth
    # audience/resource/issuer and all emitted RFC 9728 metadata derive from
    # this value byte-for-byte — a baked-in default would silently point a
    # self-hoster's OAuth metadata at the reference deployment
    # (https://mcp.ardenone.com/ytt), so there is deliberately none.
    public_url: str = ""

    # --- OAuth (upstream OIDC IdP, from ESO/OpenBao) ---
    # Optional on the Settings model itself so unit tests can construct freely;
    # ytt.auth.build_auth_provider raises at startup if oauth_client_id is
    # unset (see docs/notes/auth.md) rather than falling back to no auth.
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    jwt_signing_secret: str | None = None
    # Upstream IdP location: the reference Authentik by default, any generic
    # OIDC provider via env (docs/usage/self-hosting.md). The issuer is
    # matched byte-for-byte against the id token's iss claim, so it is
    # validated but never normalized — see _require_https_url.
    oidc_issuer: str = DEFAULT_OIDC_ISSUER
    # Upstream OIDC discovery document. Unset (absent from env/init, or an
    # explicit None) is resolved by _derive_oidc_config_url below to
    # <issuer>/.well-known/openid-configuration — the before-model-validator
    # runs ahead of field validation, so the model type is plain str after
    # construction and ytt.auth needs no None handling.
    oidc_config_url: str = DEFAULT_OIDC_CONFIG_URL

    # ------------------------------------------------------------------ #
    @field_validator(
        "rate_limit_per_min",
        "rate_limit_burst",
        "whisper_jobs_per_hour",
        "max_pending_whisper_jobs",
    )
    @classmethod
    def _rate_limits_non_negative(cls, v: int | None, info) -> int | None:
        """Negative limits are config errors (fail fast); 0 is meaningful —
        it denies everything the limit guards (fail-closed)."""
        if v is not None and v < 0:
            raise ValueError(
                f"YTT_{info.field_name.upper()} must be >= 0 (0 = deny all), "
                f"got {v}"
            )
        return v

    @field_validator("path_prefix")
    @classmethod
    def _path_prefix_trailing_slash(cls, v: str) -> str:
        if not v.endswith("/"):
            raise ValueError(
                f"YTT_PATH_PREFIX must end with '/': got {v!r} "
                "(e.g. '/ytt/'); fix the env var and restart"
            )
        if not v.startswith("/"):
            raise ValueError(f"YTT_PATH_PREFIX must start with '/': got {v!r}")
        return v

    @field_validator("public_url")
    @classmethod
    def _public_url_required_and_well_formed(cls, v: str) -> str:
        """``YTT_PUBLIC_URL`` is required — there is deliberately no fallback.

        The audience/resource/issuer and every emitted RFC 9728 metadata
        document derive from this value byte-for-byte (RFC 8707
        confused-deputy guard), so a missing or malformed value must fail
        startup, never silently target the reference deployment the retired
        baked-in default pointed at (``https://mcp.ardenone.com/ytt``).
        Same fail-closed posture as ``YTT_PROXY_URL``:

        - empty is the unset/missing case — required, not defaulted (an env
          line interpolating an unset manifest value must fail startup, not
          fall back);
        - whitespace anywhere is rejected (copy-paste line wraps);
        - scheme must be http or https and a hostname must be present — http
          stays valid for localhost/dev/smoke boots, while Anthropic's
          connector backend requires https in production;
        - query and fragment are rejected — the audience is a byte-exact
          comparison value and metadata URLs are path-shaped;
        - a trailing slash is normalized away (kept from the original
          validator — the audience must never carry one).
        """
        if not v.strip():
            raise ValueError(
                "YTT_PUBLIC_URL is required — set it to the URL clients use "
                "to reach this server (e.g. "
                "https://your-domain.example.com/ytt). There is no fallback: "
                "the OAuth audience and the emitted "
                "oauth-protected-resource/authorization-server metadata "
                "derive from this value, and a default would silently "
                "target the reference deployment."
            )
        if re.search(r"\s", v):
            raise ValueError(
                f"YTT_PUBLIC_URL contains whitespace: {v!r} — a URL is a "
                "single token (scheme://host/path); check for a copy-paste "
                "line wrap"
            )
        parsed = urlparse(v)
        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError(
                f"YTT_PUBLIC_URL must use http:// or https:// — got scheme "
                f"{parsed.scheme!r} in {v!r}. https in production "
                "(Anthropic's connector backend requires it); http is for "
                "localhost/dev boots."
            )
        if not parsed.hostname:
            raise ValueError(
                f"YTT_PUBLIC_URL has no hostname: {v!r} "
                "(expected e.g. https://your-domain.example.com/ytt)"
            )
        if parsed.query or parsed.fragment:
            raise ValueError(
                f"YTT_PUBLIC_URL must not carry a query or fragment: {v!r} "
                "— it is the byte-exact OAuth audience/resource/issuer"
            )
        # The audience/resource/issuer must be byte-identical with NO trailing
        # slash (RFC 8707 confused-deputy guard). Normalize defensively.
        return v.rstrip("/")

    @field_validator("oidc_issuer")
    @classmethod
    def _oidc_issuer_valid(cls, v: str) -> str:
        """``YTT_OIDC_ISSUER`` must be a well-formed https issuer URL.

        Fail fast at startup (same posture as ``YTT_PROXY_URL``): the issuer
        is compared byte-for-byte against the upstream id token's ``iss``
        claim by ``ytt.auth``'s JWTVerifier, so a typo'd value would sit
        unexercised until the first login and then fail every token
        verification with an opaque ``invalid_token``. Never normalized —
        see :func:`_require_https_url`.
        """
        _require_https_url("YTT_OIDC_ISSUER", v)
        return v

    @field_validator("oidc_config_url")
    @classmethod
    def _oidc_config_url_valid(cls, v: str) -> str:
        """``YTT_OIDC_CONFIG_URL`` must be a well-formed https URL.

        The discovery fetch happens once, at provider construction (startup):
        a malformed value must fail startup, not the first login. Unset
        values never reach this validator — :meth:`_derive_oidc_config_url`
        resolves them before field validation.
        """
        _require_https_url("YTT_OIDC_CONFIG_URL", v)
        return v

    @field_validator("proxy_url")
    @classmethod
    def _proxy_url_valid(cls, v: str | None) -> str | None:
        """``YTT_PROXY_URL`` must be a well-formed http(s) proxy URL — or unset.

        Fail fast at startup: a typo'd proxy URL would otherwise sit unexercised
        until YouTube blocks the direct path, and only then fail every
        ``ip_blocked`` retry (plan §ip_blocked) with an opaque dial error.

        - unset (``None``) is valid — direct egress, the default;
        - empty/whitespace is an error, not a silent unset — an env line like
          ``YTT_PROXY_URL: "${PROXY_URL}"`` with the secret missing must fail
          startup, not quietly disable the proxy (fail-closed, same posture as
          the limit validators above);
        - whitespace anywhere is rejected (copy-paste line wraps are the
          classic way a proxy URL gets split in a manifest);
        - scheme must be http/https (see ``_ALLOWED_PROXY_SCHEMES``) and a
          hostname must be present. Credentials (``user:pass@``) are optional
          and never validated — or logged (observability redacts them).
        """
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "YTT_PROXY_URL is empty — unset the variable entirely for "
                "direct egress; if the proxy is wanted, the URL must be "
                "non-empty (e.g. http://user:pass@proxy.example.com:3128)"
            )
        if re.search(r"\s", stripped):
            raise ValueError(
                f"YTT_PROXY_URL contains whitespace: {stripped!r} — a proxy "
                "URL is a single token (scheme://[user:pass@]host:port); "
                "check for a copy-paste line wrap"
            )
        parsed = urlparse(stripped)
        if parsed.scheme.lower() not in _ALLOWED_PROXY_SCHEMES:
            raise ValueError(
                f"YTT_PROXY_URL must use http:// or https:// — got scheme "
                f"{parsed.scheme!r} in {stripped!r}. SOCKS proxies are not "
                "supported (the httpx-based egress probe has no SOCKS "
                "adapter); most residential proxy providers serve a plain "
                "HTTP endpoint."
            )
        if not parsed.hostname:
            raise ValueError(
                f"YTT_PROXY_URL has no hostname: {stripped!r} "
                "(expected e.g. http://user:pass@proxy.example.com:3128)"
            )
        return stripped

    @model_validator(mode="before")
    @classmethod
    def _derive_oidc_config_url(cls, data: object) -> object:
        """Unset ``YTT_OIDC_CONFIG_URL`` derives from ``YTT_OIDC_ISSUER``.

        OIDC Discovery §4 places the discovery document at
        ``<issuer>/.well-known/openid-configuration``; the trailing-slash
        difference is absorbed (Authentik-style issuers carry one, Keycloak
        ones do not). Runs before field validation — so it sees env-sourced
        values too — which also keeps the field type plain ``str`` after
        construction: ``ytt.auth`` passes it straight to ``OIDCProxy`` with
        no None handling. An explicit ``YTT_OIDC_CONFIG_URL`` always wins
        verbatim (only ``None``/absent derives).
        """
        if not isinstance(data, dict):
            return data
        if data.get("oidc_config_url") is None and data.get("oidc_issuer"):
            data["oidc_config_url"] = (
                str(data["oidc_issuer"]).rstrip("/")
                + "/.well-known/openid-configuration"
            )
        return data

    @model_validator(mode="after")
    def _resolve_rate_limit_burst(self) -> "Settings":
        """Unset burst defaults to the per-minute rate (one full minute of
        requests may arrive at once) — so YTT_RATE_LIMIT_PER_MIN=0 with no
        explicit burst leaves no initial allowance either (fail-closed).

        An explicit positive burst under a zero rate is rejected: 0 is
        documented as deny-all (no refill), and a one-shot allowance would
        silently contradict that promise — the unsafe combination fails
        startup instead (fail-closed)."""
        if self.rate_limit_burst is None:
            self.rate_limit_burst = self.rate_limit_per_min
        elif self.rate_limit_per_min == 0 and self.rate_limit_burst > 0:
            raise ValueError(
                "YTT_RATE_LIMIT_PER_MIN=0 means deny-all (no refill), so an "
                f"explicit YTT_RATE_LIMIT_BURST={self.rate_limit_burst} would "
                "grant a one-shot allowance that contradicts it — unset the "
                "burst or raise the rate"
            )
        return self

    @model_validator(mode="after")
    def _invariant_7_eta_timeout(self) -> "Settings":
        """Invariant 7: MAX_ASR_DURATION_SEC × RT_FACTOR < WHISPER_TIMEOUT_SEC."""
        bound = self.max_asr_duration_sec * self.whisper_realtime_factor
        if not bound < self.whisper_timeout_sec:
            raise ValueError(
                "Invariant 7 violated: MAX_ASR_DURATION_SEC "
                f"({self.max_asr_duration_sec}) × RT_FACTOR "
                f"({self.whisper_realtime_factor}) = {bound} must be < "
                f"WHISPER_TIMEOUT_SEC ({self.whisper_timeout_sec})"
            )
        return self

    # ------------------------------------------------------------------ #
    @property
    def allowed_subjects_set(self) -> frozenset[str]:
        """Parsed allowlist; empty -> deny all (fail-closed).

        Entries are lowercased for case-insensitive matching. Each entry is
        either a full subject/email (exact match) or a domain pattern beginning
        with ``@`` (e.g. ``@jedcabanero.com``) that matches any email in that
        domain — see :func:`ytt.authz.subject_allowed`.
        """
        return frozenset(
            s.strip().lower() for s in self.allowed_subjects.split(",") if s.strip()
        )

    @property
    def audience(self) -> str:
        """OAuth token audience / resource / issuer — the path-bearing public URL."""
        return self.public_url

    def route(self, segment: str) -> str:
        """Build a mounted route under the path prefix (e.g. ``route('health')``)."""
        return join_path(self.path_prefix, segment)

    def validate_storage(self) -> list[str]:
        """Filesystem-dependent startup checks (call at serve() time, not import).

        Returns a list of warning strings (emptyDir advisory). Raises ``ValueError``
        if the PVC volume is smaller than ``cache_max_bytes`` (fail fast).
        """
        warnings: list[str] = []
        if self.cache_backend == "pvc":
            try:
                st = os.statvfs(self.cache_dir)
            except OSError as e:
                raise ValueError(
                    f"cannot statvfs YTT_CACHE_DIR {self.cache_dir!r}: {e}"
                ) from e
            volume_bytes = st.f_blocks * st.f_frsize
            if self.cache_max_bytes > volume_bytes:
                raise ValueError(
                    f"YTT_CACHE_MAX_BYTES ({self.cache_max_bytes}) exceeds PVC "
                    f"volume size ({volume_bytes}) at {self.cache_dir!r}"
                )
        else:  # emptydir
            warnings.append(
                f"emptyDir: ensure YTT_CACHE_MAX_BYTES ({self.cache_max_bytes}) "
                "<= manifest sizeLimit; no automatic enforcement possible from the app."
            )
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings (read once at startup)."""
    return Settings()
