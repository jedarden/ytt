"""Browser-primary caption fetch (plan: Fetch core — browser path).

YouTube's ``timedtext`` caption endpoint answers ``200`` with an EMPTY body
(or ``429``) unless the request carries a PO token minted by the real player
for that exact video + track.  yt-dlp cannot mint one, which is why its
caption-track download fails on many videos.  A real browser can: YouTube's
own player attaches the token to the request it issues itself.

This module drives a **remote** Chromium (a ``playwright run-server``
endpoint, ``YTT_BROWSER_WS_URL``): one fresh browser per fetch (the server
launches one per connection), one page, and reads the response of the
player's *own* ``/api/timedtext`` request.  No token is ever extracted,
stored or replayed — the token is minted by YouTube's player in our browser
and used by that browser.  See ``docs/notes/browser-fetch.md``.

Failure contract (what the router in :mod:`ytt.fetch` relies on):

- **Video-level errors** (private, unavailable, age/region/members gated,
  live, no captions) raise :class:`~ytt.errors.YttError` /
  :class:`~ytt.errors.NoCaptionsError` — authoritative; the router returns
  them as-is and does NOT fall back to yt-dlp.
- **Browser/infrastructure failures** (server unreachable, timeout, the
  player never issued the wanted request, a 0-byte body i.e. the PO token was
  rejected, a bot wall on this egress) raise :class:`BrowserInfraError` — the
  router then falls back to the yt-dlp path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from ytt import errors
from ytt.errors import NoCaptionsError, YttError
from ytt.fetch import (
    FetchResult,
    _in_progress_stream_error,
    _no_captions,
    _select_track,
    classify_ydl_error,
    get_available_langs,
)
from ytt.parse_json3 import parse_json3

if TYPE_CHECKING:  # pragma: no cover
    from ytt.config import Settings

log = logging.getLogger(__name__)

#: Outcome labels for the browser metrics (``ytt_browser_fetch_total``).
OUTCOME_OK = "ok"
OUTCOME_VIDEO_ERROR = "video_error"
OUTCOME_INFRA_ERROR = "infra_error"
OUTCOME_TIMEOUT = "timeout"


class BrowserInfraError(Exception):
    """The browser path itself failed — the router should try yt-dlp.

    Deliberately NOT a :class:`~ytt.errors.YttError`: it never reaches a
    tool caller.  ``reason`` is a short stable label for logs/metrics.
    """

    def __init__(self, message: str, reason: str = "infra") -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Endpoint + stealth configuration
# ---------------------------------------------------------------------------

#: Launch options sent to the ``run-server`` endpoint (it applies them to the
#: browser it starts for this connection).  Full Chromium in new-headless mode
#: with the automation switches removed: Playwright's default headless shell
#: is detected as automation and YouTube then rejects the player's PO token
#: (empty 200 bodies — ytt-daefe30b v2).
BROWSER_LAUNCH_OPTIONS: dict[str, Any] = {
    "channel": "chromium",
    "headless": True,
    "ignoreDefaultArgs": ["--enable-automation"],
    "args": [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ],
}

#: Injected into every page of the context before any site script runs.
STEALTH_INIT_JS = """
Object.defineProperty(navigator, "webdriver", {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
"""

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def browser_endpoint(ws_url: str) -> str:
    """Return *ws_url* with the stealth launch options attached.

    ``playwright run-server`` reads ``browser`` and ``launch-options`` from
    the connection URL's query.  An operator-supplied ``launch-options`` is
    left alone (it is their endpoint); otherwise ours is added.
    """
    parts = urlparse(ws_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("browser", "chromium")
    if "launch-options" not in query:
        query["launch-options"] = json.dumps(
            BROWSER_LAUNCH_OPTIONS, separators=(",", ":")
        )
    return urlunparse(parts._replace(query=urlencode(query, quote_via=quote)))


def browser_fetch_enabled(settings: "Settings") -> bool:
    """Whether the browser path is the primary fetcher for these settings."""
    mode = settings.fetch_mode
    if mode == "ytdlp":
        return False
    return bool(settings.browser_ws_url)


# ---------------------------------------------------------------------------
# In-page scripts
# ---------------------------------------------------------------------------

PLAYER_INFO_JS = """() => {
    const r = window.ytInitialPlayerResponse || null;
    if (!r) return null;
    const ps = r.playabilityStatus || {};
    const vd = r.videoDetails || {};
    const mf = (r.microformat || {}).playerMicroformatRenderer || {};
    const tl = (r.captions || {}).playerCaptionsTracklistRenderer || {};
    const audio = tl.audioTracks || [];
    const dai = tl.defaultAudioTrackIndex;
    const defCap = (dai !== undefined && audio[dai]) ? audio[dai].defaultCaptionTrackIndex : null;
    const sub = (((ps.errorScreen || {}).playerErrorMessageRenderer || {}).subreason || {}).simpleText || "";
    return {
        playability: ps.status || null,
        reason: ps.reason || "",
        subreason: sub,
        title: vd.title || null,
        author: vd.author || null,
        length_sec: vd.lengthSeconds || null,
        is_live: !!vd.isLive,
        is_upcoming: !!vd.isUpcoming,
        publish_date: mf.publishDate || mf.uploadDate || null,
        default_caption_index: (defCap === undefined) ? null : defCap,
        tracks: (tl.captionTracks || []).map(
            t => ({lang: t.languageCode, kind: t.kind || ""})),
    };
}"""

#: Ask the player for one caption track.  The captions module publishes its
#: track list a moment after ``loadModule``; passing one of ITS OWN entries to
#: ``setOption`` is the reliable form.  The player then issues the
#: ``/api/timedtext`` request itself (with the PO token attached).
SELECT_TRACK_JS = """async (want) => {
    const p = document.getElementById("movie_player");
    if (!p) return {ok: false, via: "no_player"};
    try { p.loadModule("captions"); } catch (e) {}
    try { p.mute(); p.playVideo(); } catch (e) {}
    let tl = [];
    for (let i = 0; i < 20; i++) {
        try { tl = p.getOption("captions", "tracklist") || []; } catch (e) { tl = []; }
        if (tl.length) break;
        await new Promise(r => setTimeout(r, 200));
    }
    const hit = tl.find(t => t.languageCode === want.lang && (t.kind || "") === want.kind);
    try {
        if (hit) { p.setOption("captions", "track", hit); return {ok: true, via: "tracklist"}; }
        const o = {languageCode: want.lang};
        if (want.kind) o.kind = want.kind;
        p.setOption("captions", "track", o);
        return {ok: true, via: "fallback", n: tl.length};
    } catch (e) { return {ok: false, via: "setoption_error"}; }
}"""


# ---------------------------------------------------------------------------
# Playability → error mapping
# ---------------------------------------------------------------------------

#: YouTube's own ``playabilityStatus.reason`` wording differs from the strings
#: yt-dlp raises, so the browser path carries its own seeds, checked before
#: :data:`ytt.fetch.SEED_MAP`.  Order matters: specific first.
_PLAYABILITY_SEEDS: list[tuple[str, str]] = [
    ("This video is private", errors.PRIVATE),
    ("Private video", errors.PRIVATE),
    ("members-only", errors.MEMBERS_ONLY),
    ("Join this channel", errors.MEMBERS_ONLY),
    ("Sign in to confirm your age", errors.AGE_RESTRICTED),
    ("may be inappropriate for some users", errors.AGE_RESTRICTED),
    ("available in your country", errors.REGION_BLOCKED),
    ("This video is unavailable", errors.UNAVAILABLE),
    ("has been removed", errors.UNAVAILABLE),
    ("account associated with this video has been terminated", errors.UNAVAILABLE),
]

#: Classified codes that mean "this egress/browser is blocked", not "this
#: video can't be had" — the router should try yt-dlp (and its proxy retry).
_INFRA_CODES = frozenset({errors.IP_BLOCKED, errors.RATE_LIMITED, errors.EMPTY_BODY})


def _normalize_text(text: str) -> str:
    # YouTube writes "you’re" with U+2019; the seed map uses a straight quote.
    return text.replace("’", "'").replace("‘", "'")


def classify_playability(reason: str) -> str:
    """Map a ``playabilityStatus`` reason string to a stable error code."""
    text = _normalize_text(reason or "")
    for seed, code in _PLAYABILITY_SEEDS:
        if seed in text:
            return code
    return classify_ydl_error(text)


def _check_playability(info: dict) -> None:
    """Raise for a video that cannot be fetched; return if it can.

    Video-level problems raise :class:`YttError`; an egress/bot wall raises
    :class:`BrowserInfraError` so the router can fall back.
    """
    status = info.get("playability")
    if info.get("is_live") or info.get("is_upcoming"):
        raise _in_progress_stream_error(
            {"live_status": "is_upcoming" if info.get("is_upcoming") else "is_live"}
        )
    if status == "OK":
        return
    reason = " ".join(
        p for p in (info.get("reason") or "", info.get("subreason") or "") if p
    ).strip()
    code = classify_playability(reason)
    if code in _INFRA_CODES:
        raise BrowserInfraError(
            f"player refused playback ({status}): {reason or 'no reason'}",
            reason="playability_blocked",
        )
    raise YttError(code, _normalize_text(reason) or f"Video not playable ({status}).")


# ---------------------------------------------------------------------------
# Track selection (reuses ytt.fetch._select_track)
# ---------------------------------------------------------------------------

def _synthesize_info(player: dict) -> dict:
    """Shape the player's caption tracks like a yt-dlp info dict.

    Lets the browser path share ``_select_track`` (the plan's language
    selection rules) and ``get_available_langs`` with the yt-dlp path.  Each
    fake format's ``url`` is ``track:<index>`` so the chosen track maps back.
    """
    subtitles: dict[str, list[dict]] = {}
    auto: dict[str, list[dict]] = {}
    tracks = player.get("tracks") or []
    for i, t in enumerate(tracks):
        fmt = {"ext": "json3", "url": f"track:{i}"}
        bucket = auto if t.get("kind") == "asr" else subtitles
        bucket.setdefault(t["lang"], []).append(fmt)
    language = None
    idx = player.get("default_caption_index")
    if isinstance(idx, int) and 0 <= idx < len(tracks):
        language = tracks[idx]["lang"]
    return {
        "subtitles": subtitles,
        "automatic_captions": auto,
        "language": language,
        "duration": float(player["length_sec"]) if player.get("length_sec") else None,
    }


def _published(player: dict) -> str | None:
    raw = player.get("publish_date")
    if not raw:
        return None
    digits = str(raw)[:10].replace("-", "")
    return digits if len(digits) == 8 and digits.isdigit() else None


# ---------------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------------

#: How long to wait for the player's own caption request once the track was
#: requested (module constant so tests can shrink it).
TRACK_WAIT_SEC = 20.0

_semaphore: asyncio.Semaphore | None = None
_semaphore_key: tuple[int, int] = (0, 0)


def _get_semaphore(size: int) -> asyncio.Semaphore:
    """One semaphore per (event loop, size) — never shared across loops."""
    global _semaphore, _semaphore_key
    key = (id(asyncio.get_running_loop()), size)
    if _semaphore is None or _semaphore_key != key:
        _semaphore = asyncio.Semaphore(size)
        _semaphore_key = key
    return _semaphore


def _playwright_factory():
    """Return ``async_playwright`` (indirection so tests can fake it)."""
    from playwright.async_api import async_playwright

    return async_playwright


def _is_timedtext(url: str) -> bool:
    return "/api/timedtext" in url


def _request_shape(url: str) -> dict:
    q = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    return {"lang": q.get("lang"), "kind": q.get("kind", "")}


async def _fetch_in_browser(
    video_id: str, lang: str | None, settings: "Settings"
) -> FetchResult:
    pw_factory = _playwright_factory()
    endpoint = browser_endpoint(settings.browser_ws_url)
    seen: list[dict] = []
    tasks: list[asyncio.Future] = []

    async def on_response(resp: Any) -> None:
        if not _is_timedtext(resp.url):
            return
        try:
            body = await resp.body()
        except Exception:  # response torn down (page closed / navigation)
            return
        seen.append({"status": resp.status, "body": body, **_request_shape(resp.url)})

    async with pw_factory() as pw:
        browser = None
        try:
            try:
                browser = await pw.chromium.connect(endpoint, timeout=15000)
            except Exception as exc:
                raise BrowserInfraError(
                    f"cannot reach browser server: {type(exc).__name__}",
                    reason="connect",
                ) from exc
            context = await browser.new_context(
                locale="en-US", user_agent=BROWSER_USER_AGENT
            )
            await context.add_init_script(STEALTH_INIT_JS)
            page = await context.new_page()
            page.on(
                "response", lambda r: tasks.append(asyncio.ensure_future(on_response(r)))
            )
            try:
                await page.goto(
                    f"https://www.youtube.com/watch?v={video_id}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                player = await page.evaluate(PLAYER_INFO_JS)
            except Exception as exc:
                raise BrowserInfraError(
                    f"page load failed: {type(exc).__name__}", reason="navigate"
                ) from exc
            if not player:
                raise BrowserInfraError(
                    "no player response on the watch page", reason="no_player_response"
                )

            _check_playability(player)
            info = _synthesize_info(player)
            if not info["subtitles"] and not info["automatic_captions"]:
                raise _no_captions("No captions available.", info)
            url, kind, served_lang, fallback_msg = _select_track(info, lang)
            wanted = player["tracks"][int(url.split(":", 1)[1])]

            try:
                await page.evaluate(
                    SELECT_TRACK_JS, {"lang": wanted["lang"], "kind": wanted["kind"]}
                )
            except Exception as exc:
                raise BrowserInfraError(
                    f"could not drive the player: {type(exc).__name__}",
                    reason="player_script",
                ) from exc

            hit = await _await_track_response(page, seen, wanted)
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:  # the server may already have dropped us
                    pass
            for t in tasks:
                if not t.done():
                    t.cancel()

    events = _parse_body(hit["body"])
    actual_kind = "asr" if hit["kind"] == "asr" else ""
    segments = parse_json3(events, kind=actual_kind)
    if not segments:
        raise BrowserInfraError("caption body parsed to no segments", reason="empty_parse")

    served = hit["lang"] or served_lang
    return FetchResult(
        segments=segments,
        source="caption_auto" if actual_kind == "asr" else "caption_manual",
        served_lang=served,
        requested_lang=lang if (lang is not None and lang != served) else None,
        available_langs=get_available_langs(info),
        title=player.get("title"),
        channel=player.get("author"),
        duration_sec=info["duration"],
        published=_published(player),
        message=fallback_msg,
    )


async def _await_track_response(page: Any, seen: list[dict], wanted: dict) -> dict:
    """Wait for the player's own timedtext response for the wanted track.

    The player may first fetch its default track; only a response for the
    wanted language counts.  An empty body means the PO token was rejected
    (automation detected / bad egress) — an infrastructure failure, not an
    answer about the video.
    """
    deadline = time.monotonic() + TRACK_WAIT_SEC
    nudged = False
    while time.monotonic() < deadline:
        for s in seen:
            if s["lang"] == wanted["lang"] and s["status"] == 200:
                if not s["body"]:
                    raise BrowserInfraError(
                        "player's caption request returned an empty body "
                        "(PO token rejected)",
                        reason="empty_body",
                    )
                return s
            if s["status"] == 429:
                raise BrowserInfraError(
                    "player's caption request was rate limited", reason="rate_limited"
                )
        if not nudged and time.monotonic() > deadline - TRACK_WAIT_SEC * 0.6:
            nudged = True
            try:  # toggle the CC button as a second route to a request
                await page.click(".ytp-subtitles-button", timeout=2000)
            except Exception:
                pass
        await page.wait_for_timeout(250)
    raise BrowserInfraError(
        "the player never requested the wanted caption track", reason="no_request"
    )


def _parse_body(body: bytes) -> list[dict]:
    try:
        return json.loads(body.decode("utf-8")).get("events", [])
    except (ValueError, UnicodeDecodeError) as exc:
        raise BrowserInfraError(
            "caption body was not valid json3", reason="bad_body"
        ) from exc


async def browser_fetch_transcript(
    video_id: str, lang: str | None, settings: "Settings"
) -> FetchResult:
    """Fetch captions through the remote browser (see module docstring)."""
    from ytt.observability import ytt_browser_fetch_seconds, ytt_browser_fetch_total

    started = time.monotonic()
    outcome = OUTCOME_INFRA_ERROR
    try:
        async with _get_semaphore(int(settings.browser_max_concurrency)):
            try:
                result = await asyncio.wait_for(
                    _fetch_in_browser(video_id, lang, settings),
                    timeout=float(settings.browser_timeout_sec),
                )
            except asyncio.TimeoutError:
                outcome = OUTCOME_TIMEOUT
                raise BrowserInfraError(
                    f"browser fetch timed out after {settings.browser_timeout_sec}s",
                    reason="timeout",
                ) from None
        outcome = OUTCOME_OK
        return result
    except NoCaptionsError:
        outcome = OUTCOME_VIDEO_ERROR
        raise
    except YttError:
        outcome = OUTCOME_VIDEO_ERROR
        raise
    finally:
        ytt_browser_fetch_total.labels(outcome=outcome).inc()
        ytt_browser_fetch_seconds.observe(time.monotonic() - started)
