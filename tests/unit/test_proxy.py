"""Unit tests for the ``YTT_PROXY_URL`` contract (``docs/notes/proxy-egress.md``).

Covers, end to end but fully mocked (no network):

- ``Settings`` proxy-URL validation (scheme / whitespace / empty fail-closed);
- :func:`ytt.observability.redact_credentials` — proxy creds never survive in
  free-text error strings;
- :func:`ytt.fetch.run_with_proxy_retry` — the shared direct-first + one-shot
  ``ip_blocked`` retry semantics and their defined failure behavior;
- the caption path (``fetch_transcript``) and the Whisper audio download
  (``run_whisper_job``) both constructing yt-dlp with the configured proxy
  **only on the retry**;
- the egress probe using the httpx >= 0.28 singular ``proxy=`` kwarg;
- the canary's ``--via-proxy`` end-to-end mode;
- redaction boundaries: yt-dlp error strings and the ``/admin/egress`` 502
  body are credential-free;
- the CLI wiring of ``--via-proxy``.

Regression coverage (bead ``ytt-31ec1026`` — the redaction contract end to
end, on **both** retry legs and into **rendered log output**):

- a credential-bearing yt-dlp failure on the direct attempt or the proxied
  retry reaches the caller's ``YttError.message`` (captions) and the stored
  Whisper job ``message`` (what ``get_transcript_job`` relays verbatim) with
  the userinfo stripped and host:port kept;
- the ``ExtractorError`` boundary redacts too;
- an httpx failure quoting the proxy lands redacted in the canary report;
- structlog-rendered JSON lines (the ``error=str(exc)`` log-argument shapes
  of the startup egress probe and Whisper cleanup paths) carry no creds.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yt_dlp
from starlette.testclient import TestClient

from ytt import errors
from ytt.errors import YttError
from ytt.fetch import FetchResult, _do_fetch, run_with_proxy_retry
from ytt.models import EgressReport
from ytt.observability import redact_credentials

_PROXY = "http://alice:s3cret@proxy.example.com:3128"

#: yt-dlp's datacenter-IP bot check — classifies as ``ip_blocked`` (SEED_MAP).
_BOT_CHECK = "ERROR: Sign in to confirm you're not a bot"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fetch_settings(**kwargs):
    """Build a real Settings from env overrides (test_fetch pattern)."""
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


def _make_info(subtitles: dict | None = None) -> dict:
    """Minimal yt-dlp info dict with one manual json3 track."""
    return {
        "id": "dQw4w9WgXcQ",
        "title": "Test Video",
        "channel": "Test Channel",
        "duration": 120.0,
        "upload_date": "20240101",
        "language": "en",
        "subtitles": subtitles
        or {
            "en": [
                {
                    "ext": "json3",
                    "url": "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ&lang=en",
                }
            ]
        },
        "automatic_captions": {},
    }


def _json3_bytes() -> bytes:
    import json

    return json.dumps({"events": []}).encode()


def _stub_ydl_ctx(info: dict | None = None, exc: Exception | None = None):
    """Mock YoutubeDL context manager: returns *info* or raises *exc*."""
    mock_ydl = MagicMock()
    if exc is not None:
        mock_ydl.extract_info.side_effect = exc
    else:
        mock_ydl.extract_info.return_value = info
        mock_resp = MagicMock()
        mock_resp.read.return_value = _json3_bytes()
        mock_ydl.urlopen.return_value = mock_resp
        mock_ydl.download.return_value = None
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    return mock_ctx


def _capturing_ydl(outcomes: list):
    """YoutubeDL side_effect handing out *outcomes* per construction.

    Each outcome is an ``Exception`` (raised from ``extract_info``) or an
    ``info`` dict. Records every opts dict passed to ``YoutubeDL(opts)``.
    """
    captured: list[dict] = []
    calls = {"n": 0}

    def factory(opts):
        i = min(calls["n"], len(outcomes) - 1)
        outcome = outcomes[i]
        calls["n"] += 1
        captured.append(dict(opts))
        if isinstance(outcome, Exception):
            return _stub_ydl_ctx(exc=outcome)
        return _stub_ydl_ctx(info=outcome)

    return factory, captured


def _fetch_result() -> FetchResult:
    return FetchResult(
        segments=[],
        source="caption_manual",
        served_lang="en",
        requested_lang=None,
        available_langs=["en"],
    )


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------

class TestProxyUrlValidation:
    def test_unset_is_none(self):
        assert _make_fetch_settings().proxy_url is None

    def test_http_url_accepted_verbatim(self):
        s = _make_fetch_settings(proxy_url=_PROXY)
        assert s.proxy_url == _PROXY

    def test_https_url_accepted(self):
        s = _make_fetch_settings(proxy_url="https://proxy.example.com:3128")
        assert s.proxy_url == "https://proxy.example.com:3128"

    @pytest.mark.parametrize(
        "bad",
        [
            "socks5://proxy.example.com:1080",  # no httpx SOCKS adapter
            "socks4://proxy.example.com:1080",
            "http://",  # no hostname
            "proxy.example.com:3128",  # no scheme
            "ftp://proxy.example.com:3128",
        ],
    )
    def test_invalid_urls_raise_at_construction(self, bad):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as excinfo:
            _make_fetch_settings(proxy_url=bad)
        assert "YTT_PROXY_URL" in str(excinfo.value)

    def test_empty_error_says_to_unset_entirely(self):
        """The empty case is the missing-manifest-secret case — the message
        must say how to get direct egress back (unset, not empty)."""
        with pytest.raises(Exception) as excinfo:
            _make_fetch_settings(proxy_url="")
        assert "unset the variable entirely" in str(excinfo.value)

    def test_whitespace_is_rejected(self):
        """Copy-paste line wraps are the classic way a proxy URL breaks."""
        with pytest.raises(Exception) as excinfo:
            _make_fetch_settings(proxy_url="http://proxy.example.com:3128 path")
        assert "whitespace" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Credential redaction (free-text boundary)
# ---------------------------------------------------------------------------

class TestRedactCredentials:
    @pytest.mark.parametrize(
        "dirty,expected",
        [
            (
                "Unable to communicate with proxy "
                "http://alice:s3cret@proxy.example.com:3128 timed out",
                "Unable to communicate with proxy "
                "http://proxy.example.com:3128 timed out",
            ),
            (
                "connect failed: http://alice:s3cret@proxy.example.com:3128, "
                "retrying",
                "connect failed: http://proxy.example.com:3128, retrying",
            ),
            (
                "tried http://alice:s3cret@proxy.example.com:3128 then "
                "http://bob:pw@backup.example.com:8080, both down",
                "tried http://proxy.example.com:3128 then "
                "http://backup.example.com:8080, both down",
            ),
        ],
    )
    def test_credentialed_urls_are_stripped(self, dirty, expected):
        assert redact_credentials(dirty) == expected

    def test_bare_credentialed_url_keeps_host_and_port(self):
        """The exact value yt-dlp dials with (``opts["proxy"]``): userinfo
        stripped, host:port kept for diagnosability."""
        assert redact_credentials(_PROXY) == "http://proxy.example.com:3128"

    @pytest.mark.parametrize(
        "safe",
        [
            "no credentials in here",
            "http://proxy.example.com:3128 refused the connection",
            "Extraction timed out after 60s (possible silent hang)",
            "",
        ],
    )
    def test_safe_text_passes_through_unchanged(self, safe):
        assert redact_credentials(safe) == safe


# ---------------------------------------------------------------------------
# run_with_proxy_retry — the shared semantics
# ---------------------------------------------------------------------------

class TestRunWithProxyRetry:
    @staticmethod
    def _op(outcomes: list):
        """Async op returning/raising ``outcomes[i]`` on attempt i (last one repeats)."""
        calls: list[str | None] = []

        async def op(proxy: str | None):
            calls.append(proxy)
            i = min(len(calls) - 1, len(outcomes) - 1)
            outcome = outcomes[i]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return op, calls

    async def test_direct_success_never_touches_the_proxy(self):
        op, calls = self._op(["ok"])
        result = await run_with_proxy_retry(
            op, proxy_url=_PROXY, timeout_sec=5, timeout_code=errors.RATE_LIMITED,
            what="Extraction",
        )
        assert result == "ok"
        assert calls == [None]

    async def test_direct_timeout_maps_to_timeout_code(self):
        """A real (short) wait_for timeout on the direct attempt → timeout_code."""

        async def slow(proxy):
            await asyncio.sleep(1.0)

        with pytest.raises(YttError) as excinfo:
            await run_with_proxy_retry(
                slow,
                proxy_url=None,
                timeout_sec=0.05,
                timeout_code=errors.RATE_LIMITED,
                what="Extraction",
                timeout_detail=" (possible silent hang; retrying may help)",
            )
        assert excinfo.value.error_code == errors.RATE_LIMITED
        assert "Extraction timed out after 0.05s" in excinfo.value.message
        assert "retrying may help" in excinfo.value.message

    async def test_ip_blocked_with_proxy_retries_exactly_once(self):
        op, calls = self._op(
            [YttError(errors.IP_BLOCKED, "blocked"), "via-proxy"]
        )
        result = await run_with_proxy_retry(
            op, proxy_url=_PROXY, timeout_sec=5, timeout_code=errors.RATE_LIMITED,
            what="Extraction",
        )
        assert result == "via-proxy"
        assert calls == [None, _PROXY]

    async def test_ip_blocked_without_proxy_raises_unchanged(self):
        original = YttError(errors.IP_BLOCKED, "blocked")
        op, calls = self._op([original])
        with pytest.raises(YttError) as excinfo:
            await run_with_proxy_retry(
                op, proxy_url=None, timeout_sec=5,
                timeout_code=errors.RATE_LIMITED, what="Extraction",
            )
        assert excinfo.value is original
        assert calls == [None]

    async def test_non_ip_blocked_error_is_never_retried(self):
        op, calls = self._op([YttError(errors.PRIVATE, "private video")])
        with pytest.raises(YttError) as excinfo:
            await run_with_proxy_retry(
                op, proxy_url=_PROXY, timeout_sec=5,
                timeout_code=errors.RATE_LIMITED, what="Extraction",
            )
        assert excinfo.value.error_code == errors.PRIVATE
        assert "proxy retry" not in excinfo.value.message
        assert calls == [None]

    async def test_proxied_retry_timeout_maps_to_timeout_code(self):
        """Whisper audio mapping: retry timing out → ASR_FAILED, 'also timed out'."""
        # Attempt 1 raises ip_blocked immediately; the proxied retry hangs
        # past a real (short) wait_for timeout.
        attempts = {"n": 0}
        calls: list = []

        async def op(proxy):
            attempts["n"] += 1
            calls.append(proxy)
            if proxy is None:
                raise YttError(errors.IP_BLOCKED, "blocked")
            await asyncio.sleep(1.0)
            return "ok"

        with pytest.raises(YttError) as excinfo:
            await run_with_proxy_retry(
                op,
                proxy_url=_PROXY,
                timeout_sec=0.05,
                timeout_code=errors.ASR_FAILED,
                what="Audio download",
            )
        assert excinfo.value.error_code == errors.ASR_FAILED
        assert "Audio download timed out after 0.05s" in excinfo.value.message
        assert "(proxy retry also timed out)" in excinfo.value.message
        assert calls == [None, _PROXY]

    async def test_proxied_retry_failure_keeps_retry_error_code(self):
        op, calls = self._op(
            [YttError(errors.IP_BLOCKED, "blocked"), YttError(errors.EMPTY_BODY, "empty body")]
        )
        with pytest.raises(YttError) as excinfo:
            await run_with_proxy_retry(
                op, proxy_url=_PROXY, timeout_sec=5,
                timeout_code=errors.RATE_LIMITED, what="Extraction",
            )
        assert excinfo.value.error_code == errors.EMPTY_BODY
        assert "empty body (proxy retry also failed)" in excinfo.value.message
        assert calls == [None, _PROXY]


# ---------------------------------------------------------------------------
# Caption path: the retry must construct yt-dlp with the proxy
# ---------------------------------------------------------------------------

class TestCaptionPathProxiesTheRetry:
    async def test_retry_constructs_ydl_with_the_configured_proxy(self):
        """fetch_transcript: attempt 1 direct (no proxy opt), attempt 2 via
        the configured proxy — proven at the YoutubeDL(opts) boundary."""
        from ytt.fetch import fetch_transcript

        info = _make_info()
        factory, captured = _capturing_ydl(
            [yt_dlp.utils.DownloadError(_BOT_CHECK), info]
        )
        settings = _make_fetch_settings(proxy_url=_PROXY)

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=factory):
            result = await fetch_transcript("dQw4w9WgXcQ", "en", settings)

        assert isinstance(result, FetchResult)
        assert len(captured) == 2, "expected direct attempt + one proxied retry"
        assert "proxy" not in captured[0]
        assert captured[1].get("proxy") == _PROXY


# ---------------------------------------------------------------------------
# Whisper audio path: the same retry semantics
# ---------------------------------------------------------------------------

class TestWhisperAudioPathProxiesTheRetry:
    VIDEO_ID = "dQw4w9WgXcQ"

    @staticmethod
    def _whisper_settings(scratch: str, proxy_url: str | None):
        s = MagicMock()
        s.max_asr_duration_sec = 1200
        s.whisper_realtime_factor = 2.0
        s.job_ttl_sec = 3600
        s.whisper_timeout_sec = 2880
        s.max_audio_bytes = 500 * 1024 * 1024
        s.scratch_dir = scratch
        s.whisper_url = "http://whisper.local:8000"
        s.whisper_model = "Systran/faster-whisper-small"
        s.proxy_url = proxy_url
        return s

    @staticmethod
    def _cache():
        cache = MagicMock()
        cache.get.return_value = None
        cache.put = AsyncMock()  # TranscriptCache.put is async
        return cache

    async def _run_job(self, settings, outcomes):
        """run_whisper_job with yt-dlp stubbed to *outcomes* per construction.

        Returns (captured_opts, registry).
        """
        import os
        from pathlib import Path

        from ytt.whisper import WhisperJobRegistry, run_whisper_job

        scratch = settings.scratch_dir
        os.makedirs(scratch, exist_ok=True)
        # Pre-written file satisfies the post-download glob (yt-dlp is mocked).
        audio_file = Path(scratch) / f"{self.VIDEO_ID}.mp4"
        audio_file.write_bytes(b"fake audio")

        factory, captured = _capturing_ydl(outcomes)
        registry = WhisperJobRegistry()
        job, _ = await registry.get_or_create(self.VIDEO_ID, None, settings, owner="anonymous")

        whisper_resp = MagicMock(spec=httpx.Response)
        whisper_resp.status_code = 200
        whisper_resp.json.return_value = {
            "text": "hello", "language": "en", "segments": [],
        }
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=whisper_resp)

        with patch("ytt.whisper.yt_dlp.YoutubeDL", side_effect=factory):
            await run_whisper_job(
                job, registry, settings, self._cache(),
                "Systran/faster-whisper-small",
                http_client=http_client,
            )
        return captured, registry

    async def test_retry_constructs_ydl_with_the_configured_proxy(self, tmp_path):
        """run_whisper_job: the audio download gets the same direct-first +
        one-shot proxied retry as caption fetches."""
        captured, registry = await self._run_job(
            self._whisper_settings(str(tmp_path / "scratch"), _PROXY),
            [yt_dlp.utils.DownloadError(_BOT_CHECK), {"duration": 50}],
        )
        assert len(captured) == 2, "expected direct attempt + one proxied retry"
        assert "proxy" not in captured[0]
        assert captured[1].get("proxy") == _PROXY
        final = await registry.get(self.VIDEO_ID)
        assert final is not None and final.status == "done"

    async def test_retry_timeout_reports_asr_failed(self, tmp_path):
        """Proxied retry timing out → ASR_FAILED '(proxy retry also timed out)'."""
        settings = self._whisper_settings(str(tmp_path / "scratch"), _PROXY)
        # Real, short wait_for timeout. 0.5s (not 0.05s): the direct attempt
        # must raise its ip_blocked error well inside the budget even on a
        # CPU-throttled CI pod, where first-call to_thread executor spin-up
        # alone blew a 50ms budget (in-container flake, 2026-09-18); the
        # proxied retry sleeps 10x the budget so it still reliably times out.
        settings.whisper_timeout_sec = 0.5
        scratch = settings.scratch_dir
        import os
        from pathlib import Path

        os.makedirs(scratch, exist_ok=True)
        (Path(scratch) / f"{self.VIDEO_ID}.mp4").write_bytes(b"fake audio")

        # Attempt 1 (direct) fails the bot check; the proxied retry's
        # extract_info hangs past the 0.5s wait_for timeout.
        def slow_factory(opts):
            ctx = MagicMock()
            ydl = MagicMock()

            def extract(url, download=False):
                if "proxy" not in opts:
                    raise yt_dlp.utils.DownloadError(_BOT_CHECK)
                import time as _time

                _time.sleep(5)
                return {"duration": 50}

            ydl.extract_info.side_effect = extract
            ctx.__enter__ = MagicMock(return_value=ydl)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        from ytt.whisper import WhisperJobRegistry, run_whisper_job

        registry = WhisperJobRegistry()
        job, _ = await registry.get_or_create(self.VIDEO_ID, None, settings, owner="anonymous")
        cache = self._cache()

        with patch("ytt.whisper.yt_dlp.YoutubeDL", side_effect=slow_factory):
            await run_whisper_job(
                job, registry, settings, cache,
                "Systran/faster-whisper-small",
                http_client=AsyncMock(spec=httpx.AsyncClient),
            )

        final = await registry.get(self.VIDEO_ID)
        assert final is not None and final.status == "error"
        assert final.error_code == errors.ASR_FAILED
        assert final.message is not None
        assert "proxy retry also timed out" in final.message


# ---------------------------------------------------------------------------
# Egress probe: httpx >= 0.28 singular proxy kwarg
# ---------------------------------------------------------------------------

def _egress_report() -> EgressReport:
    return EgressReport(
        ip="203.0.113.7",
        asn="AS7922",
        org="Comcast Cable",
        via_proxy=True,
        is_residential=True,
    )


class TestEgressProbeProxyKwarg:
    """httpx >= 0.28 removed ``proxies=``; passing it raises TypeError, which
    silently degraded every proxy-configured egress report to 'probe failed'.
    The probe must pass the singular ``proxy=``."""

    @staticmethod
    def _patch_ipinfo():
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"ip": "203.0.113.7", "org": "AS7922 Comcast Cable"}
        resp.raise_for_status.return_value = None
        client = MagicMock()
        client.get.return_value = resp
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        return patch("ytt.selftest.httpx.Client", return_value=client)

    def test_proxied_probe_uses_singular_proxy_kwarg(self):
        from ytt.selftest import probe_egress

        with self._patch_ipinfo() as client_cls:
            report = probe_egress(_PROXY)
        kwargs = client_cls.call_args[1]
        assert kwargs.get("proxy") == _PROXY
        assert "proxies" not in kwargs
        assert report.via_proxy is True

    def test_direct_probe_sends_no_proxy_kwarg(self):
        from ytt.selftest import probe_egress

        with self._patch_ipinfo() as client_cls:
            report = probe_egress(None)
        kwargs = client_cls.call_args[1]
        assert "proxy" not in kwargs
        assert "proxies" not in kwargs
        assert report.via_proxy is False


# ---------------------------------------------------------------------------
# Canary --via-proxy: the end-to-end vehicle
# ---------------------------------------------------------------------------

class TestCanaryViaProxy:
    async def test_probe_once_detail_forwards_proxy_to_ydl_opts(self):
        from ytt.canary import probe_once_detail

        factory, captured = _capturing_ydl([_make_info()])
        with patch("yt_dlp.YoutubeDL", side_effect=factory):
            report = probe_once_detail("jNQXAC9IVRw", proxy=_PROXY)
        assert captured[0].get("proxy") == _PROXY
        assert report["via_proxy"] is True
        assert report["outcome"] == "ok"

    async def test_probe_once_detail_default_dials_direct(self):
        from ytt.canary import probe_once_detail

        factory, captured = _capturing_ydl([_make_info()])
        with patch("yt_dlp.YoutubeDL", side_effect=factory):
            report = probe_once_detail("jNQXAC9IVRw")
        assert "proxy" not in captured[0]
        assert report["via_proxy"] is False

    async def test_probe_once_detail_error_shape_reports_via_proxy_and_redacts(
        self,
    ):
        from ytt.canary import probe_once_detail

        factory, _ = _capturing_ydl(
            [yt_dlp.utils.DownloadError(
                f"proxy dial failed at {_PROXY} unexpectedly"
            )]
        )
        with patch("yt_dlp.YoutubeDL", side_effect=factory):
            report = probe_once_detail("jNQXAC9IVRw", proxy=_PROXY)
        assert report["via_proxy"] is True
        assert report["outcome"] == errors.IP_BLOCKED or report["outcome"] != "ok"
        assert "s3cret" not in report["error"]
        assert "proxy.example.com:3128" in report["error"]

    @staticmethod
    def _run_once():
        """run_once with a proxy-configured Settings; returns (report, ydl_cls, probe_mock)."""
        from ytt.canary import run_once

        settings = _make_fetch_settings(proxy_url=_PROXY)
        factory, captured = _capturing_ydl([_make_info()])
        probe_mock = MagicMock(return_value=_egress_report())
        with patch("ytt.config.get_settings", return_value=settings):
            with patch("ytt.selftest.probe_egress", probe_mock):
                with patch("yt_dlp.YoutubeDL", side_effect=factory):
                    report = run_once(via_proxy=True)
        return report, captured, probe_mock

    def test_via_proxy_true_probes_and_fetches_through_the_proxy(self):
        report, captured, probe_mock = self._run_once()
        # The egress probe dialed through the proxy (it classifies proxy egress)
        assert probe_mock.call_args[1].get("proxy_url") == _PROXY or (
            probe_mock.call_args[0] and probe_mock.call_args[0][0] == _PROXY
        )
        # The caption fetch went THROUGH the proxy (the end-to-end check)
        assert captured[0].get("proxy") == _PROXY
        assert report["caption_fetch"]["via_proxy"] is True

    def test_default_run_once_fetches_direct_but_probes_the_proxy(self):
        from ytt.canary import run_once

        settings = _make_fetch_settings(proxy_url=_PROXY)
        factory, captured = _capturing_ydl([_make_info()])
        probe_mock = MagicMock(return_value=_egress_report())
        with patch("ytt.config.get_settings", return_value=settings):
            with patch("ytt.selftest.probe_egress", probe_mock):
                with patch("yt_dlp.YoutubeDL", side_effect=factory):
                    report = run_once()
        # Default: caption probe dials direct (matches the caption path);
        # the egress half still classifies the proxy's egress.
        assert "proxy" not in captured[0]
        assert report["caption_fetch"]["via_proxy"] is False
        assert report["egress"]["via_proxy"] is True


# ---------------------------------------------------------------------------
# Redaction boundaries: error strings that get relayed or logged
# ---------------------------------------------------------------------------

class TestErrorRedactionBoundaries:
    def test_fetch_download_error_quoting_proxy_is_redacted(self):
        """A yt-dlp DownloadError quoting the credentialed proxy URL must not
        carry the credentials into the relayable YttError message."""
        dirty = f"unable to talk to {_PROXY} right now"
        factory, _ = _capturing_ydl([yt_dlp.utils.DownloadError(dirty)])
        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=factory):
            with pytest.raises(YttError) as excinfo:
                _do_fetch("dQw4w9WgXcQ", "en", _make_fetch_settings(), proxy=_PROXY)
        assert "s3cret" not in excinfo.value.message
        assert "proxy.example.com:3128" in excinfo.value.message

    def test_admin_egress_502_body_is_redacted(self, monkeypatch):
        """The egress-probe 502 body is logged and relayed — credentials out."""
        dirty = (
            "Unable to communicate with proxy "
            f"{_PROXY} timed out"
        )
        monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", "allowed@example.com")

        # Auth: bypass the middleware check and hand the handler a token whose
        # claims match the allowlist (no Google round-trip in unit tests).
        @staticmethod
        def _fake_token():
            token = MagicMock()
            token.claims = {
                "email": "allowed@example.com",
                "email_verified": True,
            }
            return token

        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token", _fake_token
        )
        # The /admin/egress closure captured the import-time settings singleton
        # (get_settings() may have been cache-cleared + rebuilt by other tests
        # by the time this runs) — patch THAT instance's allowlist.
        import ytt.server as _server

        monkeypatch.setattr(
            _server._settings_singleton,
            "allowed_subjects",
            "allowed@example.com",
            raising=False,
        )

        def failing_probe(proxy_url=None):
            raise httpx.ConnectError(dirty)

        monkeypatch.setattr("ytt.selftest.probe_egress", failing_probe)

        from ytt.server import build_asgi_app

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/ytt/admin/egress", headers={"Authorization": "Bearer faketoken"}
        )
        assert resp.status_code == 502
        body = resp.json()
        assert "s3cret" not in body["error"]
        assert "proxy.example.com:3128" in body["error"]


# ---------------------------------------------------------------------------
# Regression (bead ytt-31ec1026): credentials never survive the direct or
# proxied retry paths, in relayed errors or rendered log lines
# ---------------------------------------------------------------------------

#: A yt-dlp/httpx-style failure string quoting the configured proxy verbatim
#: (creds included) — the shape every redaction boundary must sanitize.
_DIRTY_PROXY_ERR = (
    "Unable to communicate with proxy "
    f"{_PROXY} timed out"
)

#: The host:port the contract promises to KEEP (docs/notes/proxy-egress.md:
#: "Redaction strips the userinfo … host and port stay for diagnosability").
_PROXY_HOSTPORT = "proxy.example.com:3128"


def _assert_credential_free(text: str) -> None:
    """No proxy userinfo anywhere in *text* — the relayed/logged form is clean."""
    assert "alice" not in text
    assert "s3cret" not in text
    assert "@" not in text
    assert _PROXY not in text


class TestCaptionRetryPathCredentialRedaction:
    """A credential-bearing yt-dlp failure on either leg of the caption
    retry (direct first, one proxied retry on ``ip_blocked``) must reach the
    caller as a redacted, verbatim-relayable ``YttError.message``."""

    async def test_proxied_retry_failure_is_redacted_end_to_end(self):
        """Direct attempt hits the bot check; the proxied retry fails with an
        exception quoting the credentialed proxy — the raised message carries
        "(proxy retry also failed)" and no credentials, host:port intact."""
        from ytt.fetch import fetch_transcript

        factory, captured = _capturing_ydl(
            [
                yt_dlp.utils.DownloadError(_BOT_CHECK),
                yt_dlp.utils.DownloadError(_DIRTY_PROXY_ERR),
            ]
        )
        settings = _make_fetch_settings(proxy_url=_PROXY)

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=factory):
            with pytest.raises(YttError) as excinfo:
                await fetch_transcript("dQw4w9WgXcQ", "en", settings)

        message = excinfo.value.message
        _assert_credential_free(message)
        assert _PROXY_HOSTPORT in message, "host:port must survive for diagnosis"
        assert "(proxy retry also failed)" in message
        # Both legs ran, and only the retry dialed through the proxy.
        assert len(captured) == 2
        assert "proxy" not in captured[0]
        assert captured[1].get("proxy") == _PROXY

    async def test_direct_attempt_failure_is_redacted_and_never_retried(self):
        """Direct-only run (no proxy configured): even a DownloadError quoting
        a credentialed URL is stripped at the YttError boundary, and no retry
        happens (a non-ip_blocked classification never justifies one)."""
        from ytt.fetch import fetch_transcript

        factory, captured = _capturing_ydl(
            [yt_dlp.utils.DownloadError(_DIRTY_PROXY_ERR)]
        )
        settings = _make_fetch_settings()  # proxy_url=None — direct only

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=factory):
            with pytest.raises(YttError) as excinfo:
                await fetch_transcript("dQw4w9WgXcQ", "en", settings)

        message = excinfo.value.message
        _assert_credential_free(message)
        assert "(proxy retry" not in message
        assert len(captured) == 1, "a direct-only failure must not retry"

    async def test_extractor_error_quoting_proxy_is_redacted(self):
        """The ExtractorError boundary redacts too (only the DownloadError
        branch was pinned before) — single proxied attempt, canary-style."""
        dirty = f"Failed to extract player data from {_PROXY}"
        factory, captured = _capturing_ydl([yt_dlp.utils.ExtractorError(dirty)])

        with patch("ytt.fetch.yt_dlp.YoutubeDL", side_effect=factory):
            with pytest.raises(YttError) as excinfo:
                _do_fetch(
                    "dQw4w9WgXcQ", "en", _make_fetch_settings(), proxy=_PROXY
                )

        message = excinfo.value.message
        _assert_credential_free(message)
        assert _PROXY_HOSTPORT in message
        assert captured[0].get("proxy") == _PROXY


class TestWhisperAudioPathCredentialRedaction:
    """The Whisper audio download shares the retry machinery; a
    credential-bearing failure on either leg must leave the job's stored
    ``message`` — what ``get_transcript_job`` relays verbatim — clean."""

    VIDEO_ID = "dQw4w9WgXcQ"

    @staticmethod
    def _settings(scratch: str, proxy_url: str | None):
        s = MagicMock()
        s.max_asr_duration_sec = 1200
        s.whisper_realtime_factor = 2.0
        s.job_ttl_sec = 3600
        s.whisper_timeout_sec = 2880
        s.max_audio_bytes = 500 * 1024 * 1024
        s.scratch_dir = scratch
        s.whisper_url = "http://whisper.local:8000"
        s.whisper_model = "Systran/faster-whisper-small"
        s.proxy_url = proxy_url
        return s

    @staticmethod
    def _cache():
        cache = MagicMock()
        cache.get.return_value = None
        cache.put = AsyncMock()  # TranscriptCache.put is async
        return cache

    async def _run_job(self, settings, outcomes):
        """run_whisper_job with yt-dlp stubbed to *outcomes* per construction.

        Returns (captured opts dicts, registry) — the registry holds the job.
        """
        import os
        from pathlib import Path

        from ytt.whisper import WhisperJobRegistry, run_whisper_job

        scratch = settings.scratch_dir
        os.makedirs(scratch, exist_ok=True)
        # Pre-written file satisfies the post-download glob (yt-dlp is mocked).
        (Path(scratch) / f"{self.VIDEO_ID}.mp4").write_bytes(b"fake audio")

        factory, captured = _capturing_ydl(outcomes)
        registry = WhisperJobRegistry()
        job, _ = await registry.get_or_create(self.VIDEO_ID, None, settings, owner="anonymous")

        whisper_resp = MagicMock(spec=httpx.Response)
        whisper_resp.status_code = 200
        whisper_resp.json.return_value = {
            "text": "hello", "language": "en", "segments": [],
        }
        http_client = AsyncMock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=whisper_resp)

        with patch("ytt.whisper.yt_dlp.YoutubeDL", side_effect=factory):
            await run_whisper_job(
                job, registry, settings, self._cache(),
                "Systran/faster-whisper-small",
                http_client=http_client,
            )
        return captured, registry

    async def test_proxied_retry_failure_message_is_redacted(self, tmp_path):
        """Direct attempt blocked, proxied retry fails quoting the proxy —
        the stored job message (relayed verbatim by get_transcript_job) is
        credential-free with the retry suffix and host:port intact."""
        captured, registry = await self._run_job(
            self._settings(str(tmp_path / "scratch"), _PROXY),
            [
                yt_dlp.utils.DownloadError(_BOT_CHECK),
                yt_dlp.utils.DownloadError(_DIRTY_PROXY_ERR),
            ],
        )
        final = await registry.get(self.VIDEO_ID)
        assert final is not None and final.status == "error"
        message = final.message or ""
        _assert_credential_free(message)
        assert _PROXY_HOSTPORT in message
        assert "(proxy retry also failed)" in message
        assert len(captured) == 2
        assert "proxy" not in captured[0]
        assert captured[1].get("proxy") == _PROXY

    async def test_direct_attempt_failure_message_is_redacted(self, tmp_path):
        """A direct-only failure quoting the proxy never reaches the stored
        job message, and no retry is attempted."""
        captured, registry = await self._run_job(
            self._settings(str(tmp_path / "scratch"), None),
            [yt_dlp.utils.DownloadError(_DIRTY_PROXY_ERR)],
        )
        final = await registry.get(self.VIDEO_ID)
        assert final is not None and final.status == "error"
        message = final.message or ""
        _assert_credential_free(message)
        assert "(proxy retry" not in message
        assert len(captured) == 1

    def test_download_error_boundary_redacts_before_the_retry_layer(self, tmp_path):
        """Unit leg: ``_do_download_audio`` strips proxy creds from the raised
        YttError itself — run_with_proxy_retry only ever sees clean text."""
        from ytt.whisper import _do_download_audio

        factory, captured = _capturing_ydl(
            [yt_dlp.utils.DownloadError(_DIRTY_PROXY_ERR)]
        )
        with patch("ytt.whisper.yt_dlp.YoutubeDL", side_effect=factory):
            with pytest.raises(YttError) as excinfo:
                _do_download_audio(
                    self.VIDEO_ID,
                    str(tmp_path),
                    500 * 1024 * 1024,
                    proxy=_PROXY,
                    max_asr_duration_sec=1200,
                )
        message = excinfo.value.message
        _assert_credential_free(message)
        assert _PROXY_HOSTPORT in message
        assert captured[0].get("proxy") == _PROXY


class TestHttpxFailureRedaction:
    """httpx failures quote the proxy too (the egress probe dials through
    it, per the contract) — every relayed form must come out clean."""

    def test_run_once_egress_probe_failure_error_is_redacted(self):
        """The one-shot canary's egress half: an httpx ConnectError quoting
        the credentialed proxy lands in the JSON report redacted (host:port
        kept), and the caption fetch remains the verdict."""
        from ytt.canary import run_once

        settings = _make_fetch_settings(proxy_url=_PROXY)
        factory, _ = _capturing_ydl([_make_info()])
        probe_mock = MagicMock(side_effect=httpx.ConnectError(_DIRTY_PROXY_ERR))

        with patch("ytt.config.get_settings", return_value=settings):
            with patch("ytt.selftest.probe_egress", probe_mock):
                with patch("yt_dlp.YoutubeDL", side_effect=factory):
                    report = run_once()

        egress = report["egress"]
        _assert_credential_free(egress["error"])
        assert _PROXY_HOSTPORT in egress["error"]
        assert egress["via_proxy"] is True
        assert report["verdict"] == "ok"  # egress failure is context, not verdict


class TestStructuredLogRedaction:
    """The contract's "structured logs" leg, proven against RENDERED output:
    ``configure_logging()`` + ``get_logger()`` emitting the production
    log-argument shapes must produce JSON lines carrying no proxy creds.

    Drives the emissions that carry upstream exception text —
    ``_log.error("Startup egress probe failed", error=str(exc))``
    (``ytt.server``) and the ``log.warning(..., error=str(exc))`` Whisper
    cleanup paths (``ytt.whisper``) — plus bare-URL and free-text values.
    """

    def _rendered_line(self, capsys, emit) -> str:
        from ytt.observability import configure_logging, get_logger

        configure_logging()
        log = get_logger("ytt.test.proxy_redaction")
        emit(log)
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert lines, "expected at least one rendered JSON log line"
        return lines[-1]

    def test_error_argument_quoting_proxy_renders_clean(self, capsys):
        """The startup-egress-probe failure shape: ``error=str(exc)`` with a
        sentence quoting the proxy must render credential-free."""
        import json

        def emit(log):
            log.error("Startup egress probe failed", error=_DIRTY_PROXY_ERR)

        line = self._rendered_line(capsys, emit)
        _assert_credential_free(line)
        assert _PROXY_HOSTPORT in line
        event = json.loads(line)
        assert event["event"] == "Startup egress probe failed"
        assert "@" not in event["error"]

    def test_whisper_cleanup_warning_shape_renders_clean(self, capsys):
        """The ``whisper_audio_delete_failed`` shape: ``error=str(exc)`` on a
        warning must render credential-free."""
        import json

        def emit(log):
            log.warning(
                "whisper_audio_delete_failed",
                video_id="dQw4w9WgXcQ",
                error=_DIRTY_PROXY_ERR,
            )

        event = json.loads(self._rendered_line(capsys, emit))
        assert event["video_id"] == "dQw4w9WgXcQ"
        _assert_credential_free(event["error"])
        assert _PROXY_HOSTPORT in event["error"]

    def test_bare_credentialed_url_value_renders_clean(self, capsys):
        """A field whose whole value is the credentialed proxy URL."""
        import json

        def emit(log):
            log.info("egress probe target", endpoint=_PROXY)

        event = json.loads(self._rendered_line(capsys, emit))
        _assert_credential_free(event["endpoint"])
        assert _PROXY_HOSTPORT in event["endpoint"]

    def test_proxy_url_field_name_is_dropped(self, capsys):
        """``proxy_url`` is a blocked field name — value never renders."""
        import json

        def emit(log):
            log.info("egress configured", proxy_url=_PROXY)

        event = json.loads(self._rendered_line(capsys, emit))
        assert event["proxy_url"] == "<redacted>"

    def test_free_text_event_quoting_proxy_renders_clean(self, capsys):
        """The canary logs its (pre-sanitized) probe error as the event text;
        a regression that skips sanitization still must not render creds."""
        import json

        def emit(log):
            log.error(
                f"Canary probe error: {_DIRTY_PROXY_ERR} (outcome=empty_body)"
            )

        event = json.loads(self._rendered_line(capsys, emit))
        _assert_credential_free(event["event"])
        assert _PROXY_HOSTPORT in event["event"]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

class TestCliViaProxy:
    def test_once_via_proxy_forwarded(self):
        from ytt.cli import main as cli_main

        with patch("ytt.canary.run_once") as run_once_mock:
            run_once_mock.return_value = {"verdict": "ok", "caption_fetch": {"outcome": "ok"}}
            cli_main(["canary", "--once", "--via-proxy"])
        run_once_mock.assert_called_once_with(video_id=None, via_proxy=True)

    def test_via_proxy_without_once_is_a_usage_error(self):
        from ytt.cli import main as cli_main

        with pytest.raises(SystemExit) as excinfo:
            cli_main(["canary", "--via-proxy"])
        assert excinfo.value.code == 2
