"""README ↔ CLI drift guard for the ``ytt canary`` flag surface (bead ytt-7ce8d590).

The README documented only ``ytt canary --once`` and the bare gate
``ytt canary``; the other three flags — ``--via-proxy``, ``--evidence-dir``,
``--video-id`` — lived solely in ``docs/notes/canary-gate-evidence.md``, so
a self-hoster reading the README could not discover them.  The README now
carries the full flag table, and this module holds it to the parser the same
way ``TestReadmeVerdictDoc`` (bead ytt-066781cf) holds the ``--once`` verdict
line to the implementation: the table and the ``canary`` subparser rot
together or not at all.  A flag added to — or removed from — the subparser
fails here until the table follows, and a documented default or
mode-validity claim fails here when the CLI stops honoring it.

The *behavior* of the flag combinations is not re-tested here; it is pinned
where it is implemented (``tests/unit/test_canary_gate.py``'s usage
rejections, ``tests/unit/test_canary_once.py``'s CLI legs).  This module
pins only the documentation.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
CLI_SOURCE = (REPO_ROOT / "ytt" / "cli.py").read_text(encoding="utf-8")

#: The mode-validity surface exactly as ``ytt.cli.main``'s gatekeeping
#: enforces it (the behavior itself is pinned by
#: ``tests/unit/test_canary_gate.py``).  A flag whose valid modes change must
#: change here and in its README row.
_MODE_VALID: dict[str, frozenset[str]] = {
    "--video-id": frozenset({"--once", "--gate"}),
    "--via-proxy": frozenset({"--once"}),
    "--evidence-dir": frozenset({"--gate"}),
}


def _canary_subparser() -> argparse.ArgumentParser:
    """The ``canary`` subparser of the real CLI parser."""
    from ytt.cli import _build_parser

    for action in _build_parser()._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "canary" in choices:
            return choices["canary"]
    raise AssertionError("ytt.cli lost its `canary` subparser")


def _flags() -> dict[str, argparse.Action]:
    """Every public canary flag → its argparse action (``-h``/``--help``
    excluded — argparse owns those, not our documentation)."""
    return {
        option: action
        for action in _canary_subparser()._actions
        if "-h" not in action.option_strings
        for option in action.option_strings
    }


@pytest.fixture(scope="module")
def flag_section() -> tuple[dict[str, tuple[str, str, str]], str]:
    """The README's flag table and the prose paragraph right after it.

    The rows come back as ``{flag: (flag cell, default cell, effect cell)}``;
    the paragraph is where the no-flag (probe-loop) mode and the exit-2
    usage-rejection pointer must live, since the table can only describe
    flags that exist.  Exactly one such table may exist, or the guard could
    not say which one it checked — the table's presence is itself part of
    the contract (without it the gate flags are back to being documented
    only in the evidence note, the drift this bead closed).
    """
    text = README.read_text(encoding="utf-8")
    matches = list(
        re.finditer(
            r"^\| Flag \| Default \| Effect \|\n\|[ :|-]+\|\n(.*?)^(?![|\s])",
            text,
            re.M | re.S,
        )
    )
    assert len(matches) == 1, (
        f"expected exactly one canary flag table in README.md, found {len(matches)}"
    )
    rows: dict[str, tuple[str, str, str]] = {}
    for line in matches[0].group(1).strip().splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 3, f"malformed flag-table row: {line!r}"
        flag = cells[0].split()[0].strip("`")
        rows[flag] = (cells[0], cells[1], cells[2])
    trailing = text[matches[0].end(1):].strip().split("\n\n")[0]
    return rows, trailing


def _row(
    flag_section: tuple[dict[str, tuple[str, str, str]], str], flag: str
) -> tuple[str, str, str]:
    """The table row for *flag*, with a clear message if it went away."""
    row = flag_section[0].get(flag)
    assert row is not None, f"README's flag table lost its {flag} row"
    return row


def test_every_canary_flag_is_documented(flag_section):
    """A flag added to the ``canary`` subparser cannot ship undocumented —
    the parser is walked, not a copy of it, so this is the leg that keeps
    the table complete by construction."""
    rows, _ = flag_section
    undocumented = set(_flags()) - set(rows)
    assert not undocumented, (
        f"the canary subparser defines {sorted(undocumented)} but the README "
        "flag table has no row for them"
    )


def test_every_documented_flag_exists(flag_section):
    """No renamed or removed flag lingers in the table."""
    rows, _ = flag_section
    stale = set(rows) - set(_flags())
    assert not stale, (
        "README documents canary flags the subparser does not define: "
        f"{sorted(stale)}"
    )


def test_switches_and_value_flags_are_documented_faithfully(flag_section):
    """A ``store_true`` flag is documented as defaulting to off and carries
    no value placeholder; a value-taking flag shows one and does not claim
    a bare ``off`` default."""
    rows, _ = flag_section
    actions = _flags()
    for flag, (flag_cell, default_cell, _) in rows.items():
        placeholder = re.search(r"<[^>]+>", flag_cell)
        if isinstance(actions[flag], argparse._StoreTrueAction):
            assert default_cell == "off", (
                f"README documents {flag} default as {default_cell!r}; it is "
                "a switch with no value"
            )
            assert placeholder is None, (
                f"README shows a value placeholder on switch {flag}"
            )
        else:
            assert placeholder is not None, (
                f"README's {flag} row shows no value placeholder; argparse "
                "takes one"
            )
            assert default_cell != "off", (
                f"README documents {flag} default as {default_cell!r}; it "
                "takes a value"
            )


def test_evidence_dir_row_states_the_gate_default(flag_section):
    """The documented ``--evidence-dir`` default is the gate constant, not a
    paraphrase — ``canary_gate.DEFAULT_EVIDENCE_DIR`` moving must drag the
    README with it."""
    from ytt.canary_gate import DEFAULT_EVIDENCE_DIR

    _, default_cell, _ = _row(flag_section, "--evidence-dir")
    assert DEFAULT_EVIDENCE_DIR in default_cell, (
        f"README states evidence-dir default {default_cell!r}; "
        f"canary_gate.DEFAULT_EVIDENCE_DIR is {DEFAULT_EVIDENCE_DIR!r}"
    )


def test_video_id_row_states_the_ladder_default(flag_section):
    """``--video-id`` defaults to the head of the probe ladder (behavior
    pinned by ``test_canary_once.py``'s ladder legs); the row must say so by
    naming the constant, not hardcoding an id that could reorder."""
    _, default_cell, _ = _row(flag_section, "--video-id")
    assert "first" in default_cell and "CANARY_VIDEO_IDS" in default_cell, (
        f"README's --video-id default cell ({default_cell!r}) must state the "
        "first CANARY_VIDEO_IDS entry as the default"
    )


def test_mode_validity_map_tracks_the_cli_source():
    """``_MODE_VALID`` must stay attached to ``ytt.cli.main``'s actual
    gatekeeping: every mapped flag still has its ``is only valid with …``
    ``parser.error`` there.  If cli.py's wording changed, this leg — not a
    silently detached map — is what fails."""
    for flag in _MODE_VALID:
        assert re.search(rf"{re.escape(flag)} is only valid with", CLI_SOURCE), (
            f"cli.py no longer gatekeeps {flag} with an `is only valid with` "
            "usage error — update _MODE_VALID and the README row together"
        )


def test_mode_validity_rows_match_the_cli_gatekeeping(flag_section):
    """Each flag's ``valid with …`` clause names exactly the modes
    ``ytt.cli.main`` accepts it with — no more, no fewer."""
    rows, _ = flag_section
    for flag, modes in _MODE_VALID.items():
        effect = _row(flag_section, flag)[2]
        clause = re.search(r"[Vv]alid with (.+?)(?=[.:;])", effect)
        assert clause, f"README's {flag} row lost its `valid with …` claim"
        documented = set(re.findall(r"`(--[a-z-]+)`", clause.group(1)))
        assert documented == set(modes), (
            f"README documents {flag} as valid with {sorted(documented)}; "
            f"the CLI accepts it with {sorted(modes)}"
        )


def test_once_and_gate_are_documented_as_mutually_exclusive(flag_section):
    """``--gate and --once are mutually exclusive`` (cli.py's gatekeeping)
    must stay stated on both rows — the README's two worked examples name
    each mode separately, so the exclusivity lives only here."""
    assert "mutually exclusive" in CLI_SOURCE, (
        "cli.py no longer rejects --gate --once as mutually exclusive — "
        "the README rows' exclusivity claims are now false"
    )
    for flag, other in (("--once", "--gate"), ("--gate", "--once")):
        effect = _row(flag_section, flag)[2]
        assert f"mutually exclusive with `{other}`" in effect, (
            f"README's {flag} row must keep its mutual-exclusion claim with "
            f"{other}"
        )


def test_bare_invocation_is_documented_as_the_probe_loop(flag_section):
    """The subparser's own help promises ``probe loop by default``; the table
    can only describe flags, so the no-flag mode — the one the canary
    Deployment actually runs — must be stated in the prose beside it."""
    _, trailing = flag_section
    assert "probe loop" in trailing, (
        "README's flag-table prose no longer documents what bare "
        "`ytt canary` (no flag) does"
    )
    assert "YTT_CANARY_INTERVAL_SEC" in trailing, (
        "README's flag-table prose should point the loop's cadence at its "
        "configuration row"
    )


def test_usage_rejections_point_at_the_exit_contract(flag_section):
    """Invalid flag/mode combinations exit 2 (argparse), and the exit-code
    contract lives in the evidence spec — the README must keep saying both,
    or a caller meeting exit 2 has nowhere to go."""
    _, trailing = flag_section
    assert "exit `2`" in trailing, (
        "README's flag-table prose no longer documents usage-rejection exit 2"
    )
    assert "docs/notes/canary-gate-evidence.md" in trailing, (
        "README's flag-table prose must point at the exit-code contract"
    )
