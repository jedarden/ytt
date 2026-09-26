# Whisper Job Lifecycle & Restart Contract

This note specifies the async ASR job contract: the job state machine, what
each tool call returns in every state, when jobs and files are cleaned up, and
what a process restart does to the in-memory registry and the scratch volume.
It is the reference for `tests/unit/test_whisper_contract.py`, which pins every
clause below end to end (real registry + real cache + real `run_whisper_job`,
download/POST stubbed, driven through the real MCP tools).

Sources: plan §Whisper fallback / §Tools / §Response shape, `docs/notes/single-replica.md`,
`docs/usage/configuration.md` (job TTL, scratch sweep).

## 1. Job states — the registry FSM

A job is a `WhisperJob` (`ytt/models.py`) held in the process-local
`WhisperJobRegistry` (`ytt/whisper.py`), keyed by the canonical 11-character
`video_id`. One registry entry per video, ever; all reads and transitions run
under one `asyncio.Lock`. Each entry also records its `owner` — the
authenticated subject (`ytt.server._request_subject`) of the call that
created it — which the polling contract below binds the job handle to
(ownership rule: [auth.md §Job ownership](auth.md#job-ownership--whisper-asr-handles-are-per-subject)).

| Transition | Driven by | Side effect | Log event |
|---|---|---|---|
| *(absent) →* `pending` | `get_or_create` (tool: start or re-kick) | `created_at`, `eta_sec`, `duration_sec`, `owner` recorded | `whisper_job_created` |
| `pending` → `running` | `run_whisper_job` first act | `started_at` set | `whisper_job_status_change` |
| `running` → `done` | successful cache write | `result_ref = "<id>.whisper"` | `whisper_job_status_change`, `whisper_job_done` |
| `running` → `error` | any failure (download, POST, unexpected) | stable `error_code` + verbatim-relayable `message` | `whisper_job_error` / `whisper_job_unexpected_error` |
| `pending`/`running`/`done`/`error` → *(absent)* | TTL GC, or the evicted-result poll path | registry entry deleted | `whisper_job_ttl_gc`, `whisper_job_stale_running` |

- `done` and `error` are **terminal**: no transition leaves them. The only way
  they leave the registry is removal (TTL GC, stale GC, evicted-result poll).
- The FSM never skips `running`: every job that starts work passes through it.
- Only the background job task drives `pending → running → terminal`; tool
  handlers only read (plus the one removal on the evicted-result poll path).
- A `done`/`error` entry is **never joinable**: `get_or_create` replaces it
  with a fresh `pending` job (log event `whisper_job_replaced_terminal`). This
  is what makes the documented retry — *"re-call get_youtube_transcript"*
  below — actually restart work instead of dead-ending on the old entry.

## 2. Start contract — `get_youtube_transcript` on no captions

Trigger: cache-first lookup misses → caption fetch raises `empty_body`
(`NoCaptionsError`, which carries `duration_sec` when known).

Gate order (all before any transcription work):

1. **Queued-work cap** — if no job exists for this video and
   `pending + running ≥ YTT_MAX_PENDING_WHISPER_JOBS`, deny with
   `status=error, error_code=rate_limited` ("Whisper queue full"). Spending no
   quota slot. Joining an in-flight job is always allowed and skips this gate.
2. **Per-subject ASR quota** (`YTT_WHISPER_JOBS_PER_HOUR`) — charged before
   get-or-create; refunded when the call turns out to join an existing job or
   no job gets created. A retry after a terminal job is *new work* and is
   charged as such.
3. **Duration cap** — `duration_sec > YTT_MAX_ASR_DURATION_SEC` refuses with
   `too_long_for_asr` *before* registration: the registry stays empty, no
   quota is spent, no audio is touched.
4. **Get-or-create** under the registry lock. Joining (`is_new=False`)
   returns the existing in-flight job; only `is_new=True` starts one bounded
   background task (`_run_whisper_job_bounded`, holding one
   `YTT_MAX_CONCURRENT_WHISPER` slot for the job's whole lifecycle). Created
   jobs record the calling subject as `owner` (required keyword): the work is
   shared with any later requester, but the pollable handle belongs to the
   creator alone (§3).

Start response (the only response shape that introduces a job):

```json
{"video_id": "…", "status": "pending", "eta_sec": 60.0,
 "message": "No captions found. Transcribing with Whisper ASR (~60s). Ask me again shortly."}
```

`eta_sec = duration_sec × YTT_WHISPER_REALTIME_FACTOR` when the duration is
known, else `null` (the message then omits the ETA). Invariant 7 guarantees
the ETA budget can't outlive the timeout:
`YTT_MAX_ASR_DURATION_SEC × YTT_WHISPER_REALTIME_FACTOR < YTT_WHISPER_TIMEOUT_SEC`.

## 3. Polling contract — `get_transcript_job`

One tool, one argument (`video_id`), read-only. **Ownership gate first**: the
polling call's authenticated subject must match the job's recorded `owner` —
a mismatch answers with the *same* `not_found` an absent job gets (byte-for-
byte, so job ids are not enumerable across subjects; the denial is logged as
`transcript_job_poll_denied` with hashed subjects only). Every row below is
then reachable by the owner alone. State → response:

| Registry state | Response |
|---|---|
| absent, **or owned by another subject** (`not_found`) | `status=error, error_code=not_found`, message instructs re-calling `get_youtube_transcript` |
| `pending` | `status=pending`, `eta_sec`, "queued" message |
| `running` | `status=running`, `eta_sec`, "in progress" message |
| `error` | `status=error`, `error_code=<job's stable code, default asr_failed>`, the job's verbatim-relayable `message` (a default when the job recorded none) **plus** the fixed re-call-to-retry instruction |
| `done`, cache unit present | the transcript itself — `build_page(mode="full")`: `status=ok`, `source=whisper`, `lang=<detected>`, `transcript_quality`, text/segments, `is_final=true` |
| `done`, cache unit **evicted** | `status=error, error_code=not_found` ("evicted") **and the registry entry is removed** — the next poll is a plain `not_found` |

The job's internal `done` is never a client-visible status value; it surfaces
only as a delivered transcript (`status=ok`). Unified status set at the tool
surface: `ok | partial | pending | running | error`.

**Polling idempotence** — the contract that makes client retry loops safe:

- Polls are **free**: no rate-limit token, no ASR quota charge (fail-closed
  budgets stay exhausted and polling still works — a client waiting on one
  transcription must not drain its own budget).
- Polls are **read-only**: no state transition, no field mutation, no task
  started, registry size unchanged. The single documented exception is the
  evicted-`done` removal above.
- Polls are **repeatable**: the same state yields the same response shape;
  a `done` job yields the same transcript on every poll until TTL GC or
  eviction; an `error` job yields the same error on every poll.
- Polling **never re-triggers work**. Work restarts only via
  `get_youtube_transcript` (the re-kick), never via `get_transcript_job`.

## 4. not_found and expiration

`not_found` is a logical error emitted by `get_transcript_job` only (never a
yt-dlp taxonomy code). Four ways in, one recovery:

1. **Unknown id** — never registered (or a different replica: the registry is
   process-local, see `docs/notes/single-replica.md`).
2. **Expired** — TTL GC removed the entry (below).
3. **Evicted result** — `done` job whose `<id>.whisper.*` cache unit was
   evicted between completion and the first poll.
4. **Foreign owner** — the job exists but was started by a different
   authenticated subject (ownership gate, §3). The payload is byte-identical
   to way #1 — callers cannot probe which video ids have live jobs. The
   recovery below is also exactly right for this case: a re-kick joins the
   in-flight work or answers from the shared cache, and a re-kick after a
   terminal job starts a fresh one *owned by the re-kicking subject*.

Recovery is always the same **idempotent re-kick**: re-call
`get_youtube_transcript` with the original URL. Cache-first answers instantly
if the unit survived (clause 3 above can then never recur); otherwise a *new*
job starts (fresh `pending` + ETA). The re-kick always terminates in either a
transcript or a fresh `pending` — never an error, never a dead-end loop.

Expiration (`run_ttl_gc`, driven by the registry's GC loop every 60 s):

| Entry | Removed when | Note |
|---|---|---|
| `done` / `error` | age (`created_at`) > `YTT_JOB_TTL_SEC` | result lives on in the cache; only the handle expires |
| `running` | age (`started_at`, else `created_at`) > `YTT_WHISPER_TIMEOUT_SEC + YTT_JOB_TTL_SEC` | a task that can no longer be alive; logged at ERROR (`whisper_job_stale_running`) |
| `pending` | never (no TTL) | queued work is legitimate; the queue cap bounds it instead |

Until expiry, a terminal entry stays pollable (repeatable response, clause 3).

## 5. Cleanup timing

| What | When | Where |
|---|---|---|
| Downloaded audio file | `run_whisper_job` `finally` — success **or** failure (Invariant 4: audio always deleted) | `ytt/whisper.py` |
| Partial `{video_id}.*` scratch files (timed-out/aborted downloader) | after **every** attempt, success or failure — `_sweep_video_scratch` in the same `finally` | bounds scratch to files of jobs actually in flight |
| Terminal job handles | TTL GC, 60 s cadence (clause 4) | `WhisperJobRegistry.run_ttl_gc` |
| Stale `running` handles | TTL GC at the timeout+TTL threshold | same |
| **Every** file in `YTT_SCRATCH_DIR` | **server boot** — `startup_sweep`, unconditional | safe because `replicas:1` + `strategy:Recreate` + the singleton flock guarantee no peer is running |
| Failed transcripts | **never cached** — an `error` job writes no cache unit; errors are not transcripts | `run_whisper_job` writes the unit only on success |

Scratch hygiene is safe against the zombie downloader: a timed-out
`asyncio.to_thread` yt-dlp keeps writing to an unlinked inode that dies with
the process; `video_id` is canonicalized so the sweep glob has no
metacharacters and cannot match another video's files.

## 6. Restart contract

What a process restart does, and how each surface recovers:

| State | Survives restart? | Consequence |
|---|---|---|
| `WhisperJobRegistry` entries | **No** — in-memory | any poll of a pre-restart job returns `not_found`; recovery is the standard re-kick |
| In-flight job tasks | **No** | the work (download + POST) is lost and re-done on re-kick; at most one bounded download's worth of effort |
| Cached transcripts (`YTT_CACHE_DIR`) | **Yes on PVC; no on emptyDir** (documented tradeoff, plan §Caching) | after a restart the re-kick is answered from cache — the client may never see `not_found` |
| Scratch files | Files **yes, until swept** | `startup_sweep` deletes them all at boot; anything on the volume is stale by definition (single replica, `Recreate`) |

Boot sequence contract (per process start): `startup_sweep(YTT_SCRATCH_DIR)` →
whisper model guard (`GET /v1/models`, self-correct `YTT_WHISPER_MODEL`) →
cache `startup_scan` (re-index pre-restart units so cache-first sees them) →
registry starts empty → TTL GC loop starts. Client-visible restart recovery is
then always: poll → `not_found` → re-kick → cached answer or fresh `pending`.

## 7. Wiring status (2026-09-25)

The contract above is enforced at two layers — the tool handlers and the job
task — but the boot-time mechanisms are **specified, implemented as
components, unit-tested, and not yet wired into `serve()`**:

| Mechanism | Invoked by | Wired? |
|---|---|---|
| FSM transitions, get-or-create + terminal replacement, quota/queue gates | tool handlers | ✅ live |
| Audio deletion + per-video scratch sweep | `run_whisper_job` `finally` | ✅ live |
| Polling (all six shapes), evicted-result removal | `get_transcript_job` | ✅ live |
| `startup_sweep` | `serve()` | ❌ not called — stale scratch survives restarts in production |
| TTL GC loop (`run_ttl_gc`, stale-running GC) | registry task started at boot | ❌ never started — terminal handles accumulate in memory only (a terminal handle holds no queue capacity — `active_count` totals `pending`+`running`, see deploy/ASR-RUNBOOK.md §9 — it just stays in the registry until the process ends), `running` handles are never reaped |
| `check_model_guard` | `serve()` | ❌ not called — configured model is never self-corrected |
| cache `startup_scan` / `start_reconcile_task` | `serve()` | ❌ not called — pre-restart cache units are invisible to the in-memory index after a restart (cache-first then misses until a `put` re-adds the unit) |

Everything else in this document is true of the running server today; the
❌ rows are the gap between the specified boot sequence and `ytt/server.py::serve`.
Wiring them is tracked as a follow-up bead (they need an ASGI-lifespan home in
the uvicorn event loop — fastmcp 3.4.2's `http_app()` takes no `lifespan`
kwarg, but the returned `StarletteWithLifespan` exposes
`router.lifespan_context`, which can be chained). The contract tests pin the
mechanisms so the wiring bead can land against an already-specified contract.

## 8. Where each clause is tested

- `tests/unit/test_whisper_contract.py` — this contract end to end: start
  (pending shape, ETA, join-not-duplicate, duration cap), polling (all six
  shapes, idempotence, budget-free), success (lifecycle → delivered
  transcript → repeatable → cache-first re-kick), failure (stable error,
  never cached, no silent re-trigger, re-kick restarts), expiration (TTL GC →
  `not_found`), restart (registry lost → `not_found` → re-kick; completed
  result survives in cache), stale scratch (sweep counts/bytes, idempotence,
  dirs untouched).
- `tests/unit/test_whisper.py` — component internals (FSM unit transitions,
  GC unit math, model guard, download guards, sweep globs).
- `tests/unit/test_server.py` — individual tool shapes and the quota/queue
  gates in isolation.
- `tests/unit/test_job_ownership.py` — the ownership gate end to end: owner
  vs. stranger polls across pending, running, done, and error jobs, the
  byte-identical `not_found` for cross-subject/unknown ids, normalized
  subject keys, re-kick re-ownership, join-shares-work-not-handle, and the
  owner-less scaffolding affordance (spec: `docs/notes/auth.md` §Job
  ownership).
