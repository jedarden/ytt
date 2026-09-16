# Pluck Configuration Documentation

## Overview

Pluck is the primary bead selection strand in NEEDLE that handles >90% of all bead processing. It queries the bead store for unassigned, ready beads, filters by excluded labels, and returns them in deterministic priority order.

**Source**: NEEDLE fleet orchestrator (`~/NEEDLE/src/strand/pluck.rs`)

## Backend: bead-rs

**The ytt workspace uses bead-rs** (`bead` CLI), the canonical bead backend across
this environment since 2026-08-14. `bead-forge` (`bf`) is deprecated and **no
longer installed on this box**.

| | Value |
|---|---|
| Backend | `bead-rs` (declared in `.needle.yaml`: `bead_cli: backend: bead-rs`) |
| CLI | `bead` (`~/.cargo/bin/bead`, v0.2.6 at time of writing) |
| Live store | `.beads/beads.db` (SQLite) |
| Durable checkpoint | `.beads/checkpoint/` (`current.json`, `forensic.jsonl`, `objects/*.jsonl`) |
| Workspace identity | `.beads/config.json` (prefix `ytt`) |

`~/.local/bin/br` still exists but is a **symlink to `bead`** — it is a
deprecated alias, not bead-forge. Use `bead` in scripts and docs.

**Do not run `bf` (or any bf-shaped recovery recipe) against this workspace.**
This store is bead-rs; bf fails against it with a misleading generic SQLite
"no such column" error, and applying the other tool's corruption-recovery
recipe silently reinitializes the store with the wrong schema and destroys
live data (this happened to SEAM on 2026-08-14). If a bead command fails with
an unrecognized schema/column error, **stop and check the backend before
attempting any repair.**

## Durability: checkpoint auto-flush

SQLite is the live store; the git-tracked checkpoint is the durable copy.
Every successful mutation publishes the checkpoint automatically after its
transaction commits (bead-rs R026 activation, 2026-08-21). A stale checkpoint
therefore means publication was suppressed (`--no-auto-flush`, or
`checkpoint.auto_flush` in `.beads/config.json`) or failed after a committed
mutation — not that someone forgot a command.

```bash
bead sync flush-only   # idempotent check: database -> checkpoint
```

Recovering a broken or fresh-clone workspace (lossless while the checkpoint
was flushed recently):

```bash
bead doctor                      # read-only diagnostics; --repair for safe auto-repairs
bead init                        # rebuild schema, keeps committed workspace identity
bead sync import-only --input .beads/checkpoint/forensic.jsonl \
  --restore-into-empty --actor <you>
```

## Configuration Settings

### Workspace: `/home/coding/ytt`

The ytt workspace uses the default Pluck configuration via bead-rs (no custom
Pluck settings in `.needle.yaml`).

### 1. Default Exclude Labels

**Location**: Hardcoded in PluckStrand implementation

**Default values** (when no custom configuration is provided):

```rust
const DEFAULT_EXCLUDE_LABELS: &[&str] = &["deferred", "human", "blocked", "escalation"];
```

- `deferred` - Beads marked for later processing
- `human` - Beads requiring human intervention
- `blocked` - Beads blocked by dependencies
- `escalation` - Work the fleet has already failed to move (N-T60): applied
  automatically when an audit finding escalates instead of filing ordinary
  work, precisely because workers did not advance it — letting a worker claim
  the escalation would hand the problem back to the thing that caused it

**How it works**: When PluckStrand is initialized with an empty exclude_labels
vector, these defaults are automatically applied.

**Current configuration in ytt**: Using defaults (no custom exclude_labels
configured).

### 2. Custom Exclude Labels

**How to configure**: Provide a custom vector of labels when creating PluckStrand

```rust
let strand = PluckStrand::new(vec!["wip".to_string(), "review".to_string()], telemetry);
```

**Behavior**: When custom exclude_labels are provided, they completely replace
the defaults. The default labels are **not** merged with custom ones.

### 3. Split Threshold (Auto-split Configuration)

**Purpose**: Automatically trigger bead splitting when a bead accumulates too many consecutive failures

**Default value**: `3` (beads are split after 3 consecutive failures)

**How it works**:
- Pluck extracts failure counts from bead labels following the pattern `failure-count:N`
- When the first candidate bead's failure count >= threshold, Pluck returns a `Split` result instead of `BeadFound`
- Threshold of `0` disables auto-split

**Configuration methods**:
```rust
// Default threshold (3 failures)
let strand = PluckStrand::new(vec![], telemetry);

// Custom threshold
let strand = PluckStrand::with_split_threshold(vec![], 5, telemetry);

// Disabled
let strand = PluckStrand::with_split_threshold(vec![], 0, telemetry);
```

**Current configuration in ytt**: Using default threshold of 3

### 4. Quarantine Threshold (ADR-022)

**Purpose**: Time-bounded quarantine for repeatedly failing beads, with an
escalation ladder whose last rung is a human

**Default value**: `5` (`outcome.quarantine_after_failures` in production
wiring; the hand-constructed-strand fallback constant is also 5, and `0`
disables quarantine)

**How it works**: When a bead exceeds the failure threshold it enters a
visible, time-bounded quarantine instead of cycling straight back into the
pool. Pluck re-evaluates an expired quarantine window against the threshold
before the bead re-enters selection (`quarantine_expiry`), and re-quarantines
it if the failure count still exceeds the threshold. Closed auto-split parents
are reconciled out of selection beforehand (`mitosis`).

### 5. Sorting Order (Deterministic Priority)

**Hardcoded behavior** - not configurable

Pluck sorts candidates by:

```text
(effective_priority ASC, pinned_bucket ASC, failure_count ASC, created_at ASC, id ASC)
```

1. **effective_priority** (ASC) - min of the bead's own priority and the
   priorities of all transitively blocked open beads (priority inheritance),
   aged upward over time so old P2s eventually surface
2. **pinned_bucket** (ASC) - `-floor(log2(1 + transitive_dependent_count))`,
   so beads blocking more open dependents sort earlier and stuck chains get
   unblocked first
3. **failure_count** (ASC) - prevents struggling beads from monopolizing slot 1
4. **Created at** (ASC) - Older beads first
5. **Bead ID** (ASC) - Lexicographic tie-breaker for determinism

**Why this matters**: Given the same queue state, every worker computes the same candidate list. This enables coordination without central server state.

### 6. Relaxation Tiers (Empty-Ready Fallback)

When the normal ready query returns nothing, Pluck relaxes constraints in
tiers rather than immediately reporting no work:

`initial` → `worker-labels` → `priority` → `status-only` → `oldest-open`

**Never dropped by any tier**: assignee (claim ownership, ADR-018),
dependency safety, `blocked`/`deferred`/`human` labels, and active quarantine.

### 7. Additional Filters (Defensive Guards)

These filters are applied **after** the bead store query and are not configurable:

**Filtered out**:
- Beads with any excluded label (defensive guard against stores that don't apply label filtering)
- Beads in `in_progress` status (claimed by another worker)
- Open beads with a stale assignee (Open + assignee != None = not claimable)

**Rationale**: These beads would cause the claimer to reject them every time, leading to a SELECTING→CLAIMING→RETRYING spin loop.

## Current ytt Workspace Configuration

### File: `.beads/config.json`

```json
{"created_at":"2026-08-14T15:01:29Z","prefix":"ytt","uuid":"57fcef57-...","version":1}
```

(The legacy `.beads/config.yaml` no longer exists — bead-rs uses `config.json`
for workspace identity.)

### Pluck-Specific Settings

**Exclude labels**: Using defaults (`deferred`, `human`, `blocked`, `escalation`)

**Split threshold**: Using default (3 failures)

**Quarantine threshold**: Using default (5 failures, ADR-022)

**Workspace path**: `/home/coding/ytt`

**Bead store**: bead-rs SQLite backend at `.beads/beads.db`, auto-flushing to
the git-tracked `.beads/checkpoint/` on every mutation

**CLI**: `bead` (`~/.cargo/bin/bead`)

## Expected Behavior

### Normal Operation

1. **Query**: Pluck queries the store's ready frontier with exclude_labels
2. **Filter**: Defensive filtering removes excluded-label beads and unclaimable statuses
3. **Sort**: Returns candidates sorted by (effective_priority, pinned_bucket, failure_count, created_at, id)
4. **Split check**: If top bead has >=3 failure-count, returns Split result
5. **Return**: `BeadFound(candidates)` or `NoWork` or `Split(bead, count)`

### No Work Scenario

Pluck returns `NoWork` when:
- All ready beads have excluded labels
- No ready beads exist in the queue (after relaxation tiers are exhausted)
- Store returns empty list

### Error Handling

Pluck returns `Error(StoreError)` when:
- Bead store connection fails
- Query execution fails
- Data parsing errors occur

## Integration with NEEDLE

### Claiming Process (bead-rs)

```bash
# Inspect the ready frontier without reserving (read-only, same ordering as claim)
bead list --ready --limit 5

# Atomically claim and assign one bead (single transaction)
bead claim --assignee $WORKER

# JSON mode; empty frontier returns exit 0 with {}
bead claim --assignee $WORKER --json

# Opt-in leased claims with fencing tokens (crash-safe recovery)
bead claim --assignee $WORKER --lease-ttl 1800
bead claim --renew-lease
```

`bead claim` performs server-side selection from the ready frontier — open,
unassigned, not manually blocked, and no unfinished `blocks` dependency edges
— under its own `fifo-v1` ordering (priority ASC, created_at ASC, id ASC).
Selection and assignment occur in one atomic transaction: competing claimants
never receive the same bead ID. (The old `list`-then-`update` pattern had a
race with 11+ workers and must not be used.)

Pluck's candidate list feeds directly into `bead claim`.

### Strand Coordination

Pluck is the **primary strand** in NEEDLE's multi-strand architecture:
- **Pluck**: 90%+ of beads (normal work selection)
- **Mend**: Retry beads with previous failures
- **Other strands**: Specialized selection logic

## Claim-Safety Failure Modes

### Assigned-open beads are invisible to the frontier

The ready frontier requires **unassigned**, so an open bead carrying an
assignee is skipped by Pluck, `bead list --ready`, and `bead claim` alike.
That is silent when the assignee is a live worker and a starvation mode when
it is not. The 2026-08-16 fleet-wide sweep found **583** beads stuck this way
across 47 of 66 workspaces, with ten workspaces fully starved (`--ready`
returning zero while live workers spun). Root cause: Mend skips any assignee
whose worker *name* is still alive, and `--count 1` workers relaunch under the
same name forever (see NEEDLE bead `needle-44e7e5cd`).

Note that `bead release` does **not** fix this shape — it only acts on
`in_progress` and deliberately refuses an assigned-open bead. The fix is:

```bash
bead update <id> --clear-assignee
```

This state is invisible to `bead show` and `bead doctor`, which both report it
as healthy.

### `bead reopen` clears the assignee

Reopening a closed bead **clears the assignee**, making the bead immediately
visible to the ready frontier (2026-08-24 fix, ADR-018). Previously reopened
beads retained a stale assignee and became permanently unclaimable — the same
starvation mode as above.

## Label Semantics

### Standard Excluded Labels

| Label | Purpose | When to apply |
|-------|---------|---------------|
| `deferred` | Beads intentionally delayed | Manual or automatic deferral |
| `human` | Requires human intervention | Manual flag by user |
| `blocked` | Blocked by dependencies | Automatic via dependency DAG |
| `escalation` | A bead no fleet worker may claim (N-T60) | Automatic when an audit finding escalates at filing time |

### Other Labels (Not Excluded by Default)

| Label | Purpose | Notes |
|-------|---------|-------|
| `starvation-alert` | Starvation monitoring beads | NOT excluded by default; can be added to custom exclude_labels if needed |

### Failure Count Labels

Pattern: `failure-count:N`

- Applied by NEEDLE when a bead fails consecutive processing attempts
- Read by Pluck to trigger auto-split at threshold (default: 3) and quarantine
  at the ADR-022 threshold (default: 5)
- Example: `failure-count:5` means 5 consecutive failures

## References

- **Pluck implementation**: `~/NEEDLE/src/strand/pluck.rs`
- **NEEDLE documentation**: `~/NEEDLE/docs/` (see `checkpoint-publishing.md`,
  `bead-authoring.md`)
- **ADR-018** (reopen/assignee contract): `~/NEEDLE/docs/adr/018-reopen-assignee-contract.md`
- **ADR-022** (quarantine + escalation ladder): `~/NEEDLE/docs/adr/022-visible-time-bounded-quarantine-and-escalation-ladder.md`
- **bead-rs recovery**: `bead doctor`, or the environment `CLAUDE.md`
  "Beads (bead-rs CLI)" section

## Version

**Documented**: 2026-09-16 (rewritten for the bead-rs migration; previously
documented bead-forge as of 2026-07-06)

**Pluck version**: Current NEEDLE master (as of 2026-09-16)

**bead-rs version**: `bead` 0.2.6
