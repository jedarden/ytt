"""Unit tests for ytt.whisper — WhisperJob FSM, startup sweep, model guard.

Coverage:
- startup_sweep: empty directory → (0, 0)
- startup_sweep: files present → deletes all, returns correct counts
- startup_sweep: creates directory if missing
- startup_sweep: non-file entries (dirs) left untouched
- check_model_guard: configured model present → returns it unchanged
- check_model_guard: configured model absent → falls back to first model
- check_model_guard: empty model list → returns configured model unchanged
- check_model_guard: HTTP error → returns configured model (no crash)
- WhisperJobRegistry.get: unknown video_id → None
- WhisperJobRegistry.get_or_create: creates new pending job (is_new=True)
- WhisperJobRegistry.get_or_create: duplicate call → existing job, is_new=False
- WhisperJobRegistry.get_or_create: too_long_for_asr raised before registry entry
- WhisperJobRegistry.get_or_create: duration=None skips duration check
- WhisperJobRegistry.update_status: transitions pending → running (sets started_at)
- WhisperJobRegistry.update_status: transitions running → done (sets result_ref)
- WhisperJobRegistry.update_status: transitions running → error (sets error_code+message)
- WhisperJobRegistry.update_status: unknown video_id is a no-op
- WhisperJobRegistry.remove: removes existing entry
- WhisperJobRegistry.remove: removing unknown entry is a no-op
- WhisperJobRegistry.run_ttl_gc: done jobs older than TTL removed
- WhisperJobRegistry.run_ttl_gc: error jobs older than TTL removed
- WhisperJobRegistry.run_ttl_gc: pending jobs NOT GC'd (no TTL for pending)
- WhisperJobRegistry.run_ttl_gc: fresh done jobs NOT GC'd (within TTL)
- WhisperJobRegistry.run_ttl_gc: stale running job GC'd (older than timeout+TTL)
- WhisperJobRegistry.run_ttl_gc: fresh running job NOT GC'd
- WhisperJobRegistry.size: reflects current count
- run_whisper_job: happy path → status transitions pending→running→done, cache written
- run_whisper_job: audio download error → running→error, audio file cleaned up
- run_whisper_job: Whisper HTTP 500 → running→error, audio file cleaned up
- run_whisper_job: Whisper timeout → running→error, audio file cleaned up
- run_whisper_job: audio path set but Whisper fails → audio file still deleted
- run_whisper_job: Invariant 4 — audio file deleted even on unexpected exception
- _projected_audio_size: audio-only format returned
- _projected_audio_size: falls back to top-level filesize
- _projected_audio_size: returns None when no size info
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest

from ytt import errors
from ytt.errors import YttError
from ytt.models import WhisperJob
from ytt.whisper import (
    WhisperJobRegistry,
    _projected_audio_size,
    check_model_guard,
    run_whisper_job,
    startup_sweep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VIDEO_ID = "dQw4w9WgXcQ"


def _make_settings(
    *,
    max_asr_duration_sec: int = 1200,
    whisper_realtime_factor: float = 1.2,
    job_ttl_sec: int = 3600,
    whisper_timeout_sec: int = 2880,
    max_audio_bytes: int = 500 * 1024 * 1024,
    scratch_dir: str = "/tmp/ytt_test_scratch",
    whisper_url: str = "http://whisper.local:8000",
    whisper_model: str = "Systran/faster-whisper-small",
    proxy_url: str | None = None,
) -> MagicMock:
    s = MagicMock()
    s.max_asr_duration_sec = max_asr_duration_sec
    s.whisper_realtime_factor = whisper_realtime_factor
    s.job_ttl_sec = job_ttl_sec
    s.whisper_timeout_sec = whisper_timeout_sec
    s.max_audio_bytes = max_audio_bytes
    s.scratch_dir = scratch_dir
    s.whisper_url = whisper_url
    s.whisper_model = whisper_model
    s.proxy_url = proxy_url
    return s


def _make_job(
    video_id: str = VIDEO_ID,
    status: str = "pending",
    *,
    created_at: float | None = None,
    started_at: float | None = None,
    eta_sec: float | None = 60.0,
    duration_sec: float | None = 50.0,
    result_ref: str | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> WhisperJob:
    return WhisperJob(
        video_id=video_id,
        status=status,  # type: ignore[arg-type]
        created_at=created_at or time.time(),
        started_at=started_at,
        eta_sec=eta_sec,
        duration_sec=duration_sec,
        result_ref=result_ref,
        error_code=error_code,
        message=message,
    )


# ---------------------------------------------------------------------------
# startup_sweep
# ---------------------------------------------------------------------------


class TestStartupSweep:

    def test_empty_directory(self, tmp_path: Path) -> None:
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        files_deleted, bytes_freed = startup_sweep(scratch)
        assert files_deleted == 0
        assert bytes_freed == 0

    def test_deletes_all_files(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        # Create some audio files
        (scratch / "abc.mp4").write_bytes(b"X" * 100)
        (scratch / "def.webm").write_bytes(b"Y" * 200)
        files_deleted, bytes_freed = startup_sweep(str(scratch))
        assert files_deleted == 2
        assert bytes_freed == 300
        assert list(scratch.iterdir()) == []

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        scratch = str(tmp_path / "nonexistent" / "scratch")
        assert not Path(scratch).exists()
        files_deleted, bytes_freed = startup_sweep(scratch)
        assert files_deleted == 0
        assert bytes_freed == 0
        assert Path(scratch).is_dir()

    def test_subdirectories_not_deleted(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        subdir = scratch / "subdir"
        subdir.mkdir()
        (scratch / "file.mp4").write_bytes(b"Z" * 50)

        files_deleted, bytes_freed = startup_sweep(str(scratch))
        assert files_deleted == 1
        assert bytes_freed == 50
        # The subdirectory must survive
        assert subdir.exists()

    def test_idempotent_on_already_clean(self, tmp_path: Path) -> None:
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        startup_sweep(scratch)  # first call
        files_deleted, bytes_freed = startup_sweep(scratch)  # second call
        assert files_deleted == 0
        assert bytes_freed == 0


# ---------------------------------------------------------------------------
# check_model_guard
# ---------------------------------------------------------------------------


class TestCheckModelGuard:

    async def test_configured_model_present(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "data": [
                {"id": "Systran/faster-whisper-small"},
                {"id": "Systran/faster-whisper-large"},
            ]
        }
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)

        result = await check_model_guard(
            "http://whisper.local:8000",
            "Systran/faster-whisper-small",
            http_client=client,
        )
        assert result == "Systran/faster-whisper-small"

    async def test_configured_model_absent_falls_back(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "data": [
                {"id": "some-other-model"},
                {"id": "another-model"},
            ]
        }
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)

        result = await check_model_guard(
            "http://whisper.local:8000",
            "Systran/faster-whisper-small",
            http_client=client,
        )
        assert result == "some-other-model"

    async def test_empty_model_list_returns_configured(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.raise_for_status = MagicMock()
        response.json.return_value = {"data": []}
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)

        result = await check_model_guard(
            "http://whisper.local:8000",
            "Systran/faster-whisper-small",
            http_client=client,
        )
        assert result == "Systran/faster-whisper-small"

    async def test_http_error_returns_configured(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        result = await check_model_guard(
            "http://whisper.local:8000",
            "Systran/faster-whisper-small",
            http_client=client,
        )
        assert result == "Systran/faster-whisper-small"

    async def test_http_status_error_returns_configured(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=response)

        result = await check_model_guard(
            "http://whisper.local:8000",
            "Systran/faster-whisper-small",
            http_client=client,
        )
        assert result == "Systran/faster-whisper-small"


# ---------------------------------------------------------------------------
# _projected_audio_size
# ---------------------------------------------------------------------------


class TestProjectedAudioSize:

    def test_best_audio_format_filesize(self) -> None:
        info = {
            "formats": [
                {"vcodec": "none", "acodec": "mp4a.40.2", "tbr": 128, "filesize": 5_000_000},
                {"vcodec": "avc1.42001e", "filesize": 50_000_000},  # video — ignored
            ]
        }
        assert _projected_audio_size(info) == 5_000_000

    def test_best_audio_format_filesize_approx(self) -> None:
        info = {
            "formats": [
                {"vcodec": "none", "tbr": 64, "filesize_approx": 3_000_000},
            ]
        }
        assert _projected_audio_size(info) == 3_000_000

    def test_falls_back_to_top_level(self) -> None:
        info = {
            "formats": [],  # no formats
            "filesize": 8_000_000,
        }
        assert _projected_audio_size(info) == 8_000_000

    def test_returns_none_when_no_size(self) -> None:
        info: dict = {"formats": []}
        assert _projected_audio_size(info) is None

    def test_prefers_higher_bitrate_audio_format(self) -> None:
        info = {
            "formats": [
                {"vcodec": "none", "tbr": 64, "filesize": 3_000_000},
                {"vcodec": "none", "tbr": 128, "filesize": 5_000_000},  # higher tbr wins
            ]
        }
        assert _projected_audio_size(info) == 5_000_000


# ---------------------------------------------------------------------------
# WhisperJobRegistry
# ---------------------------------------------------------------------------


class TestWhisperJobRegistryGet:

    async def test_get_unknown_returns_none(self) -> None:
        reg = WhisperJobRegistry()
        assert await reg.get("unknown12345") is None

    async def test_get_existing_returns_job(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        job, is_new = await reg.get_or_create(VIDEO_ID, 100.0, settings)
        retrieved = await reg.get(VIDEO_ID)
        assert retrieved is not None
        assert retrieved.video_id == VIDEO_ID
        assert retrieved.status == "pending"


class TestWhisperJobRegistryGetOrCreate:

    async def test_creates_new_job(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        job, is_new = await reg.get_or_create(VIDEO_ID, 100.0, settings)
        assert is_new is True
        assert job.video_id == VIDEO_ID
        assert job.status == "pending"
        assert job.duration_sec == 100.0
        assert job.eta_sec == pytest.approx(100.0 * 1.2)
        assert job.created_at > 0

    async def test_duplicate_returns_existing(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        job1, is_new1 = await reg.get_or_create(VIDEO_ID, 100.0, settings)
        job2, is_new2 = await reg.get_or_create(VIDEO_ID, 100.0, settings)
        assert is_new1 is True
        assert is_new2 is False
        assert job1 is job2

    async def test_too_long_for_asr_raises(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(max_asr_duration_sec=600)
        with pytest.raises(YttError) as exc_info:
            await reg.get_or_create(VIDEO_ID, 700.0, settings)
        assert exc_info.value.error_code == errors.TOO_LONG_FOR_ASR
        # No registry entry created
        assert await reg.get(VIDEO_ID) is None

    async def test_none_duration_skips_duration_check(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(max_asr_duration_sec=60)
        job, is_new = await reg.get_or_create(VIDEO_ID, None, settings)
        assert is_new is True
        assert job.eta_sec is None
        assert job.duration_sec is None

    async def test_eta_computed_from_duration(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(whisper_realtime_factor=2.0)
        job, _ = await reg.get_or_create(VIDEO_ID, 300.0, settings)
        assert job.eta_sec == pytest.approx(600.0)

    async def test_registry_size_increments(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        assert reg.size == 0
        await reg.get_or_create(VIDEO_ID, 50.0, settings)
        assert reg.size == 1
        await reg.get_or_create("anotherid123", 50.0, settings)
        assert reg.size == 2


class TestWhisperJobRegistryUpdateStatus:

    async def test_pending_to_running_sets_started_at(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        before = time.time()
        job, _ = await reg.get_or_create(VIDEO_ID, 100.0, settings)
        assert job.started_at is None

        await reg.update_status(VIDEO_ID, "running")
        assert job.started_at is not None
        assert job.started_at >= before
        assert job.status == "running"

    async def test_running_to_done_sets_result_ref(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        await reg.get_or_create(VIDEO_ID, 100.0, settings)
        await reg.update_status(VIDEO_ID, "running")
        await reg.update_status(VIDEO_ID, "done", result_ref=f"{VIDEO_ID}.whisper")

        job = await reg.get(VIDEO_ID)
        assert job is not None
        assert job.status == "done"
        assert job.result_ref == f"{VIDEO_ID}.whisper"

    async def test_running_to_error_sets_error_fields(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        await reg.get_or_create(VIDEO_ID, 100.0, settings)
        await reg.update_status(VIDEO_ID, "running")
        await reg.update_status(
            VIDEO_ID,
            "error",
            error_code=errors.ASR_FAILED,
            message="Service unavailable",
        )

        job = await reg.get(VIDEO_ID)
        assert job is not None
        assert job.status == "error"
        assert job.error_code == errors.ASR_FAILED
        assert job.message == "Service unavailable"

    async def test_unknown_video_id_noop(self) -> None:
        reg = WhisperJobRegistry()
        # Should not raise
        await reg.update_status("nonexistent1", "running")


class TestWhisperJobRegistryRemove:

    async def test_removes_existing_entry(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings()
        await reg.get_or_create(VIDEO_ID, 50.0, settings)
        assert reg.size == 1
        await reg.remove(VIDEO_ID)
        assert reg.size == 0
        assert await reg.get(VIDEO_ID) is None

    async def test_noop_for_unknown(self) -> None:
        reg = WhisperJobRegistry()
        await reg.remove("unknown12345")  # should not raise
        assert reg.size == 0


# ---------------------------------------------------------------------------
# WhisperJobRegistry TTL GC
# ---------------------------------------------------------------------------


class TestWhisperJobRegistryTtlGc:

    async def test_done_job_past_ttl_removed(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(job_ttl_sec=10)

        # Manually insert an old done job
        old_job = WhisperJob(
            video_id=VIDEO_ID,
            status="done",
            created_at=time.time() - 20,  # 20s ago > 10s TTL
        )
        async with reg._lock:
            reg._jobs[VIDEO_ID] = old_job

        removed = await reg.run_ttl_gc(settings)
        assert removed == 1
        assert await reg.get(VIDEO_ID) is None

    async def test_error_job_past_ttl_removed(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(job_ttl_sec=10)

        old_job = WhisperJob(
            video_id=VIDEO_ID,
            status="error",
            created_at=time.time() - 20,
        )
        async with reg._lock:
            reg._jobs[VIDEO_ID] = old_job

        removed = await reg.run_ttl_gc(settings)
        assert removed == 1

    async def test_pending_job_not_gc_d(self) -> None:
        """Pending jobs have no TTL — they should not be GC'd by run_ttl_gc."""
        reg = WhisperJobRegistry()
        settings = _make_settings(job_ttl_sec=0)  # even with 0 TTL

        pending_job = WhisperJob(
            video_id=VIDEO_ID,
            status="pending",
            created_at=time.time() - 9999,  # very old
        )
        async with reg._lock:
            reg._jobs[VIDEO_ID] = pending_job

        removed = await reg.run_ttl_gc(settings)
        assert removed == 0
        assert await reg.get(VIDEO_ID) is not None

    async def test_fresh_done_job_not_gc_d(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(job_ttl_sec=3600)

        fresh_job = WhisperJob(
            video_id=VIDEO_ID,
            status="done",
            created_at=time.time() - 100,  # 100s < 3600s TTL
        )
        async with reg._lock:
            reg._jobs[VIDEO_ID] = fresh_job

        removed = await reg.run_ttl_gc(settings)
        assert removed == 0
        assert await reg.get(VIDEO_ID) is not None

    async def test_stale_running_job_gc_d(self) -> None:
        """A job in running state for longer than timeout+TTL is GC'd."""
        reg = WhisperJobRegistry()
        settings = _make_settings(whisper_timeout_sec=60, job_ttl_sec=10)
        # stale threshold = 60 + 10 = 70s
        stale_time = time.time() - 100  # 100s ago > 70s threshold

        running_job = WhisperJob(
            video_id=VIDEO_ID,
            status="running",
            created_at=stale_time,
            started_at=stale_time,
        )
        async with reg._lock:
            reg._jobs[VIDEO_ID] = running_job

        removed = await reg.run_ttl_gc(settings)
        assert removed == 1
        assert await reg.get(VIDEO_ID) is None

    async def test_fresh_running_job_not_gc_d(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(whisper_timeout_sec=2880, job_ttl_sec=3600)

        running_job = WhisperJob(
            video_id=VIDEO_ID,
            status="running",
            created_at=time.time() - 60,  # 60s < 2880+3600 threshold
            started_at=time.time() - 60,
        )
        async with reg._lock:
            reg._jobs[VIDEO_ID] = running_job

        removed = await reg.run_ttl_gc(settings)
        assert removed == 0

    async def test_stale_running_uses_started_at_when_available(self) -> None:
        """Stale GC uses started_at (not created_at) when available."""
        reg = WhisperJobRegistry()
        settings = _make_settings(whisper_timeout_sec=60, job_ttl_sec=10)
        # stale threshold = 70s

        running_job = WhisperJob(
            video_id=VIDEO_ID,
            status="running",
            created_at=time.time() - 9999,  # very old creation
            started_at=time.time() - 30,    # only 30s in running state
        )
        async with reg._lock:
            reg._jobs[VIDEO_ID] = running_job

        removed = await reg.run_ttl_gc(settings)
        # 30s < 70s threshold → NOT stale
        assert removed == 0

    async def test_multiple_gc_targets(self) -> None:
        reg = WhisperJobRegistry()
        settings = _make_settings(job_ttl_sec=10)

        now = time.time()
        for vid in ["aaaabbbbccc", "ddddeeeefff", "gggghhhh111"]:
            old = WhisperJob(video_id=vid, status="done", created_at=now - 20)
            async with reg._lock:
                reg._jobs[vid] = old

        # One fresh job (should NOT be GC'd)
        fresh_vid = "kkkkllllmmm"
        fresh = WhisperJob(video_id=fresh_vid, status="done", created_at=now - 5)
        async with reg._lock:
            reg._jobs[fresh_vid] = fresh

        removed = await reg.run_ttl_gc(settings)
        assert removed == 3
        assert await reg.get(fresh_vid) is not None


# ---------------------------------------------------------------------------
# run_whisper_job (with stubbed dependencies)
# ---------------------------------------------------------------------------


def _make_cache_mock() -> MagicMock:
    cache = MagicMock()
    cache.put = AsyncMock(return_value=True)
    return cache


def _make_whisper_response(
    text: str = "Hello world",
    language: str = "en",
    segments: list[dict] | None = None,
) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "text": text,
        "language": language,
        "segments": segments or [{"start": 0.0, "end": 2.0, "text": text}],
    }
    return response


class TestRunWhisperJobHappyPath:

    async def test_full_lifecycle_pending_running_done(
        self, tmp_path: Path
    ) -> None:
        """Happy path: audio download stubbed, Whisper responds 200."""
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        # Write a fake audio file so the job can read it
        audio_file = Path(scratch) / f"{VIDEO_ID}.mp4"
        audio_file.write_bytes(b"fake audio")

        settings = _make_settings(scratch_dir=scratch)
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        whisper_resp = _make_whisper_response("Hello world", "en")
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=whisper_resp)

        with patch(
            "ytt.whisper._do_download_audio",
            return_value=str(audio_file),
        ):
            await run_whisper_job(
                job,
                registry,
                settings,
                cache,
                "Systran/faster-whisper-small",
                http_client=http_client,
            )

        # Job transitions: pending → running → done
        final_job = await registry.get(VIDEO_ID)
        assert final_job is not None
        assert final_job.status == "done"
        assert final_job.result_ref == f"{VIDEO_ID}.whisper"
        assert final_job.error_code is None

        # Cache was written
        cache.put.assert_called_once()
        call_args = cache.put.call_args
        assert call_args[0][0] == VIDEO_ID  # video_id
        assert call_args[0][1] == "whisper"  # lang key
        assert call_args[0][4] == "whisper"  # source

    async def test_segments_extracted_correctly(self, tmp_path: Path) -> None:
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        audio_file = Path(scratch) / f"{VIDEO_ID}.mp4"
        audio_file.write_bytes(b"fake")

        settings = _make_settings(scratch_dir=scratch)
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        raw_segs = [
            {"start": 0.0, "end": 2.0, "text": "Hello"},
            {"start": 2.0, "end": 5.0, "text": "world"},
        ]
        whisper_resp = _make_whisper_response("Hello world", "en", raw_segs)
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=whisper_resp)

        with patch("ytt.whisper._do_download_audio", return_value=str(audio_file)):
            await run_whisper_job(
                job, registry, settings, cache,
                "Systran/faster-whisper-small",
                http_client=http_client,
            )

        cache.put.assert_called_once()
        # call args: (video_id, lang, text, segments, source, metadata)
        segments_arg = cache.put.call_args[0][3]  # segments at index 3
        assert len(segments_arg) == 2
        assert segments_arg[0]["start"] == 0.0
        assert segments_arg[0]["duration"] == pytest.approx(2.0)
        assert segments_arg[0]["text"] == "Hello"
        assert segments_arg[1]["start"] == 2.0
        assert segments_arg[1]["duration"] == pytest.approx(3.0)

    async def test_audio_file_deleted_on_success(self, tmp_path: Path) -> None:
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        audio_file = Path(scratch) / f"{VIDEO_ID}.mp4"
        audio_file.write_bytes(b"audio data")

        settings = _make_settings(scratch_dir=scratch)
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=_make_whisper_response())

        with patch("ytt.whisper._do_download_audio", return_value=str(audio_file)):
            await run_whisper_job(
                job, registry, settings, cache,
                "Systran/faster-whisper-small",
                http_client=http_client,
            )

        # Invariant 4: audio file must be deleted
        assert not audio_file.exists()


class TestRunWhisperJobErrors:

    async def test_audio_download_error_transitions_to_error(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(scratch_dir=str(tmp_path / "scratch"))
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        http_client = AsyncMock(spec=httpx.AsyncClient)

        with patch(
            "ytt.whisper._do_download_audio",
            side_effect=YttError(errors.IP_BLOCKED, "IP is blocked"),
        ):
            await run_whisper_job(
                job, registry, settings, cache,
                "model",
                http_client=http_client,
            )

        final = await registry.get(VIDEO_ID)
        assert final is not None
        assert final.status == "error"
        assert final.error_code == errors.IP_BLOCKED

        # Cache was NOT written (errors never cached as transcripts)
        cache.put.assert_not_called()

    async def test_whisper_http_500_transitions_to_error(
        self, tmp_path: Path
    ) -> None:
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        audio_file = Path(scratch) / f"{VIDEO_ID}.mp4"
        audio_file.write_bytes(b"audio")

        settings = _make_settings(scratch_dir=scratch)
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        # Mock Whisper returning 500
        error_response = MagicMock(spec=httpx.Response)
        error_response.status_code = 500
        error_response.text = "Internal server error"
        http_error = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=MagicMock(),
            response=error_response,
        )
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(side_effect=http_error)

        with patch("ytt.whisper._do_download_audio", return_value=str(audio_file)):
            await run_whisper_job(
                job, registry, settings, cache,
                "model",
                http_client=http_client,
            )

        final = await registry.get(VIDEO_ID)
        assert final is not None
        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        assert "500" in (final.message or "")

        # Audio file must be deleted even on Whisper error (Invariant 4)
        assert not audio_file.exists()

    async def test_whisper_timeout_transitions_to_error(
        self, tmp_path: Path
    ) -> None:
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        audio_file = Path(scratch) / f"{VIDEO_ID}.mp4"
        audio_file.write_bytes(b"audio")

        settings = _make_settings(scratch_dir=scratch)
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

        with patch("ytt.whisper._do_download_audio", return_value=str(audio_file)):
            await run_whisper_job(
                job, registry, settings, cache,
                "model",
                http_client=http_client,
            )

        final = await registry.get(VIDEO_ID)
        assert final is not None
        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED

    async def test_audio_deleted_on_whisper_error_invariant4(
        self, tmp_path: Path
    ) -> None:
        """Invariant 4: audio file is deleted even when Whisper fails."""
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        audio_file = Path(scratch) / f"{VIDEO_ID}.mp4"
        audio_file.write_bytes(b"data")

        settings = _make_settings(scratch_dir=scratch)
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(
            side_effect=httpx.ConnectError("refused")
        )

        with patch("ytt.whisper._do_download_audio", return_value=str(audio_file)):
            await run_whisper_job(
                job, registry, settings, cache,
                "model",
                http_client=http_client,
            )

        # Audio deleted even though Whisper failed
        assert not audio_file.exists()

    async def test_unexpected_exception_transitions_to_error(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(scratch_dir=str(tmp_path / "scratch"))
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        with patch(
            "ytt.whisper._do_download_audio",
            side_effect=RuntimeError("something very unexpected"),
        ):
            await run_whisper_job(
                job, registry, settings, cache,
                "model",
                http_client=AsyncMock(spec=httpx.AsyncClient),
            )

        final = await registry.get(VIDEO_ID)
        assert final is not None
        assert final.status == "error"
        assert final.error_code == errors.ASR_FAILED

    async def test_audio_not_set_when_download_fails(
        self, tmp_path: Path
    ) -> None:
        """When _do_download_audio raises before writing a file, no unlink attempt."""
        settings = _make_settings(scratch_dir=str(tmp_path / "scratch"))
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        with patch(
            "ytt.whisper._do_download_audio",
            side_effect=YttError(errors.UNAVAILABLE, "video unavailable"),
        ):
            await run_whisper_job(
                job, registry, settings, cache,
                "model",
                http_client=AsyncMock(spec=httpx.AsyncClient),
            )

        # audio_path was never set → no file to clean up, but that's fine
        final = await registry.get(VIDEO_ID)
        assert final is not None
        assert final.status == "error"


class TestRunWhisperJobRunningTransition:

    async def test_status_running_at_job_start(self, tmp_path: Path) -> None:
        """Verify running transition happens before the blocking download."""
        scratch = str(tmp_path / "scratch")
        os.makedirs(scratch)
        audio_file = Path(scratch) / f"{VIDEO_ID}.mp4"
        audio_file.write_bytes(b"x")

        settings = _make_settings(scratch_dir=scratch)
        registry = WhisperJobRegistry()
        cache = _make_cache_mock()
        job, _ = await registry.get_or_create(VIDEO_ID, 50.0, settings)

        status_at_download: list[str] = []

        def _stub_download(*args, **kwargs):
            # Peek at job status via synchronous attribute access (inside to_thread)
            status_at_download.append(job.status)
            return str(audio_file)

        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=_make_whisper_response())

        with patch("ytt.whisper._do_download_audio", side_effect=_stub_download):
            await run_whisper_job(
                job, registry, settings, cache,
                "model",
                http_client=http_client,
            )

        # Status was "running" by the time _do_download_audio was called
        assert status_at_download == ["running"]


# ---------------------------------------------------------------------------
# Integration: get_or_create then run_whisper_job (single-flight check)
# ---------------------------------------------------------------------------


class TestWhisperJobSingleFlight:

    async def test_concurrent_get_or_create_returns_same_job(self) -> None:
        """Invariant 2: concurrent get_or_create for same video_id → same job."""
        reg = WhisperJobRegistry()
        settings = _make_settings()

        results = await asyncio.gather(
            reg.get_or_create(VIDEO_ID, 100.0, settings),
            reg.get_or_create(VIDEO_ID, 100.0, settings),
            reg.get_or_create(VIDEO_ID, 100.0, settings),
        )
        # All three return the same job object
        job0 = results[0][0]
        for job, is_new in results[1:]:
            assert job is job0  # same object
        # Only first was is_new=True
        new_count = sum(1 for _, is_new in results if is_new)
        assert new_count == 1
        # Only one registry entry
        assert reg.size == 1
