#!/usr/bin/env bash
# Regenerate docs/bead-inventory.md and docs/bead-inventory.json from the live
# bead-rs store. Both files are fully generated — never hand-edit them; change
# this script and re-run instead (the previous hand-maintained snapshot lived
# under .beads/, which must never be hand-edited, and drifted immediately).
#
# Deliberately NOT wired into scripts/definition-of-done.sh:
#   - NEEDLE's close verification re-runs the DoD script inside a clean
#     extraction of the repo, where no live beads.db exists (*.db is
#     gitignored via .beads/.gitignore) — regeneration there would fail or
#     rebuild from a stale checkpoint.
#   - Locally, every test run would rewrite committed files with transient
#     store state, dirtying the tree for every worker sharing the checkout.
# The docs therefore carry an explicit point-in-time caveat instead. Run this
# on demand when a current snapshot is needed, and commit the result when it
# is going to be cited. A staleness gate bounds the "on demand" habit:
# tests/unit/test_bead_inventory_docs.py fails the DoD suite where a live
# bead store exists if the committed snapshot is older than 14 days —
# regenerate (and commit BOTH files) when that gate trips.
#
# Requires: bead (bead-rs CLI — this workspace's declared backend per
# .needle.yaml), python3.
set -euo pipefail
cd "$(dirname "$0")/.."

GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
COMMAND='bead list --json --limit 1000'

# One JSONL dump, so all counts derive from a single consistent read even if
# other workers mutate the store while this runs.
jsonl="$(mktemp -t bead-inventory.XXXXXX.jsonl)"
trap 'rm -f "$jsonl"' EXIT
$COMMAND > "$jsonl"

python3 - "$jsonl" "$GENERATED_AT" "$COMMAND" <<'PYEOF'
import json
import re
import sys
from datetime import datetime
from collections import Counter
from pathlib import Path

jsonl_path, generated_at, command = sys.argv[1], sys.argv[2], sys.argv[3]

# Verified against NEEDLE source src/strand/pluck.rs:28 (DEFAULT_EXCLUDE_LABELS)
# at the time this script was written. If NEEDLE grows another default exclude
# label, update this list — the doc cites it as Pluck's visibility filter.
PLUCK_DEFAULT_EXCLUDES = ["deferred", "human", "blocked", "escalation"]

beads = [json.loads(line) for line in open(jsonl_path) if line.strip()]
counts = Counter(b.get("status") for b in beads)
total = len(beads)
open_n = counts.get("open", 0)
in_progress_n = counts.get("in_progress", 0)
closed_n = counts.get("closed", 0)
active = [b for b in beads if b.get("status") != "closed"]
active.sort(key=lambda b: (b.get("priority", 9), b.get("id", "")))

def iso(value):
    """Parse RFC3339 with Z or +00:00 suffix; None (-> falls back to raw
    string comparison) on hosts/pythons that can't parse it."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def window_active(window, now_iso):
    w, n = iso(window), iso(now_iso)
    if w is None or n is None:
        return window > now_iso  # same-format string comparison
    return w > n

def fmt_labels(b):
    return ", ".join(f"`{l}`" for l in sorted(b.get("labels", []))) or "—"

def esc(s):
    return s.replace("|", "\\|")

# --- Label analysis -------------------------------------------------------
matching_excludes = []
quarantined = []
for b in active:
    labels = b.get("labels", [])
    hits = [l for l in labels if l.split(":", 1)[0] in PLUCK_DEFAULT_EXCLUDES]
    if hits:
        matching_excludes.append((b, hits))
    for l in labels:
        if l.startswith("quarantine-until:"):
            window = l.split(":", 1)[1]
            state = "active" if window_active(window, generated_at) else "expired"
            quarantined.append((b, window, state))

# --- JSON snapshot --------------------------------------------------------
snapshot = {
    "generated_at": generated_at,
    "workspace": str(Path.cwd()),
    "command": command,
    "point_in_time_notice": (
        "Snapshot of the bead store at generated_at. The live store drifts "
        "immediately; do not use these counts for planning. Regenerate with "
        "scripts/regen-bead-inventory.sh."
    ),
    "summary": {
        "total_beads": total,
        "open": open_n,
        "in_progress": in_progress_n,
        "closed": closed_n,
    },
    "beads_not_closed": [
        {
            "id": b["id"],
            "title": b["title"],
            "status": b.get("status"),
            "effective_status": b.get("effective_status"),
            "priority": b.get("priority"),
            "assignee": b.get("assignee"),
            "labels": sorted(b.get("labels", [])),
            "manual_blocked": b.get("manual_blocked", False),
            "dependencies": b.get("dependencies", []),
            "revision": b.get("revision"),
            "created_at": b.get("created_at"),
            "updated_at": b.get("updated_at"),
        }
        for b in active
    ],
    "pluck_label_analysis": {
        "default_exclude_labels": PLUCK_DEFAULT_EXCLUDES,
        "beads_matching_default_excludes": [
            {"id": b["id"], "matching_labels": hits} for b, hits in matching_excludes
        ],
        "quarantine_windows": [
            {"id": b["id"], "until": window, "state": state}
            for b, window, state in quarantined
        ],
    },
}
json_doc = Path("docs/bead-inventory.json")
tmp = json_doc.with_suffix(".json.tmp")
tmp.write_text(json.dumps(snapshot, indent=2) + "\n")
tmp.replace(json_doc)

# --- Markdown doc ---------------------------------------------------------
rows = "\n".join(
    f"| `{b['id']}` | {esc(b['title'])} | {fmt_labels(b)} | {b.get('status')} |"
    for b in active
)

if matching_excludes:
    excl_lines = "\n".join(
        f"- `{b['id']}` carries {', '.join(f'`{h}`' for h in hits)} — excluded from Pluck's candidate pool."
        for b, hits in matching_excludes
    )
else:
    excl_lines = (
        f"- No not-closed bead carries any of Pluck's default exclude labels "
        f"({', '.join('`%s`' % l for l in PLUCK_DEFAULT_EXCLUDES)})."
    )

if quarantined:
    quar_lines = "\n".join(
        f"- `{b['id']}` — window `{window}` ({state} at generation)."
        for b, window, state in quarantined
    )
else:
    quar_lines = "- No not-closed bead carries a `quarantine-until` label."

# --- History (accumulated across regenerations) ---------------------------
# Preserve the bullets from the previously committed snapshot — demoting its
# "(this snapshot)" marker to a plain dated line — so History is an audit
# trail instead of a single-slot overwrite. Static context bullets survive
# via preservation; the fallback covers a first-generation (or deleted) file.
STATIC_HISTORY = [
    "- 2026-08-21 snapshot (superseded): 13 open.",
    (
        '- The bead that commissioned this regeneration quoted "~45 open '
        'beads" — that figure did not match the live store then either; a '
        "closed/total count had been misread as \"open\". Cite `bead list` "
        "output, not this file, when the current count matters."
    ),
    (
        "- The previous machine-readable copy lived at "
        "`.beads/bead-inventory-open.json` and was removed: nothing under "
        "`.beads/` may be hand-edited, which made maintaining a snapshot "
        "there self-contradictory."
    ),
]


def prior_history(md_path: Path) -> list:
    if not md_path.exists():
        return []
    text = md_path.read_text()
    if "## History" not in text:
        return []
    section = text.split("## History", 1)[1].split("\n## ", 1)[0]
    bullets = []
    for line in section.splitlines():
        if line.startswith("- "):
            bullets.append(line)
        elif line[:1] in (" ", "\t") and bullets and line.strip():
            # continuation of a wrapped bullet — rejoin onto one line
            bullets[-1] += " " + line.strip()
        elif line.strip():
            break  # first non-bullet, non-indented line ends the list
    demoted = []
    for b in bullets:
        m = re.match(r"- (\S+) \(this snapshot\): (.*)", b)
        demoted.append(f"- {m.group(1)}: {m.group(2)}" if m else b)
    return list(dict.fromkeys(demoted))  # dedupe, keep order


history = prior_history(Path("docs/bead-inventory.md")) or STATIC_HISTORY
history.append(
    f"- {generated_at} (this snapshot): {open_n} open, {in_progress_n} in "
    f"progress, {closed_n} closed."
)
history_text = "\n".join(history)

doc = f"""# Workspace Bead Inventory

> **Point-in-time snapshot — do not trust for planning.** This file records
> what the bead store looked like when it was generated ({generated_at}) and
> begins drifting the moment any worker opens, claims, or closes a bead. For
> live state, run `bead list` yourself. It is deliberately **not** refreshed
> by `scripts/definition-of-done.sh` — see the header of the regen script for
> why (no live store inside a clean extraction; constant tree churn locally).

Generated {generated_at} from the live bead-rs store with:

```text
{command}
```

via `scripts/regen-bead-inventory.sh` (the only supported way to regenerate —
this file is fully generated, do not hand-edit).

Summary at generation time: **{open_n} open**, **{in_progress_n} in
progress**, {closed_n} closed — {total} beads total. The machine-readable
copy is [docs/bead-inventory.json](bead-inventory.json).

## Not-closed beads ({len(active)})

| ID | Title | Labels | Status |
| --- | --- | --- | --- |
{rows}

## Pluck visibility (label analysis)

Pluck's default exclude labels in the NEEDLE source at generation time were
{', '.join('`%s`' % l for l in PLUCK_DEFAULT_EXCLUDES)}
(`DEFAULT_EXCLUDE_LABELS`, `src/strand/pluck.rs`). A bead carrying any of
them is invisible to Pluck's candidate pool:

{excl_lines}

`quarantine-until` (ADR-022) excludes a bead only while the window is active;
expiry re-evaluates the conditions that caused the quarantine:

{quar_lines}

`failure-count:*`, `verification-failed`, `over-budget`, and `weave-generated`
are metadata, not exclude labels — they affect quarantine/retry behaviour but
do not by themselves hide a bead.

## History

{history_text}
"""

md_doc = Path("docs/bead-inventory.md")
tmp = md_doc.with_suffix(".md.tmp")
tmp.write_text(doc)
tmp.replace(md_doc)

print(f"wrote docs/bead-inventory.md and docs/bead-inventory.json "
      f"(open={open_n} in_progress={in_progress_n} closed={closed_n} total={total})")
PYEOF
