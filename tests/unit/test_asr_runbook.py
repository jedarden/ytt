"""ASR runbook pin — the operator contract in deploy/ASR-RUNBOOK.md.

Unit-level pin of the ASR failure & queue-exhaustion runbook (bead
``ytt-e372b1cd``).  Every remediation decision in that runbook leans on a
behavior that can be checked mechanically, so the behaviors are exercised
here, organized by the runbook's scenarios:

Acceptance scenarios (§5/§6/§7/§8 of the runbook):

- **Whisper unset / no-Whisper mode** — the declared default is the
  reference in-cluster endpoint (unset ≠ disabled, and the default matches
  ``docs/usage/configuration.md``); an empty URL still answers ``pending``
  and then fails ``asr_failed`` *without ever contacting or caching
  anything*; boot tolerates an absent service (model guard swallows the
  connection error).
- **Whisper unreachable** — connection refused maps to ``asr_failed`` with
  the relayable "Whisper service request failed" message; nothing is
  cached; the scratch audio is swept; the documented retry (re-kick)
  replaces the terminal handle with fresh work.
- **Whisper overloaded** — 429/503 and read-timeouts map to ``asr_failed``
  with the status in the message; a real 16-deep backlog denies *new* jobs
  with the exact "Whisper queue full (16/16 …)" denial while joining an
  in-flight job stays admitted and free, and polling stays free even with a
  fail-closed quota.
- **GC & restart (§9/§10)** — TTL GC reaps exactly the documented entries;
  stale-running GC's 6480 s threshold reaps zombies but not legitimately
  long jobs; a simulated restart empties the registry so polls become
  ``not_found`` and the re-kick starts fresh; the boot sweep clears scratch.

Drift guards (the ``test_deletion_runbook`` pattern): the runbook must keep
the literals its procedures depend on, the manifest's Whisper env block must
keep matching the runbook's §2 table, and the declared Settings defaults
must keep matching both.

Downstream details intentionally *not* duplicated here: the OpenAI wire
contract (``test_whisper_asr_contract.py``), the full polling/restart
contract (``test_whisper_contract.py``), and the gate shapes via stubbed
registries (``test_server.py``).
"""

from __future__ import annotations

import inspect
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from ytt import errors
from ytt.config import Settings
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.whisper import (
    WhisperJobRegistry,
    check_model_guard,
    run_whisper_job,
    startup_sweep,
)

VIDEO_ID = "dQw4w9WgXcQ"

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "deploy" / "ASR-RUNBOOK.md"
CONFIG_DOC = REPO_ROOT / "docs" / "usage" / "configuration.md"
MANIFEST_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"

#: The declared default — "unset never means disabled" (runbook §5).
DEFAULT_WHISPER_URL = "http://whisper-openai.whisper-stt.svc.cluster.local:8000"


@pytest.fixture(autouse=True)
def _bypass_authz(monkeypatch):
    """Tool-surface drives (the §8 backlog denial) run with no HTTP request,
    so there is no Google-verified token to resolve — bypass the
    AuthMiddleware gate exactly as tests/unit/test_server.py does. Authz
    itself is pinned by test_authz_tool_gate.py; this module tests the ASR
    behavior *behind* the gate.
    """
    from fastmcp.server.middleware.authorization import AuthMiddleware

    from ytt.server import mcp

    for mw in mcp.middleware:
        if isinstance(mw, AuthMiddleware):
            monkeypatch.setattr(mw, "auth", lambda ctx: True)

VERBOSE_JSON: dict[str, Any] = {
    "text": "hello",
    "language": "en",
    "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello"}],
}


# --------------------------------------------------------------------------- #
# Job scaffolding (same shape as test_whisper_asr_contract.py)                 #
# --------------------------------------------------------------------------- #


def _settings(scratch_dir: str, *, whisper_url: str = DEFAULT_WHISPER_URL) -> MagicMock:
    s = MagicMock()
    s.max_asr_duration_sec = 1200
    s.whisper_realtime_factor = 2.0
    s.job_ttl_sec = 3600
    s.whisper_timeout_sec = 2880
    s.max_audio_bytes = 500 * 1024 * 1024
    s.scratch_dir = scratch_dir
    s.whisper_url = whisper_url
    s.proxy_url = None
    return s


def _cache_mock() -> MagicMock:
    cache = MagicMock()
    cache.put = AsyncMock(return_value=True)
    return cache


def _asr_client(
    respond: Any,  # async callable Request -> Response; may raise instead
) -> httpx.AsyncClient:
    async def handle(request: httpx.Request) -> httpx.Response:
        await request.aread()
        result = respond(request)
        if inspect.isawaitable(result):
            result = await result
        return result

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def _run_job(
    registry: WhisperJobRegistry,
    settings: MagicMock,
    cache: MagicMock,
    client: httpx.AsyncClient,
    *,
    video_id: str = VIDEO_ID,
) -> Any:
    """Drive one real job end to end; download stubbed, POST via *client*."""
    scratch = Path(settings.scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    audio = scratch / f"{video_id}.m4a"
    audio.write_bytes(b"\x89fake-audio")

    job, is_new = await registry.get_or_create(video_id, 60.0, settings, owner="anonymous")
    assert is_new  # fresh work, as every re-kick in the runbook is
    with patch("ytt.whisper._do_download_audio", return_value=str(audio)):
        await run_whisper_job(
            job, registry, settings, cache, "large-v3-turbo", http_client=client
        )
    final = await registry.get(video_id)
    assert final is not None
    return final


# --------------------------------------------------------------------------- #
# Drift guards (test_deletion_runbook pattern)                                 #
# --------------------------------------------------------------------------- #


def test_runbook_quotes_the_facts_it_depends_on() -> None:
    """The runbook must keep carrying the literals its procedures rely on.

    A doc refactor that dropped a log-event name, an error code, a knob, or
    the metric-honesty table would leave operators with procedures that
    reference nothing findable.
    """
    doc = RUNBOOK.read_text(encoding="utf-8")
    for literal in (
        # the log trail (§4) — every event the procedures grep for
        "whisper_job_created",
        "whisper_job_replaced_terminal",
        "whisper_job_status_change",
        "whisper_job_done",
        "whisper_job_error",
        "whisper_job_unexpected_error",
        "whisper_job_stale_running",
        "whisper_job_ttl_gc",
        "whisper_scratch_swept",
        "audio_size_unknown",
        # the error-code vocabulary (§3)
        "asr_failed",
        "rate_limited",
        "too_long_for_asr",
        "not_found",
        # the two denial messages (§3/§8)
        "Whisper queue full",
        "Whisper ASR quota exhausted",
        # knobs and thresholds (§2/§9/§11)
        "YTT_WHISPER_URL",
        "YTT_MAX_PENDING_WHISPER_JOBS",
        "YTT_MAX_CONCURRENT_WHISPER",
        "YTT_WHISPER_JOBS_PER_HOUR",
        "YTT_WHISPER_TIMEOUT_SEC",
        "YTT_MAX_ASR_DURATION_SEC",
        "YTT_WHISPER_REALTIME_FACTOR",
        "YTT_JOB_TTL_SEC",
        "6480",
        "run_ttl_gc",
        "startup_sweep",
        DEFAULT_WHISPER_URL,
        # metrics honesty (§4) — the live one and the inert ones
        "ytt_rate_limited_total",
        "ytt_whisper_errors_total",
        "ytt_whisper_job_seconds",
        "ytt_queue_depth",
        "YttWhisperDown",
        "never incremented",
        # where things live
        "whisper-stt",
        "deploy/k8s/ardenone-cluster/ytt/deployment.yml",
        "docs/usage/configuration.md",
        "docs/notes/whisper-lifecycle.md",
        "tests/unit/test_asr_runbook.py",  # this pin (§12)
        "tests/unit/test_server.py",
        "ytt-4f1c45c2",  # the wiring-gap bead §9/§10 cite
    ):
        assert literal in doc, f"ASR-RUNBOOK.md lost {literal!r}"


def test_runbook_capacity_table_matches_manifest() -> None:
    """§2's manifest column quotes the deployment; the deployment can't drift.

    If this fails after a manifest change, update deploy/ASR-RUNBOOK.md §2
    in the same commit.
    """
    docs = [
        d
        for d in yaml.safe_load_all(
            (MANIFEST_DIR / "deployment.yml").read_text(encoding="utf-8")
        )
        if d
    ]
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    container = next(
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ytt"
    )
    env = {e["name"]: e.get("value", "") for e in container.get("env", [])}

    assert env["YTT_WHISPER_URL"] == DEFAULT_WHISPER_URL
    assert env["YTT_WHISPER_MODEL"] == "large-v3-turbo"
    assert env["YTT_WHISPER_TIMEOUT_SEC"] == "2880"
    assert env["YTT_MAX_ASR_DURATION_SEC"] == "1200"
    assert env["YTT_WHISPER_REALTIME_FACTOR"] == "2.0"
    assert env["YTT_MAX_CONCURRENT_WHISPER"] == "1"
    assert env["YTT_WHISPER_JOBS_PER_HOUR"] == "10"
    assert env["YTT_MAX_AUDIO_BYTES"] == "500Mi"
    assert env["YTT_SCRATCH_DIR"] == "/scratch"

    # The two caps the runbook marks "unset → default" are genuinely unset:
    # the manifest relies on the Settings defaults pinned below.
    assert "YTT_MAX_PENDING_WHISPER_JOBS" not in env
    assert "YTT_JOB_TTL_SEC" not in env


def test_settings_defaults_match_runbook() -> None:
    """The declared defaults behind the §2 table — schema-level, env-free."""
    fields = Settings.model_fields
    assert fields["whisper_url"].default == DEFAULT_WHISPER_URL
    assert fields["whisper_model"].default == "large-v3-turbo"
    assert fields["max_pending_whisper_jobs"].default == 16
    assert fields["whisper_jobs_per_hour"].default == 10
    assert fields["max_concurrent_whisper"].default == 1
    assert fields["whisper_timeout_sec"].default == 2880
    assert fields["max_asr_duration_sec"].default == 1200
    assert fields["whisper_realtime_factor"].default == 2.0
    assert fields["job_ttl_sec"].default == 3600

    # Invariant 7's margin, quoted in §2: 1200 × 2.0 = 2400 < 2880.
    bound = fields["max_asr_duration_sec"].default * fields[
        "whisper_realtime_factor"
    ].default
    assert bound < fields["whisper_timeout_sec"].default


def test_declared_whisper_default_matches_configuration_doc() -> None:
    """The configuration guide's YTT_WHISPER_URL default cell can't drift."""
    row = next(
        line
        for line in CONFIG_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `YTT_WHISPER_URL` |")
    )
    assert DEFAULT_WHISPER_URL in row


# --------------------------------------------------------------------------- #
# Acceptance: Whisper unset / no-Whisper mode (runbook §5)                     #
# --------------------------------------------------------------------------- #


def test_unset_whisper_url_declares_reference_default_not_disabled() -> None:
    """'Unset' points at the reference service — it never means disabled."""
    assert Settings.model_fields["whisper_url"].default == DEFAULT_WHISPER_URL


@pytest.mark.asyncio
async def test_empty_whisper_url_answers_pending_then_asr_failed_never_caches(
    tmp_path: Path,
) -> None:
    """An empty YTT_WHISPER_URL (the no-Whisper deployment) fails closed.

    The caller still gets ``pending`` first — job creation never probes
    Whisper — and the poll then ends ``asr_failed`` with the
    request-failed message.  Nothing is cached (errors are not
    transcripts), and the scratch audio is swept.
    """
    registry = WhisperJobRegistry()
    settings = _settings(str(tmp_path / "scratch"), whisper_url="")
    cache = _cache_mock()

    final = await _run_job(
        registry, settings, cache, httpx.AsyncClient()
    )

    assert final.status == "error"
    assert final.error_code == errors.ASR_FAILED
    assert "Whisper service request failed" in (final.message or "")
    assert cache.put.await_count == 0  # a failed job writes no cache unit
    assert not list(Path(settings.scratch_dir).glob(f"{VIDEO_ID}.*"))


@pytest.mark.asyncio
async def test_boot_tolerates_an_absent_whisper_service() -> None:
    """The model guard (startup probe) swallows connection errors — an
    unset/unreachable Whisper never blocks startup (runbook §5)."""
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    model = await check_model_guard(
        DEFAULT_WHISPER_URL, "large-v3-turbo", http_client=_asr_client(refuse)
    )
    assert model == "large-v3-turbo"  # configured name kept, no raise


# --------------------------------------------------------------------------- #
# Acceptance: Whisper unreachable (runbook §6)                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_connection_refused_fails_relayable_without_caching(
    tmp_path: Path,
) -> None:
    """Service down → asr_failed, relayable message, nothing cached, audio swept."""
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    registry = WhisperJobRegistry()
    settings = _settings(str(tmp_path / "scratch"))
    cache = _cache_mock()

    final = await _run_job(
        registry, settings, cache, _asr_client(refuse)
    )

    assert final.status == "error"
    assert final.error_code == errors.ASR_FAILED
    message = final.message or ""
    assert "Whisper service request failed" in message
    assert "Connection refused" in message  # the operator's one clue
    assert "Traceback" not in message       # verbatim-relayable
    assert cache.put.await_count == 0
    assert not list(Path(settings.scratch_dir).glob(f"{VIDEO_ID}.*"))


@pytest.mark.asyncio
async def test_rekick_after_failure_replaces_the_terminal_handle(
    tmp_path: Path,
) -> None:
    """The documented retry restarts real work — it never dead-ends on the
    old error entry (runbook §3: terminal entries are never joinable)."""
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    registry = WhisperJobRegistry()
    settings = _settings(str(tmp_path / "scratch"))

    await _run_job(registry, settings, _cache_mock(), _asr_client(refuse))
    assert (await registry.get(VIDEO_ID)).status == "error"

    job, is_new = await registry.get_or_create(VIDEO_ID, 60.0, settings, owner="anonymous")
    assert is_new
    assert job.status == "pending"
    assert registry.size == 1  # replaced, not duplicated


# --------------------------------------------------------------------------- #
# Acceptance: Whisper overloaded (runbook §7)                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("status", "shown"), [(429, "429"), (503, "503")])
@pytest.mark.asyncio
async def test_overloaded_service_status_lands_in_the_message(
    tmp_path: Path, status: int, shown: str
) -> None:
    """429/503 shed-load responses → asr_failed naming the status (§7)."""
    async def shed_load(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "overloaded"})

    registry = WhisperJobRegistry()
    settings = _settings(str(tmp_path / "scratch"))
    cache = _cache_mock()

    final = await _run_job(
        registry, settings, cache, _asr_client(shed_load)
    )

    assert final.status == "error"
    assert final.error_code == errors.ASR_FAILED
    assert f"Whisper service error {shown}" in (final.message or "")
    assert cache.put.await_count == 0


@pytest.mark.asyncio
async def test_saturated_service_timeout_fails_asr_failed(tmp_path: Path) -> None:
    """Alive-but-slow service blowing the read timeout → asr_failed (§7)."""
    async def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    registry = WhisperJobRegistry()
    settings = _settings(str(tmp_path / "scratch"))

    final = await _run_job(
        registry, settings, _cache_mock(), _asr_client(hang)
    )

    assert final.status == "error"
    assert final.error_code == errors.ASR_FAILED
    assert "Whisper service timed out" in (final.message or "")


# --------------------------------------------------------------------------- #
# Acceptance: queue exhaustion at the backlog cap (runbook §8)                 #
# --------------------------------------------------------------------------- #


def _video_n(n: int) -> str:
    return f"abc{n:08d}"  # 11 characters, registry-keyed


@pytest.mark.asyncio
async def test_full_backlog_denies_new_jobs_but_joins_and_free_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real 16-deep backlog produces the exact §8 denial shape — while
    joining stays admitted and free and polling stays free even with a
    fail-closed quota.

    Unlike test_server.py's stubbed-registry gates, the backlog here is a
    real WhisperJobRegistry, so ``active_count``'s pending+running math is
    exercised end to end through the tool surface.
    """
    from ytt import server
    from ytt.errors import EMPTY_BODY, YttError
    from ytt.server import mcp

    reg = WhisperJobRegistry()
    monkeypatch.setattr(server, "whisper_registry", reg)
    monkeypatch.setattr(
        server,
        "_rate_limiter",
        SubjectRateLimiter(capacity=50, refill_rate_per_sec=1.0),
    )
    quota = WhisperQuota(jobs_per_hour=1)
    monkeypatch.setattr(server, "_whisper_quota", quota)

    async def cache_miss(video_id: str, lang: str) -> None:
        return None

    async def fetch_no_captions(fn, video_id=None):
        raise YttError(EMPTY_BODY, "no captions")

    monkeypatch.setattr(server.transcript_cache, "get", cache_miss)
    monkeypatch.setattr(server._concurrency.fetch_pool, "run", fetch_no_captions)

    s = _settings("/tmp")  # only max_asr_duration/realtime_factor are read
    for i in range(16):
        job, _ = await reg.get_or_create(_video_n(i), 60.0, s, owner="anonymous")
        await reg.update_status(_video_n(i), "running")
    assert await reg.active_count() == 16

    # New caption-less request → the §8 denial, no quota spent.
    result = await mcp.call_tool(
        "get_youtube_transcript", {"url": f"https://youtu.be/{VIDEO_ID}"}
    )
    sc = result.structured_content
    assert sc["status"] == "error"
    assert sc["error_code"] == "rate_limited"
    assert "Whisper queue full" in sc["message"]
    assert "16/16" in sc["message"]
    assert quota.consume("anonymous") is True  # the denial spent nothing

    # Joining an in-flight job at full backlog: admitted, and the pre-charge
    # is refunded. Restore the proof token the assertion above spent, so the
    # join has a slot to charge (bucket 1 → 0) and refund (0 → 1); a leak —
    # charge kept on the join path — would leave 0 and fail the final probe.
    quota.refund("anonymous")
    job, _ = await reg.get_or_create(VIDEO_ID, 60.0, s, owner="anonymous")
    await reg.update_status(VIDEO_ID, "running")
    result = await mcp.call_tool(
        "get_youtube_transcript", {"url": f"https://youtu.be/{VIDEO_ID}"}
    )
    assert result.structured_content["status"] == "pending"
    assert quota.consume("anonymous") is True  # refunded

    # Polls are free even fail-closed: quota 0 denies every new job, yet the
    # queued/running jobs stay pollable (runbook §3/§8) — _video_n(0) was put
    # in `running` while filling the backlog, and the poll answers with its
    # remaining ETA and never touches the quota gate.
    monkeypatch.setattr(server, "_whisper_quota", WhisperQuota(jobs_per_hour=0))
    result = await mcp.call_tool("get_transcript_job", {"video_id": _video_n(0)})
    polled = result.structured_content
    assert polled["status"] == "running"
    assert polled["eta_sec"] == 120.0  # 60 s × the 2.0 realtime factor


# --------------------------------------------------------------------------- #
# GC & restart (runbook §9/§10)                                                #
# --------------------------------------------------------------------------- #


def _gc_settings() -> MagicMock:
    s = MagicMock()
    s.job_ttl_sec = 3600
    s.whisper_timeout_sec = 2880
    return s


@pytest.mark.asyncio
async def test_ttl_gc_reaps_exactly_the_documented_entries(tmp_path: Path) -> None:
    """done/error older than YTT_JOB_TTL_SEC go; fresh terminal and any
    pending stay — pending is never TTL'd (runbook §9 table)."""
    reg = WhisperJobRegistry()
    s = _settings(str(tmp_path))

    old_done, _ = await reg.get_or_create("aaadone0001", 60.0, s, owner="anonymous")
    await reg.update_status("aaadone0001", "done", result_ref="aaadone0001.whisper")
    old_done.created_at = time.time() - 7200
    fresh_error, _ = await reg.get_or_create("aaaerror001", 60.0, s, owner="anonymous")
    await reg.update_status("aaaerror001", "error", error_code="asr_failed")
    aged_pending, _ = await reg.get_or_create("aaapend0001", 60.0, s, owner="anonymous")
    aged_pending.created_at = time.time() - 10**7

    removed = await reg.run_ttl_gc(_gc_settings())

    assert removed == 1
    assert await reg.get("aaadone0001") is None
    assert (await reg.get("aaaerror001")).status == "error"
    assert (await reg.get("aaapend0001")).status == "pending"


@pytest.mark.asyncio
async def test_stale_running_gc_threshold_is_timeout_plus_ttl(
    tmp_path: Path,
) -> None:
    """A running handle past YTT_WHISPER_TIMEOUT_SEC + YTT_JOB_TTL_SEC
    (6480 s) is a zombie; a legitimately long job under the threshold and
    any pending job are kept (runbook §9)."""
    reg = WhisperJobRegistry()
    s = _settings(str(tmp_path))

    zombie, _ = await reg.get_or_create("aaazomb0001", 60.0, s, owner="anonymous")
    await reg.update_status("aaazomb0001", "running")
    zombie.started_at = time.time() - 7000  # > 2880 + 3600
    long_but_legit, _ = await reg.get_or_create("aaalong0001", 60.0, s, owner="anonymous")
    await reg.update_status("aaalong0001", "running")
    long_but_legit.started_at = time.time() - 6000  # under the threshold

    removed = await reg.run_ttl_gc(_gc_settings())

    assert removed == 1
    assert await reg.get("aaazomb0001") is None
    assert (await reg.get("aaalong0001")).status == "running"


@pytest.mark.asyncio
async def test_restart_empties_the_registry_and_rekick_starts_fresh(
    tmp_path: Path,
) -> None:
    """A swap replaces the process: the new registry has nothing, so polls
    would answer not_found and the documented re-kick starts new work
    (runbook §10)."""
    old_reg = WhisperJobRegistry()
    s = _settings(str(tmp_path))
    await old_reg.get_or_create(VIDEO_ID, 60.0, s, owner="anonymous")

    new_reg = WhisperJobRegistry()  # the process after the swap
    assert await new_reg.get(VIDEO_ID) is None  # → not_found on the poll path

    job, is_new = await new_reg.get_or_create(VIDEO_ID, 60.0, s, owner="anonymous")
    assert is_new
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_startup_sweep_clears_stale_scratch(tmp_path: Path) -> None:
    """§10's boot sweep: every file in the scratch dir goes, count and
    bytes reported, directories untouched."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "aaabbbccc00.m4a").write_bytes(b"x" * 100)
    (scratch / "dddEEEfff00.m4a").write_bytes(b"y" * 50)
    subdir = scratch / "keepme"
    subdir.mkdir()

    deleted, freed = startup_sweep(str(scratch))

    assert (deleted, freed) == (2, 150)
    assert sorted(p.name for p in scratch.iterdir()) == ["keepme"]
