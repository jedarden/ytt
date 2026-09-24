"""Unit tests for the post-deploy canary acceptance gate (``ytt canary --gate``).

The gate is the release gate after an image or egress change (bead
ytt-026fdbb4, ``deploy/RUNBOOK.md`` §3): direct probe + via-proxy probe when
a proxy is configured, pass only on ``outcome=ok`` from every probe, JSON
evidence retained, rollback/escalation directive on failure.  All network
I/O is mocked (``run_once`` itself); evidence writes go to ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ytt.canary_gate import (
    DEFAULT_EVIDENCE_DIR,
    GATE_ERROR,
    remediation_for,
    run_gate,
)
from ytt.cli import main as cli_main

# ---------------------------------------------------------------------------
# Fixtures — run_once-shaped probe reports (the schema test_canary_once pins)
# ---------------------------------------------------------------------------

_REPORT_KEYS = {"mode", "ran_at", "video_id", "egress", "caption_fetch", "verdict"}


def _once_report(verdict: str = "ok", *, via_proxy: bool = False) -> dict:
    ok = verdict == "ok"
    return {
        "mode": "once",
        "ran_at": "2026-09-24T18:00:00+00:00",
        "video_id": "jNQXAC9IVRw",
        "egress": {"is_residential": True, "via_proxy": via_proxy},
        "caption_fetch": {
            "ok": ok,
            "outcome": verdict,
            "langs": ["en"] if ok else [],
            "duration_sec": 0.5,
            "via_proxy": via_proxy,
            "error": None if ok else "probe failed",
        },
        "verdict": verdict,
    }


def _settings(proxy_url: str | None) -> MagicMock:
    settings = MagicMock()
    settings.proxy_url = proxy_url
    return settings


def _gate(
    *,
    proxy_url: str | None = None,
    direct: dict | Exception | None = None,
    via_proxy: dict | Exception | None = None,
    video_id: str | None = None,
    evidence_dir=None,
) -> dict:
    """Run the gate with scripted probe outcomes, in probe order."""
    outcomes = [direct if direct is not None else _once_report()]
    if proxy_url is not None:
        outcomes.append(via_proxy if via_proxy is not None else _once_report(via_proxy=True))
    side_effect = [
        outcome if isinstance(outcome, Exception) else dict(outcome)
        for outcome in outcomes
    ]

    calls: list[dict] = []

    def recording_run_once(*, video_id=None, via_proxy=False):
        calls.append({"video_id": video_id, "via_proxy": via_proxy})
        outcome = side_effect.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with (
        patch("ytt.config.get_settings", return_value=_settings(proxy_url)),
        patch("ytt.canary_gate.run_once", side_effect=recording_run_once),
    ):
        report = run_gate(video_id=video_id, evidence_dir=evidence_dir)
    report["_calls"] = calls  # popped again by the assertion helpers
    return report


# ---------------------------------------------------------------------------
# run_gate — probe selection and pass/fail
# ---------------------------------------------------------------------------

class TestProbeSelection:
    def test_proxy_configured_runs_both_probes(self, tmp_path):
        report = _gate(proxy_url="http://proxy:3128", evidence_dir=tmp_path)
        assert [(c["via_proxy"]) for c in report.pop("_calls")] == [False, True]
        assert set(report["probes"]) == {"direct", "via_proxy"}

    def test_without_proxy_only_the_direct_probe_runs(self, tmp_path):
        report = _gate(proxy_url=None, evidence_dir=tmp_path)
        assert [c["via_proxy"] for c in report.pop("_calls")] == [False]
        assert set(report["probes"]) == {"direct"}

    def test_video_id_forwarded_to_every_probe(self, tmp_path):
        report = _gate(
            proxy_url="http://proxy:3128",
            video_id="dQw4w9WgXcQ",
            evidence_dir=tmp_path,
        )
        assert {c["video_id"] for c in report.pop("_calls")} == {"dQw4w9WgXcQ"}
        assert report["video_id"] == "dQw4w9WgXcQ"

    def test_pass_requires_ok_on_every_probe(self, tmp_path):
        """The gate's whole point: a pass is only ever `outcome=ok` everywhere."""
        report = _gate(
            proxy_url="http://proxy:3128",
            direct=_once_report("ok"),
            via_proxy=_once_report("ok", via_proxy=True),
            evidence_dir=tmp_path,
        )
        report.pop("_calls")
        assert report["gate"] == "pass"
        assert report["verdict"] == "ok"
        assert report["failed_probe"] is None
        assert report["remediation"] is None

    def test_via_proxy_failure_fails_the_gate(self, tmp_path):
        report = _gate(
            proxy_url="http://proxy:3128",
            direct=_once_report("ok"),
            via_proxy=_once_report("ip_blocked", via_proxy=True),
            evidence_dir=tmp_path,
        )
        report.pop("_calls")
        assert report["gate"] == "fail"
        assert report["verdict"] == "ip_blocked"
        assert report["failed_probe"] == "via_proxy"

    def test_direct_failure_wins_when_both_probes_fail(self, tmp_path):
        """Verdict is the first failing probe in run order — deterministic."""
        report = _gate(
            proxy_url="http://proxy:3128",
            direct=_once_report("empty_body"),
            via_proxy=_once_report("ip_blocked", via_proxy=True),
            evidence_dir=tmp_path,
        )
        report.pop("_calls")
        assert report["failed_probe"] == "direct"
        assert report["verdict"] == "empty_body"


# ---------------------------------------------------------------------------
# run_gate — report contract
# ---------------------------------------------------------------------------

class TestGateReportSchema:
    def test_top_level_schema_is_stable(self, tmp_path):
        report = _gate(proxy_url=None, evidence_dir=tmp_path)
        report.pop("_calls")
        assert set(report) == {
            "mode", "ran_at", "video_id", "proxy_configured",
            "probes", "verdict", "gate", "failed_probe",
            "remediation", "evidence_file",
        }

    def test_probe_reports_keep_the_run_once_shape(self, tmp_path):
        report = _gate(proxy_url="http://proxy:3128", evidence_dir=tmp_path)
        report.pop("_calls")
        for probe in report["probes"].values():
            assert set(probe) == _REPORT_KEYS

    def test_report_round_trips_through_json_losslessly(self, tmp_path):
        """The evidence file and stdout copy must be byte-equivalent JSON."""
        for kwargs in (
            {"proxy_url": None},
            {"proxy_url": "http://proxy:3128", "via_proxy": _once_report("ip_blocked", via_proxy=True)},
        ):
            report = _gate(evidence_dir=tmp_path / "e", **kwargs)
            report.pop("_calls")
            assert json.loads(json.dumps(report)) == report
            on_disk = json.loads(Path(report["evidence_file"]).read_text())
            assert on_disk == report


# ---------------------------------------------------------------------------
# gate_error — a crashing probe must not look like a passing one
# ---------------------------------------------------------------------------

class TestGateError:
    def test_run_once_crash_becomes_gate_error_not_a_pass(self, tmp_path):
        report = _gate(direct=RuntimeError("boom"), evidence_dir=tmp_path)
        report.pop("_calls")
        assert report["gate"] == "fail"
        assert report["verdict"] == GATE_ERROR
        assert report["probes"]["direct"]["caption_fetch"]["outcome"] == GATE_ERROR
        assert report["probes"]["direct"]["caption_fetch"]["ok"] is False

    def test_crash_error_string_is_credential_redacted(self, tmp_path):
        report = _gate(
            proxy_url="http://alice:s3cret@proxy:3128",
            via_proxy=RuntimeError("connect failed http://alice:s3cret@proxy:3128"),
            evidence_dir=tmp_path,
        )
        report.pop("_calls")
        assert "s3cret" not in json.dumps(report)
        assert "alice" not in json.dumps(report)


# ---------------------------------------------------------------------------
# Evidence retention
# ---------------------------------------------------------------------------

class TestEvidence:
    def test_evidence_file_written_and_recorded(self, tmp_path):
        report = _gate(evidence_dir=tmp_path)
        report.pop("_calls")
        path = Path(report["evidence_file"])
        assert path.parent == tmp_path
        assert path.name.startswith("ytt-canary-gate-")
        assert path.name.endswith(".json")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["gate"] == "pass"
        assert on_disk["evidence_file"] == report["evidence_file"]

    def test_default_evidence_dir_is_tmp(self, tmp_path, monkeypatch):
        """No --evidence-dir → /tmp/ytt-canary-evidence (writable everywhere
        the gate runs); HOME-independent."""
        monkeypatch.setenv("HOME", str(tmp_path))
        with (
            patch("ytt.config.get_settings", return_value=_settings(None)),
            patch("ytt.canary_gate.run_once", return_value=_once_report()),
        ):
            report = run_gate()
        assert Path(report["evidence_file"]).parent == Path(DEFAULT_EVIDENCE_DIR)

    def test_evidence_dir_is_created_when_missing(self, tmp_path):
        nested = tmp_path / "a" / "b"
        report = _gate(evidence_dir=nested)
        report.pop("_calls")
        assert Path(report["evidence_file"]).is_file()

    def test_write_failure_degrades_without_flipping_the_verdict(self, tmp_path):
        """A read-only evidence location must not sink a green release —
        the stdout copy is still evidence (RUNBOOK §3)."""
        blocker = tmp_path / "blocker"
        blocker.write_text("a regular file", encoding="utf-8")
        report = _gate(evidence_dir=blocker / "sub")
        report.pop("_calls")
        assert report["gate"] == "pass"
        assert report["evidence_file"] is None
        assert "evidence_error" in report


# ---------------------------------------------------------------------------
# Remediation — the rollback/escalation decision table (RUNBOOK §3.1)
# ---------------------------------------------------------------------------

class TestRemediation:
    @pytest.mark.parametrize(
        ("failed_probe", "outcome", "proxy_configured", "must_mention"),
        [
            (
                "via_proxy", "ip_blocked", True,
                ["proxy", "escalate", "YTT_PROXY_URL", "RUNBOOK"],
            ),
            (
                "direct", "ip_blocked", True,
                ["degrade", "rollback will not fix", "escalate"],
            ),
            (
                "direct", "ip_blocked", False,
                ["revert the tag pin", "RUNBOOK", "escalate"],
            ),
            (
                "direct", "empty_body", False,
                ["non-egress outcome", "revert the tag pin", "escalate"],
            ),
            (
                "via_proxy", "empty_body", True,
                ["non-egress outcome", "revert the tag pin"],
            ),
            (
                "direct", GATE_ERROR, False,
                ["not an egress verdict", "re-run"],
            ),
        ],
    )
    def test_directive_names_the_concrete_action(
        self, failed_probe, outcome, proxy_configured, must_mention
    ):
        text = remediation_for(failed_probe, outcome, proxy_configured=proxy_configured)
        for phrase in must_mention:
            assert phrase.lower() in text.lower(), (
                f"{failed_probe}/{outcome}: missing {phrase!r}"
            )

    @pytest.mark.parametrize(
        "args",
        [
            ("via_proxy", "ip_blocked", True),
            ("direct", "ip_blocked", True),
            ("direct", "ip_blocked", False),
            ("direct", "empty_body", False),
            ("via_proxy", "rate_limited", True),
            ("direct", GATE_ERROR, True),
        ],
    )
    def test_every_directive_points_at_evidence_or_a_rerun(self, args):
        text = remediation_for(args[0], args[1], proxy_configured=args[2])
        assert "evidence JSON" in text or "re-run" in text

    def test_pass_carries_no_directive(self, tmp_path):
        report = _gate(evidence_dir=tmp_path)
        report.pop("_calls")
        assert report["remediation"] is None


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

_PASS_REPORT = {
    "mode": "gate", "ran_at": "2026-09-24T18:00:00+00:00",
    "video_id": "jNQXAC9IVRw", "proxy_configured": False,
    "probes": {"direct": _once_report()},
    "verdict": "ok", "gate": "pass", "failed_probe": None,
    "remediation": None, "evidence_file": "/tmp/ytt-canary-evidence/x.json",
}

_FAIL_REPORT = {
    "mode": "gate", "ran_at": "2026-09-24T18:00:00+00:00",
    "video_id": "jNQXAC9IVRw", "proxy_configured": True,
    "probes": {
        "direct": _once_report("ok"),
        "via_proxy": _once_report("ip_blocked", via_proxy=True),
    },
    "verdict": "ip_blocked", "gate": "fail", "failed_probe": "via_proxy",
    "remediation": "escalate to the proxy/egress owner",
    "evidence_file": "/tmp/ytt-canary-evidence/x.json",
}


class TestCli:
    def test_gate_pass_prints_json_exits_zero(self, capsys, tmp_path):
        with patch("ytt.canary_gate.run_gate", return_value=_PASS_REPORT) as gate:
            code = cli_main(["canary", "--gate", "--evidence-dir", str(tmp_path)])
        assert code == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["gate"] == "pass"
        assert captured.err == ""
        gate.assert_called_once_with(video_id=None, evidence_dir=str(tmp_path))

    def test_gate_fail_exits_one_and_prints_the_directive(self, capsys):
        with patch("ytt.canary_gate.run_gate", return_value=_FAIL_REPORT):
            code = cli_main(["canary", "--gate"])
        assert code == 1
        captured = capsys.readouterr()
        assert json.loads(captured.out)["verdict"] == "ip_blocked"
        assert "CANARY GATE FAILED" in captured.err
        assert "ip_blocked" in captured.err
        assert "escalate to the proxy/egress owner" in captured.err

    def test_gate_evidence_dir_defaults_to_none(self, capsys):
        with patch("ytt.canary_gate.run_gate", return_value=_PASS_REPORT) as gate:
            cli_main(["canary", "--gate"])
        gate.assert_called_once_with(video_id=None, evidence_dir=None)

    def test_gate_forwards_video_id(self, capsys):
        with patch("ytt.canary_gate.run_gate", return_value=_PASS_REPORT) as gate:
            cli_main(["canary", "--gate", "--video-id", "dQw4w9WgXcQ"])
        gate.assert_called_once_with(video_id="dQw4w9WgXcQ", evidence_dir=None)

    def test_gate_and_once_are_mutually_exclusive(self):
        with pytest.raises(SystemExit) as excinfo:
            cli_main(["canary", "--gate", "--once"])
        assert excinfo.value.code == 2

    def test_gate_rejects_explicit_via_proxy(self):
        """--via-proxy with --gate would be a no-op at best and a lie at
        worst (the gate derives the probe set from YTT_PROXY_URL itself)."""
        with pytest.raises(SystemExit) as excinfo:
            cli_main(["canary", "--gate", "--via-proxy"])
        assert excinfo.value.code == 2

    def test_evidence_dir_requires_gate(self):
        with pytest.raises(SystemExit) as excinfo:
            cli_main(["canary", "--once", "--evidence-dir", "/tmp/x"])
        assert excinfo.value.code == 2

    def test_video_id_requires_once_or_gate(self):
        with pytest.raises(SystemExit) as excinfo:
            cli_main(["canary", "--video-id", "x"])
        assert excinfo.value.code == 2
