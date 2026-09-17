# Single-Replica Invariant (v1)

## The constraint

`ytt` is **correct only at `replicas: 1`** (plan §Design constraints: "Single replica (v1) … Scale-out is a redesign").  This is a hard constraint of the v1 architecture, not a tuning knob.  All coordination state is **in-process**:

| Component | Where | Why it breaks at N>1 |
|---|---|---|
| LRU cache byte-counter | `ytt/cache.py` (`TranscriptCache._total_bytes`) | Each process tracks its own counter against the **shared** PVC; N counters × `YTT_CACHE_MAX_BYTES` can overshoot the volume (ENOSPC) and evict units another replica is reading |
| Single-flight map | `ytt/concurrency.py` (`ConcurrencyState`) | Two replicas fetch the same video concurrently — duplicated yt-dlp work and doubled load on the residential egress IP, the exact thing the map exists to prevent |
| Whisper job registry | `ytt/whisper.py` (`WhisperJobRegistry`) | `get_transcript_job` polls in-memory job handles; a job started on pod A is `not_found` from pod B, and its audio download/POST is orphaned on A's scratch volume |
| Rate limiter buckets | `ytt/ratelimit.py` | Per-subject budgets multiply by N — the YouTube-burn protection quietly weakens |
| Startup scratch sweep | `ytt/whisper.py` (`startup_sweep`) | Deletes **all** files in `YTT_SCRATCH_DIR` unconditionally on boot — safe only when guaranteed to be the only runner (`Recreate` gives that guarantee); at N>1 a booting pod deletes a peer's in-flight audio |
| One uvicorn worker | `ytt/server.py` `serve()` | Hardcoded `workers=1`; even within one pod, two workers would split the same state |

## Enforcement — three layers

1. **Manifest** (`deploy/k8s/ardenone-cluster/ytt/`): every Deployment pins `replicas: 1` **explicitly** and `strategy: Recreate`.  `Recreate` is load-bearing: the default `RollingUpdate` has `maxSurge >= 1`, which briefly runs the new pod **alongside** the old one — two live servers on split in-process state during every deploy.  `tests/unit/test_single_replica.py` scans both `.yml` and `.yaml` files and asserts both fields on every Deployment under `deploy/k8s/` so an edit can't silently reintroduce scale-out or a rolling strategy.
2. **Startup flock tripwire** (`ytt/singleton.py`): `serve()` takes an exclusive `flock` on `<cache_dir>/.ytt-singleton.lock` (the cache PVC — the one path every replica of the Deployment shares) and holds it for the process lifetime.  A second instance that scales in — or any stray process aimed at the same cache dir — fails to win the lock, logs `Single-replica invariant violated` with the recorded holder (pid/hostname/started_at), and exits 1 → CrashLoopBackOff: loud, not silently wrong.  The kernel releases the lock on process death, so restarts need no cleanup and there is no staleness to reap.  The lockfile is a dotfile, invisible to the cache LRU scan (`*.txt`/`*.tmp` globs).  If the filesystem cannot create or `flock` the file, startup also exits 1: an unverifiable guard must not permit split in-process state.  `tests/unit/test_singleton_runtime.py` drives this tripwire with real OS processes against a tmp cache dir: a live holder excludes a genuine second process, a competing `ytt serve` exits 1 with the holder's pid in its log and no uvicorn startup, the lock is winnable again immediately after the holder is SIGKILLed (stale lockfile still on disk, no cleanup), and a read-only cache dir fails closed.
3. **One worker per pod**: `serve()` hardcodes `uvicorn workers=1`.  The same runtime module observes the real `serve()` wiring handing `uvicorn.run` `workers=1` while already holding the singleton lock.

The deployed manifest lives in `declarative-config` (`k8s/ardenone-cluster/ytt/`, synced by ArgoCD from the copy documented in `deploy/README.md`) — keep the two copies identical, enforced byte-for-byte by `tests/unit/test_deploy_parity.py` (part of `scripts/definition-of-done.sh`; regenerate the mirror with the canonical commands in `deploy/README.md`).

## Why scale-out is a redesign, not a flag flip

Raising `replicas` requires externalizing every row of the table above:

| State | Scale-out requires |
|---|---|
| Cache byte-counter | A shared cache index (DB/Redis) with quota accounting, or object storage (S3 + index) instead of flat files on a PVC; the volume becomes `ReadWriteMany` |
| Single-flight | A distributed lease per `video_id` (Redis/DB row lock) so only one replica fetches |
| Whisper job registry | Jobs persisted in a shared store (Redis/Postgres); `get_transcript_job` polls any replica; job audio ownership moves to the queue entry; job TTL GC becomes a single elected/atomic sweep |
| Rate limiter | Token-bucket counters in the shared store |
| Scratch sweep + emptyDir scratch | Per-job audio ownership (queue-assigned); the unconditional startup sweep must be dropped or run under an exclusive cluster-wide lease — `Recreate` no longer guarantees exclusivity once the state is external |
| `strategy: Recreate` | Reverts to `RollingUpdate` once no in-process state remains |

Two further ceilings are worth naming before investing in all of the above:

- **Egress:** one residential IP is itself the throughput limit, and parallel fetches from it raise YouTube-block risk.  Scale-out only pays with a residential proxy pool (`YTT_PROXY_URL` becomes per-replica routing policy), which changes the cost and burn model more than the replica count does.
- **Whisper:** `whisper-openai` (CPU) is the slower shared dependency; `YTT_MAX_CONCURRENT_WHISPER=1` exists to protect it.  Replica scaling without ASR capacity scaling just moves the queue.

Until someone builds all of that, the correct move when capacity is tight is *not* `kubectl scale` — it is tuning `YTT_MAX_CONCURRENT_FETCHES`, the cache size, or the rate limits within the single replica.
