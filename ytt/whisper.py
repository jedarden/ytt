"""Whisper async fallback client + job FSM (plan: §Whisper fallback).

Handles the caption-less code path:

1. **Startup sweep** — clears stale audio files left by a crash.
2. **Model guard** — queries ``GET /v1/models`` on startup; self-corrects to the
   first available model if the configured model is not listed.
3. **WhisperJobRegistry** — in-memory get-or-create registry (keyed by
   ``video_id``), asyncio.Lock-protected FSM transitions, TTL GC for
   done/error jobs, stale-running GC.
4. **run_whisper_job** — the background asyncio Task that drives the full
   audio lifecycle: download bestaudio → scratch → POST /v1/audio/transcriptions
   → write ``<id>.whisper.*`` cache → delete audio (context-manager, success or
   failure, Invariant 4).

Plan references: §Whisper fallback, §Concurrency (Invariant 2), §Caching
(Invariant 4: audio always deleted), Invariant 7 (ETA-timeout safety, validated
in config.py at startup).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog
import yt_dlp
import yt_dlp.utils

from ytt import errors
from ytt.errors import YttError
from ytt.fetch import YDL_EXTRACTOR_ARGS, YDL_NO_COOKIES, classify_ydl_error
from ytt.models import WhisperJob

if TYPE_CHECKING:
    from ytt.cache import TranscriptCache
    from ytt.config import Settings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Startup sweep (plan §Whisper fallback — "Startup sweep")
# ---------------------------------------------------------------------------


def startup_sweep(scratch_dir: str) -> tuple[int, int]:
    """Delete all files in ``scratch_dir`` unconditionally on server startup.

    Plan: "Startup sweep = delete all files in YTT_SCRATCH_DIR unconditionally
    on server startup (safe because replicas:1 + strategy:Recreate guarantee no
    peer is running; any file present is stale). Log file names + sizes before
    deletion."

    Returns
    -------
    (files_deleted, bytes_freed)
    """
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    files_deleted = 0
    bytes_freed = 0

    for path in list(scratch.iterdir()):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            log.info(
                "scratch_sweep_file",
                file=path.name,
                size_bytes=size,
            )
            path.unlink()
            files_deleted += 1
            bytes_freed += size
        except OSError:
            pass

    log.info(
        "scratch_startup_sweep",
        files_deleted=files_deleted,
        bytes_freed=bytes_freed,
    )
    return files_deleted, bytes_freed


# ---------------------------------------------------------------------------
# Model guard (plan §Whisper fallback — "Self-correcting")
# ---------------------------------------------------------------------------


async def check_model_guard(
    whisper_url: str,
    whisper_model: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Query ``GET /v1/models``; fall back to first model if configured not listed.

    Plan: "on startup query GET /v1/models; if the configured model isn't listed,
    fall back to the first served model and log loudly (don't 500 forever)."

    Parameters
    ----------
    whisper_url:
        Base URL of the whisper-openai service (no trailing ``/v1/models``).
    whisper_model:
        The model name from ``YTT_WHISPER_MODEL``.
    http_client:
        Optional pre-built ``httpx.AsyncClient`` (for testing). If ``None``,
        a temporary client is created and closed internally.

    Returns
    -------
    str
        The active model name to use for all transcription requests.
    """
    own_client = http_client is None
    client: httpx.AsyncClient = http_client or httpx.AsyncClient(timeout=10.0)

    try:
        resp = await client.get(f"{whisper_url}/v1/models")
        resp.raise_for_status()
        data = resp.json()
        available: list[str] = [
            m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
        ]

        if not available:
            log.warning(
                "whisper_model_guard_empty",
                configured_model=whisper_model,
                available_models=[],
            )
            return whisper_model  # nothing to fall back to

        if whisper_model in available:
            return whisper_model  # configured model is present

        # Fall back to first served model
        fallback = available[0]
        log.warning(
            "whisper_model_self_correct",
            configured_model=whisper_model,
            fallback_model=fallback,
            available_models=available,
        )
        return fallback

    except Exception as exc:
        log.warning(
            "whisper_model_guard_error",
            configured_model=whisper_model,
            error=str(exc),
        )
        # Don't block startup on a transient Whisper outage; use configured name
        return whisper_model

    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Audio download helpers (blocking; call via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _projected_audio_size(info: dict) -> int | None:
    """Extract the best projected audio byte size from an extract_info result.

    Checks best-audio-only format entry first (matching yt-dlp's ``bestaudio``
    selection heuristic), then falls back to top-level ``filesize``/
    ``filesize_approx``.
    """
    formats: list[dict] = info.get("formats") or []
    # yt-dlp flags audio-only formats with vcodec="none"
    best_audio: dict | None = None
    best_score = -1.0
    for fmt in formats:
        if fmt.get("vcodec") not in ("none", ""):
            continue
        score = float(fmt.get("tbr") or fmt.get("abr") or 0)
        if score > best_score:
            best_score = score
            best_audio = fmt

    if best_audio is not None:
        sz = best_audio.get("filesize") or best_audio.get("filesize_approx")
        if sz:
            return int(sz)

    # Top-level fallback
    sz = info.get("filesize") or info.get("filesize_approx")
    return int(sz) if sz else None


def _do_download_audio(
    video_id: str,
    scratch_dir: str,
    max_audio_bytes: int,
    proxy: str | None = None,
) -> str:
    """Synchronous: download bestaudio for *video_id* to *scratch_dir*.

    Returns the absolute path of the downloaded file.

    - Pre-download: queries ``extract_info`` to check projected size against
      ``min(max_audio_bytes, statvfs_free)``.  Raises
      ``YttError(too_long_for_asr)`` if over cap.
    - During download: a ``progress_hooks`` callback aborts with
      ``DownloadError('audio_too_large')`` if live bytes exceed the cap.

    Must be called via ``asyncio.to_thread``.
    """
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    # Effective cap = min(configured max, available free space on scratch)
    try:
        sv = os.statvfs(scratch_dir)
        free = sv.f_bavail * sv.f_frsize
    except OSError:
        free = max_audio_bytes
    cap = min(max_audio_bytes, free)

    # --- progress hook: abort on size overrun during streaming download ------
    def _size_cap_hook(d: dict) -> None:
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            if downloaded > cap:
                raise yt_dlp.utils.DownloadError("audio_too_large")

    outtmpl = str(scratch / f"{video_id}.%(ext)s")
    opts: dict = {
        **YDL_NO_COOKIES,
        **YDL_EXTRACTOR_ARGS,
        "format": "bestaudio",
        "outtmpl": outtmpl,
        "progress_hooks": [_size_cap_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": False,
    }
    if proxy:
        opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            url = f"https://www.youtube.com/watch?v={video_id}"

            # First: extract_info only (download=False) to check projected size
            info = ydl.extract_info(url, download=False)
            if not info:
                raise YttError(errors.EMPTY_BODY, "yt-dlp returned no info for audio.")

            projected = _projected_audio_size(info)
            if projected is not None and projected > cap:
                raise YttError(
                    errors.TOO_LONG_FOR_ASR,
                    f"Projected audio size {projected:,} B exceeds cap {cap:,} B "
                    f"(min(YTT_MAX_AUDIO_BYTES, scratch_free)). "
                    "Video audio is too large for ASR.",
                )
            if projected is None:
                log.warning(
                    "audio_size_unknown",
                    video_id=video_id,
                    cap_bytes=cap,
                )

            # Now download (progress hook will abort if over cap mid-stream)
            ydl.download([url])

    except YttError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        code = classify_ydl_error(str(exc))
        raise YttError(code, str(exc)) from exc
    except yt_dlp.utils.ExtractorError as exc:
        code = classify_ydl_error(str(exc))
        raise YttError(code, str(exc)) from exc

    # Find the downloaded file (yt-dlp fills in the real extension)
    for candidate in sorted(scratch.glob(f"{video_id}.*")):
        if candidate.is_file():
            return str(candidate)

    raise YttError(errors.EMPTY_BODY, "Audio download produced no output file.")


# ---------------------------------------------------------------------------
# WhisperJobRegistry (plan §Whisper fallback — Registry + FSM)
# ---------------------------------------------------------------------------


class WhisperJobRegistry:
    """In-memory registry for the WhisperJob FSM.

    All mutable state is protected by an ``asyncio.Lock``.  The registry
    enforces Invariant 2 (one in-flight job per ``video_id``) via the
    ``get_or_create`` get-or-create pattern: a second request for the same
    ``video_id`` while a job is ``pending`` or ``running`` returns the existing
    job rather than starting a duplicate.

    States: ``pending → running → done | error``

    TTL GC (plan §Whisper fallback):
    - ``done``/``error`` jobs older than ``YTT_JOB_TTL_SEC`` are removed.
    - ``running`` jobs older than ``WHISPER_TIMEOUT_SEC + JOB_TTL_SEC`` are
      considered stale, logged at ERROR, and removed.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, WhisperJob] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._gc_task: asyncio.Task[None] | None = None

    # -- public API -----------------------------------------------------------

    async def get_or_create(
        self,
        video_id: str,
        duration_sec: float | None,
        settings: "Settings",
    ) -> tuple[WhisperJob, bool]:
        """Get an existing job or create a new ``pending`` one.

        Parameters
        ----------
        video_id:
            11-character canonical video ID.
        duration_sec:
            Video duration in seconds (from ``extract_info``). Used for ETA
            computation and the ``MAX_ASR_DURATION_SEC`` duration cap check.
        settings:
            Runtime settings (``max_asr_duration_sec``, ``whisper_realtime_factor``).

        Returns
        -------
        (job, is_new)
            ``is_new=True`` when the job was freshly created; the caller is
            responsible for starting the background transcription Task.

        Raises
        ------
        YttError(too_long_for_asr):
            When ``duration_sec`` exceeds ``MAX_ASR_DURATION_SEC``.  Raised
            *before* creating the job so that no registry entry is left behind.
        """
        # Duration check first — outside the lock so we don't hold it during the
        # raise.  (Two concurrent requests for the same over-long video will both
        # hit this; that's fine — no job is created in either case.)
        if duration_sec is not None and duration_sec > settings.max_asr_duration_sec:
            raise YttError(
                errors.TOO_LONG_FOR_ASR,
                f"Video duration {duration_sec:.0f}s exceeds "
                f"YTT_MAX_ASR_DURATION_SEC ({settings.max_asr_duration_sec}s). "
                "Too long for ASR.",
            )

        async with self._lock:
            existing = self._jobs.get(video_id)
            if existing is not None:
                return existing, False

            eta_sec = (
                duration_sec * settings.whisper_realtime_factor
                if duration_sec is not None
                else None
            )
            job = WhisperJob(
                video_id=video_id,
                status="pending",
                created_at=time.time(),
                eta_sec=eta_sec,
                duration_sec=duration_sec,
            )
            self._jobs[video_id] = job
            log.info(
                "whisper_job_created",
                video_id=video_id,
                eta_sec=eta_sec,
                duration_sec=duration_sec,
            )
            return job, True

    async def get(self, video_id: str) -> WhisperJob | None:
        """Return the job for *video_id*, or ``None`` if not found."""
        async with self._lock:
            return self._jobs.get(video_id)

    async def update_status(
        self,
        video_id: str,
        new_status: str,
        *,
        result_ref: str | None = None,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        """Transition a job's status under the registry lock.

        Plan: "WhisperJob state machine: pending → running → done | error".
        Logs the transition as ``whisper_job_status_change``.
        """
        async with self._lock:
            job = self._jobs.get(video_id)
            if job is None:
                return
            old_status = job.status
            job.status = new_status  # type: ignore[assignment]
            if new_status == "running":
                job.started_at = time.time()
            if result_ref is not None:
                job.result_ref = result_ref
            if error_code is not None:
                job.error_code = error_code
            if message is not None:
                job.message = message
            log.info(
                "whisper_job_status_change",
                video_id=video_id,
                old_status=old_status,
                new_status=new_status,
            )

    async def remove(self, video_id: str) -> None:
        """Remove a job entry from the registry (e.g. on evicted-result polling)."""
        async with self._lock:
            self._jobs.pop(video_id, None)

    async def run_ttl_gc(self, settings: "Settings") -> int:
        """Expire done/error jobs older than TTL; remove stale running jobs.

        Plan:
        - "done/errored jobs": GC after ``YTT_JOB_TTL_SEC`` seconds.
        - "stale running GC": jobs in running state longer than
          ``WHISPER_TIMEOUT_SEC + JOB_TTL_SEC`` are logged at ERROR and removed.

        Returns
        -------
        int
            Number of jobs removed.
        """
        now = time.time()
        to_remove: list[str] = []

        async with self._lock:
            count_before = len(self._jobs)
            for vid, job in list(self._jobs.items()):
                if job.status in ("done", "error"):
                    age = now - job.created_at
                    if age > settings.job_ttl_sec:
                        to_remove.append(vid)
                elif job.status == "running":
                    # Stale if running longer than whisper_timeout + ttl
                    stale_threshold = (
                        float(settings.whisper_timeout_sec) + float(settings.job_ttl_sec)
                    )
                    started = job.started_at or job.created_at
                    elapsed = now - started
                    if elapsed > stale_threshold:
                        log.error(
                            "whisper_job_stale_running",
                            video_id=vid,
                            elapsed_sec=elapsed,
                        )
                        to_remove.append(vid)

            for vid in to_remove:
                del self._jobs[vid]

            count_after = len(self._jobs)

        removed = len(to_remove)
        if removed > 0:
            log.info(
                "whisper_job_ttl_gc",
                job_count_before=count_before,
                job_count_after=count_after,
            )
        return removed

    def start_ttl_gc_task(self, settings: "Settings") -> None:
        """Start the periodic TTL GC background task.

        Must be called from within a running event loop. The GC loop runs every
        60 seconds.
        """
        self._gc_task = asyncio.create_task(
            self._gc_loop(settings), name="whisper_ttl_gc"
        )

    async def shutdown(self) -> None:
        """Cancel and await the background GC task (if running)."""
        if self._gc_task is not None:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except asyncio.CancelledError:
                pass
            self._gc_task = None

    # -- properties -----------------------------------------------------------

    @property
    def size(self) -> int:
        """Number of jobs currently in the registry."""
        return len(self._jobs)

    # -- internals ------------------------------------------------------------

    async def _gc_loop(self, settings: "Settings") -> None:
        """Background task: sleep 60s, run TTL GC, repeat."""
        while True:
            await asyncio.sleep(60)
            try:
                await self.run_ttl_gc(settings)
            except Exception:
                log.exception("whisper_ttl_gc_error")


# ---------------------------------------------------------------------------
# Background transcription task (plan §Whisper fallback — Audio lifecycle)
# ---------------------------------------------------------------------------


async def run_whisper_job(
    job: WhisperJob,
    registry: WhisperJobRegistry,
    settings: "Settings",
    cache: "TranscriptCache",
    active_model: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """End-to-end Whisper transcription job (plan §Whisper fallback).

    Audio lifecycle (Invariant 4 — audio always deleted):
    1. ``pending → running`` (status transition).
    2. Download bestaudio → scratch dir (via ``asyncio.to_thread``).
    3. POST ``/v1/audio/transcriptions`` to whisper-openai.
    4. Write result to cache as ``<id>.whisper.*``.
    5. ``running → done`` (or ``→ error`` on any failure).
    6. Delete audio file in ``finally`` (regardless of outcome).

    This coroutine is designed to run as a background asyncio Task.  Errors
    transition the job to ``error`` and are logged; they do **not** propagate
    to the caller (which started the Task and returned ``pending`` to the user).
    """
    video_id = job.video_id
    audio_path: str | None = None

    # 1. Transition to running
    await registry.update_status(video_id, "running")

    try:
        # 2. Download audio (blocking → thread, with timeout)
        try:
            audio_path = await asyncio.wait_for(
                asyncio.to_thread(
                    _do_download_audio,
                    video_id,
                    settings.scratch_dir,
                    settings.max_audio_bytes,
                    settings.proxy_url,
                ),
                timeout=float(settings.whisper_timeout_sec),
            )
        except asyncio.TimeoutError:
            raise YttError(
                errors.ASR_FAILED,
                f"Audio download timed out after {settings.whisper_timeout_sec}s.",
            )
        # YttError propagates as-is

        # 3. POST audio to Whisper service
        own_client = http_client is None
        _client: httpx.AsyncClient = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=float(settings.whisper_timeout_sec),
                write=float(settings.whisper_timeout_sec),
                pool=10.0,
            )
        )

        try:
            audio_bytes = Path(audio_path).read_bytes()
            audio_filename = Path(audio_path).name

            resp = await _client.post(
                f"{settings.whisper_url}/v1/audio/transcriptions",
                files={"file": (audio_filename, audio_bytes, "audio/mpeg")},
                data={
                    "model": active_model,
                    "response_format": "verbose_json",
                },
            )
            resp.raise_for_status()
            whisper_data: dict = resp.json()

        except httpx.TimeoutException as exc:
            raise YttError(
                errors.ASR_FAILED,
                f"Whisper service timed out: {exc}",
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise YttError(
                errors.ASR_FAILED,
                f"Whisper service error {exc.response.status_code}: "
                f"{exc.response.text[:200]}",
            ) from exc
        except httpx.RequestError as exc:
            raise YttError(
                errors.ASR_FAILED,
                f"Whisper service request failed: {exc}",
            ) from exc
        finally:
            if own_client:
                await _client.aclose()

        # 4. Extract text + segments from verbose_json response
        text: str = whisper_data.get("text", "")
        raw_segs: list[dict] = whisper_data.get("segments", [])
        segments: list[dict] = [
            {
                "start": seg.get("start", 0.0),
                "duration": max(
                    0.0,
                    float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)),
                ),
                "text": seg.get("text", ""),
            }
            for seg in raw_segs
            if isinstance(seg, dict)
        ]

        detected_lang: str = whisper_data.get("language", "")
        metadata: dict[str, Any] = {}
        if detected_lang:
            metadata["detected_language"] = detected_lang
        if job.duration_sec is not None:
            metadata["duration_sec"] = job.duration_sec

        # Write cache unit as <video_id>.whisper.*  (Invariant 5: whisper satisfies any lang)
        await cache.put(
            video_id,
            "whisper",
            text,
            segments,
            "whisper",
            metadata,
        )

        # 5. Mark done
        result_ref = f"{video_id}.whisper"
        await registry.update_status(video_id, "done", result_ref=result_ref)
        log.info("whisper_job_done", video_id=video_id)

    except YttError as exc:
        await registry.update_status(
            video_id,
            "error",
            error_code=exc.error_code,
            message=exc.message,
        )
        log.warning(
            "whisper_job_error",
            video_id=video_id,
            error_code=exc.error_code,
        )

    except Exception as exc:
        await registry.update_status(
            video_id,
            "error",
            error_code=errors.ASR_FAILED,
            message=f"Unexpected error during transcription: {exc}",
        )
        log.exception("whisper_job_unexpected_error", video_id=video_id)

    finally:
        # 6. Always delete audio — Invariant 4
        if audio_path is not None:
            try:
                Path(audio_path).unlink(missing_ok=True)
                log.debug("whisper_audio_deleted", video_id=video_id)
            except OSError as exc:
                log.warning(
                    "whisper_audio_delete_failed",
                    video_id=video_id,
                    error=str(exc),
                )
