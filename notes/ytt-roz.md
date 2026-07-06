# Pluck Configuration and Bead Discovery Investigation

**Bead:** ytt-roz  
**Date:** 2026-07-06  
**Workspace:** /home/coding/ytt

## Executive Summary

**FINDING:** Pluck configuration is working correctly. The disconnect between "11 open beads" (from outdated starvation alert) and Pluck's view is due to stale information. The actual state shows **13 open beads**, all discoverable by Pluck, and the NEEDLE worker is actively processing them.

## Pluck Configuration Architecture

### 1. Configuration Hierarchy

Pluck configuration exists at three levels:

**Level 1: NEEDLE Global Config** (`~/.needle.yaml`)
```yaml
strands:
  pluck:
    exclude_labels: []          # Empty → uses defaults
    split_after_failures: 0     # Disabled auto-split
```

**Level 2: Hardcoded Defaults** (`~/NEEDLE/src/strand/pluck.rs:13`)
```rust
const DEFAULT_EXCLUDE_LABELS: &[&str] = &[
    "deferred",
    "human", 
    "blocked",
    "starvation-alert"
];
```

**Level 3: Workspace Config** (`.beads/config.yaml`)
```yaml
issue_prefixes: [ytt]
default_priority: 2
default_type: task
claim_ttl_minutes: 30
```

### 2. Configuration Loading Flow

```
~/.needle.yaml
    ↓
needle::config::Config::from_file()
    ↓
PluckStrand::new(exclude_labels)
    ↓
IF exclude_labels.is_empty() THEN
    apply DEFAULT_EXCLUDE_LABELS
ELSE
    use provided exclude_labels
```

**Key Finding:** When `exclude_labels: []` is set in `.needle.yaml`, the code automatically substitutes the default labels. This is confirmed in `pluck.rs:28-36`.

## Pluck Bead Discovery Flow

### Code Path: `~/NEEDLE/src/strand/pluck.rs:103-156`

**Step 1: Query bead store** (line 105-116)
```rust
let filters = Filters {
    assignee: None,
    exclude_labels: self.exclude_labels.clone(),
};
let mut candidates = store.ready(&filters).await?;
```

**Step 2: Defensive label filtering** (line 118-125)
```rust
candidates.retain(|b| 
    !b.labels.iter().any(|l| self.exclude_labels.contains(l))
);
```

**Step 3: Status/assignee filtering** (line 127-133)
```rust
candidates.retain(|b| {
    !(matches!(b.status, BeadStatus::InProgress) ||
      (b.status == BeadStatus::Open && b.assignee.is_some()))
});
```

**Step 4: Sort by priority** (line 135-136)
```rust
Self::sort_candidates(&mut candidates);
// Sort key: (priority ASC, created_at ASC, id ASC)
```

**Step 5: Split threshold check** (line 138-148)
```rust
if self.split_after_failures > 0 {
    if let Some(first) = candidates.first() {
        let failure_count = Self::extract_failure_count(first);
        if failure_count >= self.split_after_failures {
            return StrandResult::Split(...);
        }
    }
}
```

**Step 6: Return result** (line 150-156)
```rust
if candidates.is_empty() {
    StrandResult::NoWork
} else {
    StrandResult::BeadFound(candidates)
}
```

## Configuration Points Affecting Discovery

### 1. Exclude Labels

**Location:** `~/.needle.yaml` → `strands.pluck.exclude_labels`

**Default behavior:**
- Empty array `[]` → applies defaults: `["deferred", "human", "blocked", "starvation-alert"]`
- Non-empty array → uses only those labels (replaces, doesn't merge)

**Impact:** Beads with any matching label are filtered out in Step 2.

**Current ytt state:** Using defaults (empty config → automatic defaults)

### 2. Workspace Path

**Location:** NEEDLE worker command line

```bash
needle run --workspace /home/coding/ytt --count 1 --identifier nd-1
```

**Impact:** Determines which `.beads/beads.db` SQLite database is queried.

**Current ytt state:** Correctly set to `/home/coding/ytt`

### 3. Split Threshold

**Location:** `~/.needle.yaml` → `strands.pluck.split_after_failures`

**Behavior:**
- `0` = disabled (current ytt setting)
- `3` = default (beads auto-split after 3 consecutive failures)
- Any positive value = trigger Split result instead of returning bead

**Impact:** Can prevent normal bead return when failure count ≥ threshold.

**Current ytt state:** Disabled (`split_after_failures: 0`)

### 4. Defensive Filters (Non-configurable)

**Built into PluckStrand code:**

1. **Label filtering** (line 125): Removes beads with excluded labels even if store doesn't filter them
2. **Status filtering** (line 130-132): Removes `InProgress` beads
3. **Assignee filtering** (line 132): Removes `Open` beads with non-None assignee

**Rationale:** These filters prevent the SELECTING→CLAIMING→RETRYING spin loop that occurs when unclaimable beads are returned.

## Current State Analysis

### Bead Inventory (2026-07-06)

**Total open beads:** 13 (not 11 as claimed in outdated starvation alert)

**All open beads and their labels:**
```
ytt-5gt   (no labels)
ytt-3ld   [phase]
ytt-5c8   [phase]
ytt-4th   [phase]
ytt-2c4   [phase]
ytt-219   [phase]
ytt-5rr   [phase]
ytt-30a   [phase]
ytt-1ed   [phase]
ytt-5c4   [phase]
ytt-4iv   [phase]
ytt-5dt   [split-child]
ytt-10x   [split-child]
```

**Discoverable by Pluck:** 13/13 (100%)
- None have excluded labels (`deferred`, `human`, `blocked`, `starvation-alert`)
- All are `Open` status
- All have `assignee: None`

**Excluded by Pluck:** 0/13

### NEEDLE Worker Status

**CONFIRMED RUNNING:**
```
PID: 1381143
Command: needle run --workspace /home/coding/ytt --count 1 --identifier nd-1
Started: 08:43
Status: Active
```

**Evidence from logs:**
- Worker successfully claiming beads: `atomically claimed bead via claim_auto`
- State transitions working: `SELECTING → BUILDING → DISPATCHING → EXECUTING`
- Bead ytt-roz currently being processed (this investigation)

## Why the Gap Exists

### Root Cause: Stale Starvation Alert

The starvation alert bead (ytt-134) contains **outdated information**:

**Claimed in alert:**
- Workspace: "default" ❌ (should be "ytt")
- Total beads: 21 ❌ (actually 36)
- Open beads: 11 ❌ (actually 13)

**Alert status:**
- Status: `blocked`
- Labels: `deferred`, `starvation-alert`, `umbrella`
- This alert bead itself is excluded from Pluck due to `deferred` label

### What Pluck Actually Sees

**Pluck's candidate list:** All 13 open beads

**Why all are discoverable:**
1. ✅ No excluded labels (`deferred`, `human`, `blocked`, `starvation-alert`)
2. ✅ All are `Open` status (not `InProgress`)
3. ✅ All have `assignee: None` (no stale assignments)
4. ✅ No beads have `failure-count ≥ threshold` (split disabled anyway)

**Result:** `StrandResult::BeadFound(13 candidates)` returned to worker

### Worker Behavior

1. Worker receives 13 candidates from Pluck
2. Calls `bf claim_auto` (atomic claim via SQLite)
3. First claimable bead is claimed (ytt-roz in this case)
4. Processing begins (this investigation)
5. After completion, worker returns to SELECTING state
6. Pluck re-evaluates and returns remaining candidates

## Verification Steps

To verify Pluck configuration is working:

```bash
# 1. Check worker is running
ps aux | grep "needle.*ytt"

# 2. Check ready bead count
br ready --format json | jq -s 'length'

# 3. List open beads with labels
br list --format json | jq -s 'map(select(.status == "open")) | .[] | {id, labels}'

# 4. Verify worker is actively claiming
tail -f ~/.needle/logs/needle-relaunch-ytt-nd-1.stderr.log | grep "claimed bead"
```

## Key Takeaways

1. **Pluck configuration is correct** - using default exclude labels as designed
2. **All 13 open beads are discoverable** - no configuration issue
3. **Worker is actively processing** - logs confirm successful claims
4. **Starvation alert is stale** - outdated numbers, wrong workspace
5. **No gap exists** - Pluck sees all discoverable beads and worker is claiming them

## References

- **Pluck implementation:** `~/NEEDLE/src/strand/pluck.rs`
- **Global config:** `~/.needle.yaml`
- **Workspace config:** `/home/coding/ytt/.beads/config.yaml`
- **Documentation:** `/home/coding/ytt/docs/pluck-configuration.md`
- **Previous audit:** `/home/coding/ytt/notes/ytt-36kf.md`

## Conclusion

The Pluck configuration and bead discovery mechanism are working as intended. The perception of a "gap" was based on an outdated starvation alert bead (ytt-134) with incorrect bead counts and workspace name. The actual state shows 13 open beads, all discoverable, and an active NEEDLE worker successfully processing them.

**No configuration changes needed.**
