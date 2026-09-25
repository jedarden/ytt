# ytt Operator Runbook — cache PVC: backup, restore & disk-exhaustion recovery

Everything about the `ytt-cache` volume: what lives on it, why almost none of
it is worth protecting, what a fill-up actually does to the service (less than
you'd fear), and the ordered way to get back to a healthy, caching pod.  The
companion volume `ytt-oauth-state` is mentioned only where the two can be
confused — it has the opposite value profile (see §2).

Related docs:

| Doc | Covers |
|---|---|
| [RUNBOOK.md](RUNBOOK.md) | Upgrade/rollback swaps, state-across-restart, the Recreate model, forbidden kubectl |
| [TRANSCRIPT-DELETION-RUNBOOK.md](TRANSCRIPT-DELETION-RUNBOOK.md) | Per-video deletion of cached units — the surgical counterpart of §6's bulk cleanup |
| [DEPLOY-CHECKLIST.md](DEPLOY-CHECKLIST.md) | Release SOP |
| [docs/notes/retention-policy.md](../docs/notes/retention-policy.md) | Age-based retention (transcripts/audio/cache TTLs) — the *policy* this runbook's *recovery* procedures serve |
| [docs/notes/single-replica.md](../docs/notes/single-replica.md) | Why all coordination (incl. the cache byte-counter) is in-process |

Facts marked **verified** were checked live against `ardenone-cluster` and the
0.2.20 code on 2026-09-25 (bead `ytt-1ff7ef2e`).

## 1. What lives on the cache volume

`ytt-cache` — a 2Gi `longhorn` PVC (RWO), mounted at `/cache` in the single
`ytt` pod.  Flat files only, one *unit* per `(video_id, lang)`:

```
/cache
├── abc12345678.en.txt        # transcript body (UTF-8)
├── abc12345678.en.json       # sidecar: {"source": ..., "segments": [...], ...}
├── abc12345678.whisper.txt   # ASR fallback unit (lang key "whisper")
├── def98765432.de.json
├── .ytt-singleton.lock       # NOT cache data — see below
└── *.tmp                     # transient; atomic-write residue, cleaned on scan
```

- **`.ytt-singleton.lock` is not cache data.**  It is the holder *record* for
  the single-replica `flock` (`ytt/singleton.py`); the lock itself is
  kernel-held by the live process.  It is a dotfile, invisible to every cache
  scan (`startup_scan` globs `*.txt`/`*.tmp` only).  Never delete it, never
  back it up, never restore it — a stale copy on a fresh volume is inert (the
  flock is not), but there is no scenario where restoring it helps.
- **`.tmp` files are write residue.**  Writes are `.tmp` → `os.replace`; a
  crash between the two leaves a stray that the next `startup_scan` deletes.
  They are never counted as cache bytes and never served.

What is *not* on this volume: OAuth client registrations and issued tokens
(`ytt-oauth-state`, `/state`) and Whisper scratch audio (`emptyDir`,
`/scratch` — §7).  The two PVCs have opposite value profiles; §2.

## 2. Value model — what's worth backing up

**Nothing on `ytt-cache` is worth an outage, and (today) nothing on it is
backed up.**  Verified 2026-09-25: the Longhorn volume has **no recurring
jobs** (`volumes.longhorn.io …/pvc-bc25cd9f-… spec.recurringJobs` is empty)
and there are **no backup resources at all**
(`kubectl get backups.longhorn.io -A` → `No resources found`).  This is the
design stance, not an omission: every cached unit is a pure function of a
video ID — a miss costs one re-fetch (or one re-transcription and its Whisper
quota spend), never data the user owned.  The PVC worth protecting is
`ytt-oauth-state`: losing it force-logs-out every connected MCP client.  That
volume is out of scope here; treat any procedure below that touches it as
forbidden.

If you want a point-in-time copy anyway (e.g. before a risky manual cleanup),
do it at the **file level, excluding the lockfile**:

```bash
KC=<a kubeconfig with pods/exec on ns ytt>   # see §6 for the access boundary
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- \
  tar czf - --exclude='./.ytt-singleton.lock' -C /cache . \
  > "ytt-cache-$(date -u +%Y%m%dT%H%M%S).tar.gz"
```

A tar copy is honest precisely because the cache is flat files: no
quiescing, no consistency risk beyond a torn unit, and a torn unit degrades
to a miss (§3).  Volume-level snapshots (Longhorn recurring jobs /
VolumeSnapshots) would work too but must be added through declarative-config
like any other manifest change — and are hard to justify for rebuildable
data.

## 3. Restore

Restore = untar the file-level backup back into `/cache`, then reconcile
inventory with disk via a restart swap (the inventory is in-process — §4):

```bash
kubectl --kubeconfig="$KC" exec -i -n ytt deploy/ytt -c ytt -- \
  tar xzf - -C /cache < ytt-cache-<ts>.tar.gz
# then bounce the pod through the GitOps door (RUNBOOK.md §1/§7): any
# manifest change triggers the Recreate swap; the next boot re-scans.
```

Two restore-specific caveats:

- **Preserve mtimes** (tar does by default).  LRU eviction is mtime-ordered;
  a restore that stamped everything "now" would make stale restored units
  look hot and survive eviction ahead of genuinely fresh ones.
- **A torn or partially restored unit degrades, it doesn't error.**  A `.txt`
  without its `.json` sidecar serves with source defaulting to
  `caption_auto`; a corrupt sidecar is ignored the same way.  Missing files
  under a registered unit are dropped from the registry on first access.
  These contracts are pinned by `tests/unit/test_cache_recovery.py`.

**0.2.20 wiring gap — restored (and pre-existing) files are not served until
the wiring lands.**  `startup_scan()` has no call site on the boot path in
0.2.20 (bead `ytt-4f1c45c2`; RUNBOOK.md §2.2), so after any restart — restore
or not — the in-memory inventory starts empty and every `get()` misses until
that `(video_id, lang)` is fetched and re-written.  Restored files are *on
disk*, *correct*, and *inert*; the practical restore procedure at 0.2.20 is
therefore often "don't bother" — a cold cache rebuilds itself organically.
Once `ytt-4f1c45c2` lands, the same restore becomes warm: boot re-scans, and
`cache_startup_scan` in the boot log reports `units_found` matching the
restored file count.  That post-fix behavior is the target state this runbook
describes where it says "post-fix".

## 4. Capacity model — two budgets, one of which is currently blind

| Budget | Enforced by | Value | Notes |
|---|---|---|---|
| App LRU cap | `TranscriptCache` eviction (`YTT_CACHE_MAX_BYTES`) | 1800Mi | only counts units it can see (below) |
| Volume | longhorn PVC request | 2Gi (2147483648 B) | `statvfs` usable ≈ 2040373248 B at boot — ext4 reserved blocks eat the rest (pvc.yml header) |

The 1800Mi cap is **not** "2Gi minus rounding" — it is sized to clear the
boot-time validation with margin: startup (`Settings.validate_storage`, and
`cache._validate_volume_capacity`) fails the pod if the configured cap
exceeds the *statvfs-reported* volume size, which is why an over-large
`YTT_CACHE_MAX_BYTES` presents as CrashLoopBackOff with
`YTT_CACHE_MAX_BYTES … exceeds PVC volume size` in the logs, and why the cap
must stay comfortably below the nominal request.  Never "fix" that
CrashLoopBackOff by raising the cap to the volume size.

The blind spot: at 0.2.20 the startup scan never runs (§3), so the LRU
byte-counter only ever sees units written *since the last restart*.  Units
from before a restart stay on the volume, remain invisible to eviction, and
accumulate across restarts — bounded by the 2Gi volume, harmless for a long
while, but they are the reason a long-lived pod can reach a full volume
without `ytt_cache_bytes` ever approaching the cap.  Post-fix
(`ytt-4f1c45c2`), the counter sees everything and the reclaim loop in §6
becomes automatic.

ENOSPC handling, per write (`TranscriptCache.put`): evict-and-retry once,
then **degrade to serve-but-don't-cache** — the tool call still returns the
transcript, `put` returns `False`, and the pod logs
`cache_enospc_degrade` (WARNING).  No partial files are left behind (the
atomic-write helper unlinks its `.tmp`s on any failure).  A full cache volume
is a caching outage, **not** a serving outage — health, captions, and ASR
keep working; transcripts simply stop being retained.

## 5. Detecting exhaustion

Signals, cheapest first — all readable through the credential-free proxy
(`kubectl --server=http://traefik-ardenone-cluster:8001`):

1. **Degrade warnings**: `kubectl logs -n ytt deploy/ytt --timestamps |
   grep -c cache_enospc_degrade` — any nonzero count means at least one write
   already failed for lack of space.
2. **`ytt_cache_bytes` plateaus** (ServiceMonitor → Prometheus) while
   `cache_write` log events continue: the counter is capped and live writes
   are pairing with evictions.  `ytt_cache_evictions_total` climbing at
   sustain is what fires `YttCacheUndersized` (>10 evictions/min for 15m —
   prometheusrule.yml), which at 0.2.20 says "the visible cache is churning",
   not necessarily "the volume is full".
3. **Volume truth** (needs exec): `kubectl exec -n ytt deploy/ytt -c ytt --
   df -h /cache` and `du -ah /cache | sort -rh | head -20`.  The gap between
   `df` used and `ytt_cache_bytes` is the invisible-orphan population (§4).
4. **Kubelet view**: `kubectl describe pvc ytt-cache -n ytt` shows the
   claim is Bound at 2Gi; kubelet evictions/events for the pod would name
   `EmptyDir`/volume pressure explicitly (scratch, §7 — not the PVC).

Practical baseline (verified 2026-09-25): zero `cache_*` events in the live
pod logs and a young cache — no exhaustion has occurred in production yet.
The procedures below are written for the first time it happens.

## 6. Recovery from a full cache volume

**First, classify urgency: a full cache volume is not an incident.**  The
degrade path keeps serving (§4).  Recovery is maintenance, scheduled like
one.  In order:

1. **Confirm the shape.**  Degrade warnings + plateaued `ytt_cache_bytes` +
   (via exec) `df -h /cache` near 100%: genuine volume exhaustion.  Warnings
   alone with a low counter and free `df`: something else (permissions,
   ro remount) — stop and diagnose, don't delete files.
2. **Durable fix first if you can: land `ytt-4f1c45c2`.**  With the startup
   scan + reconcile wired, the LRU sees every unit, evicts to the 1800Mi cap,
   and the reclaim happens by itself within one reconcile interval — no
   manual deletion at all.  Every manual cleanup below is a workaround for
   that bead being open.
3. **Manual in-place cleanup (immediate lever, needs exec).**  Delete whole
   units — `.txt` **and** matching `.json` together, oldest first — never the
   lockfile, never `/state`:

   ```bash
   # inventory: largest units first (txt bytes + sidecar)
   kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- sh -c \
     "cd /cache && wc -c *.txt *.json 2>/dev/null | sort -rn | head -30"
   # age-ordered candidate list (mtime = LRU key, touch-respecting)
   kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- sh -c \
     "cd /cache && ls -tr --full-time *.txt | head -20"
   # delete a whole unit by stem (both halves)
   kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- sh -c \
     "cd /cache && rm -f <id>.<lang>.txt <id>.<lang>.json"
   ```

   This is data hygiene inside a volume, not a kubernetes mutation — ArgoCD
   manages the PVC object, not its contents, and `selfHeal` will not
   resurrect a deleted transcript.  The running server tolerates it: a
   registered unit whose files vanish is dropped from the registry and
   counter on first access (pinned by test), and at 0.2.20 pre-restart units
   aren't registered anyway.  Stop when `df -h /cache` shows the volume back
   under ~85%; there is no prize for emptying it — those units would have
   been refetched on demand.
4. **Access boundary (say this before promising step 3).**  The
   credential-free read-only proxy **cannot exec** — its RBAC is
   `pods`/`pods/log` `get|list|watch` only; `kubectl exec` through it fails
   with `unable to upgrade connection: Forbidden` (verified 2026-09-25; note
   RUNBOOK.md §3/§7 currently describe exec as allowed through the proxy —
   that claim does not match live RBAC today and is flagged on bead
   `ytt-1ff7ef2e`).  In-pod cleanup needs a kubeconfig granting
   `pods/exec`/`create` in ns `ytt`.  From `codinghome` no such kubeconfig is
   provisioned for `ardenone-cluster` — an agent hitting a full volume should
   stop at detection (§5) and hand the evidence to an operator rather than
   route around the boundary.
5. **If the volume must be replaced** (Longhorn-level corruption, not mere
   fullness): the PVC is ArgoCD-managed, so `kubectl delete pvc` is a
   forbidden live mutation (RUNBOOK.md §7) *and* pointless — the replacement
   goes through declarative-config like any manifest change (new claim →
   sync → pod re-binds → empty cache rebuilds per §10).  Losing the cache is
   the *cheap* outcome (§2); never widen a volume-replacement procedure into
   touching `ytt-oauth-state`.

### What NOT to do

| Tempting move | Why it's wrong |
|---|---|
| `kubectl delete pod` to "clear cache pressure" | Unsanctioned restart (RUNBOOK §7): costs a swap, loses in-flight ASR jobs, and frees **zero** PVC bytes — the data is on the volume, not the pod |
| `kubectl delete pvc ytt-cache` | Destroys every unit with no undo and is a live mutation selfHeal/ArgoCD will fight (RUNBOOK §7) |
| Raise `YTT_CACHE_MAX_BYTES` toward 2Gi | CrashLoopBackOff at boot — startup validation compares against statvfs-reported size (§4) |
| Delete `.ytt-singleton.lock` during cleanup | It's the lock's holder record, not cache data; deleting it fixes nothing and normalizes touching the one file the cache scan deliberately ignores |
| `rm -rf /cache/*` as "a clean slate" | Also deletes the lockfile and any dotfiles added later; delete named units, not everything (§10 is the sanctioned fresh start) |

## 7. Scratch volume — the exhaustion that self-heals

`/scratch` is an **emptyDir (600Mi sizeLimit)** — it dies with the pod on
every Recreate swap, so it cannot accumulate across restarts by design.
Audio is bounded three ways before it can fill the volume: the download cap
is `min(YTT_MAX_AUDIO_BYTES=500Mi, scratch free space via statvfs)`, videos
projected over the cap are refused **before** download
(`TOO_LONG_FOR_ASR`), and an over-run mid-stream aborts the download
(`audio_too_large`).  A completed or failed job sweeps its own
`{video_id}.*` files (failure-path hygiene, `whisper.py::_sweep_video_scratch`).

Consequences for operators:

- **Scratch "exhaustion" presents as ASR job failures** (projected-size or
  mid-stream cap errors in `ytt_whisper_errors_total{reason=…}`), not as a
  full disk, and only for oversized videos.  Captions are unaffected.
- The startup sweep that wipes all scratch files (`whisper.startup_sweep`)
  is **also unwired** at 0.2.20 (`ytt-4f1c45c2`).  On k8s this is nearly
  moot (emptyDir resets on every swap anyway); it matters only for
  bare-metal/same-dir restarts, where a crashed run's partial file lingers
  until the next attempt at that same video sweeps it.
- A stuck or suspicious scratch volume is cleared by **any** manifest-change
  restart (GitOps door), never by touching a PVC.  If a partial file for
  video X keeps failing jobs, that's the sweep's own next attempt cleaning
  it — a manual `rm /scratch/<video>.*` via exec is equivalent and rarely
  needed.

## 8. Restarts around cache operations — the Recreate contract

Restarts are manifest-change-driven only (`declarative-config` edit → push →
ArgoCD sync → Recreate swap).  `kubectl delete pod` / `rollout restart` are
forbidden (RUNBOOK.md §7).  What a swap costs around cache state:

| State | Effect of a Recreate swap |
|---|---|
| Cached units on `/cache` | Preserved on the volume; **not served** until refetched at 0.2.20 (§3 wiring gap), warm post-fix |
| In-memory inventory + byte counter | Reset (the §4 blind spot's origin) |
| In-flight Whisper jobs | Lost — registry is in-process; clients re-kick (RUNBOOK §2.1) |
| Scratch audio | Gone with the pod (by design — this is the scratch recovery path) |
| `.ytt-singleton.lock` flock | Released by kernel on process death; a crash-looping pod can never wedge it |

Ordering rule: let an ASR-heavy queue drain before a swap you chose to make
(RUNBOOK §2.1) — a swap is the one action that turns "cache pressure" into
"lost user-visible jobs".

## 9. Post-recovery verification (run in order)

All read-only through the credential-free proxy unless noted:

1. **Pod is one, ready, right image** — `kubectl get pods -n ytt` (single
   `ytt` pod, `1/1 Running`); a CrashLoopBackOff here means a config problem
   (§4 startup validation), not a cache one — read the logs before touching
   anything.
2. **No fresh degrade warnings** — after recovery, `kubectl logs -n ytt
   deploy/ytt --timestamps | grep cache_enospc_degrade | tail` must show no
   entries newer than the recovery.
3. **Caching resumed** — trigger a fetch of any video and watch the log pair
   `cache_miss` → `cache_write` (then `cache_hit` on the second fetch).
   `ytt_cache_bytes` in `/ytt/metrics` ticks up accordingly.
4. **Volume headroom** — via exec: `df -h /cache` back under ~85%; orphan
   population shrinking is *not* expected until `ytt-4f1c45c2` lands (§4) —
   the recovery only bought headroom.
5. **Scratch clean** — `df -h /scratch` near-zero after any in-flight jobs
   finish; a nonzero residual with no jobs running is the §7 unwired-sweep
   residue and clears at the next swap.
6. **If the recovery changed the image or any env var** — the canary
   acceptance gate applies (RUNBOOK §3 step 4): `ytt canary --gate` in the
   new pod, evidence retained.  A file-level cleanup changes neither, so it
   needs only steps 1–5.
7. **Record it** — units deleted, bytes reclaimed, `df` before/after on the
   bead for the operation.  Cleanup without a record is an audit hole the
   next operator pays for.

## 10. Recovery drill — "cached data unavailable" smoke

The behavioral contract this runbook relies on — *the service recovers when
cached data is missing, partial, or unwritable* — is pinned as a runnable
smoke in `tests/unit/test_cache_recovery.py`:

```bash
uv run pytest tests/unit/test_cache_recovery.py -q
```

It exercises, at the unit level: a wiped volume rebuilding organically
(empty dir → miss → write → hit); a file-level backup/restore round-trip
(byte-identical text, source, segments after re-scan); partial restores
(missing sidecar, corrupt sidecar) serving instead of erroring; a registered
unit vanishing under a running cache (degrades to miss, counter corrects,
re-caches cleanly); an ENOSPC exhaustion degrading without residue and
recovering once space frees; and the backup procedure's contract (units
glob, lockfile excluded, stale lockfile on a restored volume ignored by the
scan).  Those tests are the *only* sanctioned "practice recovery" path —
there is no staging cluster, and drilling on production would mean
manufacturing the incident §6 exists for.

A cold-cache rebuild (the post-incident steady state) needs no drill: it is
the ordinary fetch path with an empty inventory — every miss refetches, the
counter grows, and `ytt_cache_bytes` returning to its slow climb is the
rebuild's completion signal.
