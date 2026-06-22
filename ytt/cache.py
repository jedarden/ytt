"""Flat-file, size-bounded LRU transcript cache (plan: §Caching).

Atomic unit = ``(video_id, lang)`` or ``(video_id, "whisper")`` pair:
  ``<id>.<lang>.txt``  — plain text body (UTF-8)
  ``<id>.<lang>.json`` — sidecar: ``{"source": …, "segments": […], …metadata…}``

Guarantees (plan: Invariant 1 + §Caching):

- **All touch / eviction-selection / delete under ``_lock``** — a live unit can
  never be evicted while being touched.
- **Byte counter incremented only after ``os.replace`` succeeds** — no phantom
  accounting on failed writes.
- **ENOSPC:** evict-and-retry once, then degrade to serve-but-don't-cache (not
  an error; caller still returns the transcript).
- **Startup scan** excludes and cleans stray ``.tmp`` files.
- **Reconcile** re-stats all units, corrects drift, and immediately evicts if the
  recomputed total exceeds cap.

Implemented in Phase 4.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Public data classes                                                           #
# --------------------------------------------------------------------------- #


@dataclass
class CacheHit:
    """Result of a successful :meth:`TranscriptCache.get` lookup."""

    video_id: str
    lang: str           # served lang key (may be "whisper" for the fallback)
    source: str         # "caption_manual" | "caption_auto" | "whisper"
    text: str
    segments: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Internal record                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class _CacheUnit:
    """In-memory descriptor for one cached unit (must be held under _lock)."""

    video_id: str
    lang: str
    txt_path: Path
    json_path: Path
    size_bytes: int   # txt_size + json_size (both files counted)
    mtime: float      # max(txt.st_mtime, json.st_mtime) — LRU key


# --------------------------------------------------------------------------- #
# Filename helpers                                                              #
# --------------------------------------------------------------------------- #


def _unit_stems(video_id: str, lang: str) -> tuple[str, str]:
    """Return ``(txt_filename, json_filename)`` for a cache key."""
    base = f"{video_id}.{lang}"
    return f"{base}.txt", f"{base}.json"


def _parse_key_from_stem(stem: str) -> tuple[str, str] | None:
    """Parse ``video_id`` + ``lang`` from a ``.txt`` stem, or ``None`` if invalid.

    A valid stem looks like ``<11-char-id>.<lang>`` (one dot separating them).
    Multi-dot lang keys (not used in v1) are not supported by this simple parser.
    """
    parts = stem.split(".", 1)
    if len(parts) != 2:
        return None
    video_id, lang = parts
    if len(video_id) == 11:
        return video_id, lang
    return None


# --------------------------------------------------------------------------- #
# TranscriptCache                                                               #
# --------------------------------------------------------------------------- #


class TranscriptCache:
    """Flat-file, size-bounded LRU transcript cache.

    All public methods are ``async`` and safe for concurrent calls from one
    asyncio event loop.  All mutable state is protected by ``_lock``.

    Typical lifecycle::

        cache = TranscriptCache(cache_dir="/cache", max_bytes=2<<30)
        await cache.startup_scan()
        cache.start_reconcile_task()

        hit = await cache.get("abc12345678", "en")
        await cache.put("abc12345678", "en", text, segs, "caption_auto", meta)

        await cache.shutdown()

    Plan reference: §Caching (plan.md).
    """

    def __init__(
        self,
        cache_dir: str | Path,
        max_bytes: int,
        *,
        reconcile_sec: int = 300,
    ) -> None:
        self._dir = Path(cache_dir)
        self._max_bytes = max_bytes
        self._reconcile_sec = reconcile_sec
        self._lock: asyncio.Lock = asyncio.Lock()
        # (video_id, lang) -> _CacheUnit; populated by startup_scan()
        self._units: dict[tuple[str, str], _CacheUnit] = {}
        self._total_bytes: int = 0
        self._reconcile_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # Startup / shutdown                                                    #
    # ------------------------------------------------------------------ #

    async def startup_scan(self) -> None:
        """Scan cache dir, build in-memory inventory, clean stray ``.tmp`` files.

        Plan: "startup scan that excludes/cleans stray .tmp"
        Log event: ``cache_startup_scan`` — ``units_found``, ``total_bytes``,
        ``stale_tmp_cleaned``.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        stale_tmp_cleaned = 0
        # Clean stray .tmp files (orphaned by a crash)
        for tmp_path in self._dir.glob("*.tmp"):
            try:
                tmp_path.unlink()
                stale_tmp_cleaned += 1
            except OSError:
                pass

        # Discover units: each .txt file with a valid (11-char-id).(lang) stem
        units: dict[tuple[str, str], _CacheUnit] = {}
        total_bytes = 0

        for txt_path in self._dir.glob("*.txt"):
            key = _parse_key_from_stem(txt_path.stem)
            if key is None:
                continue
            video_id, lang = key
            json_path = self._dir / f"{txt_path.stem}.json"

            try:
                txt_stat = txt_path.stat()
            except OSError:
                continue

            size = txt_stat.st_size
            mtime = txt_stat.st_mtime

            if json_path.exists():
                try:
                    js_stat = json_path.stat()
                    size += js_stat.st_size
                    mtime = max(mtime, js_stat.st_mtime)
                except OSError:
                    pass

            unit = _CacheUnit(
                video_id=video_id,
                lang=lang,
                txt_path=txt_path,
                json_path=json_path,
                size_bytes=size,
                mtime=mtime,
            )
            units[key] = unit
            total_bytes += size

        self._units = units
        self._total_bytes = total_bytes

        log.info(
            "cache_startup_scan",
            units_found=len(units),
            total_bytes=total_bytes,
            stale_tmp_cleaned=stale_tmp_cleaned,
        )

    def start_reconcile_task(self) -> None:
        """Start the background periodic reconcile task.

        Must be called from within a running event loop (i.e. after the server
        has started).  No-op if ``reconcile_sec <= 0``.
        """
        if self._reconcile_sec > 0:
            self._reconcile_task = asyncio.create_task(
                self._reconcile_loop(), name="cache_reconcile"
            )

    async def shutdown(self) -> None:
        """Cancel and await the background reconcile task (if running)."""
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None

    # ------------------------------------------------------------------ #
    # Public API                                                            #
    # ------------------------------------------------------------------ #

    async def get(self, video_id: str, lang: str) -> CacheHit | None:
        """Look up a transcript unit.  Touch both files on hit.

        A caption miss also checks the ``<id>.whisper.*`` fallback (plan:
        "A caption miss also checks the <id>.whisper.* fallback before fetching").

        Log events: ``cache_hit`` (INFO), ``cache_miss`` (INFO).
        """
        async with self._lock:
            pair = self._lookup_locked(video_id, lang)
            if pair is None and lang != "whisper":
                pair = self._lookup_locked(video_id, "whisper")

            if pair is not None:
                unit, hit = pair
                self._touch_unit_locked(unit)
                log.info(
                    "cache_hit",
                    video_id=video_id,
                    lang=hit.lang,
                    source=hit.source,
                    size_bytes=unit.size_bytes,
                )
                return hit

        log.info("cache_miss", video_id=video_id, lang=lang)
        return None

    async def put(
        self,
        video_id: str,
        lang: str,
        text: str,
        segments: list[dict[str, Any]] | None,
        source: str,
        metadata: dict[str, Any] | None,
    ) -> bool:
        """Write a cache unit atomically (``.tmp`` → ``os.replace``).

        Returns ``True`` on success, ``False`` on ENOSPC degrade (caller should
        serve the content but skip caching — not an error).

        Plan: "Byte counter incremented only after os.replace succeeds";
        "ENOSPC: Evict-and-retry once, then degrade to serve-but-don't-cache".

        Log events: ``cache_write`` (DEBUG), ``cache_eviction`` (INFO),
        ``cache_enospc_degrade`` (WARNING).
        """
        txt_content = text.encode("utf-8")
        sidecar: dict[str, Any] = {"source": source}
        if metadata:
            sidecar.update(metadata)
        if segments is not None:
            sidecar["segments"] = segments
        json_content = json.dumps(sidecar, ensure_ascii=False).encode("utf-8")
        needed = len(txt_content) + len(json_content)

        txt_name, json_name = _unit_stems(video_id, lang)
        txt_path = self._dir / txt_name
        json_path = self._dir / json_name

        async with self._lock:
            # Evict LRU units until the new unit would fit
            if self._total_bytes + needed > self._max_bytes:
                await self._evict_lru_locked(needed, trigger="write")

            # Attempt the write; retry once after eviction on ENOSPC
            try:
                self._atomic_write_locked(txt_path, txt_content, json_path, json_content)
            except OSError as exc:
                if exc.errno != 28:  # not ENOSPC — propagate
                    raise
                # ENOSPC: evict more and retry once
                await self._evict_lru_locked(needed, trigger="write")
                try:
                    self._atomic_write_locked(
                        txt_path, txt_content, json_path, json_content
                    )
                except OSError as exc2:
                    if exc2.errno == 28:
                        log.warning(
                            "cache_enospc_degrade", video_id=video_id, lang=lang
                        )
                        return False
                    raise

            # Write succeeded — update accounting
            key = (video_id, lang)
            if key in self._units:
                self._total_bytes -= self._units[key].size_bytes

            unit = _CacheUnit(
                video_id=video_id,
                lang=lang,
                txt_path=txt_path,
                json_path=json_path,
                size_bytes=needed,
                mtime=time.time(),
            )
            self._units[key] = unit
            self._total_bytes += needed

            log.debug(
                "cache_write",
                video_id=video_id,
                lang=lang,
                size_bytes=needed,
                total_cache_bytes=self._total_bytes,
            )
            return True

    async def reconcile(self) -> None:
        """Re-stat all units, recompute total, evict if over cap.

        Plan: "If the reconciled total exceeds YTT_CACHE_MAX_BYTES…immediately
        run the LRU eviction loop under the cache lock until total is at or
        below cap."

        Log event: ``reconcile`` (INFO).
        """
        async with self._lock:
            dead_keys: list[tuple[str, str]] = []
            recomputed = 0

            for key, unit in list(self._units.items()):
                try:
                    txt_stat = unit.txt_path.stat()
                except OSError:
                    # File disappeared — remove from registry
                    dead_keys.append(key)
                    continue

                size = txt_stat.st_size
                mtime = txt_stat.st_mtime
                if unit.json_path.exists():
                    try:
                        js_stat = unit.json_path.stat()
                        size += js_stat.st_size
                        mtime = max(mtime, js_stat.st_mtime)
                    except OSError:
                        pass
                unit.size_bytes = size
                unit.mtime = mtime
                recomputed += size

            for key in dead_keys:
                del self._units[key]

            counter_was = self._total_bytes
            self._total_bytes = recomputed
            drift = recomputed - counter_was
            evictions_triggered = 0

            if recomputed > self._max_bytes:
                log.info(
                    "reconcile_oversize",
                    total=recomputed,
                    cap=self._max_bytes,
                )
                evictions_triggered = await self._evict_lru_locked(0, trigger="reconcile")

            log.info(
                "reconcile",
                recomputed_bytes=recomputed,
                counter_was=counter_was,
                drift_bytes=drift,
                evictions_triggered=evictions_triggered,
            )

    async def evict_lru(self, needed_bytes: int) -> int:
        """Public wrapper: evict oldest units until ``(total + needed_bytes) ≤ max``.

        Returns the number of units evicted.  Acquires ``_lock`` internally.
        """
        async with self._lock:
            return await self._evict_lru_locked(needed_bytes, trigger="write")

    # ------------------------------------------------------------------ #
    # Properties                                                            #
    # ------------------------------------------------------------------ #

    @property
    def total_bytes(self) -> int:
        """Current in-memory byte counter (advisory; not lock-protected)."""
        return self._total_bytes

    @property
    def max_bytes(self) -> int:
        """Configured byte cap."""
        return self._max_bytes

    @property
    def unit_count(self) -> int:
        """Number of cached units in the in-memory registry (advisory)."""
        return len(self._units)

    # ------------------------------------------------------------------ #
    # Internals — must be called with _lock held                           #
    # ------------------------------------------------------------------ #

    def _lookup_locked(
        self, video_id: str, lang: str
    ) -> tuple[_CacheUnit, CacheHit] | None:
        """Find a unit in the registry and read it from disk.  Returns ``None`` on miss."""
        key = (video_id, lang)
        unit = self._units.get(key)
        if unit is None:
            return None

        try:
            text = unit.txt_path.read_text(encoding="utf-8")
        except OSError:
            # File gone (external deletion) — remove from registry
            self._total_bytes -= unit.size_bytes
            del self._units[key]
            return None

        # Read sidecar for source + segments + metadata
        source: str = "caption_auto"
        segments: list[dict[str, Any]] | None = None
        meta: dict[str, Any] = {}

        if unit.json_path.exists():
            try:
                sidecar = json.loads(unit.json_path.read_bytes())
                source = sidecar.pop("source", source)
                segments = sidecar.pop("segments", None)
                meta = sidecar  # remaining keys are metadata fields
            except (OSError, json.JSONDecodeError):
                pass

        hit = CacheHit(
            video_id=video_id,
            lang=lang,
            source=source,
            text=text,
            segments=segments,
            metadata=meta or None,
        )
        return unit, hit

    def _touch_unit_locked(self, unit: _CacheUnit) -> None:
        """Bump mtime on both files and update the in-memory record.

        Plan: "Touch (mtime bump), eviction-selection, and delete all happen
        under the same asyncio lock".
        """
        now = time.time()
        for path in (unit.txt_path, unit.json_path):
            try:
                os.utime(path, (now, now))
            except OSError:
                pass
        unit.mtime = now

    async def _evict_lru_locked(
        self, needed_bytes: int, *, trigger: str = "write"
    ) -> int:
        """Evict oldest units until ``(total + needed_bytes) ≤ max``.

        Must be called with ``_lock`` held.  Returns units evicted.
        Plan: "evict whole units LRU (oldest mtime)".
        Log event: ``cache_eviction`` (INFO).
        """
        evicted = 0
        bytes_freed = 0

        while self._total_bytes + needed_bytes > self._max_bytes and self._units:
            oldest_key = min(self._units, key=lambda k: self._units[k].mtime)
            unit = self._units.pop(oldest_key)
            for path in (unit.txt_path, unit.json_path):
                try:
                    path.unlink()
                except OSError:
                    pass
            self._total_bytes -= unit.size_bytes
            bytes_freed += unit.size_bytes
            evicted += 1

        if evicted:
            log.info(
                "cache_eviction",
                evicted_units=evicted,
                bytes_freed=bytes_freed,
                trigger=trigger,
            )
        return evicted

    def _atomic_write_locked(
        self,
        txt_path: Path,
        txt_content: bytes,
        json_path: Path,
        json_content: bytes,
    ) -> None:
        """Write txt+json via ``.tmp`` → ``os.replace`` (atomic on POSIX).

        Plan: "Atomic writes (…tmp → os.replace)".
        On any error the .tmp files are cleaned up and the exception is re-raised
        so the caller can handle ENOSPC.
        """
        txt_tmp = txt_path.with_suffix(".txt.tmp")
        json_tmp = json_path.with_suffix(".json.tmp")
        try:
            txt_tmp.write_bytes(txt_content)
            json_tmp.write_bytes(json_content)
            os.replace(txt_tmp, txt_path)
            os.replace(json_tmp, json_path)
        except Exception:
            for tmp in (txt_tmp, json_tmp):
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    async def _reconcile_loop(self) -> None:
        """Background task: sleep, reconcile, repeat."""
        while True:
            await asyncio.sleep(self._reconcile_sec)
            try:
                await self.reconcile()
            except Exception:
                log.exception("cache_reconcile_error")
