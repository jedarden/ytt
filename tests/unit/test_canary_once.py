"""Unit tests for the one-shot canary (``ytt canary --once``) and the
fixed-video coverage set it shares with the probe loop.

The one-shot mode is the lightweight Proof-Obligation canary (plan §Proof
Obligations: residential egress): one known-good video, one caption fetch,
verdict ``ok`` or a stable ``ytt.errors`` error code.  The per-cycle probe
(``_probe_all_once``) fetches **every** video in the fixed internal list
for the loop (bead ytt-1b1c6ac4 — a coverage set, not a stop-at-first-
success ladder); its per-path metric recording is tested in
``tests/unit/test_canary_monitoring.py``.  All network I/O is mocked —
these tests never touch YouTube or ipinfo.io (real-network paths are
integration-gated).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yt_dlp

from ytt.canary import (
    CANARY_VIDEO_IDS,
    _probe_all_once,
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
# _probe_all_once (the loop's per-path probe: fetch EVERY configured video
# each cycle, report one probe_once_detail-shaped dict per video)
# ---------------------------------------------------------------------------


def _detail(ok: bool, outcome: str | None = None) -> dict:
    """A ``probe_once_detail``-shaped report for scripting probe outcomes."""
    return {
        "ok": ok,
        "outcome": outcome or ("ok" if ok else "ip_blocked"),
        "langs": ["en"] if ok else [],
        "duration_sec": 0.5,
        "via_proxy": False,
        "error": None if ok else "probe failed",
    }


class TestProbeAllOnce:
    def test_probes_every_video_even_when_the_first_succeeds(self):
        """THE blind-spot pin (bead ytt-1b1c6ac4): under the old ladder the
        second video was probed only when the first failed, so a caption
        regression confined to it had zero ongoing coverage.  Every
        configured video is now fetched each cycle regardless of the
        first one's outcome."""
        with patch(
            "ytt.canary.probe_once_detail",
            side_effect=[_detail(True), _detail(False, "empty_body")],
        ) as probe:
            results = _probe_all_once()
        assert probe.call_args_list == [
            call(CANARY_VIDEO_IDS[0], proxy=None),
            call(CANARY_VIDEO_IDS[1], proxy=None),
        ]
        assert [r["video_id"] for r in results] == list(CANARY_VIDEO_IDS)
        assert [r["ok"] for r in results] == [True, False]
        assert results[1]["outcome"] == "empty_body"

    def test_probes_in_configured_order(self):
        """The per-video reports come back in ``CANARY_VIDEO_IDS`` order —
        the logs and the counter increments read in the same order the
        list is documented in."""
        reports = [_detail(True), _detail(False, "rate_limited")]
        with patch(
            "ytt.canary.probe_once_detail", side_effect=reports
        ) as probe:
            results = _probe_all_once()
        assert probe.call_args_list == [
            call(video_id, proxy=None) for video_id in CANARY_VIDEO_IDS
        ]
        assert [r["outcome"] for r in results] == ["ok", "rate_limited"]

    def test_walks_every_video_when_every_video_fails(self):
        outcomes = [_detail(False, "ip_blocked")] * len(CANARY_VIDEO_IDS)
        with patch(
            "ytt.canary.probe_once_detail", side_effect=outcomes
        ) as probe:
            results = _probe_all_once()
        assert len(results) == len(CANARY_VIDEO_IDS)
        assert all(not r["ok"] for r in results)
        assert probe.call_count == len(CANARY_VIDEO_IDS)

    def test_proxy_is_passed_through_to_every_probe(self):
        """The probe measures the path it was asked to measure — a proxy
        argument that silently dropped would probe direct twice."""
        with patch(
            "ytt.canary.probe_once_detail",
            side_effect=[_detail(True)] * len(CANARY_VIDEO_IDS),
        ) as probe:
            results = _probe_all_once(proxy="http://proxy.example:1")
        assert probe.call_args_list == [
            call(video_id, proxy="http://proxy.example:1")
            for video_id in CANARY_VIDEO_IDS
        ]
        assert all(r["ok"] for r in results)

    def test_returns_one_report_per_video_with_video_id_attached(self):
        """Each report is its own ``probe_once_detail``-shaped dict (schema
        shared with the one-shot path) with ``video_id`` added — the loop's
        metrics and logs key off both."""
        with patch(
            "ytt.canary.probe_once_detail",
            side_effect=[_detail(True), _detail(False)],
        ):
            results = _probe_all_once()
        assert len(results) == len(CANARY_VIDEO_IDS)
        for result in results:
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
# Fallback-video behavior — the fixed internal video set
# ---------------------------------------------------------------------------

class TestFallbackVideo:
    """The fixed internal video list is a coverage set (plan §Canary), not a
    stop-at-first-success ladder (bead ytt-1b1c6ac4): every entry is probed
    each cycle, in order.  The per-cycle walk itself is pinned by
    ``TestProbeAllOnce`` above; how the probe loop folds the per-video
    results into metrics is pinned by ``tests/unit/test_canary_monitoring.py``."""

    def test_ladder_is_nonempty_and_wellformed(self):
        # At least two entries, or there is no fallback to speak of; every
        # entry must be a well-formed 11-char YouTube video ID.
        assert len(CANARY_VIDEO_IDS) >= 2
        for video_id in CANARY_VIDEO_IDS:
            assert re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id), video_id

    def test_once_defaults_to_the_first_ladder_entry(self):
        assert _run_once_ok()["video_id"] == CANARY_VIDEO_IDS[0]


# ---------------------------------------------------------------------------
# README ↔ code drift — the documented `ytt canary --once` verdict vocabulary
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
CONFIG_GUIDE = REPO_ROOT / "docs" / "usage" / "configuration.md"
EVIDENCE_SPEC = REPO_ROOT / "docs" / "notes" / "canary-gate-evidence.md"


def _canary_outcome_codes() -> set[str]:
    """Every outcome the one-shot canary can report as ``verdict``.

    ``"ok"`` on a caption-bearing fetch; otherwise the range of
    ``ytt.fetch.classify_ydl_error`` — the seed-map codes plus its
    ``empty_body`` fallback, which is also the literal
    ``probe_once_detail`` returns for a caption-less known-good video.
    This is the set the README's verdict documentation must stay inside.
    """
    from ytt import errors
    from ytt.fetch import SEED_MAP

    return {"ok"} | {code for _, code in SEED_MAP} | {errors.EMPTY_BODY}


@pytest.fixture(scope="module")
def once_usage_block() -> str:
    """README's fenced ``ytt canary --once`` usage block (TestReadmeVerdictDoc)."""
    text = README.read_text(encoding="utf-8")
    for block in re.findall(r"^```[^\n]*\n(.*?)^```", text, re.S | re.M):
        if "ytt canary --once" in block:
            return block
    raise AssertionError("README.md lost its `ytt canary --once` usage block")


def _usage_comments(block: str) -> str:
    """The block's bash comment lines joined into one prose string."""
    return " ".join(
        line.split("#", 1)[1].strip()
        for line in block.strip().splitlines()
        if "#" in line
    )


def _verdict_clause(block: str) -> str:
    match = re.search(r"verdict (.*?), exit", _usage_comments(block))
    assert match, "README's --once comment no longer states a verdict vocabulary"
    return match.group(1)


class TestReadmeVerdictDoc:
    """The README's ``ytt canary --once`` comment is the first thing a
    self-hoster reads about the one-shot canary, and it historically
    documented a two-value vocabulary — ``verdict "ok" vs "ip_blocked"`` —
    while the implementation reports ``"ok"`` or any stable ``ytt.errors``
    error code (bead ytt-066781cf; ``docs/notes/canary-gate-evidence.md`` §2
    already defined ``caption_fetch.outcome`` that way).  Same shape as
    ``TestEvidenceSpecDoc``: the doc and the code rot together or not at
    all.  These legs pin the reconciled wording from both sides — no
    invented code, no relapse into a closed pair, and the taxonomy claim
    the README now makes stays true of the implementation."""

    def test_documented_examples_are_real_outcome_codes(self, once_usage_block):
        """Every verdict value the README names must be one the canary can
        actually report — a renamed seed-map code or a wishful example
        fails here instead of misdirecting an operator mid-diagnosis."""
        clause = _verdict_clause(once_usage_block)
        examples = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", clause))
        assert examples, "README's verdict clause lost its example codes"
        for example in examples:
            assert example in _canary_outcome_codes(), (
                f"README documents verdict {example!r}, which no canary path "
                f"can report (real outcomes: {sorted(_canary_outcome_codes())})"
            )

    def test_headline_verdicts_stay_documented(self, once_usage_block):
        """Reconciling the vocabulary must not drop the two verdicts the
        command exists for: ``"ok"`` — the pass — and ``ip_blocked`` — the
        egress failure this canary is the proof against."""
        clause = _verdict_clause(once_usage_block)
        assert '"ok"' in clause
        assert "ip_blocked" in clause

    def test_vocabulary_is_the_error_taxonomy_not_a_closed_pair(self, once_usage_block):
        """The documented set must be presented as open-ended — anchored on
        ``"ok" or a stable ytt.errors error code`` with an elided example
        list — because ``SEED_MAP`` is pinned to a yt-dlp version and grows
        with it (fetch.py: "update on version bumps").  A closed list here
        goes stale the moment a code is added; "ok vs X" phrasing is the
        two-value relapse this class exists to keep dead."""
        clause = _verdict_clause(once_usage_block)
        assert "ytt.errors" in clause, (
            "README's verdict clause must point at the ytt.errors taxonomy "
            "as the source of truth"
        )
        assert "error code" in clause
        assert "…" in clause, "README's example list must stay open-ended (…)"
        assert '"ok" vs' not in clause

    def test_two_value_vocabulary_gone_from_the_readme(self):
        """The stale pair must not merely move house: no README line may
        still document the --once verdict as ``"ok" vs "ip_blocked"``."""
        text = README.read_text(encoding="utf-8")
        assert '"ok" vs "ip_blocked"' not in text
        assert "ok vs ip_blocked" not in text

    def test_gate_error_is_not_documented_as_an_once_outcome(self, once_usage_block):
        """``gate_error`` is synthesized by the gate when a probe *crashes*
        (``canary_gate.GATE_ERROR``); ``--once`` has no such path — an
        unexpected crash is a traceback, not a verdict.  It must not creep
        into the --once vocabulary."""
        from ytt.canary_gate import GATE_ERROR

        assert GATE_ERROR not in _canary_outcome_codes()
        assert GATE_ERROR not in once_usage_block

    def test_every_real_outcome_is_a_stable_taxonomy_code(self):
        """``a stable ytt.errors error code`` is now a claim about the
        implementation: every non-ok outcome the canary can report must be
        a ``ytt.errors`` taxonomy constant, not an ad-hoc string —
        otherwise the README's wording (and the evidence spec's) goes false
        without either document changing."""
        from ytt import errors

        taxonomy = {
            value
            for name, value in vars(errors).items()
            if name.isupper() and isinstance(value, str)
        }
        assert _canary_outcome_codes() - {"ok"} <= taxonomy

    def test_vocabulary_agrees_with_the_evidence_spec(self):
        """The evidence spec (§2) defines the same field one level down —
        ``caption_fetch.outcome`` — and its definition is what this bead
        reconciled the README against: the two documents must keep stating
        the same vocabulary."""
        spec = EVIDENCE_SPEC.read_text(encoding="utf-8")
        assert "`\"ok\"` or a stable `ytt.errors` error code" in spec

    def test_exit_contract_documented_matches_the_cli(self, once_usage_block):
        """The exit rule hangs off ``verdict == "ok"`` alone — any non-ok
        outcome exits 1 (pinned behaviorally by ``TestReportSchema``'s CLI
        tests).  The README's phrasing must keep naming ``"ok"`` as the
        sole passing verdict, and the CLI's own summary must state the
        same rule."""
        from ytt.cli import _run_canary_once

        assert 'exit 0 iff "ok"' in _usage_comments(once_usage_block)
        assert "exit 0 iff verdict is ok" in (_run_canary_once.__doc__ or "")


@pytest.fixture(scope="module")
def config_guide_canary_paragraph() -> str:
    """configuration.md's canary paragraph (probe loop + ``--once``).

    Its presence is part of the contract: without it the one-shot egress
    check is undocumented in the full configuration reference.
    """
    text = CONFIG_GUIDE.read_text(encoding="utf-8")
    m = re.search(r"The long-running probe loop .*?(?=\n\n)", text, re.DOTALL)
    assert m, (
        "configuration.md lost its canary paragraph — the one-shot "
        "(`ytt canary --once`) and loop (`ytt canary`) probes are "
        "undocumented in the configuration reference"
    )
    return m.group(0)


def _guide_verdict_clause(paragraph: str) -> str:
    """The paragraph's ``verdict`` clause — the span describing what the
    JSON report's ``verdict`` field can hold (the guide-side twin of the
    README's ``_verdict_clause``)."""
    m = re.search(r"verdict`:\s*(.*?) plus the ipinfo", paragraph, re.DOTALL)
    assert m, "configuration.md's canary paragraph no longer states a verdict vocabulary"
    return m.group(1)


class TestConfigGuideVerdictDoc:
    """``docs/usage/configuration.md``'s canary paragraph restates the same
    ``--once`` verdict contract the README's usage block does, and it was
    missed when bead ytt-066781cf reconciled the closed ``ok`` vs
    ``ip_blocked`` pair out of the README — the guide went on documenting
    the stale two-value vocabulary next to a README the suite already held
    to the taxonomy.  Same shape as ``TestReadmeVerdictDoc``: the guide's
    paragraph and the code rot together or not at all, and the closed pair
    must not merely move house between the two documents."""

    def test_documented_examples_are_real_outcome_codes(
        self, config_guide_canary_paragraph
    ):
        """Every verdict value the paragraph names must be one the canary
        can actually report — a renamed seed-map code or a wishful example
        fails here instead of misdirecting an operator mid-diagnosis."""
        clause = _guide_verdict_clause(config_guide_canary_paragraph)
        examples = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", clause))
        assert examples, "configuration.md's verdict clause lost its example codes"
        for example in examples:
            assert example in _canary_outcome_codes(), (
                f"configuration.md documents verdict {example!r}, which no "
                f"canary path can report (real outcomes: "
                f"{sorted(_canary_outcome_codes())})"
            )

    def test_headline_verdicts_stay_documented(self, config_guide_canary_paragraph):
        """Reconciling the vocabulary must not drop the two verdicts the
        command exists for: ``ok`` — the pass — and ``ip_blocked`` — the
        egress failure this canary is the proof against."""
        clause = _guide_verdict_clause(config_guide_canary_paragraph)
        assert "`ok`" in clause
        assert "ip_blocked" in clause

    def test_vocabulary_is_the_error_taxonomy_not_a_closed_pair(
        self, config_guide_canary_paragraph
    ):
        """The paragraph must present the set as open-ended — anchored on
        ``ok`` or a stable ``ytt.errors`` error code with an elided example
        list — because ``SEED_MAP`` grows with the pinned yt-dlp version;
        "ok vs X" phrasing is the two-value relapse
        ``TestReadmeVerdictDoc`` keeps dead in the README."""
        clause = _guide_verdict_clause(config_guide_canary_paragraph)
        assert "ytt.errors" in clause, (
            "configuration.md's verdict clause must point at the "
            "ytt.errors taxonomy as the source of truth"
        )
        assert "error code" in clause
        assert "…" in clause, "configuration.md's example list must stay open-ended (…)"
        assert "`ok` vs" not in clause

    def test_two_value_vocabulary_gone_from_the_guide(self):
        """The stale pair must not merely move house: no configuration.md
        line may still document the --once verdict as ``ok`` vs
        ``ip_blocked`` — in either the backticked or the bare form."""
        text = CONFIG_GUIDE.read_text(encoding="utf-8")
        assert "`ok` vs" not in text
        assert '"ok" vs' not in text
        assert "ok vs ip_blocked" not in text

    def test_gate_error_is_not_documented_as_an_once_outcome(
        self, config_guide_canary_paragraph
    ):
        """``gate_error`` is synthesized by the gate when a probe *crashes*
        (``canary_gate.GATE_ERROR``); ``--once`` has no such path — an
        unexpected crash is a traceback, not a verdict.  It must not creep
        into the guide's --once vocabulary either."""
        from ytt.canary_gate import GATE_ERROR

        assert GATE_ERROR not in config_guide_canary_paragraph

    def test_exit_contract_documented_matches_the_cli(
        self, config_guide_canary_paragraph
    ):
        """The exit rule hangs off ``verdict == "ok"`` alone — any non-ok
        outcome exits 1.  The guide's phrasing must name ``ok`` as the sole
        passing verdict; the ambiguous ``exits 0/1`` it replaced could read
        as one exit code per verdict value."""
        assert re.search(
            r"exits 0 iff .*?`ok`", config_guide_canary_paragraph, re.DOTALL
        ), (
            "configuration.md's canary paragraph must state the exit rule "
            "as 'exits 0 iff the verdict is ok' (any non-ok outcome exits 1)"
        )
        assert "exits 0/1" not in config_guide_canary_paragraph
