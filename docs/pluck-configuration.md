# Pluck Configuration Documentation

## Overview

Pluck is the primary bead selection strand in NEEDLE that handles >90% of all bead processing. It queries the bead store for unassigned, ready beads, filters by excluded labels, and returns them in deterministic priority order.

**Source**: NEEDLE fleet orchestrator (`~/NEEDLE/src/strand/pluck.rs`)

## Configuration Settings

### Workspace: `/home/coding/ytt`

The ytt workspace uses the default Pluck configuration via bead-forge (br CLI).

### 1. Default Exclude Labels

**Location**: Hardcoded in PluckStrand implementation

**Default values** (when no custom configuration is provided):
- `deferred` - Beads marked for later processing
- `human` - Beads requiring human intervention
- `blocked` - Beads blocked by dependencies
- `starvation-alert` - Beads flagged for starvation monitoring

**How it works**:
When PluckStrand is initialized with an empty exclude_labels vector, these defaults are automatically applied:

```rust
const DEFAULT_EXCLUDE_LABELS: &[&str] = &["deferred", "human", "blocked", "starvation-alert"];
```

**Current configuration in ytt**: Using defaults (no custom exclude_labels configured in `.beads/config.yaml`)

### 2. Custom Exclude Labels

**How to configure**: Provide a custom vector of labels when creating PluckStrand

```rust
let strand = PluckStrand::new(vec!["wip".to_string(), "review".to_string()]);
```

**Behavior**: When custom exclude_labels are provided, they completely replace the defaults. The default labels are **not** merged with custom ones.

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
let strand = PluckStrand::new(vec![]);

// Custom threshold
let strand = PluckStrand::with_split_threshold(vec![], 5);

// Disabled
let strand = PluckStrand::with_split_threshold(vec![], 0);
```

**Current configuration in ytt**: Using default threshold of 3

### 4. Sorting Order (Deterministic Priority)

**Hardcoded behavior** - not configurable

Pluck always sorts candidates in this order:
1. **Priority** (ASC) - P0 before P1 before P2, etc.
2. **Created at** (ASC) - Older beads first
3. **Bead ID** (ASC) - Lexicographic tie-breaker for determinism

**Why this matters**: Given the same queue state, every worker computes the same candidate list. This enables coordination without central server state.

### 5. Additional Filters (Defensive Guards)

These filters are applied **after** the bead store query and are not configurable:

**Filtered out**:
- Beads with any excluded label (defensive guard against stores that don't apply label filtering)
- Beads in `in_progress` status (claimed by another worker)
- Open beads with a stale assignee (Open + assignee != None = not claimable)

**Rationale**: These beads would cause the claimer to reject them every time, leading to a SELECTING→CLAIMING→RETRYING spin loop.

## Current ytt Workspace Configuration

### File: `.beads/config.yaml`

```yaml
issue_prefixes: [ytt]
default_priority: 2
default_type: task
claim_ttl_minutes: 30
```

### Pluck-Specific Settings

**Exclude labels**: Using defaults (`deferred`, `human`, `blocked`, `starvation-alert`)

**Split threshold**: Using default (3 failures)

**Workspace path**: `/home/coding/ytt`

**Bead store**: bead-forge SQLite backend at `.beads/beads.db`

**CLI**: `~/.local/bin/br` (symlink to `bf` - bead-forge)

## Expected Behavior

### Normal Operation

1. **Query**: Pluck calls `store.ready(filters)` with exclude_labels
2. **Filter**: Defensive filtering removes excluded-label beads and unclaimable statuses
3. **Sort**: Returns candidates sorted by priority, created_at, id
4. **Split check**: If top bead has >=3 failure-count, returns Split result
5. **Return**: `BeadFound(candidates)` or `NoWork` or `Split(bead, count)`

### No Work Scenario

Pluck returns `NoWork` when:
- All ready beads have excluded labels
- No ready beads exist in the queue
- Store returns empty list

### Error Handling

Pluck returns `Error(StoreError)` when:
- Bead store connection fails
- Query execution fails
- Data parsing errors occur

## Integration with NEEDLE

### Claiming Process (New with bead-forge)

```bash
# Old (race condition with 11+ workers)
BEAD=$(br list --format json | jq -r '.[0].id')
br update $BEAD --status in_progress --assignee $WORKER

# New (atomic, no race)
bf claim --assignee $WORKER --format json
```

Pluck's candidate list feeds directly into `bf claim`, which atomically claims the next available bead in a single `BEGIN IMMEDIATE` transaction.

### Strand Coordination

Pluck is the **primary strand** in NEEDLE's multi-strrand architecture:
- **Pluck**: 90%+ of beads (normal work selection)
- **Mend**: Retry beads with previous failures
- **Other strands**: Specialized selection logic

## Label Semantics

### Standard Excluded Labels

| Label | Purpose | When to apply |
|-------|---------|---------------|
| `deferred` | Beads intentionally delayed | Manual or automatic deferral |
| `human` | Requires human intervention | Manual flag by user |
| `blocked` | Blocked by dependencies | Automatic via dependency DAG |
| `starvation-alert` | Starvation monitoring beads | NEEDLE auto-detection system |

### Failure Count Labels

Pattern: `failure-count:N`

- Applied by NEEDLE when a bead fails consecutive processing attempts
- Read by Pluck to trigger auto-split at threshold (default: 3)
- Example: `failure-count:5` means 5 consecutive failures

## References

- **Pluck implementation**: `~/NEEDLE/src/strand/pluck.rs`
- **bead-forge README**: `~/bead-forge/README.md`
- **NEEDLE documentation**: `~/NEEDLE/docs/`
- **br/bead-forge compatibility**: `~/bead-forge/docs/research/br-compatibility.md`

## Version

**Documented**: 2026-07-06

**Pluck version**: Current NEEDLE master (as of 2026-07-06)

**bead-forge version**: Latest release (auto-deployed via systemd timer)
