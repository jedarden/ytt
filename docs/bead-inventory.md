# Workspace Open Bead Inventory

Generated 2026-08-21 from the live bead-rs store in `/home/coding/ytt` with:

```text
bead list --status open --json --limit 1000
```

The query returned 13 open beads. This is the current live count; the “11”
mentioned in several older bead descriptions is stale.

| ID | Title | Labels | Status |
| --- | --- | --- | --- |
| `ytt-75ddd394` | Genesis: ytt (YouTube Transcript MCP) Implementation | — | open |
| `ytt-a7bb4bb4` | ibkr do-no-harm gate (additive routing + before/after regression) | — | open |
| `ytt-14e20a34` | Starvation alert: beads invisible to worker | — | open |
| `ytt-b83fc68e` | Identify Pluck-bead mismatch | — | open |
| `ytt-99480385` | Create diagnostic report | — | open |
| `ytt-11ac57fb` | Phase 10: Deploy to ardenone-cluster (human-gated ops) | — | open |
| `ytt-db4e67e3` | Phase 11: Public release - GHCR public + docs + first tag | — | open |
| `ytt-032b59da` | Test Pluck bead discovery with corrected configuration | — | open |
| `ytt-d9888685` | Phase 9: In-cluster integration test harness | `deferred`, `failure-count:1` | open |
| `ytt-135983bb` | Investigate Pluck configuration for bead discovery | — | open |
| `ytt-53928d8a` | Verify worker can now discover and claim beads | — | open |
| `ytt-fe155045` | Verify Pluck discovers beads after configuration fix | — | open |
| `ytt-a53c9acc` | ytt-build.yaml CI writes version-bump commits to GitHub, not Forgejo (breaks mirror model) | `verification-failed` | open |

## Pluck label check

The current NEEDLE source default exclude labels are `deferred`, `human`, and
`blocked`.

- `ytt-d9888685` has the exact excluded label `deferred`.
- No open bead has `human` or `blocked`.
- `failure-count:1` is Pluck failure metadata, not an exclude label.
- `verification-failed` is not in the current default exclude set.
- `starvation-alert` appears in older workspace documentation, but not in the
  current NEEDLE source defaults; no open bead has that label anyway.

The machine-readable copy is [.beads/bead-inventory-open.json](../.beads/bead-inventory-open.json).
