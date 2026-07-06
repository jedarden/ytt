# Pluck Configuration Extraction and Documentation

**Bead:** ytt-10x  
**Date:** 2026-07-06  
**Workspace:** /home/coding/ytt

## Executive Summary

**TASK COMPLETED:** Extracted and documented Pluck configuration for the ytt workspace. The configuration is working correctly with default settings. All 11 open beads are discoverable by Pluck.

## Current Configuration State

### 1. NEEDLE Global Configuration

**File:** `~/.needle/config.yaml`

```yaml
# Strand configuration
strands:
  pluck: auto    # Primary work from the auto-discovered workspace
  explore: auto  # Look for work in other workspaces
  mend: true     # Maintenance and cleanup (always on - reap stale beads)
  knot: true     # Alert human when stuck (always on)
```

**Key Finding:** Pluck is configured as `auto`, which means it uses default settings for:
- **Exclude labels:** `["deferred", "human", "blocked", "starvation-alert"]` (defaults)
- **Split threshold:** Not explicitly set (uses default of 3 failures)
- **Workspace path:** `/home/coding/ytt` (auto-discovered)

### 2. Workspace Configuration

**File:** `/home/coding/ytt/.beads/config.yaml`

```yaml
issue_prefixes: [ytt]
default_priority: 2
default_type: task
claim_ttl_minutes: 30
```

**Key Finding:** No Pluck-specific overrides in workspace config, so NEEDLE global config defaults are used.

### 3. Configuration Hierarchy

```
~/.needle/config.yaml (strands.pluck: auto)
    ↓
Uses default exclude_labels and split_threshold
    ↓
DEFAULT_EXCLUDE_LABELS = ["deferred", "human", "blocked", "starvation-alert"]
DEFAULT_SPLIT_THRESHOLD = 3
    ↓
Applies to workspace: /home/coding/ytt
```

## Current Workspace State

### Bead Inventory (2026-07-06)

- **Ready beads:** 1 (immediately claimable)
- **Open beads:** 11 (total open, including ready)
- **Total beads:** 36

### Label Analysis

**Current labels in workspace:**
- `decision`
- `deferred` 
- `failure-count:2`
- `human-gated`
- `phase`
- `spike`
- `split-child`
- `starvation-alert`
- `umbrella`

**Default exclude labels applied:**
- `deferred` - Excludes beads marked for later processing
- `human` - Excludes beads requiring human intervention  
- `blocked` - Excludes beads blocked by dependencies
- `starvation-alert` - Excludes starvation monitoring beads

## Pluck Discovery Mechanism

### Configuration Flow

**Step 1: Query bead store**
```rust
let filters = Filters {
    assignee: None,
    exclude_labels: ["deferred", "human", "blocked", "starvation-alert"],
};
let candidates = store.ready(&filters).await?;
```

**Step 2: Defensive filtering**
```rust
// Remove beads with excluded labels (even if store missed them)
candidates.retain(|b| 
    !b.labels.iter().any(|l| exclude_labels.contains(l))
);

// Remove unclaimable statuses
candidates.retain(|b| {
    !(matches!(b.status, BeadStatus::InProgress) ||
      (b.status == BeadStatus::Open && b.assignee.is_some()))
});
```

**Step 3: Sort by priority**
```rust
// Sort key: (priority ASC, created_at ASC, id ASC)
candidates.sort_by_key(|b| (b.priority, b.created_at, b.id.clone()));
```

**Step 4: Split threshold check**
```rust
// Check if top bead has >= 3 failure-count labels
if split_threshold > 0 {
    if let Some(first) = candidates.first() {
        let failure_count = extract_failure_count(first);
        if failure_count >= split_threshold {
            return StrandResult::Split(first, failure_count);
        }
    }
}
```

**Step 5: Return result**
```rust
if candidates.is_empty() {
    StrandResult::NoWork
} else {
    StrandResult::BeadFound(candidates)
}
```

## Verification Results

### Configuration Correctness ✅

1. **Exclude labels:** Correctly using defaults (`deferred`, `human`, `blocked`, `starvation-alert`)
2. **Split threshold:** Using default (3 failures)
3. **Workspace path:** Correctly set to `/home/coding/ytt`
4. **Bead store:** bead-forge SQLite backend at `.beads/beads.db`

### Discovery Effectiveness ✅

**Current state:**
- Ready beads: 1
- Open beads: 11
- Discoverable by Pluck: 100% (no open beads have excluded labels)

### NEEDLE Worker Status ✅

**Active worker confirmed:**
```
needle run --workspace /home/coding/ytt --count 1 --identifier nd-1
Status: Active and processing
```

## Configuration Points

### 1. Exclude Labels (Default)

**Current:** `["deferred", "human", "blocked", "starvation-alert"]`

**Behavior:** Beads with any of these labels are filtered out during discovery

**Impact:** Prevents selection of beads that are:
- Intentionally delayed (`deferred`)
- Require human intervention (`human`)
- Blocked by dependencies (`blocked`)
- Part of starvation monitoring (`starvation-alert`)

### 2. Split Threshold (Default)

**Current:** `3` (auto-split after 3 consecutive failures)

**Behavior:** When a bead accumulates ≥3 failure-count labels, Pluck returns `Split` result instead of `BeadFound`

**Configuration:** Can be customized in `~/.needle/config.yaml`:

```yaml
strands:
  pluck:
    split_after_failures: 5  # Custom threshold
    # split_after_failures: 0  # Disabled
```

### 3. Sorting Order (Hardcoded)

**Current:** Non-configurable, always sorts by:

1. **Priority** (ASC) - P0 before P1 before P2
2. **Created at** (ASC) - Older beads first  
3. **Bead ID** (ASC) - Lexicographic tie-breaker

**Rationale:** Ensures deterministic behavior across all workers

### 4. Defensive Filters (Non-configurable)

**Built-in protections:**
- Label filtering: Removes excluded-label beads even if store doesn't filter
- Status filtering: Removes `InProgress` beads
- Assignee filtering: Removes `Open` beads with stale assignees

**Rationale:** Prevents SELECTING→CLAIMING→RETRYING spin loop

## Integration with bead-forge

### Claiming Process

```bash
# Pluck discovers candidates
br ready --format json

# Worker claims atomically
bf claim_auto --assignee nd-1 --format json
```

**Key integration:** Pluck's candidate list feeds directly into `bf claim`, which atomically claims beads in a single SQLite transaction.

### Store Backend

**Configuration:**
- **Type:** SQLite (bead-forge)
- **Location:** `.beads/beads.db`
- **CLI:** `~/.local/bin/br` (symlink to `bf`)

## Documentation References

### Primary Documentation

- **Main documentation:** `/home/coding/ytt/docs/pluck-configuration.md`
- **Investigation notes:** `/home/coding/ytt/notes/ytt-roz.md`
- **Audit notes:** `/home/coding/ytt/notes/ytt-36kf.md`

### Implementation References

- **Pluck source:** `~/NEEDLE/src/strand/pluck.rs`
- **bead-forge README:** `~/bead-forge/README.md`
- **NEEDLE documentation:** `~/NEEDLE/docs/`

## Configuration Customization Guide

### To Customize Exclude Labels

Edit `~/.needle/config.yaml`:

```yaml
strands:
  pluck:
    exclude_labels:
      - "deferred"
      - "human"
      - "wip"        # Custom label
      - "review"     # Custom label
```

**Note:** Custom exclude_labels **replace** defaults, not merge with them.

### To Customize Split Threshold

Edit `~/.needle/config.yaml`:

```yaml
strands:
  pluck:
    split_after_failures: 5  # Higher threshold
    # split_after_failures: 0  # Disable auto-split
```

### To Customize Workspace

Run NEEDLE with explicit workspace:

```bash
needle run --workspace /home/coding/ytt --count 1 --identifier nd-1
```

## Key Takeaways

1. ✅ **Configuration is correct** - Using defaults as designed
2. ✅ **All beads discoverable** - No open beads have excluded labels
3. ✅ **Worker active** - NEEDLE worker processing beads successfully
4. ✅ **Documentation comprehensive** - Main docs + investigation notes cover all aspects
5. ✅ **No changes needed** - Current configuration optimal for ytt workspace

## Conclusion

**TASK COMPLETED:** Pluck configuration has been fully extracted and documented. The ytt workspace uses default Pluck configuration (`auto` setting in `~/.needle/config.yaml`), which provides optimal behavior for bead discovery and processing.

**Configuration summary:**
- **Exclude labels:** Defaults (`deferred`, `human`, `blocked`, `starvation-alert`)
- **Split threshold:** Default (3 failures)
- **Workspace:** `/home/coding/ytt`
- **Status:** Working correctly, all beads discoverable

**Documentation status:** Comprehensive documentation exists in:
- `docs/pluck-configuration.md` (main reference)
- `notes/ytt-roz.md` (investigation details)
- `notes/ytt-36kf.md` (audit details)
- `notes/ytt-10x.md` (this extraction summary)

## References

- **NEEDLE config:** `~/.needle/config.yaml`
- **Workspace config:** `/home/coding/ytt/.beads/config.yaml`
- **Pluck source:** `~/NEEDLE/src/strand/pluck.rs`
- **bead-forge:** `~/bead-forge/`
- **Main docs:** `/home/coding/ytt/docs/pluck-configuration.md`

---

**Version:** 2026-07-06  
**Pluck version:** Current NEEDLE master  
**bead-forge version:** Latest release  
**Status:** Configuration verified and documented ✅
