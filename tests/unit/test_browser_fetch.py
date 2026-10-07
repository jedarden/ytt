"""Browser-primary caption fetch (ytt.browser_fetch + the router in ytt.fetch).

All offline: Playwright is replaced by a fake object graph whose "player"
answers like YouTube's — it issues its own ``/api/timedtext`` request when the
page script selects a track.  What is pinned here, and why:

- the browser is only ever pointed at youtube.com (egress boundary,
  docs/notes/browser-fetch.md);
- language selection reuses the yt-dlp path's rules and forces the *chosen*
  track through the player;
- each failure class lands on the right side of the router's line: video-level
  errors are authoritative, browser/infrastructure failures fall back to yt-dlp;
- a 0-byte body (PO token rejected) is an infrastructure failure, never an
  empty transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from ytt import browser_fetch, errors, fetch
from ytt.browser_fetch import (
    BROWSER_LAUNCH_OPTIONS,
    BrowserInfraError,
    NATURAL_JS,
    PLAYER_INFO_JS,
    SELECT_TRACK_JS,
    browser_endpoint,
    browser_fetch_enabled,
    browser_fetch_transcript,
    classify_playability,
)
from ytt.config import Settings
from ytt.errors import NoCaptionsError, YttError
from ytt.fetch import FetchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WS = "ws://ytt-browser.test.local:3001/"


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
    """A minimal manual-style json3 body: one event per word."""
    events = [
        {"tStartMs": i * 1000, "dDurationMs": 900, "segs": [{"utf8": w}]}
        for i, w in enumerate(words)
    ]
    return json.dumps({"events": events}).encode()


def player(
    *,
    tracks=(("en", ""),),
    playability="OK",
    reason="",
    default_caption_index=None,
    is_live=False,
    is_upcoming=False,
    title="A Title",
    author="A Channel",
    length_sec="125",
    publish_date="2025-03-04T10:00:00-07:00",
) -> dict:
    return {
        "playability": playability,
        "reason": reason,
        "subreason": "",
        "title": title,
        "author": author,
        "length_sec": length_sec,
        "is_live": is_live,
        "is_upcoming": is_upcoming,
        "publish_date": publish_date,
        "default_caption_index": default_caption_index,
        "tracks": [{"lang": lang, "kind": kind} for lang, kind in tracks],
    }


class FakeResponse:
    def __init__(self, url: str, status: int, body: bytes) -> None:
        self.url = url
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body


class Scenario:
    """What the fake YouTube does for one fetch."""

    def __init__(
        self,
        player_response,
        *,
        bodies: dict[tuple[str, str], tuple[int, bytes]] | None = None,
        default_request: tuple[str, str] | None = None,
        connect_error: Exception | None = None,
        goto_error: Exception | None = None,
        hang_goto: bool = False,
        issue_on_select: bool = True,
        strict_order: bool = False,
        late_good: bool = False,
    ) -> None:
        self.player_response = player_response
        self.bodies = bodies or {}
        self.default_request = default_request
        self.connect_error = connect_error
        self.goto_error = goto_error
        self.hang_goto = hang_goto
        self.issue_on_select = issue_on_select
        # Emulates the real race (measured on live YouTube): a track forced
        # BEFORE the player's own first request goes out with no PO token
        # (200 + 0 bytes, no `pot` param).
        self.strict_order = strict_order
        # First response for the wanted track is empty, a good one follows.
        self.late_good = late_good
        # observations
        self.endpoint: str | None = None
        self.goto_urls: list[str] = []
        self.selected: list[dict] = []
        self.init_scripts: list[str] = []
        self.browser_closed = False


class FakePage:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc
        self._cb = None
        self._natural_done = False

    def on(self, event: str, cb) -> None:
        assert event == "response"
        self._cb = cb

    def _emit(self, lang: str, kind: str, *, pot: bool = True, body=None) -> None:
        status, default_body = self.sc.bodies.get((lang, kind), (200, json3("x")))
        q = f"v=VID&lang={lang}&fmt=json3"
        if pot:
            q += "&pot=TOKEN"
        if kind:
            q += f"&kind={kind}"
        self._cb(
            FakeResponse(
                f"https://www.youtube.com/api/timedtext?{q}",
                status,
                default_body if body is None else body,
            )
        )

    async def goto(self, url: str, **kwargs) -> None:
        self.sc.goto_urls.append(url)
        if self.sc.hang_goto:
            await asyncio.sleep(3600)
        if self.sc.goto_error:
            raise self.sc.goto_error

    async def evaluate(self, script: str, arg=None):
        if script == PLAYER_INFO_JS:
            return self.sc.player_response
        if script == NATURAL_JS:
            # phase 1: the player makes its own first request, token attached
            self._natural_done = True
            if self.sc.default_request:
                self._emit(*self.sc.default_request)
            return True
        if script == SELECT_TRACK_JS:
            self.sc.selected.append(arg)
            if self.sc.strict_order and not self._natural_done:
                self._emit(arg["lang"], arg["kind"], pot=False, body=b"")
            elif self.sc.late_good:
                self._emit(arg["lang"], arg["kind"], pot=False, body=b"")
                asyncio.get_running_loop().call_later(
                    0.02, self._emit, arg["lang"], arg["kind"]
                )
            elif self.sc.issue_on_select:
                self._emit(arg["lang"], arg["kind"])
            return {"ok": True, "via": "tracklist"}
        raise AssertionError(f"unexpected page script: {script[:40]!r}")

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(0)

    async def click(self, selector: str, **kwargs) -> None:
        return None


class FakeContext:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc

    async def add_init_script(self, script: str) -> None:
        self.sc.init_scripts.append(script)

    async def new_page(self) -> FakePage:
        return FakePage(self.sc)


class FakeBrowser:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc

    async def new_context(self, **kwargs) -> FakeContext:
        return FakeContext(self.sc)

    async def close(self) -> None:
        self.sc.browser_closed = True


class FakeChromium:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc

    async def connect(self, endpoint: str, **kwargs) -> FakeBrowser:
        self.sc.endpoint = endpoint
        if self.sc.connect_error:
            raise self.sc.connect_error
        return FakeBrowser(self.sc)


class FakePW:
    def __init__(self, sc: Scenario) -> None:
        self.chromium = FakeChromium(sc)


def install(monkeypatch: pytest.MonkeyPatch, sc: Scenario) -> Scenario:
    @asynccontextmanager
    async def factory():
        yield FakePW(sc)

    monkeypatch.setattr(browser_fetch, "_playwright_factory", lambda: factory)
    monkeypatch.setattr(browser_fetch, "TRACK_WAIT_SEC", 0.2)
    monkeypatch.setattr(browser_fetch, "NATURAL_WAIT_SEC", 0.05)
    monkeypatch.setattr(browser_fetch, "EMPTY_GRACE_SEC", 0.05)
    return sc


# ---------------------------------------------------------------------------
# Endpoint / settings
# ---------------------------------------------------------------------------


class TestEndpointAndSettings:
    def test_endpoint_carries_stealth_launch_options(self) -> None:
        url = browser_endpoint(WS)
        q = parse_qs(urlparse(url).query)
        assert q["browser"] == ["chromium"]
        assert json.loads(q["launch-options"][0]) == BROWSER_LAUNCH_OPTIONS
        opts = BROWSER_LAUNCH_OPTIONS
        # the three things that make the player's PO token accepted
        assert opts["channel"] == "chromium"  # full Chromium, not the headless shell
        assert "--enable-automation" in opts["ignoreDefaultArgs"]
        assert "--disable-blink-features=AutomationControlled" in opts["args"]

    def test_operator_supplied_launch_options_are_left_alone(self) -> None:
        url = browser_endpoint(WS + "?launch-options=%7B%7D&browser=chromium")
        q = parse_qs(urlparse(url).query)
        assert q["launch-options"] == ["{}"]

    def test_enabled_matrix(self) -> None:
        assert browser_fetch_enabled(make_settings())
        assert not browser_fetch_enabled(make_settings(browser_ws_url=""))
        assert not browser_fetch_enabled(make_settings(fetch_mode="ytdlp"))
        assert browser_fetch_enabled(make_settings(fetch_mode="browser"))

    def test_browser_mode_requires_a_url(self) -> None:
        with pytest.raises(ValueError, match="YTT_BROWSER_WS_URL"):
            make_settings(fetch_mode="browser", browser_ws_url="")

    @pytest.mark.parametrize("bad", ["http://x:3001/", "ytt-browser:3001", "ws://"])
    def test_bad_ws_url_fails_at_startup(self, bad: str) -> None:
        with pytest.raises(ValueError, match="YTT_BROWSER_WS_URL"):
            make_settings(browser_ws_url=bad)

    def test_unset_means_off_by_default(self) -> None:
        with patch.dict(os.environ, {"YTT_ALLOWED_SUBJECTS": "t"}, clear=False):
            os.environ.pop("YTT_BROWSER_WS_URL", None)
            s = Settings()
        assert s.browser_ws_url == "" and s.fetch_mode == "auto"
        assert not browser_fetch_enabled(s)


# ---------------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------------


class TestFetch:
    async def test_manual_english_track_is_chosen_and_forced_through_the_player(
        self, monkeypatch
    ) -> None:
        sc = install(
            monkeypatch,
            Scenario(
                player(tracks=(("ar", "asr"), ("en", "")), default_caption_index=0),
                # the player first fetches its DEFAULT track (ar asr)…
                default_request=("ar", "asr"),
                # …then the one we ask for.
                bodies={("en", ""): (200, json3("hello", "world"))},
            ),
        )
        res = await browser_fetch_transcript("dQw4w9WgXcQ", None, make_settings())

        assert isinstance(res, FetchResult)
        assert [s.text for s in res.segments] == ["hello", "world"]
        assert res.source == "caption_manual" and res.served_lang == "en"
        assert res.requested_lang is None
        assert res.available_langs == ["ar", "en"]
        assert (res.title, res.channel, res.duration_sec) == ("A Title", "A Channel", 125.0)
        assert res.published == "20250304"
        # the page script was told to switch to the chosen track
        assert sc.selected == [{"lang": "en", "kind": ""}]
        assert sc.browser_closed

    async def test_natural_request_for_the_wanted_track_is_used_without_forcing(
        self, monkeypatch
    ) -> None:
        """A video whose default track IS the wanted one (live: Rick Astley's
        manual English): the player's own first request already has the token,
        so nothing is forced."""
        sc = install(
            monkeypatch,
            Scenario(
                player(tracks=(("en", ""), ("de-DE", ""))),
                default_request=("en", ""),
                bodies={("en", ""): (200, json3("never", "gonna"))},
            ),
        )
        res = await browser_fetch_transcript("VID", None, make_settings())
        assert [s.text for s in res.segments] == ["never", "gonna"]
        assert res.source == "caption_manual" and res.served_lang == "en"
        assert sc.selected == []

    async def test_track_is_never_forced_before_the_players_own_first_request(
        self, monkeypatch
    ) -> None:
        """Live finding 2026-10-07: forcing a track straight after navigation
        sends it without a PO token (200 + 0 bytes), so every manual-caption
        video failed.  The player's natural request must come first."""
        sc = install(
            monkeypatch,
            Scenario(
                player(tracks=(("ar", "asr"), ("en", ""))),
                default_request=("ar", "asr"),
                strict_order=True,
                bodies={("en", ""): (200, json3("ok"))},
            ),
        )
        res = await browser_fetch_transcript("VID", None, make_settings())
        assert res.served_lang == "en" and [s.text for s in res.segments] == ["ok"]
        assert sc.selected == [{"lang": "en", "kind": ""}]

    async def test_an_empty_first_response_followed_by_a_good_one_succeeds(
        self, monkeypatch
    ) -> None:
        install(
            monkeypatch,
            Scenario(
                player(tracks=(("ar", "asr"), ("en", ""))),
                default_request=("ar", "asr"),
                late_good=True,
                bodies={("en", ""): (200, json3("late"))},
            ),
        )
        res = await browser_fetch_transcript("VID", None, make_settings())
        assert [s.text for s in res.segments] == ["late"]

    async def test_auto_only_video_serves_the_asr_track(self, monkeypatch) -> None:
        install(
            monkeypatch,
            Scenario(
                player(tracks=(("ar", "asr"),), default_caption_index=0),
                bodies={("ar", "asr"): (200, json3("a", "b"))},
            ),
        )
        res = await browser_fetch_transcript("VID", None, make_settings())
        assert res.source == "caption_auto" and res.served_lang == "ar"

    async def test_requested_language_unavailable_serves_fallback_with_message(
        self, monkeypatch
    ) -> None:
        install(
            monkeypatch,
            Scenario(player(tracks=(("en", ""),)), bodies={("en", ""): (200, json3("x"))}),
        )
        res = await browser_fetch_transcript("VID", "es", make_settings())
        assert res.served_lang == "en" and res.requested_lang == "es"
        assert "es" in (res.message or "") and "en" in (res.message or "")

    async def test_source_reflects_the_request_the_player_actually_made(
        self, monkeypatch
    ) -> None:
        """If the player answers with an ASR track when we wanted manual, the
        result must say so — ``source`` is derived from the captured request."""
        sc = Scenario(
            player(tracks=(("en", ""),)),
            issue_on_select=False,
            default_request=("en", "asr"),
            bodies={("en", "asr"): (200, json3("w"))},
        )
        install(monkeypatch, sc)
        res = await browser_fetch_transcript("VID", None, make_settings())
        assert res.source == "caption_auto"

    async def test_browser_only_ever_navigates_to_youtube(self, monkeypatch) -> None:
        sc = install(
            monkeypatch,
            Scenario(player(), bodies={("en", ""): (200, json3("x"))}),
        )
        await browser_fetch_transcript("dQw4w9WgXcQ", None, make_settings())
        assert sc.goto_urls == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
        assert sc.endpoint is not None and sc.endpoint.startswith(WS)
        assert any("webdriver" in s for s in sc.init_scripts)

    async def test_rolling_auto_captions_are_deduplicated(self, monkeypatch) -> None:
        rolling = json.dumps(
            {
                "events": [
                    {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "hello"}]},
                    {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello world"}]},
                ]
            }
        ).encode()
        install(
            monkeypatch,
            Scenario(
                player(tracks=(("en", "asr"),)), bodies={("en", "asr"): (200, rolling)}
            ),
        )
        res = await browser_fetch_transcript("VID", None, make_settings())
        assert "hello hello" not in " ".join(s.text for s in res.segments)


# ---------------------------------------------------------------------------
# Failure classes
# ---------------------------------------------------------------------------


class TestVideoLevelErrors:
    """Authoritative: raised as YttError, never a fallback trigger."""

    @pytest.mark.parametrize(
        "reason,code",
        [
            ("This video is unavailable", errors.UNAVAILABLE),
            ("Private video", errors.PRIVATE),
            ("This video is private", errors.PRIVATE),
            ("Sign in to confirm your age", errors.AGE_RESTRICTED),
            ("This video is not available in your country", errors.REGION_BLOCKED),
            ("Join this channel to get access to members-only content", errors.MEMBERS_ONLY),
        ],
    )
    async def test_playability_reasons_map_to_the_stable_taxonomy(
        self, monkeypatch, reason: str, code: str
    ) -> None:
        install(monkeypatch, Scenario(player(playability="ERROR", reason=reason)))
        with pytest.raises(YttError) as ei:
            await browser_fetch_transcript("VID", None, make_settings())
        assert ei.value.error_code == code
        assert not isinstance(ei.value, BrowserInfraError)

    @pytest.mark.parametrize("flag", ["is_live", "is_upcoming"])
    async def test_live_and_upcoming_are_is_livestream(self, monkeypatch, flag) -> None:
        install(monkeypatch, Scenario(player(**{flag: True})))
        with pytest.raises(YttError) as ei:
            await browser_fetch_transcript("VID", None, make_settings())
        assert ei.value.error_code == errors.IS_LIVESTREAM

    async def test_no_caption_tracks_raises_no_captions_with_duration(
        self, monkeypatch
    ) -> None:
        install(monkeypatch, Scenario(player(tracks=(), length_sec="321")))
        with pytest.raises(NoCaptionsError) as ei:
            await browser_fetch_transcript("VID", None, make_settings())
        # the Whisper fallback enforces MAX_ASR_DURATION_SEC from this
        assert ei.value.duration_sec == 321.0


class TestInfrastructureFailures:
    """Fallback triggers: BrowserInfraError, with a stable ``reason``."""

    async def _reason(self, monkeypatch, sc: Scenario, **settings) -> str:
        install(monkeypatch, sc)
        with pytest.raises(BrowserInfraError) as ei:
            await browser_fetch_transcript("VID", None, make_settings(**settings))
        return ei.value.reason

    async def test_server_unreachable(self, monkeypatch) -> None:
        sc = Scenario(player(), connect_error=ConnectionRefusedError("nope"))
        assert await self._reason(monkeypatch, sc) == "connect"

    async def test_zero_byte_body_means_the_po_token_was_rejected(self, monkeypatch) -> None:
        sc = Scenario(player(), bodies={("en", ""): (200, b"")})
        assert await self._reason(monkeypatch, sc) == "empty_body"

    async def test_rate_limited_caption_request(self, monkeypatch) -> None:
        sc = Scenario(player(), bodies={("en", ""): (429, b"")})
        assert await self._reason(monkeypatch, sc) == "rate_limited"

    async def test_player_never_requests_the_track(self, monkeypatch) -> None:
        sc = Scenario(player(), issue_on_select=False)
        assert await self._reason(monkeypatch, sc) == "no_request"

    async def test_default_track_response_is_not_mistaken_for_the_wanted_one(
        self, monkeypatch
    ) -> None:
        sc = Scenario(
            player(tracks=(("ar", "asr"), ("en", ""))),
            issue_on_select=False,
            default_request=("ar", "asr"),
        )
        assert await self._reason(monkeypatch, sc) == "no_request"

    async def test_bot_wall_on_this_egress(self, monkeypatch) -> None:
        sc = Scenario(
            player(playability="LOGIN_REQUIRED", reason="Sign in to confirm you’re not a bot")
        )
        assert await self._reason(monkeypatch, sc) == "playability_blocked"

    async def test_navigation_failure(self, monkeypatch) -> None:
        sc = Scenario(player(), goto_error=TimeoutError("slow"))
        assert await self._reason(monkeypatch, sc) == "navigate"

    async def test_whole_fetch_timeout(self, monkeypatch) -> None:
        sc = Scenario(player(), hang_goto=True)
        assert await self._reason(monkeypatch, sc, browser_timeout_sec=1) == "timeout"
        assert sc.browser_closed  # no leaked browser on the server

    async def test_missing_player_response(self, monkeypatch) -> None:
        assert await self._reason(monkeypatch, Scenario(None)) == "no_player_response"

    async def test_garbage_body(self, monkeypatch) -> None:
        sc = Scenario(player(), bodies={("en", ""): (200, b"<html>not json</html>")})
        assert await self._reason(monkeypatch, sc) == "bad_body"

    async def test_browser_is_closed_on_every_failure_path(self, monkeypatch) -> None:
        sc = Scenario(player(playability="ERROR", reason="This video is unavailable"))
        install(monkeypatch, sc)
        with pytest.raises(YttError):
            await browser_fetch_transcript("VID", None, make_settings())
        assert sc.browser_closed


def test_playability_classifier_handles_curly_apostrophes() -> None:
    # YouTube writes "you’re" (U+2019); the shared seed map uses a straight quote.
    assert (
        classify_playability("Sign in to confirm you’re not a bot") == errors.IP_BLOCKED
    )


# ---------------------------------------------------------------------------
# Router (ytt.fetch.fetch_transcript)
# ---------------------------------------------------------------------------


def _ok_result() -> FetchResult:
    return FetchResult(
        segments=[], source="caption_auto", served_lang="en", requested_lang=None,
        available_langs=["en"],
    )


class TestRouter:
    async def test_browser_success_never_touches_ytdlp(self) -> None:
        with patch("ytt.browser_fetch.browser_fetch_transcript", AsyncMock(return_value=_ok_result())) as b, \
             patch.object(fetch, "fetch_transcript_ytdlp", AsyncMock()) as y:
            res = await fetch.fetch_transcript("VID", None, make_settings())
        assert res.source == "caption_auto"
        b.assert_awaited_once()
        y.assert_not_awaited()

    async def test_infra_failure_falls_back_to_ytdlp(self) -> None:
        with patch("ytt.browser_fetch.browser_fetch_transcript",
                   AsyncMock(side_effect=BrowserInfraError("down", "connect"))), \
             patch.object(fetch, "fetch_transcript_ytdlp", AsyncMock(return_value=_ok_result())) as y:
            res = await fetch.fetch_transcript("VID", "en", make_settings())
        assert res.served_lang == "en"
        y.assert_awaited_once_with("VID", "en", y.await_args.args[2])

    @pytest.mark.parametrize(
        "exc",
        [YttError(errors.PRIVATE, "Private video"), NoCaptionsError("none", duration_sec=9.0)],
    )
    async def test_video_level_errors_do_not_fall_back(self, exc) -> None:
        with patch("ytt.browser_fetch.browser_fetch_transcript", AsyncMock(side_effect=exc)), \
             patch.object(fetch, "fetch_transcript_ytdlp", AsyncMock()) as y:
            with pytest.raises(YttError) as ei:
                await fetch.fetch_transcript("VID", None, make_settings())
        assert ei.value is exc
        y.assert_not_awaited()

    async def test_both_paths_failing_names_both(self) -> None:
        with patch("ytt.browser_fetch.browser_fetch_transcript",
                   AsyncMock(side_effect=BrowserInfraError("token rejected", "empty_body"))), \
             patch.object(fetch, "fetch_transcript_ytdlp",
                          AsyncMock(side_effect=YttError(errors.RATE_LIMITED, "HTTP Error 429"))):
            with pytest.raises(YttError) as ei:
                await fetch.fetch_transcript("VID", None, make_settings())
        assert ei.value.error_code == errors.RATE_LIMITED
        assert "HTTP Error 429" in ei.value.message
        assert "token rejected" in ei.value.message

    async def test_no_captions_from_the_fallback_keeps_its_type(self) -> None:
        """The Whisper path keys off NoCaptionsError; the wrapper must not eat it."""
        nc = NoCaptionsError("none", duration_sec=60.0)
        with patch("ytt.browser_fetch.browser_fetch_transcript",
                   AsyncMock(side_effect=BrowserInfraError("down", "connect"))), \
             patch.object(fetch, "fetch_transcript_ytdlp", AsyncMock(side_effect=nc)):
            with pytest.raises(NoCaptionsError) as ei:
                await fetch.fetch_transcript("VID", None, make_settings())
        assert ei.value.duration_sec == 60.0

    @pytest.mark.parametrize("kw", [{"browser_ws_url": ""}, {"fetch_mode": "ytdlp"}])
    async def test_browser_off_is_exactly_the_ytdlp_path(self, kw) -> None:
        with patch("ytt.browser_fetch.browser_fetch_transcript", AsyncMock()) as b, \
             patch.object(fetch, "fetch_transcript_ytdlp", AsyncMock(return_value=_ok_result())) as y:
            await fetch.fetch_transcript("VID", None, make_settings(**kw))
        b.assert_not_awaited()
        y.assert_awaited_once()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _count(outcome: str) -> float:
    from ytt.observability import ytt_browser_fetch_total

    return ytt_browser_fetch_total.labels(outcome=outcome)._value.get()


async def test_outcome_metric_is_labelled_per_failure_class(monkeypatch) -> None:
    ok0, ve0, ie0 = _count("ok"), _count("video_error"), _count("infra_error")
    install(monkeypatch, Scenario(player(), bodies={("en", ""): (200, json3("x"))}))
    await browser_fetch_transcript("VID", None, make_settings())
    install(monkeypatch, Scenario(player(playability="ERROR", reason="This video is unavailable")))
    with pytest.raises(YttError):
        await browser_fetch_transcript("VID", None, make_settings())
    install(monkeypatch, Scenario(player(), connect_error=OSError("x")))
    with pytest.raises(BrowserInfraError):
        await browser_fetch_transcript("VID", None, make_settings())
    assert (_count("ok") - ok0, _count("video_error") - ve0, _count("infra_error") - ie0) == (1, 1, 1)
