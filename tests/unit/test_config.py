"""Unit tests for config loading, size parsing, and startup validations.

Covers the Configuration table defaults, ``2Gi``-style size parsing, the
path-prefix trailing-slash rule + path join, public-url required-ness +
normalization, the allowlist parser, **Invariant 7** (ETA-timeout safety),
and the PVC/emptyDir storage validation. The per-subject limit knobs
(``RATE_LIMIT_PER_MIN`` / ``RATE_LIMIT_BURST`` / ``WHISPER_JOBS_PER_HOUR``)
are pinned for absent, zero, negative, malformed, and unsafe-combination
values (fail-closed rules in the :mod:`ytt.config` module docstring).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ytt.config import Settings, join_path, parse_size


# --- parse_size -------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2Gi", 2 * 2**30),
        ("500Mi", 500 * 2**20),
        ("1Ki", 1024),
        ("1G", 10**9),
        ("1M", 10**6),
        ("1.5Gi", int(1.5 * 2**30)),
        ("1048576", 1048576),
        (1048576, 1048576),
        ("0", 0),
        ("100B", 100),
        ("2gi", 2 * 2**30),  # case-insensitive
    ],
)
def test_parse_size_ok(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1Gigi", "Gi", "1.2.3Mi", True])
def test_parse_size_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_size(bad)


# --- defaults match the Configuration table --------------------------------
def test_defaults_match_plan():
    s = Settings()
    assert s.rate_limit_per_min == 20
    assert s.whisper_jobs_per_hour == 10
    assert s.cache_dir == "/cache"
    assert s.cache_backend == "pvc"
    assert s.cache_max_bytes == 2 * 2**30
    assert s.cache_reconcile_sec == 300
    assert s.scratch_dir == "/scratch"
    assert s.max_audio_bytes == 500 * 2**20
    assert s.max_concurrent_fetches == 4
    assert s.max_concurrent_whisper == 1
    assert s.extract_timeout_sec == 60
    assert s.whisper_url == "http://whisper-openai.whisper-stt.svc.cluster.local:8000"
    assert s.whisper_model == "large-v3-turbo"
    assert s.whisper_realtime_factor == 2.0
    assert s.whisper_timeout_sec == 2880
    assert s.max_asr_duration_sec == 1200
    assert s.max_pending_whisper_jobs == 16
    assert s.job_ttl_sec == 3600
    assert s.canary_interval_sec == 600
    assert s.inline_char_limit == 18000
    assert s.chunk_chars == 18000
    assert s.proxy_url is None
    assert s.path_prefix == "/ytt/"
    # public_url deliberately has NO default (required, no fallback — see the
    # dedicated section below); here it just reflects the test-session env
    # that tests/conftest.py setdefaults.


def test_env_override(monkeypatch):
    monkeypatch.setenv("YTT_RATE_LIMIT_PER_MIN", "5")
    monkeypatch.setenv("YTT_CACHE_MAX_BYTES", "512Mi")
    monkeypatch.setenv("YTT_CACHE_BACKEND", "emptydir")
    s = Settings()
    assert s.rate_limit_per_min == 5
    assert s.cache_max_bytes == 512 * 2**20
    assert s.cache_backend == "emptydir"


# --- per-subject rate limit + Whisper quota (Phase 5) ------------------------

def test_rate_limit_burst_defaults_to_one_minute_of_requests():
    """Unset burst resolves to the per-minute rate — a full minute's worth of
    requests may arrive at once."""
    assert Settings().rate_limit_burst == Settings().rate_limit_per_min == 20
    assert Settings(rate_limit_per_min=7).rate_limit_burst == 7


def test_rate_limit_burst_env_override(monkeypatch):
    monkeypatch.setenv("YTT_RATE_LIMIT_BURST", "3")
    s = Settings()
    assert s.rate_limit_burst == 3
    assert s.rate_limit_per_min == 20  # independent


@pytest.mark.parametrize("field", ["rate_limit_per_min", "rate_limit_burst", "whisper_jobs_per_hour", "max_pending_whisper_jobs"])
def test_negative_limits_rejected(field):
    """Negative limits are config errors (fail fast at startup)."""
    with pytest.raises(ValidationError, match="must be >= 0"):
        Settings(**{field: -1})


def test_zero_limits_are_valid_and_fail_closed():
    """0 is meaningful, not an error: it denies everything the limit guards
    (docs/notes/auth.md fail-closed rule — there is no "unlimited")."""
    s = Settings(rate_limit_per_min=0, whisper_jobs_per_hour=0)
    assert s.rate_limit_per_min == 0
    assert s.rate_limit_burst == 0  # unset burst follows the 0 rate
    assert s.whisper_jobs_per_hour == 0
    # The ASR backlog cap follows the same convention: 0 denies every new job.
    assert Settings(max_pending_whisper_jobs=0).max_pending_whisper_jobs == 0


@pytest.mark.parametrize("field", ["rate_limit_per_min", "rate_limit_burst", "whisper_jobs_per_hour", "max_pending_whisper_jobs"])
@pytest.mark.parametrize("bad", ["abc", "", "2.5", "20 requests"])
def test_malformed_limit_values_rejected(field, bad):
    """Non-integer limit values are config errors — startup fails instead of
    guessing a limit (or falling open)."""
    with pytest.raises(ValidationError):
        Settings(**{field: bad})


def test_malformed_limit_env_fails_startup(monkeypatch):
    """The malformed-value rejection holds on the operator path (env var),
    which is where a typo like 'YTT_RATE_LIMIT_PER_MIN=2O' would arrive."""
    monkeypatch.setenv("YTT_RATE_LIMIT_PER_MIN", "abc")
    with pytest.raises(ValidationError, match="rate_limit_per_min"):
        Settings()


def test_zero_rate_with_explicit_positive_burst_rejected():
    """rate=0 is documented deny-all (no refill); an explicit positive burst
    would hand every subject a one-shot allowance that contradicts it — the
    unsafe combination is rejected at startup (fail-closed)."""
    with pytest.raises(ValidationError, match="deny-all"):
        Settings(rate_limit_per_min=0, rate_limit_burst=5)


def test_zero_rate_with_explicit_zero_burst_ok():
    """Explicit burst=0 under rate=0 agrees with the deny-all promise."""
    s = Settings(rate_limit_per_min=0, rate_limit_burst=0)
    assert s.rate_limit_burst == 0


def test_explicit_zero_burst_with_positive_rate_is_deny_all():
    """burst=0 with a positive rate is coherent and stricter, not unsafe:
    capacity 0 means the bucket can never hold a token, so every fetch is
    denied regardless of the refill rate."""
    s = Settings(rate_limit_per_min=20, rate_limit_burst=0)
    assert s.rate_limit_burst == 0
    assert s.rate_limit_per_min == 20


# --- path prefix + join -----------------------------------------------------
def test_join_path_collapses_boundary_slash():
    assert join_path("/ytt/", "/health") == "/ytt/health"
    assert join_path("/ytt/", "health") == "/ytt/health"


def test_route_helper():
    s = Settings(path_prefix="/ytt/")
    assert s.route("health") == "/ytt/health"
    assert s.route("/health") == "/ytt/health"


def test_path_prefix_requires_trailing_slash():
    with pytest.raises(ValidationError):
        Settings(path_prefix="/ytt")


def test_path_prefix_requires_leading_slash():
    with pytest.raises(ValidationError):
        Settings(path_prefix="ytt/")


# --- public url / audience --------------------------------------------------
def test_public_url_trailing_slash_stripped():
    s = Settings(public_url="https://mcp.ardenone.com/ytt/")
    assert s.public_url == "https://mcp.ardenone.com/ytt"
    assert s.audience == "https://mcp.ardenone.com/ytt"


def test_public_url_env_used_when_set(monkeypatch):
    """The operator path: an explicit env value lands byte-for-byte (modulo
    the trailing-slash normalization) in public_url/audience."""
    monkeypatch.setenv("YTT_PUBLIC_URL", "https://mcp.example.com/ytt")
    s = Settings()
    assert s.public_url == "https://mcp.example.com/ytt"
    assert s.audience == "https://mcp.example.com/ytt"


def test_public_url_required_no_fallback(monkeypatch):
    """Unset YTT_PUBLIC_URL is a startup error, never the retired baked-in
    reference-deployment default — OAuth metadata must not silently target
    mcp.ardenone.com (bead ytt-a1fbc575)."""
    monkeypatch.delenv("YTT_PUBLIC_URL", raising=False)
    with pytest.raises(ValidationError, match="YTT_PUBLIC_URL is required"):
        Settings()


def test_public_url_empty_env_fails_closed(monkeypatch):
    """Empty is the manifest-interpolating-a-missing-value case — an error,
    not a silent fallback (same posture as YTT_PROXY_URL)."""
    monkeypatch.setenv("YTT_PUBLIC_URL", "")
    with pytest.raises(ValidationError, match="YTT_PUBLIC_URL is required"):
        Settings()


@pytest.mark.parametrize(
    "bad",
    [
        "   ",  # whitespace-only == empty
        "https://mcp.example.com/ytt ",  # surrounding whitespace
        "https://mcp.example.com/ ytt",  # embedded whitespace (line wrap)
        "mcp.example.com/ytt",  # no scheme
        "ftp://mcp.example.com/ytt",  # non-http scheme
        "https://",  # no hostname
        "https://mcp.example.com/ytt?a=1",  # query
        "https://mcp.example.com/ytt#frag",  # fragment
        "not-a-url",
    ],
)
def test_public_url_rejects_malformed_values(monkeypatch, bad):
    """Malformed values fail on the operator path (env var), which is where
    a typo would arrive — startup fails instead of booting with a garbage
    audience baked into every OAuth metadata document."""
    monkeypatch.setenv("YTT_PUBLIC_URL", bad)
    with pytest.raises(ValidationError, match="YTT_PUBLIC_URL"):
        Settings()


def test_public_url_http_allowed_for_local_boots(monkeypatch):
    """http stays valid — the built-image smoke test and localhost dev boots
    use it. Anthropic's backend needing https is a deployment concern, not a
    startup-validation one."""
    monkeypatch.setenv("YTT_PUBLIC_URL", "http://127.0.0.1:18080/ytt")
    s = Settings()
    assert s.public_url == "http://127.0.0.1:18080/ytt"
    assert s.audience == "http://127.0.0.1:18080/ytt"


# --- allowlist parsing ------------------------------------------------------
def test_allowlist_empty_is_deny_all():
    assert Settings().allowed_subjects_set == frozenset()


def test_allowlist_parsing_strips_and_dedupes():
    s = Settings(allowed_subjects="alice,  bob , alice ,")
    assert s.allowed_subjects_set == frozenset({"alice", "bob"})


# --- Invariant 7 ------------------------------------------------------------
def test_invariant_7_holds_for_defaults():
    s = Settings()
    assert s.max_asr_duration_sec * s.whisper_realtime_factor < s.whisper_timeout_sec


def test_invariant_7_violation_raises():
    with pytest.raises(ValidationError):
        # 2000 × 2.0 = 4000, not < 1000
        Settings(
            max_asr_duration_sec=2000,
            whisper_realtime_factor=2.0,
            whisper_timeout_sec=1000,
        )


# --- storage validation -----------------------------------------------------
def test_validate_storage_emptydir_warns(tmp_path):
    s = Settings(cache_backend="emptydir", cache_dir=str(tmp_path))
    warnings = s.validate_storage()
    assert any("emptyDir" in w for w in warnings)


def test_validate_storage_pvc_ok(tmp_path):
    # The real tmpfs/disk under tmp_path is far larger than the small cap.
    s = Settings(cache_backend="pvc", cache_dir=str(tmp_path), cache_max_bytes="1Mi")
    assert s.validate_storage() == []


def test_validate_storage_pvc_oversized_raises(tmp_path):
    s = Settings(cache_backend="pvc", cache_dir=str(tmp_path), cache_max_bytes="999Ti")
    with pytest.raises(ValueError, match="exceeds PVC"):
        s.validate_storage()


def test_validate_storage_pvc_missing_dir_raises():
    s = Settings(cache_backend="pvc", cache_dir="/nonexistent/ytt/cache")
    with pytest.raises(ValueError, match="statvfs"):
        s.validate_storage()


# --- upstream OIDC IdP (YTT_OIDC_ISSUER / YTT_OIDC_CONFIG_URL) ---------------
def test_oidc_defaults_are_the_reference_authentik():
    """Unset env vars resolve to the reference Authentik endpoints —
    byte-identical with the pre-0.2.21 hardcoded values in ytt/auth.py, so
    the reference deployment's behavior is unchanged (the AUTHENTIK_*
    aliases there are pinned to the same constants)."""
    s = Settings()
    assert s.oidc_issuer == "https://sso.ardenone.com/application/o/ytt/"
    assert s.oidc_config_url == (
        "https://sso.ardenone.com/application/o/ytt/.well-known/openid-configuration"
    )
    from ytt import auth

    assert auth.AUTHENTIK_ISSUER == s.oidc_issuer
    assert auth.AUTHENTIK_OIDC_CONFIG_URL == s.oidc_config_url


def test_oidc_config_url_derived_from_issuer():
    """BYO-IdP needs only YTT_OIDC_ISSUER: the discovery URL follows it per
    OIDC Discovery §4, with the trailing-slash difference absorbed (the
    reference Authentik issuer carries one; Keycloak-style realm issuers
    don't — neither may be normalized off the issuer itself)."""
    s = Settings(oidc_issuer="https://idp.example.com/realms/ytt")
    assert s.oidc_issuer == "https://idp.example.com/realms/ytt"
    assert s.oidc_config_url == (
        "https://idp.example.com/realms/ytt/.well-known/openid-configuration"
    )

    s = Settings(oidc_issuer="https://idp.example.com/application/o/ytt/")
    assert s.oidc_config_url == (
        "https://idp.example.com/application/o/ytt/.well-known/openid-configuration"
    )


def test_oidc_config_url_explicit_override_wins():
    """An IdP whose discovery document is not at the standard issuer-relative
    path gets YTT_OIDC_CONFIG_URL — used verbatim, never re-derived."""
    s = Settings(
        oidc_issuer="https://idp.example.com/realms/ytt",
        oidc_config_url="https://idp.example.com/static/discovery.json",
    )
    assert s.oidc_config_url == "https://idp.example.com/static/discovery.json"


def test_oidc_env_overrides(monkeypatch):
    monkeypatch.setenv("YTT_OIDC_ISSUER", "https://env.example.com/realms/ytt")
    s = Settings()
    assert s.oidc_issuer == "https://env.example.com/realms/ytt"
    assert s.oidc_config_url == (
        "https://env.example.com/realms/ytt/.well-known/openid-configuration"
    )


def test_oidc_env_config_url_override(monkeypatch):
    monkeypatch.setenv("YTT_OIDC_ISSUER", "https://env.example.com/realms/ytt")
    monkeypatch.setenv("YTT_OIDC_CONFIG_URL", "https://env.example.com/disc")
    s = Settings()
    assert s.oidc_config_url == "https://env.example.com/disc"


@pytest.mark.parametrize(
    "bad",
    [
        "http://idp.example.com/realms/ytt",  # not https (OIDC Core §3.1.2.1)
        "",  # empty — an error, not a silent default (fail-closed)
        "   ",  # whitespace-only == empty
        "https://idp.example.com/realms ytt",  # embedded whitespace (line wrap)
        " https://idp.example.com/realms/ytt ",  # surrounding whitespace
        "https://idp.example.com/realms?x=1",  # query (iss is byte-exact)
        "https://idp.example.com/realms#frag",  # fragment
        "not-a-url",
        "https://",  # no hostname
    ],
)
def test_oidc_issuer_rejects_malformed_values(bad):
    with pytest.raises(ValidationError):
        Settings(oidc_issuer=bad)


@pytest.mark.parametrize(
    "bad",
    [
        "http://idp.example.com/discovery",
        "",
        "https://idp.example.com/d?x=1",
        "https://idp.example.com/d#f",
        "https://",
    ],
)
def test_oidc_config_url_rejects_malformed_values(bad):
    with pytest.raises(ValidationError):
        Settings(oidc_config_url=bad)
