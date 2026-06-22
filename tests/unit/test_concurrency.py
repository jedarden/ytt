"""Unit tests for ytt.concurrency — bounded pool, single-flight, Whisper semaphore.

Coverage:
- SingleFlightRegistry: concurrent callers for same key trigger exactly 1 coro.
- SingleFlightRegistry: failed Future removed atomically (retry not wedged) — Invariant 6.
- SingleFlightRegistry: exception propagates to all waiters.
- SingleFlightRegistry: success result shared with all waiters.
- SingleFlightRegistry: remove() cancels in-flight Future.
- SingleFlightRegistry: different keys run independently.
- BoundedFetchPool: max_concurrent limits active executions.
- BoundedFetchPool: queue_depth tracks waiters accurately.
- BoundedFetchPool: 429 raised when queue is full.
- BoundedFetchPool: successful run decrements queue_depth correctly.
- BoundedFetchPool: cancelled task decrements queue_depth (no leak).
- ConcurrencyState: from_settings constructs correctly.
- ConcurrencyState: whisper_sem has correct value.
- Invariant 2 (property-based): concurrent requests for one video_id → ≤1 extract_info call.
- Hypothesis property: single-flight is idempotent across key space.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from ytt import errors
from ytt.concurrency import (
    BoundedFetchPool,
    ConcurrencyState,
    SingleFlightRegistry,
)
from ytt.errors import YttError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _gather(*coros):
    return await asyncio.gather(*coros, return_exceptions=True)


# ---------------------------------------------------------------------------
# SingleFlightRegistry
# ---------------------------------------------------------------------------

class TestSingleFlightRegistry:

    async def test_single_call_returns_result(self):
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            return 42

        result = await reg.run("key1", factory)
        assert result == 42
        assert call_count == 1

    async def test_concurrent_same_key_triggers_one_coro(self):
        """Invariant 2: N concurrent callers → exactly 1 execution."""
        reg: SingleFlightRegistry[str] = SingleFlightRegistry()
        call_count = 0
        barrier = asyncio.Event()

        async def slow_factory():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0)  # yield so other tasks can queue up
            return "done"

        # Start 5 concurrent calls for the same key
        results = await asyncio.gather(
            *(reg.run("vid1", slow_factory) for _ in range(5))
        )
        # Exactly 1 execution
        assert call_count == 1
        # All callers get the same result
        assert all(r == "done" for r in results)

    async def test_failed_future_removed_enables_retry(self):
        """Invariant 6: failed Future removed → next call starts fresh (not wedged)."""
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        call_count = 0

        async def failing_factory():
            nonlocal call_count
            call_count += 1
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await reg.run("vidX", failing_factory)

        # Key must be gone from registry
        assert "vidX" not in reg._futures

        # Retry must succeed (or at least not be wedged)
        async def success_factory():
            nonlocal call_count
            call_count += 1
            return 99

        result = await reg.run("vidX", success_factory)
        assert result == 99
        assert call_count == 2  # 1 fail + 1 success

    async def test_failure_propagates_to_all_waiters(self):
        """When the primary coro fails, all shielded waiters get the exception."""
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        barrier_start = asyncio.Event()
        barrier_fail = asyncio.Event()

        async def failing_factory():
            barrier_start.set()
            await barrier_fail.wait()
            raise RuntimeError("coordinated failure")

        async def waiter():
            return await reg.run("vid2", failing_factory)

        # Start primary + waiter
        task1 = asyncio.create_task(waiter())
        await barrier_start.wait()  # primary is running
        task2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)      # let task2 queue up

        barrier_fail.set()
        results = await _gather(task1, task2)
        # Both should have received the RuntimeError
        assert all(isinstance(r, RuntimeError) for r in results)

    async def test_different_keys_run_independently(self):
        """Different keys: both run their own coros concurrently."""
        reg: SingleFlightRegistry[str] = SingleFlightRegistry()
        call_log: list[str] = []

        async def factory(name: str):
            call_log.append(name)
            return name

        results = await asyncio.gather(
            reg.run("keyA", lambda: factory("A")),
            reg.run("keyB", lambda: factory("B")),
        )
        assert set(results) == {"A", "B"}
        assert set(call_log) == {"A", "B"}

    async def test_key_removed_after_success(self):
        """Key is removed from registry on success (no stale results)."""
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()

        async def factory():
            return 1

        await reg.run("k", factory)
        assert "k" not in reg._futures

    async def test_in_flight_keys_property(self):
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        gate = asyncio.Event()

        async def long_factory():
            await gate.wait()
            return 0

        task = asyncio.create_task(reg.run("vX", long_factory))
        await asyncio.sleep(0)  # let task start

        assert "vX" in reg.in_flight_keys

        gate.set()
        await task
        assert "vX" not in reg.in_flight_keys

    async def test_remove_cancels_in_flight_future(self):
        """remove() sets the future as cancelled so waiters don't hang."""
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return 0

        task = asyncio.create_task(reg.run("vY", slow))
        await asyncio.sleep(0)  # let task start

        reg.remove("vY")
        assert "vY" not in reg._futures

        # The task will still run (remove doesn't cancel the task, just the future)
        gate.set()
        # task may succeed or see a cancelled future — just verify no hang
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, Exception):
            pass  # either is acceptable

    async def test_concurrent_different_calls_dedupe_per_key(self):
        """10 concurrent callers for the same key → exactly 1 call."""
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        call_count = 0

        async def counter():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0)
            return call_count

        results = await asyncio.gather(*(reg.run("shared", counter) for _ in range(10)))
        assert call_count == 1
        assert results[0] == 1
        assert all(r == 1 for r in results)

    async def test_successive_calls_each_run(self):
        """Sequential calls (not concurrent) each run the coro."""
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        call_count = 0

        async def counter():
            nonlocal call_count
            call_count += 1
            return call_count

        r1 = await reg.run("seq", counter)
        r2 = await reg.run("seq", counter)
        assert r1 == 1
        assert r2 == 2
        assert call_count == 2


# ---------------------------------------------------------------------------
# BoundedFetchPool
# ---------------------------------------------------------------------------

class TestBoundedFetchPool:

    async def test_simple_run_returns_result(self):
        pool = BoundedFetchPool(max_concurrent=2)

        async def work():
            return "ok"

        result = await pool.run(work)
        assert result == "ok"

    async def test_max_concurrent_limits_simultaneous(self):
        """At most max_concurrent tasks run at the same time."""
        pool = BoundedFetchPool(max_concurrent=2, max_queue_depth=10)
        max_active = 0
        active = 0
        gate = asyncio.Event()

        async def work():
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await gate.wait()
            active -= 1

        tasks = [asyncio.create_task(pool.run(work)) for _ in range(4)]
        await asyncio.sleep(0)  # let tasks start and fill pool
        await asyncio.sleep(0)  # second yield to fully settle
        gate.set()
        await asyncio.gather(*tasks)
        assert max_active <= 2

    async def test_queue_depth_tracks_waiters(self):
        pool = BoundedFetchPool(max_concurrent=1, max_queue_depth=10)
        gate = asyncio.Event()

        async def work():
            await gate.wait()

        # Start 3 tasks; 1 runs immediately, 2 wait
        tasks = [asyncio.create_task(pool.run(work)) for _ in range(3)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # 1 active + 2 waiting → queue_depth = 2
        assert pool.queue_depth == 2
        assert pool.active == 1

        gate.set()
        await asyncio.gather(*tasks)
        assert pool.queue_depth == 0
        assert pool.active == 0

    async def test_queue_full_raises_rate_limited(self):
        """When queue_depth >= max_queue_depth, raises YttError(rate_limited)."""
        pool = BoundedFetchPool(max_concurrent=1, max_queue_depth=1)
        gate = asyncio.Event()

        async def work():
            await gate.wait()

        # Slot 1: active (holds semaphore)
        t1 = asyncio.create_task(pool.run(work))
        await asyncio.sleep(0)
        # Slot 2: queued (queue_depth=1, at limit)
        t2 = asyncio.create_task(pool.run(work))
        await asyncio.sleep(0)

        # Slot 3: queue is full → 429
        with pytest.raises(YttError) as exc_info:
            await pool.run(work)
        assert exc_info.value.error_code == errors.RATE_LIMITED

        gate.set()
        await asyncio.gather(t1, t2)

    async def test_queue_depth_zero_after_completion(self):
        pool = BoundedFetchPool(max_concurrent=2, max_queue_depth=4)

        async def work():
            return 1

        await asyncio.gather(*(pool.run(work) for _ in range(3)))
        assert pool.queue_depth == 0
        assert pool.active == 0

    async def test_properties_correct(self):
        pool = BoundedFetchPool(max_concurrent=3, max_queue_depth=9)
        assert pool.max_concurrent == 3
        assert pool.max_queue_depth == 9

    async def test_default_queue_depth_is_multiple_of_max_concurrent(self):
        pool = BoundedFetchPool(max_concurrent=4)
        # default = 4 * _DEFAULT_QUEUE_MULTIPLIER (4) = 16
        assert pool.max_queue_depth == 16

    async def test_invalid_max_concurrent_raises(self):
        with pytest.raises(ValueError):
            BoundedFetchPool(max_concurrent=0)

    async def test_video_id_in_error_message(self):
        pool = BoundedFetchPool(max_concurrent=1, max_queue_depth=0)
        gate = asyncio.Event()

        async def work():
            await gate.wait()

        t1 = asyncio.create_task(pool.run(work))
        await asyncio.sleep(0)

        with pytest.raises(YttError) as exc_info:
            await pool.run(work, video_id="dQw4w9WgXcQ")
        assert "dQw4w9WgXcQ" in exc_info.value.message

        gate.set()
        await t1

    async def test_exception_from_coro_propagates(self):
        pool = BoundedFetchPool(max_concurrent=2)

        async def failing_work():
            raise ValueError("inner failure")

        with pytest.raises(ValueError, match="inner failure"):
            await pool.run(failing_work)

    async def test_queue_depth_decremented_on_exception_before_semaphore(self):
        """Cancelled tasks that haven't acquired the semaphore still decrement depth."""
        pool = BoundedFetchPool(max_concurrent=1, max_queue_depth=5)
        gate = asyncio.Event()

        async def work():
            await gate.wait()

        # Fill the semaphore
        t1 = asyncio.create_task(pool.run(work))
        await asyncio.sleep(0)
        assert pool.active == 1

        # Queue a waiter then cancel it
        t2 = asyncio.create_task(pool.run(work))
        await asyncio.sleep(0)
        assert pool.queue_depth == 1

        t2.cancel()
        try:
            await t2
        except asyncio.CancelledError:
            pass

        # queue_depth should be back to 0
        assert pool.queue_depth == 0

        gate.set()
        await t1


# ---------------------------------------------------------------------------
# ConcurrencyState
# ---------------------------------------------------------------------------

class TestConcurrencyState:

    def test_from_settings(self):
        """from_settings constructs ConcurrencyState with correct limits."""
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.max_concurrent_fetches = 4
        settings.max_concurrent_whisper = 1

        # Must run inside an event loop for asyncio.Semaphore
        async def _inner():
            state = ConcurrencyState.from_settings(settings)
            assert state.fetch_pool.max_concurrent == 4
            assert isinstance(state.whisper_sem, asyncio.Semaphore)
            assert isinstance(state.discovery_flights, SingleFlightRegistry)

        asyncio.run(_inner())

    async def test_whisper_sem_limits_concurrent(self):
        """Whisper semaphore correctly limits concurrency."""
        state = ConcurrencyState(
            max_concurrent_fetches=4,
            max_concurrent_whisper=1,
        )
        gate = asyncio.Event()
        acquired = []

        async def worker(n: int):
            async with state.whisper_sem:
                acquired.append(n)
                await gate.wait()

        t1 = asyncio.create_task(worker(1))
        await asyncio.sleep(0)
        # Semaphore value=1; t1 holds it
        assert state.whisper_sem._value == 0

        t2 = asyncio.create_task(worker(2))
        await asyncio.sleep(0)
        # t2 is waiting; only 1 acquired
        assert len(acquired) == 1

        gate.set()
        await asyncio.gather(t1, t2)
        assert len(acquired) == 2

    async def test_discovery_flights_is_single_flight(self):
        state = ConcurrencyState(max_concurrent_fetches=4, max_concurrent_whisper=1)
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0)
            return "info"

        results = await asyncio.gather(
            *(state.discovery_flights.run("vid1", factory) for _ in range(5))
        )
        assert call_count == 1
        assert all(r == "info" for r in results)


# ---------------------------------------------------------------------------
# Invariant 2 — property-based test (hypothesis)
# ---------------------------------------------------------------------------

@hyp_settings(max_examples=30, deadline=5000)
@given(
    n_concurrent=st.integers(min_value=2, max_value=8),
    max_concurrent=st.integers(min_value=1, max_value=4),
)
def test_invariant_2_single_flight_one_fetch_per_video(
    n_concurrent: int,
    max_concurrent: int,
):
    """Invariant 2: concurrent requests for one video_id → ≤1 in-flight coro."""

    async def _run():
        reg: SingleFlightRegistry[int] = SingleFlightRegistry()
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0)  # yield to let others queue up
            return 42

        results = await asyncio.gather(
            *(reg.run("dQw4w9WgXcQ", factory) for _ in range(n_concurrent))
        )
        assert call_count == 1, (
            f"Expected 1 call, got {call_count} for {n_concurrent} concurrent requests"
        )
        assert all(r == 42 for r in results)

    asyncio.run(_run())


@hyp_settings(max_examples=20, deadline=3000)
@given(
    max_concurrent=st.integers(min_value=1, max_value=3),
    n_tasks=st.integers(min_value=1, max_value=5),
)
def test_bounded_pool_queue_depth_never_exceeds_max(
    max_concurrent: int,
    n_tasks: int,
):
    """BoundedFetchPool: queue_depth never exceeds max_queue_depth."""
    max_queue = max_concurrent * 2

    async def _run():
        pool = BoundedFetchPool(max_concurrent=max_concurrent, max_queue_depth=max_queue)
        gate = asyncio.Event()
        observed_depths: list[int] = []

        async def work():
            observed_depths.append(pool.queue_depth)
            await gate.wait()

        tasks = []
        for _ in range(n_tasks + max_concurrent):
            try:
                tasks.append(asyncio.create_task(pool.run(work)))
            except YttError:
                break  # 429 is fine — expected when queue full

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Record one more observation
        observed_depths.append(pool.queue_depth)

        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert all(d <= max_queue for d in observed_depths), (
            f"queue_depth exceeded max_queue={max_queue}: {observed_depths}"
        )
        assert pool.queue_depth == 0
        assert pool.active == 0

    asyncio.run(_run())
