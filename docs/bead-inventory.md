# Workspace Bead Inventory

> **Point-in-time snapshot — do not trust for planning.** This file records
> what the bead store looked like when it was generated (2026-09-16T20:55:32Z) and
> begins drifting the moment any worker opens, claims, or closes a bead. For
> live state, run `bead list` yourself. It is deliberately **not** refreshed
> by `scripts/definition-of-done.sh` — see the header of the regen script for
> why (no live store inside a clean extraction; constant tree churn locally).

Generated 2026-09-16T20:55:32Z from the live bead-rs store with:

```text
bead list --json --limit 1000
```

via `scripts/regen-bead-inventory.sh` (the only supported way to regenerate —
this file is fully generated, do not hand-edit).

Summary at generation time: **6 open**, **1 in
progress**, 48 closed — 55 beads total. The machine-readable
copy is [docs/bead-inventory.json](bead-inventory.json).

## Not-closed beads (7)

| ID | Title | Labels | Status |
| --- | --- | --- | --- |
| `ytt-58325cdf` | ytt: prove ardenone-cluster residential egress (plan Proof Obligation canary) | `failure-count:1`, `quarantine-until:2026-09-16T18:05:10.056041566+00:00`, `weave-generated` | open |
| `ytt-5a4e9a6e` | Add an automated deploy/ ↔ declarative-config manifest parity check (drift already happened once) | `failure-count:1`, `over-budget`, `quarantine-until:2026-09-16T20:33:13.474692427+00:00`, `verification-failed`, `weave-generated` | open |
| `ytt-6c1eef9d` | ytt: define and document per-subject rate-limit + Whisper-quota configuration surface | `failure-count:1`, `quarantine-until:2026-09-16T18:00:29.512344899+00:00`, `weave-generated` | open |
| `ytt-8efb9b9d` | Extend CHANGELOG/version metadata coverage to 0.2.13–0.2.14 | `weave-generated` | open |
| `ytt-b156b5ba` | Update docs/pluck-configuration.md for the bead-rs migration (it still documents the deprecated bf/br backend) | `failure-count:1`, `over-budget`, `quarantine-until:2026-09-16T19:52:26.077342633+00:00`, `verification-failed`, `weave-generated` | open |
| `ytt-cbbdf5c7` | Regenerate docs/bead-inventory.md and its JSON snapshot (records 13 open beads vs ~45 actually open) | `weave-generated` | in_progress |
| `ytt-f4064fab` | Document YTT_SCRATCH_DIR in the README configuration table | `weave-generated` | open |

## Pluck visibility (label analysis)

Pluck's default exclude labels in the NEEDLE source at generation time were
`deferred`, `human`, `blocked`, `escalation`
(`DEFAULT_EXCLUDE_LABELS`, `src/strand/pluck.rs`). A bead carrying any of
them is invisible to Pluck's candidate pool:

- No not-closed bead carries any of Pluck's default exclude labels (`deferred`, `human`, `blocked`, `escalation`).

`quarantine-until` (ADR-022) excludes a bead only while the window is active;
expiry re-evaluates the conditions that caused the quarantine:

- `ytt-58325cdf` — window `2026-09-16T18:05:10.056041566+00:00` (expired at generation).
- `ytt-5a4e9a6e` — window `2026-09-16T20:33:13.474692427+00:00` (expired at generation).
- `ytt-6c1eef9d` — window `2026-09-16T18:00:29.512344899+00:00` (expired at generation).
- `ytt-b156b5ba` — window `2026-09-16T19:52:26.077342633+00:00` (expired at generation).

`failure-count:*`, `verification-failed`, `over-budget`, and `weave-generated`
are metadata, not exclude labels — they affect quarantine/retry behaviour but
do not by themselves hide a bead.

## History

- 2026-08-21 snapshot (superseded): 13 open.
- 2026-09-16T20:55:32Z (this snapshot): 6 open, 1 in progress, 48 closed.
- The bead that commissioned this regeneration quoted "~45 open beads" —
  that figure does not match the live store at generation time (6
  open); it is closest to the closed count (48), suggesting a
  closed/total count was misread as "open". Cite `bead list` output, not
  this file, when the current count matters.
- The previous machine-readable copy lived at `.beads/bead-inventory-open.json`
  and was removed: nothing under `.beads/` may be hand-edited, which made
  maintaining a snapshot there self-contradictory.
