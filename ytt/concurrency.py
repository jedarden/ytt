"""Concurrency layer — bounded fetch pool, per-video single-flight, Whisper semaphore.

Implements the plan's §Concurrency requirements:

- **Single-flight** (Invariant 2): For one ``video_id``, concurrent requests
  share exactly one in-flight discovery ``Future``.  Keyed on ``video_id``
  alone (lang-agnostic); after discovery, caption work branches per
  ``(video_id, lang)`` so different-language requests don't deduplicate to one
  served language.  Failed ``Future``s are removed atomically so retries are
  never wedged (Invariant 6).

- **Bounded fetch pool**: ``asyncio.Semaphore(max_concurrent_fetches)`` caps
  simultaneous yt-dlp ``extract_info`` calls.  Requests that can't acquire the
  semaphore immediately are placed in a *bounded* soft-queue (tracked by a
  counter); when ``active + queue_depth >= max_concurrent + max_queue_depth``,
  the request is rejected with a ``429``-style :class:`~ytt.errors.YttError`
  (``error_code=rate_limited``).  This prevents unbounded pile-up on traffic
  spikes.

- **Whisper pool**: a separate ``asyncio.Semaphore(max_concurrent_whisper)``
  so Whisper jobs can't crowd out caption fetches and vice-versa.

All state is **process-local**; correct only at ``replicas:1`` (plan constraint).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Generic, TypeVar

from ytt import errors
from ytt.errors import YttError

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Single-flight registry (Invariants 2 and 6)
# ---------------------------------------------------------------------------

class SingleFlightRegistry(Generic[T]):
    """Dedup concurrent in-flight asyncio coroutines by string key.

    The first caller for a key runs the coroutine; all subsequent callers
    awaiting the same key share the resulting ``asyncio.Future`` via
    ``asyncio.shield`` (so their cancellation does not cancel the primary task).
    On failure the ``Future`` is removed atomically so retries start fresh
    (Invariant 6).  On success the ``Future`` is also removed so sequential
    calls each run their own coroutine (no stale result sharing).

    All access is event-loop-native (no threads); no external locking needed
    because asyncio is cooperative — there is no context switch between the
    ``if key in self._futures`` check and the dict mutation that follows.

    Usage::

        registry = SingleFlightRegistry()

        async def discovery():
            return await asyncio.to_thread(yt_dlp_extract_info, url)

        info = await registry.run("dQw4w9WgXcQ", discovery)
    """

    def __init__(self) -> None:
        # Future[T] keyed by the string key
        self._futures: dict[str, asyncio.Future[T]] = {}

    # -- public ---------------------------------------------------------------

    @property
    def in_flight_keys(self) -> frozenset[str]:
        """Set of keys with in-flight coroutines (snapshot; for metrics/tests)."""
        return frozenset(self._futures)

    async def run(
        self,
        key: str,
        coro_factory: Callable[[], Any],
    ) -> T:
        """Return the result of ``coro_factory()`` for ``key``, deduping concurrent calls.

        If a coroutine for ``key`` is already in-flight, this call awaits the
        *same* ``Future`` (no duplicate execution).  Exceptions propagate to all
        waiters; the failed ``Future`` is removed before propagation so the next
        call for ``key`` starts fresh.

        Parameters
        ----------
        key:
            De-duplication key (e.g. ``video_id`` or ``(video_id, lang)``).
        coro_factory:
            Zero-argument callable that returns an awaitable.  Called **at most
            once** per key per in-flight window.
        """
        if key in self._futures:
            # Another caller is in-flight; share its Future (shielded so that
            # cancellation of THIS waiter does not cancel the primary task).
            existing_fut: asyncio.Future[T] = self._futures[key]
            return await asyncio.shield(existing_fut)

        # We are the first caller; own the Future.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()
        self._futures[key] = fut

        try:
            result: T = await coro_factory()
            # Set result on Future (for any co-waiters that joined mid-flight)
            if not fut.done():
                fut.set_result(result)
            # Remove key BEFORE returning so sequential calls each run fresh.
            self._futures.pop(key, None)
            return result
        except BaseException as exc:
            # Set the exception so any shielded waiters also fail, then remove
            # the Future so the next retry starts clean (Invariant 6).
            if not fut.done():
                fut.set_exception(exc)
            self._futures.pop(key, None)
            raise

    def remove(self, key: str) -> None:
        """Forcefully remove a key (e.g. on timeout before the coro completes).

        Cancels the ``Future`` so any shielded waiters receive
        ``asyncio.CancelledError``.  The primary task that owns the coro is
        **not** cancelled — call ``task.cancel()`` separately if needed.
        """
        fut = self._futures.pop(key, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def __len__(self) -> int:  # pragma: no cover
        return len(self._futures)


# ---------------------------------------------------------------------------
# Bounded fetch pool (plan §Concurrency — bounded queue → 429)
# ---------------------------------------------------------------------------

#: Default queue-depth multiplier: allow up to N×max_concurrent waiters before
#: returning 429.  Not in the Configuration table; a reasonable internal default.
_DEFAULT_QUEUE_MULTIPLIER: int = 4


class BoundedFetchPool:
    """``asyncio.Semaphore`` with a soft bounded queue that returns 429 on overflow.

    Terminology
    -----------
    - **active**: requests currently holding the semaphore and executing.
    - **queue_depth**: requests *waiting* to acquire the semaphore.
    - **total_pending**: ``active + queue_depth``.

    The maximum total_pending is ``max_concurrent + max_queue_depth``.  When
    total_pending reaches that cap, new requests are rejected with
    ``error_code=rate_limited`` (plan §Concurrency "overflow → bounded queue → 429").

    Invariants
    ----------
    - ``active <= max_concurrent`` (enforced by the semaphore).
    - ``total_pending <= max_concurrent + max_queue_depth`` (enforced by the guard).
    - After every completed/failed/cancelled call, ``queue_depth`` and ``active``
      return to their pre-call values.
    """

    def __init__(
        self,
        max_concurrent: int,
        max_queue_depth: int | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._max_queue = (
            max_queue_depth
            if max_queue_depth is not None
            else max_concurrent * _DEFAULT_QUEUE_MULTIPLIER
        )
        self._queue_depth: int = 0
        self._active: int = 0

    # -- properties -----------------------------------------------------------

    @property
    def queue_depth(self) -> int:
        """Number of requests currently waiting to acquire the semaphore."""
        return self._queue_depth

    @property
    def active(self) -> int:
        """Number of requests currently holding the semaphore (running)."""
        return self._active

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def max_queue_depth(self) -> int:
        return self._max_queue

    # -- public ---------------------------------------------------------------

    async def run(
        self,
        coro_factory: Callable[[], Any],
        *,
        video_id: str = "",
    ) -> Any:
        """Acquire the semaphore and execute ``coro_factory()``.

        The guard rejects (``error_code=rate_limited``) when
        ``active + queue_depth >= max_concurrent + max_queue_depth``, i.e. when
        both the pool and its bounded waiting queue are full.

        Parameters
        ----------
        coro_factory:
            Zero-argument callable returning an awaitable.
        video_id:
            Used only for the error message on 429; not part of the concurrency
            key (single-flight is separate).

        Raises
        ------
        YttError(rate_limited):
            When total_pending >= max_concurrent + max_queue_depth.
        """
        # --- 429 guard -------------------------------------------------------
        # ``total_pending`` = currently active + already queued.  Once it would
        # reach max_concurrent + max_queue_depth there is nowhere to put this
        # request, so reject immediately.  No yield between the check and the
        # counter increment, so the test-and-set is atomic in asyncio.
        total_pending = self._active + self._queue_depth
        total_capacity = self._max_concurrent + self._max_queue
        if total_pending >= total_capacity:
            raise YttError(
                errors.RATE_LIMITED,
                f"Fetch pool full "
                f"(active={self._active}/{self._max_concurrent}, "
                f"queued={self._queue_depth}/{self._max_queue}); "
                f"try again shortly."
                + (f" video_id={video_id}" if video_id else ""),
            )

        # Reserve a "queue slot" (this request is now counted in total_pending)
        self._queue_depth += 1
        entered_active = False
        try:
            async with self._sem:
                # Transition from "queued" to "active"
                self._queue_depth -= 1
                self._active += 1
                entered_active = True
                try:
                    return await coro_factory()
                finally:
                    self._active -= 1
        finally:
            # Guard against cancellation / error before we acquired the semaphore
            if not entered_active:
                self._queue_depth -= 1


# ---------------------------------------------------------------------------
# Per-server concurrency state (one instance, held by the server)
# ---------------------------------------------------------------------------

class ConcurrencyState:
    """Container for all per-server concurrency primitives.

    Constructed once at server startup; passed to tool handlers.

    Attributes
    ----------
    fetch_pool:
        Bounded fetch semaphore + queue for caption fetches.
    whisper_sem:
        Semaphore for Whisper transcription jobs (shared CPU service).
    discovery_flights:
        Single-flight registry keyed by ``video_id`` for ``extract_info``.
    """

    def __init__(
        self,
        max_concurrent_fetches: int,
        max_concurrent_whisper: int,
        max_queue_depth: int | None = None,
    ) -> None:
        self.fetch_pool = BoundedFetchPool(
            max_concurrent=max_concurrent_fetches,
            max_queue_depth=max_queue_depth,
        )
        self.whisper_sem = asyncio.Semaphore(max_concurrent_whisper)
        # Single-flight for lang-agnostic extract_info (keyed by video_id)
        self.discovery_flights: SingleFlightRegistry[Any] = SingleFlightRegistry()

    @classmethod
    def from_settings(cls, settings: Any) -> "ConcurrencyState":
        """Construct from a :class:`~ytt.config.Settings` instance."""
        return cls(
            max_concurrent_fetches=settings.max_concurrent_fetches,
            max_concurrent_whisper=settings.max_concurrent_whisper,
        )
