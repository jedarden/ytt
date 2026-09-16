"""Single-replica invariant tripwire (plan §Design constraints).

"Single replica (v1). All coordination (job registry, single-flight, cache
byte-counter) is in-process; correct only at ``replicas: 1``. Scale-out is a
redesign."

Three enforcement layers keep the invariant:

1. **Manifest** — every Deployment in ``deploy/k8s/ardenone-cluster/ytt/``
   pins ``replicas: 1`` with ``strategy: Recreate`` (a default RollingUpdate
   with ``maxSurge >= 1`` briefly runs two pods against split in-process
   state).  ``tests/unit/test_single_replica.py`` asserts this against both
   YAML extensions so a future edit cannot silently reintroduce RollingUpdate.
2. **This module** — at startup :func:`ytt.server.serve` takes an exclusive
   ``flock`` on ``<cache_dir>/.ytt-singleton.lock`` and holds it for the
   process lifetime.  The cache volume (PVC, ``ReadWriteOnce``) is the one
   path every replica of the Deployment shares, so a second instance that
   scales in — or any stray process pointed at the same cache dir — fails to
   win the lock and refuses to start (exit 1 → CrashLoopBackOff: loud, not
   silently wrong).  The kernel releases the lock when the holder dies, so
   there is no staleness to reap and restarts need no cleanup.  The manifest
   is the invariant; this lock is the tripwire that turns a violation into a
   visible crash instead of two half-correct servers.
3. **One uvicorn worker** — ``serve()`` hardcodes ``workers=1``; a second
   in-process worker would split the same state within the pod.

Why there is no ``ytt selftest`` probe here: a lock observed *held* from
outside the server process is indistinguishable from the healthy server
itself (``kubectl exec deploy/ytt -- ytt selftest`` runs in its own process),
so a free/held probe would read as a violation on every healthy deployment.
The assertion belongs where a *new* process must win the lock — startup.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

import structlog

from ytt import __version__

log = structlog.get_logger(__name__)

#: Lock file on the cache volume.  A dotfile: invisible to the cache LRU
#: scan, which only globs ``*.txt``/``*.tmp`` with valid 11-char-id stems
#: (see ``ytt.cache.startup_scan``).
LOCK_FILENAME = ".ytt-singleton.lock"

#: Process-lifetime holder.  Never closed outside tests — the kernel drops
#: the flock when the process exits, which is the whole staleness-free design.
_held_fd: int | None = None


class SingletonLockHeld(RuntimeError):
    """Another live ytt process holds the cache-volume singleton lock."""

    def __init__(self, message: str, holder: dict[str, Any] | None) -> None:
        super().__init__(message)
        self.holder = holder


class SingletonLockUnavailable(RuntimeError):
    """The startup tripwire could not create or acquire its lock."""


def _lock_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / LOCK_FILENAME


def _read_holder(path: Path) -> dict[str, Any] | None:
    """Best-effort read of the holder record (pid/hostname/started_at).

    Returns ``None`` for a missing, unreadable, or corrupt file — a stale or
    hand-created lockfile must never block diagnosis, and the flock verdict
    (not the file's contents) is what decides contention.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def acquire_singleton_lock(cache_dir: str | Path) -> int:
    """Take the exclusive singleton lock on the cache volume; hold for life.

    Returns the held fd.  Raises :class:`SingletonLockHeld` when another live
    process already holds the lock, or :class:`SingletonLockUnavailable` when
    the filesystem cannot create or lock the file. Both conditions must stop
    startup: the manifest is the primary guard, while this tripwire makes a
    runtime violation or an unverifiable guard fail closed rather than serve
    split state.

    Idempotent within a process: a second call returns the already-held fd.
    """
    global _held_fd
    if _held_fd is not None:
        return _held_fd

    path = _lock_path(cache_dir)
    holder_self = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": __version__,
    }

    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SingletonLockUnavailable(
            f"cannot create single-replica lockfile {path}: {exc}"
        ) from exc

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        holder = _read_holder(path)
        raise SingletonLockHeld(
            f"another live ytt process holds {path} "
            f"(holder: {_format_holder(holder)}). The single-replica invariant "
            "(plan §Design constraints) forbids two live instances: cache "
            "byte-counter, single-flight, and Whisper job registry are "
            "in-process and correct only at replicas=1. If a scale-out or "
            "RollingUpdate strategy was just introduced, revert it; if this "
            "is a stray process on the same cache dir, stop it.",
            holder,
        ) from None
    except OSError as exc:
        os.close(fd)
        raise SingletonLockUnavailable(
            f"cannot acquire single-replica lock {path}: {exc}"
        ) from exc

    # Lock won — record the holder for the *next* contender's error message.
    # Failure to write is non-fatal: the lock itself is the enforcement.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(holder_self) + "\n").encode("utf-8"))
    except OSError as exc:
        log.warning("singleton_lock_holder_write_failed", error=str(exc))

    _held_fd = fd
    log.info(
        "singleton_lock_acquired",
        lock_path=str(path),
        pid=holder_self["pid"],
        hostname=holder_self["hostname"],
    )
    return fd


def release_singleton_lock() -> None:
    """Drop the held lock (tests only — production relies on process exit)."""
    global _held_fd
    if _held_fd is not None:
        try:
            fcntl.flock(_held_fd, fcntl.LOCK_UN)
        finally:
            os.close(_held_fd)
            _held_fd = None


def _format_holder(holder: dict[str, Any] | None) -> str:
    if not holder:
        return "unrecorded"
    return " ".join(
        f"{k}={holder[k]}" for k in ("pid", "hostname", "started_at", "version")
        if k in holder
    )
