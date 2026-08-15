# ytt Plan

## Overview

A remote MCP server that reliably downloads transcripts from pasted YouTube links, usable as a custom connector in both Claude mobile (iOS/Android) and Claude desktop. All transcript fetching happens **inside the MCP server** — no third-party transcript APIs. The server handles concurrent requests from multiple clients and caches transcripts in a size-bounded flat-file store for fast repeat access.

**Deployment target: `ardenone-cluster`, which egresses from a residential internet plan.** This natively solves the YouTube datacenter-IP block — yt-dlp's outbound requests come from a residential IP. This is a load-bearing assumption; it is tracked as a Proof Obligation (see that section), not silently trusted.

**Public URL (decided): `https://mcp.ardenone.com/ytt`** — path-based, **co-hosted on the shared `mcp.ardenone.com` host alongside the existing `ibkr-mcp` (`/ibkr`)**, matching this cluster's established MCP convention. The OAuth resource/issuer URL **and the token audience** are the full path-bearing identifier `https://mcp.ardenone.com/ytt` (RFC 9728 §3.3 — byte-identical everywhere). `ytt` clones the live `k8s/ardenone-cluster/ibkr-mcp/` manifests as its template. See "Deploying alongside ibkr-mcp" in Deployment notes for the full pattern, including `.well-known` handling via **additive** higher-priority routes that leave ibkr untouched (do-no-harm to ibkr is a hard constraint).

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
- **Do no harm to `ibkr-mcp` (hard constraint).** `ytt` shares the `mcp.ardenone.com` host, the Cloudflare tunnel, and the Traefik instance with the live `ibkr-mcp`. **Deployment and testing must not change, degrade, or risk ibkr.** This is achieved by being **purely additive** — `ytt` adds its own higher-priority, more-specific routes and **never edits ibkr's manifests**; tests target ytt's own ClusterIP/namespace; and an **ibkr regression smoke check gates the rollout** (ibkr's `.well-known` + a basic call must pass before *and* after). No host-level change (WAF, tunnel, ibkr route) is made unless verified non-impacting to ibkr.
- **Public + portable (the repo is open-source).** The image is published to **GHCR for anyone to run**, and the code must carry **no ardenone-cluster specifics** — every deployment fact (public URL, Whisper endpoint, egress/proxy, OAuth subjects, storage) is env/manifest config so a stranger can self-host. In-repo **end-user + contributor documentation is a deliverable**, not an afterthought.

## Architecture

```
Claude mobile / desktop / web  ×N clients
   │  (custom connector, Streamable HTTP, OAuth 2.1 + PKCE)
   │  add-connector + consent on web/Desktop; the MCP client that
   │  calls tools is ANTHROPIC'S BACKEND (unattended, from its cloud)
   ▼
Cloudflare edge  ──  optional host-level WAF rule on mcp.ardenone.com (NOT Access —
   │                  Access would bounce the unattended backend): allow only IPv4
   │                  160.79.104.0/21 (+ IPv6 2607:6bc0::/48). NOTE: this host is
   │                  SHARED with ibkr-mcp, so the rule is host-wide; subject
   │                  allowlist + OAuth is the real per-tool authz.
   ▼
Shared cluster Cloudflare Tunnel ─► Traefik ─► IngressRoute on Host(mcp.ardenone.com)
   │   match: PathPrefix(/ytt) || PathPrefix(/.well-known/oauth-*/ytt)
   │   (reuse the existing one-tunnel + Traefik convention; co-hosted with
   │    ibkr at /ibkr; NO per-pod cloudflared sidecar; external-dns → tunnel CNAME)
   ▼
ytt MCP server (async, replicas:1, uvicorn 1 worker, served under /ytt)
   ├─ /ytt/health (liveness, unauth, carved out of auth middleware)
   ├─ OAuth resource-server endpoints (path-inserted .well-known) — audience = https://mcp.ardenone.com/ytt
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
- Must be **publicly reachable over HTTPS** — the connector client is Anthropic's backend, not the phone. Tailscale-only is invisible to it. Exposed via the **shared cluster Cloudflare Tunnel + a Traefik IngressRoute on `Host(mcp.ardenone.com)` path-prefixed `/ytt`** (not a sidecar; co-hosted with ibkr-mcp — see Deployment). The server is **path-prefix-aware** (all routes, metadata, and emitted URLs carry `/ytt`), exactly like ibkr-mcp's `MCP_PUBLIC_URL`/`MCP_PATH_PREFIX`.

### Security & Authorization

OAuth is **authentication**, not **authorization**. On the public internet any Claude user who learns the URL could otherwise drive yt-dlp + CPU-Whisper through the household's home internet. Authorization is therefore first-class.

- **Subject allowlist (required).** After token validation every tool call checks the token **`sub` claim** against `YTT_ALLOWED_SUBJECTS`; not listed → `403`. **Empty allowlist = deny all** (fail-closed). The `sub` claim (not `email`) is the stable identifier set by the FastMCP AS — its exact value for a Claude-registered connector must be confirmed empirically at Phase 5. **Sub discovery mechanism (not blocked by the redaction filter):** add a `ytt selftest --show-sub` CLI command that reads the last decoded token `sub` from a temporary file (`/tmp/ytt_last_sub`, written once on first successful auth, mode 0600, not logged to stdout). Operators run this command after completing the OAuth flow once; the output is the exact string to place in `YTT_ALLOWED_SUBJECTS`. Document this in `docs/usage/connector.md`. The redaction filter blocks `sub` from **logs**; the temp file mechanism is not a log.
- **No open DCR in v1.** FastMCP self-issued tokens / a single known client; if DCR is ever enabled, gate behind a pre-shared token. Authorize on **subject**, never the client name (the apps register as `client_name: "claudeai"`). **The allowlist is intentionally enforced at tool invocation, not at token issuance.** Token issuance succeeds for any user who completes the OAuth flow — the token proves identity; authorization is a separate decision. A `403` at tool call (with a human-readable message: "Contact the server operator to be added to the allowlist") is the correct and intended response. Do not add allowlist checking to the `/authorize` endpoint.
- **Inbound IP allowlist (optional, host-level) at Cloudflare WAF, not the pod and not Access.** Behind the tunnel the origin sees only the tunnel, so a NetworkPolicy/app source-IP check can't enforce it. **Cloudflare Access is wrong here** — it's an identity gate that would challenge/bounce Anthropic's unattended backend. A **WAF custom rule** allowing only `160.79.104.0/21` (+ the IPv6 range) can be applied — **but `mcp.ardenone.com` is shared with ibkr-mcp, so the rule is host-wide and must be coordinated** (both are Claude-only connectors, so an Anthropic-only rule is compatible with both; ibkr currently ships none). It is **defense-in-depth only** — the **subject allowlist + audience-bound OAuth is the real per-tool authz** and stands on its own if the host-level WAF is skipped.
- **CORS middleware** (clone ibkr/stock-research): allow origins `https://claude.ai` + `https://desktop.claude.ai` + `https://claude.com`, methods GET/POST/OPTIONS, headers Authorization/Content-Type/Accept — required for the browser-side of the connector add/consent flow. (Include `https://claude.com` because the OAuth callback URLs include `https://claude.com/api/mcp/auth_callback`; browser preflight requests originating from `claude.com` would otherwise be blocked.)
- **Per-subject rate limiting.** Token bucket (`YTT_RATE_LIMIT_PER_MIN`) + Whisper quota (`YTT_WHISPER_JOBS_PER_HOUR`); a **bounded queue** in front of the fetch semaphore returns `429`+`Retry-After` when full. Cap total in-flight WhisperJobs.
- **Egress NetworkPolicy.** Restrict pod egress to YouTube/`googlevideo`, `whisper-openai.whisper-stt.svc`, Cloudflare, and the IP-info host (`ipinfo.io`); deny the rest. (Over-tight egress mimics IP-burn in metrics — start permissive + DNS-log, then tighten.) **Always allow port 53 UDP+TCP to all namespaces (kube-dns) for hostname resolution** — without it, all name-based egress rules silently fail (the pod cannot resolve `youtube.com` or cluster-local FQDNs before the IP allow-list is consulted). Mirror ibkr-mcp's DNS egress rule.
- **No YouTube cookies, ever.** A cookie file ties the household Google account to the scraper. Standing constraint. **Enforce in code:** the yt-dlp options dict passed to every `YDL()` call must explicitly set `cookiefile=None` and `cookiesfrombrowser=None` to prevent yt-dlp config files in the image from silently activating cookie extraction. Add a unit test asserting these keys are present and falsy in the production options object.
- **Secrets via ESO/OpenBao** (the cluster norm), not SealedSecrets. Paths under `ardenone-cluster/ytt/*` (e.g. `…/oauth-client-secret`, `…/webshare-url`). Enumerate keys in `ExternalSecret` manifests; document rotation; **never log** tokens, the subject list, or transcript bodies (enforced by a logging filter).
- **Diagnostic hygiene.** Public `/ytt/health` returns only liveness (matches ibkr's `/ibkr/health`; it's the k8s probe path too). Egress IP/ASN detail is at `GET /admin/egress` — requires a valid Bearer token with a subject in `YTT_ALLOWED_SUBJECTS` (same auth as tool calls; no special admin token). **Routing note:** `/ytt/admin/egress` is reachable via the public `PathPrefix(/ytt)` IngressRoute — there is no Traefik-level restriction to cluster-internal. App-level auth (Bearer token + allowlist) is therefore the **sole** gate. Consider adding a `ClientIPFilter` middleware or a dedicated internal port if stricter isolation is required; for v1, app-level auth is accepted and the "cluster-internal only" claim is removed. Its probe uses a **fixed internal video list**, never caller input.

### Auth: OAuth-secured under MCP OAuth (ADR-001 decided)

Server is the OAuth 2.1 **Resource Server**. See `docs/research/mcp-oauth-authentication.md` + `docs/research/claude-app-mcp-oauth-implementation.md`. Non-negotiable surface:

- Unauthenticated → `401` + `WWW-Authenticate: Bearer resource_metadata="…"` (the #1 silent "Add connector" failure). `/ytt/health` exempt.
- Serve RFC 9728 Protected Resource Metadata at the **path-inserted** location `https://mcp.ardenone.com/.well-known/oauth-protected-resource/ytt` (well-known at the host root, resource path `/ytt` appended — RFC 9728 §3.1). The `resource` field inside MUST equal `https://mcp.ardenone.com/ytt` exactly.
- Emit `WWW-Authenticate: Bearer resource_metadata="https://mcp.ardenone.com/.well-known/oauth-protected-resource/ytt", scope="..."` — an **absolute, byte-stable** URL. Claude follows this verbatim, which makes discovery path-correct regardless of root-probing; the server must actually serve the doc at that exact URL (a header/route mismatch is a known failure).
- AS metadata (RFC 8414) advertises `code_challenge_methods_supported: ["S256"]`.
- **Audience-bound** token validation (RFC 8707) — `aud` must match the full path-bearing `https://mcp.ardenone.com/ytt` (no trailing slash), else confused-deputy replay. **Strict path-bearing audience is what keeps the shared host safe**: an `/ibkr` token must never validate against `/ytt`. (Residual risk: a client that normalizes the resource to origin or appends a trailing slash — known in Claude *Code* CLI, not confirmed on the hosted surfaces; ibkr already runs path-based on hosted Claude, which is the working precedent. Flag for an empirical check at connector-add.)

Claude-app behavior: the connector client is Anthropic's backend (public ingress mandatory); **mobile can't ADD connectors** (no separate mobile path — get web/Desktop accepted and mobile reuses the token); **register both** `https://claude.ai/api/mcp/auth_callback` + `https://claude.com/api/mcp/auth_callback`; Advanced-settings manual client id/secret is web/Desktop only.

**ADR-001 (decided): FastMCP self-issued tokens with audience binding to `https://mcp.ardenone.com/ytt`, DCR disabled**, plus the subject allowlist. ytt is its own OAuth AS (as ibkr-mcp is its own); the AS metadata is served path-inserted at `https://mcp.ardenone.com/.well-known/oauth-authorization-server/ytt` so it doesn't collide with ibkr's. FastMCP `JWTVerifier(audience=…)` / `RemoteAuthProvider` (pin FastMCP ≥ 2.11.1, exact in lockfile; verify the self-issued token emits a spec-compliant `aud`). Don't hand-roll metadata. (Rust `rmcp` is client-only → Python locked.) **Phase 5 spike items:** (a) Locate the FastMCP API for disabling DCR and for registering a static client with **two** `redirect_uris` (`https://claude.ai/api/mcp/auth_callback` AND `https://claude.com/api/mcp/auth_callback`); if FastMCP only supports one redirect URI per registration, register two client entries sharing the same `client_id`. (b) Confirm the exact FastMCP config option to disable DCR. Document both in `auth.py` as inline comments.

**ADR-003 (decided, supersedes the undocumented Google pivot): federate to the org's self-hosted Authentik (`sso.ardenone.com`) instead of Google, via FastMCP's generic `fastmcp.server.auth.oidc_proxy.OIDCProxy`.** ADR-001's "self-issued tokens" design was never implemented as written — the shipped 0.2.0 code (`04b58da`) federates to Google via `GoogleProvider` (an `OAuthProxy` proxying Google as upstream IdP) instead, undocumented here until now. That Google client is *also* literally shared with ibkr-mcp (same GCP OAuth app, per the old `auth.py` docstring) — an unintentional coupling where two independent servers' authorization depended on one shared external credential. Reasons to move off Google now: (1) org-wide standardization on self-hosted Authentik as the IdP — OpenBao already federates this way (`k8s/openbao-authentik-oidc-runbook.md`), and ytt should follow the same precedent rather than staying a Google-only outlier; (2) `OIDCProxy` is generic (endpoints discovered from `.well-known/openid-configuration`), so no per-IdP wrapper code is needed the way `GoogleProvider` required; (3) it breaks the accidental ibkr-mcp credential coupling — ytt gets its own Authentik application/client, independently revocable and auditable. **Mechanics:** `YttGoogleProvider(GoogleProvider)` → `YttOIDCProvider(OIDCProxy)`; the same `get_routes()` path-insertion override carries over unchanged (that quirk lives in the shared `OAuthProxy` base, not in `GoogleProvider`). `config_url = "https://sso.ardenone.com/application/o/ytt/.well-known/openid-configuration"`. Same `Settings.oauth_client_id`/`oauth_client_secret` fields, same OpenBao path (`ardenone-cluster/ytt/oauth-client-id`/`-secret`) — only the values change; no ExternalSecret/manifest edits needed on the app side. **Resolved 2026-08-15, took two rounds: `verify_id_token=True` alone was NOT enough — a custom HS256 `token_verifier` is also required.** Originally framed as an open question about whether `email`/`email_verified` land on the access token; turned out to be load-bearing for a much bigger reason, discovered in two passes of live `FASTMCP_LOG_LEVEL=DEBUG` debugging (not guessed either time). **Round 1:** assumed Authentik signs access tokens HS256 but id tokens RS256, so `verify_id_token=True` alone should fix JWKS verification. Shipped as 0.2.8 — `invalid_token` persisted, identically, now on the id token. **Round 2:** this Authentik instance signs *every* OAuth2Provider's tokens — access AND id alike — with HS256 (symmetric, keyed by the client_secret), confirmed decisively by comparing against OpenBao's own provider (`openbao-ardenone-manager`), which authenticates real users daily: byte-identical `id_token_signing_alg_values_supported: ["HS256"]` and an equally empty JWKS (`{}`). This is normal, spec-compliant OIDC for confidential clients (OIDC Core §10.1) — not an Authentik misconfiguration to chase further (there is exactly one certificate on this instance, already selected as the provider's signing key; there is no other option to pick). OpenBao's OIDC client evidently never verifies the token's signature via JWKS at all — it trusts the token because it received it directly from Authentik's token endpoint over authenticated TLS. `OIDCProxy.get_token_verifier()` has no equivalent option: it unconditionally builds a JWKS-based `JWTVerifier`, with no path for symmetric verification, so it could never work against this IdP regardless of which token it pointed at. **Actual fix:** construct a `JWTVerifier(public_key=<client_secret>, algorithm="HS256", issuer=..., audience=<client_id>)` by hand — it explicitly documents shared-secret support via `public_key` — and pass it as `token_verifier=`, bypassing `OIDCProxy`'s auto-construction. `verify_id_token=True` is still required alongside it (selects the id_token, whose `aud`=client_id per OIDC Core §2, as the string handed to this verifier — and still settles the original claim-locality question, email/email_verified come from the id token). Shipped as 0.2.9. **Infra prerequisite:** a new Authentik OAuth2 provider + application blueprint entry in `declarative-config` (`k8s/ardenone-cluster/authentik/authentik-blueprints-configmap.yml`), same shape as the existing `openbao-oidc.yaml` entry — confidential client, redirect URI `https://mcp.ardenone.com/ytt/auth/callback`, scopes `openid`+`email`, policy binding restricted to the group authorized to use ytt. Authentik generates `client_id`/`client_secret` on first apply (not set in the blueprint, same as OpenBao's) — retrieving them and writing them to OpenBao is a manual step, not automatable from here. **Blueprint landed via a concurrent session** (declarative-config `3b8da323`, found already on `main` mid-implementation here with a garbled description — "YAML Template Transformer" — fixed in `3254e7a8` without touching the provider/application identifiers). **RFC 8707 — the static-analysis "ruled out" call was WRONG; confirmed live 2026-08-15 and fixed with `forward_resource=False`.** The original reasoning (a `oauth_proxy/proxy.py` source comment claiming "Claude doesn't send a resource parameter at all") turned out not to describe the real Claude connector: Traefik access logs on ardenone-cluster show Claude's own `GET /ytt/authorize` **does** send `resource=https://mcp.ardenone.com/ytt`. `OIDCProxy`'s default `forward_resource=True` then relayed it to Authentik's `.../application/o/authorize/` call, which immediately 302'd to an error — `GET /ytt/auth/callback?error=invalid_request&error_description=The+request+is+otherwise+malformed`, i.e. exactly the rejection [[reference_authentik_oauth_for_mcp]] predicted, never reaching Authentik's login page at all (this is what the user saw as a "FastMCP OAuth Error" page — ytt relaying Authentik's own rejection). Fix: `build_auth_provider()` now passes `forward_resource=False` to `YttOIDCProvider(...)`. This is the concrete lesson: **a code comment describing "what the client does" is not verified behavior — test the actual live flow before trusting it**, doubly so when a static claim contradicts a same-day memory built from a different project's live testing against the same Authentik instance.

### URL → canonical video_id (load-bearing)

Runs **before** any `extract_info`; two URL forms of one video must collapse to one ID (else duplicate fetches + cache misses).

- Normalize: `watch?v=`, `youtu.be/`, `/shorts/`, `/live/`, `/embed/`, `m.`/`music.youtube.com`, `&list=`/`&t=`/`&pp=` (stripped), bare 11-char ID.
- **Reject** playlist-only, channel (`/channel/`, `/@handle`), search → `error_code: bad_url`.
- Invariant: `canon(canon(x)) == canon(x)` (see Invariants). Unit fixtures per form (Phase 2).

### Fetch core (in-server, no third-party API)

In-process via the yt-dlp Python API (Unlicense). The `extract_info(url, download=False)` call also yields the **metadata** we surface (title, channel, duration, `published`). See `docs/research/yt-dlp-caption-extraction.md`.

1. **Captions** — `skip_download=True`, `writesubtitles=True`, `writeautomaticsub=True`, `subtitlesformat='json3'`, `extractor_args={'youtube': {'player_client': ['tv','web_embedded','mweb']}}` (avoid `web` — its subtitle endpoint needs a PoToken, returns empty bodies). Prefer manual `subtitles`, fall back to `automatic_captions`. Fetch the json3 track URL in-process. **The same `extractor_args` player_client override is required for the audio download path** (Whisper fallback `bestaudio`); reuse the same options dict (minus subtitle flags) to avoid the PoToken requirement there too.
2. **json3 parse — MUST dedup rolling auto-captions.** Auto-caption (`kind == "asr"`) tracks are a *rolling* stream: events re-emit prior words plus one new word, with overlapping `[tStartMs, tStartMs+dDurationMs]` windows and append markers. Naive `"".join(events[].segs[].utf8)` over all events **doubles the text and poisons the cache** (confirmed yt-dlp gotcha #6274/#1734; cf. `srt_fix`). **Append markers:** `aAppend=true` on a segment means it continues the previous word with no leading space; `pAppend=true` appends punctuation without a space. Algorithm: walk events in ascending `tStartMs` order; skip events with no `utf8` content (formatting-only); maintain a `last_end_ms` cursor initialized to 0; **emit an event only if `tStartMs >= last_end_ms`** (discard events whose window has already been covered); on emit, update `last_end_ms = tStartMs + dDurationMs` and collect the segment texts (respecting `aAppend`/`pAppend` spacing). **Prefix check** (secondary dedup for near-boundary events): if an event's concatenated text is a whitespace-stripped, case-sensitive strict prefix of the immediately following event's text, discard it. Concat survivors. **Manual tracks (`kind != "asr"`) are clean** → straight concat. Mandatory fixtures: one real rolling auto track (assert no doubling, assert output matches a pre-verified reference string) + one manual track. This is the single most likely "looks done, is broken" bug — gate Phase 2 on it.
3. **Error taxonomy** (stable `error_code` + verbatim-relayable `message`). Seed string→code map (a maintenance point; pin yt-dlp): `"Private video"→private`, `"members-only"→members_only`, `"Sign in to confirm your age"→age_restricted`, `"not available in your country"→region_blocked`, `"This live event will begin"/"is live"→is_livestream` (never enters Whisper), `"Video unavailable"/"has been removed"→unavailable`, `"HTTP Error 429"→rate_limited`, `"Sign in to confirm you're not a bot"/"HTTP Error 403"/"Did not get any data blocks"→ip_blocked`, empty/unrecognized→`empty_body` (distinct metric so silent breakage is visible). Also: `bad_url`, `too_long_for_asr`. *(Note: `no_captions_asr_started` and `no_captions_asr_failed` are **not** TranscriptResult `error_code` values — they are labels for the `ytt_fetch_blocks_total` metric and for the WhisperJob's internal `error_code` field. When get_youtube_transcript discovers no captions and starts Whisper, it returns `status=pending` with no `error_code`; the message field explains what's happening. When a WhisperJob fails and `get_transcript_job` is called, it returns `status=error, error_code="asr_failed", message="Transcription failed: <upstream error summary>. Re-call get_youtube_transcript to retry."` Add `asr_failed` to the `error_code` enum in `errors.py`.)* On `ip_blocked` (**on any fetch path — caption or audio**): surface clearly, fire the egress self-test asynchronously (don't block the current request), then if `YTT_PROXY_URL` is set, retry the same operation once with `proxy=YTT_PROXY_URL` in the yt-dlp options. If the retry also fails, return `ip_blocked` (note the proxy attempt in the message). The proxy retry applies equally to caption fetches and Whisper audio downloads — it is not limited to the Whisper path. **`not_found`** — job ID is unknown (expired TTL GC or never existed); emitted by `get_transcript_job` only; message = `"Job not found. Re-call get_youtube_transcript with the video URL to start a new request."` Define `not_found` as a standalone `error_code` constant in `errors.py` (not in the yt-dlp string-parsing map — it is a logical error emitted by `get_transcript_job`, not a yt-dlp error string).

### Language selection

- `lang` omitted → default/original caption language → English → any available.
- `lang=X` → manual[X] → auto[X] → manual[default] → auto[default]. **Auto-translation out for v1.** When the exact language is unavailable, serve the fallback and set `lang` = **served** language, `requested_lang` = X, `available_langs` listed, `message` = "requested X unavailable; served Y". **`available_langs` format:** a list of BCP-47 language tags normalized from yt-dlp's caption track keys — strip `.auto` suffix, lowercase, de-duplicate (e.g., `['en', 'es', 'fr']`). These are valid values for the `lang` parameter in a follow-up `get_youtube_transcript` call.
- Cache filename uses the **served** lang.

### Whisper fallback — the cluster's universal `whisper-openai`

- **Service:** `whisper-openai` in ns `whisper-stt` — `http://whisper-openai.whisper-stt.svc.cluster.local:8000`, image `fedirz/faster-whisper-server` (**upstream is now renamed `speaches`** — use speaches.ai for docs/model names; the cluster runs the frozen old image). OpenAI-compatible `POST /v1/audio/transcriptions` (multipart `file`,`model`,`response_format`) + `GET /v1/models`. **Do not** use the PBX `whisper-stt:8080` service.
- **Model:** `model` is a **HuggingFace repo ID, required, and must already be pulled on the shared service** (an un-pulled model 404/500s the first call). `YTT_WHISPER_MODEL` default `Systran/faster-whisper-small` (multilingual, fast on CPU). **Self-correcting:** on startup query `GET /v1/models`; if the configured model isn't listed, fall back to the first served model and log loudly (don't 500 forever). Phase 6 confirms name + residency via an in-pod `curl`.
- **CPU-only + shared.** Keep `YTT_MAX_CONCURRENT_WHISPER=1` (don't starve PBX). 
- **ETA / timeout / duration are reconciled** (they were inconsistent): `ETA_sec = duration_sec × YTT_WHISPER_REALTIME_FACTOR` (default 1.2 — CPU transcription is ~realtime-or-slower; confirm against the live model in Phase 6). **Invariant:** `YTT_MAX_ASR_DURATION_SEC × RT_FACTOR < YTT_WHISPER_TIMEOUT_SEC`. Defaults satisfy it: `MAX_ASR_DURATION=1200` (20 min) × `1.2` = 1440s < `TIMEOUT=2880`s (default; see Configuration table and Performance budget note on 2× margin). Longer videos → refuse with `too_long_for_asr` (message includes cap + length).
- **Single-flight covers discovery + job.** The single-flight key wraps the lang-agnostic `extract_info` discovery; `WhisperJob` creation is **get-or-create under a lock keyed by video_id** — a second request during `pending`/`running` returns the existing job, never starts a second run or re-downloads audio.
- **Job state machine:** `pending → running → done | error`. First caption-less call returns `pending` + ETA + message ("No captions; transcribing now (~N min); ask me again shortly"); the tool **description tells the model to relay the ETA and stop — not tight-poll**.
- **Audio lifecycle:** download bestaudio to a **dedicated scratch volume** (`YTT_SCRATCH_DIR`, separate from cache), projected audio size checked **before** download against the cap `min(YTT_MAX_AUDIO_BYTES, statvfs(YTT_SCRATCH_DIR) free)` (reject → `too_long_for_asr`/insufficient-space), POST, then delete via context-manager (success or failure). **Startup sweep** clears audio orphaned by a crash (see below). Needs **ffmpeg** in the image. **Projected size** = `info['filesize'] or info['filesize_approx'] or None` from the best-audio format entry in `extract_info`. If `None` (e.g., live streams, age-gated): proceed but monitor download size against `min(YTT_MAX_AUDIO_BYTES, statvfs_free)` during streaming using a **`progress_hooks`** callback: the hook checks `downloaded_bytes` on each progress event and raises `yt_dlp.utils.DownloadError('audio_too_large')` if the cap is exceeded. Map `'audio_too_large'` → `too_long_for_asr` in the error taxonomy. Clean up via context-manager (success or failure). Log a warning at job start when size is unknown. **Startup sweep** = delete all files in `YTT_SCRATCH_DIR` unconditionally on server startup (safe because `replicas:1` + `strategy:Recreate` guarantee no peer is running; any file present is stale). Log file names + sizes before deletion.
- **Registry:** in-memory + TTL GC (`YTT_JOB_TTL_SEC`). On restart in-flight jobs are lost; `get_transcript_job(unknown)` → `not_found` with "re-call get_youtube_transcript" (idempotent re-kick). Completed results survive restart **only with PVC** (emptyDir loses them — stated tradeoff). Failed Futures are removed atomically; **errors are never cached as transcripts**. **Evicted result during polling:** if `get_transcript_job` finds a job in `done` state but the `result_ref` cache file is absent (evicted between job completion and polling), return `status=error, error_code=not_found, message="Transcript was cached but has been evicted. Re-call get_youtube_transcript to re-fetch."` Remove the job entry from the registry. Do not silently re-trigger Whisper. **Stale `running` job GC:** the TTL GC also expires jobs that have been in `running` state longer than `YTT_WHISPER_TIMEOUT_SEC + YTT_JOB_TTL_SEC` — these are tasks whose asyncio Future completed (timeout/error) but whose registry entry was somehow not cleaned up. Log at ERROR (`video_id`, `elapsed_sec`) and remove. Include a unit test for this transition.
- **Progress notifications:** not relied on (clients don't uniformly honor `resetTimeoutOnProgress`); async-job + cache-poll instead. Best-effort keepalive only on the sub-60s caption path.

### Concurrency (multiple clients in parallel)

- Fully async; blocking yt-dlp runs via `asyncio.to_thread`. **`extract_info` must be wrapped in `asyncio.wait_for(..., timeout=YTT_EXTRACT_TIMEOUT_SEC)` (default `60`s).** On timeout, resolve the single-flight Future as an error and remove it from the registry; return `error_code: rate_limited` (the most likely cause of a silent hang) or `empty_body` if the cause is unknown. Add `YTT_EXTRACT_TIMEOUT_SEC` to the Configuration table.
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
- **Reconcile.** Every `YTT_CACHE_RECONCILE_SEC` (default 300): re-`stat` all units, recompute the total, overwrite the in-memory counter, log if drift exceeds a threshold. **If the reconciled total exceeds `YTT_CACHE_MAX_BYTES`** (possible if the counter was under-estimated due to a bug or external writes), immediately run the LRU eviction loop under the cache lock until the total is at or below cap. Log: `"Reconcile found total=X > cap=Y; evicting Z units."` This ensures the cache-bound invariant is re-established at reconcile time, not only on writes.
- **ENOSPC.** Evict-and-retry once, then degrade to **serve-but-don't-cache** (don't error the request).
- **Backends.** PVC (persistent; `storageClassName: longhorn`; size = `requests.storage`) or emptyDir (ephemeral; `sizeLimit`; loses completed Whisper results). Startup validation behavior depends on backend: for **PVC**, read volume size via `os.statvfs(YTT_CACHE_DIR)` (`f_blocks`×`f_frsize`) and **fail fast** if `YTT_CACHE_MAX_BYTES` exceeds it. For **emptyDir**, skip the statvfs check (statvfs returns node disk, not the kubelet-enforced `sizeLimit` — the check would always pass and give a false sense of safety); instead, log a startup warning: `"emptyDir: ensure YTT_CACHE_MAX_BYTES ({X}) ≤ manifest sizeLimit; no automatic enforcement possible from the app."` **Default: PVC, `2Gi`.**
- **TTL (optional).** Captions rarely change; livestream/`processing` discovery results get a **short/zero TTL** so a finished stream isn't pinned as `is_livestream`.

### Response shape & size (MCP has no spec limit; clients cap)

Claude Code ~25K-token default, 500K-char ceiling; the API connector inlines everything. See `docs/research/mcp-response-limits.md`.

- **Modes `full` | `chunk`** (default `full`). `summary` **dropped** (no server-side LLM — it'd lie or truncate; the model summarizes). Honest token-reducers instead: `start`/`end` (segment time bounds) and `query` (**case-insensitive substring** match, returns matching segments **±2 segments of context**, mutually exclusive with `start`/`end`). **Mode semantics:** `mode=full` inlines the transcript when text ≤ `YTT_INLINE_CHAR_LIMIT`; paginates (returns chunk 1 + continuation hint) when over. `mode=chunk` **always** returns paginated output regardless of transcript length — it is an explicit opt-in to pagination; callers should always expect `offset`/`total_chars`/`is_final` in the response, even for short videos.
- **Inline vs paginate.** Inline when text ≤ `YTT_INLINE_CHAR_LIMIT`. `mode:full` on an over-limit transcript still paginates (it never violates the client cap); the difference vs `chunk` is only that `full` defaults to returning chunk 1 with the continuation hint.
- **Char-offset chunking** (`YTT_CHUNK_CHARS`), don't split mid-multibyte, segment-align when segments are returned. MCP pagination does **not** apply to tool results — this is our own arg.
- **Token budget is conservative for non-Latin.** `YTT_INLINE_CHAR_LIMIT`/`YTT_CHUNK_CHARS` default 18000 chars ≈ 6K tokens at **3.0 chars/token** (Latin). Dense CJK/Thai/Arabic run ~1–2 chars/token, so for non-Latin scripts estimate tokens as `bytes/3` and cap on that — a fixed char count alone can approach the 25K ceiling in one chunk.
- **Filtered pagination.** When `query`/`start`/`end` are present, they produce a **filtered virtual document**; pagination (`offset`/`total_chars`/cursor) operates over that filtered document, not the full transcript, and the filter args are part of the cursor (below) so a different filter can't reuse a stale cursor.
- **Cursor.** Opaque string = `base64url(sha256(json.dumps({"c": base64(content_bytes).decode(), "lang": served_lang, "source": source, "filter": canonical_filter_args}).encode())[:16])` + `':'` + `str(total_chars)` + `':'` + `str(offset)`. Example: `"abc123def456ghi7:45000:18000"`. Use structured serialization (not raw byte concatenation) to avoid hash collisions between distinct `(content, lang)` pairs. `canonical_filter_args` = dict with sorted keys of active filter args (`query`, `start`, `end`); empty dict `{}` if no filter is active. Bound to the (possibly filtered) addressable content. If the unit changed/refreshed → `error_code: cursor_stale` (force fresh page-1). **If the unit was evicted between pages → also `cursor_stale`** (never silently re-fetch and serve at the old offset, which could swap content). Result carries `offset`, `total_chars`, `is_final`.
- **Loud partial.** Non-final chunk text leads with `⚠️ PARTIAL: chars A–B of T (chunk i/n). INCOMPLETE — call get_youtube_transcript again with cursor='…' before summarizing, unless the user only needs the start.` Machine-readable continuation also in `structuredContent`.
- **ADR-002 (decided): chunking for v1.** No `https://` transcript link in v1 (it would need a public path under `/ytt` + an unguessable token + its own authz). `transcript_url` stays unset until a later phase ships it.

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
- **Ingress** — shared cluster Cloudflare Tunnel → Traefik **IngressRoute** on `Host(mcp.ardenone.com)` PathPrefix `/ytt` (co-hosted with ibkr-mcp); SSE + CORS middlewares; internal-CA origin cert; optional host-level WAF IP allowlist at the edge.
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

- **`status` (unified)** ∈ `{ ok, partial, pending, running, error }`. A WhisperJob `done` surfaces through `get_transcript_job` as TranscriptResult `status: ok` (explicit mapping; the job's internal `done` is never a TranscriptResult status). `EgressReport.is_residential` matches the `ytt_egress_is_residential` metric (one term); it is **derived** (ipinfo.io returns `org`/ASN, not a residential flag): `is_residential = asn not in DATACENTER_ASNS and not any(p in org.upper() for p in DATACENTER_ORG_PATTERNS)`. Seed `DATACENTER_ASNS` in `selftest.py` as a `frozenset` of known-datacenter AS numbers (AWS/AS16509, GCP/AS15169, Azure/AS8075, DigitalOcean/AS14061, Linode/AS63949, Cloudflare/AS13335, etc.) and `DATACENTER_ORG_PATTERNS` as a tuple of uppercase string fragments (`'AMAZON'`, `'GOOGLE'`, `'MICROSOFT'`, `'DIGITALOCEAN'`, `'LINODE'`, `'CLOUDFLARE'`). Both are maintenance points — update on canary false-positives. This derivation is the concrete test behind the residential Proof Obligation.
- **Per-status field matrix** (what is set):
  - `ok` — text|segments, source, **transcript_quality**, lang, metadata, is_final=true; on a language-fallback hit also `requested_lang` + `available_langs` + `message`.
  - `partial` — text, source, lang, transcript_quality, offset, total_chars, next_cursor, is_final=false.
  - `pending`/`running` — eta_sec, message (no text).
  - `error` — error_code, message (no text).
- **`transcript_quality`** (one per `source`): `caption_manual → "human-authored captions"`, `caption_auto → "auto-captions — may contain errors, no punctuation/speaker labels"`, `whisper → "ASR (Whisper) — may contain errors, no speaker labels"`.
- `source` ∈ { caption_manual, caption_auto, whisper }. Cache files: `<id>.<lang>.txt`(+`.json`) for captions, `<id>.whisper.txt`(+`.json`) for ASR.

### Tools

- `get_youtube_transcript(url, lang?, mode?, cursor?, start?, end?, query?)` — canonicalize → cache-first → transcript (inline or chunk-1 + `next_cursor`) or `pending`+ETA. Description tells the model: pass messy/short URLs directly; on `partial` continue with `next_cursor` before answering; on `pending` relay the ETA and stop.
- `get_transcript_job(video_id)` — poll; **when `done`, returns the transcript directly** (same shape/pagination), collapsing 3 calls to 2. `not_found` → instruct re-call. *(Implementation note: delivered in two phases — job status envelope only in Phase 6; full transcript delivery wired in Phase 7, which depends on the chunking/cursor layer. This is intentional phase sequencing, not a contract change. **Phase 6 behavior when the job is `done`:** return `status=ok` with `text=None`/`segments=None` and `message="Transcript ready; call get_youtube_transcript again to retrieve it."` — this is a temporary stub; Phase 7 replaces it with actual transcript delivery.)*

`selftest_egress` is **not** a model tool. Egress diagnostics live at authenticated/cluster-internal `GET /admin/egress` + a startup log; public `/ytt/health` is liveness only; probe uses a fixed internal video list.

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
| `YTT_MAX_AUDIO_BYTES` | `500Mi` | per-job audio cap; checked before download (also bounded by scratch free space) |
| `YTT_MAX_CONCURRENT_FETCHES` | `4` | yt-dlp caption fetches |
| `YTT_MAX_CONCURRENT_WHISPER` | `1` | shared CPU service |
| `YTT_WHISPER_URL` | `http://whisper-openai.whisper-stt.svc.cluster.local:8000` | |
| `YTT_WHISPER_MODEL` | `Systran/faster-whisper-small` | HF repo id; must be pre-pulled; self-corrects via `/v1/models` |
| `YTT_WHISPER_REALTIME_FACTOR` | `1.2` | ETA = duration×factor; confirm in Phase 6 |
| `YTT_WHISPER_TIMEOUT_SEC` | `2880` | HTTP timeout; must exceed MAX_ASR_DURATION×RT_FACTOR (2880 = 1200×1.2×2.0, providing 2× margin for ±50% ETA variance) |
| `YTT_MAX_ASR_DURATION_SEC` | `1200` | refuse longer → `too_long_for_asr` |
| `YTT_JOB_TTL_SEC` | `3600` | GC for done/errored jobs |
| `YTT_CANARY_INTERVAL_SEC` | `600` | canary probe interval (consumed by the canary Deployment, not the main server) |
| `YTT_INLINE_CHAR_LIMIT` | `18000` | ~6K tokens (Latin); non-Latin uses bytes/3 |
| `YTT_CHUNK_CHARS` | `18000` | char-offset chunk size |
| `YTT_EXTRACT_TIMEOUT_SEC` | `60` | timeout for yt-dlp `extract_info` call; on expiry resolves the single-flight Future as error |
| `YTT_PROXY_URL` | *(unset)* | optional Webshare fallback (itself datacenter IP) |
| `YTT_PATH_PREFIX` | `/ytt/` | path the server is mounted under (matches the IngressRoute). Must end with `/` — startup validation raises an error and exits 1 if the trailing slash is missing. **Path construction rule:** always strip one leading slash from route segments — `prefix + route.lstrip('/')` → `/ytt/health`. This avoids double-slash when `prefix` ends with `/` and `route` starts with `/`. Codify as a helper in `config.py`. |
| `YTT_PUBLIC_URL` | `https://mcp.ardenone.com/ytt` | public base URL; OAuth resource/audience + emitted metadata derive from it |
| OAuth | — | client id/secret, issuer/resource = `https://mcp.ardenone.com/ytt`, DCR off |

## Deliverables (file tree the agent must produce)

This repo is **public** — the deliverables include a publishable image (GHCR) and end-user/contributor docs, not just internal code.

```
ytt/                      (repo root — already scaffolded: README, docs/)
  README.md               PUBLIC front door: what it is, quick-start (docker pull
                          ghcr.io/jedarden/ytt + run), env-config reference, "add as a
                          Claude connector" steps, self-hosting requirements/caveats
  LICENSE                 OSS license (e.g. MIT/Apache-2.0) — required for a public repo
  CONTRIBUTING.md  SECURITY.md  CHANGELOG.md  NOTICE
  docs/                   (internal: notes/, research/, plan/) + a public docs/usage/:
    usage/configuration.md   full env-var reference (the Configuration table, expanded)
    usage/self-hosting.md    deploy anywhere: BYO Whisper, BYO residential egress/proxy,
                             BYO OAuth subjects; what's required vs optional
    usage/connector.md       add to Claude desktop/mobile; OAuth/allowlist setup
    usage/deploy-ardenone.md the ardenone-cluster/ibkr co-hosting specifics (reference)
  pyproject.toml          uv-managed; pinned Python 3.12.x; pinned exact fastmcp,
                          yt-dlp, uvicorn, httpx, pydantic-settings, prometheus-client,
                          structlog; uv.lock committed. [project.scripts] ytt=ytt.cli:main
  Dockerfile             multi-stage; base python:3.12-slim + `apt-get install -y ffmpeg`;
                          a test stage runs `pytest` (red = failed build); installs locked
                          deps; non-root; CMD ["ytt","serve"]. OCI labels → repo. Pinned digest.
  ytt/
    __init__.py (__version__)  __main__.py  cli.py  config.py  server.py
    canonicalize.py  fetch.py  parse_json3.py  whisper.py  cache.py
    auth.py  authz.py  ratelimit.py  errors.py  models.py  observability.py  selftest.py
    canary.py     (standalone probe: calls yt-dlp directly, not via the tool endpoint — bypasses OAuth; emits Prometheus metrics via pushgateway or exposes /metrics on a separate port consumed by the ServiceMonitor in the canary Deployment)
  tests/unit/  tests/integration/  tests/fixtures/  (json3 rolling+manual, URL-form table,
                          stubbed DownloadError builders)
```
CLI: `ytt serve` (uvicorn, 1 worker — the container default), `ytt test [--unit|--integration]`, `ytt selftest` (egress probe). Exit 0/nonzero; `ytt test` emits JSON to stdout and the `/admin` endpoint.

### Documentation (in-repo, public-facing)

Because the repo is public, documentation is a tracked deliverable, written *alongside* the code (not deferred to the end):

- **`README.md`** — the front door: one-paragraph what/why, **quick-start** (`docker run ghcr.io/jedarden/ytt …` with the minimal env), the tool list, a link into `docs/usage/`, and an honest "requirements & caveats" box (needs a residential egress IP or a proxy; needs a Whisper endpoint for caption-less videos; single-replica). Required **before** the repo is advertised as usable by others.
- **`docs/usage/configuration.md`** — the full env-var reference (the Configuration table expanded with units/defaults/examples). Lands when config stabilizes (Phase 1, updated per phase).
- **`docs/usage/self-hosting.md`** — deploy-anywhere guide: BYO Whisper, BYO residential egress/proxy, BYO OAuth subjects; what's required vs optional; a plain `docker-compose`/single-pod example that does **not** assume ardenone/ibkr.
- **`docs/usage/connector.md`** — how to add the server to Claude desktop/mobile, the OAuth flow, and setting the subject allowlist.
- **`docs/usage/deploy-ardenone.md`** — the ardenone-cluster + `mcp.ardenone.com/ytt` + ibkr co-hosting specifics, as a concrete reference deployment (keeps cluster-specifics out of the generic docs).
- **`LICENSE`** (OSS), **`CONTRIBUTING.md`**, **`SECURITY.md`** (how to report; the authz/abuse posture), **`CHANGELOG.md`** (keep-a-changelog; entries gate each release tag).

The existing `docs/notes`, `docs/research`, `docs/plan` stay as the **internal** design record; `docs/usage/` is the **external** surface. Docs are verified as part of the release gate (README quick-start must actually work against the published GHCR image).

**Manifests in `jedarden/declarative-config` (separate repo):**
- `k8s/ardenone-cluster/ytt/` (clone the live `k8s/ardenone-cluster/ibkr-mcp/` manifests as the template): Deployment (`replicas:1`, `strategy: Recreate`, resource limits, ephemeral-storage limit, scratch+cache volumes; container port 8080), Service (ClusterIP `:8080`), PVC (`longhorn`, 2Gi), `ExternalSecret` (ESO/OpenBao), Traefik `IngressRoute` on `Host(mcp.ardenone.com)` + `Middleware`s (SSE no-buffering + CORS), cert-manager `Certificate` for `mcp.ardenone.com` via `ardenone-ca-issuer` (secret `mcp-ardenone-com-tls` in the `ytt` ns), `NetworkPolicy` (egress + whisper-allow), `ServiceMonitor`, `PrometheusRule`, canary Deployment, `ytt-test` Deployment. (ArgoCD ApplicationSet **auto-discovers** this dir → app `ytt-ns-ardenone-cluster`, ns `ytt`, `CreateNamespace=true`.)
- **No edit to ibkr's manifests.** `ytt`'s `.well-known` interception is **additive**: ytt adds its own higher-`priority`, suffix-specific root rules so Traefik routes only ytt's metadata to ytt, leaving ibkr's broad `/.well-known` rule (and everything else) untouched. See "`.well-known` handling" in Deployment notes. (Narrowing ibkr's route is explicitly *avoided* — it would put ibkr at risk for no benefit.)
- `k8s/iad-ci/argo-workflows/ytt-build-workflowtemplate.yml` + `k8s/iad-ci/argo-events/ytt-sensor.yml`: on push to `jedarden/ytt` → **run `pytest` (unit) → build → push to GHCR `ghcr.io/jedarden/ytt:<tag>`** (public; the image others consume) → auto-bump the tag in `k8s/ardenone-cluster/ytt/` (model on `telegram-claude-bridge-build`, adding a test step + the GHCR destination). Needs a **GHCR push credential** (GitHub token with `write:packages`) stored in iad-ci. **Verify whether iad-ci has ESO installed** (it may not — it is the CI cluster, not ardenone-cluster). If ESO is available: use an `ExternalSecret` from OpenBao. If not: use a Kubernetes `Secret` created directly (iad-ci is internal CI infra, not subject to the same ESO-only rule as ardenone-cluster production workloads); document the creation command in `k8s/iad-ci/argo-workflows/` README or a comment in the WorkflowTemplate. Add a `ytt-build` row to CLAUDE.md's template table. **GitHub Actions stay disabled — the build runs on Argo and pushes to GHCR** (don't reach for GH Actions just because the registry is GitHub's).

## Observability

- **Metrics (`prometheus-client`, `/metrics`):** `ytt_fetch_blocks_total{outcome=<error_code|"ok">}`, `ytt_fetch_empty_body_total`, `ytt_whisper_errors_total{reason=<error_code>}`, `ytt_whisper_job_seconds` (histogram), `ytt_cache_bytes` (gauge), `ytt_cache_evictions_total`, `ytt_queue_depth` (gauge), `ytt_rate_limited_total{subject_hash=<8-char>}`, `ytt_egress_is_residential` (gauge). The `outcome` label on `ytt_fetch_blocks_total` uses the stable `error_code` string (e.g., `no_captions_asr_started`, `no_captions_asr_failed`, `ip_blocked`) or `"ok"` on success. Scraped via a **`ServiceMonitor`** (Prometheus-operator is present on the cluster).
- **Alerts (`PrometheusRule`, `promtool check rules` must pass):** block-rate spike → "home IP burned"; Whisper 5xx → "Whisper down"; sustained evictions → "cache undersized"; `ytt_egress_is_residential=0` → "egress changed". Route via the cluster's existing Alertmanager receiver.
- **Canary:** a long-running probe Deployment (no K8s Jobs) runs `canary.py` — which calls **yt-dlp directly** (the same code path as `fetch.py`, not via the HTTP tool endpoint) using a fixed internal video list. This avoids the OAuth + allowlist gate entirely and tests what actually matters: residential egress and yt-dlp caption extraction. Interval: every 10 min (`YTT_CANARY_INTERVAL_SEC=600`). Metrics emitted by the canary Deployment: `ytt_canary_last_success_timestamp_seconds` (gauge, updated on each successful probe), `ytt_canary_failures_total` (counter). PrometheusRule: fire `YttCanaryFailed` if `time() - ytt_canary_last_success_timestamp_seconds > 1800` (3 consecutive probes missed). Route via the cluster's existing Alertmanager receiver.
- **Logging:** `structlog` JSON to stdout; a redaction filter guarantees tokens / subject list / transcript bodies are **never** logged. Every log event is structured (no bare f-string messages). Required log events and levels:

  | Event | Level | Key fields |
  |---|---|---|
  | Server startup | INFO | `public_url`, `cache_backend`, `cache_max_bytes`, `whisper_url`, `whisper_model`, `max_concurrent_fetches`, `subjects_count` (count only, not values) |
  | Startup validation failure | ERROR | `reason`, then exit 1 |
  | Cache startup scan | INFO | `units_found`, `total_bytes`, `stale_tmp_cleaned` |
  | Scratch dir startup sweep | INFO | `files_deleted`, `bytes_freed` |
  | Whisper model self-correct | WARNING | `configured_model`, `fallback_model`, `available_models` |
  | Tool call received | INFO | `tool`, `video_id` (if extractable), `subject_hash` (sha256[:8] of sub — never the raw sub) |
  | Cache hit | INFO | `video_id`, `lang`, `source`, `size_bytes` |
  | Cache miss | INFO | `video_id`, `lang` |
  | Fetch started | INFO | `video_id` |
  | Fetch completed | INFO | `video_id`, `source`, `lang`, `duration_sec`, `elapsed_ms` |
  | Fetch error | WARNING | `video_id`, `error_code`, `elapsed_ms` |
  | Fetch blocked by semaphore | INFO | `video_id`, `queue_depth` |
  | 429 returned | INFO | `video_id`, `reason` (semaphore_full or rate_limit), `subject_hash` |
  | Cache write | DEBUG | `video_id`, `lang`, `size_bytes`, `total_cache_bytes` |
  | Cache eviction | INFO | `evicted_units`, `bytes_freed`, `trigger` (write or reconcile) |
  | Cache ENOSPC degrade | WARNING | `video_id`, `lang` |
  | Reconcile | INFO | `recomputed_bytes`, `counter_was`, `drift_bytes`, `evictions_triggered` |
  | WhisperJob created | INFO | `video_id`, `eta_sec`, `duration_sec` |
  | WhisperJob state change | INFO | `video_id`, `old_status`, `new_status` |
  | WhisperJob TTL GC | INFO | `job_count_before`, `job_count_after` |
  | ip_blocked detected | ERROR | `video_id`, `via_proxy` (bool) |
  | Egress self-test result | INFO | `ip`, `asn`, `org`, `is_residential` |
  | AuthZ 403 | WARNING | `subject_hash`, `tool` |
  | Rate limit 429 | WARNING | `subject_hash`, `tool`, `bucket_remaining` |
  | Wrong-audience token rejected | WARNING | `token_aud`, `expected_aud` |

  Redaction filter blocks: raw `sub`/`email` values, `YTT_ALLOWED_SUBJECTS` contents, any field named `token`, `secret`, `key`, `authorization`, `transcript` (bodies), `audio_path`, `proxy_url`. **Any URL containing `@` (credential-bearing)** must be sanitized before logging — strip user:password from the URL and log only the host:port. This prevents Webshare credentials from leaking via startup logs, error logs, or the `ip_blocked` retry message. Log rotation is handled by the container runtime (stdout → cluster log aggregation); no in-process file rotation needed.

## Performance budget

Personal scale — budgets are sanity targets, not SLAs:
- Cache-hit response: < 50 ms server-side.
- Caption fetch (cold, captioned video): p50 < 4 s, p99 < 12 s (network-bound).
- Whisper ETA accuracy: within ±50% of actual (calibrate `RT_FACTOR` in Phase 9, in-cluster). **Note:** the Timeout invariant (`MAX_ASR_DURATION × RT_FACTOR < TIMEOUT`) is validated against configured values, not runtime variance. With ±50% actual variance the default `TIMEOUT=1800s` provides only ~25% headroom for a max-duration video (`1200 × 1.2 × 1.25 = 1800`). Consider defaulting `YTT_WHISPER_TIMEOUT_SEC=2880` (`1200 × 1.2 × 2.0`) for a full 2× margin. Confirm during Phase 9 calibration.
- Concurrency: `YTT_MAX_CONCURRENT_FETCHES=4` is the first-cut ceiling (tune from observed memory); queue beyond it → `429`.
- Pod first-cut resources: requests `100m`/`256Mi`, limits `1`/`1Gi`, `ephemeral-storage` limit = scratch `sizeLimit` + headroom (+ cache `sizeLimit` only when `backend=emptydir`; a PVC cache is a separate volume and doesn't count toward ephemeral-storage).

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
14. **Co-hosting isolation + ibkr-unchanged (manual)** — `ytt`'s `/.well-known/oauth-protected-resource/ytt` resolves with `resource = https://mcp.ardenone.com/ytt`; an `/ibkr`-audience token is **rejected** by `ytt` (and vice-versa); **ibkr's `.well-known/*` and a basic ibkr call are byte-for-byte identical before and after the ytt rollout** (the do-no-harm gate). If the optional host-level WAF is adopted: non-allowlisted IP blocked at the edge, allowlisted IP reaches `401` — and re-verify ibkr still reachable.

Pass/fail: 1–6,8,11,12 integration; 9,10 unit+integration; 7 canary; 13,14 manual deploy checklist (explicitly human-gated).

## Testing Strategy

- **Unit — runs ANYWHERE.** URL canonicalization (every form + idempotence), **json3 dedup (rolling vs manual fixtures)**, language fallback (served-lang/`available_langs`/note), cache LRU + `bytes≤cap` invariant under concurrency + whole-unit eviction + `.tmp` exclusion + **reconcile drift correction** + **ENOSPC degrade**, single-flight dedup **both caption and Whisper-job paths** + **failed-Future cleanup**, **Whisper FSM** (transitions, TTL GC, `not_found`, restart re-kick), **orphan-audio startup sweep + failure-path delete**, pagination/cursor (char offsets, content-hash + **eviction → cursor_stale**), error-taxonomy seed-string table + `is_livestream`/`too_long_for_asr` never start a job, authz allow/deny, **rate-limit bucket refill + per-subject isolation** + queue-full→429, OAuth metadata shape + 401-`WWW-Authenticate` + **wrong-audience token rejected**, **wrong-`YTT_WHISPER_MODEL` startup guard** (stubbed `/v1/models`). Property-based tests for Invariants 1–6; Invariant 7 via a startup-validation unit test. Gates every phase; runs in Argo CI (`ytt-build` template).
- **Integration — `ardenone-cluster` only** (datacenter IPs blocked elsewhere). Scenarios 1–6,8,11,12,14 + a live concurrency check + a **saturation/load test** (drive the fetch semaphore to queue-full, assert `429`+`Retry-After`+`queue_depth`). Harness = `ytt test --integration` via `kubectl exec` into the pod or the `ytt-test` Deployment (no Jobs).
  - **Do-no-harm to ibkr (every run):** the saturation/load test targets ytt's **own ClusterIP Service** in-cluster, **never the shared Cloudflare edge/Traefik**, so it can't stress ibkr's ingress. An **ibkr smoke check** (ibkr's `.well-known/*` + a basic call) runs as a fixture at the start *and* end of the suite and must be byte-identical — a diff fails the run. No ytt test writes to or restarts anything outside the `ytt` namespace.
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

- [ ] **Phase 0 (prereq):** create `pyproject.toml`+lock, package skeleton, Dockerfile (with ffmpeg), and the `ytt-build` WorkflowTemplate + Sensor in declarative-config; first image build. *(Touches a second repo.)* Exit: Docker build completes without error; `docker run --rm <image> ytt --help` exits 0; WorkflowTemplate in declarative-config passes `argo lint` dry-run.
- [ ] **Phase 1:** async MCP skeleton (tool stubs over Streamable HTTP), URL canonicalizer, config loader + startup validations; stdio dev. Exit: `tools/list` returns 2 tools; canonicalizer + idempotence tests green.
- [ ] **Phase 2:** fetch core — yt-dlp json3 + **dedup** + metadata, language selection, error taxonomy. Exit: parse/dedup/taxonomy unit tests green (rolling fixture asserts no doubling).
- [ ] **Phase 3:** concurrency — fetch pool + bounded queue + per-video single-flight (caption discovery path) + failed-Future cleanup. (The WhisperJob get-or-create single-flight lands in Phase 6 with the job FSM.) Exit: single-flight dedup tests green (concurrent same-video requests trigger exactly one in-flight yt-dlp call) + 429/queue-full tests green. *(Note: "dedup" here means single-flight dedup, not the json3 rolling-caption dedup from Phase 2.)*
- [ ] **Phase 4:** cache — flat units, whole-unit LRU + lock discipline + reconcile + ENOSPC degrade + startup scan. Exit: invariant-under-concurrency tests green.
- [ ] **Phase 5:** AuthN/AuthZ + rate limiting (ADR-001) — FastMCP OAuth (audience-bound), subject allowlist `403`, token bucket + queue `429`. **Spike first: confirm FastMCP can emit path-bearing identifiers (`resource`/`issuer` = `…/ytt`) at the path-inserted `.well-known` location; if not, implement custom Starlette metadata routes.** Exit: authz/ratelimit/metadata-shape (path-inserted, path-bearing `resource`)/wrong-audience tests green.
- [ ] **Phase 6:** Whisper — integrate `whisper-openai`, job FSM + get-or-create + ETA + TTL GC, scratch vol + sweep, timeout, `too_long_for_asr`, `<id>.whisper.*` namespace. In Phase 6 `get_transcript_job` returns only the `pending`/`running`/`error` status envelope (a completed job's status is observable; **transcript delivery + pagination through `get_transcript_job` is wired in Phase 7**, since it depends on the chunking/cursor built there). Exit: FSM/sweep/model-guard tests green (use stubbed `/v1/models` response for unit tests). **Note:** in-pod model-name confirmation and RT_FACTOR calibration require a live Whisper call; these are Phase 9 tasks (added there).
- [ ] **Phase 7:** response shape — `chunk` pagination (char offset + content-hash cursor + cursor_stale + loud PARTIAL + `structuredContent`), `start`/`end`/`query`; `get_transcript_job` returns transcript when done (ADR-002: chunk-only).
- [ ] **Phase 8:** observability + self-test — `/admin/egress`, startup egress log, metrics, `ServiceMonitor`, `PrometheusRule` (`promtool` passes), canary Deployment.
- [ ] **Phase 9:** in-cluster harness — `ytt test --integration` in ardenone-cluster; wire unit suite into Argo CI; prove scenarios 1–6,8,11,12 + load test (7 is canary-verified). Also: **confirm model name via in-pod `curl GET /v1/models`** and **calibrate `YTT_WHISPER_REALTIME_FACTOR`** against observed transcription time for a known-duration video (update the default in config if measured factor differs by >20%).
- [ ] **Phase 10 (human-gated ops):** deploy via declarative-config (ApplicationSet auto-creates the app). **ibkr regression gate (do-no-harm):** capture ibkr's `.well-known/*` responses + a basic ibkr call **before** apply; ytt's routing is purely additive (no ibkr manifest edit); re-run the ibkr checks **after** apply and confirm byte-for-byte unchanged — if anything differs, roll back the ytt change (`git revert`) before proceeding. First-sync ordering: ExternalSecret before Deployment. **Human runs the credentialed one-time ops** — OpenBao secret writes; the host-level Cloudflare WAF rule only if explicitly chosen (it touches the shared host → re-verify ibkr after). DNS + IngressRoute + cert need no manual step (external-dns → existing tunnel; in-cluster CA). Then add the connector on desktop (URL `https://mcp.ardenone.com/ytt`), verify mobile (13), and verify well-known + audience isolation + ibkr-unchanged (14). The agent produces all manifests; a human supplies OpenBao/Cloudflare credentials.
- [ ] **Phase 11 (public release):** set the GHCR package public + linked to the repo; finalize `README.md` quick-start and `docs/usage/*`; add `LICENSE`/`CONTRIBUTING`/`SECURITY`/`CHANGELOG`; tag the first semver release. Add a **`NOTICE`** file listing bundled third-party licenses: ffmpeg (LGPL/GPL Debian build — `apt-get install ffmpeg` in the image), yt-dlp (Unlicense). Reference `NOTICE` from `LICENSE`. **Release gate:** the README quick-start must actually run against the published `ghcr.io/jedarden/ytt` image (a clean self-host smoke test, no ardenone specifics) before the repo is advertised as usable by others.

## Deployment notes (ardenone-cluster)

- **GitOps only** via `jedarden/declarative-config` + ArgoCD; never `kubectl apply` (selfHeal reverts). The cluster is read-only from the EX44 box.
- **ApplicationSet auto-discovers** `k8s/ardenone-cluster/ytt/` → app `ytt-ns-ardenone-cluster`, ns `ytt`. No hand-authored Application. First sync: ExternalSecret must materialize before the Deployment mounts it (expect a transient CrashLoop, or use sync-waves).
### Deploying alongside ibkr-mcp (shared `mcp.ardenone.com` host)

`ytt` co-hosts with the live `ibkr-mcp` (`/ibkr`) on one host, reusing the **existing shared cluster Cloudflare Tunnel + Traefik** — no new tunnel, no per-pod cloudflared sidecar. Clone the `k8s/ardenone-cluster/ibkr-mcp/` manifests; the only structural difference is the path (`/ytt`) and the **additive** `.well-known` interception (next subsection) — **ibkr's manifests are not touched.**

- **IngressRoute** (`ytt` namespace), mirroring ibkr's. The two `.well-known/*/ytt` rules carry an explicit higher `priority` so they intercept only ytt's metadata; ibkr's broad rule still serves everything else:
  ```
  match: Host(`mcp.ardenone.com`) && (PathPrefix(`/ytt`)
         || PathPrefix(`/.well-known/oauth-protected-resource/ytt`)
         || PathPrefix(`/.well-known/oauth-authorization-server/ytt`))
  priority: 1000           # ibkr has no explicit priority; Traefik auto-computes from rule string length (~80 chars → ~80 priority). Use 1000 for a wide, stable margin — add a Phase 10 verification step: curl /.well-known/oauth-protected-resource/ytt and confirm resource=https://mcp.ardenone.com/ytt (not ibkr's).
  entryPoints: [websecure, vpn]
  services: [{ name: ytt, port: 8080 }]
  middlewares: [ytt-sse, ytt-cors]
  tls: { secretName: mcp-ardenone-com-tls }
  annotations:  # external-dns auto-creates the DNS → existing tunnel (SAME target as ibkr)
    external-dns.alpha.kubernetes.io/hostname: mcp.ardenone.com
    external-dns.alpha.kubernetes.io/target: 062c8e8a-8c15-4afb-ad08-9430743550fe.cfargotunnel.com
  ```
#### `.well-known` handling (three layers — and NOT breaking ibkr)

The OAuth metadata documents live at the **host root** (`mcp.ardenone.com/.well-known/…`), not under `/ytt`, so they can't ride the `PathPrefix(/ytt)` rule. Serving them correctly for ytt *without touching ibkr* requires all three layers:

1. **Routing (Traefik) — additive, ibkr untouched.** `ytt`'s IngressRoute adds two **more-specific, explicitly higher-`priority`** root rules: `PathPrefix(/.well-known/oauth-protected-resource/ytt)` and `PathPrefix(/.well-known/oauth-authorization-server/ytt)`. Being longer/more specific than ibkr's broad `PathPrefix(/.well-known)` and carrying an explicit higher `priority`, Traefik routes **only ytt's suffixes** to ytt; **everything else — including ibkr's own `/ibkr` suffix and any bare-root probe — keeps hitting ibkr's existing rule, unchanged.** ⇒ **ibkr's manifest is NOT edited** (the key difference from a "narrow both routes" approach — additive interception carries zero risk to ibkr). The OIDC-appended form `/ytt/.well-known/openid-configuration` already falls under `PathPrefix(/ytt)`, so only the two root-inserted forms need these extra rules.
2. **Server (app) — serve the exact path-inserted URLs.** ytt answers at those root paths (not under `/ytt`) with **path-bearing** identifiers: PRM `resource = https://mcp.ardenone.com/ytt`; AS metadata `issuer = https://mcp.ardenone.com/ytt`, all endpoints under `/ytt`, `S256`. **No `StripPrefix` middleware** (the app receives full paths). And **always emit `WWW-Authenticate: Bearer resource_metadata="https://mcp.ardenone.com/.well-known/oauth-protected-resource/ytt"`** on 401 — Claude follows it verbatim, making discovery deterministic and removing any dependence on the ambiguous bare-root probe. ⚠️ **FastMCP risk (Phase-5 spike):** FastMCP's built-in OAuth metadata typically assumes a **root-origin** server (`resource = origin`, well-known at root). Confirm it can emit *path-bearing* identifiers at the *path-inserted* location; if not, add **custom Starlette routes** serving the corrected documents. This is the most likely place the build stalls.
3. **Verification (proves no impact to ibkr).** From in-cluster, curl all four and assert: ytt's PRM/AS return `…/ytt`; **ibkr's PRM/AS still return `…/ibkr` byte-for-byte unchanged**; ytt's transport 401 carries the right `WWW-Authenticate`. The **bare un-suffixed root probe** (`/.well-known/oauth-protected-resource`, no suffix) is the one case a shared host can't serve for two tools — by design ytt never needs it (header-driven), but the connector-add acceptance test (scenario 14) must confirm the hosted Claude surfaces don't require it; if one does, that surface needs subdomain-per-tool (ytt's fallback), and **ibkr is still unaffected** either way.
- **Middlewares** (clone ibkr/stock-research): `ytt-sse` sets `X-Accel-Buffering: no` + `Cache-Control: no-cache` (un-buffer Streamable HTTP); `ytt-cors` allows `https://claude.ai` + `https://desktop.claude.ai` + `https://claude.com`.
- **TLS:** cert-manager `Certificate` for CN `mcp.ardenone.com` via the internal `ardenone-ca-issuer` ClusterIssuer → secret `mcp-ardenone-com-tls` **in the `ytt` namespace** (secrets are namespaced; ytt needs its own copy even though ibkr has one for the same host). This is the origin cert (cloudflared→Traefik); Claude sees Cloudflare's edge cert.
- **App is path-prefix-aware:** set `YTT_PUBLIC_URL=https://mcp.ardenone.com/ytt` and `YTT_PATH_PREFIX=/ytt/` (mirrors ibkr's `MCP_PUBLIC_URL`/`MCP_PATH_PREFIX`); all transport routes, health (`/ytt/health`), `.well-known`, `resource`, and `aud` derive from it.
- **Optional host-level WAF:** if the Anthropic-IP allowlist is adopted, it applies to the whole `mcp.ardenone.com` host (shared with ibkr) — coordinate it as a host concern, or skip it and rely on the per-tool OAuth + subject allowlist (the real authz). ibkr currently ships none.
### Image publishing (public repo → GHCR)

`jedarden/ytt` is **public**, so the image must live where the community can pull it. Decision:

- **Primary registry = GHCR `ghcr.io/jedarden/ytt`** (public). This is the image external users pull and the cluster deploys. The GHCR **package must be set to public** and **linked to the repo** (GHCR packages default to private even for a public repo). Add OCI `org.opencontainers.image.source` labels so the package links back.
- **Cluster pulls GHCR directly with no imagePullSecret** — a public GHCR image needs no auth. So the ardenone-cluster Deployment references `ghcr.io/jedarden/ytt:<tag>` and drops the `docker-hub-registry` pull secret for this app. (Dual-pushing to `ronaldraygun/ytt` on Docker Hub is **optional** and only if a private fallback is wanted — not required.)
- **Built by Argo, not GitHub Actions.** Even though GHCR is GitHub's registry, the build stays on Argo Workflows (GH Actions disabled fleet-wide); the `ytt-build` template pushes to GHCR using a `write:packages` token.
- **Tags:** immutable **semver** tags for public consumers (`ghcr.io/jedarden/ytt:0.1.0`), plus an optional moving `:latest` **for external convenience only** — the cluster manifest always pins a specific version, never `:latest`. Bump SOP: edit deps → CI runs unit tests + builds + pushes the tag → run integration suite in-cluster → `sed`-bump the pinned tag in `k8s/ardenone-cluster/ytt/` → commit → Argo syncs. **Rollback = `git revert` the bump commit** (never `kubectl`; selfHeal undoes live edits).
- **Image must be generic / portable** (it's for others too): **no ardenone-specifics baked in.** `mcp.ardenone.com`, the `whisper-openai` service DNS, the residential egress, the OAuth subjects, the cache backend — all come from env/manifest (`YTT_PUBLIC_URL`, `YTT_WHISPER_URL`, `YTT_PROXY_URL`, `YTT_ALLOWED_SUBJECTS`, `YTT_CACHE_*`). A self-hoster supplies their own Whisper endpoint (or runs `whisper-openai`), their own residential egress (or proxy), and their own OAuth — documented in `docs/usage/self-hosting.md`. Bundled `ffmpeg`/`yt-dlp` licenses (GPL/Unlicense) are compatible with redistribution (separate binaries, mere aggregation); the repo `LICENSE` covers ytt's own code.
- **Whisper dep:** `whisper-openai.whisper-stt.svc:8000` (ClusterIP, same cluster). NetworkPolicy allows egress to `whisper-openai` in ns `whisper-stt` (not the `whisper-stt` service).
- **Storage:** `longhorn` class (confirm name); PVC vs emptyDir per `YTT_CACHE_BACKEND`.
- **Resources:** set the first-cut requests/limits + ephemeral-storage from the Performance budget; confirm a node has room for the (light) ytt pod.
- **Secrets:** ESO `ExternalSecret` from OpenBao paths `ardenone-cluster/ytt/*`; documented rotation; never logged. **No cookies.** Required OpenBao keys (all under `ardenone-cluster/ytt/`):

  | Key | Maps to env | Notes |
  |---|---|---|
  | `jwt-signing-secret` | FastMCP token signing secret | Required; FastMCP's built-in AS uses this to sign self-issued tokens |
  | `oauth-client-id` | Static client registration | The single pre-registered client_id |
  | `oauth-client-secret` | Static client secret | Paired with client_id |
  | `allowed-subjects` | `YTT_ALLOWED_SUBJECTS` | Comma-separated `sub` values; sensitive (reveals who has access) |
  | `webshare-url` | `YTT_PROXY_URL` | Optional; contains Webshare credentials in the URL |

  Enumerate all five in the `ExternalSecret` manifest. Add rotation docs (jwt-signing-secret rotation requires restarting the pod; all existing tokens are invalidated).
- **Decommission (reverse of bootstrap):** remove the connector in Claude → remove any host-level WAF rule → delete `k8s/ardenone-cluster/ytt/` (Argo prunes the ytt app). Because ytt was **additive**, ibkr needs no restoration — its routes were never touched, and the shared `mcp.ardenone.com` DNS persists (ibkr also annotates it). Re-run the ibkr smoke check to confirm it's unaffected → delete the ytt OpenBao paths.

## Risks & posture

- **YouTube ToS / takedown** — bulk extraction violates ToS; personal + allowlisted + rate-limited is the mitigation.
- **Household collateral** — the residential IP is the user's home internet; a burn degrades the household. The Webshare proxy *protects* it, but **implement as fallback only** until the Open Question is resolved at Phase 10: `YTT_PROXY_URL` is unset by default; proxy is used only after an `ip_blocked` detection. The Phase 10 deploy checklist includes a human decision: set `YTT_PROXY_URL` as default in the manifest (protect the home IP proactively) or leave unset (rely on the residential egress advantage). See the Webshare Open Question.
- **yt-dlp breakage** — surfaced by the canary + `empty_body` metric; bump via SOP.
- **Single replica = no HA** — restart drops in-flight jobs and (emptyDir) the cache; accepted for v1. ytt is a read-only consumer of whisper-openai, so it rolls back independently of that dependency.

## Open Questions

- Should Webshare be the **default** (protect the home IP) rather than fallback? **(resolve before Phase 10.)**
- Calibrate `YTT_WHISPER_REALTIME_FACTOR` + confirm `YTT_WHISPER_MODEL` residency against the live service. **(Phase 6 blocks on this.)**
- Multi-client identity — cache is already global (one flat-file store per video_id, shared across subjects — resolved). Rate-limit scope — **resolved: cache hits do NOT consume the rate-limit bucket.** Only fetch and Whisper paths trigger the token bucket (cache hits cost nothing server-side; penalizing them would deter beneficial caching behavior).

## Resolved (was open)

- Self-host (yt-dlp), no third-party API. · Python + FastMCP. · Host on `ardenone-cluster` (residential, proven via self-test/canary). · Whisper = cluster `whisper-openai`. · Cache = flat files on PVC/emptyDir, LRU. · Integration tests in-cluster; unit anywhere. · Mobile OAuth path: none to build; register both callbacks. · AuthZ = subject allowlist (fail-closed) + DCR off + rate limits. · `replicas:1`. · `mode:summary` dropped → `start/end/query` slicing. · Inbound IP allowlist = Cloudflare **WAF** (not Access; not the pod). · **Public URL = `https://mcp.ardenone.com/ytt`** (path-based, co-hosted with ibkr-mcp; clone its manifests). · **ADR-001 = FastMCP self-issued, audience-bound to the path-bearing URL, DCR off.** · **ADR-002 = chunk-only v1.** · **ADR-003 = federate to self-hosted Authentik (not Google) via generic `OIDCProxy`; own client, not shared with ibkr-mcp.** · **Edge = existing shared tunnel + Traefik IngressRoute on `Host(mcp.ardenone.com)/ytt` (no sidecar, no new tunnel); `.well-known` handled by ADDITIVE higher-priority ytt rules — ibkr manifests untouched.** · **Do-no-harm to ibkr = hard constraint (additive routing, ClusterIP-scoped tests, before/after ibkr regression gate).** · **Secrets = ESO/OpenBao.** · **Image = immutable semver tags (not digests).** · **ffmpeg in image.** · **json3 rolling dedup required.** · **ETA/timeout/duration reconciled.** · **Public repo → image published to GHCR `ghcr.io/jedarden/ytt` (cluster pulls it directly, no secret); built by Argo (GH Actions stay off); Docker Hub dual-push optional.** · **Image is generic/portable — no ardenone specifics baked in.** · **In-repo public docs (README + `docs/usage/*` + LICENSE/CONTRIBUTING/SECURITY/CHANGELOG) are a deliverable.** · **Longhorn storage class name = `longhorn`** (confirmed from `k8s/ardenone-cluster/ibkr-mcp/` PVC in declarative-config — use directly, no need to verify at Phase 4/10). · **Rate-limit scope = fetch/Whisper only** (cache hits bypass the per-subject bucket — decided).
