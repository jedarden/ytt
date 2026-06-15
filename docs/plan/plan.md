# ytt Plan

## Overview

A remote MCP server that reliably downloads transcripts from pasted YouTube links, usable as a custom connector in both Claude mobile (iOS/Android) and Claude desktop. All transcript fetching happens **inside the MCP server** — no third-party transcript APIs. The server handles concurrent requests from multiple clients and caches transcripts in a size-bounded flat-file store for fast repeat access.

**Deployment target: `ardenone-cluster`, which egresses from a residential internet plan.** This is decisive: it natively solves the YouTube datacenter-IP block (the single hardest part of the project) *for free*, because yt-dlp's outbound requests already come from a residential IP. **We assume residential egress** (it is the deployment premise, not a thing to gate on) — and ship a self-test so the operator can confirm the live external IP at any time.

> Prior art already in the cluster: a `yt-transcript-fetcher` pod (`python:3.12-slim`) has run in the `claude-code-research` namespace for ~79 days — evidence the residential-egress + yt-dlp pattern already works here. Worth a look before building.

## Design constraints

- **No third-party APIs.** Transcript extraction happens within the MCP server itself (self-hosted yt-dlp), not via a managed transcript service (Supadata, transcriptapi.com). Explicitly rejected, even as a v1 default.
- **Authenticated AND authorized.** OAuth proves *who* is calling; an explicit **subject allowlist** decides *whether they may*. Without it, a public OAuth connector is an open YouTube-download proxy running through the home internet (see Security & Authorization). This is a hard constraint.
- **Concurrency.** The server serves parallel requests from multiple clients without blocking. A slow fetch for one client must not stall others.
- **Single replica (v1).** All coordination (job registry, single-flight, cache byte-counter) is in-process memory; the service is correct **only at `replicas: 1`**. Scaling out is a redesign, not a config change.
- **Caching.** Transcripts are cached and served from cache on repeat access. Cache is **flat files named by video ID** on a volume that is **either a PVC or an emptyDir**, each with a configured size; when full it evicts LRU to stay under a configured cap.
- **Residential egress assumed.** Don't gate the build on verifying it; assume it and expose a self-test of the external IP.
- **Whisper via the cluster's universal Whisper deployment.** When audio must be downloaded to transcribe (no captions), use the existing in-cluster `whisper-openai` service — don't bundle our own model.
- **Testing in `ardenone-cluster`.** Integration tests hit YouTube, so they cannot run on the EX44 Hetzner box or on Argo/iad-ci (datacenter IPs → blocked). The in-cluster test harness is part of the deliverable.

## Architecture

```
Claude mobile / desktop / web  ×N clients
   │  (custom connector, Streamable HTTP, OAuth 2.1 + PKCE)
   │  add-connector + consent on web/Desktop & on-device;
   │  the MCP client that calls tools is ANTHROPIC'S BACKEND
   ▼
Cloudflare edge  ──  Cloudflare Access / WAF rule:
   │                  allow only IPv4 160.79.104.0/21 + IPv6 2607:6bc0::/48
   │                  (this is the ONLY place inbound IP-allowlisting can live —
   │                   the origin pod sees only cloudflared, not the client IP)
   ▼
Cloudflare Tunnel (cloudflared sidecar) ──► ytt MCP server (async, replicas:1)
   │                          ├─ /healthz (liveness, unauth)   ── carved out of auth
   │                          ├─ OAuth resource-server endpoints (.well-known/*)
   │                          ├─ AuthN: validate bearer token (audience-bound)
   │                          ├─ AuthZ: subject ∈ YTT_ALLOWED_SUBJECTS else 403
   │                          ├─ rate limit: per-subject token bucket → 429
   │                          ├─ async handler + per-video single-flight
   │                          ▼
   │                       Transcript cache (flat files, PVC|emptyDir, LRU) ─► hit
   │                          │ miss
   │                          ▼
   │                       URL → canonical video_id  (reject playlist/channel)
   │                          ▼
   │                       Fetch core (in-server)
   │                          1. yt-dlp caption track (json3)
   │                          2. no captions ──► async WhisperJob:
   │                             yt-dlp audio (scratch vol) ─► whisper-openai
   │                             ─► cache ─► delete audio
   │                          ▼
   └─ egress NetworkPolicy ─► ardenone-cluster RESIDENTIAL egress ─► YouTube
                              (assumed residential; optional Webshare proxy if burned)
```

### Transport decision: remote MCP, not stdio

- Claude mobile cannot run a local process, so stdio servers (`claude_desktop_config.json`) are desktop-only.
- To cover mobile **and** desktop with one server, build a **remote MCP server over Streamable HTTP**, added as a "custom connector."
- The server must be **publicly reachable over HTTPS** — Anthropic's backend is the MCP client that calls it (not the phone). A Tailscale-only endpoint is invisible to it. Expose via **Cloudflare Tunnel**.

### Security & Authorization

OAuth gives **authentication**, not **authorization**. Because the connector is on the public internet and any Claude user who learns the URL could otherwise add it and drive yt-dlp + CPU-Whisper through the household's home internet, authorization is a first-class control, not an afterthought.

- **Subject allowlist (required).** After token validation, every tool call checks the token's subject/email against `YTT_ALLOWED_SUBJECTS` (comma-separated). Not on the list → `403`. An **empty allowlist denies all** (fail-closed), so a misconfigured deploy is locked, not open.
- **No open Dynamic Client Registration in v1.** DCR lets anyone register; for a personal tool use FastMCP self-issued tokens or a single known client_id/secret. If DCR is ever enabled, gate it behind a pre-shared registration token. (Note: the Claude apps register as `client_name: "claudeai"` — don't allowlist *clients* by an exact `"Claude"` string match; do authorization on the **subject**, not the client name.)
- **Inbound IP allowlist lives at Cloudflare, not the pod.** With Cloudflare Tunnel the origin pod only ever sees `cloudflared`, never the real client IP — so a NetworkPolicy or app-level source-IP check **cannot** enforce the Anthropic-egress allowlist. Implement it as a **Cloudflare Access policy** (preferred — identity/service-token aware) or a **WAF custom rule** allowing only `160.79.104.0/21` + `2607:6bc0::/48` on the public hostname. Treat this as defense-in-depth, **not** a substitute for the subject allowlist (Anthropic's egress ranges are shared infra).
- **Per-subject rate limiting.** Global concurrency semaphores bound parallelism, not volume. Add a per-subject **token bucket** (`YTT_RATE_LIMIT_PER_MIN`) and a per-subject Whisper quota (`YTT_WHISPER_JOBS_PER_HOUR`). Put a **bounded queue** in front of the fetch semaphore that returns `429` + `Retry-After` when full, rather than buffering unboundedly. Cap total in-flight WhisperJobs.
- **Egress NetworkPolicy.** This is an internet-facing pod running yt-dlp (which has had RCE-adjacent CVEs). Restrict pod egress to YouTube/Google CDNs + `whisper-stt` + Cloudflare + the IP-info self-test host; deny the rest to limit blast radius.
- **No YouTube cookies, ever.** A yt-dlp cookie file ties the household Google account to the scraper and risks account action. v1 uses no cookies; this is a standing constraint, not a default to revisit lightly.
- **Secrets.** OAuth client secret, the Cloudflare tunnel token, and any optional Webshare creds are stored as **SealedSecrets** (per the declarative-config convention) — or ESO/OpenBao if available. Enumerate keys in the manifest; document a rotation procedure per secret. **Never log** bearer tokens, the subject list, or full transcript bodies (enforce in a logging filter, not just by convention).
- **Diagnostic endpoint hygiene.** `GET /healthz` (liveness) is unauthenticated and returns only `200`. The egress IP/ASN detail is **not** public — see Self-test below.

### Auth: OAuth-secured under MCP OAuth (hard requirement)

The server is the OAuth 2.1 **Resource Server**. See `docs/research/mcp-oauth-authentication.md` (spec/server side) and `docs/research/claude-app-mcp-oauth-implementation.md` (Claude-app behavior). Non-negotiable surface:

- On unauthenticated requests, return `401` with `WWW-Authenticate: Bearer resource_metadata="…"` — **the #1 reason "Add connector" silently fails** is omitting this header. (`/healthz` is exempt from this middleware.)
- Serve RFC 9728 Protected Resource Metadata at `/.well-known/oauth-protected-resource`.
- The authorization server must serve RFC 8414 metadata advertising `code_challenge_methods_supported: ["S256"]`.
- **Token validation must include audience binding** (RFC 8707), not just signature/expiry — else confused-deputy replay.

How the Claude apps drive it:

- **The MCP client is Anthropic's backend, full stop** — discovery, DCR, token exchange, and every tool call run from Anthropic's cloud; only the user-consent browser step is on-device. → confirms public ingress is mandatory and the Cloudflare-edge IP allowlist.
- **Mobile cannot ADD connectors** — it only *uses* servers added on web/Desktop, reusing the token Anthropic already holds. There is **no separate mobile OAuth path to build**; get the web/Desktop add-flow accepted and mobile works for free. "Verify on mobile" = add on desktop, then confirm the tool works from the phone.
- **Register both callbacks:** `https://claude.ai/api/mcp/auth_callback` **and** `https://claude.com/api/mcp/auth_callback`. "Desktop works, mobile fails" is almost always only `claude.ai` registered, not `claude.com`.
- **Advanced settings (manual Client ID/Secret) is web/Desktop only** (mobile has no add UI) — fine for personal v1.

**ADR-001 (decide before Phase 9): Authorization-Server topology + authz.** The resource server is `ytt`; the AS is one of (a) FastMCP self-issued tokens, (b) FastMCP `OAuthProxy` in front of a managed IdP, or (c) a managed IdP with `RemoteAuthProvider`. For personal v1 the lowest-effort path is FastMCP-managed auth with audience binding to `ytt`'s resource URL **and DCR disabled**, plus the subject allowlist above. **FastMCP** auto-generates the metadata plumbing — don't hand-roll. (Rust `rmcp` is client-only → Python is locked.)

### URL → canonical video_id (load-bearing)

Every cache filename, single-flight key, and job key is derived from a **canonical 11-char video ID** — so this normalization runs *before* any expensive `extract_info` call, and two URL forms of the same video must collapse to one ID (else duplicate fetches + cache misses).

- Accept and normalize: `youtube.com/watch?v=ID`, `youtu.be/ID`, `/shorts/ID`, `/live/ID`, `/embed/ID`, `m.`/`music.youtube.com`, URLs with `&list=`/`&t=`/`&pp=` (strip those params), and a bare `[A-Za-z0-9_-]{11}` ID.
- Extract the 11-char ID; discard timestamp/playlist context.
- **Reject** playlist-only (`/playlist?list=`), channel (`/channel/`, `/@handle`), and search URLs with a distinct `error_code: bad_url` (don't hand them to yt-dlp blindly and hang).
- Unit-test fixtures for every URL form (Phase 2).

### Fetch core (in-server, no third-party API)

In-process via the yt-dlp Python API (Unlicense). See `docs/research/yt-dlp-caption-extraction.md`.

1. **Captions** — `extract_info(url, download=False)` with `skip_download=True`, `writesubtitles=True`, `writeautomaticsub=True`, `subtitlesformat='json3'`, and `extractor_args={'youtube': {'player_client': ['tv','web_embedded','mweb']}}` (avoid the `web` client — its subtitle endpoint now needs a PoToken and returns empty bodies). The same `extract_info` call also yields the **metadata** we surface (title, channel, duration, etc.). Prefer manual `subtitles`, fall back to `automatic_captions`. Fetch the json3 track URL in-process and parse `events[].tStartMs/dDurationMs/segs[].utf8` into segments.
2. **No captions → Whisper (async)** — see the Whisper section. Slow → never synchronous inside one tool call.
3. **Error taxonomy (not just "blocked").** Map yt-dlp `DownloadError`/`ExtractorError` messages to a stable `error_code` so the model can relay something useful. At minimum: `private`, `members_only`, `age_restricted`, `region_blocked`, `unavailable` (deleted/no-such-video), `is_livestream` (no final transcript — must **not** enter the Whisper path), `too_long_for_asr` (include cap + video length), `no_captions_asr_started`, `no_captions_asr_failed`, `ip_blocked`, `rate_limited`, `bad_url`. Each maps to a human message phrased for verbatim relay. String-matching is brittle to upstream changes → pin the yt-dlp version and treat this table as a maintenance point; emit a distinct metric for "empty body / unrecognized" vs. an explicit block so silent breakage is visible. Fixtures (stubbed exceptions) run in the unit suite anywhere.

### Language selection

- `lang` omitted → serve the video's **default/original** caption language; fall back to English, then to "any available," in that order.
- `lang=X` requested → resolution order: manual[X] → auto[X] → manual[default] → auto[default]. **Auto-translation is out of scope for v1** (it compounds ASR-style errors); when the exact language is unavailable, serve the fallback and set `TranscriptResult.lang` to the **served** language plus a note "requested <X> unavailable; served <Y>" with `available_langs` listed.
- `TranscriptResult.lang` is **always the served language**, and the cache filename uses the served lang (ties to the Whisper namespace below).

### Whisper fallback — the cluster's universal Whisper deployment

When a video has no captions, audio must be downloaded and transcribed using the **existing universal Whisper service** rather than a bundled model:

- **Service:** `whisper-openai` in namespace `whisper-stt` — ClusterIP `http://whisper-openai.whisper-stt.svc.cluster.local:8000`, image `fedirz/faster-whisper-server` → **OpenAI-compatible** `POST /v1/audio/transcriptions` (multipart: `file`, `model`, `response_format`). (The other service, `whisper-stt:8080` / `ronaldraygun/whisper-stt`, is PBX-specific — do **not** use it.)
- **CPU-only** (`latest-cpu`) → slow; drives the async-job design, a max-duration cap, and an HTTP timeout. It is a **shared** service (other tenants, e.g. PBX) → keep `YTT_MAX_CONCURRENT_WHISPER` low (default 1) to avoid starving them, and set `YTT_WHISPER_TIMEOUT_SEC` so a hung call doesn't strand a job in `running` forever.
- **Whisper output is language-independent of the request.** Whisper transcribes the *spoken* audio into whatever language is spoken — it does not honor a requested caption-language code. So Whisper results are cached under a **lang-independent** name `<video_id>.whisper.txt`, and cache lookup falls back to that file before deciding to fetch. A Whisper result **satisfies any `lang` request**; `TranscriptResult.lang` reports the detected/spoken language, `source = whisper`. This makes the no-caption cache-hit loop deterministic.
- **Single-flight covers discovery and the job.** The single-flight key wraps the `extract_info` *discovery* call (so concurrent callers share the "has no captions" finding), and `WhisperJob` creation is **get-or-create under a lock keyed by video_id**: a second `get_youtube_transcript` while a job is `pending`/`running` returns the **existing** job's status and never starts a second Whisper run or re-downloads audio.
- **Job state machine:** `pending → running → done | error`. Create on first caption-less request, return `pending` immediately with an **ETA** (server knows video duration × a configured CPU realtime factor) and a human message: *"No captions; transcribing now (~N min for an M-min video). Ask me for this transcript again shortly."* The tool description instructs the model to **relay the ETA and stop — not tight-poll**.
- **Audio lifecycle:** download bestaudio to a **dedicated scratch volume** (`YTT_SCRATCH_DIR`, separate from the cache so audio can't evict transcripts or fill the cache volume), enforce the size/duration cap **before** download, POST to Whisper, then delete the audio (success or failure) via a context-manager. A **startup sweep** deletes orphaned audio left by a crash between download and delete.
- **Registry:** in-memory, with a **TTL/GC sweep** (`YTT_JOB_TTL_SEC`) so completed/errored jobs don't leak memory. On pod restart in-flight jobs are lost; `get_transcript_job` for an unknown id returns `not_found` with an instruction to re-call `get_youtube_transcript` (which re-kicks idempotently). **Completed transcripts survive restart only with the PVC cache backend** — with emptyDir they're lost (a stated tradeoff of choosing emptyDir).
- **Refusal cap:** `YTT_MAX_ASR_DURATION_SEC` (default 3600) — refuse with `too_long_for_asr` (message includes the cap and the video length) rather than pinning CPU Whisper for an hour.
- **Progress notifications:** we do **not** rely on `notifications/progress` to hold a long Whisper call open — clients don't uniformly honor `resetTimeoutOnProgress` and the API connector exposes no resettable per-tool timeout (see research §6). Hence async-job + cache-poll. We may emit best-effort progress as a keepalive on the sub-60s caption path only.

### Concurrency (multiple clients in parallel)

- Fully async (asyncio). yt-dlp is blocking → each fetch runs via `asyncio.to_thread`; a slow fetch never blocks the loop or other clients.
- **Bounded fetch pool** — `asyncio.Semaphore(YTT_MAX_CONCURRENT_FETCHES)`; excess requests hit the bounded queue (→ `429` when full), not an unbounded buffer.
- **Single-flight per video** — event-loop-native `Future` registry keyed by canonical video_id; covers caption discovery and Whisper job (above).
- **Whisper pool** — separate small `Semaphore(YTT_MAX_CONCURRENT_WHISPER)`.
- All three primitives are **process-local → `replicas: 1` only** (see Risks).

### Caching — flat files, size-bounded, PVC or emptyDir

Deliberately simple (per feedback): no database.

- **Layout / atomic unit.** The cache unit is a `(video_id, lang)` pair (or `(video_id, whisper)`): a `<video_id>.<lang>.txt` (plain text, canonical/sufficient) plus an optional sidecar `<video_id>.<lang>.json` (timestamped segments + `source` + metadata). The two files are **created, evicted, accounted, and touched together** — never split.
- **Cache-first.** Check for the file before any network call; a hit returns immediately and `touch`es **both** files (mtime = last access). On a caption miss, also check the `<video_id>.whisper.*` fallback before fetching.
- **Atomic writes.** Write `…tmp` then `os.replace`; single-flight prevents concurrent writers for the same unit.
- **Size budget (`YTT_CACHE_MAX_BYTES`).** In-memory running total (initialized by a startup scan that **excludes/cleans stray `.tmp` files**), updated on every write/evict under one asyncio lock. **Invariant: total cache bytes ≤ `YTT_CACHE_MAX_BYTES` after each completed write.** On a would-exceed write, evict LRU (oldest mtime, whole units) until it fits. A single transcript larger than the whole cap is returned but not retained. Periodically reconcile the counter against actual disk usage (not just at boot).
- **ENOSPC handling.** If a write hits `ENOSPC` (emptyDir `sizeLimit` reached, or disk pressure), evict-and-retry once, then degrade to **serve-but-don't-cache** rather than erroring the request.
- **Storage backend (configurable, both with sizes).** **PVC** — persistent across restarts; size = `resources.requests.storage`; `storageClassName` set explicitly. **emptyDir** — ephemeral, with `sizeLimit` on the volume; completed Whisper results do **not** survive restart. `YTT_CACHE_MAX_BYTES` must be **≤ the volume size**; a startup validation fails fast (or logs loudly) if it exceeds detected free space.
- **TTL (optional).** Captions rarely change; an optional max-age lets stale auto-captions refresh. If a paginated read spans a refresh, the cursor's content-hash guard (below) catches it.

### Response shape & size (MCP has no spec limit, but clients cap)

MCP defines no max result size, but clients do (Claude Code ~25K-token default, 500K-char ceiling; the API connector inlines everything into context). See `docs/research/mcp-response-limits.md`.

- **Modes are `full` | `chunk` only.** `summary` is **dropped** — the server has no LLM, so a "summary" would either lie or be naive truncation; *the calling model is the summarizer.* Instead, the server offers honest token-reducing selectors it *can* implement: `start`/`end` time bounds (slice segments) and `query` (return matching segments + context).
- **Inline vs. paginate.** Return inline when the text is ≤ `YTT_INLINE_CHAR_LIMIT`. Above it, **self-paginate by character offset** (deterministic, language-agnostic; don't split mid-multibyte-char; segment-align when segments are returned). MCP pagination does **not** apply to tool results — this is our own arg.
- **Cursor.** Opaque, and **bound to a content hash/version** of the cached unit. If the underlying file changed between calls (TTL refresh), return `error_code: cursor_stale` rather than silently serving mismatched chunks. Result carries `total_chars`, `offset`, `is_final`.
- **Loud partial signal.** A non-final chunk's text leads with an explicit, unmissable marker, e.g. `⚠️ PARTIAL: chars 0–24000 of 142233 (segment 1 of 6). INCOMPLETE — call get_youtube_transcript again with cursor='…' before summarizing or answering, unless the user only needs the start.` Machine-readable continuation (`segment_index`, `total_segments`, `time_range`, `is_final`, `next_cursor`) also goes in **`structuredContent`**, not just prose.
- **ADR-002: long transcripts may be delivered as an `https://` link.** The server already has a public HTTPS origin; serving the full transcript at `https://<host>/t/<video_id>.<lang>.txt` and returning a `resource_link` + short preview is more reliable for the model than a multi-call chunk loop (MCP `resource://` is *not* auto-followable on the API connector, but a plain web URL is fetchable). **Access-control wrinkle:** that path must carry an unguessable token (not a bare video_id), and is subject to the same authz stance as the tools. Decide in ADR-002 whether v1 ships links, chunking, or both (recommend: chunking for v1, link as a fast-follow).

## Components

- **MCP server** — async, Streamable HTTP. **Python + FastMCP** (locked). `replicas: 1`.
- **Transport/ingress** — Cloudflare Tunnel (cloudflared sidecar) → pod; public HTTPS; Cloudflare Access/WAF enforces the Anthropic IP allowlist at the edge.
- **AuthN/AuthZ** — FastMCP OAuth 2.1 + PKCE resource-server endpoints; audience-bound token validation; DCR disabled; subject allowlist (`403` otherwise); per-subject rate limit + Whisper quota.
- **URL canonicalizer** — URL/bare-ID → 11-char video_id; rejects playlist/channel/search.
- **Fetch core** — yt-dlp caption extraction + metadata + error taxonomy. No external transcript API.
- **Whisper client** — calls `whisper-openai`; audio on scratch vol, deleted post-call; timeout; ETA.
- **Residential egress** — native from ardenone-cluster; egress NetworkPolicy; optional `YTT_PROXY_URL` Webshare fallback.
- **Self-test / diagnostics** — egress-IP + ASN check (internal/authed endpoint + startup log); fixed probe-video list (no caller-supplied URL).
- **Concurrency layer** — asyncio + bounded fetch pool + bounded queue + per-video single-flight + Whisper pool + job registry w/ TTL GC.
- **Cache** — flat `<video_id>.<lang>.txt`(+`.json`) units on PVC|emptyDir; size-bounded LRU; scratch on a *separate* volume.
- **Observability** — Prometheus metrics + alerts + a scheduled yt-dlp canary.
- **Test harness** — unit suite (anywhere) + integration suite (ardenone-cluster only).

## Data Models

```
TranscriptRequest  { url, lang?, mode? ("full"|"chunk"), cursor?,
                     start?, end?, query? }
TranscriptResult   { video_id, status, source, lang, requested_lang?,
                     available_langs?, title, channel, duration_sec, published,
                     transcript_quality, text?, segments?,
                     cached, offset?, total_chars?, is_final?, next_cursor?,
                     transcript_url?, error_code?, message? }
Segment            { start, duration, text }
WhisperJob         { video_id, status ("pending"|"running"|"done"|"error"),
                     created_at, eta_sec?, result_ref?, error_code?, message? }
EgressReport       { ip, asn, org, via_proxy (bool), looks_residential (bool) }
```

- `status` ∈ { ok, partial, pending, running, error }. `source` ∈ { caption_manual, caption_auto, whisper }.
- `transcript_quality` — human note derived from `source` (e.g. `"asr_auto — may contain errors, no speaker labels"`) so the model knows when to hedge.
- `error_code` — the taxonomy enum above; `message` is phrased for verbatim relay to the user.
- Cache is the filesystem: `<video_id>.<lang>.txt`(+`.json`) for captions, `<video_id>.whisper.txt`(+`.json`) for ASR.

### Tools

- `get_youtube_transcript(url, lang?, mode?, cursor?, start?, end?, query?)` — canonicalize → cache-first → return transcript (inline or first chunk + `next_cursor`), or `pending` + ETA if the video has no captions. `start`/`end`/`query` slice without an LLM. The tool **description** tells the model: pass messy/short YouTube URLs directly; on `partial`, continue with `next_cursor` before answering; on `pending`, relay the ETA and stop (don't tight-poll).
- `get_transcript_job(video_id)` — poll a Whisper job. **When `done`, returns the transcript directly** (same shape/pagination as `get_youtube_transcript`), collapsing the old 3-call loop to 2. `not_found` → instruct re-call.

`selftest_egress` is **not** a model-facing tool (it'd burn tool-selection context and tempt spurious calls). Egress diagnostics live at an **authenticated/internal** endpoint (`GET /admin/egress`, reachable via Tailscale/cluster, not the public tunnel) plus a startup log line; the public `/healthz` returns only liveness. The probe uses a **fixed internal video list**, never a caller-supplied URL.

## Configuration

All knobs have concrete defaults/units; sizes accept human-readable forms (`2Gi`) parsed to bytes.

| Env | Default | Notes |
|---|---|---|
| `YTT_ALLOWED_SUBJECTS` | *(empty = deny all)* | comma-separated subjects/emails; required to permit anyone |
| `YTT_RATE_LIMIT_PER_MIN` | `20` | per-subject token bucket |
| `YTT_WHISPER_JOBS_PER_HOUR` | `10` | per-subject ASR quota |
| `YTT_CACHE_DIR` | `/cache` | PVC or emptyDir mount |
| `YTT_CACHE_BACKEND` | `pvc` | `pvc`\|`emptydir` (documents persistence intent; volume set in manifest) |
| `YTT_CACHE_MAX_BYTES` | `2Gi` | app LRU cap; must be ≤ volume size (validated at startup) |
| `YTT_SCRATCH_DIR` | `/scratch` | audio temp; **separate** emptyDir w/ own `sizeLimit` |
| `YTT_MAX_CONCURRENT_FETCHES` | `4` | yt-dlp caption fetches |
| `YTT_MAX_CONCURRENT_WHISPER` | `1` | shared CPU service — keep low |
| `YTT_WHISPER_URL` | `http://whisper-openai.whisper-stt.svc.cluster.local:8000` | |
| `YTT_WHISPER_MODEL` | *(confirm in Phase 6)* | must match what faster-whisper-server serves; wrong value 500s every ASR call |
| `YTT_WHISPER_TIMEOUT_SEC` | `900` | HTTP timeout so a hung call fails the job |
| `YTT_MAX_ASR_DURATION_SEC` | `3600` | refuse longer videos with `too_long_for_asr` |
| `YTT_JOB_TTL_SEC` | `3600` | GC for completed/errored jobs |
| `YTT_INLINE_CHAR_LIMIT` | `24000` | ~6K tokens; above → paginate |
| `YTT_CHUNK_CHARS` | `24000` | char-offset chunk size |
| `YTT_PROXY_URL` | *(unset)* | optional Webshare fallback; egress is already residential |
| OAuth | — | client id/secret, issuer/resource URLs, DCR disabled |

## Observability

The three things the plan says will break must emit a watched signal — the self-test alone is pull-only.

- **Metrics (Prometheus):** `ytt_fetch_blocks_total`, `ytt_fetch_empty_body_total` (silent yt-dlp breakage), `ytt_whisper_errors_total`, `ytt_whisper_job_seconds`, `ytt_cache_bytes`, `ytt_cache_evictions_total`, `ytt_queue_depth`, `ytt_rate_limited_total`, `ytt_egress_is_residential` (gauge from a periodic self-test).
- **Alerts:** block-rate spike → "home IP likely burned"; Whisper 5xx rate → "Whisper down"; sustained evictions → "cache undersized"; `egress_is_residential=0` → "egress changed / proxy needed".
- **Canary:** a long-running probe (Deployment, per the no-K8s-Jobs convention) fetches a known-captioned video every N minutes and alerts on failure/empty-body — catches both IP-burn and yt-dlp breakage *before* users do.
- **Logging:** structured; a filter guarantees tokens / subject list / full transcript bodies are never logged.

## Acceptance Scenarios

1. **Captioned, short** — full transcript inline with segments + metadata (title/channel/duration); 2nd request hits cache (no network), faster.
2. **Captioned, long** — first chunk + loud PARTIAL marker + `next_cursor` + `structuredContent`; continuing reassembles with no gaps/overlap; `is_final` on the last.
3. **No captions** — first call returns `pending` + ETA; `get_transcript_job` reports `running` then `done` **and returns the transcript inline**; cached under `<id>.whisper.txt`; scratch audio gone afterward.
4. **Concurrent same-video** — N simultaneous requests (caption *and* no-caption variants) trigger exactly **one** fetch / **one** Whisper job; all N get the same result.
5. **Cache pressure** — past `YTT_CACHE_MAX_BYTES`, on-disk bytes stay ≤ cap; whole `(video_id,lang)` units evicted LRU; `.txt`+`.json` never split.
6. **Egress residential** — internal `/admin/egress` reports a non-datacenter ASN; canary fetch succeeds without a proxy.
7. **Error taxonomy** — private / age-gated / livestream / too-long videos each return their distinct `error_code` + relayable `message`, not a stack trace.
8. **AuthZ** — a valid token whose subject is **not** in `YTT_ALLOWED_SUBJECTS` gets `403`; empty allowlist denies all.
9. **Rate limit** — a subject exceeding `YTT_RATE_LIMIT_PER_MIN` gets `429` + `Retry-After`; queue-full also `429`.
10. **URL forms** — `youtu.be`, `/shorts/`, `&list=`, bare ID all resolve to one cache entry; playlist/channel URLs return `bad_url`.
11. **Connector add** — adding on Claude desktop completes OAuth; the same tool then works from Claude mobile without re-adding.

Pass/fail: 1–5, 7, 10 in the integration suite; 8–9 in unit + integration; 6 via canary/self-test; 11 a manual deploy checklist item.

## Testing Strategy

Two tiers, split by whether the test touches YouTube:

- **Unit — runs ANYWHERE (EX44, Argo/iad-ci, local).** No YouTube. Covers: URL canonicalization (every form), json3→segments parsing (fixtures), language fallback selection, cache LRU + `bytes ≤ cap` invariant under concurrent insert/evict + whole-unit eviction + `.tmp` exclusion, single-flight dedup for *both* caption and Whisper-job paths (stubbed), pagination/cursor (char offsets, content-hash staleness), the error-taxonomy string-match table (stubbed exceptions), authz (subject allow/deny), rate-limit/queue-full → 429, OAuth metadata shape + 401-emits-`WWW-Authenticate`. Gates every phase; runs in Argo CI.
- **Integration / e2e — runs ONLY in `ardenone-cluster`.** Real YouTube + real Whisper, so it **cannot** run on EX44 or Argo (datacenter → blocked). Covers scenarios 1–3, 6, 7, 10 and a live concurrency check (4).
  - **Harness:** the app image ships a test entrypoint (`ytt test`). Per the no-K8s-Jobs convention, run **inside the cluster** via `kubectl exec` into the running pod or a small long-running `ytt-test` Deployment that runs on a trigger and exposes results — not a CronJob/Job. Results logged + surfaced via an endpoint.
  - Fixtures: a tiny stable set of known video IDs (one well-captioned; one short caption-less to bound Whisper CPU time).

## Implementation Phases

- [ ] Phase 1: Async MCP skeleton — tool stubs over Streamable HTTP; URL canonicalizer; runs locally (stdio) for dev; unit-test scaffold.
- [ ] Phase 2: Fetch core (captions) — yt-dlp json3 extraction + metadata, correct `player_client`, language selection, `TranscriptResult`, error taxonomy; unit tests on canonicalization + parsing + taxonomy.
- [ ] Phase 3: Concurrency — fetch pool + bounded queue + per-video single-flight (caption + job); unit tests for dedup + 429.
- [ ] Phase 4: Cache — flat `(video_id,lang)` units, whole-unit LRU to `YTT_CACHE_MAX_BYTES`, atomic writes, startup scan/`.tmp` clean, ENOSPC degrade; unit test for the invariant under concurrency.
- [ ] Phase 5: AuthN/AuthZ + rate limiting — FastMCP OAuth resource-server endpoints, audience binding, subject allowlist (`403`), per-subject token bucket + Whisper quota (`429`); unit tests. (ADR-001.)
- [ ] Phase 6: Whisper async fallback — integrate `whisper-openai` (**confirm `YTT_WHISPER_MODEL`** against the live service — phase blocks on this), job state machine + get-or-create + ETA + TTL GC, scratch volume + startup sweep, timeout, `too_long_for_asr`, `<id>.whisper.*` cache namespace.
- [ ] Phase 7: Response shape — `chunk` pagination (char offset + content-hash cursor + loud PARTIAL + `structuredContent`), `start`/`end`/`query` slicing; `get_transcript_job` returns transcript when `done`. (ADR-002: link vs chunk.)
- [ ] Phase 8: Self-test + observability — internal `/admin/egress`, startup egress log, Prometheus metrics, alert rules, yt-dlp canary Deployment.
- [ ] Phase 9: In-cluster test harness — package integration suite into the image; stand up `ytt-test` in ardenone-cluster; wire unit tests into Argo CI; prove scenarios 1–10 in-cluster.
- [ ] Phase 10: Deploy + connect — manifests into `jedarden/declarative-config` (ArgoCD; no direct `kubectl apply`); `replicas:1`/`Recreate`; cloudflared sidecar + tunnel secret + DNS + Cloudflare Access IP rule; pod resource/ephemeral-storage limits; egress NetworkPolicy; SealedSecrets; add connector on desktop, verify on mobile (scenario 11).

## Deployment notes (ardenone-cluster)

- **All cluster changes go through `jedarden/declarative-config` (k8s/ path) + ArgoCD** — never `kubectl apply` directly; ArgoCD `selfHeal` reverts live edits. The cluster is read-only from the EX44 box via kubectl-proxy.
- **Single replica.** `replicas: 1`, `strategy: Recreate`, with a manifest comment: in-memory job registry, single-flight, and cache byte-counter are process-local; scaling out silently corrupts state and is a redesign.
- **Cloudflare Tunnel.** `cloudflared` as a **sidecar** in the `ytt` pod (shares lifecycle). Tunnel **token** in a SealedSecret. Pick a public hostname; `cloudflared` ingress rule `hostname → http://localhost:<port>`; DNS CNAME → `<tunnel-id>.cfargotunnel.com` created via `cloudflared route dns` (capture as a one-time op note). Pin the `cloudflared` image digest. Enforce the Anthropic IP allowlist as a **Cloudflare Access** policy (or WAF rule) on that hostname — the pod cannot do it.
- **Resource limits.** Set CPU/memory `requests`/`limits` (sized from observed yt-dlp/audio footprint) and an **`ephemeral-storage` limit** covering scratch + (if used) emptyDir cache. Scratch is its own `emptyDir` with `sizeLimit`. Enforce the audio size/duration cap before download (Content-Length is absent/spoofable — re-check mid-download).
- **Cache volume.** PVC (persistent, `storageClassName` explicit) or emptyDir (ephemeral, `sizeLimit`); `YTT_CACHE_MAX_BYTES` ≤ volume size.
- **Whisper dependency.** `whisper-openai.whisper-stt.svc:8000`, same cluster (ClusterIP, no extra ingress). NetworkPolicy must allow `ytt → whisper-stt`; egress policy otherwise restricts the pod to YouTube/Google CDNs + Cloudflare + the IP-info host.
- **No `:latest` tags** for `ytt` or `cloudflared` — pin digests. (Upstream `whisper-openai` runs `:latest-cpu`; that's an existing dependency, not ours to fix.)
- **Secrets** as SealedSecrets (OAuth client secret, tunnel token, optional Webshare); documented rotation; never logged. **No YouTube cookies.**

## Risks & posture

- **YouTube ToS / takedown.** Bulk transcript/audio extraction via yt-dlp violates YouTube ToS; keeping the service **personal + subject-allowlisted + rate-limited** is the primary mitigation against drawing volume/attention.
- **Household collateral.** The residential egress IP **is the user's home internet.** If YouTube throttles/bans it, the household's connectivity degrades — a non-consenting third party bears the blast radius. Mitigation: the Webshare proxy *protects the home IP*; consider making the proxy the default if any non-trivial volume is expected, not just "if burned." Accept this tradeoff consciously.
- **yt-dlp breakage.** YouTube changes break yt-dlp regularly; the pinned version *will* go stale. The canary + `empty_body` metric surface it early; SOP: bump pin → run integration suite in-cluster → promote.
- **Single replica = no HA.** A pod restart drops in-flight jobs and (with emptyDir) the cache. Accepted for v1; HA is a redesign (shared lock + external job store).

## Open Questions

- Default `YTT_CACHE_MAX_BYTES` / ship PVC or emptyDir by default for v1.
- `YTT_WHISPER_MODEL` exact value + practical max ASR duration on the CPU service (Phase 6 blocks on confirming against the live service).
- ADR-001 final: AS topology + whether DCR stays fully disabled.
- ADR-002 final: ship `https://` transcript links in v1, chunking only, or both.
- Should the Webshare proxy be the **default** (protect the home IP) rather than fallback, given household risk?

## Resolved (was open)

- ~~Self-host fetcher vs. managed API~~ → **Self-host in-server (yt-dlp), no third-party API.** (User.)
- ~~Implementation language~~ → **Python + FastMCP.**
- ~~Where to host / residential IP~~ → **`ardenone-cluster` (residential egress)** — assumed, with a self-test, not a gate. (User.)
- ~~Whisper backend~~ → **cluster `whisper-openai`** (faster-whisper-server, OpenAI API). (User.)
- ~~Cache store~~ → **flat `<video_id>.txt` files on PVC or emptyDir, size-bounded LRU.** (User.)
- ~~Where testing runs~~ → **integration in `ardenone-cluster`; unit anywhere.** (User.)
- ~~Mobile OAuth path~~ → **none to build**; register both `claude.ai` + `claude.com` callbacks.
- ~~Authorization model~~ → **OAuth + a required subject allowlist (fail-closed), DCR disabled, per-subject rate limits.** (User.)
- ~~Multi-replica / coordination~~ → **`replicas: 1` (process-local state); scale-out is a redesign.**
- ~~`mode: summary`~~ → **dropped** (no server-side LLM); replaced by `start`/`end`/`query` slicing.
- ~~Inbound IP allowlist location~~ → **Cloudflare Access/WAF at the edge** (pod can't see client IP behind the tunnel).
