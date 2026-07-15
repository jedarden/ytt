"""Unit tests for Phase 8 observability (plan §Observability).

Covers:
- Prometheus metrics existence + label names (no network required)
- structlog redaction processor (sensitive field names + credential URLs)
- ``_sanitize_url`` helper
- ``DATACENTER_ASNS`` + ``DATACENTER_ORG_PATTERNS`` presence
- ``derive_is_residential`` logic
- ``/ytt/metrics`` endpoint returns 200 + Prometheus text
- ``/ytt/admin/egress`` endpoint: 401 (no token), 401 (bad token), 403 (non-listed sub),
  200 (stubbed egress probe)
- ``run_selftest(show_sub=True)`` — reads ``/tmp/ytt_last_sub``
- ``run_selftest(show_sub=True)`` — missing file returns exit 1
- ``configure_logging()`` is idempotent (callable multiple times without error)
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# observability module — metrics + redaction
# ---------------------------------------------------------------------------


def test_metrics_exist():
    """All required Prometheus metrics must be registered with the correct names."""
    from ytt.observability import (  # noqa: F401
        ytt_cache_bytes,
        ytt_cache_evictions_total,
        ytt_canary_failures_total,
        ytt_canary_last_success_timestamp_seconds,
        ytt_egress_is_residential,
        ytt_fetch_blocks_total,
        ytt_fetch_empty_body_total,
        ytt_queue_depth,
        ytt_rate_limited_total,
        ytt_whisper_errors_total,
        ytt_whisper_job_seconds,
    )
    from prometheus_client import REGISTRY

    names = {m.name for m in REGISTRY.collect()}
    # prometheus_client strips the _total suffix from Counter names in the registry
    # (the suffix appears in the exposition format but not in REGISTRY.collect().name).
    expected = {
        "ytt_fetch_blocks",         # Counter (registry name sans _total)
        "ytt_fetch_empty_body",     # Counter
        "ytt_whisper_errors",       # Counter
        "ytt_whisper_job_seconds",  # Histogram (no _total)
        "ytt_cache_bytes",          # Gauge
        "ytt_cache_evictions",      # Counter
        "ytt_queue_depth",          # Gauge
        "ytt_rate_limited",         # Counter
        "ytt_egress_is_residential",  # Gauge
        "ytt_canary_last_success_timestamp_seconds",  # Gauge
        "ytt_canary_failures",      # Counter
    }
    missing = expected - names
    assert not missing, f"Missing metrics: {missing}"


def test_fetch_blocks_total_labels():
    """ytt_fetch_blocks_total must accept an 'outcome' label."""
    from ytt.observability import ytt_fetch_blocks_total

    # Should not raise — labels are pre-declared
    ytt_fetch_blocks_total.labels(outcome="ok")
    ytt_fetch_blocks_total.labels(outcome="ip_blocked")
    ytt_fetch_blocks_total.labels(outcome="no_captions_asr_started")


def test_whisper_errors_total_labels():
    """ytt_whisper_errors_total must accept a 'reason' label."""
    from ytt.observability import ytt_whisper_errors_total

    ytt_whisper_errors_total.labels(reason="asr_failed")
    ytt_whisper_errors_total.labels(reason="timeout")


def test_rate_limited_total_labels():
    """ytt_rate_limited_total must accept a 'subject_hash' label."""
    from ytt.observability import ytt_rate_limited_total

    ytt_rate_limited_total.labels(subject_hash="abc12345")


def test_cache_bytes_gauge():
    """ytt_cache_bytes must be settable and gettable."""
    from ytt.observability import ytt_cache_bytes

    ytt_cache_bytes.set(1024 * 1024)
    # No assertion on the retrieved value — other tests may have changed it;
    # just assert it doesn't raise.


def test_egress_is_residential_gauge():
    """ytt_egress_is_residential must accept 0/1 values."""
    from ytt.observability import ytt_egress_is_residential

    ytt_egress_is_residential.set(1)
    ytt_egress_is_residential.set(0)


# ---------------------------------------------------------------------------
# Redaction processor
# ---------------------------------------------------------------------------


def test_redaction_strips_sensitive_field_names():
    """Sensitive field names must be replaced with '<redacted>'."""
    from ytt.observability import redaction_processor

    event_dict: dict[str, Any] = {
        "event": "some event",
        "sub": "user-123",
        "email": "user@example.com",
        "token": "Bearer eyJ...",
        "secret": "s3cr3t",
        "key": "abc123",
        "authorization": "Bearer eyJ...",
        "transcript": "Hello world",
        "audio_path": "/scratch/video.m4a",
        "proxy_url": "http://user:pass@host:3128",
        "allowed_subjects": "user1,user2",
        "safe_field": "visible",
    }
    result = redaction_processor(None, "info", event_dict)

    # All sensitive fields replaced
    for field in ("sub", "email", "token", "secret", "key", "authorization",
                  "transcript", "audio_path", "proxy_url", "allowed_subjects"):
        assert result[field] == "<redacted>", f"field {field!r} not redacted"

    # Safe fields preserved
    assert result["safe_field"] == "visible"
    assert result["event"] == "some event"


def test_redaction_case_insensitive_keys():
    """Field name check must be case-insensitive."""
    from ytt.observability import redaction_processor

    event_dict: dict[str, Any] = {
        "TOKEN": "should-be-redacted",
        "Authorization": "Bearer eyJ...",
    }
    result = redaction_processor(None, "info", event_dict)
    assert result["TOKEN"] == "<redacted>"
    assert result["Authorization"] == "<redacted>"


def test_redaction_credential_bearing_url():
    """String values containing 'user:pass@' must be sanitized."""
    from ytt.observability import redaction_processor

    event_dict: dict[str, Any] = {
        "event": "Connecting to proxy",
        "url": "http://user:pass@proxy.example.com:3128",
    }
    result = redaction_processor(None, "info", event_dict)
    sanitized = result["url"]
    # Password must be gone
    assert "pass" not in sanitized
    assert "user" not in sanitized
    # Host should still be present (for debugging)
    assert "proxy.example.com" in sanitized


def test_redaction_plain_url_untouched():
    """URLs without credentials must not be modified."""
    from ytt.observability import redaction_processor

    event_dict: dict[str, Any] = {
        "event": "request",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    result = redaction_processor(None, "info", event_dict)
    assert result["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_redaction_email_in_url_untouched():
    """A URL with a query-parameter email (no @user:pass@ pattern) is untouched."""
    from ytt.observability import redaction_processor

    # e.g., "?email=user@example.com" in a URL — this doesn't follow
    # the http://user:pass@host pattern so the regex won't match it.
    plain_url = "https://example.com/callback?email=user%40example.com"
    event_dict: dict[str, Any] = {"event": "x", "url": plain_url}
    result = redaction_processor(None, "info", event_dict)
    # The URL has no credential-bearing '@' pattern → untouched
    assert result["url"] == plain_url


# ---------------------------------------------------------------------------
# _sanitize_url helper
# ---------------------------------------------------------------------------


def test_sanitize_url_strips_credentials():
    """_sanitize_url must strip user:password from a URL."""
    from ytt.observability import _sanitize_url

    url = "http://alice:secret@proxy.example.com:3128/path"
    sanitized = _sanitize_url(url)
    assert "alice" not in sanitized
    assert "secret" not in sanitized
    assert "proxy.example.com" in sanitized


def test_sanitize_url_no_credentials_unchanged():
    """_sanitize_url must return the value unchanged if there are no credentials."""
    from ytt.observability import _sanitize_url

    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert _sanitize_url(url) == url


# ---------------------------------------------------------------------------
# configure_logging idempotent
# ---------------------------------------------------------------------------


def test_configure_logging_idempotent():
    """configure_logging must be callable multiple times without raising."""
    from ytt.observability import configure_logging

    configure_logging()
    configure_logging()  # second call — must not raise


# ---------------------------------------------------------------------------
# selftest — DATACENTER_ASNS / DATACENTER_ORG_PATTERNS / derive_is_residential
# ---------------------------------------------------------------------------


def test_datacenter_asns_seeded():
    """DATACENTER_ASNS must contain the required seed ASNs from the plan."""
    from ytt.selftest import DATACENTER_ASNS

    required = {"AS16509", "AS15169", "AS8075", "AS14061", "AS63949", "AS13335"}
    missing = required - DATACENTER_ASNS
    assert not missing, f"Missing datacenter ASNs: {missing}"


def test_datacenter_org_patterns_seeded():
    """DATACENTER_ORG_PATTERNS must contain the required uppercase fragments."""
    from ytt.selftest import DATACENTER_ORG_PATTERNS

    required = {"AMAZON", "GOOGLE", "MICROSOFT", "DIGITALOCEAN", "LINODE", "CLOUDFLARE"}
    patterns_set = set(DATACENTER_ORG_PATTERNS)
    missing = required - patterns_set
    assert not missing, f"Missing org patterns: {missing}"


@pytest.mark.parametrize(
    "asn,org,expected",
    [
        # Known datacenter ASNs
        ("AS16509", "Amazon AWS", False),
        ("AS15169", "Google LLC", False),
        ("AS8075", "Microsoft Azure", False),
        ("AS14061", "DigitalOcean", False),
        ("AS63949", "Linode LLC", False),
        ("AS13335", "Cloudflare", False),
        # Known datacenter org patterns (even if ASN is unknown)
        ("AS99999", "AMAZON WEB SERVICES", False),
        ("AS99999", "Google Cloud Platform", False),
        ("AS99999", "Hetzner Online GmbH", False),
        # Residential (unknown ASN + no dc pattern)
        ("AS12345", "ISP Corp Residential", True),
        # None/None defaults to residential
        (None, None, True),
        # Unknown ASN but org has no dc keyword → residential
        ("AS99998", "Community Broadband", True),
    ],
)
def test_derive_is_residential(asn: str | None, org: str | None, expected: bool):
    """derive_is_residential must return the correct classification."""
    from ytt.selftest import derive_is_residential

    result = derive_is_residential(asn, org)
    assert result == expected, (
        f"derive_is_residential({asn!r}, {org!r}) = {result}, expected {expected}"
    )


def test_derive_is_residential_case_insensitive_org():
    """Org matching must be case-insensitive (org is uppercased internally)."""
    from ytt.selftest import derive_is_residential

    # lowercase 'amazon' in org — should still detect as datacenter
    assert derive_is_residential("AS99999", "amazon web services") is False


# ---------------------------------------------------------------------------
# run_selftest --show-sub
# ---------------------------------------------------------------------------


def test_show_sub_file_missing(capsys):
    """run_selftest(show_sub=True) must return 1 when /tmp/ytt_last_sub is absent."""
    from ytt.selftest import _show_sub

    # Patch _LAST_SUB_PATH to a definitely-absent file
    with patch("ytt.selftest._LAST_SUB_PATH", "/tmp/_ytt_test_absent_sub_file"):
        rc = _show_sub()
    assert rc == 1
    err = capsys.readouterr().err
    assert "No sub claim on file yet" in err


def test_show_sub_file_present(capsys, tmp_path):
    """run_selftest(show_sub=True) must print the sub and return 0."""
    from ytt.selftest import _show_sub

    sub_file = tmp_path / "last_sub"
    sub_file.write_text("user|12345\n")

    with patch("ytt.selftest._LAST_SUB_PATH", str(sub_file)):
        rc = _show_sub()

    assert rc == 0
    out = capsys.readouterr().out
    assert "user|12345" in out


def test_show_sub_empty_file(capsys, tmp_path):
    """run_selftest(show_sub=True) must return 1 when the sub file is empty."""
    from ytt.selftest import _show_sub

    sub_file = tmp_path / "last_sub"
    sub_file.write_text("")

    with patch("ytt.selftest._LAST_SUB_PATH", str(sub_file)):
        rc = _show_sub()

    assert rc == 1
    err = capsys.readouterr().err
    assert "empty" in err.lower() or "no authenticated" in err.lower()


# ---------------------------------------------------------------------------
# /ytt/metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_200():
    """GET /ytt/metrics must return 200 with Prometheus text content."""
    from ytt.server import build_asgi_app

    app = build_asgi_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/ytt/metrics")
    assert resp.status_code == 200
    # Content-Type should be the prometheus text format
    ct = resp.headers.get("content-type", "")
    assert "text/plain" in ct
    # Body must contain at least one known metric name
    assert "ytt_fetch_blocks_total" in resp.text


def test_metrics_endpoint_contains_all_required_metrics():
    """GET /ytt/metrics must expose all required metric names."""
    from ytt.server import build_asgi_app

    app = build_asgi_app()
    with TestClient(app) as client:
        resp = client.get("/ytt/metrics")
    body = resp.text
    for metric in (
        "ytt_fetch_blocks_total",
        "ytt_fetch_empty_body_total",
        "ytt_whisper_errors_total",
        "ytt_whisper_job_seconds",
        "ytt_cache_bytes",
        "ytt_cache_evictions_total",
        "ytt_queue_depth",
        "ytt_rate_limited_total",
        "ytt_egress_is_residential",
    ):
        assert metric in body, f"Metric {metric!r} not found in /metrics output"


# ---------------------------------------------------------------------------
# /ytt/admin/egress endpoint
# ---------------------------------------------------------------------------


def test_admin_egress_no_token_returns_401():
    """GET /ytt/admin/egress without a Bearer token must return 401."""
    from ytt.server import build_asgi_app

    app = build_asgi_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/ytt/admin/egress")
    assert resp.status_code == 401
    data = resp.json()
    assert data.get("error_code") == "forbidden"


def _fake_access_token(email: str | None, verified: bool = True):
    """Build a real FastMCP AccessToken for verify_token mocking.

    ``get_access_token()`` (used by both admin_egress_endpoint and
    ytt.authz.check_subject_auth) type-checks against the real pydantic
    AccessToken class — a SimpleNamespace duck-type isn't accepted.
    """
    from fastmcp.server.auth.auth import AccessToken

    claims = {}
    if email is not None:
        claims = {"email": email, "email_verified": verified}
    return AccessToken(
        token="faketoken",
        client_id="test-client",
        scopes=[],
        expires_at=None,
        claims=claims,
    )


def test_admin_egress_non_allowlisted_sub_returns_403():
    """GET /ytt/admin/egress with valid token but non-listed sub must return 403."""
    from ytt.server import build_asgi_app, mcp

    app = build_asgi_app()

    with patch.object(
        mcp.auth, "verify_token", return_value=_fake_access_token("unknown@example.com")
    ):
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/ytt/admin/egress",
                headers={"Authorization": "Bearer faketoken"},
            )
    assert resp.status_code == 403
    data = resp.json()
    assert data.get("error_code") == "forbidden"


def test_admin_egress_bad_token_returns_401():
    """GET /ytt/admin/egress with a token Google rejects must return 401."""
    from ytt.server import build_asgi_app, mcp

    app = build_asgi_app()

    with patch.object(mcp.auth, "verify_token", return_value=None):
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/ytt/admin/egress",
                headers={"Authorization": "Bearer notavalidtoken"},
            )
    assert resp.status_code == 401


def test_admin_egress_returns_egress_report(monkeypatch):
    """GET /ytt/admin/egress with a valid allowlisted token must return the egress report."""
    from ytt.models import EgressReport
    from ytt.server import _settings_singleton, build_asgi_app, mcp

    fake_report = EgressReport(
        ip="203.0.113.1",
        asn="AS12345",
        org="Home ISP",
        via_proxy=False,
        is_residential=True,
    )

    app = build_asgi_app()
    monkeypatch.setattr(_settings_singleton, "allowed_subjects", "allowed@example.com")

    with patch.object(
        mcp.auth, "verify_token", return_value=_fake_access_token("allowed@example.com")
    ), patch("ytt.selftest.probe_egress", return_value=fake_report):
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/ytt/admin/egress",
                headers={"Authorization": "Bearer validtoken"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ip"] == "203.0.113.1"
    assert data["asn"] == "AS12345"
    assert data["org"] == "Home ISP"
    assert data["is_residential"] is True
    assert data["via_proxy"] is False


def test_admin_egress_probe_failure_returns_502(monkeypatch):
    """GET /ytt/admin/egress must return 502 when the egress probe itself fails."""
    from ytt.server import _settings_singleton, build_asgi_app, mcp

    app = build_asgi_app()
    monkeypatch.setattr(_settings_singleton, "allowed_subjects", "allowed@example.com")

    with patch.object(
        mcp.auth, "verify_token", return_value=_fake_access_token("allowed@example.com")
    ), patch("ytt.selftest.probe_egress", side_effect=Exception("network error")):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/ytt/admin/egress",
                headers={"Authorization": "Bearer validtoken"},
            )

    assert resp.status_code == 502
