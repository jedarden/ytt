"""Runtime (cross-process) tests for the single-replica flock guard.

``tests/unit/test_singleton.py`` proves the lock *semantics* inside one
process (a second open-file description stands in for the contender, and
``fcntl.flock`` is monkeypatched for the failure branches).  This module
proves the *runtime* behaviour with real OS processes, which is the only
way to observe what the guard exists for:

- one live process acquires ``<cache_dir>/.ytt-singleton.lock`` and a real
  second process cannot win it (flock conflicts across processes, not just
  across fds in one process);
- a competing ``ytt serve`` process fails closed: exit 1, a
  "Single-replica invariant violated" log naming the *holder's* metadata
  (pid/hostname/version), and no uvicorn startup — the loser never serves;
- the lock frees itself when the holder dies: the kernel drops the flock,
  the stale lockfile needs no reaping, and the next process simply wins it
  and overwrites the holder record;
- a real filesystem failure (read-only cache dir) fails closed — the
  monkeypatched-ENOLCK branch stays covered in ``test_singleton.py``;
  here the failure is a genuine ``EACCES`` from the filesystem;
- the one-worker invariant: ``serve()`` hands uvicorn ``workers=1`` and
  already holds the singleton lock when it does — the in-process half of
  "all coordination is correct only at replicas: 1" (plan §Design
  constraints).

Every child is a real ``sys.executable`` subprocess against a tmp cache
dir.  Nothing here touches the network: the loser exits before the startup
egress probe, and the one-worker child stubs ``probe_egress``,
``build_asgi_app`` and ``uvicorn.run``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ytt.singleton import (
    LOCK_FILENAME,
    SingletonLockHeld,
    acquire_singleton_lock,
    release_singleton_lock,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Long enough for a cold subprocess importing ytt (structlog et al.) on a
#: loaded box; children never legitimately run this long.
_CHILD_TIMEOUT_SEC = 180.0

#: Holder child: win the lock, signal readiness, then block until killed.
#: Parent kills it in a finally, so the sleep never expires — production
#: relies on process exit to drop the lock, and so do these tests.
_HOLDER_CODE = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from ytt.singleton import acquire_singleton_lock

    acquire_singleton_lock(sys.argv[1])
    Path(sys.argv[2]).write_text("ready", encoding="utf-8")
    time.sleep(120)
    """
)

#: Competing child: the real ``ytt serve`` entrypoint against a cache dir
#: already locked by another process.  ``import tests.conftest`` installs the
#: same fake-OAuth env defaults + OIDC discovery patch the unit suite uses
#: (``ytt.server`` builds its app at import time, before serve() can fail on
#: the lock) — without it the child would die for the wrong reason.
_LOSER_CODE = textwrap.dedent(
    """
    import tests.conftest  # noqa: F401
    from ytt.cli import main

    raise SystemExit(main(["serve"]))
    """
)

#: One-worker child: drive the real ``serve()`` wiring but record what it
#: hands to ``uvicorn.run`` instead of starting a server.  Patching
#: ``ytt.selftest.probe_egress`` works because serve() imports it inside the
#: function body; ``uvicorn.run`` and ``build_asgi_app`` are plain module
#: attributes.  ``lock_held`` proves the lock is taken *before* uvicorn runs.
_ONE_WORKER_CODE = textwrap.dedent(
    """
    import json, sys
    import tests.conftest  # noqa: F401
    import uvicorn
    from ytt import selftest as _selftest
    from ytt import server
    from ytt import singleton


    class _FakeReport:
        ip = "203.0.113.7"
        asn = "64500"
        org = "TEST-ORG"
        via_proxy = False
        is_residential = True


    _selftest.probe_egress = lambda proxy_url: _FakeReport()

    recorded = []

    def _fake_run(app, **kwargs):
        recorded.append(
            {"workers": kwargs.get("workers"), "lock_held": singleton._held_fd is not None}
        )

    uvicorn.run = _fake_run
    server.build_asgi_app = lambda: object()

    rc = server.serve()
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(recorded, fh)
    raise SystemExit(rc)
    """
)


@pytest.fixture(autouse=True)
def _parent_holds_no_lock():
    """The pytest process itself must never hold the lock across tests."""
    release_singleton_lock()
    yield
    release_singleton_lock()


def _child_env(cache_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["YTT_CACHE_DIR"] = str(cache_dir)
    # emptydir backend: validate_storage() only warns, so a loser child's
    # exit 1 is attributable to the lock and nothing else.
    env["YTT_CACHE_BACKEND"] = "emptydir"
    # Same fake pair tests/conftest.py sets for the unit suite.
    env.setdefault("YTT_OAUTH_CLIENT_ID", "test-client-id")
    env.setdefault("YTT_OAUTH_CLIENT_SECRET", "test-client-secret")
    return env


def _spawn_holder(cache_dir: Path, ready_file: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER_CODE, str(cache_dir), str(ready_file)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _kill(proc: subprocess.Popen) -> None:
    """SIGKILL and reap — SIGKILL is the strongest form of the death path the
    kernel release design must survive (no atexit, no handlers run)."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _wait_ready(proc: subprocess.Popen, ready_file: Path) -> None:
    deadline = time.monotonic() + _CHILD_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if ready_file.exists():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    _kill(proc)
    stderr = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")
    raise AssertionError(
        f"child pid {proc.returncode} never signalled readiness; stderr:\n{stderr}"
    )


def _run_child(
    code: str, cache_dir: Path, *args: Path
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [sys.executable, "-c", code, *[str(a) for a in args]],
            cwd=REPO_ROOT,
            env=_child_env(cache_dir),
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"child timed out after {_CHILD_TIMEOUT_SEC}s; "
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        ) from exc


# ---------------------------------------------------------------------------
# One process acquires; a real second process cannot
# ---------------------------------------------------------------------------


def test_one_process_holds_lock_and_real_peer_cannot_acquire(tmp_path):
    cache_dir = tmp_path / "cache"
    ready = tmp_path / "holder.ready"
    holder = _spawn_holder(cache_dir, ready)
    try:
        _wait_ready(holder, ready)

        # The lockfile names the holder process — a real pid, not this one.
        holder_record = json.loads(
            (cache_dir / LOCK_FILENAME).read_text(encoding="utf-8")
        )
        assert holder_record["pid"] == holder.pid
        assert holder_record["pid"] != os.getpid()

        # This process is now a genuine competing process: the flock conflict
        # is cross-process (independent open-file descriptions in independent
        # processes), not the same-fd emulation test_singleton.py uses.
        with pytest.raises(SingletonLockHeld) as excinfo:
            acquire_singleton_lock(cache_dir)
        assert excinfo.value.holder is not None
        assert excinfo.value.holder["pid"] == holder.pid
    finally:
        _kill(holder)


# ---------------------------------------------------------------------------
# A competing `ytt serve` process fails closed, with holder metadata
# ---------------------------------------------------------------------------


def test_competing_serve_process_exits_1_with_holder_metadata(tmp_path):
    cache_dir = tmp_path / "cache"
    ready = tmp_path / "holder.ready"
    holder = _spawn_holder(cache_dir, ready)
    try:
        _wait_ready(holder, ready)

        loser = _run_child(_LOSER_CODE, cache_dir)

        assert loser.returncode == 1, (
            f"expected the competing serve to exit 1; stdout:\n{loser.stdout}\n"
            f"stderr:\n{loser.stderr}"
        )
        # Fail-closed log names the holder — an operator reading the CrashLoop
        # can find the offending process without exec'ing into the pod.
        assert "Single-replica invariant violated" in loser.stdout
        assert f"pid={holder.pid}" in loser.stdout
        assert "hostname=" in loser.stdout
        # …and it exited *before* serving: no startup-complete event, no
        # uvicorn banner.  Failure to serve split state is the entire design.
        assert "Server startup" not in loser.stdout
        assert "Uvicorn running" not in loser.stdout
    finally:
        _kill(holder)


# ---------------------------------------------------------------------------
# The lock is available again once the holder dies — with no cleanup
# ---------------------------------------------------------------------------


def test_lock_becomes_available_after_holder_process_death(tmp_path):
    cache_dir = tmp_path / "cache"
    lock_path = cache_dir / LOCK_FILENAME
    ready_a = tmp_path / "holder-a.ready"
    holder_a = _spawn_holder(cache_dir, ready_a)
    try:
        _wait_ready(holder_a, ready_a)
        assert lock_path.exists()
    finally:
        _kill(holder_a)

    assert holder_a.returncode != 0  # SIGKILL, not a clean exit
    # The lockfile survives the death — the kernel releases only the flock,
    # nothing reaps the file.  It must not block the next process.
    assert lock_path.exists()

    ready_b = tmp_path / "holder-b.ready"
    holder_b = _spawn_holder(cache_dir, ready_b)
    try:
        # No cleanup between death and this acquire: the whole staleness-free
        # design is that a fresh process wins immediately.
        _wait_ready(holder_b, ready_b)

        # The holder record was rewritten for the new process…
        holder_record = json.loads(lock_path.read_text(encoding="utf-8"))
        assert holder_record["pid"] == holder_b.pid
        # …and the flock really transferred: this process now loses to B.
        with pytest.raises(SingletonLockHeld) as excinfo:
            acquire_singleton_lock(cache_dir)
        assert excinfo.value.holder is not None
        assert excinfo.value.holder["pid"] == holder_b.pid
    finally:
        _kill(holder_b)


# ---------------------------------------------------------------------------
# Real filesystem failure fails closed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root ignores directory write bits — cannot stage a real EACCES",
)
def test_readonly_cache_dir_fails_closed(tmp_path):
    """The startup check cannot establish ownership of an unwritable cache
    dir → serve exits 1 with the fail-closed log, before uvicorn starts."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    os.chmod(cache_dir, 0o555)
    try:
        loser = _run_child(_LOSER_CODE, cache_dir)

        assert loser.returncode == 1, (
            f"expected exit 1 on an unwritable cache dir; stdout:\n{loser.stdout}\n"
            f"stderr:\n{loser.stderr}"
        )
        assert "Single-replica invariant violated" in loser.stdout
        assert "cannot create single-replica lockfile" in loser.stdout
        assert "Server startup" not in loser.stdout
    finally:
        os.chmod(cache_dir, 0o755)


# ---------------------------------------------------------------------------
# The one-worker invariant
# ---------------------------------------------------------------------------


def test_serve_hands_uvicorn_exactly_one_worker_and_holds_the_lock(tmp_path):
    """``serve()`` must run uvicorn with workers=1 (a second in-process
    worker would split the same state within the pod) and must already hold
    the singleton lock at that point — i.e. the lock is taken before the
    server starts accepting connections, never after."""
    report = tmp_path / "uvicorn-run.json"
    child = _run_child(_ONE_WORKER_CODE, tmp_path / "cache", report)

    assert child.returncode == 0, (
        f"serve() child failed; stdout:\n{child.stdout}\nstderr:\n{child.stderr}"
    )
    assert report.exists(), f"child never reached uvicorn.run; stdout:\n{child.stdout}"

    calls = json.loads(report.read_text(encoding="utf-8"))
    assert calls == [{"workers": 1, "lock_held": True}]
    # The real startup sequence ran up to uvicorn: storage validated, lock
    # acquired, "Server startup" logged — only uvicorn.run itself was stubbed.
    assert "Server startup" in child.stdout
