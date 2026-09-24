# Workspace Bead Inventory

> **Point-in-time snapshot — do not trust for planning.** This file records
> what the bead store looked like when it was generated (2026-09-24T13:39:02Z) and
> begins drifting the moment any worker opens, claims, or closes a bead. For
> live state, run `bead list` yourself. It is deliberately **not** refreshed
> by `scripts/definition-of-done.sh` — see the header of the regen script for
> why (no live store inside a clean extraction; constant tree churn locally).

Generated 2026-09-24T13:39:02Z from the live bead-rs store with:

```text
bead list --json --limit 1000
```

via `scripts/regen-bead-inventory.sh` (the only supported way to regenerate —
this file is fully generated, do not hand-edit).

Summary at generation time: **8 open**, **1 in
progress**, 75 closed — 84 beads total. The machine-readable
copy is [docs/bead-inventory.json](bead-inventory.json).

## Not-closed beads (9)

| ID | Title | Labels | Status |
| --- | --- | --- | --- |
| `ytt-026fdbb4` | Define a post-deploy canary acceptance workflow | `weave-generated` | open |
| `ytt-042fee08` | Reconcile plan.md’s current bead list with live bead state | `weave-generated` | in_progress |
| `ytt-4f1c45c2` | Wire planned startup sequence into the server boot path (startup_scan, startup_sweep, TTL GC, reconcile loop never run) | `ops`, `startup` | open |
| `ytt-71e846a6` | Resync deploy/ mirror with declarative-config (canary Deployment/Service + 3 files drifted) | — | open |
| `ytt-c4205423` | Make the upstream OIDC provider configurable instead of hardcoded to sso.ardenone.com | `weave-generated` | open |
| `ytt-cf8157a2` | Add a built-image self-hosting smoke test | `weave-generated` | open |
| `ytt-d18f0ab1` | Reconcile release metadata 0.2.15–0.2.20 (CHANGELOG coverage + README quick-start tag) and add a drift guard | `weave-generated` | open |
| `ytt-e21eb9be` | Add a no-third-party transcript API regression guard | `weave-generated` | open |
| `ytt-f77d1be4` | Pre-register ytt_fetch_blocks_total so metric absence is unambiguous (canary exports sibling zero-counters but not this one) | `weave-generated` | open |

## Pluck visibility (label analysis)

Pluck's default exclude labels in the NEEDLE source at generation time were
`deferred`, `human`, `blocked`, `escalation`
(`DEFAULT_EXCLUDE_LABELS`, `src/strand/pluck.rs`). A bead carrying any of
them is invisible to Pluck's candidate pool:

- No not-closed bead carries any of Pluck's default exclude labels (`deferred`, `human`, `blocked`, `escalation`).

`quarantine-until` (ADR-022) excludes a bead only while the window is active;
expiry re-evaluates the conditions that caused the quarantine:

- No not-closed bead carries a `quarantine-until` label.

`failure-count:*`, `verification-failed`, `over-budget`, and `weave-generated`
are metadata, not exclude labels — they affect quarantine/retry behaviour but
do not by themselves hide a bead.

## History

- 2026-08-21 snapshot (superseded): 13 open.
- 2026-09-17T15:43:03Z: 9 open, 2 in progress, 54 closed.
- The bead that commissioned this regeneration quoted "~45 open beads" — that figure does not match the live store at generation time (9 open); it is closest to the closed count (54), suggesting a closed/total count was misread as "open". Cite `bead list` output, not this file, when the current count matters.
- The previous machine-readable copy lived at `.beads/bead-inventory-open.json` and was removed: nothing under `.beads/` may be hand-edited, which made maintaining a snapshot there self-contradictory.
- 2026-09-24T13:39:02Z (this snapshot): 8 open, 1 in progress, 75 closed.
