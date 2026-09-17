"""Unit tests for the one-shot canary (``ytt canary --once``).

The one-shot mode is the lightweight Proof-Obligation canary (plan §Proof
Obligations: residential egress): one known-good video, one caption fetch,
verdict ``ok`` vs ``ip_blocked``.  All network I/O is mocked — these tests
never touch YouTube or ipinfo.io (real-network paths are integration-gated).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from ytt.canary import _probe_one, probe_once_detail, run_once
from ytt.cli import main as cli_main
from ytt.models import EgressReport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ydl_returning(info: dict) -> MagicMock:
    """Mock yt_dlp.YoutubeDL context manager returning ``info`` (fetch-test pattern)."""
    ydl = MagicMock()
    ydl.extract_info.return_value = info
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ydl)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _ydl_raising(exc: Exception) -> MagicMock:
    """Mock yt_dlp.YoutubeDL context manager whose extract_info raises ``exc``."""
    ydl = MagicMock()
    ydl.extract_info.side_effect = exc
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ydl)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _caption_info() -> dict:
    return {
        "subtitles": {"en": [{"ext": "json3", "url": "https://example.test/en.json3"}]},
        "automatic_captions": {"en.auto": [{"ext": "json3", "url": "https://x.test/a"}]},
    }


# ---------------------------------------------------------------------------
# probe_once_detail
# ---------------------------------------------------------------------------

class TestProbeOnceDetail:
    def test_ok_with_caption_tracks(self):
        with patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())):
            report = probe_once_detail("jNQXAC9IVRw")
        assert report["ok"] is True
        assert report["outcome"] == "ok"
        assert "en" in report["langs"]
        assert report["error"] is None
        assert report["duration_sec"] >= 0

    def test_ip_blocked_classified(self):
        ctx = _ydl_raising(
            yt_dlp.utils.DownloadError(
                "ERROR: [youtube] jNQXAC9IVRw: HTTP Error 403: Forbidden"
            )
        )
        with patch("yt_dlp.YoutubeDL", return_value=ctx):
            report = probe_once_detail("jNQXAC9IVRw")
        assert report["ok"] is False
        assert report["outcome"] == "ip_blocked"
        assert report["langs"] == []
        assert "403" in report["error"]

    def test_private_video_classified(self):
        ctx = _ydl_raising(
            yt_dlp.utils.DownloadError(
                "ERROR: [youtube] xyz: Private video. Sign in if you've been granted access"
            )
        )
        with patch("yt_dlp.YoutubeDL", return_value=ctx):
            report = probe_once_detail("xyz")
        assert report["outcome"] == "private"

    def test_no_caption_tracks_is_empty_body(self):
        with patch("yt_dlp.YoutubeDL", return_value=_ydl_returning({"subtitles": {}})):
            report = probe_once_detail("jNQXAC9IVRw")
        assert report["ok"] is False
        assert report["outcome"] == "empty_body"

    def test_uses_same_ydl_base_opts_as_fetch_path(self):
        """The canary must exercise the production fetch path (plan §Canary)."""
        from ytt.fetch import YDL_BASE_OPTS

        captured: dict = {}

        def capturing_ydl(opts):
            captured.update(opts)
            ydl = MagicMock()
            ydl.__enter__.return_value = ydl
            ydl.extract_info.return_value = _caption_info()
            return ydl

        with patch("yt_dlp.YoutubeDL", side_effect=capturing_ydl):
            probe_once_detail("jNQXAC9IVRw")

        for key, value in YDL_BASE_OPTS.items():
            assert captured.get(key) == value, f"canary dropped {key!r} from YDL opts"


# ---------------------------------------------------------------------------
# _probe_one (loop path keeps its bool contract)
# ---------------------------------------------------------------------------

class TestProbeOne:
    def test_true_on_success(self):
        with patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())):
            assert _probe_one("jNQXAC9IVRw", settings=None) is True

    def test_false_on_error(self):
        ctx = _ydl_raising(yt_dlp.utils.DownloadError("HTTP Error 403"))
        with patch("yt_dlp.YoutubeDL", return_value=ctx):
            assert _probe_one("jNQXAC9IVRw", settings=None) is False


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------

_RESIDENTIAL = EgressReport(
    ip="203.0.113.7", asn="AS7922", org="Comcast Cable",
    via_proxy=False, is_residential=True,
)


class TestRunOnce:
    def test_report_is_self_dating_utc(self):
        """The report is pasted somewhere durable as evidence — it must carry
        its own UTC timestamp."""
        from datetime import datetime, timezone

        with (
            patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
            patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())),
        ):
            report = run_once()
        stamped = datetime.fromisoformat(report["ran_at"])
        assert stamped.tzinfo is timezone.utc

    def test_verdict_ok(self):
        with (
            patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
            patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())),
        ):
            report = run_once()
        assert report["verdict"] == "ok"
        assert report["mode"] == "once"
        assert report["video_id"] == "jNQXAC9IVRw"  # first known-good video
        assert report["egress"]["is_residential"] is True
        assert report["caption_fetch"]["ok"] is True

    def test_video_id_override(self):
        with (
            patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
            patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())),
        ):
            report = run_once(video_id="dQw4w9WgXcQ")
        assert report["video_id"] == "dQw4w9WgXcQ"

    def test_ip_blocked_verdict_despite_residential_egress(self):
        """Egress classification alone proves nothing — the fetch is the verdict."""
        ctx = _ydl_raising(
            yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot")
        )
        with (
            patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
            patch("yt_dlp.YoutubeDL", return_value=ctx),
        ):
            report = run_once()
        assert report["verdict"] == "ip_blocked"
        assert report["egress"]["is_residential"] is True  # context, not verdict

    def test_egress_probe_failure_does_not_sink_the_report(self):
        """ipinfo.io being down must not mask the caption-fetch ground truth."""
        with (
            patch("ytt.selftest.probe_egress", side_effect=RuntimeError("dns fail")),
            patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())),
        ):
            report = run_once()
        assert report["verdict"] == "ok"
        assert "error" in report["egress"]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

class TestCli:
    def test_canary_once_exit_zero_and_json(self, capsys):
        with (
            patch("ytt.canary.run_once")
            as run_once_mock,
        ):
            run_once_mock.return_value = {
                "mode": "once", "video_id": "jNQXAC9IVRw",
                "egress": {"is_residential": True},
                "caption_fetch": {"ok": True, "outcome": "ok", "langs": ["en"]},
                "verdict": "ok",
            }
            code = cli_main(["canary", "--once"])
        assert code == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["verdict"] == "ok"

    def test_canary_once_exit_one_on_ip_blocked(self, capsys):
        with patch("ytt.canary.run_once") as run_once_mock:
            run_once_mock.return_value = {
                "mode": "once", "video_id": "jNQXAC9IVRw",
                "egress": {"is_residential": True},
                "caption_fetch": {"ok": False, "outcome": "ip_blocked", "langs": []},
                "verdict": "ip_blocked",
            }
            code = cli_main(["canary", "--once"])
        assert code == 1
        assert "CANARY FAILED" in capsys.readouterr().err

    def test_canary_once_video_id_forwarded(self):
        with patch("ytt.canary.run_once") as run_once_mock:
            run_once_mock.return_value = {"verdict": "ok", "video_id": "x"}
            cli_main(["canary", "--once", "--video-id", "x"])
        run_once_mock.assert_called_once_with(video_id="x", via_proxy=False)

    def test_canary_without_once_uses_loop_main(self):
        with patch("ytt.canary.main", return_value=0) as loop_main:
            assert cli_main(["canary"]) == 0
        loop_main.assert_called_once_with()

    def test_video_id_without_once_is_a_usage_error(self):
        """`--video-id` only makes sense one-shot; silently ignoring it in
        loop mode would probe the wrong video list."""
        with pytest.raises(SystemExit) as excinfo:
            cli_main(["canary", "--video-id", "x"])
        assert excinfo.value.code == 2
