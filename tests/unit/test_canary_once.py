"""Unit tests for the one-shot canary (``ytt canary --once``) and the
fallback-video ladder it shares with the probe loop.

The one-shot mode is the lightweight Proof-Obligation canary (plan §Proof
Obligations: residential egress): one known-good video, one caption fetch,
verdict ``ok`` vs ``ip_blocked``.  The ladder (``_probe_ladder_once``) walks
the fixed internal video list for the loop; its per-path metric recording is
tested in ``tests/unit/test_canary_monitoring.py``.  All network I/O is
mocked — these tests never touch YouTube or ipinfo.io (real-network paths
are integration-gated).
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, call, patch

import pytest
import yt_dlp

from ytt.canary import (
    CANARY_VIDEO_IDS,
    _probe_ladder_once,
    probe_once_detail,
    run_once,
)
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
# _probe_ladder_once (the loop's per-path probe: walk the ladder, stop at the
# first success, report the terminating probe with its video attached)
# ---------------------------------------------------------------------------


def _detail(ok: bool, outcome: str | None = None) -> dict:
    """A ``probe_once_detail``-shaped report for scripting ladder outcomes."""
    return {
        "ok": ok,
        "outcome": outcome or ("ok" if ok else "ip_blocked"),
        "langs": ["en"] if ok else [],
        "duration_sec": 0.5,
        "via_proxy": False,
        "error": None if ok else "probe failed",
    }


class TestProbeLadderOnce:
    def test_stops_at_first_success(self):
        """The fallback video is only probed when the primary fails — one
        good fetch per cycle, no wasted residential bandwidth."""
        with patch(
            "ytt.canary.probe_once_detail",
            side_effect=[_detail(False), _detail(True)],
        ) as probe:
            result = _probe_ladder_once()
        assert result["ok"] is True
        assert result["video_id"] == CANARY_VIDEO_IDS[1]
        assert probe.call_args_list == [
            call(CANARY_VIDEO_IDS[0], proxy=None),
            call(CANARY_VIDEO_IDS[1], proxy=None),
        ]

    def test_walks_full_ladder_when_every_video_fails(self):
        outcomes = [_detail(False, "ip_blocked")] * len(CANARY_VIDEO_IDS)
        with patch(
            "ytt.canary.probe_once_detail", side_effect=outcomes
        ) as probe:
            result = _probe_ladder_once()
        assert result["ok"] is False
        assert result["outcome"] == "ip_blocked"
        assert result["video_id"] == CANARY_VIDEO_IDS[-1]
        assert probe.call_count == len(CANARY_VIDEO_IDS)

    def test_proxy_is_passed_through_to_each_probe(self):
        """The ladder measures the path it was asked to measure — a proxy
        argument that silently dropped would probe direct twice."""
        with patch(
            "ytt.canary.probe_once_detail", side_effect=[_detail(True)]
        ) as probe:
            result = _probe_ladder_once(proxy="http://proxy.example:1")
        assert probe.call_args_list == [
            call(CANARY_VIDEO_IDS[0], proxy="http://proxy.example:1")
        ]
        assert result["ok"] is True

    def test_returns_the_terminating_probe_report_plus_video(self):
        """The result is the terminating probe's own report (schema shared
        with ``probe_once_detail``) with ``video_id`` added — the loop's
        metrics and logs key off both."""
        with patch(
            "ytt.canary.probe_once_detail", side_effect=[_detail(True)]
        ):
            result = _probe_ladder_once()
        assert set(result) == {"ok", "outcome", "langs", "duration_sec",
                               "via_proxy", "error", "video_id"}


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


# ---------------------------------------------------------------------------
# JSON report schema (the report is pasted somewhere durable as evidence —
# its key set is a contract, not an implementation detail)
# ---------------------------------------------------------------------------

_PROBE_KEYS = {"ok", "outcome", "langs", "duration_sec", "via_proxy", "error"}
_REPORT_KEYS = {"mode", "ran_at", "video_id", "egress", "caption_fetch", "verdict"}
_EGRESS_OK_KEYS = {"ip", "asn", "org", "via_proxy", "is_residential"}
_EGRESS_ERROR_KEYS = {"error", "via_proxy"}


def _probe_ok() -> dict:
    with patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())):
        return probe_once_detail("jNQXAC9IVRw")


def _probe_blocked() -> dict:
    ctx = _ydl_raising(
        yt_dlp.utils.DownloadError("ERROR: [youtube] jNQXAC9IVRw: HTTP Error 403: Forbidden")
    )
    with patch("yt_dlp.YoutubeDL", return_value=ctx):
        return probe_once_detail("jNQXAC9IVRw")


def _run_once_ok() -> dict:
    with (
        patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
        patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())),
    ):
        return run_once()


def _run_once_blocked() -> dict:
    ctx = _ydl_raising(
        yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot")
    )
    with (
        patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
        patch("yt_dlp.YoutubeDL", return_value=ctx),
    ):
        return run_once()


class TestReportSchema:
    def test_probe_schema_is_stable_across_outcomes(self):
        """Success and every failure shape share one schema, discriminated by
        ``ok``/``outcome`` — consumers (the CLI exit path, pasted evidence)
        never branch on missing keys."""
        assert set(_probe_ok()) == _PROBE_KEYS
        assert set(_probe_blocked()) == _PROBE_KEYS

    def test_probe_field_types(self):
        report = _probe_ok()
        assert isinstance(report["ok"], bool)
        assert isinstance(report["langs"], list)
        assert all(isinstance(lang, str) for lang in report["langs"])
        assert isinstance(report["duration_sec"], float)
        assert isinstance(report["via_proxy"], bool)
        assert report["error"] is None

    def test_run_once_top_level_schema(self):
        report = _run_once_ok()
        assert set(report) == _REPORT_KEYS
        assert report["mode"] == "once"
        assert set(report["egress"]) == _EGRESS_OK_KEYS
        assert set(report["caption_fetch"]) == _PROBE_KEYS

    def test_run_once_egress_error_schema(self):
        """The degraded-egress shape swaps the classification fields for
        ``error`` + ``via_proxy`` (no null-ip half-report)."""
        with (
            patch("ytt.selftest.probe_egress", side_effect=RuntimeError("dns fail")),
            patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())),
        ):
            report = run_once()
        assert set(report["egress"]) == _EGRESS_ERROR_KEYS

    def test_verdict_mirrors_caption_fetch_outcome(self):
        """``verdict`` is the caption fetch outcome — the exit code contract
        hangs off it, so the mirror must hold on both sides."""
        ok_report = _run_once_ok()
        assert ok_report["verdict"] == ok_report["caption_fetch"]["outcome"] == "ok"
        blocked = _run_once_blocked()
        assert blocked["verdict"] == blocked["caption_fetch"]["outcome"] == "ip_blocked"

    def test_report_round_trips_through_json_losslessly(self):
        """Pasted-as-evidence means ``json.dumps`` is lossless: no datetimes,
        no exceptions, no non-string keys leaking into the report."""
        for report in (_run_once_ok(), _run_once_blocked()):
            assert json.loads(json.dumps(report)) == report

    def test_cli_prints_the_real_report_and_exits_zero(self, capsys):
        """End to end: stdout is run_once's own report as JSON — schema,
        verdict and langs intact — and the exit code is 0 on ok."""
        with (
            patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
            patch("yt_dlp.YoutubeDL", return_value=_ydl_returning(_caption_info())),
        ):
            code = cli_main(["canary", "--once"])
        assert code == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert set(parsed) == _REPORT_KEYS
        assert parsed["verdict"] == "ok"
        assert parsed["caption_fetch"]["langs"]
        assert captured.err == ""

    def test_cli_ip_blocked_exits_one_with_failed_verdict(self, capsys):
        """End to end: a real ip_blocked fetch prints the full report, exits 1
        and says why on stderr."""
        with (
            patch("ytt.selftest.probe_egress", return_value=_RESIDENTIAL),
            patch(
                "yt_dlp.YoutubeDL",
                return_value=_ydl_raising(
                    yt_dlp.utils.DownloadError(
                        "ERROR: Sign in to confirm you're not a bot"
                    )
                ),
            ),
        ):
            code = cli_main(["canary", "--once"])
        assert code == 1
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["verdict"] == "ip_blocked"
        assert "CANARY FAILED" in captured.err
        assert "ip_blocked" in captured.err


# ---------------------------------------------------------------------------
# Fallback-video behavior — the fixed internal video ladder
# ---------------------------------------------------------------------------

class TestFallbackVideo:
    """The fixed internal video list is a fallback ladder (plan §Canary):
    probe in order, stop at the first success, and count one failure only
    when the whole ladder misses.  The walk itself is pinned by
    ``TestProbeLadderOnce`` above; how the probe loop folds the two paths
    into metrics is pinned by ``tests/unit/test_canary_monitoring.py``."""

    def test_ladder_is_nonempty_and_wellformed(self):
        # At least two entries, or there is no fallback to speak of; every
        # entry must be a well-formed 11-char YouTube video ID.
        assert len(CANARY_VIDEO_IDS) >= 2
        for video_id in CANARY_VIDEO_IDS:
            assert re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id), video_id

    def test_once_defaults_to_the_first_ladder_entry(self):
        assert _run_once_ok()["video_id"] == CANARY_VIDEO_IDS[0]
