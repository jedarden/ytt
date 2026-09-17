"""Configuration loader + startup validations (plan: Configuration table).

A pydantic-settings ``Settings`` model reading the ``YTT_*`` environment with the
exact names and defaults from the plan's Configuration table. Beyond plain
parsing it enforces:

- **Invariant 7** (ETA-timeout safety): ``MAX_ASR_DURATION_SEC × RT_FACTOR <
  WHISPER_TIMEOUT_SEC`` — validated at construction (raises -> server exits 1).
- ``YTT_PATH_PREFIX`` must end with ``/`` (raises if missing).
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
- Storage sizing: for ``pvc`` backend, ``statvfs(cache_dir)`` must be >=
  ``cache_max_bytes`` (fail fast); for ``emptydir`` a warning is emitted instead
  (statvfs reports node disk, not the kubelet ``sizeLimit``). This filesystem
  check runs at startup via :meth:`Settings.validate_storage`, not at import.

Human-readable sizes (``2Gi``, ``500Mi``) are accepted everywhere a byte count
is expected.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BeforeValidator, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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


def join_path(prefix: str, route: str) -> str:
    """Join a path prefix with a route segment, collapsing the boundary slash.

    ``join_path("/ytt/", "/health") == "/ytt/health"`` (plan: Path construction
    rule — always strip one leading slash from the route segment).
    """
    return prefix + route.lstrip("/")


class Settings(BaseSettings):
    """Runtime configuration (plan: Configuration table). Env prefix ``YTT_``."""

    model_config = SettingsConfigDict(
        env_prefix="YTT_",
        env_file=None,
        extra="ignore",
        validate_default=True,
    )

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
    public_url: str = "https://mcp.ardenone.com/ytt"

    # --- OAuth (Google, from ESO/OpenBao) ---
    # Optional on the Settings model itself so unit tests can construct freely;
    # ytt.auth.build_auth_provider raises at startup if oauth_client_id is
    # unset (see docs/notes/auth.md) rather than falling back to no auth.
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    jwt_signing_secret: str | None = None

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
    def _public_url_no_trailing_slash(cls, v: str) -> str:
        # The audience/resource/issuer must be byte-identical with NO trailing
        # slash (RFC 8707 confused-deputy guard). Normalize defensively.
        return v.rstrip("/")

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
