# Workspace Bead Inventory

> **Point-in-time snapshot — do not trust for planning.** This file records
> what the bead store looked like when it was generated (2026-10-08T11:10:11Z) and
> begins drifting the moment any worker opens, claims, or closes a bead. For
> live state, run `bead list` yourself. It is deliberately **not** refreshed
> by `scripts/definition-of-done.sh` — see the header of the regen script for
> why (no live store inside a clean extraction; constant tree churn locally).
> A daily cadence on the designated host (`scripts/bead-inventory-cadence.sh
> run`, timer `ytt-bead-inventory-regen.timer` in `scripts/systemd/`)
> normally refreshes this pair; check `scripts/bead-inventory-cadence.sh
> age` before citing — it prints this snapshot's age and exits nonzero once
> it exceeds the freshness bound.

Generated 2026-10-08T11:10:11Z from the live bead-rs store with:

```text
bead list --json --limit 1000
```

via `scripts/regen-bead-inventory.sh` (the only supported way to regenerate —
this file is fully generated, do not hand-edit).

Summary at generation time: **2 open**, **2 in
progress**, 185 closed, 1 deferred — 190 beads total. The
machine-readable copy is [docs/bead-inventory.json](bead-inventory.json).

## Not-closed beads (5)

| ID | Title | Labels | Status |
| --- | --- | --- | --- |
| `ytt-1e4c448b` | prod: NetworkPolicy 'ytt' blocks ytt -> ytt-browser:3001, so browser fetch fails at connect and falls back to yt-dlp (429) | `fetch-core`, `ops` | in_progress |
| `ytt-cd9e04a5` | Run same-egress caption retest for videos that hit upstream 429s | `captions`, `deployment`, `diagnostics` | in_progress |
| `ytt-4f1c45c2` | Wire planned startup sequence into the server boot path (startup_scan, startup_sweep, TTL GC, reconcile loop never run) | `ops`, `startup` | open |
| `ytt-7656c3a2` | Add an anonymous container-image pull release gate | `failure-count:2`, `quarantine-until:2026-09-25T23:34:14.292819353+00:00`, `weave-generated` | deferred |
| `ytt-96bb54f6` | Verify yt-dlp 2026.8.19 clears the caption-track 429s in ardenone-cluster once a release carrying it is deployed | `human`, `verification`, `yt-dlp` | open |

## Pluck visibility (label analysis)

Pluck's default exclude labels in the NEEDLE source at generation time were
`deferred`, `human`, `blocked`, `escalation`
(`DEFAULT_EXCLUDE_LABELS`, `src/strand/pluck.rs`). A bead carrying any of
them is invisible to Pluck's candidate pool:

- `ytt-96bb54f6` carries `human` — excluded from Pluck's candidate pool.

`quarantine-until` (ADR-022) excludes a bead only while the window is active;
expiry re-evaluates the conditions that caused the quarantine:

- `ytt-7656c3a2` — window `2026-09-25T23:34:14.292819353+00:00` (expired at generation).

`failure-count:*`, `verification-failed`, `over-budget`, and `weave-generated`
are metadata, not exclude labels — they affect quarantine/retry behaviour but
do not by themselves hide a bead.

## History

- 2026-08-21 snapshot (superseded): 13 open.
- 2026-09-17T15:43:03Z: 9 open, 2 in progress, 54 closed.
- The bead that commissioned this regeneration quoted "~45 open beads" — that figure does not match the live store at generation time (9 open); it is closest to the closed count (54), suggesting a closed/total count was misread as "open". Cite `bead list` output, not this file, when the current count matters.
- The previous machine-readable copy lived at `.beads/bead-inventory-open.json` and was removed: nothing under `.beads/` may be hand-edited, which made maintaining a snapshot there self-contradictory.
- 2026-09-24T13:39:02Z: 8 open, 1 in progress, 75 closed.
- 2026-09-27T12:29:59Z: 8 open, 3 in progress, 143 closed, 3 deferred.
- 2026-09-27T12:38:47Z: 11 open, 3 in progress, 143 closed, 3 deferred.
- 2026-09-28T12:38:48Z: 1 open, 1 in progress, 176 closed, 2 deferred.
- 2026-09-29T11:10:09Z: 2 open, 3 in progress, 182 closed, 1 deferred.
- 2026-10-05T11:10:10Z: 2 open, 2 in progress, 183 closed, 1 deferred.
- 2026-10-07T11:10:11Z: 2 open, 3 in progress, 183 closed, 1 deferred.
- 2026-10-08T11:10:11Z (this snapshot): 2 open, 2 in progress, 185 closed, 1 deferred.
