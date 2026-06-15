# ytt Plan

## Overview

A remote MCP server that reliably downloads transcripts from pasted YouTube links, usable as a custom connector in both Claude mobile (iOS/Android) and Claude desktop. All transcript fetching happens **inside the MCP server** — no third-party transcript APIs. The server handles concurrent requests from multiple clients and caches transcripts in a size-bounded store for fast repeat access.

**Deployment target: `ardenone-cluster`, which egresses from a residential internet plan.** This is decisive: it natively solves the YouTube datacenter-IP block (the single hardest part of the project) *for free*, because yt-dlp's outbound requests already come from a residential IP. No proxy is needed in the common case.

## Design constraints (from feedback)

- **No third-party APIs.** The transcript extraction must happen within the MCP server itself (self-hosted yt-dlp), not by calling a managed transcript service (Supadata, transcriptapi.com, etc.). Those are explicitly rejected — even as a v1 default.
- **Concurrency.** The server must serve parallel requests from multiple clients without blocking. A slow fetch for one client must not stall others.
- **Caching.** Transcripts are cached and served from cache on repeat access (same video).
- **Bounded cache.** The cache must never exceed a **configured maximum storage size**. When full, it evicts (LRU) to stay under the limit.

## Architecture

The system splits into an easy half (MCP/transport plumbing) and a hard half (reliable transcript fetching). The hard half is where nearly all the engineering risk lives, and the "no third-party API" constraint means we own it.

```
Claude mobile/desktop  ×N clients
   │  (custom connector, Streamable HTTP, OAuth 2.1 + PKCE)
   ▼
Anthropic backend (MCP client)
   │  public HTTPS — NOT Tailscale-only; allowlist 160.79.104.0/21
   ▼
Cloudflare Tunnel ──► ytt MCP server  (async, on ardenone-cluster)
                          ├─ OAuth resource-server endpoints (.well-known/*)
                          ├─ async request handler (concurrent clients)
                          ├─ per-video single-flight lock (dedupe in-flight fetches)
                          ▼
                       Transcript cache (size-bounded, LRU) ──► hit: return
                          │ miss
                          ▼
                       Fetch core (in-server, fallback ladder)
                          1. yt-dlp caption track (json3)
                          2. no captions ──► yt-dlp audio ──► Whisper (async job)
                          │
                          ▼
                       ardenone-cluster RESIDENTIAL egress ──► YouTube
                       (native residential IP — no proxy needed;
                        optional Webshare proxy only if IP gets burned)
```

### Transport decision: remote MCP, not stdio

- Claude mobile cannot run a local process, so stdio servers (`claude_desktop_config.json`) are desktop-only.
- To cover **both** mobile and desktop with one server, build a **remote MCP server over Streamable HTTP**, added as a "custom connector."
- The server must be **publicly reachable over HTTPS** — Anthropic's backend is the MCP client that calls it, not the phone. A Tailscale-only endpoint will not work. Use a **Cloudflare Tunnel** (or public Traefik ingress) in front of a cluster service. Allowlist Anthropic egress `160.79.104.0/21`.
- Auth is **OAuth 2.1 + PKCE** (see Auth below).

### Auth: OAuth-secured under MCP OAuth (hard requirement)

- The server is the OAuth 2.1 **Resource Server**. Non-negotiable surface (see `docs/research/mcp-oauth-authentication.md`):
  - On unauthenticated requests, return `401` with `WWW-Authenticate: Bearer resource_metadata="…"` (the #1 reason connectors fail to add is omitting this header).
  - Serve RFC 9728 Protected Resource Metadata at `/.well-known/oauth-protected-resource`.
  - The authorization server must serve RFC 8414 metadata advertising `code_challenge_methods_supported: ["S256"]`.
  - **Token validation must include audience binding** (RFC 8707), not just signature/expiry — else confused-deputy replay risk.
- Registration: manual Client ID/Secret for personal v1; DCR (RFC 7591) if shared later.
- Register both `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`.
- **FastMCP** (`RemoteAuthProvider`/`OAuthProxy`) or the MCP Python SDK auto-generate this metadata plumbing — don't hand-roll. (The Rust `rmcp` SDK is client-side only, which is a strong argument for Python.)

### The reliability problem (2026 reality) — solved by the deployment location

The single hardest part of this project — YouTube blocking datacenter IPs — is **solved for free by hosting on `ardenone-cluster`, which egresses from a residential internet plan.**

- YouTube **blocks datacenter IPs** (AWS/GCP/Azure/Hetzner/Rackspace) — yt-dlp fails from any cloud host. **IP reputation is the binding constraint.** Because ardenone-cluster's outbound traffic comes from a *residential* IP, yt-dlp's requests look like an ordinary home viewer, and the block does not apply.
- **PoToken** (proof-of-origin bot detection) is largely *not* applied to the caption/timedtext path for the right player clients; PoToken providers exist but "no longer reliably bypass the bot wall," so we don't lean on them — and on a clean residential IP we don't need to.
- **No proxy in the common case.** This is the "own the IP" / residential-worker pattern the egress research recommended — except we get it natively from the deployment target instead of buying it. (See `docs/research/residential-egress-options.md`.)
- **Optional fallback only:** if the single residential IP ever gets rate-limited or burned (e.g. high volume, or YouTube flags the home IP), wire a rotating residential proxy (Webshare, ~$3.50/mo for 1 GB, free 1 GB tier) behind a config flag. Keep this as a documented escape hatch, not a v1 dependency.

> **Caveat to verify:** confirm ardenone-cluster's *outbound* path actually presents the residential IP (no upstream NAT/VPN/Tailscale exit that re-routes egress through a datacenter). A one-off `yt-dlp` test from a pod is the cheap proof. If egress is unexpectedly datacenter, fall back to the Webshare proxy.

### Fetch core (in-server, no third-party API)

Single ladder, all in-process via the yt-dlp Python API (Unlicense — unrestricted embedding). See `docs/research/yt-dlp-caption-extraction.md`.

1. **Captions** — `extract_info(url, download=False)` with `skip_download=True`, `writesubtitles=True`, `writeautomaticsub=True`, `subtitlesformat='json3'`, and `extractor_args={'youtube': {'player_client': ['tv','web_embedded','mweb']}}` (avoid the `web` client — its subtitle endpoint now needs a PoToken and returns empty bodies). Prefer manual `subtitles`, fall back to `automatic_captions`. Fetch the json3 track URL in-process and parse `events[].tStartMs/dDurationMs/segs[].utf8` into segments.
2. **No captions → Whisper** — yt-dlp pulls audio (over the cluster's native residential egress) → Whisper transcription. This is **slow** and cannot run synchronously inside one tool call (≈60s client timeout). Run it as an **async job**: first call kicks off the job and returns a "pending" status with the video ID; client (or a follow-up tool call) polls by ID until ready. Lean on existing `franken_whisper` / whisper-stt infra.
3. **Block detection** — yt-dlp has no dedicated exception; catch `DownloadError`/`ExtractorError` and string-match `not a bot` / `HTTP Error 429` / `HTTP Error 403` / `Did not get any data blocks` to detect IP blocks and surface a clear error / trigger proxy rotation.

### Concurrency (multiple clients in parallel)

- The server is **fully async** (asyncio). yt-dlp is blocking/CPU-ish, so run each fetch in a **thread/process pool executor** (`asyncio.to_thread` / `ProcessPoolExecutor`) — a slow fetch never blocks the event loop or other clients.
- **Bounded worker pool** — a semaphore caps concurrent yt-dlp fetches (protects the proxy/IP from a burst that looks bot-like, and bounds memory). Excess requests queue.
- **Single-flight per video** — an in-process lock keyed by video ID dedupes concurrent requests for the *same* video: the first does the fetch, the rest await its result. Prevents N clients triggering N identical fetches (wasteful + extra block exposure).
- Whisper jobs run in a separate, smaller-concurrency pool (they're heavy); tracked in a job registry keyed by video ID.

### Caching (size-bounded)

- **Key:** video ID (+ language). **Value:** the `TranscriptResult` (segments + metadata).
- **Repeat access** returns from cache without touching YouTube — faster and reduces block exposure.
- **Bounded by configured storage size** (`YTT_CACHE_MAX_BYTES`, e.g. 500 MB default). Track the on-disk/in-store size; on insert that would exceed the cap, **evict LRU** entries until it fits. A single transcript larger than the whole cap is stored transiently / streamed but not retained.
- **Store:** SQLite (transcript text + a `last_accessed` + `size_bytes` column for LRU accounting) is the pragmatic default — single file, survives restarts, easy size queries. (Could use `frankensqlite`.) In-memory LRU is insufficient because we want persistence across restarts and accurate byte accounting.
- **TTL (optional):** captions rarely change, but allow an optional max-age so stale auto-captions can refresh.

### Response size (MCP has no spec limit, but clients cap)

Transcripts can be huge; MCP defines no max result size, but clients do (Claude Code ~25K-token default, 500K-char ceiling; the API connector inlines everything into context). See `docs/research/mcp-response-limits.md`. So:

- Default to returning the transcript inline only when small (≤ ~8–10K tokens).
- For long transcripts, **self-paginate**: the tool takes an `offset`/`cursor` argument and returns a chunk plus a "call again with this cursor" hint. (MCP pagination does **not** apply to tool results — it must be built into our own tool args.)
- Offer a `mode` (e.g. `full` | `chunk` | `summary`) and treat ~25K tokens as a self-imposed hard cap; never rely on client truncation.

## Components

- **MCP server** — async, Streamable HTTP; tools below. **Python + FastMCP** (locked: FastMCP/Python SDK auto-generate OAuth metadata; Rust `rmcp` is client-only).
- **Transport/ingress** — Cloudflare Tunnel → cluster service; public HTTPS; Anthropic egress allowlisted.
- **Auth** — OAuth 2.1 + PKCE resource-server endpoints (FastMCP `RemoteAuthProvider`); manual client ID/secret v1.
- **Fetch core** — in-server yt-dlp caption extraction + Whisper async fallback. No external transcript API.
- **Residential egress** — **native** from `ardenone-cluster` (residential internet plan); no proxy in the common case. Optional Webshare rotating-residential proxy behind a config flag as a burned-IP fallback.
- **Concurrency layer** — asyncio + bounded executor pool + per-video single-flight lock + Whisper job registry.
- **Cache** — SQLite-backed, size-bounded LRU keyed by video ID; configurable max bytes.
- **Whisper backend** — `franken_whisper` / whisper-stt for caption-less videos.

## Data Models

```
TranscriptRequest  { url, lang?, mode? ("full"|"chunk"|"summary"), cursor? }
TranscriptResult   { video_id, text, segments[], source, lang, cached,
                     truncated, next_cursor? }
Segment            { start, duration, text }
WhisperJob         { video_id, status ("pending"|"running"|"done"|"error"),
                     created_at, result_ref? }
CacheEntry         { video_id, lang, payload, size_bytes, last_accessed }
```

`source` ∈ { caption_manual, caption_auto, whisper }.

### Tools

- `get_youtube_transcript(url, lang?, mode?, cursor?)` — cache-first; returns transcript (paginated for long ones), or a `pending` status pointing at a Whisper job if no captions.
- `get_transcript_job(video_id)` — poll a Whisper job started by a prior caption-less request.

## Configuration

- `YTT_CACHE_MAX_BYTES` — hard cap on cache storage (LRU eviction above it).
- `YTT_MAX_CONCURRENT_FETCHES` — semaphore size for yt-dlp fetches.
- `YTT_MAX_CONCURRENT_WHISPER` — semaphore for transcription jobs.
- `YTT_PROXY_URL` — *optional* Webshare rotating-residential proxy endpoint; unset by default (cluster egress is already residential), set only as a burned-IP fallback.
- `YTT_INLINE_TOKEN_LIMIT` — threshold above which results paginate (default ~8–10K).
- OAuth: client ID/secret, issuer/resource URLs.

## Implementation Phases

- [ ] Phase 1: Async MCP server skeleton — `get_youtube_transcript` over Streamable HTTP, runs locally (stdio) for dev; async request handling from the start.
- [ ] Phase 2: In-server fetch core — yt-dlp caption extraction (json3, correct player_client), `TranscriptResult` shape, block-error detection.
- [ ] Phase 3: Concurrency — executor pool + bounded semaphore + per-video single-flight; verify parallel multi-client requests don't stall.
- [ ] Phase 4: Size-bounded cache — SQLite store, LRU eviction to `YTT_CACHE_MAX_BYTES`, cache-first lookup.
- [ ] Phase 5: Verify residential egress — run yt-dlp from an ardenone-cluster pod; confirm YouTube is *not* blocking (proves the residential IP). Only if blocked, wire the optional Webshare proxy.
- [ ] Phase 6: Whisper async fallback — caption-less videos → async job + `get_transcript_job` polling tool.
- [ ] Phase 7: Pagination/response sizing — `mode`/`cursor` handling, inline-vs-chunk threshold.
- [ ] Phase 8: Remote deployment + OAuth — manifests into `jedarden/declarative-config` (ArgoCD-synced; no direct `kubectl apply`); Cloudflare Tunnel, public HTTPS, OAuth 2.1 + PKCE resource-server endpoints; add as custom connector and verify on mobile + desktop.

## Deployment notes (ardenone-cluster)

- **All cluster changes go through `jedarden/declarative-config` (k8s/ path) + ArgoCD** — never `kubectl apply` directly; ArgoCD `selfHeal` reverts live edits.
- ardenone-cluster is **read-only** from the Hetzner box via the kubectl-proxy; manifest changes are made by committing to declarative-config and letting ArgoCD sync.
- One Tailscale-exposed Service per cluster (Traefik) — but this connector needs **public** HTTPS (Anthropic's backend calls it), so expose via **Cloudflare Tunnel**, not Tailscale.
- Cache persistence: back the SQLite store with a PVC so it survives pod restarts.

## Open Questions

- Whisper cost/latency budget — which backend, and a max video length cap before we refuse ASR?
- Cache: PVC storage class on ardenone-cluster, and what default `YTT_CACHE_MAX_BYTES`?
- Does ardenone-cluster's outbound path actually present the residential IP (no upstream datacenter NAT/VPN)? — cheap to verify with a pod-side yt-dlp test (Phase 5).
- Whether the single residential IP can sustain expected request volume before YouTube rate-limits it (informs if/when the optional proxy is needed).
- Auth model — personal (manual client ID/secret) only, or DCR for sharing with others?
- Multi-client identity — do we scope cache/rate-limits per OAuth client, or global?

## Resolved (was open)

- ~~Self-host fetcher vs. managed transcript API~~ → **Self-host in-server (yt-dlp), no third-party API.** (User decision.)
- ~~Implementation language~~ → **Python + FastMCP** (OAuth plumbing + ecosystem; Rust SDK is client-only).
- ~~Where to host / how to get a residential IP~~ → **`ardenone-cluster`, which egresses residential** — natively solves the datacenter-IP block; no proxy needed in the common case. (User decision.)
