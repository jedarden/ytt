"""Startup egress probe + the ``ytt_egress_is_residential`` gauge.

``docs/notes/canary-first-fetch.md`` treats the server pod's startup egress
probe and its ``ytt_egress_is_residential`` gauge as a first-class
residential-egress signal, and ``docs/notes/http-endpoints.md`` documents
that the public ``/ytt/metrics`` scrape exposes that gauge to the internet —
yet the probe itself had no pinning test: the gauge's *value* could drift
from the probe's behaviour silently, and a startup-path regression would
surface only as a mystery 0 on a production scrape.  Three regressions,
three layers:

1. **Runs at startup; gauge always exported, 0 or 1, never absent.**  A
   fresh interpreter drives the real ``serve()`` (uvicorn and the ASGI app
   stubbed, the probe stubbed to a known-residential report): the probe is
   called exactly once, before the listener starts, receives the configured
   proxy path, and the gauge reads 1.  A plain import — no probe at all —
   still exports exactly one 0-valued series, so "metric missing" can only
   ever mean a scrape or routing problem, never "the probe has not run
   yet": the same unambiguity rationale as the pre-registered
   ``ytt_fetch_blocks_total`` outcomes.
2. **Fails soft, bounded.**  A probe that raises (unreachable ipinfo, DNS
   failure, timeout) must not take the boot down with it: the real
   ``serve()`` still reaches uvicorn, with the gauge at 0 and a
   ``Startup egress probe failed`` log.  The bound itself is pinned on the
   un-stubbed probe: it hands httpx the finite ``_PROBE_TIMEOUT_SEC``
   timeout, and a genuine connection-refused (closed loopback port) raises
   promptly instead of hanging the startup path.
3. **Label surface.**  The gauge carries no labels at all — exactly one
   series per scrape, no label keys — the narrowest slice of the bounded
   public ``/ytt/metrics`` label set that
   ``tests/unit/test_metrics_cardinality.py`` enforces family-wide.

The two ``serve()`` legs run in real subprocesses because the probe-and-set
sequence lives inside ``serve()`` (before uvicorn), where an in-process test
can neither observe the ordering nor scrape the registry "as the pod would
have it" — the same reasoning as ``tests/unit/test_singleton_runtime.py``,
whose child harness shape (fake-OAuth conftest import, stubbed
``uvicorn.run``/``build_asgi_app``, JSON result file) this module reuses.
Nothing here touches the network beyond a closed loopback port.
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Long enough for a cold subprocess importing ytt (structlog, FastMCP et al.)
#: on a loaded box; children never legitimately run this long — the real
#: probe timeout is bounded at 10 s and stubbed out here anyway.
_CHILD_TIMEOUT_SEC = 120.0

#: A syntactically valid, deliberately fake proxy URL — the child asserts the
#: startup probe receives it verbatim (the classified IP must be the proxy's
#: egress, not the pod's native one).
_FAKE_PROXY_URL = "http://probe-user:probe-pass@egress-probe.test:3128"

# ---------------------------------------------------------------------------
# Subprocess children: the real serve() startup sequence
# ---------------------------------------------------------------------------

#: Shared child skeleton: run the real ``serve()`` wiring with uvicorn and
#: the ASGI app stubbed, record the probe/uvicorn event order plus the proxy
#: argument the probe received, scrape the default registry after serve()
#: returns, and dump everything as JSON.  Patching ``ytt.selftest.probe_egress``
#: works because serve() imports it inside the function body (the same trick
#: ``test_singleton_runtime`` uses).  ``_PROBE_BODY`` is the per-case stub.
_CHILD_TEMPLATE = textwrap.dedent(
    """
    import json, sys
    import tests.conftest  # noqa: F401  (fake-OAuth env + OIDC discovery patch)
    import uvicorn
    from prometheus_client import generate_latest
    from ytt import selftest as _selftest
    from ytt import server

    events = []
    probe_args = []

    {probe_body}

    def _fake_run(app, **kwargs):
        events.append("uvicorn")

    uvicorn.run = _fake_run
    server.build_asgi_app = lambda: object()

    rc = server.serve()
    gauge_series = [
        line
        for line in generate_latest().decode().splitlines()
        if line.startswith("ytt_egress_is_residential ")
    ]
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(
            {{"rc": rc, "events": events, "probe_args": probe_args,
              "gauge_series": gauge_series}},
            fh,
        )
    raise SystemExit(rc)
    """
)

_SUCCESS_CHILD_CODE = _CHILD_TEMPLATE.format(
    probe_body=textwrap.dedent(
        """
        class _FakeReport:
            ip = "203.0.113.7"
            asn = "AS64500"
            org = "TEST-ORG"
            via_proxy = True
            is_residential = True

        def _fake_probe(proxy_url):
            events.append("probe")
            probe_args.append(proxy_url)
            return _FakeReport()

        _selftest.probe_egress = _fake_probe
        """
    )
)

_FAILURE_CHILD_CODE = _CHILD_TEMPLATE.format(
    probe_body=textwrap.dedent(
        """
        import httpx

        def _failing_probe(proxy_url):
            events.append("probe")
            probe_args.append(proxy_url)
            raise httpx.ConnectError("connection refused")

        _selftest.probe_egress = _failing_probe
        """
    )
)


def _child_env(cache_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["YTT_CACHE_DIR"] = str(cache_dir)
    # emptydir backend: validate_storage() only warns, so a child's outcome is
    # attributable to the egress probe and nothing else.
    env["YTT_CACHE_BACKEND"] = "emptydir"
    env["YTT_PROXY_URL"] = _FAKE_PROXY_URL
    env.update(extra or {})
    return env


def _run_serve_child(code: str, cache_dir: Path, result: Path) -> dict:
    try:
        child = subprocess.run(
            [sys.executable, "-c", code, str(result)],
            cwd=REPO_ROOT,
            env=_child_env(cache_dir),
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"serve() child timed out after {_CHILD_TIMEOUT_SEC}s — the startup "
            f"probe hung the boot; stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        ) from exc
    if not result.exists():
        raise AssertionError(
            f"serve() child never wrote its result (rc={child.returncode}); "
            f"stdout:\n{child.stdout}\nstderr:\n{child.stderr}"
        )
    out = json.loads(result.read_text(encoding="utf-8"))
    out["_stdout"] = child.stdout
    out["_stderr"] = child.stderr
    out["_returncode"] = child.returncode
    return out


# ---------------------------------------------------------------------------
# 1. Runs at startup; the gauge is always exported, 0 or 1, never absent
# ---------------------------------------------------------------------------


def test_serve_probes_egress_once_before_the_listener_and_sets_the_gauge(
    tmp_path,
):
    """The real serve() calls the egress probe exactly once, before uvicorn,
    with the configured proxy path, and exports the verdict as the gauge."""
    result = tmp_path / "serve-result.json"
    out = _run_serve_child(_SUCCESS_CHILD_CODE, tmp_path / "cache", result)

    assert out["rc"] == 0, (
        f"serve() failed on a healthy probe; stdout:\n{out['_stdout']}"
        f"\nstderr:\n{out['_stderr']}"
    )
    # Exactly one probe, and it ran *before* the listener started — it is a
    # startup step, not a lazily-triggered or periodic one.
    assert out["events"] == ["probe", "uvicorn"]
    # The probe dialed the configured egress path, not the native one.
    assert out["probe_args"] == [_FAKE_PROXY_URL]
    # The gauge is exported, present, and carries the probe's verdict.
    assert out["gauge_series"] == ["ytt_egress_is_residential 1.0"]
    assert "Startup egress probe" in out["_stdout"]
    assert "Startup egress probe failed" not in out["_stdout"]


def test_gauge_is_exported_by_import_alone_before_any_probe_runs():
    """A process that has not probed anything still exports exactly one
    ``ytt_egress_is_residential`` series (registered at import, default 0) —
    metric absence stays unambiguous: it can only mean the scrape or the
    routing is broken, never "the probe has not run yet"."""
    import ytt.observability  # noqa: F401  (registers the gauge on import)

    families = {
        fam.name: fam
        for fam in text_string_to_metric_families(generate_latest(REGISTRY).decode())
    }
    samples = list(families["ytt_egress_is_residential"].samples)
    assert len(samples) == 1
    (sample,) = samples
    assert sample.labels == {}
    assert sample.value in (0.0, 1.0)


def test_public_metrics_scrape_carries_one_label_free_zero_or_one_series():
    """The public /ytt/metrics body shows the same shape: exactly one
    label-free ``ytt_egress_is_residential`` series, value 0 or 1."""
    from ytt.server import build_asgi_app

    app = build_asgi_app()
    with TestClient(app) as client:
        resp = client.get("/ytt/metrics")
    assert resp.status_code == 200
    families = {
        fam.name: fam
        for fam in text_string_to_metric_families(resp.text)
    }
    samples = list(families["ytt_egress_is_residential"].samples)
    assert len(samples) == 1
    (sample,) = samples
    assert sample.labels == {}
    assert sample.value in (0.0, 1.0)


def test_metrics_scrape_never_probes_or_sets_the_gauge(monkeypatch):
    """A scrape is read-only — it must not re-run the probe (one-shot per
    boot, re-probed only by /ytt/admin/egress or a restart)."""
    from ytt.server import build_asgi_app

    calls: list[int] = []

    def _must_not_probe(proxy_url=None):
        calls.append(1)
        raise AssertionError("a /ytt/metrics scrape re-ran the egress probe")

    monkeypatch.setattr("ytt.selftest.probe_egress", _must_not_probe)
    app = build_asgi_app()
    with TestClient(app) as client:
        assert client.get("/ytt/metrics").status_code == 200
    assert calls == []


# ---------------------------------------------------------------------------
# 2. Fails soft, bounded — the probe is in the startup path
# ---------------------------------------------------------------------------


def test_failing_probe_fails_soft_gauge_stays_zero_and_boot_completes(tmp_path):
    """An unreachable probe target must not crash or hang the boot: serve()
    still reaches uvicorn, logs the failure, and the gauge stays exported at
    0 (which reads as "not classified residential" — never as absent)."""
    result = tmp_path / "serve-result.json"
    out = _run_serve_child(_FAILURE_CHILD_CODE, tmp_path / "cache", result)

    assert out["rc"] == 0, (
        f"a failing egress probe took the boot down; stdout:\n{out['_stdout']}"
        f"\nstderr:\n{out['_stderr']}"
    )
    # The probe ran (once), failed, and the server kept booting.
    assert out["events"] == ["probe", "uvicorn"]
    assert out["gauge_series"] == ["ytt_egress_is_residential 0.0"]
    assert "Startup egress probe failed" in out["_stdout"]


def test_probe_hands_httpx_a_finite_bounded_timeout():
    """The startup path's network call is bounded: the probe passes its
    finite ``_PROBE_TIMEOUT_SEC`` to httpx (direct and proxied alike), so a
    black-holed ipinfo cannot hang the boot past that constant."""
    from ytt import selftest

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"ip": "203.0.113.7", "org": "AS64500 TEST-ORG"}
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    for proxy_url in (None, _FAKE_PROXY_URL):
        with patch("ytt.selftest.httpx.Client", return_value=client) as client_cls:
            selftest.probe_egress(proxy_url)
        timeout = client_cls.call_args[1]["timeout"]
        assert timeout == selftest._PROBE_TIMEOUT_SEC
        assert math.isfinite(timeout) and timeout > 0


def test_unreachable_probe_target_raises_promptly_not_hangs(monkeypatch):
    """A genuinely unreachable target fails fast (connection refused on a
    closed loopback port) with an httpx error serve() can catch — never a
    hang — well inside the probe's own timeout."""
    from ytt import selftest

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]  # released: nothing is listening
    monkeypatch.setattr(
        selftest, "_IPINFO_URL", f"http://127.0.0.1:{dead_port}/json"
    )

    started = time.monotonic()
    with pytest.raises(httpx.HTTPError):
        selftest.probe_egress(None)
    elapsed = time.monotonic() - started

    assert elapsed < selftest._PROBE_TIMEOUT_SEC + 10, (
        f"the probe took {elapsed:.1f}s against a dead loopback port — the "
        "startup path is not bounded as documented"
    )


# ---------------------------------------------------------------------------
# 3. Label surface
# ---------------------------------------------------------------------------


def test_gauge_stays_within_the_bounded_public_label_set():
    """``ytt_egress_is_residential`` must stay label-free: any label key on
    it would ship to the public /ytt/metrics scrape outside the documented
    bounded surface (the per-family allowlist in
    tests/unit/test_metrics_cardinality.py pins the whole table; this pins
    the gauge's own row)."""
    from ytt.observability import ytt_egress_is_residential
    from ytt.server import build_asgi_app

    assert ytt_egress_is_residential._labelnames == ()

    app = build_asgi_app()
    with TestClient(app) as client:
        resp = client.get("/ytt/metrics")
    families = {
        fam.name: fam for fam in text_string_to_metric_families(resp.text)
    }
    samples = list(families["ytt_egress_is_residential"].samples)
    assert len(samples) == 1
    assert samples[0].labels == {}
