# Retention Policy — transcripts, audio, cache, and jobs

What `ytt` retains, for how long, what deletes it, and how an operator deletes
it deliberately. Every claim here is pinned by a regression test in
`tests/unit/test_retention.py` (§8) — the guarantees are mechanical, not
aspirational.

Companion documents: `deploy/RUNBOOK.md` (upgrade mechanics, live wiring
status, what must never be done with kubectl), `docs/notes/single-replica.md`
(why all retention state is per-process), `docs/notes/auth.md`
(OAuth-state retention specifics).

## 1. Retention at a glance

| Data | Lives in | Bounded by | Deleted by |
|---|---|---|---|
| Transcript cache units (`<id>.<lang>.txt` + `.json` sidecar, incl. `<id>.whisper.*`) | `YTT_CACHE_DIR` (`ytt-cache` PVC) | `YTT_CACHE_MAX_BYTES` (default 2 Gi), size-bounded LRU — **no time-based expiry** | Write-time/reconcile LRU eviction; operator `rm` (§7); PVC deletion (destructive, forbidden without a runbook decision) |
| Audio scratch (`<id>.<ext>` downloads) | `YTT_SCRATCH_DIR` (emptyDir, 600 Mi cap in the manifest) | `min(YTT_MAX_AUDIO_BYTES, scratch free space)` | Per-attempt `finally` delete + per-video sweep; unconditional startup sweep; pod death (emptyDir) |
| Whisper job records (FSM `pending → running → done \| error`) | process memory | queue cap `YTT_MAX_PENDING_WHISPER_JOBS`; terminal backlog TTL | `run_ttl_gc` after `YTT_JOB_TTL_SEC`; stale-running GC; restart (in-memory) |
| Rate-limit / quota buckets, single-flight map | process memory | per-subject budgets, request-scoped | Token refill (seconds–minutes); restart |
| Pagination cursors | client-held (opaque, content-hash bound) | valid only while the unit is unchanged | Server never stores them; a changed/evicted unit returns `cursor_stale` |
| OAuth client registrations + tokens (FastMCP OAuthProxy) | `ytt-oauth-state` PVC (`FASTMCP_HOME`) | 256 Mi | Provider expiry/revocation — **out of scope here; named so a transcript cleanup never touches it** |
| Logs | stdout (JSON) | collector-side retention | The collector, not `ytt` — redaction happens before emission (§6) |

The single most important line in that table: **the transcript cache has no
TTL**. A transcript is deterministic content for a given video and language;
there is no freshness model in v1 that would make a 6-month-old transcript
wrong. Retention pressure is handled entirely by the size bound (§2), so
`YTT_JOB_TTL_SEC` and the scratch sweeps can be aggressive without ever
deleting a transcript a client is mid-way through paginating — and the
regression tests hold that boundary (expired jobs and swept audio never take a
cached transcript with them).

## 2. Transcript cache — eviction, not expiry

Implementation: `ytt/cache.py::TranscriptCache` (flat files, one unit =
`.txt` body + `.json` sidecar per `(video_id, lang)`; plan Invariant 1:
total bytes ≤ `YTT_CACHE_MAX_BYTES` after every completed write).

**Eviction is whole-unit, LRU by file mtime, under the same lock as every
read and write.** A live unit can never be evicted out from under a reader:
touch-on-hit (`os.utime` on both files), eviction selection, and the deletes
all run inside `_lock`. Eviction never splits a unit — the `.txt` and `.json`
disappear together, so a hit can never serve a body whose sidecar (source,
segments, metadata) is gone.

Triggers, in the order they can fire:

1. **Write-time** — before every `put`, evict oldest units until the new unit
   fits (`cache_eviction` log event, `ytt_cache_evictions_total` counter).
   The byte counter is incremented only after `os.replace` succeeds, so a
   failed write never produces phantom accounting.
2. **ENOSPC degrade** — if the volume itself is full after evict-and-retry
   once, `put` returns `False` and logs `cache_enospc_degrade` (WARNING): the
   caller still serves the transcript, it just isn't cached. Serving is never
   held hostage to caching.
3. **Reconcile** — every `YTT_CACHE_RECONCILE_SEC` (default 300 s),
   `reconcile()` re-stats every unit, corrects counter drift, deregisters
   units whose files disappeared externally (operator `rm`, §7), and evicts
   immediately if the recomputed total exceeds the cap.
4. **Startup scan** — on boot, `startup_scan()` rebuilds the inventory from
   disk and deletes stray `*.tmp` files orphaned by a crash mid-write
   (`cache_startup_scan` event: `units_found`, `total_bytes`,
   `stale_tmp_cleaned`).

What eviction deliberately does **not** do: expire by age. There is no
`YTT_CACHE_TTL` and there should not be one — see §1.

## 3. Audio scratch — four layers, one lifetime

Audio has exactly one permitted lifetime: **a single job attempt**. Plan
Invariant 4 ("audio always deleted") is enforced by four independent layers,
so no single bug can leak disk:

1. **Normal exit** — `run_whisper_job`'s `finally` unlinks the downloaded
   file (`whisper_audio_deleted`) on success *and* on every error path.
2. **Per-video sweep** — the same `finally` then calls
   `_sweep_video_scratch(video_id, scratch_dir)`, which deletes every
   `{video_id}.*` file: the partial output of a download that timed out or
   aborted mid-stream, where `audio_path` was never assigned. This runs after
   *every* attempt — a video that always fails can no longer fill the scratch
   volume by repeated retries (`whisper_scratch_swept` /
   `whisper_scratch_sweep_failed`).
3. **Startup sweep** — `startup_sweep(scratch_dir)` deletes *every* file in
   `YTT_SCRATCH_DIR` unconditionally on boot and logs names and sizes first
   (`scratch_sweep_file`, then `scratch_startup_sweep` with
   `files_deleted`/`bytes_freed`). This is safe only because the deployment
   pins `replicas: 1` + `strategy: Recreate` (see
   `docs/notes/single-replica.md`) — a booting pod is guaranteed to be the
   only runner, so any file present at boot is stale by definition.
4. **The volume itself** — scratch is an `emptyDir`: it dies with the pod even
   if all three code layers somehow failed.

Two properties worth calling out because they look accidental and are not:

- **Zombie-downloader safety.** A timed-out `asyncio.to_thread` yt-dlp keeps
  writing from its thread. Deleting its output unlinks the name; POSIX keeps
  the inode alive until the writer finishes, then frees it. A partial file can
  never outlive the process.
- **Scope.** The per-video sweep's glob contains no metacharacters (the
  canonicalized 11-character ID is the pattern), so it can only ever match
  that video's own files — it cannot reach another video's in-flight download,
  and being pointed at `YTT_SCRATCH_DIR`, it cannot reach the cache volume.

Inbound caps (before any of the cleanup layers matter): the projected download
size is checked against `min(YTT_MAX_AUDIO_BYTES, statvfs free)` *before*
any bytes are downloaded, a progress hook aborts mid-stream on overrun, and
the `YTT_MAX_ASR_DURATION_SEC` duration backstop refuses over-long videos
before the network is touched at all — work that never starts needs no
retention.

## 4. Whisper job records — TTL GC

Implementation: `ytt/whisper.py::WhisperJobRegistry` (in-memory FSM; one entry
per `video_id`; all transitions under `asyncio.Lock`). Each registry-created
job also records the authenticated subject that started it, and its poll
handle answers only that subject — any other subject's poll gets the same
`not_found` an unknown id gets (`docs/notes/auth.md`, §Job ownership), so the
deletion procedures in §7 never have to reason about cross-subject handle
exposure.

- **Terminal jobs expire.** `run_ttl_gc` removes `done`/`error` entries older
  than `YTT_JOB_TTL_SEC` (default 3600 s), logging `whisper_job_ttl_gc` with
  before/after counts. The GC loop ticks every 60 s
  (`start_ttl_gc_task`). A terminal handle is a courtesy — it lets a client
  poll a result — not a record: after expiry its polls return `not_found`,
  and the documented client contract handles that (re-call the tool).
- **Stale running jobs are reaped harder.** A `running` entry older than
  `YTT_WHISPER_TIMEOUT_SEC + YTT_JOB_TTL_SEC` is logged at ERROR
  (`whisper_job_stale_running` — this should be unreachable; it means a job
  escaped its own timeout) and removed. This also frees the queue slot a
  cancelled job's entry would otherwise pin forever.
- **`pending` jobs are not TTL-GC'd.** A pending job exists only for the
  window between registry insertion and its background task starting work;
  the population is bounded by `YTT_MAX_PENDING_WHISPER_JOBS` and cleared by
  restart. There is deliberately no TTL that could garbage-collect a job that
  is about to run.
- **Queue depth stays honest.** `active_count` counts only `pending`/`running`
  — terminal entries awaiting TTL never consume queue capacity, so the TTL
  backlog cannot deny new work.

The retention boundary this section exists for: **job GC removes registry
entries and nothing else.** It never touches `YTT_CACHE_DIR`. A `done` job
whose cache unit was LRU-evicted before any poll removed it is answered by
the cache-first lookup; a transcript outliving its job record is the normal
case, not an edge case (`tests/unit/test_retention.py::TestExpiredJobs`).

## 5. Restart behavior

All coordination state is per-process (single-replica invariant), so a pod
swap — upgrade, rollback, or crash — redraws the line sharply:

| | Survives restart | Lost |
|---|---|---|
| Transcript cache | **Files** on the `ytt-cache` PVC | In-memory inventory + LRU counter (rebuilt by `startup_scan`) |
| Audio scratch | Nothing (emptyDir died with the pod) | Everything — startup sweep clears anything a crash left |
| Job registry | Nothing | Every job; polls return `not_found`; clients re-kick per the tool contract |
| Rate/quota budgets | Nothing | Reset to full (a retry mid-window re-fetches; this is the documented cost) |
| OAuth sessions | `ytt-oauth-state` PVC | Nothing — clients do not re-login (the PVC's entire reason to exist) |

**The designed boot sequence** (the order matters — hygiene before reuse):
`startup_sweep` (scratch clean) → `cache.startup_scan()` (rebuild inventory,
clean `*.tmp`, validate volume capacity) → `start_reconcile_task()` (drift
correction) → `registry.start_ttl_gc_task()` (terminal-job expiry).

A retention trigger is only a guarantee where its invocation actually runs.
The per-job audio layers (§3) run inside every job attempt unconditionally;
the boot-time and periodic triggers above are wired by the boot sequence, and
`deploy/RUNBOOK.md` §2.2 is the live tracker of that wiring status — including
the period-correct caveat that until `startup_scan` is on the boot path,
persisted cache files are *preserved but not warm* (not served until
re-fetched, invisible to the LRU counter). This document pins the contract;
the runbook pins the current state, so the two rot independently.

Client-visible restart contract, in one sentence: in-flight jobs are lost and
said so (`not_found` → re-kick re-downloads and re-spends quota), while any
transcript that reached the cache survives as files and — once the inventory
is rebuilt — as answers.

## 6. Logging & redaction guarantees

Retention policy for logs is different in kind: `ytt` does not retain logs at
all (JSON to stdout; the collector owns retention), so the guarantees that
matter are about what can never *enter* a log in the first place —
`ytt/observability.py::configure_logging` runs `redaction_processor` on every
structured event, unconditionally:

- **Field denylist** — sensitive field names (tokens, credentials,
  transcript bodies, audio) render as `<redacted>` wherever they appear as
  structured fields.
- **Free-text scrub** — string values pass through `redact_credentials()`
  (userinfo stripped, host:port kept) before rendering, because error strings
  are the leak path that matters: yt-dlp and httpx exceptions quote configured
  URLs, and a proxy URL carries credentials in its userinfo.
- **Subjects are hashed.** Per-subject metrics and rate-limit events carry
  only `subject_hash` (first 8 hex chars of SHA-256 of the email) — never the
  address. Denials, quota charges, and 403s are attributable without being
  identifiable.
- **`video_id` is logged in clear.** Deliberate: it is public data by nature
  (it is half of the URL the client sent), and it is the one correlator that
  makes an operator's `kubectl logs` useful. Transcript *content*, segments,
  and audio bytes are never logged — cache log events carry `size_bytes`, not
  text.

Consequence for this policy: an operator deleting transcripts per §7 does not
need a matching log purge — nothing in the retained log lines reproduces the
deleted content.

## 7. Operator deletion procedures

All cluster access mechanics (which kubeconfig, what the read-only proxy
cannot do) are in `deploy/RUNBOOK.md` §7 — the one-line summary is that
resource mutations are GitOps-only (`selfHeal` reverts anything else), while
`exec` into the pod for data operations is diagnostics-adjacent and allowed.
Order the procedures by how often they should be needed (most first — which
is "never, the mechanisms in §2–§4 do it for you").

### 7.1 Delete one video's transcripts

```bash
# <id> is the 11-character YouTube ID. The * glob covers every language
# and the whisper fallback; .txt and .json leave together.
kubectl exec -n <ns> deploy/ytt -- sh -c 'rm -f /cache/dQw4w9WgXcQ.*'
```

Effects, in order: the in-memory unit is deregistered the next time it is
touched (`get` treats an ENOENT as an external deletion and removes the
entry), `reconcile` removes it without being touched, and the next request
re-fetches from source. Any client paginating the old copy gets
`cursor_stale` (the cursor is content-hash bound) and restarts at page 1 —
never a silently wrong continuation. The job registry needs no action: a
`done` handle whose unit was deleted is inert until TTL GC, and its poll
path already handles the evicted-unit case.

### 7.2 Wipe the whole transcript cache

```bash
kubectl exec -n <ns> deploy/ytt -- sh -c 'rm -f /cache/*.txt /cache/*.json'
```

The `*.txt`/`*.json` globs are exactly the unit-file shapes; they cannot match
the singleton lockfile (`.ytt-singleton.lock`, a dotfile) or anything else on
that volume. Never `rm -rf /cache` — the mountpoint and the lockfile live
there. Never touch `ytt-oauth-state` while intending a transcript cleanup:
deleting it force-logs-out every connected client and has no undo.

After a bulk wipe, follow with a GitOps-sanctioned pod bounce (a manifest
change that triggers the swap — `deploy/RUNBOOK.md` §1/§7; `kubectl delete
pod` is forbidden) so the in-memory counter and inventory restart from the
now-empty directory instead of carrying phantom bytes until each stale entry
is individually touched.

### 7.3 Delete scratch audio

Normally nothing to do — §3 bounds a file to one job attempt. If an operator
ever looks (`ls -la` via exec) and wants it empty: `rm -f /scratch/*` is safe
when no ASR job is in flight. Deleting the audio of a job that *is* in flight
fails that job with a stable, relayable `asr_failed` error, and the job's own
`finally` hygiene still runs — the sweep layers tolerate files that are
already gone.

### 7.4 Delete job records

In-memory only: there is nothing on disk to delete. A restart clears the
registry (with the §5 consequences); terminal entries clear themselves via
TTL GC. There is no procedure to delete a single job record, and none is
needed — a handle is one small struct and expires within
`YTT_JOB_TTL_SEC`.

### 7.5 Verification

- `curl .../metrics` → `ytt_cache_bytes` (post-wipe: near zero),
  `ytt_cache_evictions_total` (eviction pressure over time).
- `kubectl logs` on restart → `scratch_startup_sweep` (files/bytes freed),
  `cache_startup_scan` (`units_found` matching the intended survivor count).
- Log events in this document (`cache_eviction`, `cache_enospc_degrade`,
  `reconcile`, `whisper_job_ttl_gc`, `whisper_job_stale_running`,
  `whisper_scratch_swept`) are the audit trail for every automatic deletion —
  grep for the event name, not for content (§6: content is not in there).

## 8. Regression evidence

`tests/unit/test_retention.py` holds the cross-component guarantees this
document claims — deliberately *not* in the per-mechanism suites, because the
failure mode worth guarding is one component's cleanup reaching into
another's data:

| Guarantee | Test |
|---|---|
| TTL GC of expired `done`/`error`/stale-`running` jobs removes registry entries only — every valid cached transcript still serves byte-identical text, files intact | `TestExpiredJobs` |
| Job exit paths (success, ASR failure, unexpected failure) leave zero scratch files, write only the promised cache unit, and never disturb another video's pre-existing cached transcript | `TestAudioCleanup` |
| The per-video sweep matches only its own glob; the startup sweep cannot reach the cache volume; cache and scratch are disjoint | `TestAudioCleanup` |
| LRU eviction removes whole units (both files) to admit new work; untouched survivors still serve byte-identical text | `TestEviction` |
| A restart-equivalent (fresh registry + startup sweep + `startup_scan` over the same volumes) loses job records and audio but serves previously cached transcripts | `TestRestart` |
| This document names every mechanism, env var, and log event it claims — so the prose and the code rot together or not at all | `test_policy_doc_pins_its_mechanisms` |
