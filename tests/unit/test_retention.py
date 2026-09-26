"""Cross-component retention regression tests — docs/notes/retention-policy.md.

The per-mechanism suites assert each cleanup in isolation (test_cache.py for
eviction, test_whisper.py for the FSM/GC/sweeps). The failure mode this module
exists for is different: one component's retention mechanism reaching into
another component's data. So every test here wires **real** cache + real
scratch + real registry together and holds the policy boundary:

- Job expiry (TTL GC, stale-running GC) removes registry entries and never
  touches a cached transcript — expired jobs and valid transcripts are
  independent (policy §4).
- Temporary audio is removed on every job exit path, and neither the
  per-video sweep nor the startup sweep can reach the cache volume or another
  video's scratch (policy §3).
- LRU eviction removes whole units; untouched survivors still serve
  byte-identical text (policy §2).
- A restart-equivalent (fresh registry + startup sweep + startup_scan over
  the same volumes) loses job records and audio but serves previously cached
  transcripts (policy §5).
- retention-policy.md names every mechanism, env var, and log event it
  claims, and each named thing exists in the code — the prose and the code
  rot together or not at all (policy §8).

Coverage:
- run_ttl_gc: expired done job GC'd; its own and other videos' cached
  transcripts still served, files intact
- run_ttl_gc: expired error jobs GC'd; surviving unit count unchanged
- run_ttl_gc: stale running job GC'd; cached transcript still served
- run_ttl_gc: fresh terminal jobs not GC'd; cache untouched
- run_whisper_job: success → zero scratch files, whisper unit cached, unit
  serves via the whisper-fallback lookup
- run_whisper_job: ASR failure → partial audio swept, nothing cached for the
  failed video, another video's pre-existing unit byte-identical
- run_whisper_job: unexpected failure → same hygiene
- _sweep_video_scratch: scoped to its own video's glob only
- startup_sweep: empties scratch, cannot reach the cache volume
- TranscriptCache eviction: whole-unit removal, survivor serves identical text
- TranscriptCache.startup_scan: stray .tmp cleaned, real units kept
- Restart equivalence: jobs + audio gone, cached transcript still served
- retention-policy.md ↔ code: env vars, functions, log events all present
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from ytt import errors
from ytt.cache import TranscriptCache
from ytt.errors import YttError
from ytt.models import WhisperJob
from ytt.whisper import (
    WhisperJobRegistry,
    _sweep_video_scratch,
    run_whisper_job,
    startup_sweep,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RETENTION_DOC = REPO_ROOT / "docs" / "notes" / "retention-policy.md"

VIDEO_A = "dQw4w9WgXcQ"
VIDEO_B = "abc12345678"
VIDEO_C = "xyz98765432"

#: Creator subject for jobs these tests create via ``get_or_create`` (the API
#: requires one). Ownership is irrelevant to the retention boundaries under
#: test — no assertion here depends on which subject polls — so one constant
#: is as good as any; the ownership rule itself lives in test_job_ownership.
OWNER = "caller@example.com"

TEXT_A = "Transcript for video A — must survive every retention mechanism."
TEXT_B = "Transcript for video B — the bystander unit."
TEXT_C = "Transcript for video C — the evictor unit."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(
    *,
    scratch_dir: str = "/tmp/ytt_test_scratch",
    job_ttl_sec: int = 3600,
    whisper_timeout_sec: int = 2880,
    max_audio_bytes: int = 500 * 1024 * 1024,
) -> MagicMock:
    s = MagicMock()
    s.scratch_dir = scratch_dir
    s.job_ttl_sec = job_ttl_sec
    s.whisper_timeout_sec = whisper_timeout_sec
    s.max_audio_bytes = max_audio_bytes
    s.whisper_url = "http://whisper.local:8000"
    s.whisper_model = "Systran/faster-whisper-small"
    s.whisper_realtime_factor = 1.2
    s.max_asr_duration_sec = 1200
    s.proxy_url = None
    return s


def _whisper_response(text: str, language: str = "en") -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "text": text,
        "language": language,
        "segments": [{"start": 0.0, "end": 2.0, "text": text}],
    }
    return response


def _job(
    video_id: str,
    status: str,
    *,
    age_sec: float = 0.0,
    started: bool = False,
) -> WhisperJob:
    now = time.time()
    return WhisperJob(
        video_id=video_id,
        status=status,  # type: ignore[arg-type]
        created_at=now - age_sec,
        started_at=now - age_sec if started else None,
        eta_sec=None,
        duration_sec=None,
    )


async def _insert(registry: WhisperJobRegistry, job: WhisperJob) -> None:
    async with registry._lock:
        registry._jobs[job.video_id] = job


def _make_cache(path: Path, max_bytes: int = 8 << 20) -> TranscriptCache:
    """Cache over an existing directory — ``put`` writes .tmp files in place
    and does not mkdir, so create the directory up front (same prerequisite
    ``test_cache.py``'s fixtures establish via ``startup_scan``)."""
    path.mkdir(parents=True, exist_ok=True)
    return TranscriptCache(path, max_bytes=max_bytes)


async def _put(
    cache: TranscriptCache,
    video_id: str,
    text: str,
    *,
    lang: str = "en",
    source: str = "caption_auto",
) -> bool:
    return await cache.put(video_id, lang, text, None, source, None)


def _unit_files(cache_dir: Path, video_id: str) -> list[Path]:
    return sorted(cache_dir.glob(f"{video_id}.*"))


# ---------------------------------------------------------------------------
# Expired jobs never take cached transcripts with them (policy §4)
# ---------------------------------------------------------------------------


class TestExpiredJobs:
    async def test_expired_done_job_gc_leaves_its_transcript_served(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path / "cache")
        await _put(cache, VIDEO_A, TEXT_A)

        registry = WhisperJobRegistry()
        await _insert(registry, _job(VIDEO_A, "done", age_sec=7200))

        removed = await registry.run_ttl_gc(_settings(job_ttl_sec=3600))

        assert removed == 1
        assert await registry.get(VIDEO_A) is None
        # The transcript the done job pointed at is untouched — its retention
        # is governed by the cache size bound, never by the job TTL.
        hit = await cache.get(VIDEO_A, "en")
        assert hit is not None
        assert hit.text == TEXT_A
        assert _unit_files(tmp_path / "cache", VIDEO_A), "unit files must exist"

    async def test_expired_error_jobs_gc_keeps_other_videos_transcripts(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path / "cache")
        for vid, text in ((VIDEO_A, TEXT_A), (VIDEO_B, TEXT_B), (VIDEO_C, TEXT_C)):
            await _put(cache, vid, text)

        registry = WhisperJobRegistry()
        await _insert(registry, _job(VIDEO_A, "error", age_sec=7200))
        await _insert(registry, _job(VIDEO_B, "error", age_sec=7200))

        removed = await registry.run_ttl_gc(_settings(job_ttl_sec=3600))

        assert removed == 2
        assert cache.unit_count == 3
        assert (await cache.get(VIDEO_A, "en")).text == TEXT_A  # type: ignore[union-attr]
        assert (await cache.get(VIDEO_B, "en")).text == TEXT_B  # type: ignore[union-attr]
        assert (await cache.get(VIDEO_C, "en")).text == TEXT_C  # type: ignore[union-attr]

    async def test_stale_running_gc_keeps_cached_transcript(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path / "cache")
        await _put(cache, VIDEO_A, TEXT_A)

        registry = WhisperJobRegistry()
        # Stale = running longer than whisper_timeout + ttl. Tiny timeouts make
        # a job started 100s ago decisively stale.
        await _insert(
            registry,
            _job(VIDEO_A, "running", age_sec=100, started=True),
        )

        removed = await registry.run_ttl_gc(
            _settings(job_ttl_sec=10, whisper_timeout_sec=10)
        )

        assert removed == 1
        assert await registry.get(VIDEO_A) is None
        hit = await cache.get(VIDEO_A, "en")
        assert hit is not None and hit.text == TEXT_A

    async def test_fresh_terminal_jobs_not_gc_d_cache_untouched(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path / "cache")
        await _put(cache, VIDEO_A, TEXT_A)

        registry = WhisperJobRegistry()
        await _insert(registry, _job(VIDEO_A, "done"))
        await _insert(registry, _job(VIDEO_B, "error"))

        removed = await registry.run_ttl_gc(_settings(job_ttl_sec=3600))

        assert removed == 0
        assert await registry.get(VIDEO_A) is not None
        assert cache.unit_count == 1


# ---------------------------------------------------------------------------
# Audio cleanup never takes cached transcripts with it (policy §3)
# ---------------------------------------------------------------------------


class TestAudioCleanup:
    async def test_success_leaves_no_audio_and_caches_transcript(
        self, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        cache_dir = tmp_path / "cache"
        cache = _make_cache(cache_dir)
        audio = scratch / f"{VIDEO_A}.mp4"
        audio.write_bytes(b"fake audio")

        registry = WhisperJobRegistry()
        settings = _settings(scratch_dir=str(scratch))
        job, _ = await registry.get_or_create(VIDEO_A, 50.0, settings, owner=OWNER)
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=_whisper_response(TEXT_A))

        with patch("ytt.whisper._do_download_audio", return_value=str(audio)):
            await run_whisper_job(
                job, registry, settings, cache, "m", http_client=http_client
            )

        assert (await registry.get(VIDEO_A)).status == "done"  # type: ignore[union-attr]
        # Temporary audio is gone — scratch is empty.
        assert list(scratch.iterdir()) == []
        # The transcript is cached as the whisper unit and serves via the
        # whisper-fallback lookup (any lang).
        hit = await cache.get(VIDEO_A, "en")
        assert hit is not None
        assert hit.text == TEXT_A
        assert hit.source == "whisper"

    async def test_asr_failure_sweeps_audio_caches_nothing_keeps_bystander(
        self, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        cache_dir = tmp_path / "cache"
        cache = _make_cache(cache_dir)
        await _put(cache, VIDEO_B, TEXT_B)
        # Simulate the partial file a mid-stream-aborted downloader leaves:
        # audio_path was never assigned, so only the sweep can remove it.
        partial = scratch / f"{VIDEO_A}.mp4"
        partial.write_bytes(b"partial")

        registry = WhisperJobRegistry()
        settings = _settings(scratch_dir=str(scratch))
        job, _ = await registry.get_or_create(VIDEO_A, 50.0, settings, owner=OWNER)

        with patch(
            "ytt.whisper._do_download_audio",
            side_effect=YttError(errors.ASR_FAILED, "Audio download failed"),
        ):
            await run_whisper_job(job, registry, settings, cache, "m")

        final = await registry.get(VIDEO_A)
        assert final is not None
        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        # Scratch swept to empty despite audio_path never being assigned.
        assert list(scratch.iterdir()) == []
        # Errors are never cached as transcripts (plan Invariant 6)…
        assert await cache.get(VIDEO_A, "en") is None
        assert _unit_files(cache_dir, VIDEO_A) == []
        # …and the bystander unit is byte-identical and still on disk.
        hit = await cache.get(VIDEO_B, "en")
        assert hit is not None and hit.text == TEXT_B
        assert len(_unit_files(cache_dir, VIDEO_B)) == 2  # .txt + .json

    async def test_unexpected_failure_still_cleans_audio_and_keeps_bystander(
        self, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        cache_dir = tmp_path / "cache"
        cache = _make_cache(cache_dir)
        await _put(cache, VIDEO_B, TEXT_B)
        (scratch / f"{VIDEO_A}.webm").write_bytes(b"partial")

        registry = WhisperJobRegistry()
        settings = _settings(scratch_dir=str(scratch))
        job, _ = await registry.get_or_create(VIDEO_A, 50.0, settings, owner=OWNER)

        with patch(
            "ytt.whisper._do_download_audio",
            side_effect=RuntimeError("boom"),
        ):
            await run_whisper_job(job, registry, settings, cache, "m")

        final = await registry.get(VIDEO_A)
        assert final is not None
        assert final.status == "error"
        assert list(scratch.iterdir()) == []
        hit = await cache.get(VIDEO_B, "en")
        assert hit is not None and hit.text == TEXT_B

    def test_per_video_sweep_scoped_to_its_own_glob(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / f"{VIDEO_A}.mp4").write_bytes(b"a")
        (scratch / f"{VIDEO_A}.info.json").write_bytes(b"{}")
        (scratch / f"{VIDEO_B}.webm").write_bytes(b"b-in-flight")

        deleted = _sweep_video_scratch(VIDEO_A, str(scratch))

        assert deleted == 2
        remaining = {p.name for p in scratch.iterdir()}
        assert remaining == {f"{VIDEO_B}.webm"}

    async def test_startup_sweep_cannot_reach_the_cache_volume(
        self, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        for i in range(3):
            (scratch / f"stale{i}.mp4").write_bytes(b"x" * 10)
        cache_dir = tmp_path / "cache"
        cache = _make_cache(cache_dir)
        await _put(cache, VIDEO_A, TEXT_A)

        files_deleted, bytes_freed = startup_sweep(str(scratch))

        assert (files_deleted, bytes_freed) == (3, 30)
        assert list(scratch.iterdir()) == []
        # A different directory entirely: the sweep had no way to touch it.
        hit = await cache.get(VIDEO_A, "en")
        assert hit is not None and hit.text == TEXT_A
        assert len(_unit_files(cache_dir, VIDEO_A)) == 2


# ---------------------------------------------------------------------------
# Eviction removes whole units; survivors stay valid (policy §2)
# ---------------------------------------------------------------------------


class TestEviction:
    async def test_eviction_removes_whole_unit_survivor_still_serves(
        self, tmp_path: Path
    ) -> None:
        cache_dir = tmp_path / "cache"
        # Tight cap: A+B fit, C does not — exactly one eviction.
        cache = _make_cache(cache_dir, max_bytes=1200)
        await _put(cache, VIDEO_A, "a" * 500)
        await _put(cache, VIDEO_B, "b" * 500)
        # Touch A so B becomes the LRU victim.
        await cache.get(VIDEO_A, "en")

        await _put(cache, VIDEO_C, "c" * 500)

        # The victim went whole (both files)…
        assert await cache.get(VIDEO_B, "en") is None
        assert _unit_files(cache_dir, VIDEO_B) == []
        # …and the touched survivor still serves byte-identical text.
        hit = await cache.get(VIDEO_A, "en")
        assert hit is not None and hit.text == "a" * 500
        assert len(_unit_files(cache_dir, VIDEO_A)) == 2
        assert await cache.get(VIDEO_C, "en") is not None
        assert cache.total_bytes <= cache.max_bytes


# ---------------------------------------------------------------------------
# Restart equivalence (policy §5)
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_restart_loses_jobs_and_audio_but_serves_cached_transcript(
        self, tmp_path: Path
    ) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        cache_dir = tmp_path / "cache"

        # --- before: a served transcript, a completed job, its (leaked-by-
        # crash) audio still in scratch ---
        cache1 = _make_cache(cache_dir)
        await _put(cache1, VIDEO_A, TEXT_A)
        registry1 = WhisperJobRegistry()
        await _insert(registry1, _job(VIDEO_A, "done"))
        (scratch / f"{VIDEO_A}.mp4").write_bytes(b"audio")

        # --- the restart: fresh process state over the same volumes ---
        files_deleted, _ = startup_sweep(str(scratch))
        cache2 = TranscriptCache(cache_dir, max_bytes=8 << 20)  # restart: dir exists
        await cache2.startup_scan()
        registry2 = WhisperJobRegistry()

        assert files_deleted == 1
        assert list(scratch.iterdir()) == []  # audio gone
        assert await registry2.get(VIDEO_A) is None  # job record gone
        # …but the cached transcript survives and serves byte-identical text,
        # and the rebuilt inventory accounts for its bytes.
        hit = await cache2.get(VIDEO_A, "en")
        assert hit is not None and hit.text == TEXT_A
        assert cache2.total_bytes > 0

    async def test_startup_scan_cleans_stray_tmp_keeps_units(
        self, tmp_path: Path
    ) -> None:
        cache_dir = tmp_path / "cache"
        cache = _make_cache(cache_dir)
        await _put(cache, VIDEO_A, TEXT_A)
        (cache_dir / "crash-left.txt.tmp").write_bytes(b"orphan")
        (cache_dir / f"{VIDEO_A}.en.txt.tmp").write_bytes(b"half-write")

        await cache.startup_scan()

        assert list(cache_dir.glob("*.tmp")) == []
        assert cache.unit_count == 1
        hit = await cache.get(VIDEO_A, "en")
        assert hit is not None and hit.text == TEXT_A


# ---------------------------------------------------------------------------
# The policy doc pins the code, and the code backs the policy doc (policy §8)
# ---------------------------------------------------------------------------


class TestPolicyDocPinsItsMechanisms:
    """retention-policy.md must name only things the code actually has.

    The doc makes operational claims ('this env var bounds that mechanism',
    'this log event is the audit trail'). Each claim is checkable: the env
    var exists in ytt.config, the function exists in the ytt package, the
    log event is emitted somewhere in it. A doc that outlives its mechanism
    — or a mechanism renamed under a live runbook — fails here instead of
    misleading an operator mid-incident.
    """

    DOC_FILE = REPO_ROOT / "docs" / "notes" / "retention-policy.md"

    def test_policy_doc_exists_with_required_sections(self) -> None:
        doc = self.DOC_FILE.read_text(encoding="utf-8")
        for section in (
            "# Retention Policy",
            "## 2. Transcript cache",
            "## 3. Audio scratch",
            "## 4. Whisper job records",
            "## 5. Restart behavior",
            "## 6. Logging & redaction",
            "## 7. Operator deletion procedures",
            "### 7.1 Delete one video",
            "### 7.2 Wipe the whole transcript cache",
            "## 8. Regression evidence",
        ):
            assert section in doc, f"retention-policy.md lost section: {section}"

    def test_doc_env_vars_exist_in_config(self) -> None:
        doc = self.DOC_FILE.read_text(encoding="utf-8")
        config_src = (REPO_ROOT / "ytt" / "config.py").read_text(encoding="utf-8")
        env_vars = [
            "YTT_CACHE_DIR",
            "YTT_CACHE_MAX_BYTES",
            "YTT_CACHE_RECONCILE_SEC",
            "YTT_SCRATCH_DIR",
            "YTT_MAX_AUDIO_BYTES",
            "YTT_JOB_TTL_SEC",
            "YTT_WHISPER_TIMEOUT_SEC",
            "YTT_MAX_PENDING_WHISPER_JOBS",
            "YTT_MAX_ASR_DURATION_SEC",
        ]
        missing_in_doc = [v for v in env_vars if v not in doc]
        missing_in_code = [
            v for v in env_vars
            if v.removeprefix("YTT_").lower() not in config_src
        ]
        assert not missing_in_doc, f"doc stopped documenting: {missing_in_doc}"
        assert not missing_in_code, f"doc names nonexistent settings: {missing_in_code}"

    def test_doc_log_events_are_emitted_by_the_code(self) -> None:
        doc = self.DOC_FILE.read_text(encoding="utf-8")
        package_src = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted((REPO_ROOT / "ytt").glob("*.py"))
        )
        events = [
            "cache_eviction",
            "cache_enospc_degrade",
            "cache_startup_scan",
            "scratch_startup_sweep",
            "whisper_job_ttl_gc",
            "whisper_job_stale_running",
            "whisper_audio_deleted",
            "whisper_scratch_swept",
        ]
        missing_in_doc = [e for e in events if e not in doc]
        missing_in_code = [e for e in events if f'"{e}"' not in package_src]
        assert not missing_in_doc, f"doc stopped naming events: {missing_in_doc}"
        assert not missing_in_code, f"doc names unemitted events: {missing_in_code}"

    def test_doc_functions_exist_in_the_package(self) -> None:
        doc = self.DOC_FILE.read_text(encoding="utf-8")
        package_src = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted((REPO_ROOT / "ytt").glob("*.py"))
        )
        functions = [
            "def startup_sweep",
            "def _sweep_video_scratch",
            "def run_ttl_gc",
            "def startup_scan",
            "def reconcile",
            "def redact_credentials",
            "def redaction_processor",
            "def active_count",
        ]
        missing_in_doc = [f for f in functions if f.removeprefix("def ") not in doc]
        missing_in_code = [f for f in functions if f not in package_src]
        assert not missing_in_doc, f"doc stopped naming functions: {missing_in_doc}"
        assert not missing_in_code, f"doc names nonexistent functions: {missing_in_code}"

    def test_doc_states_the_no_ttl_stance_and_metrics(self) -> None:
        doc = self.DOC_FILE.read_text(encoding="utf-8")
        observability_src = (
            REPO_ROOT / "ytt" / "observability.py"
        ).read_text(encoding="utf-8")
        # The stance that makes the rest of the policy safe to be aggressive.
        assert "no time-based expiry" in doc
        for metric in ("ytt_cache_bytes", "ytt_cache_evictions_total"):
            assert metric in doc, f"doc must tell operators to watch {metric}"
            assert metric in observability_src, f"{metric} vanished from metrics"
        # Deletion procedures must keep the cursor-staleness consequence.
        assert "cursor_stale" in doc
        assert "ytt-oauth-state" in doc  # never collateral damage
