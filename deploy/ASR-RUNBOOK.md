# ytt Operator Runbook — ASR failure & queue exhaustion (Whisper fallback)

Everything that can go wrong on the caption-less path: what the caller sees
in every job state, what the operator can observe while it happens, what is
worth remediating and how, and what a restart costs.  Covers the states the
job FSM and the tool surface can actually emit — `pending`, `running`,
`done` (delivered as `status=ok`), `error` (`asr_failed` and friends),
queue-full and quota-exceeded denials (`rate_limited`), `too_long_for_asr`,
`not_found` — plus stale-running GC, TTL GC, and restart recovery.

Related docs:

| Doc | Covers |
|---|---|
| [docs/notes/whisper-lifecycle.md](../docs/notes/whisper-lifecycle.md) | The job FSM/polling/restart contract this runbook operates — clause-level reference, pinned by `tests/unit/test_whisper_contract.py` |
| [RUNBOOK.md](RUNBOOK.md) | Upgrade/rollback swaps, §2.1 "in-flight ASR jobs are lost", forbidden kubectl |
| [CACHE-RUNBOOK.md](CACHE-RUNBOOK.md) | The cache volume ASR results are written to; scratch-volume §7 |
| [CANARY-MONITORING-RUNBOOK.md](CANARY-MONITORING-RUNBOOK.md) | Fetch-path (caption) failures — the canary never probes ASR |
| [docs/usage/configuration.md](../docs/usage/configuration.md) | Every env var named below, with defaults |

Facts here are pinned, not remembered: `tests/unit/test_asr_runbook.py` (§12)
runs the acceptance scenarios and drift-guards this document against the
code and the manifests.

## 1. The path that fails

```
get_youtube_transcript(url)
  → cache-first miss (no captions ever cached for this video)
  → yt-dlp caption fetch → empty_body = NoCaptionsError   ← the ASR trigger
  → queue gate (backlog cap)        → rate_limited "Whisper queue full"
  → quota gate (per-subject bucket) → rate_limited "Whisper ASR quota exhausted"
  → duration cap                    → too_long_for_asr (no job, no spend)
  → get_or_create → pending → background task:
        pending → running → download bestaudio (≤ YTT_WHISPER_TIMEOUT_SEC)
                          → POST /v1/audio/transcriptions
                          → done  (cache unit <id>.whisper) | error (asr_failed, …)
get_transcript_job(video_id)   ← free, read-only, repeatable polling
```

Two different things are called "the queue": the **job backlog** the backlog
cap bounds (§2), and the one-at-a-time **concurrency semaphore**
(`YTT_MAX_CONCURRENT_WHISPER`, 1 in the reference deployment).  A job holds
one semaphore slot from `pending → running` all the way to `done`/`error`;
queued (`pending`) jobs are waiting for that slot.  Every slow Whisper is
therefore also a queue problem (§8).

## 2. Capacity model — three budgets and the drain rate

| Budget | Env (default) | Manifest | Meaning |
|---|---|---|---|
| Concurrency | `YTT_MAX_CONCURRENT_WHISPER` (1) | 1 | Transcriptions running at once. The shared Whisper box is CPU-bound; this is the drain-rate knob |
| Backlog cap | `YTT_MAX_PENDING_WHISPER_JOBS` (16) | *(unset → 16)* | pending + running combined. New jobs denied above it; joining never denied. `0` = deny all new ASR jobs |
| Per-subject quota | `YTT_WHISPER_JOBS_PER_HOUR` (10) | 10 | New jobs per subject per rolling hour (token bucket, starts full, refills `jobs/3600` per s). `0` = deny-all for new jobs; polls stay free |

Timeouts bounding one job, both `2880` s in the reference deployment:

- **Audio download** — one `asyncio.wait_for` of `YTT_WHISPER_TIMEOUT_SEC`
  around the yt-dlp download (direct-first, one proxy retry on `ip_blocked`).
- **Whisper POST** — httpx `read`/`write` timeouts of
  `YTT_WHISPER_TIMEOUT_SEC` (connect fixed at 10 s).

So worst case a single wedged job holds its concurrency slot ≈
2880 + 2880 = 5760 s (~96 min).  Invariant 7 (startup-validated) guarantees
the *promised* ETA can never exceed the timeout:
`YTT_MAX_ASR_DURATION_SEC (1200) × YTT_WHISPER_REALTIME_FACTOR (2.0) = 2400 < 2880` —
don't widen one without the other (§11).

**Drain rate** = `YTT_MAX_CONCURRENT_WHISPER` jobs per job-duration.  With
the defaults, a saturated queue of healthy ~2-min videos drains in ~30 min;
a queue of worst-case wedged jobs is ~96 min **each**.  A full backlog is a
slow-burn problem, not a crash — the service keeps serving captions the
whole time (§8).

## 3. Caller-visible behavior, state by state

Tool-surface statuses are `ok | partial | pending | running | error`; the
registry's internal `done` is never client-visible — it surfaces as a
delivered transcript.  Match on `error_code`, never on message text.

| Caller sees | From | Caller should | Operator correlation |
|---|---|---|---|
| `status=pending` (+ `eta_sec` when duration known) | job created or joined | relay the ETA; poll `get_transcript_job` later | `whisper_job_created` (join: no new event, no quota spent) |
| `status=running` (+ remaining ETA) | mid-transcription | keep polling | `whisper_job_status_change pending→running` |
| `status=ok`, `source=whisper` | `done` + cache hit | deliver the transcript; re-polls repeat it | `whisper_job_done`; repeatable until TTL GC/eviction |
| `status=error`, `error_code=asr_failed` | download or POST failed (§5–§7) | relay the message; retry by re-calling `get_youtube_transcript` — the re-kick is new work and spends quota again | `whisper_job_error` with the same code |
| `status=error`, `error_code=rate_limited`, "Whisper **queue full** (16/16 …)" | backlog at cap (§8) | wait and retry later | `ytt_rate_limited_total{subject_hash}` ticked |
| `status=error`, `error_code=rate_limited`, "Whisper ASR **quota exhausted** (10 jobs/hour …)" | per-subject bucket empty | wait for the `~Ns` hint in the message | same counter (§4) |
| `status=error`, `error_code=rate_limited`, "Rate limit exceeded" | plain per-minute fetch limit | wait for the hint | same counter |
| `status=error`, `error_code=too_long_for_asr` | duration/size cap before any work | don't retry this video | nothing started — no job, no quota spend |
| `status=error`, `error_code=not_found` | unknown / GC-expired / evicted handle, or a pre-restart job (§10) | re-call `get_youtube_transcript` (idempotent re-kick) | registry has no such id |

Every `asr_failed` message is verbatim-relayable and appends the fixed
recovery line "Re-call get_youtube_transcript with the video URL to retry."
The message prefixes discriminate the failure:

| Message prefix | Phase | Meaning |
|---|---|---|
| `Audio download timed out after …s.` | download | yt-dlp blew the whole `YTT_WHISPER_TIMEOUT_SEC` budget (egress/proxy problem, not Whisper — CANARY-MONITORING-RUNBOOK territory) |
| `Whisper service timed out: …` | POST | connected, but transcription blew the read timeout — Whisper alive but saturated (§7) |
| `Whisper service error <status>: <body>` | POST | Whisper answered with 4xx/5xx (§6/§7; a 404 naming a model means `YTT_WHISPER_MODEL` is wrong — §7) |
| `Whisper service request failed: …` | POST | no answer at all — connection refused/DNS/unset URL (§5/§6) |
| `Unexpected error during transcription: …` | any | bug-shaped; treat like `asr_failed` and read the pod log (`whisper_job_unexpected_error`, with stack) |

## 4. Diagnostics — what actually moves

All read-only, through the credential-free proxy:

```bash
KS=http://traefik-ardenone-cluster:8001

# structured log trail (JSON lines, structlog)
kubectl --server=$KS logs -n ytt deploy/ytt --timestamps | grep whisper_job_created
kubectl --server=$KS logs -n ytt deploy/ytt --timestamps | grep whisper_job_status_change | tail
kubectl --server=$KS logs -n ytt deploy/ytt --timestamps | grep whisper_job_error | tail
kubectl --server=$KS logs -n ytt deploy/ytt --timestamps | grep -c "Whisper queue full"

# the ASR service itself (same cluster, other namespace)
kubectl --server=$KS get pods -n whisper-stt
```

Log events (all live):

| Event | Level | Meaning |
|---|---|---|
| `whisper_job_created` | INFO | new job (with `eta_sec`, `duration_sec`) |
| `whisper_job_replaced_terminal` | INFO | a re-kick replaced a `done`/`error` handle — retry traffic |
| `whisper_job_status_change` | INFO | every FSM transition (`old_status`/`new_status`) |
| `whisper_job_done` | INFO | success; cache unit written |
| `whisper_job_error` | WARNING | job failed; carries the stable `error_code` |
| `whisper_job_unexpected_error` | ERROR | non-`YttError` exception, with stack |
| `whisper_job_stale_running` | ERROR | stale-running GC reaping a zombie (§9 — **unwired at 0.2.21**) |
| `whisper_job_ttl_gc` | INFO | GC removed terminal handles (§9 — **unwired**) |
| `whisper_scratch_swept` / `whisper_audio_deleted` | DEBUG | per-job audio cleanup (Invariant 4) |
| `audio_size_unknown` | WARNING | download proceeded without a projected size |

Metrics — **one live signal, several registered-but-inert ones.  Know the
difference before trusting a quiet dashboard:**

| Series | State | Note |
|---|---|---|
| `ytt_rate_limited_total{subject_hash}` | **live** | ticks on every denial — rate limit, queue full, and quota alike (`subject_hash` = first 8 hex of sha256). It cannot tell you *which* denial; the log/messages do |
| `ytt_whisper_errors_total{reason}` | registered, **never incremented** | no call site in 0.2.21, so the series is absent |
| `ytt_whisper_job_seconds` | registered, **never observed** | absent |
| `ytt_queue_depth` | registered, **never set** | also fetch-queue-shaped, not the ASR backlog |

Consequence: the **`YttWhisperDown` alert cannot fire** — its expression is
`rate(ytt_whisper_errors_total[10m]) * 60 > 0.5`, and an absent series never
fires.  Until the counter is instrumented, the ASR-down detector is the log
grep above (`whisper_job_error` sustained ≈ what the alert intends), plus
the caller-visible symptom wall in §6.  `ytt_whisper_errors_total`,
`ytt_whisper_job_seconds` and `ytt_queue_depth` are pinned as
registered-but-uninstrumented by the acceptance suite so a future
instrumentation change updates this section in the same commit.

## 5. Scenario — Whisper unset (no-Whisper mode)

`YTT_WHISPER_URL` has a default (the reference in-cluster endpoint,
`http://whisper-openai.whisper-stt.svc.cluster.local:8000`) — *unset never
means disabled*, it means "point at the reference service".  Deliberate
no-Whisper operation is a deployment choice: set the variable to an
unreachable address (documented in docs/usage/configuration.md), or to an
empty value.

What callers see, either way:

1. Caption-less video → `pending` (the job gates never probe Whisper — job
   creation succeeds with Whisper down or absent).
2. The job runs, downloads audio, then the POST fails immediately
   (`request failed`) → poll returns `asr_failed`.
3. Captioned videos are unaffected, always.

Operator expectations: startup is **not** blocked by an absent/unreachable
Whisper (the boot-time model guard swallows connection errors by design),
health stays green, and the only signals are `whisper_job_error` /
`asr_failed` on caption-less requests.  If you *meant* to disable ASR,
nothing to fix; if not, restore `YTT_WHISPER_URL` via declarative-config
(GitOps door — RUNBOOK §7).  Failed jobs still spend their quota slot
(refunds happen only on joins/caps/registry faults), so a busy no-Whisper
deployment also burns subjects' hourly budgets — one more reason to disable
deliberately (`YTT_WHISPER_JOBS_PER_HOUR=0`) instead of letting jobs fail.

## 6. Scenario — Whisper unreachable (down)

**Symptoms, in order:** caption-less requests still get `pending`; each job
fails after its download phase with `Whisper service request failed` (a
down-but-resolvable service) or `error <status>` if something answers badly;
`whisper_job_error` at WARNING in the pod log; callers polling get
`asr_failed` + the re-kick line.  No crash, no health impact, captions
unaffected.

**Diagnose:**

1. Confirm from the messages: `kubectl --server=$KS logs -n ytt deploy/ytt
   --timestamps | grep whisper_job_error | tail` — `request failed` with a
   connect error is "service down/unreachable"; `error 503` is "alive but
   refusing" (§7).
2. Check the service side: `kubectl --server=$KS get pods -n whisper-stt`
   — CrashLoopBackOff/OOM/0/1 there is the incident; the ytt side is the
   messenger.
3. Check the URL hasn't drifted: the `Server startup` log line prints
   `whisper_url`/`whisper_model`; compare with the env block of
   `deploy/k8s/ardenone-cluster/ytt/deployment.yml`.

**Remediate:** fix the whisper-stt deployment (its own repo/runbook — ytt
owns neither).  Nothing on the ytt side needs touching for a transient
outage: jobs fail fast (~10 s connect timeout), callers re-kick, and quota
absorbs the retry pressure.  **Do not** raise `YTT_WHISPER_TIMEOUT_SEC` to
"wait out" an outage — connect failures aren't timeouts, and the knob is
Invariant-7-coupled (§11).

## 7. Scenario — Whisper overloaded (slow, 429/503, wrong model)

**Symptoms:** jobs take the full ETA (or blow it → `Whisper service timed
out`); bursts of `Whisper service error 429/503` messages; the backlog
climbs (§8) as new jobs queue behind slow ones; queue-full denials start
appearing once pending+running hits 16.

Failure-shape → cause:

| Shape | Cause | Lever |
|---|---|---|
| `error 429` / `error 503` in messages | the service sheds load | slow down: `YTT_MAX_CONCURRENT_WHISPER` is already 1; the pressure is *queue depth*, see §8 |
| `Whisper service timed out` sustained | CPU-starved service, jobs slower than `YTT_WHISPER_REALTIME_FACTOR` assumes | fix Whisper capacity; recalibrate the factor so ETAs are honest (docs change, GitOps door) |
| `error 404` whose body names a model | `YTT_WHISPER_MODEL` not served by the endpoint | set a model the service actually serves. The boot-time model guard that would self-correct this is **unwired at 0.2.21** (§9), so a wrong name fails every job until the manifest is fixed |

**Caller impact:** subjects whose jobs keep failing also drain their
`YTT_WHISPER_JOBS_PER_HOUR` budget (no refund on failure — refunds are only
for joins/caps/registry faults).  A caller that automates re-kicks in a
tight loop will exhaust its own quota; that is the quota gate doing its job.

## 8. Queue exhaustion (backlog at `YTT_MAX_PENDING_WHISPER_JOBS`)

**What it looks like:** new caption-less requests denied with
`error_code=rate_limited`, message "Whisper queue full (16/16 jobs pending
or running). Try again later." — no ETA, no quota spent.  Joining an
already in-flight job is still admitted at full backlog (it adds no work)
and is free.  Captioned videos never touch this gate.

**Why it fills:** drain rate is 1 job at a time (§2).  A burst of
caption-less requests, or Whisper gone slow/down (§6/§7), or a single wedged
job (worst case ~96 min) all monetize the same backlog.  Remember the
cap counts `pending + running`: 15 queued + 1 running = full.

**Assess:** count denials and look at the live transitions —

```bash
kubectl --server=$KS logs -n ytt deploy/ytt --timestamps | grep -c "Whisper queue full"
kubectl --server=$KS logs -n ytt deploy/ytt --timestamps | grep whisper_job_status_change | tail -20
```

A backlog draining (steady `pending→running`/`→done` pairs) needs no
action; one that never moves means the head job is wedged or Whisper is
down (§6).

**Remediate, in order of preference:**

1. **Nothing** — the cap exists so callers get an honest "later" instead of
   an unbounded wait.  Queues formed by a burst drain on their own.
2. **Fix the head** — a wedged/slow Whisper is the usual cause (§6/§7).
3. **Raise `YTT_MAX_PENDING_WHISPER_JOBS`** (declarative-config) only if
   denials are the actual complaint and the service can catch up; a deeper
   queue lengthens worst-case wait per caller.  `0` disables new ASR jobs
   entirely — a deliberate circuit breaker, fail-closed like the other `0`
   conventions.
4. **Restart** (GitOps door, RUNBOOK §2.1) resets the registry, all budgets
   and the queue — and destroys every in-flight job (§10).  Schedule it;
   don't reflex it.

## 9. GC — TTL and stale-running: specified vs wired

Specified (and implemented, and unit-tested — `WhisperJobRegistry.run_ttl_gc`,
60 s cadence once started):

| Entry | Removed when | Log |
|---|---|---|
| `done`/`error` | age > `YTT_JOB_TTL_SEC` (3600 s) | `whisper_job_ttl_gc` |
| `running` | age (`started_at`, else `created_at`) > `YTT_WHISPER_TIMEOUT_SEC + YTT_JOB_TTL_SEC` = 6480 s | `whisper_job_stale_running` (ERROR) |
| `pending` | never — queued work is legitimate; the backlog cap bounds it | — |

**Wiring status at 0.2.21: the GC loop is never started.**  As with the
cache startup scan (RUNBOOK §2.2, CACHE-RUNBOOK §3), the boot-path wiring is
tracked by bead `ytt-4f1c45c2`; `serve()` has no call site today.  Honest
consequences:

- **Terminal handles accumulate until the next restart.**  Memory only, and
  small — a handle does *not* hold queue capacity (the backlog counts
  pending+running only) and does not block a re-kick (terminal entries are
  replaced, §3).  The cost is registry growth and stale `done` handles
  staying pollable past the documented hour.
- **Stale-running reaping never happens.**  A zombie `running` handle would
  hold a concurrency slot and backlog capacity indefinitely.  This is
  largely theoretical while every phase is timeout-bounded (§2 worst case
  ~96 min < the 6480 s stale threshold), but the belt-and-braces is off.
- The stale threshold exceeding the worst legitimate job lifetime is the
  design margin — if you raise timeouts, re-check it (§11).

Until the wiring lands, **a planned restart is the only reaper** (§10).

## 10. Restart cases — what a swap does to ASR state

Restarts are manifest-change-driven only (declarative-config → ArgoCD →
Recreate swap; `kubectl delete pod`/`rollout restart` are forbidden —
RUNBOOK §7).  RUNBOOK §2.1 says "in-flight jobs are lost"; here is the full
ledger:

| State | Survives? | Caller experience |
|---|---|---|
| Registry entries (pending/running/done/error) | **No** — in-memory | any poll → `not_found` → re-kick (idempotent, §3) |
| In-flight work (download + POST) | **No** | lost; a re-kick redoes it — at most one bounded download of wasted effort |
| Rate-limit buckets, quota buckets, single-flight map | **No** | budgets reset to full; a mid-window re-kick is admitted again |
| Cached transcripts (`ytt-cache` PVC) | Files yes | served again only once the cache inventory is rebuilt — **unwired at 0.2.21** (`ytt-4f1c45c2`), so a post-restart re-kick of an already-done video re-fetches captions and, if none exist, starts a *new* ASR job (fresh quota spend, fresh ETA). Post-fix the same re-kick is answered from cache instantly |
| Scratch audio (`emptyDir`) | **No** — dies with the pod | by design; the boot sweep (`startup_sweep`) that would clear a same-dir restart is likewise **unwired** at 0.2.21 — on k8s the emptyDir reset makes it moot |

Operational reading: a restart is also the *reset lever* for a wedged queue
or a bloated registry (§8/§9) — but it is paid for in lost jobs and, until
the cache wiring lands, in re-transcription quota.  Schedule ASR-heavy
swaps for quiet periods (RUNBOOK §2.1) and let the backlog drain first.

## 11. What NOT to do

| Tempting move | Why it's wrong |
|---|---|
| `kubectl delete pod` / `rollout restart` to "clear the queue" | Forbidden live mutation (RUNBOOK §7); selfHeal reverts it; use the GitOps door if a restart is genuinely the remedy (§10) |
| Raise `YTT_WHISPER_TIMEOUT_SEC` to wait out a slow/outaged Whisper | Invariant 7 (startup-validated): `YTT_MAX_ASR_DURATION_SEC × YTT_WHISPER_REALTIME_FACTOR` must stay below it, or the pod exit-1s into CrashLoopBackOff; and connect-refused failures don't wait for timeouts anyway |
| Raise `YTT_MAX_CONCURRENT_WHISPER` "to drain faster" | The Whisper box is CPU-bound; concurrency multiplies the slowness and the 429s (§7). It's the right lever only after Whisper capacity actually grew |
| Set `YTT_WHISPER_JOBS_PER_HOUR=0` to "stop the errors" | It denies every subject's *new* ASR job (fail-closed) — a deliberate circuit breaker, not a fix; captions keep working, caption-less videos all fail |
| Refund quota for failed jobs by hand or by restart-hopping | Refunds are defined at the gates only (joins/caps/registry faults); failure spend is the design's anti-retry-storm pressure |
| Trust a quiet `YttWhisperDown`-less dashboard as "ASR healthy" | The alert cannot fire at 0.2.21 (§4) — check the logs |

## 12. Acceptance tests

The behaviors this runbook promises — including the three acceptance
scenarios (Whisper **unset**, **unreachable**, **overloaded**) and the
drift-guards that keep this document honest — are pinned by:

```bash
uv run pytest tests/unit/test_asr_runbook.py -q
```

Coverage map:

- **Unset/no-Whisper mode** — declared default is the reference endpoint
  (and matches docs/usage/configuration.md), an empty/unset URL still
  answers `pending` then fails `asr_failed` without ever caching, and boot
  tolerates an absent service (model guard).
- **Unreachable** — connection-refused → `asr_failed` with the relayable
  `Whisper service request failed` message, nothing cached, audio swept,
  re-kick replaces the terminal handle.
- **Overloaded** — 429/503 and read-timeouts → `asr_failed` with the status
  in the message; a real 16-deep backlog denies new jobs with the exact
  "Whisper queue full (16/16 …)" shape while joining and free polling keep
  working; quota exhaustion shape (pinned with the other gates in
  `tests/unit/test_server.py`).
- **GC & restart** — TTL/stale-running thresholds reap exactly the
  documented entries (component level; the loop's unwired status is §9), a
  simulated restart empties the registry and the re-kick starts fresh, and
  the boot sweep clears scratch.
- **Drift guards** — this document's literals (log events, error codes,
  knobs, metric names, paths) and the manifest's Whisper env block must
  keep matching the code.

Adjacent pins this runbook relies on but does not duplicate:
`tests/unit/test_whisper_asr_contract.py` (the OpenAI wire contract),
`tests/unit/test_whisper_contract.py` (lifecycle/polling/restart end to
end), `tests/unit/test_server.py` (gate shapes), `tests/unit/test_whisper.py`
(component internals).  There is no staging cluster — these suites are the
only sanctioned way to rehearse the scenarios above; drilling on production
would mean manufacturing the incident.
