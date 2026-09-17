# Workspace Bead Inventory

> **Point-in-time snapshot — do not trust for planning.** This file records
> what the bead store looked like when it was generated (2026-09-17T15:43:03Z) and
> begins drifting the moment any worker opens, claims, or closes a bead. For
> live state, run `bead list` yourself. It is deliberately **not** refreshed
> by `scripts/definition-of-done.sh` — see the header of the regen script for
> why (no live store inside a clean extraction; constant tree churn locally).

Generated 2026-09-17T15:43:03Z from the live bead-rs store with:

```text
bead list --json --limit 1000
```

via `scripts/regen-bead-inventory.sh` (the only supported way to regenerate —
this file is fully generated, do not hand-edit).

Summary at generation time: **9 open**, **2 in
progress**, 54 closed — 65 beads total. The machine-readable
copy is [docs/bead-inventory.json](bead-inventory.json).

## Not-closed beads (11)

| ID | Title | Labels | Status |
| --- | --- | --- | --- |
| `ytt-58325cdf` | ytt: prove ardenone-cluster residential egress (plan Proof Obligation canary) | `failure-count:3`, `quarantine-until:2026-09-17T01:26:17.734133443+00:00`, `verification-failed`, `weave-generated` | open |
| `ytt-5a4e9a6e` | Add an automated deploy/ ↔ declarative-config manifest parity check (drift already happened once) | `failure-count:1`, `over-budget`, `quarantine-until:2026-09-16T20:33:13.474692427+00:00`, `verification-failed`, `weave-generated` | in_progress |
| `ytt-5b715a7f` | Add runtime tests for the single-replica flock guard | `failure-count:1`, `over-budget`, `quarantine-until:2026-09-17T00:50:11.101035190+00:00`, `verification-failed`, `weave-generated` | open |
| `ytt-70690b3d` | Pin and regression-test yt-dlp player-client and PoToken behavior | `degraded-window-failure:0`, `failure-count:1`, `quarantine-until:2026-09-17T11:08:38.244027619+00:00`, `weave-generated` | open |
| `ytt-80a4da40` | Add automated MCP OAuth discovery and audience/path conformance tests | `failure-count:1`, `over-budget`, `quarantine-until:2026-09-16T23:51:27.442189654+00:00`, `weave-generated` | open |
| `ytt-89d1e56d` | Add bounded download and Whisper resource guardrails | `failure-count:1`, `quarantine-until:2026-09-17T01:07:19.174841989+00:00`, `weave-generated` | open |
| `ytt-8a2beb88` | Gate broken: close_verification — close reason carries no verification evidence (fingerprint:ea603be160bd) | `degraded-window-failure:0`, `failure-count:1`, `fingerprint:ea603be160bd`, `infra`, `priority:0`, `quarantine-until:2026-09-17T13:12:43.040927133+00:00` | open |
| `ytt-8c702583` | Implement and test YTT_PROXY_URL end to end | `degraded-window-failure:0`, `failure-count:1`, `quarantine-until:2026-09-17T07:02:35.304318608+00:00`, `weave-generated` | open |
| `ytt-8efb9b9d` | Extend CHANGELOG/version metadata coverage to 0.2.13–0.2.14 | `failure-count:1`, `quarantine-until:2026-09-16T22:12:37.253887497+00:00`, `weave-generated` | open |
| `ytt-cbbdf5c7` | Regenerate docs/bead-inventory.md and its JSON snapshot (records 13 open beads vs ~45 actually open) | `failure-count:1`, `quarantine-until:2026-09-16T21:12:31.038261018+00:00`, `verification-failed`, `weave-generated` | in_progress |
| `ytt-f4064fab` | Document YTT_SCRATCH_DIR in the README configuration table | `failure-count:1`, `quarantine-until:2026-09-16T22:49:30.162116397+00:00`, `verification-failed`, `weave-generated` | open |

## Pluck visibility (label analysis)

Pluck's default exclude labels in the NEEDLE source at generation time were
`deferred`, `human`, `blocked`, `escalation`
(`DEFAULT_EXCLUDE_LABELS`, `src/strand/pluck.rs`). A bead carrying any of
them is invisible to Pluck's candidate pool:

- No not-closed bead carries any of Pluck's default exclude labels (`deferred`, `human`, `blocked`, `escalation`).

`quarantine-until` (ADR-022) excludes a bead only while the window is active;
expiry re-evaluates the conditions that caused the quarantine:

- `ytt-58325cdf` — window `2026-09-17T01:26:17.734133443+00:00` (expired at generation).
- `ytt-5a4e9a6e` — window `2026-09-16T20:33:13.474692427+00:00` (expired at generation).
- `ytt-5b715a7f` — window `2026-09-17T00:50:11.101035190+00:00` (expired at generation).
- `ytt-70690b3d` — window `2026-09-17T11:08:38.244027619+00:00` (expired at generation).
- `ytt-80a4da40` — window `2026-09-16T23:51:27.442189654+00:00` (expired at generation).
- `ytt-89d1e56d` — window `2026-09-17T01:07:19.174841989+00:00` (expired at generation).
- `ytt-8a2beb88` — window `2026-09-17T13:12:43.040927133+00:00` (expired at generation).
- `ytt-8c702583` — window `2026-09-17T07:02:35.304318608+00:00` (expired at generation).
- `ytt-8efb9b9d` — window `2026-09-16T22:12:37.253887497+00:00` (expired at generation).
- `ytt-cbbdf5c7` — window `2026-09-16T21:12:31.038261018+00:00` (expired at generation).
- `ytt-f4064fab` — window `2026-09-16T22:49:30.162116397+00:00` (expired at generation).

`failure-count:*`, `verification-failed`, `over-budget`, and `weave-generated`
are metadata, not exclude labels — they affect quarantine/retry behaviour but
do not by themselves hide a bead.

## History

- 2026-08-21 snapshot (superseded): 13 open.
- 2026-09-17T15:43:03Z (this snapshot): 9 open, 2 in progress, 54 closed.
- The bead that commissioned this regeneration quoted "~45 open beads" —
  that figure does not match the live store at generation time (9
  open); it is closest to the closed count (54), suggesting a
  closed/total count was misread as "open". Cite `bead list` output, not
  this file, when the current count matters.
- The previous machine-readable copy lived at `.beads/bead-inventory-open.json`
  and was removed: nothing under `.beads/` may be hand-edited, which made
  maintaining a snapshot there self-contradictory.
