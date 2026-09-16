"""Unit tests for the single-replica tripwire (``ytt.singleton``).

Covered:

- acquire: creates the lockfile, records the holder (pid/hostname/version)
- acquire: second concurrent acquire raises SingletonLockHeld with holder info
- acquire: idempotent within a process (returns the already-held fd)
- release: frees the lock; a fresh acquire then succeeds
- acquire: corrupt/unreadable holder file does not block acquisition
- acquire: flock-unsupported filesystem raises (fail-closed)
- the lockfile is invisible to the cache LRU scan (no unit, no bytes)
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket

import pytest

from ytt import __version__
from ytt.cache import TranscriptCache
from ytt.singleton import (
    LOCK_FILENAME,
    SingletonLockHeld,
    SingletonLockUnavailable,
    acquire_singleton_lock,
    release_singleton_lock,
)


@pytest.fixture(autouse=True)
def _no_stale_process_lock():
    """Every test starts with no lock held by this process."""
    release_singleton_lock()
    yield
    release_singleton_lock()


# ---------------------------------------------------------------------------
# acquire / release
# ---------------------------------------------------------------------------


def test_acquire_creates_lockfile_and_records_holder(tmp_path):
    cache_dir = tmp_path / "cache"
    fd = acquire_singleton_lock(cache_dir)

    assert fd is not None
    lock_path = cache_dir / LOCK_FILENAME
    assert lock_path.exists()

    holder = json.loads(lock_path.read_text(encoding="utf-8"))
    assert holder["pid"] == os.getpid()
    assert holder["hostname"] == socket.gethostname()
    assert holder["version"] == __version__


def test_second_acquire_while_held_raises_with_holder_info(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    lock_path = cache_dir / LOCK_FILENAME

    # Emulate another live holder: a separate open-file description with a
    # pre-written holder record.  flock(2) treats two fds for the same file
    # as independent, so this conflicts exactly as a second process would.
    lock_path.write_text(
        json.dumps(
            {
                "pid": 424242,
                "hostname": "ytt-peer-pod",
                "started_at": "2026-09-16T00:00:00Z",
                "version": "0.0.0-test",
            }
        ),
        encoding="utf-8",
    )
    foreign_fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(foreign_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SingletonLockHeld) as excinfo:
            acquire_singleton_lock(cache_dir)

        holder = excinfo.value.holder
        assert holder is not None
        assert holder["pid"] == 424242
        assert holder["hostname"] == "ytt-peer-pod"
        assert "replicas=1" in str(excinfo.value)
    finally:
        fcntl.flock(foreign_fd, fcntl.LOCK_UN)
        os.close(foreign_fd)


def test_acquire_is_idempotent_within_a_process(tmp_path):
    cache_dir = tmp_path / "cache"
    fd1 = acquire_singleton_lock(cache_dir)
    fd2 = acquire_singleton_lock(cache_dir)
    assert fd1 == fd2


def test_release_frees_the_lock(tmp_path):
    cache_dir = tmp_path / "cache"
    acquire_singleton_lock(cache_dir)
    release_singleton_lock()

    fd = acquire_singleton_lock(cache_dir)  # must win immediately
    assert fd is not None


def test_corrupt_holder_file_does_not_block_acquisition(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / LOCK_FILENAME).write_text("not json at all{{{", encoding="utf-8")

    fd = acquire_singleton_lock(cache_dir)
    assert fd is not None


def test_flock_unsupported_fails_closed(tmp_path, monkeypatch):
    def _no_flock(fd, mode):
        raise OSError(errno.ENOLCK, "flock not supported here")

    monkeypatch.setattr("fcntl.flock", _no_flock)
    with pytest.raises(SingletonLockUnavailable, match="cannot acquire"):
        acquire_singleton_lock(tmp_path / "cache")


def test_lockfile_creation_failure_fails_closed(tmp_path, monkeypatch):
    """A startup check that cannot establish ownership must not serve."""
    cache_dir = tmp_path / "cache"

    def _no_open(*args, **kwargs):
        raise OSError(errno.EROFS, "read-only filesystem")

    monkeypatch.setattr("os.open", _no_open)
    with pytest.raises(SingletonLockUnavailable, match="cannot create"):
        acquire_singleton_lock(cache_dir)


def test_serve_exits_when_singleton_check_cannot_be_verified(tmp_path, monkeypatch):
    """Phase 8 startup must fail closed if ownership cannot be established."""
    from ytt import server
    from ytt.config import Settings

    settings = Settings(
        cache_backend="emptydir",
        cache_dir=str(tmp_path / "cache"),
    )

    def _unavailable(_cache_dir):
        raise SingletonLockUnavailable("cannot acquire single-replica lock")

    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "acquire_singleton_lock", _unavailable)

    assert server.serve() == 1


# ---------------------------------------------------------------------------
# Interaction with the cache LRU scan
# ---------------------------------------------------------------------------


async def test_lockfile_invisible_to_cache_scan(tmp_path):
    """The lock lives in cache_dir but must never be a cache unit.

    Startup scan globs *.txt/*.tmp with 11-char-id stems; the dotfile lock
    matches neither, so it must not be counted (or evicted) by the LRU.
    """
    cache_dir = tmp_path / "cache"
    acquire_singleton_lock(cache_dir)

    cache = TranscriptCache(cache_dir=str(cache_dir), max_bytes=1024)
    await cache.startup_scan()

    assert cache.unit_count == 0
    assert cache.total_bytes == 0
    assert (cache_dir / LOCK_FILENAME).exists()  # untouched by the scan
