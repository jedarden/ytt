# Workspace Bead Inventory

> **Point-in-time snapshot — do not trust for planning.** This file records
> what the bead store looked like when it was generated (2026-09-27T12:38:47Z) and
> begins drifting the moment any worker opens, claims, or closes a bead. For
> live state, run `bead list` yourself. It is deliberately **not** refreshed
> by `scripts/definition-of-done.sh` — see the header of the regen script for
> why (no live store inside a clean extraction; constant tree churn locally).
> A daily cadence on the designated host (`scripts/bead-inventory-cadence.sh
> run`, timer `ytt-bead-inventory-regen.timer` in `scripts/systemd/`)
> normally refreshes this pair; check `scripts/bead-inventory-cadence.sh
> age` before citing — it prints this snapshot's age and exits nonzero once
> it exceeds the freshness bound.

Generated 2026-09-27T12:38:47Z from the live bead-rs store with:

```text
bead list --json --limit 1000
```

via `scripts/regen-bead-inventory.sh` (the only supported way to regenerate —
this file is fully generated, do not hand-edit).

Summary at generation time: **11 open**, **3 in
progress**, 143 closed, 3 deferred — 160 beads total. The
machine-readable copy is [docs/bead-inventory.json](bead-inventory.json).

## Not-closed beads (17)

| ID | Title | Labels | Status |
| --- | --- | --- | --- |
| `ytt-397baf79` | Operator: flip ghcr.io/jedarden/ytt to Public on GitHub, then verify anonymous pull (ytt-build cannot; GitHub UI only) | `ghcr`, `operator` | deferred |
| `ytt-058f0004` | Roll ardenone-cluster and the deploy/ mirror off the burned 0.2.22 pin onto 0.2.23 and record rollout evidence | `deferred:2026-09-26T21:38:12.565744678+00:00`, `failure-count:1`, `quarantine-until:2026-09-26T19:43:11.767691437+00:00`, `weave-generated` | in_progress |
| `ytt-134402d8` | Give docs/bead-inventory.md a regeneration cadence so the snapshot cannot silently go stale | `weave-generated` | in_progress |
| `ytt-183dc6ac` | Ship the derived-URL SSRF allowlist in a released image and roll ardenone-cluster off 0.2.20 | `failure-count:2`, `quarantine-until:2026-09-26T15:43:23.825205721+00:00`, `weave-generated` | in_progress |
| `ytt-4f1c45c2` | Wire planned startup sequence into the server boot path (startup_scan, startup_sweep, TTL GC, reconcile loop never run) | `ops`, `startup` | open |
| `ytt-56679d29` | Normalize container log timestamps to UTC (canary/server pods log in node-local EDT) | `failure-count:1`, `quarantine-until:2026-09-27T09:16:47.898413609+00:00`, `weave-generated` | open |
| `ytt-72dcf035` | Release 0.2.21: first image carrying the per-path canary metrics — activates the applied YttCanary* alert rules | `failure-count:3`, `observability`, `quarantine-until:2026-09-26T07:35:48.351711023+00:00` | open |
| `ytt-7656c3a2` | Add an anonymous container-image pull release gate | `failure-count:2`, `quarantine-until:2026-09-25T23:34:14.292819353+00:00`, `weave-generated` | deferred |
| `ytt-7829b244` | Add MCP Streamable HTTP session-lifecycle conformance tests | `deferred:2026-09-27T15:17:45.528610755+00:00`, `failure-count:3`, `quarantine-until:2026-09-27T07:37:44.635570145+00:00`, `weave-generated` | open |
| `ytt-7ce8d590` | Document the full canary CLI flag surface (--once/--gate/--via-proxy/--evidence-dir/--video-id) in the README | `weave-generated` | open |
| `ytt-958dccc1` | Make canary-gate evidence artifacts survive their run environment (default /tmp dir, CI podGC) and give them an operator retention story | `weave-generated` | open |
| `ytt-96bb54f6` | Verify yt-dlp 2026.8.19 clears the caption-track 429s in ardenone-cluster once a release carrying it is deployed | `verification`, `yt-dlp` | deferred |
| `ytt-ab79a7e8` | Test caption-only operation without YTT_WHISPER_URL | `failure-count:2`, `quarantine-until:2026-09-27T04:16:13.544954872+00:00`, `weave-generated` | open |
| `ytt-b0afb5b4` | Pin the README claim that every YouTube URL form normalizes to the same cache entry | `deferred:2026-09-27T13:49:14.772314935+00:00`, `failure-count:1`, `quarantine-until:2026-09-27T11:54:13.878819040+00:00`, `weave-generated` | open |
| `ytt-e1036a3b` | Record an explicit accept-or-mitigate decision for the DNS-rebinding residual in the derived-URL allowlist (no DNS resolution in the gate) | `weave-generated` | open |
| `ytt-317fa8df` | Fix remaining exec-through-proxy instructions (deploy-ardenone.md integration path, CONTRIBUTING.md) | — | open |
| `ytt-4d76a316` | Correct deploy/RUNBOOK.md exec-through-proxy claims (live RBAC forbids pods/exec on the credential-free endpoint) | `docs`, `k8s` | open |

## Pluck visibility (label analysis)

Pluck's default exclude labels in the NEEDLE source at generation time were
`deferred`, `human`, `blocked`, `escalation`
(`DEFAULT_EXCLUDE_LABELS`, `src/strand/pluck.rs`). A bead carrying any of
them is invisible to Pluck's candidate pool:

- `ytt-058f0004` carries `deferred:2026-09-26T21:38:12.565744678+00:00` — excluded from Pluck's candidate pool.
- `ytt-7829b244` carries `deferred:2026-09-27T15:17:45.528610755+00:00` — excluded from Pluck's candidate pool.
- `ytt-b0afb5b4` carries `deferred:2026-09-27T13:49:14.772314935+00:00` — excluded from Pluck's candidate pool.

`quarantine-until` (ADR-022) excludes a bead only while the window is active;
expiry re-evaluates the conditions that caused the quarantine:

- `ytt-058f0004` — window `2026-09-26T19:43:11.767691437+00:00` (expired at generation).
- `ytt-183dc6ac` — window `2026-09-26T15:43:23.825205721+00:00` (expired at generation).
- `ytt-56679d29` — window `2026-09-27T09:16:47.898413609+00:00` (expired at generation).
- `ytt-72dcf035` — window `2026-09-26T07:35:48.351711023+00:00` (expired at generation).
- `ytt-7656c3a2` — window `2026-09-25T23:34:14.292819353+00:00` (expired at generation).
- `ytt-7829b244` — window `2026-09-27T07:37:44.635570145+00:00` (expired at generation).
- `ytt-ab79a7e8` — window `2026-09-27T04:16:13.544954872+00:00` (expired at generation).
- `ytt-b0afb5b4` — window `2026-09-27T11:54:13.878819040+00:00` (expired at generation).

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
- 2026-09-27T12:38:47Z (this snapshot): 11 open, 3 in progress, 143 closed, 3 deferred.
