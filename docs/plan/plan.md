# ytt Plan

## Overview

A remote MCP server that reliably downloads transcripts from pasted YouTube links, usable as a custom connector in both Claude mobile (iOS/Android) and Claude desktop. All transcript fetching happens **inside the MCP server** — no third-party transcript APIs. The server handles concurrent requests from multiple clients and caches transcripts in a size-bounded flat-file store for fast repeat access.

**Deployment target: `ardenone-cluster`, which egresses from a residential internet plan.** This is decisive: it natively solves the YouTube datacenter-IP block (the single hardest part of the project) *for free*, because yt-dlp's outbound requests already come from a residential IP. **We assume residential egress** (it is the deployment premise, not a thing to gate on) — and ship a self-test so the operator can confirm the live external IP at any time.

> Prior art already in the cluster: a `yt-transcript-fetcher` pod (`python:3.12-slim`) has run in the `claude-code-research` namespace for ~79 days — evidence the residential-egress + yt-dlp pattern already works here. Worth a look before building.

## Design constraints (from feedback)

- **No third-party APIs.** Transcript extraction happens within the MCP server itself (self-hosted yt-dlp), not via a managed transcript service (Supadata, transcriptapi.com). Explicitly rejected, even as a v1 default.
- **Concurrency.** The server serves parallel requests from multiple clients without blocking. A slow fetch for one client must not stall others.
- **Caching.** Transcripts are cached and served from cache on repeat access (same video).
- **Bounded cache.** The cache never exceeds a **configured maximum storage size**; when full it evicts (LRU) to stay under the limit. Cache is **flat files named by video ID** (a `.txt` per video is sufficient), on a volume that is **either a PVC or an emptyDir**, each with a configured size.
- **Residential egress assumed.** Don't gate the build on verifying it; assume it and expose a self-test of the external IP.
- **Whisper via the cluster's universal Whisper deployment.** When audio must be downloaded to transcribe (no captions), use the existing in-cluster Whisper service — don't bundle our own model.
- **Testing must run in `ardenone-cluster`.** Integration tests hit YouTube, so they cannot run on the EX44 Hetzner box or on Argo/iad-ci (both datacenter IPs → blocked). The in-cluster test harness is part of the deliverable.

## Architecture

The system splits into an easy half (MCP/transport plumbing) and a hard half (reliable transcript fetching). The "no third-party API" constraint means we own the hard half.

```
Claude mobile / desktop / web  ×N clients
   │  (custom connector, Streamable HTTP, OAuth 2.1 + PKCE)
   │  add-connector + consent happen on web/Desktop & on-device;
   │  the MCP client that calls tools is ANTHROPIC'S BACKEND
   ▼
Anthropic backend (MCP client)
   │  public HTTPS — NOT Tailscale-only
   │  allowlist egress: IPv4 160.79.104.0/21  +  IPv6 2607:6bc0::/48
   ▼
Cloudflare Tunnel ──► ytt MCP server  (async, on ardenone-cluster)
                          ├─ OAuth resource-server endpoints (.well-known/*)
                          ├─ async request handler (concurrent clients)
                          ├─ per-video single-flight lock (dedupe in-flight fetches)
                          ├─ self-test: report live external egress IP + ASN
                          ▼
                       Transcript cache  (flat <video_id>.txt files,
                          │  on PVC or emptyDir, size-bounded LRU) ──► hit: return
                          │ miss
                          ▼
                       Fetch core (in-server)
                          1. yt-dlp caption track (json3)
                          2. no captions ──► yt-dlp audio ──► Whisper (async job)
                          │                                      │
                          │                                      ▼
                          │                       whisper-openai.whisper-stt.svc:8000
                          │                       (faster-whisper-server, OpenAI API)
                          ▼
                       ardenone-cluster RESIDENTIAL egress ──► YouTube
                       (assumed residential; optional Webshare proxy only if burned)
```

### Transport decision: remote MCP, not stdio

- Claude mobile cannot run a local process, so stdio servers (`claude_desktop_config.json`) are desktop-only.
- To cover mobile **and** desktop with one server, build a **remote MCP server over Streamable HTTP**, added as a "custom connector."
- The server must be **publicly reachable over HTTPS** — Anthropic's backend is the MCP client that calls it (not the phone). A Tailscale-only endpoint is invisible to it. Expose via **Cloudflare Tunnel**.

### Auth: OAuth-secured under MCP OAuth (hard requirement)

The server is the OAuth 2.1 **Resource Server**. See `docs/research/mcp-oauth-authentication.md` (spec/server side) and `docs/research/claude-app-mcp-oauth-implementation.md` (how the Claude apps actually behave). Non-negotiable surface:

- On unauthenticated requests, return `401` with `WWW-Authenticate: Bearer resource_metadata="…"` — **the #1 reason "Add connector" silently fails** is omitting this header.
- Serve RFC 9728 Protected Resource Metadata at `/.well-known/oauth-protected-resource`.
- The authorization server must serve RFC 8414 metadata advertising `code_challenge_methods_supported: ["S256"]`.
- **Token validation must include audience binding** (RFC 8707), not just signature/expiry — else confused-deputy replay.

How the Claude apps drive it (these *sharpen* the design):

- **The MCP client is Anthropic's backend, full stop** — discovery, DCR, token exchange, and every tool call run from Anthropic's cloud; only the user-consent browser step is on-device. → confirms public ingress is mandatory and the egress allowlist above.
- **Mobile cannot ADD connectors** — it only *uses* servers added on web/Desktop, reusing the token Anthropic already holds. There is **no separate mobile OAuth path/redirect to build**; get the web/Desktop add-flow accepted and mobile works for free. "Verify on mobile" = add on desktop, then confirm the tool works from the phone.
- **Register both callbacks:** `https://claude.ai/api/mcp/auth_callback` **and** `https://claude.com/api/mcp/auth_callback`. "Desktop works, mobile fails" is almost always only `claude.ai` registered, not `claude.com` (migration in flight).
- **DCR registers with `client_name: "claudeai"`** (lowercase, no space) — if the AS allowlists clients by an exact `"Claude"` match, registration silently fails.
- **Advanced settings (manual Client ID/Secret) is web/Desktop only** (mobile has no add UI) — fine for personal v1.

Authorization-Server topology (ADR-001, decide before Phase 9): the resource server is `ytt`; the AS is one of (a) FastMCP self-issued tokens, (b) FastMCP `OAuthProxy` in front of a managed IdP, or (c) a managed IdP directly with `RemoteAuthProvider` validating its tokens. For personal v1 the lowest-effort path is FastMCP-managed auth with audience binding to `ytt`'s resource URL. **FastMCP** (`RemoteAuthProvider`/`OAuthProxy`) auto-generates the metadata plumbing — don't hand-roll. (Rust `rmcp` is client-only → Python is locked.)

### The reliability problem — assumed solved by deployment location

The single hardest part — YouTube blocking datacenter IPs — is **assumed solved by hosting on `ardenone-cluster` (residential egress).**

- YouTube blocks datacenter IPs (AWS/GCP/Azure/Hetzner/Rackspace). **IP reputation is the binding constraint.** A residential egress IP makes yt-dlp look like an ordinary home viewer.
- **PoToken** is largely *not* applied to the caption/timedtext path for the right player clients; on a clean residential IP we don't need a PoToken provider.
- **No proxy in the common case.** This is the "own the IP" pattern, gotten natively from the deployment target. (See `docs/research/residential-egress-options.md`.)
- **Self-test instead of a gate:** ship a diagnostic (tool + HTTP endpoint + startup log) that fetches the live external IP through the same egress path yt-dlp uses and reports IP + ASN/org. The operator confirms residential on demand; we don't block the build on it.
- **Optional fallback only:** if the residential IP is ever rate-limited/burned, set `YTT_PROXY_URL` to a Webshare rotating-residential proxy (~$3.50/mo for 1 GB, free 1 GB tier). The self-test honors this path too.

### Fetch core (in-server, no third-party API)

In-process via the yt-dlp Python API (Unlicense). See `docs/research/yt-dlp-caption-extraction.md`.

1. **Captions** — `extract_info(url, download=False)` with `skip_download=True`, `writesubtitles=True`, `writeautomaticsub=True`, `subtitlesformat='json3'`, and `extractor_args={'youtube': {'player_client': ['tv','web_embedded','mweb']}}` (avoid the `web` client — its subtitle endpoint now needs a PoToken and returns empty bodies). Prefer manual `subtitles`, fall back to `automatic_captions`. Fetch the json3 track URL in-process and parse `events[].tStartMs/dDurationMs/segs[].utf8` into segments.
2. **No captions → Whisper (async)** — see the Whisper section below. Slow → never synchronous inside one tool call.
3. **Block detection** — yt-dlp has no dedicated exception; catch `DownloadError`/`ExtractorError` and string-match `not a bot` / `HTTP Error 429` / `HTTP Error 403` / `Did not get any data blocks`. On a block: surface a clear error, fire the egress self-test (is the IP burned?), and — if `YTT_PROXY_URL` is set — retry via proxy. Note: string-matching is brittle to upstream message changes; pin the yt-dlp version and treat this matcher as a maintenance point.

### Whisper fallback — the cluster's universal Whisper deployment

When a video has no captions, audio must be downloaded and transcribed. We use the **existing universal Whisper service in `ardenone-cluster`** rather than bundling a model:

- **Service:** `whisper-openai` in namespace `whisper-stt` — ClusterIP `http://whisper-openai.whisper-stt.svc.cluster.local:8000`, image `fedirz/faster-whisper-server` → exposes an **OpenAI-compatible** `POST /v1/audio/transcriptions` (multipart: `file`, `model`, `response_format`). (The other service, `whisper-stt:8080` / `ronaldraygun/whisper-stt`, is the PBX-specific one — do **not** use it; it's not the general API.)
- **It is CPU-only** (`latest-cpu`) → transcription is slow. This drives both the async-job design and a max-duration refusal cap.
- **Job contract (state machine):**
  - `get_youtube_transcript` on a caption-less video creates a `WhisperJob{video_id, status:"pending"}` in an in-process registry, kicks off the async pipeline, and returns `pending` immediately (well under the ~60s client timeout).
  - Pipeline: `pending → running` (yt-dlp downloads bestaudio to a temp dir over residential egress) → POST audio to the Whisper endpoint → on success write transcript to cache, set `done`, `result_ref` = cache key; on failure set `error` with a message.
  - Audio lifecycle: download to a scratch dir, **delete immediately after the Whisper call** (success or failure); enforce a size/duration cap before downloading.
  - Client polls `get_transcript_job(video_id)` until `done` (then calls `get_youtube_transcript` again → cache hit) or `error`.
  - Registry is in-memory; on pod restart in-flight jobs are lost and the next request simply re-kicks (idempotent by video_id). Completed results survive because they're in the PVC-backed cache.
- **Refusal cap:** `YTT_MAX_ASR_DURATION_SEC` (default e.g. 3600) — refuse ASR for longer videos with a clear message rather than pinning the CPU Whisper for an hour.

### Concurrency (multiple clients in parallel)

- The server is **fully async** (asyncio). yt-dlp is blocking, so each fetch runs in a thread/process pool executor (`asyncio.to_thread`) — a slow fetch never blocks the event loop or other clients.
- **Bounded fetch pool** — `asyncio.Semaphore(YTT_MAX_CONCURRENT_FETCHES)` caps concurrent yt-dlp fetches (bounds memory and avoids a burst that looks bot-like). Excess requests queue.
- **Single-flight per video** — an asyncio lock/`Future` registry keyed by video ID: the first request fetches, concurrent duplicates await the same result. Prevents N clients triggering N identical fetches (waste + extra block exposure). The registry is **event-loop-native** (asyncio), since fetches are dispatched from the loop.
- **Whisper pool** — a separate, smaller `Semaphore(YTT_MAX_CONCURRENT_WHISPER)`; the CPU Whisper service is the bottleneck, so keep this low (e.g. 1–2).

### Caching — flat files, size-bounded, PVC or emptyDir

Deliberately simple (per feedback): no database.

- **Layout:** one file per video in `YTT_CACHE_DIR`, named by video ID: `<video_id>.<lang>.txt` holding the plain transcript text. Optional sidecar `<video_id>.<lang>.json` holds the timestamped segments when a caller wants them; the `.txt` is the canonical, sufficient default.
- **Cache-first:** `get_youtube_transcript` checks for the file before any network call; a hit returns immediately and `touch`es the file (bump mtime = last access).
- **Atomic writes:** write to `…/<video_id>.<lang>.txt.tmp` then `os.replace` → no torn reads; single-flight already prevents concurrent writers for the same video.
- **Size budget (`YTT_CACHE_MAX_BYTES`):** maintain an in-memory running total (initialized by summing file sizes at startup), updated on every write/evict under a single asyncio lock. **Invariant: total cache bytes ≤ `YTT_CACHE_MAX_BYTES` after each completed write.** On a write that would exceed the cap, evict **LRU** (oldest mtime first) until it fits. A single transcript larger than the whole cap is returned but not retained.
- **Storage backend (configurable, both with sizes):**
  - **PVC** — persistent across pod restarts; size = the PVC `resources.requests.storage`. Default for "remember transcripts."
  - **emptyDir** — ephemeral (lost on restart), with `sizeLimit` set on the volume. Default for "just dedupe within a session / no persistence wanted."
  - `YTT_CACHE_MAX_BYTES` is the app-level cap and must be set **≤ the volume size**; the app cap is what enforces LRU, the volume size is the hard k8s ceiling.
- **TTL (optional):** captions rarely change; allow an optional max-age so stale auto-captions can refresh.

### Response size (MCP has no spec limit, but clients cap)

MCP defines no max result size, but clients do (Claude Code ~25K-token default, 500K-char ceiling; the API connector inlines everything into context). See `docs/research/mcp-response-limits.md`.

- Return inline only when small (≤ `YTT_INLINE_TOKEN_LIMIT`, ~8–10K tokens).
- For long transcripts, **self-paginate**: the tool takes an `offset`/`cursor` arg and returns a chunk + a "call again with this cursor" hint. (MCP pagination does **not** apply to tool results.)
- Offer `mode` (`full` | `chunk` | `summary`); treat ~25K tokens as a self-imposed hard cap; never rely on client truncation.

## Components

- **MCP server** — async, Streamable HTTP. **Python + FastMCP** (locked).
- **Transport/ingress** — Cloudflare Tunnel → cluster service; public HTTPS; Anthropic egress (v4+v6) allowlisted.
- **Auth** — OAuth 2.1 + PKCE resource-server endpoints via FastMCP; both Claude callbacks registered; manual client ID/secret for v1.
- **Fetch core** — in-server yt-dlp caption extraction + Whisper async fallback. No external transcript API.
- **Whisper client** — calls `whisper-openai.whisper-stt.svc:8000` (`/v1/audio/transcriptions`); audio downloaded by yt-dlp and deleted post-call.
- **Residential egress** — native from ardenone-cluster; optional `YTT_PROXY_URL` Webshare fallback.
- **Self-test / diagnostics** — egress-IP + ASN check (tool + HTTP endpoint + startup log); optional yt-dlp dry-run against a known video.
- **Concurrency layer** — asyncio + bounded fetch pool + per-video single-flight + Whisper pool + job registry.
- **Cache** — flat `<video_id>.txt` files on PVC or emptyDir; size-bounded LRU.
- **Test harness** — unit suite (runs anywhere) + integration suite (runs only in ardenone-cluster).

## Data Models

```
TranscriptRequest  { url, lang?, mode? ("full"|"chunk"|"summary"), cursor? }
TranscriptResult   { video_id, text, segments?, source, lang, cached,
                     truncated, next_cursor? }
Segment            { start, duration, text }
WhisperJob         { video_id, status ("pending"|"running"|"done"|"error"),
                     created_at, result_ref?, error? }
EgressReport       { ip, asn, org, via_proxy (bool), looks_residential (bool) }
```

Cache is the filesystem itself — no record type; `<video_id>.<lang>.txt` (+ optional `.json` segments). `source` ∈ { caption_manual, caption_auto, whisper }.

### Tools

- `get_youtube_transcript(url, lang?, mode?, cursor?)` — cache-first; returns the transcript (paginated when long), or a `pending` status pointing at a Whisper job if the video has no captions.
- `get_transcript_job(video_id)` — poll a Whisper job started by a prior caption-less request.
- `selftest_egress(probe_video?)` — report the live external egress IP/ASN (and whether it looks residential); optionally do a yt-dlp dry-run against `probe_video` to confirm YouTube isn't blocking. Also exposed as an unauthenticated `GET /healthz/egress` for ops.

## Configuration

- `YTT_CACHE_DIR` — cache directory (mount point of the PVC or emptyDir).
- `YTT_CACHE_MAX_BYTES` — app-level LRU cap (must be ≤ the volume size).
- `YTT_CACHE_BACKEND` — informational: `pvc` | `emptydir` (the actual volume is set in the manifest; this documents intent/persistence expectations).
- `YTT_MAX_CONCURRENT_FETCHES` — semaphore for yt-dlp caption fetches.
- `YTT_MAX_CONCURRENT_WHISPER` — semaphore for transcription jobs (keep low; CPU Whisper).
- `YTT_WHISPER_URL` — default `http://whisper-openai.whisper-stt.svc.cluster.local:8000`.
- `YTT_WHISPER_MODEL` — model name passed to faster-whisper-server.
- `YTT_MAX_ASR_DURATION_SEC` — refuse ASR above this length.
- `YTT_PROXY_URL` — *optional* Webshare proxy; unset by default (egress already residential).
- `YTT_INLINE_TOKEN_LIMIT` — threshold above which results paginate (~8–10K).
- OAuth: client ID/secret, issuer/resource URLs.

## Acceptance Scenarios

1. **Captioned video, short** — paste a link to a video with manual captions → tool returns the full transcript inline with segments; a second request for the same video returns from cache (no network) and is faster.
2. **Captioned video, long (>inline limit)** — returns the first chunk + `next_cursor`; calling again with the cursor returns the next chunk; chunks reassemble to the full transcript with no gaps/overlap.
3. **No-caption video** — first call returns `pending` with a video_id; `get_transcript_job` reports `running` then `done`; a follow-up `get_youtube_transcript` returns the Whisper transcript from cache. Audio temp file is gone afterward.
4. **Concurrent same-video** — N simultaneous requests for the same uncached video trigger exactly **one** yt-dlp fetch (single-flight); all N receive the same result.
5. **Cache pressure** — fill the cache past `YTT_CACHE_MAX_BYTES`; total on-disk bytes stay ≤ cap and the least-recently-accessed files are the ones evicted.
6. **Egress is residential** — `selftest_egress` / `GET /healthz/egress` reports a non-datacenter ASN; a yt-dlp dry-run succeeds without a proxy.
7. **Block handling** — when YouTube blocks (simulated/stubbed), the tool returns a clear, actionable error (not a stack trace) and the self-test flags the IP.
8. **Connector add** — adding the server as a custom connector on Claude desktop completes the OAuth flow; the same tool then works from Claude mobile without re-adding.

Pass/fail: each scenario has a deterministic assertion (1–5, 7 in the integration suite; 6 via self-test; 8 is a manual deploy checklist item).

## Testing Strategy

Two tiers, split by whether the test touches YouTube:

- **Unit tests — run ANYWHERE (EX44, Argo/iad-ci, local).** No YouTube network. Cover: json3 → segments parsing (fixtures), cache LRU accounting + the `bytes ≤ cap` invariant under concurrent insert/evict, single-flight dedup (stubbed fetch), pagination chunk boundaries, block-detection string matcher, OAuth metadata document shape, 401-emits-`WWW-Authenticate`. These gate every phase and run in CI on Argo (`rust-verify`-style template, or a Python equivalent).
- **Integration / e2e tests — run ONLY in `ardenone-cluster`.** They hit real YouTube and the real Whisper service, so they **cannot** run on the EX44 box or Argo/iad-ci (datacenter IPs → YouTube blocks them). Cover: real caption fetch (scenario 1/2), real no-caption → Whisper round-trip against `whisper-openai` (scenario 3), residential-egress self-test (scenario 6), and a live concurrency check (scenario 4).
  - **Harness:** the app image ships a test entrypoint (`python -m ytt.selftest` / `ytt test`). Per the no-K8s-Jobs convention, run it **inside the cluster** either by `kubectl exec` into the running `ytt` pod or via a small long-running `ytt-test` Deployment that runs the suite on a trigger and exposes results — not a CronJob/Job. Results are logged and surfaced via an endpoint.
  - Use a tiny, stable set of known video IDs (one well-captioned, one caption-less-but-short) as fixtures; keep the caption-less one short to bound Whisper CPU time.

## Implementation Phases

- [ ] Phase 1: Async MCP skeleton — `get_youtube_transcript`/`get_transcript_job`/`selftest_egress` tool stubs over Streamable HTTP; runs locally (stdio) for dev; unit-test scaffold.
- [ ] Phase 2: Fetch core (captions) — yt-dlp json3 extraction, correct `player_client`, `TranscriptResult`, block detection; unit tests on parsing + matcher.
- [ ] Phase 3: Concurrency — fetch pool + per-video single-flight + Whisper pool; unit test for single-flight dedup.
- [ ] Phase 4: Cache — flat `<video_id>.txt` store, LRU eviction to `YTT_CACHE_MAX_BYTES`, atomic writes, startup size scan; unit test for the byte-cap invariant under concurrency.
- [ ] Phase 5: Self-test / egress diagnostics — `selftest_egress` tool + `GET /healthz/egress` + startup log of egress IP/ASN; honors `YTT_PROXY_URL`.
- [ ] Phase 6: Whisper async fallback — integrate `whisper-openai` (`/v1/audio/transcriptions`), the `WhisperJob` state machine, audio download+delete, `YTT_MAX_ASR_DURATION_SEC` cap.
- [ ] Phase 7: Pagination/response sizing — `mode`/`cursor`, inline-vs-chunk threshold.
- [ ] Phase 8: In-cluster test harness — package the integration suite into the image; stand up the `ytt-test` path in ardenone-cluster; wire unit tests into Argo CI. Prove scenarios 1–7 in-cluster.
- [ ] Phase 9: Deploy + OAuth — manifests into `jedarden/declarative-config` (ArgoCD-synced; no direct `kubectl apply`); Cloudflare Tunnel; OAuth 2.1 + PKCE resource-server endpoints (ADR-001 AS choice); register both Claude callbacks; add as a custom connector on desktop and verify use from mobile (scenario 8).

## Deployment notes (ardenone-cluster)

- **All cluster changes go through `jedarden/declarative-config` (k8s/ path) + ArgoCD** — never `kubectl apply` directly; ArgoCD `selfHeal` reverts live edits. The cluster is read-only from the EX44 box via kubectl-proxy.
- **Public exposure via Cloudflare Tunnel**, not Tailscale (Anthropic's backend must reach it). One Tailscale-exposed Service per cluster is reserved for Traefik.
- **Whisper dependency:** `whisper-openai.whisper-stt.svc.cluster.local:8000` (same cluster, ClusterIP — no extra ingress). Confirm NetworkPolicy allows `ytt` → `whisper-stt`.
- **Cache volume:** choose PVC (persistent) or emptyDir (ephemeral) in the manifest; set the volume size and a matching/smaller `YTT_CACHE_MAX_BYTES`. PVC must set `storageClassName` explicitly.
- **No `:latest` tags** for the `ytt` image — pin a digest/version. (Note the upstream `whisper-openai` runs `:latest-cpu`; that's an existing deployment we depend on, not ours to fix here.)
- **Secrets** (OAuth client secret, optional Webshare creds) via the cluster's sealed-secrets/ESO convention; never log tokens or full transcript bodies.

## Open Questions

- Default `YTT_CACHE_MAX_BYTES` and whether v1 ships PVC or emptyDir by default.
- Whisper: confirm `whisper-openai`'s exact request fields/model names against faster-whisper-server, and the practical max-duration before CPU transcription is unacceptably slow.
- ADR-001: which OAuth Authorization-Server topology (FastMCP self-issued vs. managed IdP vs. OAuthProxy).
- Multi-client identity — scope cache/rate-limits per OAuth client, or global?
- Whether the residential IP sustains expected volume before rate-limiting (informs if/when `YTT_PROXY_URL` is needed) — observable via the self-test.

## Resolved (was open)

- ~~Self-host fetcher vs. managed transcript API~~ → **Self-host in-server (yt-dlp), no third-party API.** (User.)
- ~~Implementation language~~ → **Python + FastMCP** (OAuth plumbing; Rust SDK is client-only).
- ~~Where to host / how to get a residential IP~~ → **`ardenone-cluster` (residential egress)** — assumed, with a self-test rather than a gate. (User.)
- ~~Whisper backend~~ → **the cluster's universal `whisper-openai` service** (faster-whisper-server, OpenAI-compatible API). (User.)
- ~~Cache store~~ → **flat `<video_id>.txt` files on PVC or emptyDir, size-bounded LRU** (no database). (User.)
- ~~Where testing runs~~ → **in `ardenone-cluster` for integration** (EX44/Argo are datacenter → blocked); unit tests anywhere. (User.)
- ~~Mobile OAuth path~~ → **none to build** — mobile can't add connectors, only uses ones added on web/Desktop; register both `claude.ai` + `claude.com` callbacks.
