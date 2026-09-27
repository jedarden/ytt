"""UTC log-timestamp normalization (bead ytt-56679d29).

docs/notes/canary-first-fetch.md had to do manual +4h arithmetic to correlate
canary log lines with ``ytt_canary_last_success_timestamp_seconds``: every
other evidence source (evidence artifacts, Prometheus scrape timestamps,
``ran_at`` fields) is UTC, but the log lines carried two non-UTC stamps —
the stdlib default formatter's *local* time (no offset) and, prefixed in
front of it by ``kubectl logs --timestamps``, the container runtime's stamp
in the **node's** timezone (EDT on the mini-PC agents).  Nothing in the
image can move the runtime's stamp, so the fix has two legs and a guard:

1. **In-message UTC stamps** — the canary surfaces route through
   ``ytt.observability.configure_stdlib_logging()``, whose formatter emits
   an ISO-8601 ``+00:00`` stamp inside each line, TZ-independently; a line
   now matches the epoch-valued gauges directly.
2. **``TZ=UTC`` in the image** — the belt: anything that still renders
   local time (stdlib fallbacks, third-party libs) renders UTC.
3. **The server stays pinned** — structlog's ``TimeStamper`` already
   defaults to UTC (``...Z``); a future ``utc=False`` regression would
   silently reintroduce the arithmetic, so it is pinned here.

Legs:

1. Formatter unit contract — renders UTC from the record's epoch under a
   hostile local timezone, correlating to the gauge epoch to the
   millisecond (the very conversion the evidence note did by hand).
2. Root-logger wiring — a canary-shaped line comes out with the in-message
   stamp and the historical ``LEVEL:name:message`` tail.
3. Server leg — structlog events carry ``Z`` timestamps.
4. Artifact guards — the Dockerfile pins ``TZ=UTC`` in the base stage (so
   builder/test/runtime all inherit), and the entry points route through
   ``configure_stdlib_logging`` with no bare ``basicConfig`` left to
   reintroduce local-time stamps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The third probe success from docs/notes/canary-first-fetch.md, exactly as
#: the Prometheus gauge reported it — the data point whose correlation cost
#: this bead removes.  23:14:05.156Z, which the note reached by hand from a
#: ``19:14:05.156-04:00`` runtime-stamped log line.
EVIDENCE_EPOCH = 1789773245.156
EVIDENCE_STAMP = "2026-09-18T23:14:05.156+00:00"

#: A POSIX TZ string naming US Eastern — parseable by glibc/musl from the
#: rule text alone, no tzdata files needed (python:*-slim ships none).
HOSTILE_TZ = "EST5EDT,M3.2.0/2,M11.1.0/2"


@pytest.fixture
def _clean_root_logger():
    """Snapshot/restore the root logger around tests that rewire it.

    Strips any ``UTCISO8601Formatter`` handler earlier tests leaked first:
    the CLI helpers call ``configure_stdlib_logging()`` under test (the
    ``--once`` suite drives ``_run_canary_once``) and the attached handler
    survives them on the root logger.  Starting each test with no UTC
    handler means ``configure_stdlib_logging`` genuinely attaches here and
    the single-handler assertions hold regardless of module order.
    """
    from ytt.observability import UTCISO8601Formatter

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers[:] = [
        h
        for h in saved_handlers
        if not isinstance(getattr(h, "formatter", None), UTCISO8601Formatter)
    ]
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def hostile_tz(monkeypatch):
    """Set TZ to US Eastern for the test, restoring the process after.

    The explicit restore-then-``tzset`` matters: ``time.tzset()`` re-reads
    the env var into libc's process-wide state, so tearing down without
    re-reading the original value would leak Eastern onto every later test
    in the process even after monkeypatch restores ``os.environ``.
    """
    original = os.environ.get("TZ")
    monkeypatch.setenv("TZ", HOSTILE_TZ)
    time.tzset()
    yield
    if original is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", original)
    time.tzset()


def _local_hours_for_evidence_epoch() -> int:
    """Hour component of ``EVIDENCE_EPOCH`` in the process's local time."""
    return datetime.fromtimestamp(EVIDENCE_EPOCH).hour


# ---------------------------------------------------------------------------
# 1. Formatter contract
# ---------------------------------------------------------------------------


def _format_record_at_evidence_epoch() -> str:
    from ytt.observability import UTCISO8601Formatter

    formatter = UTCISO8601Formatter(
        "%(asctime)s %(levelname)s:%(name)s:%(message)s"
    )
    record = logging.LogRecord(
        name="ytt.canary",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Canary probe succeeded for %s",
        args=("jNQXAC9IVRw",),
        exc_info=None,
    )
    record.created = EVIDENCE_EPOCH
    record.msecs = (EVIDENCE_EPOCH - int(EVIDENCE_EPOCH)) * 1000
    return formatter.format(record)


def test_formatter_stamps_utc_under_edt_localtime(hostile_tz):
    """The in-message stamp must be UTC even when local time says EDT."""
    stamp = _format_record_at_evidence_epoch()
    assert stamp == (
        f"{EVIDENCE_STAMP} INFO:ytt.canary:"
        "Canary probe succeeded for jNQXAC9IVRw"
    )


def test_formatter_beats_the_default_formatter_under_edt(hostile_tz):
    """Guard against a vacuous pass: TZ must actually move local time.

    If the platform ignored ``TZ`` the UTC assertion above would hold for
    the wrong reason, so under a working Eastern TZ the stdlib *default*
    formatter must render the same instant in local time — 4 hours behind
    the UTC stamp, exactly the arithmetic the evidence note performed.
    """
    local_hour = _local_hours_for_evidence_epoch()
    utc_hour = datetime.fromtimestamp(EVIDENCE_EPOCH, tz=timezone.utc).hour
    if local_hour == utc_hour:
        pytest.skip("platform ignored TZ — formatter is TZ-free regardless")
    record = logging.LogRecord(
        "ytt.canary", logging.INFO, __file__, 1, "m", None, None
    )
    record.created = EVIDENCE_EPOCH
    default_stamp = logging.Formatter().formatTime(record)
    default_hour = int(default_stamp.split(" ")[1].split(":")[0])
    assert default_hour == local_hour, (
        f"default formatter rendered hour {default_hour}, expected the "
        f"Eastern local hour {local_hour} — TZ is not taking effect"
    )
    assert default_hour != utc_hour


def test_formatter_output_matches_the_epoch_gauge_directly():
    """The stamp correlates to ``ytt_canary_*`` epoch gauges with no offset.

    The gauge value ``1789773245.156`` (evidence note, third probe) must be
    recoverable from the log line alone — parse the stamp back, interpret
    it UTC, and it is the gauge epoch to the millisecond.
    """
    stamp = _format_record_at_evidence_epoch().split(" ")[0]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is timezone.utc or parsed.utcoffset().total_seconds() == 0
    assert parsed.timestamp() == pytest.approx(EVIDENCE_EPOCH, abs=1e-3)


# ---------------------------------------------------------------------------
# 2. Root-logger wiring (the canary loop's path)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_clean_root_logger")
def test_canary_loop_line_carries_in_message_utc_stamp(monkeypatch):
    """A ``ytt canary``-shaped line: UTC stamp, then the historical tail.

    The capture is a StringIO swapped in for ``sys.stderr`` — which the
    handler resolves at emit time — rather than ``capsys``, because the
    suite's other capture layers leave unrelated text in the shared buffers
    and the line under test must be matched exactly.
    """
    import io

    from ytt.observability import configure_stdlib_logging

    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)
    configure_stdlib_logging()
    logging.getLogger("ytt.canary").info(
        "Canary probe succeeded for %s", "jNQXAC9IVRw"
    )
    err = buffer.getvalue().strip()
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00 "
        r"INFO:ytt.canary:Canary probe succeeded for jNQXAC9IVRw",
        err,
    ), f"canary log line lost its UTC in-message stamp: {err!r}"


@pytest.mark.usefixtures("_clean_root_logger", "hostile_tz")
def test_configure_stdlib_logging_is_tz_independent(monkeypatch):
    """End to end under EDT local time, the emitted line is still UTC."""
    import io

    from ytt.observability import configure_stdlib_logging

    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)
    configure_stdlib_logging()
    logging.getLogger("ytt.canary").info("probe")
    stamp = buffer.getvalue().strip().split(" ")[0]
    assert stamp.endswith("+00:00")
    parsed = datetime.fromisoformat(stamp)
    assert parsed.timestamp() == pytest.approx(time.time(), abs=5)


@pytest.mark.usefixtures("_clean_root_logger")
def test_configure_stdlib_logging_sets_info_level():
    """Parity with the ``basicConfig(level=INFO)`` it replaces: INFO lines pass."""
    from ytt.observability import configure_stdlib_logging

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    configure_stdlib_logging()
    assert root.level == logging.INFO


@pytest.mark.usefixtures("_clean_root_logger")
def test_configure_stdlib_logging_is_idempotent(monkeypatch):
    """A second call must not stack a second handler.

    Evidence lines printed twice would double every retained stderr record
    and break exact-match greps over the runbook's shapes — the same event
    must render exactly once however often the entry points call this.
    """
    import io

    from ytt.observability import UTCISO8601Formatter, configure_stdlib_logging

    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)
    root = logging.getLogger()
    configure_stdlib_logging()
    configure_stdlib_logging()
    wired = [
        h for h in root.handlers if isinstance(h.formatter, UTCISO8601Formatter)
    ]
    assert len(wired) == 1
    logging.getLogger("ytt.canary").info("probe")
    assert buffer.getvalue().count("INFO:ytt.canary:probe") == 1


# ---------------------------------------------------------------------------
# 3. Server leg — structlog stays UTC
# ---------------------------------------------------------------------------


def test_structlog_server_stream_is_utc_z():
    """configure_logging's TimeStamper must emit ``...Z``, never local time.

    stdout is redirected around the logger's creation and write so the
    suite's shared capture buffers can't leak unrelated text into the
    assertion.
    """
    import contextlib
    import io

    import structlog

    from ytt.observability import configure_logging

    configure_logging()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        structlog.get_logger("ytt.log_timezone").info(
            "probe", video="jNQXAC9IVRw"
        )
    event_line = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert event_line["timestamp"].endswith("Z")
    parsed = datetime.fromisoformat(event_line["timestamp"].replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0
    assert parsed.timestamp() == pytest.approx(time.time(), abs=5)


# ---------------------------------------------------------------------------
# 4. Artifact guards — image and entry points
# ---------------------------------------------------------------------------


def test_dockerfile_pins_tz_utc_in_base_stage():
    """TZ=UTC must be set in the *base* stage so all stages inherit it.

    The runtime image is what ships to the cluster, but the test stage runs
    the suite under the same TZ production will have — a test that only
    passed under a different timezone than the shipped image is a trap.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    base_stage = re.split(r"(?m)^FROM ", dockerfile)[1]  # first stage after header
    base_stage = base_stage.split("\nFROM ")[0]
    assert re.search(r"(?m)^ENV TZ=UTC", base_stage), (
        "Dockerfile base stage lost ENV TZ=UTC — local-time rendering in the "
        "image stops matching the UTC evidence sources (bead ytt-56679d29)"
    )


def test_canary_loop_entry_routes_through_utc_formatter():
    """canary.main() must configure the UTC stdlib logging, not basicConfig."""
    source = (REPO_ROOT / "ytt" / "canary.py").read_text(encoding="utf-8")
    main_body = source.split("def main() -> int:", 1)[1]
    assert "configure_stdlib_logging()" in main_body, (
        "canary.main() no longer wires the UTC log formatter — probe lines "
        "lose their in-message UTC stamps (bead ytt-56679d29)"
    )


@pytest.mark.parametrize(
    ("helper", "what"),
    [
        ("_run_canary_once", "one-shot canary"),
        ("_run_canary_gate", "acceptance gate"),
    ],
)
def test_canary_evidence_helpers_route_through_utc_formatter(helper, what):
    """The --once/--gate helpers retain stderr lines as evidence — UTC them."""
    source = (REPO_ROOT / "ytt" / "cli.py").read_text(encoding="utf-8")
    body = source.split(f"def {helper}(", 1)[1].split("\ndef ", 1)[0]
    assert "configure_stdlib_logging()" in body, (
        f"{what} evidence helper no longer wires the UTC log formatter "
        f"(bead ytt-56679d29)"
    )


def test_no_bare_basiconfig_left_in_package():
    """Any future stdlib logging setup must go through the UTC formatter.

    A ``logging.basicConfig`` re-added anywhere in ytt/ would silently
    restore local-time, offset-less stamps on whatever surface calls it —
    the exact regression this bead closed.
    """
    offenders = [
        py.name
        for py in (REPO_ROOT / "ytt").glob("*.py")
        if "basicConfig(" in py.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"bare logging.basicConfig reappeared in {offenders} — route stdlib "
        f"logging through observability.configure_stdlib_logging() so log "
        f"lines keep their UTC stamps"
    )
