"""Unit tests for the post-deploy canary acceptance gate (``ytt canary --gate``).

The gate is the release gate after an image or egress change (bead
ytt-026fdbb4, ``deploy/RUNBOOK.md`` §3): direct probe + via-proxy probe when
a proxy is configured, pass only on ``outcome=ok`` from every probe, JSON
evidence retained, rollback/escalation directive on failure.  All network
I/O is mocked (``run_once`` itself); evidence writes go to ``tmp_path``.

The evidence artifact's full contract — schema, destination, retention,
failure output, secret exclusion — is specified in
``docs/notes/canary-gate-evidence.md`` (bead ytt-7f576b65);
``TestEvidenceSpecDoc`` drift-guards that document against this code, so a
contract change fails here until the spec follows — and vice versa.
``TestRunbookRemediationMirror`` (bead ytt-fefb4698) pins the other recorded
mirror the same way: the rollback/escalation directives of
``remediation_for`` against ``deploy/RUNBOOK.md`` §3.1's decision table.
``TestEvidenceDurabilityDoc`` (bead ytt-958dccc1) pins spec §4's durability
contract — what survives the run environment, the release record as the
durable copy — to the operator surfaces that enact it (RUNBOOK §3 step 4,
DEPLOY-CHECKLIST §5, README).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ytt.canary_gate import (
    _EVIDENCE_FILENAME_PREFIX,
    _evidence_path,
    DEFAULT_EVIDENCE_DIR,
    GATE_ERROR,
    PROBE_ORDER,
    remediation_for,
    run_gate,
)
from ytt.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DOC = REPO_ROOT / "docs" / "notes" / "canary-gate-evidence.md"


@pytest.fixture(scope="module")
def spec() -> str:
    """The evidence-artifact spec doc, read once (TestEvidenceSpecDoc)."""
    assert SPEC_DOC.is_file(), "the evidence spec doc went missing"
    return SPEC_DOC.read_text(encoding="utf-8")

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
# Evidence durability — spec §3/§4: atomic, append-only, never overwritten
# ---------------------------------------------------------------------------

class TestEvidenceDurability:
    def test_evidence_path_never_collides(self, tmp_path):
        """Same-second runs disambiguate with -2/-3/… instead of clobbering
        the earlier artifact (spec §3 — the runbook's immediate re-run)."""
        ran = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        first = _evidence_path(tmp_path, ran)
        assert first.name == "ytt-canary-gate-20260927T120000Z.json"
        first.write_text("{}", encoding="utf-8")
        second = _evidence_path(tmp_path, ran)
        assert second.name == "ytt-canary-gate-20260927T120000Z-2.json"
        second.write_text("{}", encoding="utf-8")
        assert _evidence_path(tmp_path, ran).name.endswith("-3.json")

    def test_same_second_rerun_appends_and_keeps_the_first_artifact(self, tmp_path):
        """End to end: two gates in one second both survive, each report
        pointing at its own file."""
        frozen = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: N805 — datetime subclass idiom
                return frozen

        with patch("ytt.canary_gate.datetime", _FrozenDatetime):
            first = _gate(evidence_dir=tmp_path)
            second = _gate(evidence_dir=tmp_path)
        first.pop("_calls")
        second.pop("_calls")
        p1 = Path(first["evidence_file"])
        p2 = Path(second["evidence_file"])
        assert p1 != p2
        assert p1.name == f"{_EVIDENCE_FILENAME_PREFIX}-20260927T120000Z.json"
        assert p2.name == f"{_EVIDENCE_FILENAME_PREFIX}-20260927T120000Z-2.json"
        # The first run's artifact is byte-intact, not replaced:
        assert json.loads(p1.read_text(encoding="utf-8"))["evidence_file"] == str(p1)
        assert {p.name for p in tmp_path.iterdir()} == {p1.name, p2.name}

    def test_successful_write_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        report = _gate(evidence_dir=tmp_path)
        report.pop("_calls")
        assert [p.name for p in tmp_path.iterdir()] == [
            Path(report["evidence_file"]).name
        ]

    def test_rename_failure_leaves_no_partial_artifact(self, tmp_path):
        """A crash between write and rename must not leave a file that parses
        as evidence, and must still degrade exactly like a failed write."""
        with patch("ytt.canary_gate.os.replace", side_effect=OSError("no space")):
            report = _gate(evidence_dir=tmp_path)
        report.pop("_calls")
        assert report["gate"] == "pass"  # the verdict is never flipped
        assert report["evidence_file"] is None
        assert "evidence_error" in report
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Sensitive-configuration exclusion — spec §6, enforced at the artifact
# boundary (the gate re-scrubs what run_once already redacted)
# ---------------------------------------------------------------------------

class TestSecretExclusion:
    def test_probe_report_strings_are_scrubbed_before_retention(self, tmp_path):
        """A probe error that quotes the credentialed proxy URL is redacted
        in the retained artifact even if an upstream layer failed to — the
        host:port survives, the userinfo never does (spec §6)."""
        leaky = _once_report("ip_blocked", via_proxy=True)
        leaky["caption_fetch"]["error"] = (
            "dial http://alice:s3cret@proxy:3128 failed"
        )
        report = _gate(
            proxy_url="http://alice:s3cret@proxy:3128",
            direct=_once_report(),
            via_proxy=leaky,
            evidence_dir=tmp_path,
        )
        report.pop("_calls")
        on_disk = Path(report["evidence_file"]).read_text(encoding="utf-8")
        for secret in ("s3cret", "alice"):
            assert secret not in on_disk
        assert "http://proxy:3128 failed" in on_disk

    def test_configured_proxy_url_never_reaches_a_clean_pass_artifact(self, tmp_path):
        report = _gate(
            proxy_url="http://alice:s3cret@proxy:3128", evidence_dir=tmp_path
        )
        report.pop("_calls")
        on_disk = Path(report["evidence_file"]).read_text(encoding="utf-8")
        assert "s3cret" not in on_disk
        assert "alice" not in on_disk


# ---------------------------------------------------------------------------
# Retained evidence on failure — spec §4/§5: the artifact is self-contained
# for the release record, pass or fail
# ---------------------------------------------------------------------------

class TestRetainedEvidenceContract:
    def test_direct_only_failure_artifact_is_self_contained(self, tmp_path):
        report = _gate(
            proxy_url=None, direct=_once_report("ip_blocked"), evidence_dir=tmp_path
        )
        report.pop("_calls")
        on_disk = json.loads(Path(report["evidence_file"]).read_text("utf-8"))
        assert on_disk == report
        assert on_disk["gate"] == "fail"
        assert on_disk["failed_probe"] == "direct"
        assert on_disk["verdict"] == "ip_blocked"
        assert on_disk["remediation"]
        assert on_disk["probes"]["direct"]["caption_fetch"]["outcome"] == "ip_blocked"

    def test_proxy_failure_artifact_keeps_both_probe_results(self, tmp_path):
        report = _gate(
            proxy_url="http://proxy:3128",
            direct=_once_report("ok"),
            via_proxy=_once_report("ip_blocked", via_proxy=True),
            evidence_dir=tmp_path,
        )
        report.pop("_calls")
        on_disk = json.loads(Path(report["evidence_file"]).read_text("utf-8"))
        assert set(on_disk["probes"]) == {"direct", "via_proxy"}
        assert on_disk["probes"]["direct"]["caption_fetch"]["via_proxy"] is False
        assert on_disk["probes"]["via_proxy"]["caption_fetch"]["via_proxy"] is True

    def test_success_artifact_records_each_probe_path(self, tmp_path):
        report = _gate(proxy_url="http://proxy:3128", evidence_dir=tmp_path)
        report.pop("_calls")
        on_disk = json.loads(Path(report["evidence_file"]).read_text("utf-8"))
        assert on_disk["gate"] == "pass"
        assert on_disk["probes"]["direct"]["caption_fetch"]["ok"] is True
        assert on_disk["probes"]["via_proxy"]["caption_fetch"]["ok"] is True


# ---------------------------------------------------------------------------
# Spec doc — docs/notes/canary-gate-evidence.md ↔ code drift guard (§7)
# ---------------------------------------------------------------------------

class TestEvidenceSpecDoc:
    """The evidence contract lives in the spec doc; every key, constant and
    guarantee it claims must still be true of the code, and every key the
    code emits must still be documented there."""

    def test_spec_names_the_implementation(self, spec):
        assert "ytt/canary_gate.py" in spec
        assert "ytt-7f576b65" in spec

    def test_every_report_key_is_documented(self, spec, tmp_path):
        keys: set[str] = set()
        for kwargs in (
            {"proxy_url": None},
            {
                "proxy_url": "http://proxy:3128",
                "direct": _once_report("ok"),
                "via_proxy": _once_report("ip_blocked", via_proxy=True),
            },
        ):
            report = _gate(evidence_dir=tmp_path / "e", **kwargs)
            report.pop("_calls")
            keys |= set(report)
        assert keys >= {"verdict", "gate", "remediation", "evidence_file"}
        for key in keys:
            assert f"`{key}`" in spec, f"report key {key!r} is not in the spec doc"

    def test_conditional_and_probe_keys_are_documented(self, spec):
        assert "`evidence_error`" in spec
        for key in _REPORT_KEYS:
            assert f"`{key}`" in spec, f"probe key {key!r} is not in the spec doc"

    def test_documented_constants_match_the_code(self, spec):
        assert DEFAULT_EVIDENCE_DIR in spec
        assert f"{_EVIDENCE_FILENAME_PREFIX}-" in spec
        assert GATE_ERROR in spec
        for probe in PROBE_ORDER:
            assert f"`{probe}`" in spec
        assert "--evidence-dir" in spec
        assert "redact_credentials" in spec
        assert "_scrub_secrets" in spec

    def test_failure_output_and_retention_contract_is_documented(self, spec):
        assert "CANARY GATE FAILED" in spec  # the stderr banner, verbatim
        for phrase in ("exit code", "argparse", "never deletes", "release bead"):
            assert phrase in spec, f"spec doc lost the {phrase!r} guarantee"
        assert "RUNBOOK" in spec and "§3.1" in spec  # the remediation mirror


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
# RUNBOOK §3.1 — remediation_for's prose mirror (deploy/RUNBOOK.md ↔ code)
# ---------------------------------------------------------------------------

RUNBOOK = REPO_ROOT / "deploy" / "RUNBOOK.md"


@pytest.fixture(scope="module")
def runbook_31() -> str:
    """``deploy/RUNBOOK.md`` §3.1 — the canary-gate decision table (TestRunbookRemediationMirror)."""
    assert RUNBOOK.is_file(), "deploy/RUNBOOK.md went missing"
    text = RUNBOOK.read_text(encoding="utf-8")
    match = re.search(r"^### 3\.1 .*?(?=^## )", text, re.S | re.M)
    assert match, "deploy/RUNBOOK.md lost its §3.1 decision-table section"
    return match.group(0)


class TestRunbookRemediationMirror:
    """``deploy/RUNBOOK.md`` §3.1 is the prose twin of :func:`remediation_for`
    — the one text an operator reads mid-incident on a failed gate; drift
    there is a wrong rollback/escalation directive.  The doc condenses the
    directives to prose (it does not quote them verbatim), so the mirror is
    pinned at the level of the decision-bearing phrases: each phrase below
    must still be emitted by ``remediation_for`` *and* still legible in §3.1
    (case-insensitively — the table bolds its verbs).  An edit to either side
    that drops one fails here instead of silently diverging: update both in
    the same commit."""

    @pytest.mark.parametrize(
        ("failed_probe", "outcome", "proxy_configured", "phrases"),
        [
            # §3.1 row 1 — via_proxy / ip_blocked
            (
                "via_proxy", "ip_blocked", True,
                [
                    "the fallback egress path is broken",
                    "residential IP is burned",
                    "YTT_PROXY_URL",
                    "declarative-config",
                    "rollback will not help",
                    "to the proxy/egress owner with the evidence JSON",
                    "down for users",
                    "as an incident",
                ],
            ),
            # §3.1 row 2 — direct / ip_blocked with the proxy probe healthy
            (
                "direct", "ip_blocked", True,
                [
                    "to its proxied fallback",
                    "rollback will not fix it",
                    "to the egress owner with the evidence",
                    "whether to keep or revert the tag",
                ],
            ),
            # §3.1 row 3 — direct / ip_blocked, no proxy configured
            (
                "direct", "ip_blocked", False,
                [
                    "changed fetch code or bumped yt-dlp",
                    "declarative-config",
                    "re-gate on the previous tag",
                    "is burned",
                    "to the egress owner with the evidence",
                ],
            ),
            # §3.1 row 4 — non-egress outcome, either probe (both shown)
            (
                "direct", "empty_body", False,
                [
                    "not an egress verdict",
                    "known-good canary video",
                    "yt-dlp/extractor regression shipped in the new image",
                    "declarative-config",
                    "re-gate on the previous tag",
                    "changed no fetch code",
                    "to the maintainers with the evidence",
                    "the fixed canary video list itself may need updating",
                ],
            ),
            (
                "via_proxy", "rate_limited", True,
                [
                    "not an egress verdict",
                    "known-good canary video",
                    "re-gate on the previous tag",
                    "to the maintainers with the evidence",
                ],
            ),
            # §3.1 row 5 — gate_error, either probe (both shown)
            (
                "direct", GATE_ERROR, False,
                [
                    "gate crashed before",
                    "a canary result",
                    "fix the gate environment",
                    "re-run",
                    "with the evidence",
                    "if it persists",
                ],
            ),
            (
                "via_proxy", GATE_ERROR, True,
                [
                    "gate crashed before",
                    "a canary result",
                    "fix the gate environment",
                    "re-run",
                    "with the evidence",
                    "if it persists",
                ],
            ),
        ],
    )
    def test_runbook_31_carries_every_directive_phrase(
        self, runbook_31, failed_probe, outcome, proxy_configured, phrases
    ):
        directive = remediation_for(
            failed_probe, outcome, proxy_configured=proxy_configured
        )
        for phrase in phrases:
            assert phrase.lower() in directive.lower(), (
                f"remediation_for({failed_probe}, {outcome}) lost {phrase!r} — "
                "RUNBOOK §3.1 pins it; update the code and the runbook together"
            )
            assert phrase.lower() in runbook_31.lower(), (
                f"RUNBOOK §3.1 lost {phrase!r} — remediation_for still emits it "
                f"for ({failed_probe}, {outcome}); update both together"
            )

    def test_rerun_once_preamble_is_mirrored(self, runbook_31):
        """'Re-run the gate once before acting' leads every egress and
        non-egress directive (deliberately not the gate_error one — that is
        row 5's own fix-and-rerun) and is §3.1's stated rule above the
        table."""
        for outcome in ("ip_blocked", "empty_body", "rate_limited"):
            for probe, proxy in (("direct", False), ("via_proxy", True)):
                directive = remediation_for(probe, outcome, proxy_configured=proxy)
                assert "re-run the gate once before acting" in directive
        assert "re-run the gate once before acting" in runbook_31

    def test_section_names_the_implementation(self, runbook_31):
        """§3.1 must keep pointing back at the code it mirrors, and must keep
        naming the report fields an operator reads on a failure."""
        assert "ytt.canary_gate.remediation_for" in runbook_31
        assert "updated together" in runbook_31
        assert "`report.failed_probe`" in runbook_31
        assert "`report.verdict`" in runbook_31
        # the example non-egress outcomes §3.1 quotes stay real outcome codes
        assert "`empty_body`" in runbook_31
        assert "`rate_limited`" in runbook_31
        assert "§3.1" in (remediation_for.__doc__ or ""), (
            "remediation_for's docstring stopped pointing at RUNBOOK §3.1"
        )


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


# ---------------------------------------------------------------------------
# CLI full stack — exit codes, stdout/stderr split and the retained artifact
# through the real run_gate (only run_once is mocked) — spec §5
# ---------------------------------------------------------------------------

class TestCliFullStack:
    def test_pass_exits_zero_and_retains_the_artifact(self, capsys, tmp_path):
        with (
            patch("ytt.config.get_settings", return_value=_settings(None)),
            patch("ytt.canary_gate.run_once", return_value=_once_report()),
        ):
            code = cli_main(["canary", "--gate", "--evidence-dir", str(tmp_path)])
        assert code == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        report = json.loads(captured.out)
        assert report["gate"] == "pass"
        assert Path(report["evidence_file"]).is_file()
        # stdout and disk are the same object (spec §5: parse either):
        on_disk = json.loads(Path(report["evidence_file"]).read_text("utf-8"))
        assert on_disk == report

    def test_fail_exits_one_directs_on_stderr_and_retains_evidence(self, capsys, tmp_path):
        with (
            patch(
                "ytt.config.get_settings",
                return_value=_settings("http://alice:s3cret@proxy:3128"),
            ),
            patch(
                "ytt.canary_gate.run_once",
                side_effect=[
                    _once_report("ok"),
                    _once_report("ip_blocked", via_proxy=True),
                ],
            ),
        ):
            code = cli_main(["canary", "--gate", "--evidence-dir", str(tmp_path)])
        assert code == 1
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        assert report["gate"] == "fail"
        assert "CANARY GATE FAILED" in captured.err
        assert report["remediation"] in captured.err
        # the credentialed proxy URL is clean on every channel (spec §6):
        for channel in (captured.out, captured.err):
            assert "s3cret" not in channel
            assert "alice" not in channel
        # the failure artifact still lands and still carries the directive:
        on_disk = json.loads(Path(report["evidence_file"]).read_text("utf-8"))
        assert on_disk == report
        assert on_disk["gate"] == "fail"
        assert on_disk["remediation"]


# ---------------------------------------------------------------------------
# Durability contract — spec §4 ↔ the operator surfaces that enact it
# (RUNBOOK §3 step 4, DEPLOY-CHECKLIST §5, README) — bead ytt-958dccc1
# ---------------------------------------------------------------------------

CHECKLIST = REPO_ROOT / "deploy" / "DEPLOY-CHECKLIST.md"
README = REPO_ROOT / "README.md"

#: The capture assertion every operator surface must carry — spec §4's
#: "exists, non-empty, parses as a gate report" in executable form.
_CAPTURE_ASSERTION = (
    'if [ -n "$F" ] && [ -s "$F" ] && jq -e \'.gate\' "$F" >/dev/null; then'
)


def _collapsed(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def runbook_step3() -> str:
    """``deploy/RUNBOOK.md`` §3 up to §3.1 — the step-4 gate procedure."""
    text = RUNBOOK.read_text(encoding="utf-8")
    match = re.search(r"^## 3\. .*?(?=^### 3\.1)", text, re.S | re.M)
    assert match, "deploy/RUNBOOK.md lost its §3 post-deploy validation section"
    return " ".join(match.group(0).split())


class TestEvidenceDurabilityDoc:
    """Retention (the gate never prunes) is pinned by ``TestEvidenceSpecDoc``;
    these legs pin *durability* — spec §4's survival contract and the
    operator surfaces that enact it.  The gate cannot make its artifact
    outlive the run environment (an ephemeral CI pod deletes everything at
    completion), so the contract names the release record as the durable
    copy and turns the capture into a mandatory, asserted checklist step in
    both operator docs.  An edit that drops the assertion, the failure
    branch, the retention bound, or the README pointer fails here instead of
    silently promising evidence that no longer has a survival story."""

    def test_spec_states_the_survival_contract(self, spec):
        collapsed = " ".join(spec.split())
        for phrase in (
            "never deletes",                              # the retention bound
            "podGC",                                      # the CI counterexample
            "OnPodCompletion",
            "the release record is the durable copy",
            "grows without bound",                        # never-pruned, at any destination
            "never under `YTT_CACHE_DIR`/`YTT_SCRATCH_DIR`",
            "the cache volume is never the evidence home",
            "`deploy/RUNBOOK.md` §3 step 4",
            "`deploy/DEPLOY-CHECKLIST.md` §5",
        ):
            assert phrase in collapsed, f"spec doc lost the §4 guarantee {phrase!r}"

    def test_runbook_step4_carries_the_capture_and_assertion(self, runbook_step3):
        assert 'tee "canary-gate-$(date -u +%Y%m%dT%H%M%SZ).json"' in runbook_step3
        assert _CAPTURE_ASSERTION in runbook_step3
        assert "NO RETAINED EVIDENCE" in runbook_step3
        assert "durable copy retained" in runbook_step3
        assert "pod-lifetime retention" in runbook_step3
        assert "canary-gate-evidence.md` §4" in runbook_step3

    def test_deploy_checklist_carries_the_same_assertion(self):
        t = _collapsed(CHECKLIST)
        assert _CAPTURE_ASSERTION in t
        assert "NO RETAINED EVIDENCE" in t
        assert "durable copy retained" in t
        assert "pod-lifetime retention" in t
        assert "canary-gate-evidence.md` §4" in t

    def test_readme_points_the_retention_promise_at_the_contract(self):
        t = _collapsed(README)
        assert "retains the JSON evidence" in t
        assert (
            "the durability contract in `docs/notes/canary-gate-evidence.md` §4"
            in t
        )

    def test_stdout_capture_satisfies_the_documented_assertion(
        self, capsys, tmp_path
    ):
        """The property the checklist assertion checks — the capture exists,
        is non-empty, and parses as a gate report — is true of the gate's
        actual stdout: `tee` alone produces a durable copy."""
        with (
            patch("ytt.config.get_settings", return_value=_settings(None)),
            patch("ytt.canary_gate.run_once", return_value=_once_report()),
        ):
            assert cli_main(["canary", "--gate", "--evidence-dir", str(tmp_path)]) == 0
        capture = tmp_path / "canary-gate-capture.json"
        capture.write_text(capsys.readouterr().out, encoding="utf-8")  # what tee does
        assert capture.stat().st_size > 0  # `test -s "$F"`
        # `jq -e '.gate'` — the capture parses as a gate report:
        assert json.loads(capture.read_text(encoding="utf-8"))["gate"] == "pass"
