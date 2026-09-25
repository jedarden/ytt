"""Continuous canary freshness monitoring — loop metrics and drift guards.

Binds the three artifacts of standing canary monitoring together so they
cannot drift apart silently (bead ytt-2b3ca59e):

1. **Code** — ``ytt/canary.py``: the per-path probe metrics
   (``ytt_canary_probe_last_success_timestamp_seconds{probe}``,
   ``ytt_canary_probes_total{probe, outcome}``), the shared
   ``direct|via_proxy`` vocabulary and the boot-initialization semantics.
2. **Rules** — ``deploy/k8s/ardenone-cluster/ytt/prometheusrule.yml``: the
   four ``YttCanary*`` alerts, their expressions and severities.
3. **Runbook** — ``deploy/CANARY-MONITORING-RUNBOOK.md``: the signal
   catalog, the alert catalog with expressions verbatim, and the
   rollback-vs-escalation response table.

Loop-metric tests run exactly one probe-loop cycle with the metric objects
mocked (the prometheus_client REGISTRY is process-global; the established
pattern from test_canary_once.py) and the ladder mocked per path.  No
network I/O.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ytt import canary, canary_gate
from ytt.canary import CANARY_PROBE_OUTCOMES, CANARY_VIDEO_IDS

REPO_ROOT = Path(__file__).resolve().parents[2]
RULE_PATH = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt" / "prometheusrule.yml"
RUNBOOK_PATH = REPO_ROOT / "deploy" / "CANARY-MONITORING-RUNBOOK.md"

#: The canary alert family — the runbook and the rule must carry exactly
#: this set, no more (undocumented alert) and no fewer (missing monitoring).
CANARY_ALERTS = {
    "YttCanaryFailed",
    "YttCanaryDirectBlocked",
    "YttCanaryFallbackBroken",
    "YttCanaryProbeFlapping",
}

#: Canonical overall-path expression — byte-stable across the monitoring
#: change so old and new images satisfy it identically (runbook §2.1/§6).
CANARY_FAILED_EXPR = "time() - ytt_canary_last_success_timestamp_seconds > 1800"


def _rules() -> dict[str, dict]:
    doc = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return {r["alert"]: r for group in doc["spec"]["groups"] for r in group["rules"]}


def _collapse(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Probe-loop metrics — one cycle with mocked metrics and ladder
# ---------------------------------------------------------------------------


class _StopLoop(Exception):
    """Raised by the patched ``asyncio.sleep`` to end the loop after one cycle."""


def _detail(ok: bool, outcome: str | None = None) -> dict:
    """A ``_probe_ladder_once``-shaped terminating report."""
    return {
        "ok": ok,
        "outcome": outcome or ("ok" if ok else "ip_blocked"),
        "video_id": CANARY_VIDEO_IDS[0],
        "langs": ["en"] if ok else [],
        "duration_sec": 0.5,
        "via_proxy": False,
        "error": None if ok else "probe failed",
    }


def _run_cycle(
    direct: dict, via: dict | None = None, *, proxy_url: str | None = None
) -> SimpleNamespace:
    """Run exactly one ``run_probe_loop`` cycle; return the metric mocks.

    ``direct``/``via`` are the ladder results each path's probe returns.
    """
    overall, failures = MagicMock(), MagicMock()
    pgauge, pcount = MagicMock(), MagicMock()
    settings = MagicMock(proxy_url=proxy_url)
    ladder_calls: list[str | None] = []

    def ladder(proxy=None):
        ladder_calls.append(proxy)
        return dict(via if proxy else direct)

    with (
        patch("ytt.config.get_settings", return_value=settings),
        patch("ytt.canary._probe_ladder_once", side_effect=ladder),
        patch("ytt.canary.ytt_canary_last_success_timestamp_seconds", overall),
        patch("ytt.canary.ytt_canary_failures_total", failures),
        patch("ytt.canary.ytt_canary_probe_last_success_timestamp_seconds", pgauge),
        patch("ytt.canary.ytt_canary_probes_total", pcount),
        patch("asyncio.sleep", side_effect=_StopLoop),
    ):
        with pytest.raises(_StopLoop):
            asyncio.run(canary.run_probe_loop(interval_sec=600))

    return SimpleNamespace(
        overall=overall,
        failures=failures,
        pgauge=pgauge,
        pcount=pcount,
        ladder_calls=ladder_calls,
    )


def _label_kwargs(mock: MagicMock) -> set[str]:
    return {call.kwargs.get("probe") for call in mock.labels.call_args_list}


class TestProbeLoopMetrics:
    def test_no_proxy_probes_only_the_direct_path(self):
        cycle = _run_cycle(_detail(True))
        assert cycle.ladder_calls == [None]

    def test_proxy_configured_probes_both_paths(self):
        cycle = _run_cycle(_detail(True), _detail(True), proxy_url="http://p:1")
        assert cycle.ladder_calls == [None, "http://p:1"]

    def test_boot_init_stamps_freshness_for_probed_paths_only(self):
        """Gauges initialize to loop start — for exactly the paths the loop
        will probe.  No via_proxy child is ever created without a proxy, so
        the series is absent (runbook §1: absent means "not probed", never
        "broken")."""
        no_proxy = _run_cycle(_detail(True))
        assert _label_kwargs(no_proxy.pgauge) == {"direct"}

        with_proxy = _run_cycle(_detail(True), _detail(True), proxy_url="http://p:1")
        assert _label_kwargs(with_proxy.pgauge) == {"direct", "via_proxy"}

    def test_boot_init_pre_registers_zero_counter_children(self):
        """Every canonical outcome gets a zero child per probed path, so an
        absent series reads as "process predates this", not "not
        registered" (runbook §1)."""
        cycle = _run_cycle(_detail(True), _detail(True), proxy_url="http://p:1")
        pairs = {
            (call.kwargs.get("probe"), call.kwargs.get("outcome"))
            for call in cycle.pcount.labels.call_args_list
        }
        expected = {
            (probe, outcome)
            for probe in ("direct", "via_proxy")
            for outcome in CANARY_PROBE_OUTCOMES
        }
        # the cycle's own terminations add (path, "ok") pairs on top
        assert expected <= pairs
        assert ("direct", "ok") in pairs and ("via_proxy", "ok") in pairs

    def test_cycle_success_refreshes_overall_and_path_gauges(self):
        cycle = _run_cycle(_detail(True), _detail(True), proxy_url="http://p:1")
        # boot stamp + one any-path stamp
        assert cycle.overall.set.call_count == 2
        assert cycle.failures.inc.called is False
        assert cycle.pgauge.labels.call_count == 4  # boot x2 + success x2

    def test_any_path_success_refreshes_the_overall_gauge(self):
        """Overall freshness is ANY-path: a working proxy keeps
        YttCanaryFailed quiet while direct is blocked (runbook §1)."""
        cycle = _run_cycle(_detail(False), _detail(True), proxy_url="http://p:1")
        assert cycle.overall.set.call_count == 2
        assert cycle.failures.inc.called is False

    def test_no_path_success_increments_failures_not_the_gauge(self):
        cycle = _run_cycle(_detail(False), _detail(False), proxy_url="http://p:1")
        assert cycle.failures.inc.call_count == 1
        assert cycle.overall.set.call_count == 1  # boot stamp only
        assert cycle.pgauge.labels.call_count == 2  # boot stamps only

    def test_termination_outcome_labels_each_path_counter(self):
        cycle = _run_cycle(
            _detail(False, "empty_body"), _detail(False, "rate_limited"),
            proxy_url="http://p:1",
        )
        pairs = {
            (call.kwargs.get("probe"), call.kwargs.get("outcome"))
            for call in cycle.pcount.labels.call_args_list
        }
        assert ("direct", "empty_body") in pairs
        assert ("via_proxy", "rate_limited") in pairs


class TestRecordProbe:
    """_record_probe — the per-path fold (counter always, gauge on ok)."""

    def _record(self, probe: str, detail: dict) -> SimpleNamespace:
        pgauge, pcount = MagicMock(), MagicMock()
        with (
            patch("ytt.canary.ytt_canary_probe_last_success_timestamp_seconds", pgauge),
            patch("ytt.canary.ytt_canary_probes_total", pcount),
        ):
            ok = canary._record_probe(probe, detail)
        return SimpleNamespace(ok=ok, pgauge=pgauge, pcount=pcount)

    def test_success_counts_and_stamps(self):
        rec = self._record("direct", _detail(True))
        assert rec.ok is True
        rec.pcount.labels.assert_called_once_with(probe="direct", outcome="ok")
        rec.pcount.labels.return_value.inc.assert_called_once_with()
        rec.pgauge.labels.assert_called_once_with(probe="direct")
        rec.pgauge.labels.return_value.set.assert_called_once()

    def test_failure_counts_without_stamping(self):
        rec = self._record("via_proxy", _detail(False, "ip_blocked"))
        assert rec.ok is False
        rec.pcount.labels.assert_called_once_with(
            probe="via_proxy", outcome="ip_blocked"
        )
        rec.pcount.labels.return_value.inc.assert_called_once_with()
        rec.pgauge.labels.assert_not_called()


# ---------------------------------------------------------------------------
# Vocabulary — one probe-path language across gate, metrics and rules
# ---------------------------------------------------------------------------


def test_probe_vocabulary_pinned_to_gate_order():
    """canary.PROBE_LABELS == canary_gate.PROBE_ORDER — an alert, a
    remediation directive and a log line must name a path the same way."""
    assert tuple(canary.PROBE_LABELS) == ("direct", "via_proxy")
    assert canary_gate.PROBE_ORDER == tuple(canary.PROBE_LABELS)


def test_outcome_labels_are_ok_plus_real_error_codes():
    """Pre-registered outcomes must be real ytt.errors codes (or ok) — a
    typo here ships a zero child nothing can ever increment."""
    from ytt import errors as ytt_errors

    real_codes = {
        ytt_errors.PRIVATE,
        ytt_errors.UNAVAILABLE,
        ytt_errors.RATE_LIMITED,
        ytt_errors.IP_BLOCKED,
        ytt_errors.EMPTY_BODY,
    }
    assert CANARY_PROBE_OUTCOMES[0] == "ok"
    assert set(CANARY_PROBE_OUTCOMES[1:]) <= real_codes


# ---------------------------------------------------------------------------
# Drift guards — rules ↔ runbook ↔ code
# ---------------------------------------------------------------------------


class TestPrometheusRule:
    def test_canary_alert_set_is_exactly_the_documented_four(self):
        rules = _rules()
        assert {a for a in rules if a.startswith("YttCanary")} == CANARY_ALERTS

    def test_canary_failed_expression_is_byte_stable_and_critical(self):
        rule = _rules()["YttCanaryFailed"]
        assert _collapse(rule["expr"]) == CANARY_FAILED_EXPR
        assert rule["labels"]["severity"] == "critical"

    @pytest.mark.parametrize(
        ("alert", "stale", "fresh"),
        [
            ("YttCanaryDirectBlocked", "direct", "via_proxy"),
            ("YttCanaryFallbackBroken", "via_proxy", "direct"),
        ],
    )
    def test_paired_alerts_pin_paths_and_matching(self, alert, stale, fresh):
        """One path stale (3 missed cycles) while the other is fresh (1
        interval) — and `and ignoring(probe)` present, without which the
        differently-labelled operands match nothing and the alert can never
        fire."""
        expr = _collapse(_rules()[alert]["expr"])
        assert f'{{probe="{stale}"}} > 1800' in expr
        assert f'{{probe="{fresh}"}} < 600' in expr
        assert "and ignoring(probe)" in expr
        assert _rules()[alert]["labels"]["severity"] == "warning"

    def test_flapping_rule_is_a_share_of_terminations(self):
        expr = _collapse(_rules()["YttCanaryProbeFlapping"]["expr"])
        assert "ytt_canary_probes_total" in expr
        assert 'outcome!="ok"' in expr
        assert "[30m]" in expr
        assert "> 0.5" in expr

    def test_rule_probe_labels_use_the_shared_vocabulary(self):
        probes = set()
        for alert in CANARY_ALERTS:
            probes |= set(
                re.findall(r'probe="([\w]+)"', _rules()[alert]["expr"])
            )
        assert probes <= set(canary.PROBE_LABELS)

    def test_every_rule_metric_is_defined_in_code(self):
        """Each ytt_canary_* name an alert references must be a metric the
        code actually defines — a renamed metric would otherwise silence
        the alert (empty operand) instead of failing anything."""
        canary_src = (REPO_ROOT / "ytt" / "canary.py").read_text(encoding="utf-8")
        observability_src = (REPO_ROOT / "ytt" / "observability.py").read_text(
            encoding="utf-8"
        )
        for alert in CANARY_ALERTS:
            for name in set(
                re.findall(r"\byt\w*canary\w*_\w+\b", _rules()[alert]["expr"])
            ):
                assert name in canary_src or name in observability_src, (
                    f"{alert} references {name}, defined nowhere in code"
                )


class TestRunbookDrift:
    @classmethod
    def runbook(cls) -> str:
        return RUNBOOK_PATH.read_text(encoding="utf-8")

    def test_runbook_exists_and_documents_every_alert(self):
        text = self.runbook()
        for alert in CANARY_ALERTS:
            assert alert in text, f"{alert} missing from the runbook"

    def test_runbook_severities_match_the_rule(self):
        rows = dict(
            re.findall(r"^\|\s*`(YttCanary\w+)`\s*\|\s*(\w+)\s*\|", self.runbook(), re.M)
        )
        assert rows, "runbook §2 alert table not found — format drifted?"
        for alert in CANARY_ALERTS:
            assert rows.get(alert) == _rules()[alert]["labels"]["severity"], (
                f"{alert}: runbook says {rows.get(alert)!r}, rule says "
                f"{_rules()[alert]['labels']['severity']!r}"
            )

    def test_runbook_carries_canonical_expressions(self):
        """The runbook quotes the alert expressions verbatim (whitespace
        aside) — an operator reading §2 sees exactly what Prometheus
        evaluates."""
        text = _collapse(self.runbook())
        assert CANARY_FAILED_EXPR in text
        for alert in ("YttCanaryDirectBlocked", "YttCanaryFallbackBroken",
                      "YttCanaryProbeFlapping"):
            assert _collapse(_rules()[alert]["expr"]) in text, (
                f"{alert}: runbook expression differs from prometheusrule.yml"
            )

    def test_runbook_documents_the_signal_catalog(self):
        text = self.runbook()
        for metric in (
            "ytt_canary_last_success_timestamp_seconds",
            "ytt_canary_failures_total",
            "ytt_canary_probe_last_success_timestamp_seconds",
            "ytt_canary_probes_total",
        ):
            assert metric in text, f"{metric} missing from the runbook §1 table"

    def test_runbook_response_table_covers_every_alert(self):
        """§4 (rollback vs escalation) must have a row per alert — an alert
        without a documented response is a page nobody knows how to act
        on."""
        text = self.runbook()
        for alert in CANARY_ALERTS:
            assert text.count(alert) >= 2, (
                f"{alert} appears only in the catalog — no §4 response row"
            )

    def test_runbook_names_both_rollback_and_escalation(self):
        text = self.runbook().lower()
        assert "roll back" in text or "rollback" in text
        assert "escalate" in text


def test_canary_module_points_at_the_runbook():
    """The code's own docstring must route an operator reading the metrics
    to the runbook that explains them."""
    src = (REPO_ROOT / "ytt" / "canary.py").read_text(encoding="utf-8")
    assert "CANARY-MONITORING-RUNBOOK" in src
