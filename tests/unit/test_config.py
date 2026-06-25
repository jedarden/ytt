"""Unit tests for config loading, size parsing, and startup validations.

Covers the Configuration table defaults, ``2Gi``-style size parsing, the
path-prefix trailing-slash rule + path join, public-url normalization, the
allowlist parser, **Invariant 7** (ETA-timeout safety), and the PVC/emptyDir
storage validation.
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
    assert s.job_ttl_sec == 3600
    assert s.canary_interval_sec == 600
    assert s.inline_char_limit == 18000
    assert s.chunk_chars == 18000
    assert s.proxy_url is None
    assert s.path_prefix == "/ytt/"
    assert s.public_url == "https://mcp.ardenone.com/ytt"


def test_env_override(monkeypatch):
    monkeypatch.setenv("YTT_RATE_LIMIT_PER_MIN", "5")
    monkeypatch.setenv("YTT_CACHE_MAX_BYTES", "512Mi")
    monkeypatch.setenv("YTT_CACHE_BACKEND", "emptydir")
    s = Settings()
    assert s.rate_limit_per_min == 5
    assert s.cache_max_bytes == 512 * 2**20
    assert s.cache_backend == "emptydir"


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
