# ytt Operator Runbook — scoped transcript-cache deletion

How to deliberately remove *one video's* transcript unit — or one language of
one video — from the `ytt-cache` volume: how to identify exactly what is
there, delete it without disturbing anything else, prove the result, and
understand what cannot be undone. This is the *procedure* behind the
retention policy's permission for deliberate operator deletion
(`docs/notes/retention-policy.md` §7); the *policy* — what ytt retains, for
how long, and what deletes it automatically — lives there, and
*capacity-driven* bulk cleanup lives in [CACHE-RUNBOOK.md](CACHE-RUNBOOK.md)
§6.

"Scoped" is the operative word. Everything here deletes by exact video ID —
never a language glob across videos, never the whole volume. The unit
boundary, the blast radius, and the verification are all per-video.

Related docs:

| Doc | Covers |
|---|---|
| [docs/notes/retention-policy.md](../docs/notes/retention-policy.md) | The retention policy this procedure serves — the permission and the automatic deletions |
| [CACHE-RUNBOOK.md](CACHE-RUNBOOK.md) | The same volume from the capacity side: backup/restore, ENOSPC recovery, bulk oldest-first cleanup |
| [RUNBOOK.md](RUNBOOK.md) | Upgrade/rollback swaps, state across restart, forbidden kubectl |
| [docs/notes/single-replica.md](../docs/notes/single-replica.md) | Why one pod owns all of this state |

Facts marked **verified** were checked live against `ardenone-cluster` and
the 0.2.20 code on 2026-09-25 (bead `ytt-8f8356de`) — including the access
boundary in §4, which contradicts older prose in `RUNBOOK.md` §3/§7 (the
correction is tracked as bead `ytt-4d76a316`).

## 1. When to delete, and when not to

Legitimate reasons, in the order they are likely to occur:

- **A takedown / deletion request**: "remove everything you hold for video X."
- **A known-bad transcript**: a caption track that doesn't match the video,
  or a whisper unit that transcribed the wrong audio — deleting the unit
  forces a fresh fetch on the next request.
- **One language is poisoned** and only that language should go.

Not legitimate reasons:

- **Freshness.** The cache has no TTL and should not: a transcript is
  deterministic content for a given video and language (retention-policy
  §1). Deleting a unit to "refresh" it buys nothing the next fetch wouldn't
  do anyway, at the cost of a refetch.
- **Disk pressure.** That is CACHE-RUNBOOK §6's job — bulk, oldest-first,
  headroom-targeted. A per-video procedure is the wrong tool for a full
  volume, and a volume-replacement is CACHE-RUNBOOK §6 step 5, not a shell
  glob.
- **OAuth trouble, audio leftovers, stuck jobs.** None of them live on this
  volume (§6) — deleting transcripts cannot fix any of them.

## 2. What one deletion unit is

Flat files, one *unit* per `(video_id, lang)` — `ytt/cache.py`:

| File | What it is |
|---|---|
| `<id>.<lang>.txt` | transcript body (UTF-8) — the served text |
| `<id>.<lang>.json` | sidecar: `{"source": …, "segments": […], …}` |
| `<id>.whisper.txt` / `<id>.whisper.json` | the ASR fallback unit (lang key `whisper`); satisfies *any* language request for the video |
| `<id>.<lang>.txt.tmp` / `.json.tmp` | transient atomic-write residue (the `os.replace` midpoint); a crash mid-write leaves it, the next boot's scan deletes it |
| `.ytt-singleton.lock` | **not cache data** — the single-replica `flock`'s holder record. A dotfile, invisible to every cache scan by design. Never delete it, never restore it (CACHE-RUNBOOK §1) |

The unit is the **pair**: a `.txt` and its same-stem `.json` are one logical
object (§5.3 covers what each half alone means). The in-memory registry and
the LRU byte-counter hold one entry per unit, in the server process.

The ID: exactly 11 characters of `[A-Za-z0-9_-]`, **case-sensitive**
(`ytt/canonicalize.py`). `dQw4w9WgXcQ` and `dqw4w9wgxcq` are different
strings and only one is the video. Copy it exactly (§3).

## 3. Identify the target — URL → ID → files

`canonicalize()` (`ytt/canonicalize.py`) is what turned the client's URL
into the ID on the cache files; the same mapping, for the operator:

| Input | ID |
|---|---|
| `https://www.youtube.com/watch?v=dQw4w9WgXcQ` | `dQw4w9WgXcQ` (the `v=` value, verbatim) |
| `https://youtu.be/dQw4w9WgXcQ` | `dQw4w9WgXcQ` (first path segment) |
| `https://www.youtube.com/shorts/dQw4w9WgXcQ` | `dQw4w9WgXcQ` (same for `/live/`, `/embed/`, `/v/`) |
| `dQw4w9WgXcQ` (bare 11-char id) | itself |

Playlist / channel / handle / search URLs are rejected by the server
(`bad_url`), so they can never have been cached under any other key: every
unit's key is a bare 11-char ID, and the `youtu.be` / `watch` / `shorts`
forms of one video all collapsed to it. Case-sensitive: strip nothing,
lowercase nothing.

List what is actually on the volume for that ID (sizes = what a delete
frees):

```bash
KC=<a kubeconfig with pods/exec on ns ytt>   # §4 — the read-only proxy cannot do this
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- sh -c \
  "cd /cache && ls -la <id>.* 2>&1; wc -c <id>.* 2>/dev/null"
```

An unmatched glob is itself information: `ls` answering
`No such file or directory` for the literal `<id>.*` means there is nothing
to delete — the request is already satisfied.

There is no MCP tool that lists the cache; this listing is the only
inventory (`ytt_cache_bytes` is one gauge for the whole volume, not
per-unit).

## 4. The access boundary — read before promising anything

Verified live 2026-09-25 against the credential-free read-only proxy
(`kubectl --server=http://traefik-ardenone-cluster:8001`):

| Check | Result |
|---|---|
| `auth can-i get pods -n ytt` | **yes** |
| `auth can-i get pods/log -n ytt` | **yes** |
| `auth can-i create pods/exec -n ytt` | **no** |

So: **every verification step in §7 runs through the proxy or the public
endpoints — but the §3 listing and the §5 deletion need `exec`**, and exec
needs a kubeconfig granting `pods/exec` on ns `ytt`. From `codinghome` no
such kubeconfig is provisioned for `ardenone-cluster`; an agent working this
runbook stops after §3 and hands the evidence to an operator. (RUNBOOK.md
§3/§7 currently describe exec-through-proxy as allowed; that does not match
live RBAC today — the same correction CACHE-RUNBOOK §6 step 4 documents,
tracked as bead `ytt-4d76a316`.)

Two boundaries worth keeping distinct:

- Deleting files *inside* the volume is data hygiene, not a Kubernetes
  mutation: ArgoCD manages the PVC object, not its contents, and `selfHeal`
  will not resurrect a deleted transcript. No GitOps dance is needed for the
  `rm` itself.
- Every *Kubernetes* action around it still goes through the GitOps door:
  no `delete pod`, no `rollout restart`, no PVC surgery (§9). The deletion
  needs no restart at all — that is §5's first line.

## 5. The procedure

### 5.1 Pre-flight — all through the read-only proxy

```bash
KS="kubectl --server=http://traefik-ardenone-cluster:8001"
$KS get pods -n ytt                                 # exactly one ytt pod, 1/1 Running
$KS logs -n ytt deploy/ytt --timestamps | tail -20  # no crash/restart in progress
# an in-flight ASR job for this video will re-write <id>.whisper.* (§5.4):
$KS logs -n ytt deploy/ytt --timestamps | grep <id> | grep whisper_job_status_change | tail -5
```

Anything else — quiescing, draining, a maintenance window — is **not**
required. The server is built to tolerate units vanishing under it: single
replica means there is no second reader; reads, writes, and eviction take
the one process lock; and an external delete is just an ENOENT to the next
reader, which deregisters the unit and corrects the counter on that touch
(pinned by test — §10).

### 5.2 Delete

Whole video — every language, the whisper fallback, and any `.tmp` residue
of that ID, in one glob (the ID itself is `[A-Za-z0-9_-]`, so the pattern
carries no metacharacters to escape):

```bash
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- \
  sh -c 'rm -f /cache/<id>.*'
```

One language only — name both halves explicitly, never one alone (§5.3):

```bash
kubectl --kubeconfig="$KC" exec -n ytt deploy/ytt -c ytt -- \
  sh -c 'rm -f /cache/<id>.<lang>.txt /cache/<id>.<lang>.json'
```

**The `sh -c` is load-bearing.** `kubectl exec` does not run a remote
shell: an unquoted glob is expanded by — or fails in — your *local* shell,
and a quoted glob reaches the remote `rm` as a literal string that matches
nothing, which `-f` then silences. Either way the command reports success
and deletes nothing. Wrapping the payload in `sh -c` is what makes the glob
expand inside the pod, and §7.1's re-listing is what proves it worked.

### 5.3 Why both halves, and what each half alone does

- **`.txt` present, `.json` gone** — still serves. The sidecar read is
  best-effort: source defaults to `caption_auto`, segments/metadata are
  lost. Degraded, not broken (pinned in `tests/unit/test_cache_recovery.py`).
- **`.json` present, `.txt` gone** — an **orphan sidecar**. It is never
  served (a unit is discovered by its `.txt`: the scan globs `*.txt` and a
  lookup reads the `.txt` first), and **no code path ever deletes it**:
  `startup_scan` ignores it, `reconcile()` deregisters the dead unit but
  unlinks nothing, and it is invisible to `ytt_cache_bytes`. The sidecar
  carries the segments — often most of the unit's bytes — so an orphan is
  not noise, it is stranded data on the volume. This is why the procedure
  deletes both halves in one command, and why §7.1 re-lists the glob rather
  than trusting the `rm`'s exit code.

### 5.4 If the files reappear

A reappearing unit means a writer was in flight for that same video:

- a `pending`/`running` whisper job finished after your `rm` and wrote
  `<id>.whisper.*`; or
- a caption fetch for the video completed its `put` after your glob
  expanded. `put` writes `.tmp` → `os.replace` under the process lock; the
  operator's `rm` is not under that lock, so a write landing after the
  expansion survives it — and a write landing *midway* can strand an orphan
  sidecar (§5.3).

The response is ordering, not force: let the writer finish (the job reaches
`done` in `whisper_job_status_change`; a caption path logs `cache_miss` then
`cache_write` for the ID), re-run the identical §5.2 command, re-verify §7.
Never loop the `rm` against a live writer.

## 6. Blast radius — what is NOT affected

| System | Why it is untouched |
|---|---|
| OAuth state (`ytt-oauth-state` PVC, mounted at `/state` via `FASTMCP_HOME`) | A different volume with no shared path — nothing in §5 can reach it. Connected clients do not re-login. Never widen a transcript deletion into "and wipe /state too": that force-logs-out every client and has no undo (RUNBOOK §7) |
| Scratch audio (`/scratch`, emptyDir) | A different volume. The per-video scratch sweep only ever touches `/scratch/<video_id>.*` (`ytt/whisper.py`); cache deletion cannot reach it and it cannot reach the cache. In-flight audio for the video is unaffected |
| Whisper job records | In process memory only — nothing on disk to delete (retention-policy §7.4). A `done` handle whose unit you deleted is handled by the poll path itself: the cache-first lookup misses, the entry is removed, the client gets `error_code: not_found` plus the documented re-call instruction. No operator action |
| Other videos' units | The §5.2 glob contains exactly one ID; it cannot match another video's files or the lockfile (pinned mechanically — §10) |
| Rate-limit / quota budgets | Untouched by the deletion itself — but the next fetch of this video spends rate-limit budget again, and a deleted `<id>.whisper.*` re-transcribes under ASR quota (`YTT_WHISPER_JOBS_PER_HOUR`) if asked for |
| The canary (`deploy/ytt-canary`) | Stateless by manifest — no volumes at all; it never touches this PVC |
| Logs | Transcript content and segment text are never logged (retention-policy §6) — a deletion needs no matching log purge, because nothing in the retained lines reproduces what was deleted |

## 7. Verification — in order, cheapest first

1. **The glob is empty** (exec — the same command as §3's listing):
   `sh -c 'ls -la /cache/<id>.* 2>&1'` must answer `No such file or
   directory`. This one check also catches §5.4 (reappearing unit), the
   stranded-sidecar case (§5.3), and the silent no-op of a mis-quoted glob
   (§9). Anything remaining: delete by exact name, re-check.
2. **Refetch behaves** (read-only proxy): trigger a fetch of the video from
   any allowed MCP client and watch the log:
   ```bash
   $KS logs -n ytt deploy/ytt --timestamps | grep <id> | grep -E 'cache_(miss|write|hit)'
   ```
   `cache_miss` then `cache_write` (fresh copy), `cache_hit` on the second
   fetch. A miss with no write means the source fetch failed — the deletion
   worked; the video may be gone upstream (check the error event).
3. **Everything else still serves** (read-only proxy): `curl -s
   https://mcp.ardenone.com/ytt/health` → `{"status": "ok"}`; fetch any
   *other* video → `cache_hit`. No restart happened, so nothing else could
   have moved — a 30-second sanity net, not a requirement.
4. **Byte-counter honesty — the 0.2.20 wiring caveat.** Do not expect
   `ytt_cache_bytes` (`curl -s https://mcp.ardenone.com/ytt/metrics`) to
   drop. The `reconcile()` loop that would correct external drift is not on
   the boot path at 0.2.20 — nor is `startup_scan()` (bead `ytt-4f1c45c2`;
   RUNBOOK §2.2) — so a unit written *since the last restart* leaves phantom
   bytes in the gauge until that key is next touched (the §7.2 miss
   deregisters it and corrects the counter) or the pod next swaps. Post-fix,
   `reconcile()` corrects the drift within `YTT_CACHE_RECONCILE_SEC` (300 s)
   and logs the `reconcile` event with `drift_bytes`, and
   `cache_startup_scan` reports `units_found` on boot. Do not bounce the pod
   to "clean the gauge" — a Recreate swap costs in-flight ASR jobs
   (RUNBOOK §2.1) and buys a number that already self-corrects on touch.
5. **Record it**: ID, languages deleted, bytes freed (§3's `wc -c` before,
   §7.1's absence after), and the refetch evidence — on the bead or PR for
   the operation. A deletion without a record is an audit hole (same rule
   as CACHE-RUNBOOK §9).

## 8. Recovery — what this cannot undo

**There is no undelete.** The cache volume has no backups by design —
verified 2026-09-25: no Longhorn recurring jobs on the volume, no backup
resources in the cluster (CACHE-RUNBOOK §2). `rm` is the whole operation;
recovery means re-creation from source:

- **Caption units** (`<id>.<lang>.*`): the next request re-fetches from
  YouTube and re-writes the unit — deterministic content, free beyond one
  fetch's rate-limit budget. Effectively no loss.
- **Whisper units** (`<id>.whisper.*`): the next request for any language of
  that video re-downloads the audio and re-transcribes — an ASR quota spend
  (`YTT_WHISPER_JOBS_PER_HOUR`), minutes of latency, and it only works if
  YouTube still serves the video.
- **If the source is gone, it is gone.** Video deleted, private, or
  age/region-gated upstream: a deleted transcript is unrecoverable by any
  means ytt has. For a planned deletion of a unit you might conceivably want
  back, snapshot **before** deleting — a Longhorn VolumeSnapshot is a
  manifest change (declarative-config → GitOps door; CACHE-RUNBOOK §2), so
  it must be arranged ahead of time. There is no after-the-fact option.
- **Clients mid-pagination** on the old copy: cursors are content-hash
  bound; the next page returns `cursor_stale` and the client restarts at
  page 1 of the fresh copy — never a silently wrong continuation. That is
  the documented client contract, not an incident.

## 9. What NOT to do

| Tempting move | Why it's wrong |
|---|---|
| `kubectl exec … -- rm -f /cache/<id>.*` without `sh -c` | No remote shell: an unquoted glob expands (or fails) in your *local* shell; a quoted one reaches remote `rm` as a literal that matches nothing, and `-f` silences the miss — **success exit, zero deletions** (§5.2) |
| `rm -f /cache/*.en.*` — "just the English ones" | A language glob across *every* video. Scope is one ID, always (§1); bulk shape is CACHE-RUNBOOK §6's oldest-first procedure |
| Deleting just the `.txt` ("the json is only metadata") | Strands an orphan sidecar no code path ever cleans — segments and most bytes stay on the volume (§5.3) |
| `rm -rf /cache` or `rm -f /cache/*` | Takes the mountpoint contents including `.ytt-singleton.lock` and any dotfile added later; a genuine fresh start is a manifest-driven PVC replacement (CACHE-RUNBOOK §6 step 5), not a shell glob |
| `rm` on `/state/…` or `/scratch/…` "while you're in there" | Different volumes, different procedures; `/state` force-logs-out every client with no undo (§6) |
| `kubectl delete pod` to "apply" or "clean up after" the deletion | Forbidden (RUNBOOK §7), loses in-flight ASR jobs, and is pointless — there is nothing to apply; the server already knows (§5.1) |
| `kubectl delete pvc ytt-cache` | Destroys every unit with no undo and is a live mutation ArgoCD reverts (RUNBOOK §7) |
| Deleting a unit to "refresh" a transcript | No TTL by design (§1): the next fetch refetches anyway; the deletion only adds a refetch you didn't need |

## 10. Pins

The load-bearing claims in this runbook are mechanical, pinned by
`tests/unit/test_deletion_runbook.py`: the §5.2 glob matches exactly one
video's files and never the lockfile or another video; IDs are
case-sensitive; the §3 URL→ID table is `canonicalize()` itself; the §5.3
half-unit behaviors (serve-without-sidecar; the orphan sidecar no scan
discovers and no code path removes); external deletion tolerated under a
live registry with counter correction on touch; the §6 manifest facts
(volumes, mounts, the stateless canary); and the literals §7's verification
depends on. If any of these drift — code or manifest changed, runbook
didn't — the DoD gate fails until the doc follows.
