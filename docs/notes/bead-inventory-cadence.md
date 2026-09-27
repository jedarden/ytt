# Bead-inventory regeneration cadence

`docs/bead-inventory.md` + `docs/bead-inventory.json` are a committed
point-in-time snapshot of the live bead-rs store, generated only by
`scripts/regen-bead-inventory.sh`. The snapshot rotted once for real: the
2026-08-21 pair still claimed to be current three weeks later, and a
commissioning bead quoted an open-bead count an order of magnitude above
the live store's — the misread recorded in the inventory's own History
(bead `ytt-cbbdf5c7` regenerated the pair; bead `ytt-134402d8` commissioned
the cadence described here so no one has to remember again).

## What the exclusion still stands

The snapshot is deliberately **not** refreshed by
`scripts/definition-of-done.sh`. The recorded rationale is unchanged: a
clean extraction (which is where the DoD is authoritatively re-run) has no
`.beads/beads.db` to regenerate from, and regenerating on every worker's
local test run would dirty the shared tree with transient store state. This
cadence respects that — it is a separate scheduled path, and the suite's
freshness leg stays a *backstop* that fires only if the cadence dies.

## Designated host: codinghome

The timer runs on exactly one host: **codinghome** (`/home/coding/ytt`).
It is the only place the cadence *can* run — the live bead store this
snapshot is regenerated from lives in that checkout — and it already has
`Linger=yes`, so its systemd `--user` manager runs without a login session
(the established house pattern: this box schedules all of its fleet-local
maintenance as `--user` timers). The committed units
(`scripts/systemd/ytt-bead-inventory-regen.{service,timer}`) hardcode that
path deliberately; moving the checkout means editing the units and
re-running the install (below).

A host-local timer is also the *only* sanctioned scheduler shape here: the
org-wide rules forbid k8s CronJobs outright (ArgoCD cannot manage them
idempotently, their pods are never pruned), and the store is local to this
host anyway.

## Mechanics — `scripts/bead-inventory-cadence.sh run`

Each tick (daily; `OnBootSec=10min`, `OnUnitActiveSec=1d`):

1. **Skip if the pair is dirty.** Uncommitted changes to the pair mean a
   worker is mid-regeneration or about to commit their own refresh — the
   tick refuses to commit or discard someone else's in-flight work.
2. **Regenerate from the live store** via
   `scripts/regen-bead-inventory.sh`. On failure the pair is restored to
   its committed state before exiting nonzero — a half-written pair is
   never left behind.
3. **Commit only on a data change.** The old and new JSON payloads are
   compared with the per-run metadata (`generated_at`, `workspace`)
   stripped; timestamp-only churn on a quiet store is discarded and the
   worktree is left byte-identical. Most ticks therefore touch nothing.
4. **Heartbeat at most every 7 days.** A quiet store still gets a
   liveness commit (a `generated_at` bump) at least weekly — half the
   suite's 14-day freshness bound — so the backstop can never trip on a
   healthy-but-silent cadence. The regenerator collapses consecutive
   same-count History bullets, so heartbeats do not bloat the audit trail.
5. **Pathspec-limited commit + push.** The commit carries only
   `docs/bead-inventory.{md,json}` under the fleet identity
   (`jedarden` / `github@jedarden.com`) — never the box's ever-dirty
   `.beads/checkpoint` churn — and pushes to Forgejo `origin`. A rejected
   push is reconciled with a merge commit (never a force-push); if the
   publish still fails, the tick rolls its own commit back and retries
   next tick.

Every decision line lands in the journal under
`ytt-bead-inventory-regen`, so a skipped or failed tick is diagnosable
without touching the repo.

## Reading the snapshot safely

The snapshot self-labels its freshness at read time:
`scripts/bead-inventory-cadence.sh age` prints the generated timestamp and
age against the 14-day bound (kept drift-locked to
`tests/unit/test_bead_inventory_docs.py`'s `MAX_AGE_DAYS`), and exits
nonzero once stale. Run it before citing the inventory; when it says
STALE, regenerate and commit the pair (or wait for the tick).

## Operator runbook

```bash
scripts/bead-inventory-cadence.sh install                     # install + enable the timer
systemctl --user list-timers ytt-bead-inventory-regen.timer   # is it scheduled?
systemctl --user start ytt-bead-inventory-regen.service       # force a tick now
journalctl --user -u ytt-bead-inventory-regen -n 50           # what did ticks decide?
```

## Test coverage

`tests/unit/test_bead_inventory_docs.py` guards the cadence itself: its
`MAX_AGE_DAYS` must equal the suite's, its `age` subcommand must classify
fresh / at-bound / stale / missing / unreadable snapshots correctly
(fabricated fixtures, no store needed), the script must stay executable,
and the committed units must keep ExecStarting its `run` path on a real
interval. The structural legs run everywhere; the freshness leg still
skips where no live store exists.
