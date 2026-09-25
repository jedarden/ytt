"""End-to-end MCP tool contract tests over the real Streamable HTTP transport.

Binds the README API contract (``README.md`` §Tools + the "Pass any YouTube
URL form" promise) to executable tests that drive the actual ASGI app
(``build_asgi_app()`` + Starlette ``TestClient``) with full MCP sessions —
initialize handshake → ``tools/list`` → ``tools/call`` — not
``mcp.call_tool()`` (that level is covered by ``test_server.py`` and
``test_subject_limits_e2e.py``) and not the operational-route contract
(``test_endpoint_contract.py``). Everything after the fake IdP — scope gate,
allowlist middleware, subject resolution, transport session handling, tool
dispatch, cache, registry, pagination — is the production path.

Pinned here, over the wire:

- **tools/list** advertises exactly the two README tools, with the argument
  surfaces the README documents.
- **URL aliases** — watch?v=, youtu.be, /shorts/, /live/, /embed/, the ``m.``
  subdomain and the bare 11-char id all canonicalize to one video and land in
  **one** cache unit on disk (README: "all normalize to the same cache
  entry"); follow-up calls carrying the served ``lang`` are served from that
  unit without another fetch.
- **Inline vs paginated** — a short transcript with ``mode=full`` is one
  complete answer; ``mode=chunk`` (or text over the inline limit) yields
  ``status='partial'`` with the loud ⚠️ PARTIAL prefix and a ``next_cursor``.
- **Cursor continuation & staleness** — ``next_cursor`` walks to an
  ``is_final`` page whose text reassembles the full transcript; a tampered
  cursor, a malformed cursor, a cursor carried to a different video, and a
  cursor held across a content refresh all fail with
  ``error_code='cursor_stale'``.
- **Cache hits** — a repeat call with the served lang re-fetches nothing and
  spends no rate-limit token (README: "cache hits … are free"), while a fresh
  miss under the same exhausted bucket is denied ``rate_limited``.
- **Whisper states** — no captions → ``status='pending'`` + a job in the
  registry; ``get_transcript_job`` sees the queued job, delivers the finished
  transcript directly (``source='whisper'``), relays the job's error shape,
  answers ``not_found`` for unknown videos and for done-but-evicted
  transcripts, and a second caller joins the in-flight job instead of
  starting a second one.
- **Authorization before transcript work** — unauthenticated → 401 + RFC 9728
  challenge; a valid token whose subject is not allowlisted → an MCP-level
  isError denial and an empty ``tools/list``, with the transcript pipeline
  (caption fetch / job create / ASR run / cache write) completely silent.

Cache-key semantics (plan §Language selection): a cache unit is keyed
``(video_id, served_lang)``; the first response's ``lang`` field is what a
follow-up call passes to hit it. The unit suite mocks
``transcript_cache.get`` wholesale (``test_server.py``), so the real
keying — and the on-disk one-entry-per-video property — is only exercised
here.

Auth tooling: the transport's RequireAuthMiddleware demands an
``Authorization: Bearer`` header and the provider's required scopes before
``verify_token`` is consulted, so the fake ``AccessToken`` carries the scope
set ``ytt.auth`` requires of every real client (``openid email profile
offline_access``) and ``email_verified: False`` — the value the reference
Authentik hardcodes (``ytt.authz`` docstring); the allowlist must not care.

Hermeticity: an autouse guard replaces ``ytt.fetch.fetch_transcript`` and
``ytt.whisper.run_whisper_job`` with fail-hard stubs, so no test in this
file can reach YouTube or a Whisper service — tests install their own
recording stub on those same seams when the pipeline must produce or fail.
(A probe during development proved a live fetch otherwise fires: an early
draft let one stub expire between two calls and the second call really did
fetch YouTube. The unit gate must never do that.)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from starlette.testclient import TestClient

import ytt.fetch
import ytt.whisper
from ytt import server
from ytt.cache import TranscriptCache
from ytt.errors import EMPTY_BODY, YttError
from ytt.fetch import FetchResult
from ytt.models import Segment, WhisperJob
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.config import get_settings
from ytt.server import build_asgi_app, mcp
from ytt.whisper import WhisperJobRegistry

# ---------------------------------------------------------------------------
# Constants + token/auth helpers
# ---------------------------------------------------------------------------

#: A allowlisted subject for the whole module (the autouse fixtures wire it).
SUBJECT = "reader@example.com"
#: A valid-token subject the allowlist does not know.
STRANGER = "stranger@example.com"

#: Synthetic 11-char ids ([A-Za-z0-9_-]{11}) — never real videos.
VIDEO = "aliasVideo1"
VIDEO2 = "freshMiss01"
WORDLIST = [f"w{i:03d}" for i in range(60)]  # 299 chars joined — multi-chunk
SHORT_WORDS = ["hello", "world"]

ACCEPT = {"Accept": "application/json, text/event-stream"}

#: The scopes ``ytt.auth``'s provider requires of every real client — the
#: transport 403s (``insufficient_scope``) a token missing any of them before
#: verify_token is ever consulted.
SCOPES = ["openid", "email", "profile", "offline_access"]


def _access_token(email: str):
    """A real FastMCP AccessToken standing in for a verified IdP response.

    ``email_verified`` is False on purpose: the reference Authentik hardcodes
    it False for every account (``ytt.authz`` module docstring), and the
    allowlist must admit the subject anyway.
    """
    from fastmcp.server.auth.auth import AccessToken

    return AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=SCOPES,
        expires_at=None,
        claims={"email": email, "email_verified": False},
    )


def _bearer_as(monkeypatch, email: str) -> None:
    """Make the production token-verification path resolve to *email*."""

    async def _verify(token: str):
        return _access_token(email)

    monkeypatch.setattr(mcp.auth, "verify_token", _verify)


# ---------------------------------------------------------------------------
# Autouse fixtures — hermetic, authorized, deterministic
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_egress(monkeypatch):
    """Default-deny stubs on the two network seams the tools can reach.

    Every test that needs the pipeline to produce or fail installs its own
    stub on the same seams; anything that reaches the guarded originals is a
    test bug, not a network call.
    """

    async def _no_fetch(*args: Any, **kwargs: Any):
        raise AssertionError(
            "real caption fetch attempted — install a fetch_transcript stub"
        )

    async def _no_whisper(*args: Any, **kwargs: Any):
        raise AssertionError(
            "real Whisper job attempted — install a run_whisper_job stub"
        )

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _no_fetch)
    monkeypatch.setattr(ytt.whisper, "run_whisper_job", _no_whisper)


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    """Allowlist SUBJECT for every reader in this module.

    Both the tool path (``get_youtube_transcript``) and the allowlist gate
    (``ytt.authz.check_subject_auth``) read ``get_settings()`` — an lru_cache
    any earlier test module may have reset with a differently-configured
    instance (test_auth, test_authz_tool_gate). Patching one instance's field
    is therefore not enough: this follows the test_authz_tool_gate pattern —
    set the env var and reset the cache so the next read reconstructs — and
    clears again on the way out so the suite stays consistent."""
    monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", SUBJECT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _bearer_subject(monkeypatch):
    """Every request rides a valid bearer token for SUBJECT — tests that
    exercise the allowlist gate re-point this at STRANGER, so the 401 path
    (no/invalid token) stays test_endpoint_contract.py's subject."""
    _bearer_as(monkeypatch, SUBJECT)


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    """Deterministic per-subject limits — roomy by default; tests that need
    exhaustion install their own."""
    monkeypatch.setattr(
        server,
        "_rate_limiter",
        SubjectRateLimiter(capacity=20, refill_rate_per_sec=20.0 / 60.0),
    )
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(jobs_per_hour=10))


# ---------------------------------------------------------------------------
# Transport session plumbing
# ---------------------------------------------------------------------------


def _json_rpc_events(resp) -> list[dict]:
    """Parse a transport response body into its JSON-RPC message(s)."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        events = []
        for block in resp.text.split("\n\n"):
            data = "".join(
                line[5:].lstrip()
                for line in block.splitlines()
                if line.startswith("data:")
            )
            if data:
                events.append(json.loads(data))
        return events
    return [json.loads(resp.text)]


class McpSession:
    """One live Streamable-HTTP MCP session against the real ASGI app."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self._next_id = 0
        self._session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {**ACCEPT, "Authorization": "Bearer test-token"}
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def start(self) -> None:
        """initialize handshake → notifications/initialized."""
        self._next_id += 1
        resp = self._client.post(
            "/ytt",
            json={
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-contract-test", "version": "0"},
                },
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, (
            f"initialize failed: {resp.status_code} {resp.text[:200]}"
        )
        self._session_id = resp.headers["mcp-session-id"]
        ack = self._client.post(
            "/ytt",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._headers(),
        )
        assert ack.status_code == 202, ack.status_code

    def request(self, method: str, params: dict | None = None) -> dict:
        """One JSON-RPC request → its single response message."""
        self._next_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        resp = self._client.post("/ytt", json=payload, headers=self._headers())
        assert resp.status_code == 200, (
            f"{method}: HTTP {resp.status_code} {resp.text[:200]}"
        )
        events = _json_rpc_events(resp)
        mine = [e for e in events if e.get("id") == self._next_id]
        assert len(mine) == 1, f"{method}: expected one response, got {events!r}"
        return mine[0]

    def call(self, name: str, arguments: dict) -> dict:
        """tools/call → the full MCP result object.

        A tool-level error is *data* (``structuredContent`` with
        ``status='error'``, ``isError`` false/absent); an authorization
        denial is protocol-level (``isError`` true, no structuredContent).
        """
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in resp, resp
        return resp["result"]

    def call_payload(self, name: str, arguments: dict) -> dict:
        """tools/call → the TranscriptResult dict the tool returned."""
        result = self.call(name, arguments)
        sc = result.get("structuredContent")
        assert sc is not None, f"no structuredContent: {json.dumps(result)[:300]}"
        return sc


@pytest.fixture
def client():
    with TestClient(build_asgi_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def session(client):
    """An initialized MCP session as the allowlisted SUBJECT."""
    s = McpSession(client)
    s.start()
    return s


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A real flat-file cache (tmp dir) wired into the server singleton."""
    c = TranscriptCache(tmp_path, max_bytes=1 << 20, reconcile_sec=0)
    monkeypatch.setattr(server, "transcript_cache", c)
    return c


@pytest.fixture
def registry(monkeypatch):
    """A fresh Whisper job registry wired into the server singleton."""
    r = WhisperJobRegistry()
    monkeypatch.setattr(server, "whisper_registry", r)
    return r


# ---------------------------------------------------------------------------
# Pipeline stub factories + helpers
# ---------------------------------------------------------------------------


def _caption_fetch(calls: list, words: list[str], source: str = "caption_manual"):
    """A fetch stub that records video_ids and returns a captioned result."""

    async def _fetch(video_id, lang, settings):
        calls.append(video_id)
        segs = [
            Segment(start=float(i), duration=1.0, text=w)
            for i, w in enumerate(words)
        ]
        return FetchResult(
            segments=segs,
            source=source,
            served_lang="en",
            requested_lang=None,
            available_langs=["en"],
            title="Test Video",
            channel="Test Channel",
            duration_sec=float(len(words)),
            published=None,
            message=None,
        )

    return _fetch


def _captionless_fetch(calls: list):
    """A fetch stub for videos with no captions (the Whisper trigger)."""

    async def _fetch(video_id, lang, settings):
        calls.append(video_id)
        raise YttError(EMPTY_BODY, "video has no captions")

    return _fetch


def _gated_whisper_run(gate: dict):
    """An ASR run stub holding its job in ``pending`` until released.

    The gate dict is the test's handle: set ``gate["release"] = True`` (and
    ``gate["text"]``) to let the "transcription" complete. The wait loop is
    bounded so a test that forgets to release cannot hold the shared
    YTT_MAX_CONCURRENT_WHISPER slot forever.
    """

    async def _run(job, job_registry, settings, cache, active_model):
        for _ in range(2000):  # ~20 s at 10 ms — bounded, releases the slot
            if gate.get("release"):
                break
            await asyncio.sleep(0.01)
        if not gate.get("release"):
            return
        job.status = "running"
        await cache.put(
            job.video_id,
            "whisper",
            gate["text"],
            [
                {"start": float(i), "duration": 0.5, "text": w}
                for i, w in enumerate(gate["text"].split())
            ],
            "whisper",
            {"title": "ASR Video"},
        )
        job.status = "done"
        job.result_ref = f"{job.video_id}.whisper.txt"

    return _run


def _failing_whisper_run(error_code: str = "asr_failed", message: str = "ASR exploded"):
    """An ASR run stub that immediately drives its job to ``error``.

    No awaits before the transition: the job is terminal before the creating
    call's response has even been read, so the poll below is deterministic.
    """

    async def _run(job, job_registry, settings, cache, active_model):
        job.status = "error"
        job.error_code = error_code
        job.message = message

    return _run


def _capture_created_jobs(monkeypatch, registry: WhisperJobRegistry) -> list:
    """Record (video_id, is_new) per get_or_create through the real one.

    A list, not a dict keyed by video: the join scenario calls get_or_create
    twice for the SAME video and both outcomes matter."""
    records: list[tuple[str, bool]] = []
    original = registry.get_or_create

    async def _recording(video_id, duration_sec, settings):
        job, is_new = await original(
            video_id, duration_sec=duration_sec, settings=settings
        )
        records.append((video_id, is_new))
        return job, is_new

    monkeypatch.setattr(registry, "get_or_create", _recording)
    return records


def _poll_until(session: McpSession, video_id: str, statuses: set[str]) -> dict:
    """Poll get_transcript_job until it reaches one of *statuses*.

    A real client polls (README: "call get_transcript_job later"), and the
    background job advances on the app's event loop — so the test polls with
    a deadline instead of assuming task scheduling.
    """
    deadline = time.monotonic() + 5.0
    payload: dict = {}
    while time.monotonic() < deadline:
        payload = session.call_payload(
            "get_transcript_job", {"video_id": video_id}
        )
        if payload.get("status") in statuses:
            return payload
        time.sleep(0.05)
    return payload


def _strip_partial_prefix(text: str) -> str:
    """Drop the loud ⚠️ PARTIAL banner (ends with a blank line)."""
    if text.startswith("⚠️ PARTIAL:"):
        return text.split("\n\n", 1)[1]
    return text


def _shrink_page_budget(monkeypatch, inline: int, chunk: int) -> None:
    """Shrink the pagination budget for a test.

    Env + cache reset, not field-patching: the tool and ``build_page`` read
    ``get_settings()`` (see the ``_allowlist`` fixture for why)."""
    monkeypatch.setenv("YTT_INLINE_CHAR_LIMIT", str(inline))
    monkeypatch.setenv("YTT_CHUNK_CHARS", str(chunk))
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# tools/list — the README §Tools table
# ---------------------------------------------------------------------------


def test_tools_list_advertises_exactly_the_readme_tools(session):
    """The transport exposes exactly the two documented tools, and their
    argument surfaces match the README signatures — nothing more is callable,
    nothing documented is missing."""
    resp = session.request("tools/list")
    tools = resp["result"]["tools"]
    by_name = {t["name"]: t for t in tools}

    assert sorted(by_name) == ["get_transcript_job", "get_youtube_transcript"]

    fetch_tool = by_name["get_youtube_transcript"]
    assert fetch_tool["description"], "tool description must not be empty"
    schema = fetch_tool["inputSchema"]
    assert schema["required"] == ["url"]
    # README signature: get_youtube_transcript(url, lang?, mode?, cursor?,
    # start?, end?, query?)
    assert set(schema["properties"]) == {
        "url",
        "lang",
        "mode",
        "cursor",
        "start",
        "end",
        "query",
    }

    job_tool = by_name["get_transcript_job"]
    assert job_tool["description"], "tool description must not be empty"
    assert job_tool["inputSchema"]["required"] == ["video_id"]


# ---------------------------------------------------------------------------
# URL aliases — one canonical video, one cache entry
# ---------------------------------------------------------------------------


def test_url_aliases_normalize_to_one_cache_entry(session, cache, monkeypatch):
    """watch?v=, youtu.be, /shorts/, /live/, /embed/, the m. subdomain and
    the bare id all canonicalize to the same video and land in ONE cache
    unit on disk (README: "all normalize to the same cache entry"); the
    follow-up calls carrying the served lang are served from that unit
    without another fetch."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, SHORT_WORDS))

    aliases = [
        f"https://www.youtube.com/watch?v={VIDEO}&list=PLwhatever",
        f"https://youtu.be/{VIDEO}?si=abc123",
        f"https://www.youtube.com/shorts/{VIDEO}",
        f"https://www.youtube.com/live/{VIDEO}",
        f"https://www.youtube.com/embed/{VIDEO}",
        f"https://m.youtube.com/watch?v={VIDEO}",
        VIDEO,
    ]

    payloads = []
    for i, alias in enumerate(aliases):
        args: dict[str, Any] = {"url": alias}
        if i > 0:
            args["lang"] = "en"  # follow-up calls carry the served lang
        payload = session.call_payload("get_youtube_transcript", args)
        payloads.append(payload)

    first = payloads[0]
    assert first["status"] == "ok"
    assert first["video_id"] == VIDEO
    assert first["lang"] == "en"
    assert first["text"] == "hello world"
    for payload in payloads[1:]:
        assert payload["video_id"] == first["video_id"]
        assert payload["text"] == first["text"]
        assert payload["lang"] == first["lang"]
        assert payload["source"] == first["source"]

    # One canonical fetch, one physical cache unit (the .txt/.json pair).
    assert calls == [VIDEO]
    assert sorted(p.name for p in cache._dir.iterdir()) == [
        f"{VIDEO}.en.json",
        f"{VIDEO}.en.txt",
    ]


# ---------------------------------------------------------------------------
# Inline vs paginated responses
# ---------------------------------------------------------------------------


def test_short_transcript_inline_full(session, cache, monkeypatch):
    """mode=full + text under the inline limit → one complete answer:
    status ok, is_final true, no next_cursor, quality + metadata attached."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, SHORT_WORDS))

    payload = session.call_payload("get_youtube_transcript", {"url": VIDEO})

    assert payload["status"] == "ok"
    assert payload["video_id"] == VIDEO
    assert payload["text"] == "hello world"
    assert payload["lang"] == "en"
    assert payload["source"] == "caption_manual"
    assert payload["transcript_quality"] == "human-authored captions"
    assert payload["title"] == "Test Video"
    assert payload["channel"] == "Test Channel"
    assert payload["is_final"] is True
    assert payload["offset"] == 0
    assert payload["total_chars"] == len("hello world")
    assert "next_cursor" not in payload


def test_mode_chunk_always_paginates(session, cache, monkeypatch):
    """mode=chunk paginates a transcript that would fit inline: status
    partial, is_final false, a truthy next_cursor and the loud PARTIAL
    banner (README: "paginated chunks + next_cursor for long videos";
    tool description: "On mode='chunk', always paginates regardless of
    length"). The chunk size is shrunk below the transcript length — a
    transcript fitting in one chunk completes on page 1 even in chunk
    mode."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, SHORT_WORDS))
    _shrink_page_budget(monkeypatch, inline=18000, chunk=8)

    first = session.call_payload(
        "get_youtube_transcript", {"url": VIDEO, "mode": "chunk", "lang": "en"}
    )

    assert first["status"] == "partial"
    assert first["is_final"] is False
    assert first["next_cursor"]
    assert first["text"].startswith("⚠️ PARTIAL:")
    assert "INCOMPLETE" in first["text"]

    second = session.call_payload(
        "get_youtube_transcript",
        {"url": VIDEO, "mode": "chunk", "lang": "en", "cursor": first["next_cursor"]},
    )
    assert second["is_final"] is True
    assert (
        _strip_partial_prefix(first["text"]) + second["text"] == "hello world"
    )


def test_over_limit_transcript_paginates_in_full_mode(session, cache, monkeypatch):
    """mode=full text over the inline limit → chunk 1 + next_cursor (the
    README's 'long videos' case), served from the cache on every page."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, WORDLIST))
    _shrink_page_budget(monkeypatch, inline=100, chunk=100)

    full_text = " ".join(WORDLIST)
    payload = session.call_payload("get_youtube_transcript", {"url": VIDEO})

    assert payload["status"] == "partial"
    assert payload["is_final"] is False
    assert payload["total_chars"] == len(full_text)
    assert payload["offset"] == 0
    assert payload["next_cursor"]
    assert calls == [VIDEO]  # exactly one fetch across all the pages below


def test_cursor_continuation_walks_to_the_final_page(session, cache, monkeypatch):
    """Following next_cursor to the end: contiguous offsets, every non-final
    page carrying the loud banner, and the de-bannered chunks reassembling
    the transcript byte-for-byte."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, WORDLIST))
    _shrink_page_budget(monkeypatch, inline=100, chunk=100)

    full_text = " ".join(WORDLIST)
    page = session.call_payload("get_youtube_transcript", {"url": VIDEO})
    assert page["status"] == "partial"

    chunks: list[str] = []
    offsets: list[int] = []
    pages = 0
    while True:
        assert pages < 50, "pagination did not terminate"
        pages += 1
        if page["status"] == "partial":
            assert page["is_final"] is False
            assert page["text"].startswith("⚠️ PARTIAL:")
            chunks.append(_strip_partial_prefix(page["text"]))
            offsets.append(page["offset"])
            page = session.call_payload(
                "get_youtube_transcript",
                {"url": VIDEO, "lang": "en", "cursor": page["next_cursor"]},
            )
            continue
        assert page["status"] == "ok"
        assert page["is_final"] is True
        assert "next_cursor" not in page
        chunks.append(page["text"])
        offsets.append(page["offset"])
        break

    assert pages >= 2, "expected the walk to take more than one page"
    assert offsets == sorted(offsets) and offsets[0] == 0
    assert "".join(chunks) == full_text
    assert calls == [VIDEO]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: "AAAA" + c[4:], id="tampered-hash"),
        pytest.param(lambda c: "not-a-cursor", id="malformed"),
        pytest.param(lambda c: c.rsplit(":", 1)[0] + ":99999", id="offset-past-end"),
    ],
)
def test_stale_cursor_is_rejected_with_cursor_stale(
    session, cache, monkeypatch, mutate
):
    """A cursor that does not validate against the served content — tampered
    hash, malformed, or out-of-range offset — is a clean tool-level error
    telling the caller to restart pagination (never a crash, never wrong
    content)."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, WORDLIST))
    _shrink_page_budget(monkeypatch, inline=100, chunk=100)

    page = session.call_payload("get_youtube_transcript", {"url": VIDEO})
    assert page["status"] == "partial"

    payload = session.call_payload(
        "get_youtube_transcript",
        {"url": VIDEO, "lang": "en", "cursor": mutate(page["next_cursor"])},
    )
    assert payload["status"] == "error"
    assert payload["error_code"] == "cursor_stale"
    assert "without a cursor" in payload["message"]


def test_cursor_is_bound_to_content_and_video(session, cache, monkeypatch):
    """The cursor hash encodes (content, lang, source): a cursor carried to a
    different video, or to the same video after its cached content changed,
    is stale — pagination can never serve page 2 of something else."""
    calls: list = []

    def _install(words):
        monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, words))
        _shrink_page_budget(monkeypatch, inline=100, chunk=100)

    # Page 1 of VIDEO_A.
    _install(WORDLIST)
    page_a = session.call_payload("get_youtube_transcript", {"url": VIDEO})
    assert page_a["status"] == "partial"

    # (a) A's cursor against a different video → stale.
    other_words = [w + "zz" for w in WORDLIST]
    _install(other_words)
    page_b = session.call_payload("get_youtube_transcript", {"url": VIDEO2})
    assert page_b["status"] == "partial"
    cross = session.call_payload(
        "get_youtube_transcript",
        {"url": VIDEO2, "lang": "en", "cursor": page_a["next_cursor"]},
    )
    assert cross["status"] == "error"
    assert cross["error_code"] == "cursor_stale"

    # (b) The unit is refreshed under A (re-fetch overwrote the cache) —
    # A's old cursor must not serve page 2 of the new content.
    refreshed_words = [w + "yy" for w in WORDLIST]
    _install(refreshed_words)
    page_a2 = session.call_payload("get_youtube_transcript", {"url": VIDEO})
    assert page_a2["status"] == "partial"  # new content, new page 1
    stale = session.call_payload(
        "get_youtube_transcript",
        {"url": VIDEO, "lang": "en", "cursor": page_a["next_cursor"]},
    )
    assert stale["status"] == "error"
    assert stale["error_code"] == "cursor_stale"


# ---------------------------------------------------------------------------
# Cache hits — served without a fetch, free of rate-limit charge
# ---------------------------------------------------------------------------


def test_cache_hit_refetches_nothing_and_spends_no_rate_token(
    session, cache, monkeypatch
):
    """A repeat call with the served lang is answered from the cache unit:
    no second fetch, and no rate-limit spend even with a one-token bucket —
    while a fresh miss under the same exhausted bucket is denied
    rate_limited (README: "Charged only on the cache-miss fetch path —
    cache hits … are free")."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _caption_fetch(calls, SHORT_WORDS))
    monkeypatch.setattr(
        server,
        "_rate_limiter",
        SubjectRateLimiter(capacity=1, refill_rate_per_sec=0.0),
    )

    miss = session.call_payload(
        "get_youtube_transcript", {"url": VIDEO, "lang": "en"}
    )
    assert miss["status"] == "ok"  # spent the only token

    hit = session.call_payload(
        "get_youtube_transcript", {"url": VIDEO, "lang": "en"}
    )
    assert hit["status"] == "ok"  # served anyway — hits are free
    assert hit["text"] == miss["text"]
    assert hit["lang"] == miss["lang"]

    fresh = session.call_payload(
        "get_youtube_transcript", {"url": VIDEO2, "lang": "en"}
    )
    assert fresh["status"] == "error"
    assert fresh["error_code"] == "rate_limited"  # the bucket really is empty

    assert calls == [VIDEO]  # exactly one fetch across all three calls


# ---------------------------------------------------------------------------
# Whisper pending / done / error through get_transcript_job
# ---------------------------------------------------------------------------


def test_no_captions_starts_one_pending_whisper_job(
    session, cache, registry, monkeypatch
):
    """A caption-less video answers pending with a Whisper ETA and creates
    exactly one registry job (README: "Auto-starts Whisper ASR if no
    captions exist")."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _captionless_fetch(calls))
    gate: dict = {"release": False}
    try:
        monkeypatch.setattr(ytt.whisper, "run_whisper_job", _gated_whisper_run(gate))
        created = _capture_created_jobs(monkeypatch, registry)

        payload = session.call_payload(
            "get_youtube_transcript", {"url": VIDEO, "lang": "en"}
        )

        assert payload["status"] == "pending"
        assert payload["video_id"] == VIDEO
        assert "Whisper" in payload["message"]
        assert calls == [VIDEO]
        assert created == [(VIDEO, True)]  # exactly one new job

        poll = session.call_payload("get_transcript_job", {"video_id": VIDEO})
        assert poll["status"] == "pending"
        assert "queued" in poll["message"]
    finally:
        gate["release"] = True  # never strand the shared ASR slot


def test_whisper_done_delivers_transcript_directly(
    session, cache, registry, monkeypatch
):
    """When the job finishes, get_transcript_job returns the transcript in
    the get_youtube_transcript shape (plan: "collapsing 3 calls to 2") —
    source whisper, the ASR text, cache-served."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _captionless_fetch(calls))
    gate: dict = {"release": False, "text": "asr produced these words"}
    try:
        monkeypatch.setattr(ytt.whisper, "run_whisper_job", _gated_whisper_run(gate))

        started = session.call_payload(
            "get_youtube_transcript", {"url": VIDEO, "lang": "en"}
        )
        assert started["status"] == "pending"

        gate["release"] = True  # let the "transcription" complete
        poll = _poll_until(session, VIDEO, {"ok", "error"})
        assert poll["status"] == "ok", poll
        assert poll["text"] == gate["text"]
        assert poll["lang"] == "whisper"
        assert poll["source"] == "whisper"
        assert poll["transcript_quality"] == (
            "ASR (Whisper) — may contain errors, no speaker labels"
        )
    finally:
        gate["release"] = True


def test_whisper_error_state_is_relayed(session, cache, registry, monkeypatch):
    """A failed ASR job surfaces through the poll as the tool-level error
    shape with the job's own error_code and message (README: "On
    status='error' … call get_youtube_transcript again … to restart")."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _captionless_fetch(calls))
    monkeypatch.setattr(
        ytt.whisper,
        "run_whisper_job",
        _failing_whisper_run(error_code="asr_failed", message="ASR service exploded"),
    )

    started = session.call_payload(
        "get_youtube_transcript", {"url": VIDEO, "lang": "en"}
    )
    assert started["status"] == "pending"  # the job was accepted…

    poll = _poll_until(session, VIDEO, {"error"})
    assert poll["status"] == "error"
    assert poll["error_code"] == "asr_failed"
    # The job's own message is relayed (composition with the fixed re-call
    # instruction is pinned by test_whisper_contract.py).
    assert poll["message"].startswith("ASR service exploded")
    assert "Re-call get_youtube_transcript" in poll["message"]


def test_poll_unknown_video_is_not_found(session, cache, registry):
    """Polling a video with no job is a tool-level not_found that tells the
    caller how to restart — not a protocol error."""
    payload = session.call_payload("get_transcript_job", {"video_id": VIDEO})
    assert payload["status"] == "error"
    assert payload["error_code"] == "not_found"
    assert "get_youtube_transcript" in payload["message"]


def test_done_job_with_evicted_cache_answers_not_found(
    session, cache, registry
):
    """A done job whose whisper cache unit is gone (evicted between
    completion and polling) is not_found and the dead registry entry is
    removed — the caller is sent back to get_youtube_transcript."""
    registry._jobs[VIDEO] = WhisperJob(
        video_id=VIDEO, status="done", created_at=time.time()
    )

    payload = session.call_payload("get_transcript_job", {"video_id": VIDEO})
    assert payload["status"] == "error"
    assert payload["error_code"] == "not_found"
    assert "evicted" in payload["message"]
    assert VIDEO not in registry._jobs  # the dead entry was cleaned up


def test_second_call_joins_the_in_flight_job(session, cache, registry, monkeypatch):
    """A second caller (any alias) of a caption-less video joins the running
    job instead of starting a second one — one get_or_create, one job, two
    pending answers (Invariant 2)."""
    calls: list = []
    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _captionless_fetch(calls))
    gate: dict = {"release": False}
    try:
        monkeypatch.setattr(ytt.whisper, "run_whisper_job", _gated_whisper_run(gate))
        created = _capture_created_jobs(monkeypatch, registry)

        first = session.call_payload(
            "get_youtube_transcript", {"url": f"https://youtu.be/{VIDEO}", "lang": "en"}
        )
        second = session.call_payload(
            "get_youtube_transcript", {"url": VIDEO, "lang": "en"}
        )

        assert first["status"] == "pending"
        assert second["status"] == "pending"
        assert calls == [VIDEO, VIDEO]  # two caption probes …
        assert created == [(VIDEO, True), (VIDEO, False)]  # … one job
    finally:
        gate["release"] = True


# ---------------------------------------------------------------------------
# Authorization before transcript work
# ---------------------------------------------------------------------------


def test_unauthenticated_transport_is_a_401_challenge(client):
    """No token → 401 + Bearer challenge pointing at the RFC 9728 metadata
    (README: "Auth required: OAuth 2.1 with a subject allowlist")."""
    resp = client.post(
        "/ytt",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "anon", "version": "0"},
            },
        },
        headers=ACCEPT,
    )
    assert resp.status_code == 401
    challenge = resp.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer"), challenge
    assert 'resource_metadata="' in challenge


def test_non_allowlisted_subject_is_denied_before_any_transcript_work(
    client, cache, registry, monkeypatch
):
    """A valid token for a subject outside YTT_ALLOWED_SUBJECTS gets an
    empty tools/list and isError denials on both tools — with the whole
    transcript pipeline silent. The same session then serving an allowlisted
    call proves the denial was the allowlist, not a broken server."""
    calls: list = []

    def _install_pipeline(words):
        monkeypatch.setattr(
            ytt.fetch, "fetch_transcript", _caption_fetch(calls, words)
        )

        original_put = cache.put

        async def _put_spy(*args, **kwargs):
            calls.append(f"cache_put:{args[0]}")
            return await original_put(*args, **kwargs)

        monkeypatch.setattr(cache, "put", _put_spy)

        original_run = ytt.whisper.run_whisper_job

        async def _run_spy(*args, **kwargs):
            calls.append("run_whisper_job")
            return await original_run(*args, **kwargs)

        monkeypatch.setattr(ytt.whisper, "run_whisper_job", _run_spy)

    _install_pipeline(SHORT_WORDS)
    _bearer_as(monkeypatch, STRANGER)

    s = McpSession(client)
    s.start()  # the handshake itself is not subject-gated

    listing = s.request("tools/list")
    assert listing["result"]["tools"] == []  # fail-closed discovery

    for name, arguments in (
        ("get_youtube_transcript", {"url": VIDEO, "lang": "en"}),
        ("get_transcript_job", {"video_id": VIDEO}),
    ):
        result = s.call(name, arguments)
        assert result.get("isError") is True, f"{name}: {result}"
        assert "structuredContent" not in result
        assert "Authorization failed" in result["content"][0]["text"]

    assert calls == []  # no fetch, no cache write, no ASR run

    # The allowlisted subject works through the very same session.
    _bearer_as(monkeypatch, SUBJECT)
    payload = s.call_payload(
        "get_youtube_transcript", {"url": VIDEO, "lang": "en"}
    )
    assert payload["status"] == "ok"
    # and only now did the pipeline run: one fetch, one cache write
    assert calls == [VIDEO, f"cache_put:{VIDEO}"]
