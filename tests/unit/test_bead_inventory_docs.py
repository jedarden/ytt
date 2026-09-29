"""Guards against stale bead-status documentation (bead ytt-042fee08).

The authoritative source of bead state is the **live bead-rs store**
(``bead list``); ``docs/bead-inventory.md`` / ``docs/bead-inventory.json``
are *generated* point-in-time snapshots produced by
``scripts/regen-bead-inventory.sh``.  The snapshot rotted for real once: the
committed 2026-09-17 pair still listed 11 not-closed beads a week later,
every one of which had since closed, and a planning strand consulting it got
a count that matched nothing live.  These tests make that failure class
mechanical instead of habitual:

* the two generated files must agree with each other — same timestamp, same
  summary counts, same ID set — catching a hand-edited or half-regenerated
  pair;
* curated docs must carry no hand-written bead-status sections or counts —
  a status list pasted into a plan or README is stale the moment it is
  committed, so status claims belong only in the generated inventory
  (dated archival material — ``notes/``, ``docs/research/`` — is exempt);
* on hosts with a live bead store the committed snapshot must be at most
  ``MAX_AGE_DAYS`` old.  Same skip shape as the deploy-parity guard: clean
  extractions and CI image builds have no ``.beads/beads.db`` (gitignored),
  so freshness is enforced only where regeneration is actually possible —
  run ``scripts/regen-bead-inventory.sh`` and commit the refreshed pair.
  Normally nobody has to remember to: the regeneration cadence
  (``scripts/bead-inventory-cadence.sh`` under the ``ytt-bead-inventory-
  regen.timer`` systemd --user unit on the designated host) refreshes the
  pair daily from the live store and commits it; this suite leg is the
  backstop that fires only if that cadence dies;
* the cadence itself cannot rot silently either — its age bound must match
  this module's ``MAX_AGE_DAYS``, its ``age`` self-label must classify
  fabricated fresh/stale/boundary snapshots correctly, and the committed
  systemd units must stay wired to it.

``scripts/definition-of-done.sh`` runs these as part of the unit suite.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_MD = REPO_ROOT / "docs" / "bead-inventory.md"
INVENTORY_JSON = REPO_ROOT / "docs" / "bead-inventory.json"
BEADS_DB = REPO_ROOT / ".beads" / "beads.db"
CADENCE_SCRIPT = REPO_ROOT / "scripts" / "bead-inventory-cadence.sh"
CADENCE_NOTE = REPO_ROOT / "docs" / "notes" / "bead-inventory-cadence.md"
CADENCE_SERVICE_UNIT = REPO_ROOT / "scripts" / "systemd" / "ytt-bead-inventory-regen.service"
CADENCE_TIMER_UNIT = REPO_ROOT / "scripts" / "systemd" / "ytt-bead-inventory-regen.timer"

# The observed rot was 7 days old when it misinformed a planning strand
# (2026-09-17 snapshot consulted 2026-09-24).  14 days gives any weekly-ish
# regeneration pass a margin while preventing fossilization.
MAX_AGE_DAYS = 14

NOT_CLOSED_STATUSES = {"open", "in_progress"}

# Hand-written status claims that must not appear in curated docs.  A section
# header like "Current Open Beads" and a count like "13 open beads" are both
# snapshots-in-prose: unverifiable at read time and wrong by the next claim.
STATUS_CLAIM_PATTERNS = (
    re.compile(r"(?i)\bcurrent(ly)?\s+open\s+beads?\b"),
    re.compile(r"(?i)\b\d+\s+(?:open|in-progress|in\s+progress)\s+beads?\b"),
)

# Dated archival / source material, plus the generated inventory itself.
LINT_EXEMPT_PREFIXES = (
    REPO_ROOT / "notes",        # per-bead investigation records, timestamped
    REPO_ROOT / "docs" / "research",  # third-party research and prior art
)
LINT_EXEMPT_FILES = (INVENTORY_MD,)


def _lint_targets() -> list[Path]:
    targets = [
        path
        for path in sorted(REPO_ROOT.glob("*.md")) + sorted((REPO_ROOT / "docs").rglob("*.md"))
        if path.is_file()
        and path not in LINT_EXEMPT_FILES
        and not any(path.is_relative_to(prefix) for prefix in LINT_EXEMPT_PREFIXES)
    ]
    assert targets, "bead-status lint found no curated docs to scan — scope is wrong"
    return targets


def test_inventory_json_is_internally_consistent():
    doc = json.loads(INVENTORY_JSON.read_text())
    summary = doc["summary"]
    listed = doc["beads_not_closed"]
    ids = [b["id"] for b in listed]

    assert all(b["status"] != "closed" for b in listed), (
        "docs/bead-inventory.json lists a closed bead under beads_not_closed — "
        "regenerate with scripts/regen-bead-inventory.sh"
    )
    assert len(ids) == len(set(ids)), "duplicate IDs in beads_not_closed"
    assert summary["open"] == sum(1 for b in listed if b["status"] == "open")
    assert summary["in_progress"] == sum(1 for b in listed if b["status"] == "in_progress")
    # The status vocabulary is not closed (deferred appeared live
    # 2026-09-27): every non-headline status must be counted under `other`
    # and named in other_statuses, never folded silently into the total.
    other_by_status = {}
    for b in listed:
        if b["status"] not in NOT_CLOSED_STATUSES:
            other_by_status[b["status"]] = other_by_status.get(b["status"], 0) + 1
    assert summary["other"] == len(listed) - sum(
        1 for b in listed if b["status"] in NOT_CLOSED_STATUSES
    )
    assert summary["other_statuses"] == other_by_status
    assert summary["open"] + summary["in_progress"] + summary["other"] == len(listed)
    assert (
        summary["total_beads"]
        == summary["open"] + summary["in_progress"] + summary["closed"] + summary["other"]
    )


def test_inventory_md_agrees_with_json():
    doc = json.loads(INVENTORY_JSON.read_text())
    summary = doc["summary"]
    md = INVENTORY_MD.read_text()

    generated = re.search(r"Generated (\S+) from the live", md)
    assert generated, "docs/bead-inventory.md lost its 'Generated <ts>' line — regenerate it"
    assert generated.group(1) == doc["generated_at"], (
        "docs/bead-inventory.md and .json carry different generation timestamps "
        f"({generated.group(1)} vs {doc['generated_at']}) — one file was "
        "hand-edited or the pair was only half-regenerated; re-run "
        "scripts/regen-bead-inventory.sh and commit BOTH files"
    )

    # The summary line hard-wraps ("**N in\nprogress**"), so match flexibly;
    # the non-headline-status clause ("3 deferred") is optional and only
    # present when other_statuses is non-empty.
    counts = re.search(
        r"Summary at generation time:\s*\*\*(\d+)\s+open\*\*,\s*\*\*(\d+)\s+in\s*progress\*\*,"
        r"\s*(\d+)\s+closed(?:,\s*([^-—]+?))?\s*—\s*(\d+)\s+beads\s+total",
        md,
    )
    assert counts, "docs/bead-inventory.md lost its summary line — regenerate it"
    md_open, md_in_progress, md_closed, md_other_clause, md_total = counts.groups()
    assert (int(md_open), int(md_in_progress), int(md_closed), int(md_total)) == (
        summary["open"],
        summary["in_progress"],
        summary["closed"],
        summary["total_beads"],
    ), (
        f"docs/bead-inventory.md summary disagrees with the JSON snapshot — "
        "re-run scripts/regen-bead-inventory.sh and commit BOTH files"
    )
    md_others = dict(
        (status, int(n))
        for n, status in re.findall(r"(\d+)\s+(\w+)", md_other_clause or "")
    )
    assert md_others == summary["other_statuses"], (
        f"docs/bead-inventory.md non-headline status clause {md_others} "
        f"disagrees with the JSON snapshot {summary['other_statuses']} — "
        "re-run scripts/regen-bead-inventory.sh and commit BOTH files"
    )

    md_ids = set(re.findall(r"^\| `([\w-]+)` \|", md, re.M))
    json_ids = {b["id"] for b in doc["beads_not_closed"]}
    assert md_ids == json_ids, (
        f"not-closed bead IDs differ between the markdown table and the JSON "
        f"snapshot (only in md: {sorted(md_ids - json_ids)}, only in json: "
        f"{sorted(json_ids - md_ids)}) — the pair was only half-regenerated; "
        "re-run scripts/regen-bead-inventory.sh and commit BOTH files"
    )


def test_curated_docs_carry_no_bead_status_claims():
    offenders: list[str] = []
    for path in _lint_targets():
        text = path.read_text()
        for pattern in STATUS_CLAIM_PATTERNS:
            match = pattern.search(text)
            if match:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)!r}")
    assert not offenders, "\n".join(
        [
            "hand-written bead-status claims found in curated docs:",
            *(f"  - {offender}" for offender in offenders),
            "",
            "A status list pasted into prose is stale the moment it is "
            "committed. The live store (bead list) is authoritative and "
            "docs/bead-inventory.{md,json} is the only sanctioned snapshot "
            "(generated by scripts/regen-bead-inventory.sh). Replace the "
            "claim with a pointer to one of those.",
        ]
    )


def test_committed_snapshot_is_fresh_where_a_live_store_exists():
    if not (shutil.which("bead") and BEADS_DB.exists()):
        pytest.skip(
            "no live bead store (bead CLI + .beads/beads.db) — freshness is "
            "enforced only on workspace hosts where regeneration is possible; "
            "structural checks still ran"
        )

    generated_at = datetime.fromisoformat(
        json.loads(INVENTORY_JSON.read_text())["generated_at"].replace("Z", "+00:00")
    )
    age_days = (datetime.now(timezone.utc) - generated_at).days
    assert age_days <= MAX_AGE_DAYS, (
        f"docs/bead-inventory.json is {age_days} days old (max {MAX_AGE_DAYS}) "
        "and this host has the live store it should be regenerated from — run "
        "scripts/regen-bead-inventory.sh and commit BOTH regenerated files "
        "(the snapshot otherwise keeps getting cited as if it were current)"
    )


# --- regeneration cadence guards (bead ytt-134402d8) ------------------------


def _cadence(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CADENCE_SCRIPT), *args], capture_output=True, text=True, timeout=60
    )


def _snapshot_json(generated_at: datetime) -> str:
    return json.dumps({"generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")})


def test_cadence_age_bound_matches_the_suite_bound():
    text = CADENCE_SCRIPT.read_text()
    match = re.search(r"^MAX_AGE_DAYS=(\d+)$", text, re.M)
    assert match, "scripts/bead-inventory-cadence.sh lost its MAX_AGE_DAYS constant"
    assert int(match.group(1)) == MAX_AGE_DAYS, (
        f"the cadence's staleness bound ({match.group(1)}d) diverged from this "
        f"module's ({MAX_AGE_DAYS}d) — the suite leg would then enforce a "
        "different freshness contract than the self-label `age` prints; "
        "update both together"
    )


def test_age_banner_self_labels_fresh_boundary_and_stale_snapshots(tmp_path):
    now = datetime.now(timezone.utc)
    fresh = tmp_path / "fresh.json"
    fresh.write_text(_snapshot_json(now))
    boundary = tmp_path / "boundary.json"
    boundary.write_text(_snapshot_json(now - timedelta(days=MAX_AGE_DAYS)))
    stale = tmp_path / "stale.json"
    stale.write_text(_snapshot_json(now - timedelta(days=MAX_AGE_DAYS + 1)))

    fresh_run = _cadence("age", str(fresh))
    assert fresh_run.returncode == 0, fresh_run.stdout + fresh_run.stderr
    assert "0 days old" in fresh_run.stdout and "fresh" in fresh_run.stdout

    boundary_run = _cadence("age", str(boundary))
    assert boundary_run.returncode == 0, boundary_run.stdout + boundary_run.stderr
    assert f"{MAX_AGE_DAYS} days old" in boundary_run.stdout, (
        "the bound itself must still read fresh (suite leg asserts <= MAX_AGE_DAYS)"
    )

    stale_run = _cadence("age", str(stale))
    assert stale_run.returncode == 1
    assert "STALE" in stale_run.stdout and f"(bound: {MAX_AGE_DAYS})" in stale_run.stdout


def test_age_banner_fails_loudly_on_a_missing_or_unreadable_snapshot(tmp_path):
    missing = _cadence("age", str(tmp_path / "nope.json"))
    assert missing.returncode == 1
    assert "does not exist" in missing.stdout + missing.stderr

    unreadable = tmp_path / "garbage.json"
    unreadable.write_text("{}")
    garbage = _cadence("age", str(unreadable))
    assert garbage.returncode == 1
    assert "not a readable snapshot" in garbage.stdout + garbage.stderr


def test_cadence_units_stay_wired_to_the_script():
    assert os.access(CADENCE_SCRIPT, os.X_OK), (
        "scripts/bead-inventory-cadence.sh lost its exec bit — the systemd "
        "unit ExecStarts it directly"
    )
    service = CADENCE_SERVICE_UNIT.read_text()
    exec_line = re.search(r"^ExecStart=(.*)$", service, re.M)
    assert exec_line, f"{CADENCE_SERVICE_UNIT.name} lost its ExecStart"
    assert "bead-inventory-cadence.sh run" in exec_line.group(1), (
        "the service unit no longer runs the cadence's scheduled path — "
        "update the unit together with the script"
    )
    timer = CADENCE_TIMER_UNIT.read_text()
    assert re.search(r"^OnUnitActiveSec=\d", timer, re.M), (
        f"{CADENCE_TIMER_UNIT.name} lost its interval — the cadence would "
        "never fire"
    )


def test_cadence_note_step_list_matches_script_markers():
    expected = [
        "dirty-skip",
        "regenerate-restore",
        "data-change",
        "heartbeat",
        "publish-rollback",
    ]
    note_steps = re.findall(r"cadence-step:\s*([a-z0-9-]+)", CADENCE_NOTE.read_text())
    script_steps = re.findall(
        r"^\s*#\s*cadence-step:\s*([a-z0-9-]+)$", CADENCE_SCRIPT.read_text(), re.M
    )
    assert note_steps == expected, (
        "the cadence note's numbered decision steps changed without updating "
        f"the guarded step IDs: {note_steps!r}"
    )
    assert script_steps == expected, (
        "the cadence script's decision markers changed without updating the "
        f"documented workflow: {script_steps!r}"
    )
