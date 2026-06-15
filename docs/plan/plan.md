# ytt Plan

## Overview

A remote MCP server that reliably downloads transcripts from pasted YouTube links, usable as a custom connector in both Claude mobile (iOS/Android) and Claude desktop. All transcript fetching happens **inside the MCP server** — no third-party transcript APIs. The server handles concurrent requests from multiple clients and caches transcripts in a size-bounded flat-file store for fast repeat access.

**Deployment target: `ardenone-cluster`, which egresses from a residential internet plan.** This natively solves the YouTube datacenter-IP block — yt-dlp's outbound requests come from a residential IP. This is a load-bearing assumption; it is tracked as a Proof Obligation (see that section), not silently trusted.

**Public hostname (decided): `ytt.ardenone.com`** — the OAuth resource/issuer URL, `.well-known` metadata, the Cloudflare WAF allowlist, and any transcript link all derive from this exact origin (`https://ytt.ardenone.com`). DNS zone `ardenone.com` is owned on this cluster.

> Prior art already in the cluster: a `yt-transcript-fetcher` pod (`python:3.12-slim`) has run in `claude-code-research` for ~79 days — evidence the residential-egress + yt-dlp pattern already works here. Worth a look before building.

## Glossary

- **canonical video_id** — the 11-char `[A-Za-z0-9_-]{11}` YouTube ID; the cache/single-flight/job key.
- **json3** — YouTube's timedtext caption JSON format (`events[].segs[].utf8`).
- **ASR** — automatic speech recognition (Whisper); the fallback when a video has no captions.
- **rolling captions** — auto-caption tracks that re-emit prior words plus one new word per event; require dedup.
- **single-flight** — dedup primitive: concurrent requests for one video share a single in-flight fetch.
- **PoToken** — YouTube "proof-of-origin" bot-detection token; avoided by player-client choice.
- **the connector client** — Anthropic's backend (not the phone/desktop app), which is the actual MCP HTTP client.

## Design constraints

- **No third-party APIs.** Extraction happens in-server (yt-dlp), not via a managed transcript service. Rejected even as a v1 default.
- **Authenticated AND authorized.** OAuth proves *who*; a required **subject allowlist** decides *whether*. Without it a public OAuth connector is an open YouTube-download proxy on the home internet.
- **Concurrency.** Parallel requests from multiple clients never block each other.
- **Single replica (v1).** All coordination (job registry, single-flight, cache byte-counter) is in-process; correct **only at `replicas: 1`**. Scale-out is a redesign.
- **Caching.** Flat files named by video ID on a PVC or emptyDir, each with a configured size; LRU eviction under a configured cap.
- **Residential egress assumed, but proven.** Don't gate the build on it; assume it, ship a self-test + canary, and track it as a Proof Obligation.
- **Whisper via the cluster's universal `whisper-openai` deployment.** Don't bundle a model.
- **Testing in `ardenone-cluster`.** Integration tests hit YouTube → can't run on the EX44 box or Argo/iad-ci (datacenter IPs → blocked). The in-cluster harness is part of the deliverable.

## Architecture

```
Claude mobile / desktop / web  ×N clients
   │  (custom connector, Streamable HTTP, OAuth 2.1 + PKCE)
   │  add-connector + consent on web/Desktop; the MCP client that
   │  calls tools is ANTHROPIC'S BACKEND (unattended, from its cloud)
   ▼
Cloudflare edge  ──  WAF custom rule (NOT Access — Access would bounce the
   │                  unattended backend): allow only IPv4 160.79.104.0/21
   │                  (+ IPv6 2607:6bc0::/48, treated as unverified-but-safe)
   ▼
Shared cluster Cloudflare Tunnel ─► Traefik ─► IngressRoute(ytt.ardenone.com)
   │   (reuse the existing one-tunnel-per-cluster + Traefik convention;
   │    NO per-pod cloudflared sidecar)
   ▼
ytt MCP server (async, replicas:1, uvicorn 1 worker)
   ├─ /healthz (liveness, unauth, carved out of auth middleware)
   ├─ OAuth resource-server endpoints (.well-known/*) — audience = https://ytt.ardenone.com
   ├─ AuthN: validate bearer (audience-bound) → AuthZ: subject ∈ allowlist else 403
   ├─ per-subject rate limit (token bucket) → 429
   ├─ async handler + per-video single-flight
   ▼
   Transcript cache (flat files, PVC|emptyDir, LRU) ─► hit
   │ miss
   ▼  URL → canonical video_id (reject playlist/channel/search)
   Fetch core (in-server, needs ffmpeg in image)
     1. yt-dlp caption track (json3) → dedup rolling auto-captions
     2. no captions ─► async WhisperJob:
        yt-dlp bestaudio (scratch vol, needs ffmpeg) ─► whisper-openai ─► cache ─► delete audio
        │
        ▼  http://whisper-openai.whisper-stt.svc.cluster.local:8000/v1/audio/transcriptions
   egress NetworkPolicy ─► ardenone-cluster RESIDENTIAL egress ─► YouTube
                           (optional Webshare proxy if burned — itself datacenter IP)
```

### Transport decision: remote MCP, not stdio

- Mobile can't run a local process → stdio is desktop-only. One remote **Streamable HTTP** server covers mobile + desktop as a custom connector.
- Must be **publicly reachable over HTTPS** — the connector client is Anthropic's backend, not the phone. Tailscale-only is invisible to it. Exposed via the **shared cluster Cloudflare Tunnel + Traefik IngressRoute** (not a sidecar — see Deployment).

### Security & Authorization

OAuth is **authentication**, not **authorization**. On the public internet any Claude user who learns the URL could otherwise drive yt-dlp + CPU-Whisper through the household's home internet. Authorization is therefore first-class.

- **Subject allowlist (required).** After token validation every tool call checks the token subject/email against `YTT_ALLOWED_SUBJECTS`; not listed → `403`. **Empty allowlist = deny all** (fail-closed).
- **No open DCR in v1.** FastMCP self-issued tokens / a single known client; if DCR is ever enabled, gate behind a pre-shared token. Authorize on **subject**, never the client name (the apps register as `client_name: "claudeai"`).
- **Inbound IP allowlist at Cloudflare WAF, not the pod and not Access.** Behind the tunnel the origin sees only the tunnel, so a NetworkPolicy/app source-IP check can't enforce it. **Cloudflare Access is wrong here** — it's an identity gate that would challenge/bounce Anthropic's unattended backend. Use a **WAF custom rule** allowing only `160.79.104.0/21` (+ the IPv6 range) on `ytt.ardenone.com`. Defense-in-depth, **not** a substitute for the subject allowlist (the ranges are shared infra).
- **Per-subject rate limiting.** Token bucket (`YTT_RATE_LIMIT_PER_MIN`) + Whisper quota (`YTT_WHISPER_JOBS_PER_HOUR`); a **bounded queue** in front of the fetch semaphore returns `429`+`Retry-After` when full. Cap total in-flight WhisperJobs.
- **Egress NetworkPolicy.** Restrict pod egress to YouTube/`googlevideo`, `whisper-openai.whisper-stt.svc`, Cloudflare, and the IP-info host (`ipinfo.io`); deny the rest. (Over-tight egress mimics IP-burn in metrics — start permissive + DNS-log, then tighten.)
- **No YouTube cookies, ever.** A cookie file ties the household Google account to the scraper. Standing constraint.
- **Secrets via ESO/OpenBao** (the cluster norm), not SealedSecrets. Paths under `ardenone-cluster/ytt/*` (e.g. `…/oauth-client-secret`, `…/webshare-url`). Enumerate keys in `ExternalSecret` manifests; document rotation; **never log** tokens, the subject list, or transcript bodies (enforced by a logging filter).
- **Diagnostic hygiene.** Public `/healthz` returns only liveness. Egress IP/ASN detail is at an authenticated/cluster-internal `GET /admin/egress`, never the public tunnel; its probe uses a **fixed internal video list**, never caller input.

### Auth: OAuth-secured under MCP OAuth (ADR-001 decided)

Server is the OAuth 2.1 **Resource Server**. See `docs/research/mcp-oauth-authentication.md` + `docs/research/claude-app-mcp-oauth-implementation.md`. Non-negotiable surface:

- Unauthenticated → `401` + `WWW-Authenticate: Bearer resource_metadata="…"` (the #1 silent "Add connector" failure). `/healthz` exempt.
- Serve RFC 9728 Protected Resource Metadata at `/.well-known/oauth-protected-resource` (resource = `https://ytt.ardenone.com`).
- AS metadata (RFC 8414) advertises `code_challenge_methods_supported: ["S256"]`.
- **Audience-bound** token validation (RFC 8707) — `aud` must match `https://ytt.ardenone.com`, else confused-deputy replay.

Claude-app behavior: the connector client is Anthropic's backend (public ingress mandatory); **mobile can't ADD connectors** (no separate mobile path — get web/Desktop accepted and mobile reuses the token); **register both** `https://claude.ai/api/mcp/auth_callback` + `https://claude.com/api/mcp/auth_callback`; Advanced-settings manual client id/secret is web/Desktop only.

**ADR-001 (decided): FastMCP self-issued tokens with audience binding to `https://ytt.ardenone.com`, DCR disabled**, plus the subject allowlist. FastMCP `JWTVerifier(audience=…)` / `RemoteAuthProvider` (pin FastMCP ≥ 2.11.1, exact in lockfile; verify the self-issued token emits a spec-compliant `aud`). Don't hand-roll metadata. (Rust `rmcp` is client-only → Python locked.)

### URL → canonical video_id (load-bearing)

Runs **before** any `extract_info`; two URL forms of one video must collapse to one ID (else duplicate fetches + cache misses).

- Normalize: `watch?v=`, `youtu.be/`, `/shorts/`, `/live/`, `/embed/`, `m.`/`music.youtube.com`, `&list=`/`&t=`/`&pp=` (stripped), bare 11-char ID.
- **Reject** playlist-only, channel (`/channel/`, `/@handle`), search → `error_code: bad_url`.
- Invariant: `canon(canon(x)) == canon(x)` (see Invariants). Unit fixtures per form (Phase 2).

### Fetch core (in-server, no third-party API)

In-process via the yt-dlp Python API (Unlicense). The `extract_info(url, download=False)` call also yields the **metadata** we surface (title, channel, duration, upload date). See `docs/research/yt-dlp-caption-extraction.md`.

1. **Captions** — `skip_download=True`, `writesubtitles=True`, `writeautomaticsub=True`, `subtitlesformat='json3'`, `extractor_args={'youtube': {'player_client': ['tv','web_embedded','mweb']}}` (avoid `web` — its subtitle endpoint needs a PoToken, returns empty bodies). Prefer manual `subtitles`, fall back to `automatic_captions`. Fetch the json3 track URL in-process.
2. **json3 parse — MUST dedup rolling auto-captions.** Auto-caption (`kind == "asr"`) tracks are a *rolling* stream: events re-emit prior words + one new word, with overlapping `[tStartMs, tStartMs+dDurationMs]` windows and append markers. Naive `"".join(events[].segs[].utf8)` over all events **doubles the text and poisons the cache** (confirmed yt-dlp gotcha #6274/#1734; cf. `srt_fix`). Algorithm: walk events in time order; drop pure-formatting events (no `utf8`); for consecutive overlapping events, **discard an earlier event whose text is a strict prefix of the next**, keep only finalized non-overlapping cues; concat the survivors. **Manual tracks (`kind != "asr"`) are clean** → straight concat. Mandatory fixtures: one real rolling auto track (assert no doubling) + one manual track. This is the single most likely "looks done, is broken" bug — gate Phase 2 on it.
3. **Error taxonomy** (stable `error_code` + verbatim-relayable `message`). Seed string→code map (a maintenance point; pin yt-dlp): `"Private video"→private`, `"members-only"→members_only`, `"Sign in to confirm your age"→age_restricted`, `"not available in your country"→region_blocked`, `"This live event will begin"/"is live"→is_livestream` (never enters Whisper), `"Video unavailable"/"has been removed"→unavailable`, `"HTTP Error 429"→rate_limited`, `"Sign in to confirm you're not a bot"/"HTTP Error 403"/"Did not get any data blocks"→ip_blocked`, empty/unrecognized→`empty_body` (distinct metric so silent breakage is visible). Also: `bad_url`, `too_long_for_asr`, `no_captions_asr_started`, `no_captions_asr_failed`. On `ip_blocked`: surface clearly, fire the egress self-test, retry via `YTT_PROXY_URL` if set.

### Language selection

- `lang` omitted → default/original caption language → English → any available.
- `lang=X` → manual[X] → auto[X] → manual[default] → auto[default]. **Auto-translation out for v1.** When the exact language is unavailable, serve the fallback and set `lang` = **served** language, `requested_lang` = X, `available_langs` listed, `message` = "requested X unavailable; served Y".
- Cache filename uses the **served** lang.

### Whisper fallback — the cluster's universal `whisper-openai`

- **Service:** `whisper-openai` in ns `whisper-stt` — `http://whisper-openai.whisper-stt.svc.cluster.local:8000`, image `fedirz/faster-whisper-server` (**upstream is now renamed `speaches`** — use speaches.ai for docs/model names; the cluster runs the frozen old image). OpenAI-compatible `POST /v1/audio/transcriptions` (multipart `file`,`model`,`response_format`) + `GET /v1/models`. **Do not** use the PBX `whisper-stt:8080` service.
- **Model:** `model` is a **HuggingFace repo ID, required, and must already be pulled on the shared service** (an un-pulled model 404/500s the first call). `YTT_WHISPER_MODEL` default `Systran/faster-whisper-small` (multilingual, fast on CPU). **Self-correcting:** on startup query `GET /v1/models`; if the configured model isn't listed, fall back to the first served model and log loudly (don't 500 forever). Phase 6 confirms name + residency via an in-pod `curl`.
- **CPU-only + shared.** Keep `YTT_MAX_CONCURRENT_WHISPER=1` (don't starve PBX). 
- **ETA / timeout / duration are reconciled** (they were inconsistent): `ETA_sec = duration_sec × YTT_WHISPER_REALTIME_FACTOR` (default 1.2 — CPU transcription is ~realtime-or-slower; confirm against the live model in Phase 6). **Invariant:** `YTT_MAX_ASR_DURATION_SEC × RT_FACTOR < YTT_WHISPER_TIMEOUT_SEC`. Defaults satisfy it: `MAX_ASR_DURATION=1200` (20 min) × `1.2` = 1440s < `TIMEOUT=1800`s. Longer videos → refuse with `too_long_for_asr` (message includes cap + length).
- **Single-flight covers discovery + job.** The single-flight key wraps the lang-agnostic `extract_info` discovery; `WhisperJob` creation is **get-or-create under a lock keyed by video_id** — a second request during `pending`/`running` returns the existing job, never starts a second run or re-downloads audio.
- **Job state machine:** `pending → running → done | error`. First caption-less call returns `pending` + ETA + message ("No captions; transcribing now (~N min); ask me again shortly"); the tool **description tells the model to relay the ETA and stop — not tight-poll**.
- **Audio lifecycle:** download bestaudio to a **dedicated scratch volume** (`YTT_SCRATCH_DIR`, separate from cache), cap checked **before** download, POST, then delete via context-manager (success or failure). **Startup sweep** clears audio orphaned by a crash. Needs **ffmpeg** in the image.
- **Registry:** in-memory + TTL GC (`YTT_JOB_TTL_SEC`). On restart in-flight jobs are lost; `get_transcript_job(unknown)` → `not_found` with "re-call get_youtube_transcript" (idempotent re-kick). Completed results survive restart **only with PVC** (emptyDir loses them — stated tradeoff). Failed Futures are removed atomically; **errors are never cached as transcripts**.
- **Progress notifications:** not relied on (clients don't uniformly honor `resetTimeoutOnProgress`); async-job + cache-poll instead. Best-effort keepalive only on the sub-60s caption path.

### Concurrency (multiple clients in parallel)

- Fully async; blocking yt-dlp runs via `asyncio.to_thread`.
- **Bounded fetch pool** `Semaphore(YTT_MAX_CONCURRENT_FETCHES)`; overflow → bounded queue → `429` (not unbounded buffering).
- **Single-flight** event-loop-native `Future` registry keyed by canonical video_id. The discovery call is keyed on `video_id` alone (lang-agnostic); **after discovery, caption work branches per-`(video_id, lang)`** so `lang=es` and `lang=fr` don't dedupe to one served language. Failed Futures removed atomically.
- **Whisper pool** separate small `Semaphore(YTT_MAX_CONCURRENT_WHISPER)`.
- All process-local → `replicas: 1` only.

### Caching — flat files, size-bounded, PVC or emptyDir

No database (deliberately simple).

- **Atomic unit** = a `(video_id, lang)` or `(video_id, whisper)` pair: `<id>.<lang>.txt` (plain text, canonical) + optional sidecar `<id>.<lang>.json` (segments + `source` + metadata). Created/evicted/accounted/**touched together** — never split.
- **Cache-first.** Check before any network call; hit returns immediately and `touch`es both files. A caption miss also checks the `<id>.whisper.*` fallback before fetching.
- **Atomic writes** (`…tmp` → `os.replace`); single-flight prevents concurrent same-unit writers.
- **Size budget (`YTT_CACHE_MAX_BYTES`).** In-memory total (startup scan that **excludes/cleans stray `.tmp`**), incremented **only after `os.replace` succeeds**. **Touch (mtime bump), eviction-selection, and delete all happen under the same asyncio lock** so a live unit can't be evicted while being touched. On a would-exceed write, evict whole units LRU (oldest mtime) until it fits.
- **Reconcile.** Every `YTT_CACHE_RECONCILE_SEC` (default 300): re-`stat` all units, recompute the total, overwrite the in-memory counter, log if drift exceeds a threshold.
- **ENOSPC.** Evict-and-retry once, then degrade to **serve-but-don't-cache** (don't error the request).
- **Backends.** PVC (persistent; `storageClassName: longhorn` — confirm the cluster's class; size = `requests.storage`) or emptyDir (ephemeral; `sizeLimit`; loses completed Whisper results). Startup validation reads volume size via `os.statvfs(YTT_CACHE_DIR)` (`f_blocks`×`f_frsize`) and fails fast if `YTT_CACHE_MAX_BYTES` exceeds it (note: for emptyDir, statvfs reports node disk, so the kubelet-enforced `sizeLimit` is the real ceiling — keep `YTT_CACHE_MAX_BYTES` ≤ `sizeLimit`). **Default: PVC, `2Gi`.**
- **TTL (optional).** Captions rarely change; livestream/`processing` discovery results get a **short/zero TTL** so a finished stream isn't pinned as `is_livestream`.

### Response shape & size (MCP has no spec limit; clients cap)

Claude Code ~25K-token default, 500K-char ceiling; the API connector inlines everything. See `docs/research/mcp-response-limits.md`.

- **Modes `full` | `chunk`** (default `full`). `summary` **dropped** (no server-side LLM — it'd lie or truncate; the model summarizes). Honest token-reducers instead: `start`/`end` (segment time bounds) and `query` (**case-insensitive substring** match, returns matching segments **±2 segments of context**, mutually exclusive with `start`/`end`).
- **Inline vs paginate.** Inline when text ≤ `YTT_INLINE_CHAR_LIMIT`. `mode:full` on an over-limit transcript still paginates (it never violates the client cap); the difference vs `chunk` is only that `full` defaults to returning chunk 1 with the continuation hint.
- **Char-offset chunking** (`YTT_CHUNK_CHARS`), don't split mid-multibyte, segment-align when segments are returned. MCP pagination does **not** apply to tool results — this is our own arg.
- **Token budget is conservative for non-Latin.** `YTT_INLINE_CHAR_LIMIT`/`YTT_CHUNK_CHARS` default 18000 chars ≈ 6K tokens at **3.0 chars/token** (Latin). Dense CJK/Thai/Arabic run ~1–2 chars/token, so for non-Latin scripts estimate tokens as `bytes/3` and cap on that — a fixed char count alone can approach the 25K ceiling in one chunk.
- **Filtered pagination.** When `query`/`start`/`end` are present, they produce a **filtered virtual document**; pagination (`offset`/`total_chars`/cursor) operates over that filtered document, not the full transcript, and the filter args are part of the cursor (below) so a different filter can't reuse a stale cursor.
- **Cursor.** Opaque = `hash(content + served_lang + source + filter_args) + total_chars + offset`, bound to the (possibly filtered) addressable content. If the unit changed/refreshed → `error_code: cursor_stale` (force fresh page-1). **If the unit was evicted between pages → also `cursor_stale`** (never silently re-fetch and serve at the old offset, which could swap content). Result carries `offset`, `total_chars`, `is_final`.
- **Loud partial.** Non-final chunk text leads with `⚠️ PARTIAL: chars A–B of T (chunk i/n). INCOMPLETE — call get_youtube_transcript again with cursor='…' before summarizing, unless the user only needs the start.` Machine-readable continuation also in `structuredContent`.
- **ADR-002 (decided): chunking for v1.** No `https://` transcript link in v1 (it would need the public hostname + an unguessable token path + its own authz). `transcript_url` stays unset until a later phase ships it.

## Invariants (named; 1–6 CI-enforced via property/concurrency tests, 7 via a startup assertion)

1. **Cache bound:** total cache bytes ≤ `YTT_CACHE_MAX_BYTES` after every completed write.
2. **Single fetch / single job:** for one `video_id`, concurrent requests trigger exactly one discovery fetch and at most one in-flight WhisperJob.
3. **Canonicalization idempotence:** `canon(canon(x)) == canon(x)`; all known URL forms of a video map to one id.
4. **Audio always deleted:** no audio file survives a completed/failed/ crashed job (context-manager + startup sweep).
5. **Whisper satisfies any lang:** a `whisper` cache unit answers any `lang` request; `source=whisper`, `lang`=detected.
6. **Errors never cached as transcripts;** failed single-flight Futures are removed so retries aren't wedged.
7. **ETA timeout safety:** `MAX_ASR_DURATION_SEC × RT_FACTOR < WHISPER_TIMEOUT_SEC` (validated at startup).

## Components

- **MCP server** — async Streamable HTTP, **Python + FastMCP**, `replicas:1`, **uvicorn 1 worker**.
- **Ingress** — shared cluster Cloudflare Tunnel → Traefik **IngressRoute** (`ytt.ardenone.com`); WAF IP allowlist at the edge.
- **AuthN/AuthZ** — FastMCP OAuth (audience-bound), DCR off, subject allowlist (`403`), per-subject rate limit + Whisper quota (`429`).
- **URL canonicalizer**, **fetch core** (+ json3 dedup + metadata + taxonomy), **Whisper client** (httpx, scratch vol, ffmpeg), **cache** (flat units, LRU), **concurrency layer**, **observability**, **self-test**, **test harness**.
- **Libraries (chosen):** `uvicorn`, `httpx`, `pydantic-settings`, `prometheus-client`, `structlog`, hand-rolled in-proc token bucket. IP-info host: `ipinfo.io`.

## Data Models

```
TranscriptRequest  { url, lang?, mode?("full"|"chunk"), cursor?, start?, end?, query? }
TranscriptResult   { video_id, status, source?, lang?, requested_lang?, available_langs?,
                     title?, channel?, duration_sec?, published?, transcript_quality?,
                     text?, segments?, offset?, total_chars?, is_final?, next_cursor?,
                     eta_sec?, transcript_url?(reserved/unset v1), error_code?, message? }
Segment            { start, duration, text }
WhisperJob         { video_id, status("pending"|"running"|"done"|"error"),
                     created_at, eta_sec?, result_ref?, error_code?, message? }
EgressReport       { ip, asn, org, via_proxy, is_residential }
```

- **`status` (unified)** ∈ `{ ok, partial, pending, running, error }`. A WhisperJob `done` surfaces through `get_transcript_job` as TranscriptResult `status: ok` (explicit mapping; the job's internal `done` is never a TranscriptResult status). `EgressReport.is_residential` matches the `ytt_egress_is_residential` metric (one term); it is **derived** (ipinfo.io returns `org`/ASN, not a residential flag) by testing the ASN/org against a known-datacenter-ASN set — that derivation is the concrete test behind the residential Proof Obligation.
- **Per-status field matrix** (what is set):
  - `ok` — text|segments, source, **transcript_quality**, lang, metadata, is_final=true; on a language-fallback hit also `requested_lang` + `available_langs` + `message`.
  - `partial` — text, offset, total_chars, next_cursor, is_final=false.
  - `pending`/`running` — eta_sec, message (no text).
  - `error` — error_code, message (no text).
- **`transcript_quality`** (one per `source`): `caption_manual → "human-authored captions"`, `caption_auto → "auto-captions — may contain errors, no punctuation/speaker labels"`, `whisper → "ASR (Whisper) — may contain errors, no speaker labels"`.
- `source` ∈ { caption_manual, caption_auto, whisper }. Cache files: `<id>.<lang>.txt`(+`.json`) for captions, `<id>.whisper.txt`(+`.json`) for ASR.

### Tools

- `get_youtube_transcript(url, lang?, mode?, cursor?, start?, end?, query?)` — canonicalize → cache-first → transcript (inline or chunk-1 + `next_cursor`) or `pending`+ETA. Description tells the model: pass messy/short URLs directly; on `partial` continue with `next_cursor` before answering; on `pending` relay the ETA and stop.
- `get_transcript_job(video_id)` — poll; **when `done`, returns the transcript directly** (same shape/pagination), collapsing 3 calls to 2. `not_found` → instruct re-call.

`selftest_egress` is **not** a model tool. Egress diagnostics live at authenticated/cluster-internal `GET /admin/egress` + a startup log; public `/healthz` is liveness only; probe uses a fixed internal video list.

## Configuration

Sizes accept human-readable forms (`2Gi`); all knobs have defaults/units.

| Env | Default | Notes |
|---|---|---|
| `YTT_ALLOWED_SUBJECTS` | *(empty = deny all)* | comma-separated subjects/emails |
| `YTT_RATE_LIMIT_PER_MIN` | `20` | per-subject token bucket |
| `YTT_WHISPER_JOBS_PER_HOUR` | `10` | per-subject ASR quota |
| `YTT_CACHE_DIR` | `/cache` | PVC or emptyDir mount |
| `YTT_CACHE_BACKEND` | `pvc` | `pvc`\|`emptydir` (volume set in manifest) |
| `YTT_CACHE_MAX_BYTES` | `2Gi` | ≤ volume size (startup-validated) |
| `YTT_CACHE_RECONCILE_SEC` | `300` | counter↔disk reconcile interval |
| `YTT_SCRATCH_DIR` | `/scratch` | audio temp; **separate** emptyDir w/ `sizeLimit` |
| `YTT_MAX_CONCURRENT_FETCHES` | `4` | yt-dlp caption fetches |
| `YTT_MAX_CONCURRENT_WHISPER` | `1` | shared CPU service |
| `YTT_WHISPER_URL` | `http://whisper-openai.whisper-stt.svc.cluster.local:8000` | |
| `YTT_WHISPER_MODEL` | `Systran/faster-whisper-small` | HF repo id; must be pre-pulled; self-corrects via `/v1/models` |
| `YTT_WHISPER_REALTIME_FACTOR` | `1.2` | ETA = duration×factor; confirm in Phase 6 |
| `YTT_WHISPER_TIMEOUT_SEC` | `1800` | HTTP timeout; must exceed MAX_ASR_DURATION×RT_FACTOR |
| `YTT_MAX_ASR_DURATION_SEC` | `1200` | refuse longer → `too_long_for_asr` |
| `YTT_JOB_TTL_SEC` | `3600` | GC for done/errored jobs |
| `YTT_INLINE_CHAR_LIMIT` | `18000` | ~6K tokens (Latin); non-Latin uses bytes/3 |
| `YTT_CHUNK_CHARS` | `18000` | char-offset chunk size |
| `YTT_PROXY_URL` | *(unset)* | optional Webshare fallback (itself datacenter IP) |
| OAuth | — | client id/secret, issuer/resource = `https://ytt.ardenone.com`, DCR off |

## Deliverables (file tree the agent must produce)

```
ytt/                      (repo root — already scaffolded: README, docs/)
  pyproject.toml          uv-managed; pinned Python 3.12.x; pinned exact fastmcp,
                          yt-dlp, uvicorn, httpx, pydantic-settings, prometheus-client,
                          structlog; uv.lock committed. [project.scripts] ytt=ytt.cli:main
  Dockerfile             multi-stage; base python:3.12-slim + `apt-get install -y ffmpeg`;
                          installs locked deps; non-root; CMD ["ytt","serve"]. Pinned digest.
  ytt/
    __init__.py (__version__)  __main__.py  cli.py  config.py  server.py
    canonicalize.py  fetch.py  parse_json3.py  whisper.py  cache.py
    auth.py  authz.py  ratelimit.py  errors.py  models.py  observability.py  selftest.py
  tests/unit/  tests/integration/  tests/fixtures/  (json3 rolling+manual, URL-form table,
                          stubbed DownloadError builders)
```
CLI: `ytt serve` (uvicorn, 1 worker — the container default), `ytt test [--unit|--integration]`, `ytt selftest` (egress probe). Exit 0/nonzero; `ytt test` emits JSON to stdout and the `/admin` endpoint.

**Manifests in `jedarden/declarative-config` (separate repo):**
- `k8s/ardenone-cluster/ytt/`: Deployment (`replicas:1`, `strategy: Recreate`, resource limits, ephemeral-storage limit, scratch+cache volumes), Service, PVC (`longhorn`, 2Gi), `ExternalSecret` (ESO/OpenBao), Traefik `IngressRoute` (`ytt.ardenone.com`), `NetworkPolicy` (egress + whisper-allow), `ServiceMonitor`, `PrometheusRule`, canary Deployment, `ytt-test` Deployment. (ArgoCD ApplicationSet **auto-discovers** this dir → app `ytt-ns-ardenone-cluster`, ns `ytt`, `CreateNamespace=true`.)
- `k8s/iad-ci/argo-workflows/ytt-build-workflowtemplate.yml` + `k8s/iad-ci/argo-events/ytt-sensor.yml`: Docker build → `ronaldraygun/ytt:<tag>` → auto-bump the tag in `k8s/ardenone-cluster/ytt/` (model on `telegram-claude-bridge-build`). Add a `ytt-build` row to CLAUDE.md's template table.

## Observability

- **Metrics (`prometheus-client`, `/metrics`):** `ytt_fetch_blocks_total`, `ytt_fetch_empty_body_total`, `ytt_whisper_errors_total`, `ytt_whisper_job_seconds`, `ytt_cache_bytes`, `ytt_cache_evictions_total`, `ytt_queue_depth`, `ytt_rate_limited_total`, `ytt_egress_is_residential` (gauge). Scraped via a **`ServiceMonitor`** (Prometheus-operator is present on the cluster).
- **Alerts (`PrometheusRule`, `promtool check rules` must pass):** block-rate spike → "home IP burned"; Whisper 5xx → "Whisper down"; sustained evictions → "cache undersized"; `egress_is_residential=0` → "egress changed". Route via the cluster's existing Alertmanager receiver.
- **Canary:** a long-running probe Deployment (no K8s Jobs) fetches a known-captioned video every N min and alerts on failure/empty-body — catches IP-burn and yt-dlp breakage before users.
- **Logging:** `structlog` JSON; a redaction filter guarantees tokens / subject list / transcript bodies are never logged.

## Performance budget

Personal scale — budgets are sanity targets, not SLAs:
- Cache-hit response: < 50 ms server-side.
- Caption fetch (cold, captioned video): p50 < 4 s, p99 < 12 s (network-bound).
- Whisper ETA accuracy: within ±50% of actual (calibrate `RT_FACTOR` in Phase 6).
- Concurrency: `YTT_MAX_CONCURRENT_FETCHES=4` is the first-cut ceiling (tune from observed memory); queue beyond it → `429`.
- Pod first-cut resources: requests `100m`/`256Mi`, limits `1`/`1Gi`, `ephemeral-storage` limit = scratch `sizeLimit` + cache `sizeLimit` + headroom.

## Acceptance Scenarios

1. **Captioned, short** — full transcript inline + segments + metadata; 2nd request cache-hit (no network), faster.
2. **Captioned, long** — chunk-1 + loud PARTIAL + `next_cursor` + `structuredContent`; continuation reassembles with no gaps/overlap; `is_final` on the last.
3. **Auto-captioned (rolling)** — transcript is **not doubled** (dedup) and matches expected text.
4. **No captions** — `pending`+ETA; `get_transcript_job` → `running` → status `ok` (the job's `done`) **and returns the transcript** (inline or paginated); cached `<id>.whisper.txt`; scratch audio gone.
5. **Concurrent same-video** — N simultaneous (caption + no-caption) → exactly one fetch / one Whisper job; fail if a 2nd job starts.
6. **Cache pressure** — bytes stay ≤ cap; whole units evicted LRU; `.txt`+`.json` never split.
7. **Egress residential** — `/admin/egress`/canary reports non-datacenter ASN; fetch succeeds without a proxy.
8. **Error taxonomy** — private/age/livestream/region/too-long each return their `error_code` + relayable `message`, no stack trace.
9. **AuthZ** — valid token, subject not in allowlist → `403`; empty allowlist denies all.
10. **Rate limit** — over `RATE_LIMIT_PER_MIN` → `429`+`Retry-After`; queue-full → `429`.
11. **URL forms** — `youtu.be`/`/shorts/`/`&list=`/bare id → one cache entry; playlist/channel → `bad_url`.
12. **Dependency-down** — whisper-openai 5xx/timeout → job lands `error` with `no_captions_asr_failed`; ENOSPC → serve-but-don't-cache.
13. **Connector add (manual)** — add on Claude desktop completes OAuth; same tool works from mobile without re-adding.
14. **WAF (manual)** — request from a non-allowlisted IP is blocked at the edge; allowlisted IP reaches the `401`.

Pass/fail: 1–6,8,11,12 integration; 9,10 unit+integration; 7 canary; 13,14 manual deploy checklist (explicitly human-gated).

## Testing Strategy

- **Unit — runs ANYWHERE.** URL canonicalization (every form + idempotence), **json3 dedup (rolling vs manual fixtures)**, language fallback (served-lang/`available_langs`/note), cache LRU + `bytes≤cap` invariant under concurrency + whole-unit eviction + `.tmp` exclusion + **reconcile drift correction** + **ENOSPC degrade**, single-flight dedup **both caption and Whisper-job paths** + **failed-Future cleanup**, **Whisper FSM** (transitions, TTL GC, `not_found`, restart re-kick), **orphan-audio startup sweep + failure-path delete**, pagination/cursor (char offsets, content-hash + **eviction → cursor_stale**), error-taxonomy seed-string table + `is_livestream`/`too_long_for_asr` never start a job, authz allow/deny, **rate-limit bucket refill + per-subject isolation** + queue-full→429, OAuth metadata shape + 401-`WWW-Authenticate` + **wrong-audience token rejected**, **wrong-`YTT_WHISPER_MODEL` startup guard** (stubbed `/v1/models`). Property-based tests for Invariants 1–6; Invariant 7 via a startup-validation unit test. Gates every phase; runs in Argo CI (`ytt-build` template).
- **Integration — `ardenone-cluster` only** (datacenter IPs blocked elsewhere). Scenarios 1–8,11,12 + a live concurrency check + a **saturation/load test** (drive the fetch semaphore to queue-full, assert `429`+`Retry-After`+`queue_depth`). Harness = `ytt test --integration` via `kubectl exec` into the pod or the `ytt-test` Deployment (no Jobs).
- **Local-dev honesty:** stdio dev covers only stubbed/fixture logic; **any real fetch/Whisper is datacenter-blocked off-cluster** — don't chase a "blocked" local fetch; verify real behavior in-cluster (Phase 9).
- **Manual/human-gated:** OAuth connector-add (13) and the WAF allowlist (14) — stated, not automatable.

## Proof Obligations / Known-Unknowns

| Decision (confidence) | Must be true | Invalidation signal | Fallback |
|---|---|---|---|
| Residential egress is "decisive, free" (HIGH) | ardenone-cluster egresses a non-datacenter ASN at deploy | `egress_is_residential=0`; block-rate spike; canary empty-body | Set `YTT_PROXY_URL` (Webshare) — **but Webshare is datacenter-range residential proxy you pay for; the "free" premise collapses and the cost/risk calculus changes.** Re-evaluate hosting. |
| `whisper-openai` model is pulled & named as expected (MED) | `GET /v1/models` lists a usable model | every ASR 404/500s | self-correct to first served model; pin the confirmed name |
| yt-dlp options stay valid (MED, drifts) | `tv/web_embedded/mweb` subs path works | `empty_body` metric / canary fail | bump pinned yt-dlp via SOP; rotate player_client |
| Anthropic egress range current (MED, drifts) | `160.79.104.0/21` still Anthropic | connector can't reach origin | update WAF rule from the published ranges |

## Implementation Phases

Each phase's exit = its unit suite green on one commit (real-fetch behavior is verified only in-cluster, Phase 9 — expected, not a bug).

- [ ] **Phase 0 (prereq):** create `pyproject.toml`+lock, package skeleton, Dockerfile (with ffmpeg), and the `ytt-build` WorkflowTemplate + Sensor in declarative-config; first image build. *(Touches a second repo.)*
- [ ] **Phase 1:** async MCP skeleton (tool stubs over Streamable HTTP), URL canonicalizer, config loader + startup validations; stdio dev. Exit: `tools/list` returns 2 tools; canonicalizer + idempotence tests green.
- [ ] **Phase 2:** fetch core — yt-dlp json3 + **dedup** + metadata, language selection, error taxonomy. Exit: parse/dedup/taxonomy unit tests green (rolling fixture asserts no doubling).
- [ ] **Phase 3:** concurrency — fetch pool + bounded queue + per-video single-flight (caption + job) + failed-Future cleanup. Exit: dedup + 429 tests green.
- [ ] **Phase 4:** cache — flat units, whole-unit LRU + lock discipline + reconcile + ENOSPC degrade + startup scan. Exit: invariant-under-concurrency tests green.
- [ ] **Phase 5:** AuthN/AuthZ + rate limiting (ADR-001) — FastMCP OAuth (audience-bound), subject allowlist `403`, token bucket + queue `429`. Exit: authz/ratelimit/metadata/wrong-audience tests green.
- [ ] **Phase 6:** Whisper — integrate `whisper-openai` (**confirm model via in-pod `/v1/models`**), job FSM + get-or-create + ETA + TTL GC, scratch vol + sweep, timeout, `too_long_for_asr`, `<id>.whisper.*` namespace, RT_FACTOR calibration. In Phase 6 `get_transcript_job` returns only the `pending`/`running`/`error` status envelope (a completed job's status is observable; **transcript delivery + pagination through `get_transcript_job` is wired in Phase 7**, since it depends on the chunking/cursor built there). Exit: FSM/sweep/model-guard tests green.
- [ ] **Phase 7:** response shape — `chunk` pagination (char offset + content-hash cursor + cursor_stale + loud PARTIAL + `structuredContent`), `start`/`end`/`query`; `get_transcript_job` returns transcript when done (ADR-002: chunk-only).
- [ ] **Phase 8:** observability + self-test — `/admin/egress`, startup egress log, metrics, `ServiceMonitor`, `PrometheusRule` (`promtool` passes), canary Deployment.
- [ ] **Phase 9:** in-cluster harness — `ytt test --integration` in ardenone-cluster; wire unit suite into Argo CI; prove scenarios 1–8,11,12 + load test.
- [ ] **Phase 10 (human-gated ops):** deploy via declarative-config (ApplicationSet auto-creates the app); first-sync ordering (ExternalSecret before Deployment); **human runs the credentialed one-time ops** — OpenBao secret writes, Cloudflare WAF rule, Traefik IngressRoute on the shared tunnel; then add the connector on desktop and verify mobile (13), verify WAF (14). The agent produces all manifests; a human supplies Cloudflare/OpenBao credentials.

## Deployment notes (ardenone-cluster)

- **GitOps only** via `jedarden/declarative-config` + ArgoCD; never `kubectl apply` (selfHeal reverts). The cluster is read-only from the EX44 box.
- **ApplicationSet auto-discovers** `k8s/ardenone-cluster/ytt/` → app `ytt-ns-ardenone-cluster`, ns `ytt`. No hand-authored Application. First sync: ExternalSecret must materialize before the Deployment mounts it (expect a transient CrashLoop, or use sync-waves).
- **Ingress:** Service + Traefik **IngressRoute** for `ytt.ardenone.com` on the **existing shared cluster tunnel** — **no per-pod cloudflared sidecar** (one edge exposure per cluster). WAF allowlist applied at the Cloudflare edge.
- **Image tags, not digests** (matches the repo's `sed`-based auto-bump): `ronaldraygun/ytt:<semver-or-sha>`, no `:latest`. Bump SOP: edit deps → CI builds tag → run integration suite in-cluster → `sed`-bump tag in `k8s/ardenone-cluster/ytt/` → commit → Argo syncs. **Rollback = `git revert` the bump commit** (never `kubectl`; selfHeal undoes live edits).
- **Whisper dep:** `whisper-openai.whisper-stt.svc:8000` (ClusterIP, same cluster). NetworkPolicy allows egress to `whisper-openai` in ns `whisper-stt` (not the `whisper-stt` service).
- **Storage:** `longhorn` class (confirm name); PVC vs emptyDir per `YTT_CACHE_BACKEND`.
- **Resources:** set the first-cut requests/limits + ephemeral-storage from the Performance budget; confirm a node has room for the (light) ytt pod.
- **Secrets:** ESO `ExternalSecret` from OpenBao paths `ardenone-cluster/ytt/*`; documented rotation; never logged. **No cookies.**
- **Decommission (reverse of bootstrap):** remove the connector in Claude → remove WAF rule + IngressRoute → delete `k8s/ardenone-cluster/ytt/` (Argo prunes) → delete the OpenBao paths.

## Risks & posture

- **YouTube ToS / takedown** — bulk extraction violates ToS; personal + allowlisted + rate-limited is the mitigation.
- **Household collateral** — the residential IP is the user's home internet; a burn degrades the household. The Webshare proxy *protects* it; consider making the proxy the default if non-trivial volume is expected (see the Webshare Open Question).
- **yt-dlp breakage** — surfaced by the canary + `empty_body` metric; bump via SOP.
- **Single replica = no HA** — restart drops in-flight jobs and (emptyDir) the cache; accepted for v1. ytt is a read-only consumer of whisper-openai, so it rolls back independently of that dependency.

## Open Questions

- Should Webshare be the **default** (protect the home IP) rather than fallback? **(resolve before Phase 10.)**
- Confirm the exact `longhorn` storage class name on this cluster. **(resolve before Phase 4 manifest / Phase 10.)**
- Calibrate `YTT_WHISPER_REALTIME_FACTOR` + confirm `YTT_WHISPER_MODEL` residency against the live service. **(Phase 6 blocks on this.)**
- Multi-client identity — scope cache/rate-limits per OAuth subject, or global?

## Resolved (was open)

- Self-host (yt-dlp), no third-party API. · Python + FastMCP. · Host on `ardenone-cluster` (residential, proven via self-test/canary). · Whisper = cluster `whisper-openai`. · Cache = flat files on PVC/emptyDir, LRU. · Integration tests in-cluster; unit anywhere. · Mobile OAuth path: none to build; register both callbacks. · AuthZ = subject allowlist (fail-closed) + DCR off + rate limits. · `replicas:1`. · `mode:summary` dropped → `start/end/query` slicing. · Inbound IP allowlist = Cloudflare **WAF** (not Access; not the pod). · **Hostname = `ytt.ardenone.com`.** · **ADR-001 = FastMCP self-issued, audience-bound, DCR off.** · **ADR-002 = chunk-only v1.** · **Edge = shared tunnel + Traefik IngressRoute (no sidecar).** · **Secrets = ESO/OpenBao.** · **Image = immutable tags (not digests).** · **ffmpeg in image.** · **json3 rolling dedup required.** · **ETA/timeout/duration reconciled.**
