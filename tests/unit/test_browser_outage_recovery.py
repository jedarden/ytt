"""Outage recovery for the browser-primary fetch (ytt.browser_outage).

Integration-level and all offline: the real router (:func:`ytt.fetch.fetch_transcript`
where noted), the real transport fetch
(:func:`ytt.browser_fetch.browser_fetch_transcript`) and the real recovery
policy run together — only the sidecar is fake, scripted per attempt.

What is pinned here, and why (spec: docs/notes/browser-outage-recovery.md):

- every outage shape is **bridged**: a fetch whose attempt died at connect,
  mid-navigation, while driving the player, or mid-caption-wait is
  re-attempted whole and succeeds against the fresh server — the six-hour
  recycle window is the motivating case;
- a crash **mid-capture** escapes the transport as a raw Playwright error —
  it must surface as ``BrowserInfraError(connection_lost)`` and never leak
  to a tool caller, while an unrecognised exception is ``unclassified`` and
  is deliberately **not** retried;
- verdict-shaped failures (``timeout``, ``empty_body``, ``rate_limited``) and
  video-level errors are **not** retried — the yt-dlp fallback sees them
  after exactly one browser attempt;
- a persistent outage exhausts the attempts and falls back to yt-dlp with
  the browser failure named; a bridged outage never touches yt-dlp;
- retries are paced by a backoff, hold the browser-concurrency slot only
  while an attempt is actually running, and stay observable: one metric
  increment per attempt plus a warning log line per retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest

from ytt import browser_outage, errors, fetch
from ytt.browser_fetch import (
    NATURAL_JS,
    PLAYER_INFO_JS,
    SELECT_TRACK_JS,
    BrowserInfraError,
)
from ytt.browser_outage import (
    BROWSER_ATTEMPTS,
    RETRYABLE_REASONS,
    classify_raw_failure,
    fetch_with_outage_recovery,
    looks_like_disconnect,
)
from ytt.config import Settings
from ytt.errors import NoCaptionsError, YttError
from ytt.fetch import FetchResult

WS = "ws://ytt-browser.test.local:3001/"


class TargetClosedError(Exception):
    """Mimics Playwright's TargetClosedError — the classifier keys on the
    class-name shape, so a local class with the same name exercises the same
    path as the real one."""


def make_settings(**overrides: object) -> Settings:
    env = {
        "YTT_ALLOWED_SUBJECTS": "test-sub",
        "YTT_CACHE_BACKEND": "emptydir",
        "YTT_CACHE_DIR": "/tmp",
        "YTT_SCRATCH_DIR": "/tmp",
        "YTT_BROWSER_WS_URL": WS,
        "YTT_BROWSER_TIMEOUT_SEC": "5",
        **{f"YTT_{k.upper()}": str(v) for k, v in overrides.items()},
    }
    with patch.dict(os.environ, env, clear=False):
        return Settings()


def json3(*words: str) -> bytes:
    events = [
        {"tStartMs": i * 1000, "dDurationMs": 900, "segs": [{"utf8": w}]}
        for i, w in enumerate(words)
    ]
    return json.dumps({"events": events}).encode()


def healthy_player(tracks=(("en", ""),)) -> dict:
    return {
        "playability": "OK",
        "reason": "",
        "subreason": "",
        "title": "A Title",
        "author": "A Channel",
        "length_sec": "125",
        "is_live": False,
        "is_upcoming": False,
        "publish_date": None,
        "default_caption_index": 0,
        "tracks": [{"lang": lang, "kind": kind} for lang, kind in tracks],
    }


# ---------------------------------------------------------------------------
# The scripted sidecar
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    """What the fake sidecar does for ONE ytt fetch attempt.

    ``crash`` names the phase where the server process dies (the six-hour
    recycle or a crash): ``"context"`` / ``"goto"`` / ``"natural"`` raise the
    disconnect from that exact call, ``"wait"`` from every
    ``wait_for_timeout`` (the fetch is inside its caption wait when the
    recycle hits — this is the raw-escape path).  ``crash_exc`` overrides the
    raised exception (default: the TargetClosed shape).
    """

    player: dict | None = None
    body: bytes | None = None
    status: int = 200
    connect_error: Exception | None = None
    crash: str | None = None
    crash_exc: Exception | None = None
    hang_goto: bool = False

    def crash_exception(self) -> Exception:
        return self.crash_exc or TargetClosedError(
            "Target page, context or browser has been closed"
        )


@dataclass
class AttemptLog:
    connects: list[float] = field(default_factory=list)
    goto_urls: list[str] = field(default_factory=list)
    selected: list[dict] = field(default_factory=list)
    closed: int = 0


class FakeResponse:
    def __init__(self, url: str, status: int, body: bytes) -> None:
        self.url = url
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body


class FakePage:
    def __init__(self, attempt: Attempt, log: AttemptLog) -> None:
        self.attempt = attempt
        self.log = log
        self._cb = None

    def on(self, event: str, cb) -> None:
        assert event == "response"
        self._cb = cb

    def _emit(self, lang: str, kind: str) -> None:
        body = self.attempt.body if self.attempt.body is not None else json3("ok")
        q = f"v=VID&lang={lang}&fmt=json3&pot=TOKEN"
        if kind:
            q += f"&kind={kind}"
        assert self._cb is not None
        self._cb(
            FakeResponse(
                f"https://www.youtube.com/api/timedtext?{q}",
                self.attempt.status,
                body,
            )
        )

    async def goto(self, url: str, **kwargs) -> None:
        self.log.goto_urls.append(url)
        if self.attempt.hang_goto:
            await asyncio.sleep(3600)
        if self.attempt.crash == "goto":
            raise self.attempt.crash_exception()

    async def evaluate(self, script: str, arg=None):
        if script == PLAYER_INFO_JS:
            return healthy_player() if self.attempt.player is None else self.attempt.player
        if script == NATURAL_JS:
            if self.attempt.crash == "natural":
                raise self.attempt.crash_exception()
            return True
        if script == SELECT_TRACK_JS:
            self.log.selected.append(arg)
            self._emit(arg["lang"], arg["kind"])
            return {"ok": True, "via": "tracklist"}
        raise AssertionError(f"unexpected page script: {script[:40]!r}")

    async def wait_for_timeout(self, ms: int) -> None:
        if self.attempt.crash == "wait":
            raise self.attempt.crash_exception()
        await asyncio.sleep(0)

    async def click(self, selector: str, **kwargs) -> None:
        return None


class FakeContext:
    def __init__(self, attempt: Attempt, log: AttemptLog) -> None:
        self.attempt = attempt
        self.log = log

    async def add_init_script(self, script: str) -> None:
        return None

    async def new_page(self) -> FakePage:
        return FakePage(self.attempt, self.log)


class FakeBrowser:
    def __init__(self, attempt: Attempt, log: AttemptLog) -> None:
        self.attempt = attempt
        self.log = log

    async def new_context(self, **kwargs) -> FakeContext:
        if self.attempt.crash == "context":
            raise self.attempt.crash_exception()
        return FakeContext(self.attempt, self.log)

    async def close(self) -> None:
        self.log.closed += 1


class SidecarScript:
    """A flat queue of :class:`Attempt`, one per transport fetch attempt.

    ``browser_fetch`` opens one playwright context per attempt, so each
    ``factory()`` call pops the next attempt.  Running out of scripted
    attempts fails the test with a retry-count-drift message — exactly what
    the attempt-count assertions below pin.
    """

    def __init__(self, *attempts: Attempt) -> None:
        self.attempts = list(attempts)
        self.i = 0
        self.logs: list[AttemptLog] = []

    def next(self) -> tuple[Attempt, AttemptLog]:
        if self.i >= len(self.attempts):
            raise AssertionError(
                f"transport made attempt {self.i + 1} but only "
                f"{len(self.attempts)} scripted — retry-count drift"
            )
        attempt = self.attempts[self.i]
        self.i += 1
        log = AttemptLog()
        self.logs.append(log)
        return attempt, log

    @property
    def connect_count(self) -> int:
        return sum(len(log.connects) for log in self.logs)


def install(
    monkeypatch: pytest.MonkeyPatch,
    script: SidecarScript,
    *,
    backoff: float = 0.0,
) -> SidecarScript:
    """Point ``browser_fetch`` at the scripted sidecar and shrink the clocks."""

    def factory():
        attempt, log = script.next()

        class _Chromium:
            async def connect(self, endpoint: str, **kwargs) -> FakeBrowser:
                log.connects.append(time.monotonic())
                if attempt.connect_error is not None:
                    raise attempt.connect_error
                return FakeBrowser(attempt, log)

        class _PW:
            chromium = _Chromium()

        @asynccontextmanager
        async def ctx():
            yield _PW()

        return ctx

    from ytt import browser_fetch

    monkeypatch.setattr(browser_fetch, "_playwright_factory", lambda: factory())
    monkeypatch.setattr(browser_fetch, "TRACK_WAIT_SEC", 0.2)
    monkeypatch.setattr(browser_fetch, "NATURAL_WAIT_SEC", 0.05)
    monkeypatch.setattr(browser_fetch, "EMPTY_GRACE_SEC", 0.05)
    monkeypatch.setattr(browser_outage, "BROWSER_RETRY_BACKOFF_SEC", backoff)
    return script


def ok() -> Attempt:
    return Attempt()


# ---------------------------------------------------------------------------
# The disconnect classifier
# ---------------------------------------------------------------------------


class TestDisconnectClassifier:
    def test_target_closed_by_name(self) -> None:
        assert looks_like_disconnect(TargetClosedError("x"))

    def test_connection_errors_by_type(self) -> None:
        assert looks_like_disconnect(ConnectionRefusedError("refused"))
        assert looks_like_disconnect(ConnectionResetError("reset"))

    def test_transport_message_shapes(self) -> None:
        class Generic(Exception):
            pass

        assert looks_like_disconnect(Generic("Connection closed unexpectedly"))
        assert looks_like_disconnect(Generic("Browser has been closed"))

    def test_ordinary_errors_are_not_disconnects(self) -> None:
        assert not looks_like_disconnect(ValueError("nope"))
        assert not looks_like_disconnect(AssertionError("assert"))
        assert not looks_like_disconnect(TimeoutError("too slow"))

    def test_classify_raw_failure_labels(self) -> None:
        assert classify_raw_failure(TargetClosedError("x")) == "connection_lost"
        assert classify_raw_failure(ValueError("nope")) == "unclassified"

    def test_retryable_reasons_are_exactly_the_outage_shapes(self) -> None:
        """The four phases an outage can kill a fetch in — and nothing else."""
        assert RETRYABLE_REASONS == frozenset(
            {"connect", "navigate", "player_script", "connection_lost"}
        )
        # Verdicts about the egress/token, whole-budget exhaustion, player
        # behaviour and the unknown are deliberately absent.
        assert not RETRYABLE_REASONS & {
            "timeout",
            "empty_body",
            "rate_limited",
            "playability_blocked",
            "no_request",
            "no_player_response",
            "bad_body",
            "empty_parse",
            "unclassified",
        }
        assert BROWSER_ATTEMPTS == 3


# ---------------------------------------------------------------------------
# Outage bridging (real wrapper + real transport fetch, scripted sidecar)
# ---------------------------------------------------------------------------


class TestOutageBridging:
    async def test_connect_refused_during_recycle_window_is_bridged(
        self, monkeypatch
    ) -> None:
        """The server process is mid-restart: attempt 1 cannot connect,
        attempt 2 (a second later, in production) gets the fresh server."""
        sc = install(
            monkeypatch,
            SidecarScript(
                Attempt(connect_error=ConnectionRefusedError("restart in progress")),
                ok(),
            ),
        )
        res = await fetch_with_outage_recovery("VID", None, make_settings())
        assert [s.text for s in res.segments] == ["ok"]
        assert sc.connect_count == 2

    async def test_crash_while_creating_the_context_is_bridged(
        self, monkeypatch
    ) -> None:
        sc = install(monkeypatch, SidecarScript(Attempt(crash="context"), ok()))
        res = await fetch_with_outage_recovery("VID", None, make_settings())
        assert res.served_lang == "en"
        assert sc.connect_count == 2

    async def test_crash_during_navigation_is_bridged(self, monkeypatch) -> None:
        sc = install(monkeypatch, SidecarScript(Attempt(crash="goto"), ok()))
        res = await fetch_with_outage_recovery("VID", None, make_settings())
        assert res.served_lang == "en"
        assert sc.connect_count == 2

    async def test_crash_while_driving_the_player_is_bridged(
        self, monkeypatch
    ) -> None:
        sc = install(monkeypatch, SidecarScript(Attempt(crash="natural"), ok()))
        res = await fetch_with_outage_recovery("VID", None, make_settings())
        assert res.served_lang == "en"
        assert sc.connect_count == 2

    async def test_crash_during_the_caption_wait_is_bridged(self, monkeypatch) -> None:
        """The six-hour recycle landing on the caption-capture wait: the
        raw Playwright escape must be classified, retried and forgotten."""
        sc = install(monkeypatch, SidecarScript(Attempt(crash="wait"), ok()))
        res = await fetch_with_outage_recovery("VID", None, make_settings())
        assert [s.text for s in res.segments] == ["ok"]
        assert sc.connect_count == 2

    async def test_persistent_outage_exhausts_the_attempts(self, monkeypatch) -> None:
        sc = install(
            monkeypatch,
            SidecarScript(*[Attempt(connect_error=ConnectionRefusedError("down"))] * 3),
        )
        with pytest.raises(BrowserInfraError) as ei:
            await fetch_with_outage_recovery("VID", None, make_settings())
        assert ei.value.reason == "connect"
        assert sc.connect_count == BROWSER_ATTEMPTS

    async def test_crash_on_every_attempt_is_connection_lost(self, monkeypatch) -> None:
        sc = install(
            monkeypatch, SidecarScript(*[Attempt(crash="wait")] * BROWSER_ATTEMPTS)
        )
        with pytest.raises(BrowserInfraError) as ei:
            await fetch_with_outage_recovery("VID", None, make_settings())
        assert ei.value.reason == "connection_lost"
        assert sc.connect_count == BROWSER_ATTEMPTS

    async def test_unknown_exception_is_unclassified_and_not_retried(
        self, monkeypatch
    ) -> None:
        """A bug-shaped exception must fail loudly once, never be retried,
        and still reach the router as an infra failure."""
        sc = install(
            monkeypatch,
            SidecarScript(Attempt(crash="wait", crash_exc=ValueError("a bug"))),
        )
        with pytest.raises(BrowserInfraError) as ei:
            await fetch_with_outage_recovery("VID", None, make_settings())
        assert ei.value.reason == "unclassified"
        assert "ValueError" in str(ei.value)
        assert sc.connect_count == 1


# ---------------------------------------------------------------------------
# What is deliberately NOT retried
# ---------------------------------------------------------------------------


class TestNoRetryForVerdicts:
    async def test_timeout_is_not_retried(self, monkeypatch) -> None:
        sc = install(
            monkeypatch,
            SidecarScript(Attempt(hang_goto=True)),
        )
        with pytest.raises(BrowserInfraError) as ei:
            await fetch_with_outage_recovery(
                "VID", None, make_settings(browser_timeout_sec=1)
            )
        assert ei.value.reason == "timeout"
        assert sc.connect_count == 1  # the budget is spent; a retry re-burns it

    async def test_empty_body_is_not_retried(self, monkeypatch) -> None:
        sc = install(monkeypatch, SidecarScript(Attempt(body=b"")))
        with pytest.raises(BrowserInfraError) as ei:
            await fetch_with_outage_recovery("VID", None, make_settings())
        assert ei.value.reason == "empty_body"
        assert sc.connect_count == 1

    async def test_rate_limited_is_not_retried(self, monkeypatch) -> None:
        sc = install(
            monkeypatch, SidecarScript(Attempt(status=429, body=b""))
        )
        with pytest.raises(BrowserInfraError) as ei:
            await fetch_with_outage_recovery("VID", None, make_settings())
        assert ei.value.reason == "rate_limited"
        assert sc.connect_count == 1

    @pytest.mark.parametrize(
        "player",
        [
            {**healthy_player(), "playability": "ERROR",
             "reason": "This video is unavailable"},
            {**healthy_player(tracks=()), "length_sec": "321"},
        ],
        ids=["unavailable", "no-captions"],
    )
    async def test_video_level_answers_are_never_retried(
        self, monkeypatch, player: dict
    ) -> None:
        """Authoritative answers: one attempt, raised as-is, no retry and
        (pinned in TestRouter below) no yt-dlp fallback."""
        sc = install(monkeypatch, SidecarScript(Attempt(player=player)))
        with pytest.raises(YttError):
            await fetch_with_outage_recovery("VID", None, make_settings())
        assert sc.connect_count == 1


# ---------------------------------------------------------------------------
# Retry pacing and slot discipline
# ---------------------------------------------------------------------------


class TestRetryPacing:
    async def test_backoff_paces_the_retries(self, monkeypatch) -> None:
        sc = install(
            monkeypatch,
            SidecarScript(
                Attempt(connect_error=ConnectionRefusedError("x")),
                Attempt(connect_error=ConnectionRefusedError("x")),
                ok(),
            ),
            backoff=0.05,
        )
        await fetch_with_outage_recovery("VID", None, make_settings())
        assert sc.connect_count == 3
        times = sorted(t for log in sc.logs for t in log.connects)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert len(gaps) == 2 and all(g >= 0.05 for g in gaps)

    async def test_backoff_sleeps_outside_the_concurrency_slot(
        self, monkeypatch
    ) -> None:
        """A retrying fetch must not hold the browser-concurrency slot while
        it backs off: with max_concurrency=1 another fetch runs to completion
        during the first one's backoff."""
        sc = install(
            monkeypatch,
            SidecarScript(
                Attempt(connect_error=ConnectionRefusedError("x")),
                ok(),
                ok(),
            ),
            backoff=0.3,
        )
        settings = make_settings(browser_max_concurrency=1)

        first = asyncio.create_task(
            fetch_with_outage_recovery("A", None, settings)
        )
        while sc.connect_count < 1:  # first attempt failed, backoff starts
            await asyncio.sleep(0)
        t_before = time.monotonic()
        second = asyncio.create_task(
            fetch_with_outage_recovery("B", None, settings)
        )
        res_b = await second
        t_b_done = time.monotonic()
        res_a = await first
        assert res_a.served_lang == "en" and res_b.served_lang == "en"
        # B finished while A was still backing off (well inside 0.3 s) —
        # the slot was free during the sleep.
        assert t_b_done - t_before < 0.3

    async def test_retry_logs_a_warning_with_the_reason(
        self, monkeypatch, caplog
    ) -> None:
        install(
            monkeypatch,
            SidecarScript(Attempt(connect_error=ConnectionRefusedError("x")), ok()),
        )
        with caplog.at_level(logging.WARNING, logger="ytt.browser_outage"):
            await fetch_with_outage_recovery("VID", None, make_settings())
        assert any(
            "browser fetch attempt 1/3 failed (connect" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Observability: attempts, not user fetches
# ---------------------------------------------------------------------------


def _count(outcome: str) -> float:
    from ytt.observability import ytt_browser_fetch_total

    return ytt_browser_fetch_total.labels(outcome=outcome)._value.get()


class TestAttemptMetrics:
    async def test_a_bridged_outage_counts_one_failed_attempt_and_one_ok(
        self, monkeypatch
    ) -> None:
        """``ytt_browser_fetch_total`` counts attempts: a bridged outage is
        visible (infra_error +1) even though the user fetch succeeded —
        hiding the retry would hide the drift."""
        ok0, ie0 = _count("ok"), _count("infra_error")
        install(
            monkeypatch,
            SidecarScript(Attempt(crash="wait"), ok()),
        )
        await fetch_with_outage_recovery("VID", None, make_settings())
        assert (_count("ok") - ok0, _count("infra_error") - ie0) == (1, 1)


# ---------------------------------------------------------------------------
# The router (ytt.fetch.fetch_transcript) with a mocked transport fetch
# ---------------------------------------------------------------------------


def _ok_result() -> FetchResult:
    return FetchResult(
        segments=[],
        source="caption_auto",
        served_lang="en",
        requested_lang=None,
        available_langs=["en"],
    )


def _infra(reason: str) -> BrowserInfraError:
    return BrowserInfraError(f"failure ({reason})", reason=reason)


class TestRouter:
    """The full router path with the transport fetch mocked — the wiring the
    server actually calls."""

    async def test_bridged_outage_never_touches_ytdlp(self) -> None:
        with patch(
            "ytt.browser_fetch.browser_fetch_transcript",
            AsyncMock(side_effect=[_infra("connect"), _ok_result()]),
        ) as b, patch.object(
            fetch, "fetch_transcript_ytdlp", AsyncMock()
        ) as y:
            res = await fetch.fetch_transcript("VID", None, make_settings())
        assert res.served_lang == "en"
        assert b.await_count == 2
        y.assert_not_awaited()

    async def test_persistent_outage_falls_back_after_the_attempts(self) -> None:
        with patch(
            "ytt.browser_fetch.browser_fetch_transcript",
            AsyncMock(side_effect=_infra("connect")),
        ) as b, patch.object(
            fetch,
            "fetch_transcript_ytdlp",
            AsyncMock(return_value=_ok_result()),
        ) as y:
            res = await fetch.fetch_transcript("VID", None, make_settings())
        assert res.served_lang == "en"
        assert b.await_count == BROWSER_ATTEMPTS
        y.assert_awaited_once()

    async def test_raw_transport_crash_reaches_the_fallback_never_the_caller(
        self,
    ) -> None:
        """The regression this layer closes: a mid-capture crash used to
        escape as a raw Playwright error; now it is connection_lost, retried,
        and finally handed to the yt-dlp fallback."""
        with patch(
            "ytt.browser_fetch.browser_fetch_transcript",
            AsyncMock(
                side_effect=TargetClosedError(
                    "Target page, context or browser has been closed"
                )
            ),
        ) as b, patch.object(
            fetch,
            "fetch_transcript_ytdlp",
            AsyncMock(return_value=_ok_result()),
        ) as y:
            res = await fetch.fetch_transcript("VID", None, make_settings())
        assert res.served_lang == "en"
        assert b.await_count == BROWSER_ATTEMPTS
        y.assert_awaited_once()

    async def test_video_level_errors_do_not_fall_back_or_retry(self) -> None:
        exc = YttError(errors.UNAVAILABLE, "Video unavailable")
        with patch(
            "ytt.browser_fetch.browser_fetch_transcript",
            AsyncMock(side_effect=exc),
        ) as b, patch.object(fetch, "fetch_transcript_ytdlp", AsyncMock()) as y:
            with pytest.raises(YttError) as ei:
                await fetch.fetch_transcript("VID", None, make_settings())
        assert ei.value is exc
        assert b.await_count == 1
        y.assert_not_awaited()

    async def test_no_captions_keeps_its_type_through_the_wrapper(self) -> None:
        nc = NoCaptionsError("none", duration_sec=60.0)
        with patch(
            "ytt.browser_fetch.browser_fetch_transcript",
            AsyncMock(side_effect=nc),
        ), patch.object(fetch, "fetch_transcript_ytdlp", AsyncMock()) as y:
            with pytest.raises(NoCaptionsError) as ei:
                await fetch.fetch_transcript("VID", None, make_settings())
        assert ei.value.duration_sec == 60.0
        y.assert_not_awaited()

    async def test_both_paths_failing_names_both(self) -> None:
        with patch(
            "ytt.browser_fetch.browser_fetch_transcript",
            AsyncMock(side_effect=_infra("connect")),
        ), patch.object(
            fetch,
            "fetch_transcript_ytdlp",
            AsyncMock(side_effect=YttError(errors.RATE_LIMITED, "HTTP Error 429")),
        ):
            with pytest.raises(YttError) as ei:
                await fetch.fetch_transcript("VID", None, make_settings())
        assert ei.value.error_code == errors.RATE_LIMITED
        assert "HTTP Error 429" in ei.value.message
        assert "connect" in ei.value.message

    async def test_browser_off_is_exactly_the_ytdlp_path(self) -> None:
        with patch(
            "ytt.browser_fetch.browser_fetch_transcript", AsyncMock()
        ) as b, patch.object(
            fetch,
            "fetch_transcript_ytdlp",
            AsyncMock(return_value=_ok_result()),
        ) as y:
            await fetch.fetch_transcript(
                "VID", None, make_settings(fetch_mode="ytdlp")
            )
        b.assert_not_awaited()
        y.assert_awaited_once()
