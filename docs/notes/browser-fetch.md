# Browser-primary caption fetch

**Status:** decided 2026-10-07, bead `ytt-204947b5`. Reverses the earlier "no browser,
no PoToken" contract (`docs/notes/yt-dlp-player-client.md`) for the primary path.

## Why

YouTube's caption endpoint (`/api/timedtext`) answers a request that lacks a valid
**PO token** with `200` and an **empty body**, or `429`. The token is minted by
YouTube's own player (BotGuard) for one exact *video and track*. yt-dlp cannot mint
it, so its caption-track download fails on many videos — in the 2026-09-28 retest
**31 of 36** known-bad videos still returned `HTTP Error 429` after a yt-dlp bump
(`ytt-96bb54f6`), from the cluster's residential IP, spaced eight seconds apart across
*distinct* videos. That pattern is not request-volume throttling.

Evidence that the token — not the IP and not the client's TLS fingerprint — is the gate
(diagnostics `ytt-daefe30b`, run in-cluster on the same egress as ytt):

| Observation | Result |
|---|---|
| Stealth Chromium, the player's own caption request | real body, **19/19** videos yt-dlp 429s on; first byte ~2.0 s median (1.8–3.0 s) |
| Same, plain Playwright headless shell | `200` + **0 bytes** (token rejected: automation detected) |
| Captured URL replayed with a non-browser HTTP client, token intact | identical bytes → TLS/client fingerprint is irrelevant |
| Same URL, `pot`/`potc` stripped | `200` + **0 bytes** → the token is the gate |
| Same URL, `lang=` swapped, token kept | `200` + 0 bytes → the token is bound to the track |
| Player asked for another track via its API | fresh request with a fresh token, real body |

## Design

```
ytt (python:3.12-slim + playwright CLIENT wheel)           ytt-browser pod (separate)
  fetch_transcript()                                         playwright launch-server :3001/ytt
   └ browser_fetch_transcript ──────── ws://…:3001/ytt ────►   └ ONE stealth Chromium, options
        │   (fresh context per fetch, closed afterwards)          fixed in the server config;
        ▼                                                         recycled every 6 h
   1. goto watch page            2. read ytInitialPlayerResponse
      (the ONLY navigation)         playability → error taxonomy; title/author/length/date;
                                    caption-track list
   3. choose track with the SAME rules as the yt-dlp path (ytt.fetch._select_track)
   4. force it through the player API → the player issues its own /api/timedtext
      request, PO token attached
   5. capture that response → parse_json3 → FetchResult (same shape as yt-dlp path)
```

- **Separate server, stealth fixed on the server.** A browser crash cannot restart ytt,
  whose job registry and single-flight table are in-process. The server image is the
  stock `mcr.microsoft.com/playwright/python` image; ytt only needs the small client
  wheel (no Dockerfile base change). Client and server Playwright versions must match
  major.minor.

- **`launch-server --config`, not `run-server`.** Since Playwright 1.6x, `run-server`
  silently drops client-supplied `args` / `ignoreDefaultArgs` (the very options that make
  the browser look un-automated) unless started with `--unsafe` — which lets any client
  set `executablePath`, i.e. execute arbitrary commands in the pod. Measured 2026-10-07:
  through `run-server` the browser came up `HeadlessChrome` with `navigator.webdriver ===
  true`, the player's PO token was rejected, and **39/39 in-cluster browser fetches
  returned an empty body** (the first acceptance run). `launch-server --config` fixes the
  options on the server where clients cannot touch them. (Bench's older 1.59 `run-server`
  honoured client options, which is why the approach looked fine there.)
  This contract is pinned, not folklore: `tests/unit/test_browser_launch_mode.py`
  fails if `run-server` or `--unsafe` reaches an executable launch line — in
  the deploy/ mirror and, wherever a declarative-config checkout exists, in
  the applied `k8s/ardenone-cluster/ytt/` manifests themselves — ties the
  three copies of the launch options (the manifest's server config, the
  self-hosting compose example, the client's `BROWSER_LAUNCH_OPTIONS`) so
  they cannot drift apart, and holds the stealth section below to the server
  config in both directions: every option it claims must be in the config,
  and every config launch option must be claimed here.
- **Shared browser, fresh context per fetch.** A single long-lived *context* reused across
  pages grew to the pod's 2.5 GiB limit and was OOM-killed after ~19 pages. With a fresh
  context per fetch, closed afterwards, the shared browser's RSS plateaus (measured
  ~860 MB after 25 full fetch cycles, ~1 MB/cycle drift) — and the server's bash loop
  recycles the browser every 6 h to bound that drift.
- **Stealth is load-bearing.** Launch options (authoritative copy: the server config in
  `deploy/k8s/ardenone-cluster/ytt/browser-deployment.yml`; ytt also sends the same set as
  `BROWSER_LAUNCH_OPTIONS` in the connection URL, which only servers that accept client
  options honour and which is harmless otherwise): full Chromium (`channel=chromium`, new
  headless), `--enable-automation` removed, `--disable-blink-features=AutomationControlled`,
  plus an init script (applied by ytt per context) hiding `navigator.webdriver`. The default headless shell is detected and its token rejected.
  This is an arms race: expect to revisit it (see Risks in the plan).
- **The token never leaves the browser.** ytt does not extract, store or replay it; it
  reads the response of a request the browser made itself. The browser context is
  anonymous (no login, no cookies from us — the plan's "no YouTube cookies" rule holds).
- **Order matters: let the player go first.** Measured on live YouTube (2026-10-07,
  bench): a track forced straight after navigation is requested *without a PO token*
  (`200` + 0 bytes, no `pot` parameter); once the player has made its own first
  caption request — which carries a token — forcing another track also gets one
  (`de-DE` 7,764 B, `ja` 8,009 B on a manual-caption video). So the fetch is two-phase:
  switch captions on and wait for the player's natural request (use it if it is the
  wanted track), and only then force the wanted track. Automatic-caption videos hid
  this in the first diagnostics; every manual-caption video exposed it.
- **Track list quirks.** The player's `tracklist` hides an automatic track that shares a
  language with a manual one, and is empty for videos with only automatic tracks; the
  forcing script therefore falls back to `{languageCode, kind}` — which is what works for
  automatic-only videos.
- **Source/language are what the player actually fetched.** `FetchResult.source` and
  `served_lang` come from the captured request's `kind`/`lang` parameters, not from
  what we asked for.

### Release contract

The current release pair is pinned to Playwright **1.63.0**:

- ytt's client wheel: `playwright==1.63.0` in `pyproject.toml` and `uv.lock`;
- ytt-browser's stock server image: `mcr.microsoft.com/playwright/python:v1.63.0-noble`;
- the server startup install: `pip3 install --quiet playwright==1.63.0`.

The image tag is a full `vX.Y.Z-noble` tag, never `latest`, a floating
major/minor tag, or an untagged image.  The release guard in
`tests/unit/test_browser_release_contract.py` derives the expected server pair
from the client dependency, checks both the `deploy/` mirror and an available
`declarative-config` checkout, and checks this note and the self-hosting example.
When the client wheel or server image is bumped, update all three pins in one
release change and run that guard before the deployment can proceed.  The
operator sequence, including mirror refresh and the browser-primary canary, is
in [`deploy/RUNBOOK.md` §8](../../deploy/RUNBOOK.md#8-the-browser-server-ytt-browser).

## Failure contract

| Situation | Raised as | Router action |
|---|---|---|
| private / unavailable / age / region / members | `YttError` with the taxonomy code | returned as-is (yt-dlp would only repeat it) |
| live or upcoming | `YttError(is_livestream)` | returned as-is, no Whisper |
| no caption tracks | `NoCaptionsError(duration_sec)` | returned as-is → existing Whisper fallback |
| server unreachable / timeout / navigation failed | `BrowserInfraError` | fall back to yt-dlp |
| player never requested the wanted track | `BrowserInfraError(no_request)` | fall back |
| `200` + 0 bytes (token rejected), or `429` | `BrowserInfraError(empty_body\|rate_limited)` | fall back |
| bot wall on this egress (`LOGIN_REQUIRED … not a bot`) | `BrowserInfraError(playability_blocked)` | fall back (and its proxy retry) |
| both paths fail | the yt-dlp error, message naming the browser failure | — |

Metrics: `ytt_browser_fetch_total{outcome=ok|video_error|infra_error|timeout}` and
`ytt_browser_fetch_seconds`. A rising `infra_error` share means the browser path is
drifting — the fallback is hiding it from users, not from the dashboard.

## Operating it

- **Config:** `YTT_BROWSER_WS_URL`, `YTT_FETCH_MODE`, `YTT_BROWSER_TIMEOUT_SEC`,
  `YTT_BROWSER_MAX_CONCURRENCY` — see `docs/usage/configuration.md`. With the URL unset
  ytt is exactly the yt-dlp-only server it was before.
- **Rollback:** `YTT_FETCH_MODE=ytdlp` (restart), no image change.
- **Server:** one `playwright launch-server --config` Deployment, `ClusterIP` only, no
  ingress, endpoint `ws://ytt-browser.ytt.svc.cluster.local:3001/ytt`. It is
  **unauthenticated and can browse from the home IP** — never expose it publicly. Run it
  with a PID 1 that reaps children (e.g. `bash` supervising the server in a loop that also
  recycles it every 6 h): Chromium leaves zombie helper processes otherwise (measured: 12
  after three fetches with the server as PID 1, 0 with bash as PID 1).
- **Sizing:** the shared browser idles near 0.7 GiB and each in-flight page adds memory;
  size the server for `YTT_BROWSER_MAX_CONCURRENCY` pages plus headroom (deployed: 1 GiB
  request, 3 GiB limit).
- **Pod alerts:** `YttBrowserContainerRestarted`, `YttBrowserOOMKilled`, and
  `YttBrowserMemoryNearLimit` live in
  `declarative-config/k8s/ardenone-cluster/ytt/prometheusrule.yml`. They use the
  existing kube-state-metrics and kubelet cAdvisor scrapes because the browser
  WebSocket server has no HTTP metrics endpoint. The six-hour recycle restarts
  the browser *process* inside the container; a container restart is abnormal.
- **First-fetch race:** a client that starts before the server is Ready gets a connect
  failure and falls back to yt-dlp (observed once in the acceptance run: the first fetch
  came 27 s after the server pod started). Retry-before-fallback is the known gap.
- **bench:** `ws://bench:3001/` is a `run-server` (1.59) that happens to honour client
  launch options, so ytt works against it unchanged (verified 2026-10-07) — but it allows
  only two clients and is not reachable from pods without extra plumbing.
- **Not covered:** `YTT_PROXY_URL` applies to the yt-dlp path only; the browser's egress
  is the server pod's.

### Verify the production recycle and memory bound

On `ardenone-cluster`, use read-only pod inspection and the Prometheus VPN
endpoint (`https://prometheus-ardenone-cluster-ts.ardenone.com:8444`):

1. Get `ytt-browser` pod logs with timestamps and confirm consecutive
   `ws://0.0.0.0:3001/ytt` startup lines are about 21,601 seconds apart while
   the container restart count stays at zero. Each line is a new browser-server
   process after the `timeout 21600` loop in `browser-deployment.yml`.
2. Query `container_memory_rss{namespace="ytt",container="browser",pod=~"ytt-browser-[^-]+-[^-]+"}`
   and `container_memory_working_set_bytes` with the same selector through
   `/api/v1/query_range` at a 60-second step across at least 72 hours. Compare
   the sample before and after each six-hour startup, then the peak and end of
   each cycle. RSS should fall on recycle and cycle peaks should stay below the
   pod's 3 GiB limit without an upward staircase. Query
   `kube_pod_container_resource_limits{namespace="ytt",container="browser",resource="memory",unit="byte"}`
   to confirm the deployed limit rather than relying on this note.
3. Query `sum(increase(ytt_browser_fetch_total[72h]))` over the same window.
   If the result is low, repeat the memory comparison after representative
   browser traffic; an idle plateau does not establish the peak under load.

**Production observation, 2026-11-08 (bead `ytt-ca6da0e2`):** the deployed
`ytt-browser-6b44ff69bd-p7kkj` container had been running 19 days with zero
restarts and a 3 GiB memory limit. Its log startup lines were six hours plus
about one second apart. Across 12 recycle boundaries in the last 72 hours,
cAdvisor RSS fell from 186–196 MiB before recycle to 172–180 MiB after;
the full-window RSS range was 172–196 MiB and working set was 292–317 MiB.
The 14-day maxima were 611 MiB RSS and 745 MiB working set, below the
3 GiB limit. This confirms the recycle and bounded baseline on the deployed
pod over the observed window. There were zero browser fetches in the last
72 hours and about nine in 14 days, so the sustained-load bound remains to
be checked when production traffic supplies a representative window.

## What this does not change

Whisper audio download, the cache, auth, rate limits and the error taxonomy are untouched.
The yt-dlp player-client pin and its contract tests still guard the fallback and audio
paths.
