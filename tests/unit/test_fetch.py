"""Unit tests for ytt.fetch — yt-dlp integration layer (plan: Phase 2).

All tests use stubs/mocks for yt-dlp; no real network calls are made.

Coverage:
- YDL_BASE_OPTS has ``cookiefile=None`` and ``cookiesfrombrowser=None`` (no-cookies).
- Error taxonomy: each seed string maps to the correct error_code.
- ``classify_ydl_error`` fallback to EMPTY_BODY for unknown strings.
- ``get_available_langs``: merges subtitles + auto_captions, strips .auto, deduplicates.
- ``_normalize_lang_key``: strips .auto suffix, lowercases.
- ``_find_json3_url``: picks json3 ext from format list.
- ``_select_track``: manual[lang] > auto[lang] > manual[default] > auto[default] > any.
- ``_select_track`` with lang=None: default/original > English > any.
- ``_select_track`` raises EMPTY_BODY when no tracks available.
- ``_do_fetch``: stub extract_info returns info dict; FetchResult fields populated.
- ``_do_fetch``: DownloadError mapped to correct error_code via taxonomy.
- ``_do_fetch``: ExtractorError mapped to correct error_code.
- ``_do_fetch``: proxy kwarg forwarded to YDL opts.
- ``_do_fetch``: empty info (None) raises EMPTY_BODY.
- ``fetch_transcript`` async: asyncio.TimeoutError → RATE_LIMITED YttError.
- ``fetch_transcript`` async: ip_blocked + proxy_url triggers proxy retry.
- ``fetch_transcript`` async: ip_blocked with no proxy_url re-raises immediately.
- ``fetch_transcript`` async: proxy retry failure preserves error_code.
- FetchResult ``requested_lang`` only set when lang != served_lang.
- Language fallback: message set when served != requested.
- ``source`` is 'caption_auto' for 'asr' kind, 'caption_manual' for manual.
"""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from typing import Any

import pytest
import yt_dlp
import yt_dlp.utils

from ytt import errors
from ytt.errors import YttError
from ytt.fetch import (
    SEED_MAP,
    YDL_BASE_OPTS,
    FetchResult,
    _do_fetch,
    _find_json3_url,
    _normalize_lang_key,
    _select_track,
    classify_ydl_error,
    fetch_transcript,
    get_available_langs,
)
from ytt.models import Segment


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_settings(**kwargs):
    """Create a Settings object with test-safe defaults (no filesystem checks)."""
    import os
    env_overrides = {
        "YTT_ALLOWED_SUBJECTS": "test-sub",
        "YTT_CACHE_BACKEND": "emptydir",  # avoid statvfs
        "YTT_CACHE_DIR": "/tmp",
        "YTT_SCRATCH_DIR": "/tmp",
        "YTT_EXTRACT_TIMEOUT_SEC": "60",
        **{f"YTT_{k.upper()}": str(v) for k, v in kwargs.items()},
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        from ytt.config import Settings
        return Settings()


def _make_format_list(ext: str = "json3", url: str = "http://example.com/caps.json3"):
    return [{"ext": ext, "url": url}]


def _make_info(
    subtitles: dict | None = None,
    automatic_captions: dict | None = None,
    language: str = "en",
    title: str = "Test Video",
    channel: str = "Test Channel",
    duration: float = 120.0,
    upload_date: str = "20240101",
) -> dict:
    """Build a minimal yt-dlp info dict."""
    return {
        "id": "dQw4w9WgXcQ",
        "title": title,
        "channel": channel,
        "duration": duration,
        "upload_date": upload_date,
        "language": language,
        "subtitles": subtitles or {},
        "automatic_captions": automatic_captions or {},
    }


def _make_json3_response(events: list[dict] | None = None) -> bytes:
    """Produce a minimal json3 bytes payload."""
    return json.dumps({"events": events or []}).encode()


def _stub_ydl(info: dict, json3_bytes: bytes):
    """Return a mock yt_dlp.YoutubeDL context manager that returns the given info."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json3_bytes

    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = info
    mock_ydl.urlopen.return_value = mock_resp

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    return mock_ctx, mock_ydl


# ---------------------------------------------------------------------------
# No-cookies enforcement (plan §Security)
# ---------------------------------------------------------------------------

class TestNoCookiesEnforcement:
    """YDL_BASE_OPTS must have cookiefile and cookiesfrombrowser present and falsy."""

    def test_cookiefile_present(self):
        assert "cookiefile" in YDL_BASE_OPTS

    def test_cookiefile_is_none(self):
        assert YDL_BASE_OPTS["cookiefile"] is None

    def test_cookiesfrombrowser_present(self):
        assert "cookiesfrombrowser" in YDL_BASE_OPTS

    def test_cookiesfrombrowser_is_none(self):
        assert YDL_BASE_OPTS["cookiesfrombrowser"] is None

    def test_both_keys_are_falsy(self):
        assert not YDL_BASE_OPTS["cookiefile"]
        assert not YDL_BASE_OPTS["cookiesfrombrowser"]

    def test_extractor_args_player_client_list(self):
        """Player-client override must be the tv/web_embedded/mweb list."""
        ea = YDL_BASE_OPTS["extractor_args"]
        assert ea["youtube"]["player_client"] == ["tv", "web_embedded", "mweb"]


# ---------------------------------------------------------------------------
# Error taxonomy — classify_ydl_error (plan §Error taxonomy)
# ---------------------------------------------------------------------------

class TestClassifyYdlError:
    """Each seed string must map to the documented error_code."""

    @pytest.mark.parametrize("seed,expected_code", SEED_MAP)
    def test_seed_string_to_code(self, seed: str, expected_code: str):
        result = classify_ydl_error(f"[youtube] dQw4w9WgXcQ: {seed}")
        assert result == expected_code

    def test_unrecognized_string_returns_empty_body(self):
        assert classify_ydl_error("some totally random yt-dlp error") == errors.EMPTY_BODY

    def test_empty_string_returns_empty_body(self):
        assert classify_ydl_error("") == errors.EMPTY_BODY

    def test_private_video_seed(self):
        assert classify_ydl_error("Private video") == errors.PRIVATE

    def test_members_only_seed(self):
        assert classify_ydl_error("members-only content") == errors.MEMBERS_ONLY

    def test_age_restricted_seed(self):
        assert classify_ydl_error("Sign in to confirm your age") == errors.AGE_RESTRICTED

    def test_region_blocked_seed(self):
        assert classify_ydl_error("not available in your country") == errors.REGION_BLOCKED

    def test_livestream_seed_begin(self):
        assert classify_ydl_error("This live event will begin") == errors.IS_LIVESTREAM

    def test_livestream_seed_is_live(self):
        assert classify_ydl_error("is live") == errors.IS_LIVESTREAM

    def test_unavailable_seed(self):
        assert classify_ydl_error("Video unavailable") == errors.UNAVAILABLE

    def test_removed_seed(self):
        assert classify_ydl_error("has been removed") == errors.UNAVAILABLE

    def test_rate_limited_seed(self):
        assert classify_ydl_error("HTTP Error 429") == errors.RATE_LIMITED

    def test_ip_blocked_bot_seed(self):
        assert classify_ydl_error("Sign in to confirm you're not a bot") == errors.IP_BLOCKED

    def test_ip_blocked_403_seed(self):
        assert classify_ydl_error("HTTP Error 403") == errors.IP_BLOCKED

    def test_ip_blocked_data_blocks_seed(self):
        assert classify_ydl_error("Did not get any data blocks") == errors.IP_BLOCKED

    def test_audio_too_large_seed(self):
        assert classify_ydl_error("audio_too_large") == errors.TOO_LONG_FOR_ASR


# ---------------------------------------------------------------------------
# _normalize_lang_key + _find_json3_url
# ---------------------------------------------------------------------------

class TestNormalizeLangKey:
    def test_strips_auto_suffix(self):
        from ytt.fetch import _normalize_lang_key
        assert _normalize_lang_key("en.auto") == "en"

    def test_lowercases(self):
        from ytt.fetch import _normalize_lang_key
        assert _normalize_lang_key("EN") == "en"

    def test_no_auto_suffix_unchanged(self):
        from ytt.fetch import _normalize_lang_key
        assert _normalize_lang_key("es-419") == "es-419"

    def test_already_lowercase(self):
        from ytt.fetch import _normalize_lang_key
        assert _normalize_lang_key("fr") == "fr"


class TestFindJson3Url:
    def test_finds_json3(self):
        fmts = [{"ext": "vtt", "url": "a"}, {"ext": "json3", "url": "b"}]
        assert _find_json3_url(fmts) == "b"

    def test_returns_none_when_no_json3(self):
        fmts = [{"ext": "vtt", "url": "a"}, {"ext": "srt", "url": "c"}]
        assert _find_json3_url(fmts) is None

    def test_returns_none_on_empty_list(self):
        assert _find_json3_url([]) is None

    def test_returns_none_on_none(self):
        assert _find_json3_url(None) is None

    def test_returns_first_json3(self):
        fmts = [
            {"ext": "json3", "url": "first"},
            {"ext": "json3", "url": "second"},
        ]
        assert _find_json3_url(fmts) == "first"


# ---------------------------------------------------------------------------
# get_available_langs
# ---------------------------------------------------------------------------

class TestGetAvailableLangs:
    def test_empty_info(self):
        assert get_available_langs({}) == []

    def test_subtitles_only(self):
        info = {"subtitles": {"en": [], "fr": []}}
        assert get_available_langs(info) == ["en", "fr"]

    def test_auto_captions_only(self):
        info = {"automatic_captions": {"es": [], "de": []}}
        assert get_available_langs(info) == ["de", "es"]

    def test_merges_both(self):
        info = {
            "subtitles": {"en": []},
            "automatic_captions": {"fr": [], "es": []},
        }
        assert get_available_langs(info) == ["en", "es", "fr"]

    def test_deduplicates(self):
        info = {
            "subtitles": {"en": []},
            "automatic_captions": {"en": [], "es": []},
        }
        assert get_available_langs(info) == ["en", "es"]

    def test_strips_auto_suffix(self):
        info = {"automatic_captions": {"en.auto": []}}
        assert get_available_langs(info) == ["en"]

    def test_lowercases(self):
        info = {"subtitles": {"EN": [], "Fr": []}}
        assert get_available_langs(info) == ["en", "fr"]

    def test_none_values_handled(self):
        info = {"subtitles": None, "automatic_captions": None}
        assert get_available_langs(info) == []

    def test_sorted_output(self):
        info = {"subtitles": {"zh": [], "ar": [], "en": []}}
        result = get_available_langs(info)
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# _select_track — language selection logic
# ---------------------------------------------------------------------------

MANUAL_EN_URL = "http://example.com/manual_en.json3"
AUTO_EN_URL = "http://example.com/auto_en.json3"
MANUAL_FR_URL = "http://example.com/manual_fr.json3"
AUTO_FR_URL = "http://example.com/auto_fr.json3"


def _manual(url: str) -> list[dict]:
    return [{"ext": "json3", "url": url}]


def _auto(url: str) -> list[dict]:
    return [{"ext": "json3", "url": url}]


class TestSelectTrackWithLang:
    """lang=X: manual[X] > auto[X] > manual[default] > auto[default] > any."""

    def _info(self, subtitles=None, auto=None, language="en"):
        return _make_info(subtitles=subtitles, automatic_captions=auto, language=language)

    def test_prefers_manual_over_auto(self):
        info = self._info(
            subtitles={"en": _manual(MANUAL_EN_URL)},
            auto={"en": _auto(AUTO_EN_URL)},
        )
        url, kind, served, msg = _select_track(info, "en")
        assert url == MANUAL_EN_URL
        assert kind == ""
        assert served == "en"
        assert msg is None

    def test_falls_back_to_auto_when_no_manual(self):
        info = self._info(auto={"en": _auto(AUTO_EN_URL)})
        url, kind, served, msg = _select_track(info, "en")
        assert url == AUTO_EN_URL
        assert kind == "asr"
        assert served == "en"
        assert msg is None

    def test_falls_back_to_default_lang_manual(self):
        # Request 'fr' but only 'en' (the default) is available manually
        info = self._info(
            subtitles={"en": _manual(MANUAL_EN_URL)},
            language="en",
        )
        url, kind, served, msg = _select_track(info, "fr")
        assert url == MANUAL_EN_URL
        assert served == "en"
        assert msg is not None
        assert "fr" in msg
        assert "en" in msg

    def test_falls_back_to_default_lang_auto(self):
        info = self._info(
            auto={"en": _auto(AUTO_EN_URL)},
            language="en",
        )
        url, kind, served, msg = _select_track(info, "fr")
        assert url == AUTO_EN_URL
        assert kind == "asr"
        assert served == "en"
        assert msg is not None

    def test_falls_back_to_any_available_manual(self):
        info = self._info(subtitles={"fr": _manual(MANUAL_FR_URL)}, language="de")
        url, kind, served, msg = _select_track(info, "en")
        assert url == MANUAL_FR_URL
        assert served == "fr"
        assert msg is not None

    def test_falls_back_to_any_available_auto(self):
        info = self._info(auto={"fr": _auto(AUTO_FR_URL)}, language="de")
        url, kind, served, msg = _select_track(info, "en")
        assert url == AUTO_FR_URL
        assert kind == "asr"
        assert served == "fr"
        assert msg is not None

    def test_raises_empty_body_when_no_tracks(self):
        info = self._info()
        with pytest.raises(YttError) as exc_info:
            _select_track(info, "en")
        assert exc_info.value.error_code == errors.EMPTY_BODY

    def test_no_fallback_message_on_exact_match(self):
        info = self._info(subtitles={"en": _manual(MANUAL_EN_URL)})
        _, _, _, msg = _select_track(info, "en")
        assert msg is None

    def test_lang_fr_preferred_over_en_default(self):
        """If fr is requested and available, it should be served."""
        info = self._info(
            subtitles={"en": _manual(MANUAL_EN_URL), "fr": _manual(MANUAL_FR_URL)},
            language="en",
        )
        url, kind, served, msg = _select_track(info, "fr")
        assert url == MANUAL_FR_URL
        assert served == "fr"
        assert msg is None


class TestSelectTrackWithoutLang:
    """lang=None: default/original > English > any."""

    def _info(self, subtitles=None, auto=None, language="en"):
        return _make_info(subtitles=subtitles, automatic_captions=auto, language=language)

    def test_prefers_default_language_manual(self):
        info = self._info(
            subtitles={"en": _manual(MANUAL_EN_URL), "fr": _manual(MANUAL_FR_URL)},
            language="en",
        )
        url, kind, served, msg = _select_track(info, None)
        assert url == MANUAL_EN_URL
        assert served == "en"
        assert msg is None

    def test_falls_to_english_if_default_unavailable(self):
        info = self._info(
            auto={"en": _auto(AUTO_EN_URL)},
            language="de",
        )
        url, kind, served, msg = _select_track(info, None)
        assert url == AUTO_EN_URL
        assert served == "en"

    def test_falls_to_any_when_no_english(self):
        info = self._info(auto={"fr": _auto(AUTO_FR_URL)}, language="")
        url, kind, served, msg = _select_track(info, None)
        assert url == AUTO_FR_URL
        assert served == "fr"

    def test_raises_empty_body_when_no_tracks_at_all(self):
        info = self._info()
        with pytest.raises(YttError) as exc_info:
            _select_track(info, None)
        assert exc_info.value.error_code == errors.EMPTY_BODY

    def test_manual_preferred_over_auto(self):
        info = self._info(
            subtitles={"en": _manual(MANUAL_EN_URL)},
            auto={"en": _auto(AUTO_EN_URL)},
            language="en",
        )
        url, kind, served, msg = _select_track(info, None)
        assert url == MANUAL_EN_URL
        assert kind == ""


# ---------------------------------------------------------------------------
# _do_fetch — synchronous core (stubbed yt-dlp)
# ---------------------------------------------------------------------------

class TestDoFetch:
    """Verify _do_fetch populates FetchResult correctly from mocked yt-dlp."""

    def _settings(self, **kwargs):
        return _make_settings(**kwargs)

    def _run(self, info: dict, lang=None, settings=None, proxy=None, events=None):
        """Helper: patch YoutubeDL, call _do_fetch, return FetchResult."""
        if settings is None:
            settings = self._settings()
        json3_bytes = _make_json3_response(events)
        mock_ctx, mock_ydl = _stub_ydl(info, json3_bytes)
        with patch("ytt.fetch.yt_dlp.YoutubeDL", return_value=mock_ctx):
            return _do_fetch("dQw4w9WgXcQ", lang, settings, proxy)

    def test_basic_manual_caption_result(self):
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)}, language="en")
        result = self._run(info, lang="en")
        assert isinstance(result, FetchResult)
        assert result.source == "caption_manual"
        assert result.served_lang == "en"

    def test_auto_caption_sets_source_caption_auto(self):
        info = _make_info(automatic_captions={"en": _auto(AUTO_EN_URL)})
        result = self._run(info, lang="en")
        assert result.source == "caption_auto"

    def test_title_populated(self):
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)}, title="My Title")
        result = self._run(info, lang="en")
        assert result.title == "My Title"

    def test_channel_populated(self):
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)}, channel="My Channel")
        result = self._run(info, lang="en")
        assert result.channel == "My Channel"

    def test_duration_sec_populated(self):
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)}, duration=240.0)
        result = self._run(info, lang="en")
        assert result.duration_sec == 240.0

    def test_published_populated(self):
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)}, upload_date="20230601")
        result = self._run(info, lang="en")
        assert result.published == "20230601"

    def test_requested_lang_set_on_fallback(self):
        # Requesting 'fr' but only 'en' available → fallback → requested_lang='fr'
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)}, language="en")
        result = self._run(info, lang="fr")
        assert result.requested_lang == "fr"
        assert result.served_lang == "en"
        assert result.message is not None

    def test_requested_lang_none_when_exact_match(self):
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)}, language="en")
        result = self._run(info, lang="en")
        assert result.requested_lang is None

    def test_available_langs_populated(self):
        info = _make_info(
            subtitles={"en": _manual(MANUAL_EN_URL)},
            automatic_captions={"fr": _auto(AUTO_FR_URL)},
        )
        result = self._run(info, lang="en")
        assert "en" in result.available_langs
        assert "fr" in result.available_langs

    def test_proxy_forwarded_to_ydl_opts(self):
        """When proxy is set, it must appear in the YDL opts dict."""
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)})
        captured_opts: list[dict] = []

        def capturing_ydl(opts):
            captured_opts.append(dict(opts))
            mock_ctx, mock_ydl = _stub_ydl(info, _make_json3_response())
            return mock_ctx

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=capturing_ydl):
            _do_fetch("dQw4w9WgXcQ", "en", self._settings(), proxy="http://proxy:8080")

        assert captured_opts, "YDL must be constructed at least once"
        assert captured_opts[0].get("proxy") == "http://proxy:8080"

    def test_no_proxy_in_opts_when_none(self):
        """When proxy=None, 'proxy' must not appear in the YDL opts."""
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)})
        captured_opts: list[dict] = []

        def capturing_ydl(opts):
            captured_opts.append(dict(opts))
            mock_ctx, _ = _stub_ydl(info, _make_json3_response())
            return mock_ctx

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=capturing_ydl):
            _do_fetch("dQw4w9WgXcQ", "en", self._settings(), proxy=None)

        assert "proxy" not in captured_opts[0]

    def test_download_error_raises_ytt_error(self):
        def bad_ydl(opts):
            mock_ctx = MagicMock()
            mock_ydl = MagicMock()
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("Private video")
            mock_ctx.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            return mock_ctx

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=bad_ydl):
            with pytest.raises(YttError) as exc_info:
                _do_fetch("dQw4w9WgXcQ", "en", self._settings(), proxy=None)
        assert exc_info.value.error_code == errors.PRIVATE

    def test_extractor_error_raises_ytt_error(self):
        def bad_ydl(opts):
            mock_ctx = MagicMock()
            mock_ydl = MagicMock()
            mock_ydl.extract_info.side_effect = yt_dlp.utils.ExtractorError(
                "HTTP Error 403"
            )
            mock_ctx.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            return mock_ctx

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=bad_ydl):
            with pytest.raises(YttError) as exc_info:
                _do_fetch("dQw4w9WgXcQ", "en", self._settings(), proxy=None)
        assert exc_info.value.error_code == errors.IP_BLOCKED

    def test_none_info_raises_empty_body(self):
        def null_ydl(opts):
            mock_ctx = MagicMock()
            mock_ydl = MagicMock()
            mock_ydl.extract_info.return_value = None
            mock_ctx.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            return mock_ctx

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=null_ydl):
            with pytest.raises(YttError) as exc_info:
                _do_fetch("dQw4w9WgXcQ", "en", self._settings(), proxy=None)
        assert exc_info.value.error_code == errors.EMPTY_BODY

    def test_segments_from_json3_events(self):
        """Events in the json3 response are parsed into segments."""
        events = [
            {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello"}]},
            {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": " world"}]},
        ]
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)})
        result = self._run(info, lang="en", events=events)
        assert len(result.segments) >= 1
        assert any("Hello" in s.text or "world" in s.text for s in result.segments)

    def test_uploader_fallback_for_channel(self):
        """Falls back to 'uploader' if 'channel' is absent."""
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)})
        info.pop("channel", None)
        info["uploader"] = "Fallback Uploader"
        result = self._run(info, lang="en")
        assert result.channel == "Fallback Uploader"

    def test_no_cookies_in_ydl_opts_used_for_real_call(self):
        """The actual opts passed to YDL must always include cookiefile=None."""
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)})
        seen_opts: list[dict] = []

        def capturing_ydl(opts):
            seen_opts.append(dict(opts))
            mock_ctx, _ = _stub_ydl(info, _make_json3_response())
            return mock_ctx

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=capturing_ydl):
            _do_fetch("dQw4w9WgXcQ", "en", self._settings(), proxy=None)

        assert seen_opts
        opts = seen_opts[0]
        assert "cookiefile" in opts and opts["cookiefile"] is None
        assert "cookiesfrombrowser" in opts and opts["cookiesfrombrowser"] is None


# ---------------------------------------------------------------------------
# fetch_transcript — async public API (timeout + proxy retry)
# ---------------------------------------------------------------------------

class TestFetchTranscriptAsync:
    """Async wrapper: timeout→RATE_LIMITED, ip_blocked→proxy retry."""

    def _settings_with_proxy(self, proxy_url: str = "http://proxy:8080"):
        return _make_settings(proxy_url=proxy_url)

    def _settings_no_proxy(self):
        return _make_settings()

    async def test_success_returns_fetch_result(self):
        info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)})
        json3_bytes = _make_json3_response()
        mock_ctx, _ = _stub_ydl(info, json3_bytes)

        with patch("ytt.fetch.yt_dlp.YoutubeDL", return_value=mock_ctx):
            result = await fetch_transcript("dQw4w9WgXcQ", "en", self._settings_no_proxy())
        assert isinstance(result, FetchResult)

    async def test_timeout_raises_rate_limited(self):
        """asyncio.wait_for TimeoutError must be converted to RATE_LIMITED."""

        async def _slow():
            await asyncio.sleep(999)

        with patch("ytt.fetch.asyncio.to_thread", side_effect=lambda f, *a, **kw: _slow()):
            with patch(
                "ytt.fetch.asyncio.wait_for",
                side_effect=asyncio.TimeoutError,
            ):
                with pytest.raises(YttError) as exc_info:
                    await fetch_transcript(
                        "dQw4w9WgXcQ", "en", self._settings_no_proxy()
                    )
        assert exc_info.value.error_code == errors.RATE_LIMITED

    async def test_ip_blocked_with_proxy_retries(self):
        """ip_blocked + proxy_url → second call uses the proxy."""
        call_count = 0

        def side_effect_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise YttError(errors.IP_BLOCKED, "blocked")
            # Second call (with proxy) succeeds
            info = _make_info(subtitles={"en": _manual(MANUAL_EN_URL)})
            return FetchResult(
                segments=[],
                source="caption_manual",
                served_lang="en",
                requested_lang=None,
                available_langs=["en"],
            )

        with patch("ytt.fetch._do_fetch", side_effect=side_effect_fetch):
            result = await fetch_transcript(
                "dQw4w9WgXcQ", "en", self._settings_with_proxy()
            )
        assert call_count == 2
        assert isinstance(result, FetchResult)

    async def test_ip_blocked_no_proxy_raises(self):
        """ip_blocked with no proxy_url should re-raise immediately (no retry)."""

        def fail_fetch(*args, **kwargs):
            raise YttError(errors.IP_BLOCKED, "blocked")

        with patch("ytt.fetch._do_fetch", side_effect=fail_fetch):
            with pytest.raises(YttError) as exc_info:
                await fetch_transcript(
                    "dQw4w9WgXcQ", "en", self._settings_no_proxy()
                )
        assert exc_info.value.error_code == errors.IP_BLOCKED

    async def test_proxy_retry_failure_preserves_error_code(self):
        """If the proxy retry also fails, the retry error_code is preserved."""
        call_count = 0

        def fail_twice(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise YttError(errors.IP_BLOCKED, f"blocked call {call_count}")

        with patch("ytt.fetch._do_fetch", side_effect=fail_twice):
            with pytest.raises(YttError) as exc_info:
                await fetch_transcript(
                    "dQw4w9WgXcQ", "en", self._settings_with_proxy()
                )
        assert call_count == 2
        assert exc_info.value.error_code == errors.IP_BLOCKED
        assert "proxy retry also failed" in exc_info.value.message

    async def test_non_ip_blocked_error_not_retried(self):
        """PRIVATE error must not trigger a proxy retry."""
        call_count = 0

        def fail_private(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise YttError(errors.PRIVATE, "Private video")

        with patch("ytt.fetch._do_fetch", side_effect=fail_private):
            with pytest.raises(YttError) as exc_info:
                await fetch_transcript(
                    "dQw4w9WgXcQ", "en", self._settings_with_proxy()
                )
        # Should NOT retry — call count must be exactly 1
        assert call_count == 1
        assert exc_info.value.error_code == errors.PRIVATE
