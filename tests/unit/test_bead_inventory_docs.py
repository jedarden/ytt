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

``scripts/definition-of-done.sh`` runs these as part of the unit suite.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_MD = REPO_ROOT / "docs" / "bead-inventory.md"
INVENTORY_JSON = REPO_ROOT / "docs" / "bead-inventory.json"
BEADS_DB = REPO_ROOT / ".beads" / "beads.db"

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

    assert all(b["status"] in NOT_CLOSED_STATUSES for b in listed), (
        "docs/bead-inventory.json lists a closed bead under beads_not_closed — "
        "regenerate with scripts/regen-bead-inventory.sh"
    )
    assert len(ids) == len(set(ids)), "duplicate IDs in beads_not_closed"
    assert summary["open"] == sum(1 for b in listed if b["status"] == "open")
    assert summary["in_progress"] == sum(1 for b in listed if b["status"] == "in_progress")
    assert summary["open"] + summary["in_progress"] == len(listed)
    assert summary["total_beads"] == summary["open"] + summary["in_progress"] + summary["closed"]


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

    # The summary line hard-wraps ("**N in\nprogress**"), so match flexibly.
    counts = re.search(
        r"Summary at generation time:\s*\*\*(\d+)\s+open\*\*,\s*\*\*(\d+)\s+in\s*progress\*\*,"
        r"\s*(\d+)\s+closed\s+—\s*(\d+)\s+beads\s+total",
        md,
    )
    assert counts, "docs/bead-inventory.md lost its summary line — regenerate it"
    md_counts = tuple(int(g) for g in counts.groups())
    json_counts = (
        summary["open"],
        summary["in_progress"],
        summary["closed"],
        summary["total_beads"],
    )
    assert md_counts == json_counts, (
        f"docs/bead-inventory.md summary {md_counts} disagrees with the JSON "
        f"snapshot {json_counts} — re-run scripts/regen-bead-inventory.sh and "
        "commit BOTH files"
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
