"""Configuration loader + startup validations (plan: Configuration table).

A pydantic-settings ``Settings`` model reading the ``YTT_*`` environment with the
exact names and defaults from the plan's Configuration table. Beyond plain
parsing it enforces:

- **Invariant 7** (ETA-timeout safety): ``MAX_ASR_DURATION_SEC × RT_FACTOR <
  WHISPER_TIMEOUT_SEC`` — validated at construction (raises -> server exits 1).
- ``YTT_PATH_PREFIX`` must end with ``/`` (raises if missing).
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
    allowed_subjects: str = ""  # comma-separated; empty == deny all (fail-closed)
    rate_limit_per_min: int = 20
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

    # --- OAuth (from ESO/OpenBao; optional so unit tests can construct freely) ---
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    jwt_signing_secret: str | None = None

    # ------------------------------------------------------------------ #
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
        """Parsed allowlist; empty -> deny all (fail-closed)."""
        return frozenset(s.strip() for s in self.allowed_subjects.split(",") if s.strip())

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
