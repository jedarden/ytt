# Browser-sidecar outage recovery and in-flight fetch semantics

Scope: what happens to a caption fetch when the `ytt-browser` sidecar is
unavailable, crashes, or hits its scheduled recycle — while the fetch is
connecting, mid-navigation, driving the player, or waiting for the caption
response — plus the operator procedure for telling a real outage from
recycle noise and recovering from it.

Policy owner: `ytt/browser_outage.py` (`fetch_with_outage_recovery`, wired
into the router at `ytt/fetch.py` `fetch_transcript`). Verification:
`tests/unit/test_browser_outage_recovery.py`. The transport's own failure
taxonomy and the router's fallback table are
`docs/notes/browser-fetch.md` §Failure contract; this note layers the
outage policy on top of that contract and does not change it.

## The sidecar's failure modes

`ytt-browser` is a separate single-replica Deployment
(`deploy/k8s/ardenone-cluster/ytt/browser-deployment.yml`) whose PID 1 is a
bash loop running `timeout 21600` around the Playwright server. Three
things happen to it routinely:

| Mode | Cause | Window |
|---|---|---|
| **Scheduled recycle** | the 6 h `timeout` fires; the server process is killed and the loop restarts it after `sleep 1` | ~1 s, every 6 h, at an arbitrary point in whatever fetches are in flight |
| **Crash / OOM** | Chromium or the server process dies early; the same loop restarts it immediately | seconds; a `Recreate` swap after `OOMKilled` can take longer |
| **Unavailable** | pod not Ready yet, DNS, or `browser-networkpolicy.yml` not admitting `ytt → ytt-browser:3001` | from ~1 s (restart) to persistent (a policy or wiring regression) |

The server holds **no per-fetch state**: every fetch opens a fresh
anonymous context and closes it. That is what makes the retry policy below
safe — there is nothing to restore or resume, so a retry is always a
complete, independent fetch.

## What an in-flight fetch sees

A fetch in flight when the server process dies has its WebSocket transport
drop under it. Playwright raises from **whatever call was pending** — these
escapes are not `BrowserInfraError`, so before this policy existed they
would have left the browser path as raw transport errors and the router
would never have reached its yt-dlp fallback:

| Fetch phase when the sidecar dies | Raw escape | Classified as |
|---|---|---|
| `connect` to the WebSocket endpoint | `ConnectionError` from the transport | `BrowserInfraError(connect)` (already the transport's own reason) |
| creating the context / navigating | `TargetClosedError`-shaped exception from `new_context` / `goto` | `BrowserInfraError(connection_lost)` |
| driving the player (the natural-capture script) | same shape from the page `evaluate` | `BrowserInfraError(connection_lost)` |
| waiting for the caption response | same shape from the caption wait | `BrowserInfraError(connection_lost)` |

Classification is by **shape, not identity** (`looks_like_disconnect`):
`ConnectionError` subclasses, exception class names carrying "closed", and
transport messages like "Connection closed"/"has been closed". Anything
else that escapes the transport is wrapped as `unclassified` — the unknown
must fail loudly once, never be retried and never be mistaken for an
outage.

## The policy: classify → retry → fall back

`fetch_with_outage_recovery` wraps the whole transport fetch:

1. **Classify.** Video-level answers (`YttError`, `NoCaptionsError`) are
   authoritative and re-raised untouched on the first attempt — yt-dlp
   would only repeat them, and the router already routes them without a
   fallback. Every other failure of this path — a transport-raised
   `BrowserInfraError` or a raw escape wrapped per the table above —
   becomes a `BrowserInfraError` with a `reason`, so the router's contract
   holds for every failure including crashes mid-capture.

2. **Retry** outage-shaped reasons only, up to `BROWSER_ATTEMPTS = 3`
   attempts (initial try + two retries), paced by an escalating backoff of
   `BROWSER_RETRY_BACKOFF_SEC` (1 s) × attempt number — 1 s before the
   first retry, 2 s before the second. A retry is a completely fresh
   fetch: new connect, new anonymous context, new page.

   Retried reasons — the same outage seen at different fetch phases:

   | Reason | Is the outage during… |
   |---|---|
   | `connect` | connecting (pod restarting / down / policy) |
   | `navigate` | page navigation |
   | `player_script` | driving the player |
   | `connection_lost` | any mid-fetch phase (crash or recycle under the fetch) |

3. **Fall back.** When the attempts are exhausted (or the reason was never
   outage-shaped) the final `BrowserInfraError` propagates to the router,
   which falls back to yt-dlp naming the browser failure — exactly the
   pre-existing contract. A bridged outage never touches yt-dlp.

### Deliberately not retried

| Reason | Why one attempt is final |
|---|---|
| `timeout` | the whole-fetch budget (`YTT_BROWSER_TIMEOUT_SEC`, default 45 s) is already spent; a retry would only re-burn it against a server that, if it were back, would have answered inside the budget |
| `empty_body` | a verdict about this browser's PO token (flagged as automation), not availability |
| `rate_limited` | the player's own caption request got 429 — a verdict about this egress seconds later does not change |
| `playability_blocked` | bot wall on this egress — see `docs/notes/proxy-egress.md` |
| `no_request`, `no_player_response`, `bad_body`, `empty_parse` | player/video behaviour, not sidecar availability |
| `unclassified` | unknown ⇒ terminal, so bugs fail loudly once instead of three times |
| video-level `YttError` / `NoCaptionsError` | correct answers; retried or fallen back they would only be repeated |

### Budget and reconnect semantics

- **Worst-case added wall time** for an outage-shaped failure is
  `(BROWSER_ATTEMPTS - 1) × YTT_BROWSER_TIMEOUT_SEC` plus the backoffs —
  up to ~92 s beyond what the single-attempt path already allowed. That is
  the price of bridging a recycle instead of degrading a user fetch to the
  yt-dlp subset; `timeout` verdicts never pay it (see the table above).
- **Reconnect is whole-fetch.** There is no session to resume: the server
  keeps no per-fetch state, a recycled server process has lost every
  context, and each retry reconnects from scratch. This is why the policy
  needs no handshake, no session tokens and no resume protocol.
- **Concurrency slot discipline.** The transport acquires the browser
  semaphore (`YTT_BROWSER_MAX_CONCURRENCY`, default 3) per attempt, inside
  `browser_fetch_transcript`. The backoff sleeps **outside** the slot, so
  a fetch that is retrying does not hold a browser concurrency slot while
  it waits — other fetches proceed, and memory stays bounded by
  genuinely-in-flight attempts.
- **Recycle window arithmetic.** The recycle gap is `sleep 1` plus process
  start — the first retry (1 s backoff, then a fresh connect) bridges it.
  A `Recreate` swap or OOM restart (seconds) is bridged by the second.
  Anything longer is a real outage the yt-dlp fallback takes over.

## Observability — attempts, not fetches

Each attempt is one `browser_fetch_transcript` call, so
`ytt_browser_fetch_total` counts **attempts**:

- A bridged outage shows as `infra_error +1, ok +1` with no user-visible
  failure. Retries are observable in Prometheus and in the
  `browser fetch attempt N/3 failed (<reason>: …); retrying in Xs` warning
  lines. This is deliberate: a hide-the-outage policy would hide the drift
  too. (Before this policy, the same recycle showed as one `infra_error`
  **and** a degraded user fetch — the metrics did not get noisier, the
  outcomes got better.)
- When attempts exhaust, the router logs its existing
  `browser caption fetch failed (<reason>: …); falling back to yt-dlp`
  warning — one line per user fetch, after the attempt lines.
- `YttBrowserFetchFailing` (`deploy/k8s/ardenone-cluster/ytt/prometheusrule.yml`)
  is unchanged by this layer and is *not* a recycle detector: it needs
  > 50 % `infra_error`/`timeout` over 30 m. Isolated `infra_error, ok`
  attempt pairs from six-hourly recycles stay far below that; a sustained
  firing means the sidecar or its path is genuinely broken.
- The canary's browser leg (`ytt canary --once --browser`) calls the
  transport fetch directly and measures **single-attempt** availability on
  purpose — it must stay able to see a down sidecar without the serving
  path's retries masking it. The outage policy belongs to the serving path
  only (`ytt/fetch.py` `fetch_transcript`), never to the canary or the
  gate.

## Operator procedure

The full alert-response runbook (diagnosis order, the per-`reason`
response table, rollback) is `deploy/RUNBOOK.md` §"What to look at, in
order, when `YttBrowserFetchFailing` fires" and §"Rollback". This section
covers what this policy changes about reading the signals.

**Recycle noise is expected and needs no action.** Around every 6 h the
metrics show a short `infra_error`/`ok` attempt pair and the logs show one
`attempt 1/3 failed (connection_lost|connect …)` line with no fallback
line. A single pair is the recycle being bridged — the user fetch
succeeded; do not treat it as an incident and do not restart anything.

**Reading a real outage**, in order (all read-only):

1. `kubectl --server=http://traefik-ardenone-cluster:8001 get pods -n ytt`
   — is `ytt-browser` restarting, `OOMKilled`, or missing? `describe pod`
   shows the last state. Memory is sized for
   `YTT_BROWSER_MAX_CONCURRENCY` (3) × ~0.7 GiB.
2. The attempt/fallback log lines in ytt:
   - `attempt N/3 failed (connect …)` repeating to a fallback line with
     the pod Running and a clean server log → the NetworkPolicy /
     Service-wiring class, not load (the RUNBOOK table's `connect` row).
   - attempt lines ending in a fallback line with `empty_body` /
     `playability_blocked` → not an outage; the arms-race rows of the
     RUNBOOK table apply (PO token / egress), and the retry layer is
     correctly *not* retrying them.
   - `connection_lost` attempt lines repeating for minutes → the server
     process is crash-looping; step 1's pod state is the evidence.
3. While degraded, captions are still served by the yt-dlp fallback — for
   the subset of videos it can fetch. `video_error` outcomes are correct
   answers and never count toward the alert.

**Recovery actions** go through the GitOps path only — `selfHeal` reverts
any live kubectl mutation, and `kubectl rollout restart` is explicitly
forbidden in `deploy/RUNBOOK.md` §7. To bounce the sidecar, make the
manifest in `declarative-config` say so (any applied change triggers the
`Recreate` swap) and let ArgoCD sync. If captions must be served without
the browser path while investigating, the rollback is
`YTT_FETCH_MODE: "ytdlp"` in the same GitOps path — mark the service
degraded; that is degradation, not recovery.

**Verify recovery** with one browser canary probe
(`ytt canary --once --browser`) — its single-attempt semantics above are
exactly why it is the right instrument here: it proves the sidecar answers
a fresh fetch without the serving path's retries in between.

## What the tests pin

`tests/unit/test_browser_outage_recovery.py` runs the real router, the
real transport fetch and the real recovery policy against a scripted fake
sidecar, one attempt per scripted entry:

- every outage phase (connect refused, context creation, navigation,
  player script, caption wait) is bridged by exactly one retry;
- a persistent outage exhausts `BROWSER_ATTEMPTS` and reaches yt-dlp
  naming the browser failure; a bridged outage never touches yt-dlp;
- a mid-capture crash is classified `connection_lost` (never leaks raw to
  a caller), an unknown exception is `unclassified` and is not retried;
- `timeout`, `empty_body`, `rate_limited` and video-level answers get
  exactly one attempt;
- the backoff paces retries and sleeps outside the concurrency slot;
- metrics count attempts (one `infra_error` + one `ok` for a bridged
  outage), and each retry logs its warning line.
