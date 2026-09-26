"""Public-observability data-leak regression (docs/notes/http-endpoints.md).

The Traefik IngressRoute exposes the whole ``/ytt`` prefix publicly, so
``/ytt/metrics`` and ``/ytt/health`` are readable from anywhere on the
internet without a token. The visibility model turns that into a hard rule:
*every unauthenticated response body must be safe to expose publicly,
regardless of what traffic the process has just served*
(docs/notes/http-endpoints.md, "Visibility model").

The existing pins hold the two bodies to their documented shapes
(``test_endpoint_contract.py``, ``test_metrics_cardinality.py``), but they
drive only synthetic counter pokes (``_record_rate_limited``) or scrape in a
near-clean state. This module closes the gap the bead asks for: it drives the
**real pipeline with dirty traffic first** — subjects, YouTube URLs, proxy
credentials, OAuth failures, and ASR errors — and only then scrapes the two
public endpoints, asserting that none of the marker material surfaces:

- **subjects** — an allowlisted subject drives a fetch failure and a real
  rate-limit denial (minting the ``subject_hash`` series through the tool
  path, not a counter poke); a second allowlisted subject runs a successful
  fetch and an ASR job; a third, authenticated-but-not-allowlisted subject is
  refused by the ``AuthMiddleware`` gate;
- **YouTube URLs** — a messy ``watch?v=…`` URL with tracking parameters and
  an embedded email query parameter, a channel URL carrying *credentialed*
  userinfo (rejected as ``bad_url`` with the raw URL quoted into the
  authenticated error message), and three bare video IDs;
- **proxy credentials** — every injected upstream failure message quotes a
  ``user:password@host:port`` proxy URL verbatim, deliberately *dirtier than
  production*: the real fetch/ASR boundaries run messages through
  ``redact_credentials()`` before they are relayed, while this module injects
  the unredacted form as deep as the pipeline accepts it. If a future change
  plumbs job/error messages into a metric label, a log-derived series name,
  or the health body, the public scrape fails here;
- **OAuth failures** — transport 401s (no token, a garbage bearer token) and
  an ``/admin/egress`` 401, with a marker token value in the ``Authorization``
  header;
- **ASR errors** — a Whisper job taken ``pending → running → error`` through
  the real registry/FSM, its terminal message quoting the credentialed proxy
  and an upstream exception detail, then read back via ``get_transcript_job``;
- **transcript text** — a successful caption fetch served through the real
  cache write + ``build_page`` path.

After all of that, both public bodies are held to:

1. the documented bounded aggregate surface — reused verbatim from
   ``test_metrics_cardinality.py`` so the two modules cannot drift;
2. the subject representation rule: the only subject-derived value allowed
   public is the first 8 hex chars of sha256(subject), proven here for the
   exact hash the traffic minted;
3. a raw substring scan for every marker secret — token, subject emails,
   raw URLs, video IDs, transcript text, proxy userinfo, upstream exception
   detail, and a query-echo slug sent to the public endpoints themselves (a
   naive request-controlled branch would reflect it back).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastmcp.exceptions import AuthorizationError
from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

from ytt.server import build_asgi_app, mcp

# ---------------------------------------------------------------------------
# The marker secrets (deliberately unique — an accidental match is impossible)
# ---------------------------------------------------------------------------

#: Allowlisted subject #1 — drives the ip_blocked failure + the rate-limit
#: denial (so the subject_hash series is minted by real tool traffic).
MARKER_SUBJECT = "leak-probe.subject@example.com"

#: Allowlisted subject #2 — drives the successful fetch and the ASR job.
MARKER_SUBJECT_2 = "leak-probe.reader@example.com"

#: Authenticated but NOT allowlisted — the OAuth authorization-failure shape.
MARKER_DENIED_SUBJECT = "leak-probe.mallory@example.com"

#: Bearer token value sent in Authorization headers and public-endpoint queries.
MARKER_TOKEN = "leak-probe-bearer-not-a-real-token"

#: Video IDs (11 chars each) for the fetch-failure / ASR / success scenarios.
MARKER_VIDEO_FETCH_FAIL = "leakProbe01"
MARKER_VIDEO_ASR = "leakProbeAS"
MARKER_VIDEO_OK = "leakProbeOK"

#: A messy watch URL: tracking parameters plus an email query parameter.
MARKER_RAW_URL = (
    "https://www.youtube.com/watch?v=leakProbe01"
    "&list=PLleakprobe&email=leak-probe.subject%40example.com"
)

#: A channel URL carrying credentialed userinfo — canonicalize rejects it and
#: quotes the raw (still-credentialed) input into the bad_url message.
MARKER_BAD_URL = (
    "https://leakprobe-user:leakprobe-secret@www.youtube.com/channel/UCleakprobe"
)

MARKER_PROXY_USER = "leakprobe-user"
MARKER_PROXY_PASS = "leakprobe-secret"
MARKER_PROXY_URL = (
    f"http://{MARKER_PROXY_USER}:{MARKER_PROXY_PASS}@proxy.example.com:3128"
)

#: Transcript body of the successful fetch — real cache write + build_page.
MARKER_TRANSCRIPT = "leak-probe transcript body plughxyzzy"

#: Upstream exception detail injected alongside the proxy URL in the failure
#: messages (the "upstream exception details never appear" leg).
MARKER_EXCEPTION = "leak-probe upstream exception TraceDetail-7c4f"

#: Slug sent as a query parameter to the public endpoints themselves — a
#: request-controlled branch that echoed input would reflect it back.
MARKER_SLUG = "leak-probe-query-echo"

#: Every string that must never appear on a public body, with where it was
#: injected (for the failure message). Percent-encoded variants of the values
#: sent in query strings are scanned too, so an urlencoded echo is caught.
LEAK_MARKERS: dict[str, str] = {
    "allowlisted subject email": MARKER_SUBJECT,
    "second allowlisted subject email": MARKER_SUBJECT_2,
    "denied subject email": MARKER_DENIED_SUBJECT,
    "bearer token value": MARKER_TOKEN,
    "proxy username": MARKER_PROXY_USER,
    "proxy password": MARKER_PROXY_PASS,
    "credentialed proxy URL": MARKER_PROXY_URL,
    "proxy host:port": "proxy.example.com:3128",
    "raw YouTube URL": MARKER_RAW_URL,
    "YouTube tracking parameter": "list=PLleakprobe",
    "credentialed channel URL": MARKER_BAD_URL,
    "video id (fetch failure)": MARKER_VIDEO_FETCH_FAIL,
    "video id (ASR)": MARKER_VIDEO_ASR,
    "video id (success)": MARKER_VIDEO_OK,
    "transcript text": MARKER_TRANSCRIPT,
    "upstream exception detail": MARKER_EXCEPTION,
    "query-echo slug": MARKER_SLUG,
    "urlencoded transcript (query echo)": quote(MARKER_TRANSCRIPT),
    "urlencoded raw URL (query echo)": quote(MARKER_RAW_URL, safe=""),
}

#: sha256 prefix the rate-limit denial is documented to mint
#: (ytt/server.py `_record_rate_limited`: first 8 hex of sha256(subject)).
EXPECTED_SUBJECT_HASH = hashlib.sha256(MARKER_SUBJECT.encode()).hexdigest()[:8]

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "leak-probe", "version": "0"},
    },
}
_ACCEPT_JSON = {"Accept": "application/json, text/event-stream"}


# ---------------------------------------------------------------------------
# The dirty traffic
# ---------------------------------------------------------------------------


async def _drive_dirty_pipeline(token_cell: dict) -> None:
    """Run every in-process dirty scenario through the real tool pipeline.

    Every dirty message injected here is *unredacted* — production runs
    upstream failures through ``redact_credentials()`` at the fetch/ASR
    boundaries; this driver skips that on purpose, so the public bodies are
    proven clean even for material dirtier than the server should ever hold.
    Each step asserts its effect first: if the traffic didn't actually flow,
    the fixture fails loudly instead of letting the leak scans pass vacuously.
    """
    from ytt.errors import ASR_FAILED, IP_BLOCKED, NoCaptionsError, YttError
    from ytt.fetch import FetchResult
    from ytt.models import Segment
    from ytt import server

    # -- 1. OAuth authorization failure: authenticated, not allowlisted ------
    token_cell["token"] = SimpleNamespace(claims={"email": MARKER_DENIED_SUBJECT})
    with pytest.raises(AuthorizationError) as exc_info:
        await mcp.call_tool("get_youtube_transcript", {"url": MARKER_RAW_URL})
    assert MARKER_DENIED_SUBJECT not in str(exc_info.value), (
        "the authZ refusal echoed the denied subject to the caller"
    )

    # -- 2. Allowlisted subject: ip_blocked failure quoting the proxy --------
    token_cell["token"] = SimpleNamespace(claims={"email": MARKER_SUBJECT})
    result = await mcp.call_tool("get_youtube_transcript", {"url": MARKER_RAW_URL})
    sc = result.structured_content
    assert sc["status"] == "error" and sc["error_code"] == "ip_blocked", sc
    assert MARKER_PROXY_URL in sc["message"] and MARKER_EXCEPTION in sc["message"], sc
    result = await mcp.call_tool("get_youtube_transcript", {"url": MARKER_RAW_URL})
    sc = result.structured_content
    assert sc["error_code"] == "ip_blocked", sc  # second token spent

    # -- 3. Same subject, bucket now empty: real rate-limit denial -----------
    # (mints ytt_rate_limited_total{subject_hash} through the tool path)
    result = await mcp.call_tool("get_youtube_transcript", {"url": MARKER_RAW_URL})
    sc = result.structured_content
    assert sc["status"] == "error" and sc["error_code"] == "rate_limited", sc

    # -- 4. bad_url: the raw credentialed URL echoed in the (auth'd) message --
    result = await mcp.call_tool("get_youtube_transcript", {"url": MARKER_BAD_URL})
    sc = result.structured_content
    assert sc["status"] == "error" and sc["error_code"] == "bad_url", sc
    assert MARKER_BAD_URL in sc["message"], sc

    # -- 5. Successful fetch: transcript text through cache + build_page -----
    token_cell["token"] = SimpleNamespace(claims={"email": MARKER_SUBJECT_2})
    result = await mcp.call_tool("get_youtube_transcript", {"url": MARKER_VIDEO_OK})
    sc = result.structured_content
    assert MARKER_TRANSCRIPT in json.dumps(sc), (
        f"success scenario did not serve the transcript marker: {sc}"
    )

    # -- 6. ASR error: pending → running → error with a dirty terminal message
    result = await mcp.call_tool("get_youtube_transcript", {"url": MARKER_VIDEO_ASR})
    sc = result.structured_content
    assert sc["status"] == "pending", sc
    registry = server.whisper_registry
    deadline = time.monotonic() + 5.0
    while True:
        job = await registry.get(MARKER_VIDEO_ASR)
        if job is not None and job.status == "error":
            break
        assert time.monotonic() < deadline, "ASR stub never reached error state"
        await asyncio.sleep(0.01)
    result = await mcp.call_tool("get_transcript_job", {"video_id": MARKER_VIDEO_ASR})
    sc = result.structured_content
    assert sc["status"] == "error" and sc["error_code"] == ASR_FAILED, sc
    assert MARKER_PROXY_URL in sc["message"] and MARKER_EXCEPTION in sc["message"], sc


def _install_dirty_pipeline(monkeypatch, tmp_path) -> dict:
    """Wire the stubs the dirty traffic runs through, at the source modules.

    Same seams the production pipeline uses (and the other suites patch):
    ``ytt.fetch._do_fetch`` under the real ``run_with_proxy_retry`` wrapper,
    ``ytt.whisper.run_whisper_job`` resolved per-call by
    ``_run_whisper_job_bounded``, fresh per-subject limiters so the denial is
    deterministic, and a fresh cache/registry under ``tmp_path``.
    """
    import ytt.authz as authz_mod
    import ytt.fetch as fetch_mod
    import ytt.whisper as whisper_mod
    from fastmcp.server import dependencies as deps
    from fastmcp.server.middleware import authorization as mw_authz

    from ytt.cache import TranscriptCache
    from ytt.config import get_settings
    from ytt.errors import ASR_FAILED, IP_BLOCKED, NoCaptionsError, YttError
    from ytt.fetch import FetchResult
    from ytt.models import Segment
    from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
    from ytt.whisper import WhisperJobRegistry
    from ytt import server

    # Real allowlist through env (same pattern as test_authz_tool_gate).
    monkeypatch.setenv(
        "YTT_ALLOWED_SUBJECTS", f"{MARKER_SUBJECT},{MARKER_SUBJECT_2}"
    )
    monkeypatch.setattr(
        authz_mod, "_LAST_SUB_PATH", str(tmp_path / "ytt_last_sub")
    )
    monkeypatch.setattr(authz_mod, "_written_subs", set())
    get_settings.cache_clear()

    # The real server's startup_scan() creates the cache dir; a unit fixture
    # must create it itself or the first cache write fails with ENOENT.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        server,
        "transcript_cache",
        TranscriptCache(
            cache_dir=str(cache_dir),
            max_bytes=64 * 1024 * 1024,
            reconcile_sec=10_000,
        ),
    )
    monkeypatch.setattr(server, "whisper_registry", WhisperJobRegistry())
    # Two tokens per subject: the fetch-failure subject spends both, then its
    # third call is the real denial; the second subject spends one per scenario.
    monkeypatch.setattr(
        server, "_rate_limiter", SubjectRateLimiter.from_rate_per_min(2)
    )
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(10))

    token_cell: dict = {"token": None}
    monkeypatch.setattr(mw_authz, "get_access_token", lambda: token_cell["token"])
    monkeypatch.setattr(deps, "get_access_token", lambda: token_cell["token"])

    def fake_do_fetch(video_id, lang, settings, proxy):
        if video_id == MARKER_VIDEO_FETCH_FAIL:
            raise YttError(
                IP_BLOCKED,
                f"Unable to communicate with proxy {MARKER_PROXY_URL} "
                f"({MARKER_EXCEPTION})",
            )
        if video_id == MARKER_VIDEO_ASR:
            raise NoCaptionsError("No captions available.", duration_sec=42.0)
        if video_id == MARKER_VIDEO_OK:
            return FetchResult(
                segments=[Segment(start=0.0, duration=1.0, text=MARKER_TRANSCRIPT)],
                source="caption_manual",
                served_lang="en",
                requested_lang=None,
                available_langs=["en"],
                title=f"{MARKER_SLUG} title",
                channel=f"{MARKER_SLUG} channel",
            )
        raise AssertionError(f"unexpected fetch for video id {video_id!r}")

    monkeypatch.setattr(fetch_mod, "_do_fetch", fake_do_fetch)

    async def fake_run_whisper_job(job, reg, settings, cache, active_model, **kw):
        # Mimic the real FSM transitions, then fail with a message quoting the
        # credentialed proxy + upstream detail — unredacted, deliberately.
        await reg.update_status(job.video_id, "running")
        await reg.update_status(
            job.video_id,
            "error",
            error_code=ASR_FAILED,
            message=(
                f"Whisper service request failed: dial {MARKER_PROXY_URL}: "
                f"{MARKER_EXCEPTION}"
            ),
        )

    monkeypatch.setattr(whisper_mod, "run_whisper_job", fake_run_whisper_job)

    return token_cell


@pytest.fixture
def dirty_scrapes(monkeypatch, tmp_path):
    """Drive the dirty traffic, then scrape both public endpoints once.

    Returns the ``/ytt/metrics`` and ``/ytt/health`` bodies (the latter also
    parsed). The OAuth-failure drives and scrape health are asserted inside
    the fixture so every leak test below fails loudly if the traffic itself
    broke, rather than passing vacuously over a clean process.
    """
    token_cell = _install_dirty_pipeline(monkeypatch, tmp_path)
    from ytt.config import get_settings

    try:
        asyncio.run(_drive_dirty_pipeline(token_cell))

        # -- HTTP-level OAuth failures ----------------------------------------
        # Drop the synthetic in-process token first: the HTTP drives must go
        # through the real provider resolution, not the pipeline stubs.
        token_cell["token"] = None
        from unittest.mock import patch

        with TestClient(build_asgi_app(), raise_server_exceptions=False) as client:
            no_token = client.post("/ytt", json=_INITIALIZE, headers=_ACCEPT_JSON)
            assert no_token.status_code == 401, "transport must 401 without a token"
            bad_token = client.post(
                "/ytt",
                json=_INITIALIZE,
                headers={**_ACCEPT_JSON, "Authorization": f"Bearer {MARKER_TOKEN}"},
            )
            assert bad_token.status_code == 401, "transport must 401 a garbage token"
            # The documented "token the IdP rejects → 401" leg
            # (docs/notes/http-endpoints.md §/ytt/admin/egress step 1).
            with patch.object(mcp.auth, "verify_token", return_value=None):
                egress = client.get(
                    "/ytt/admin/egress",
                    headers={"Authorization": f"Bearer {MARKER_TOKEN}"},
                )
            assert egress.status_code == 401, "admin/egress must 401 a rejected token"

            # The public scrapes themselves, with dirty query strings — a
            # request-controlled branch that echoed input would reflect it.
            metrics_resp = client.get(
                "/ytt/metrics",
                params={
                    "token": MARKER_TOKEN,
                    "subject": MARKER_SUBJECT,
                    "transcript": MARKER_TRANSCRIPT,
                    "echo": MARKER_SLUG,
                },
            )
            health_resp = client.get(
                "/ytt/health",
                params={
                    "token": MARKER_TOKEN,
                    "subject": MARKER_SUBJECT,
                    "url": MARKER_RAW_URL,
                },
            )

        assert metrics_resp.status_code == 200, metrics_resp.text[:500]
        assert health_resp.status_code == 200, health_resp.text[:500]
        return SimpleNamespace(
            metrics=metrics_resp.text,
            health=health_resp.text,
            health_json=health_resp.json(),
        )
    finally:
        # Drop the cached Settings built with this fixture's allowlist env.
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The leak assertions
# ---------------------------------------------------------------------------


def _assert_no_marker(body: str, endpoint: str) -> None:
    """No marker secret may appear anywhere on a public response body."""
    leaks = [
        (what, marker)
        for what, marker in LEAK_MARKERS.items()
        if marker in body
    ]
    assert not leaks, (
        f"{endpoint} leaked sensitive material after dirty traffic: "
        + "; ".join(f"{what} ({marker!r})" for what, marker in leaks)
    )


def test_health_body_stays_exact_liveness_shape_after_dirty_traffic(dirty_scrapes):
    """/ytt/health is publicly routed and must stay the fixed liveness body —
    exactly {"status": "ok"} — no matter what the process just served
    (docs/notes/http-endpoints.md §/ytt/health)."""
    assert dirty_scrapes.health_json == {"status": "ok"}


def test_health_body_carries_no_leak_marker(dirty_scrapes):
    """After subjects, URLs, proxy credentials, OAuth failures, and ASR
    errors, the unauthenticated liveness body carries none of it — including
    a reflection of the dirty query string the scrape itself was sent."""
    _assert_no_marker(dirty_scrapes.health, "GET /ytt/health")


def test_metrics_body_carries_no_leak_marker(dirty_scrapes):
    """After the same dirty traffic, the unauthenticated exposition carries
    no token, subject, raw URL, video id, transcript text, credential, or
    upstream exception detail."""
    _assert_no_marker(dirty_scrapes.metrics, "GET /ytt/metrics")


def test_subject_surfaces_only_as_the_documented_sha256_prefix(dirty_scrapes):
    """The rate-limit denial the fixture minted must appear as
    ``ytt_rate_limited_total{subject_hash="<sha256-prefix>"}`` — proving the
    documented subject representation end-to-end through real traffic: the
    exact 8-hex sha256 prefix of the subject, the subject itself nowhere."""
    samples = [
        sample
        for family in text_string_to_metric_families(dirty_scrapes.metrics)
        if family.name == "ytt_rate_limited"
        for sample in family.samples
    ]
    hashes = {s.labels.get("subject_hash") for s in samples}
    assert EXPECTED_SUBJECT_HASH in hashes, (
        f"expected subject_hash {EXPECTED_SUBJECT_HASH!r} (sha256 prefix of "
        f"{MARKER_SUBJECT!r}) not minted by the rate-limit denial; got {hashes}"
    )
    for subject_hash in hashes:
        assert re.fullmatch(r"[0-9a-f]{8}", subject_hash), (
            f"subject_hash {subject_hash!r} is not the documented 8-hex "
            "sha256 prefix"
        )


def test_metrics_surface_stays_bounded_after_dirty_traffic(dirty_scrapes):
    """The exposition still carries exactly the documented aggregate families
    with exactly their documented label keys and bounded label values —
    reusing test_metrics_cardinality's bound verbatim so the two modules
    cannot drift (docs/notes/http-endpoints.md §/ytt/metrics)."""
    from tests.unit.test_metrics_cardinality import (
        SERVER_REQUIRED_FAMILIES,
        _assert_bounded_public_surface,
    )

    _assert_bounded_public_surface(
        dirty_scrapes.metrics,
        required_families=SERVER_REQUIRED_FAMILIES,
        context="GET /ytt/metrics after dirty traffic",
    )
