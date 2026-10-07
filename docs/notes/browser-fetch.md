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
- **First-fetch race:** a client that starts before the server is Ready gets a connect
  failure and falls back to yt-dlp (observed once in the acceptance run: the first fetch
  came 27 s after the server pod started). Retry-before-fallback is the known gap.
- **bench:** `ws://bench:3001/` is a `run-server` (1.59) that happens to honour client
  launch options, so ytt works against it unchanged (verified 2026-10-07) — but it allows
  only two clients and is not reachable from pods without extra plumbing.
- **Not covered:** `YTT_PROXY_URL` applies to the yt-dlp path only; the browser's egress
  is the server pod's.

## What this does not change

Whisper audio download, the cache, auth, rate limits and the error taxonomy are untouched.
The yt-dlp player-client pin and its contract tests still guard the fallback and audio
paths.
