# Configuration Reference

All ytt configuration is via environment variables.  `YTT_PUBLIC_URL` and the
OAuth client pair (`YTT_OAUTH_CLIENT_ID`, `YTT_OAUTH_CLIENT_SECRET`) are
required — the server exits 1 without the client pair, and the baked-in
`YTT_PUBLIC_URL` default points at the reference deployment.  Every other
variable has a working default.

## Required variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_PUBLIC_URL` | *(required)* | The public base URL of the server. Used as the OAuth resource/audience and in emitted metadata. Must not have a trailing slash. **Set this to your own domain** before exposing the server. |
| `YTT_PATH_PREFIX` | `/ytt/` | The path prefix the server is mounted under. Must end with `/`. Startup exits 1 if the slash is missing. Must match the IngressRoute / reverse-proxy config. |

## OAuth provider (upstream IdP)

ytt federates authentication to an upstream OIDC provider (decision and
threat model: [../notes/auth.md](../notes/auth.md)); it never falls back to
an unauthenticated mode.  **The upstream issuer is currently hardcoded** to
the reference Authentik instance — `https://sso.ardenone.com/application/o/ytt/`
(`AUTHENTIK_ISSUER` / `AUTHENTIK_OIDC_CONFIG_URL` in `ytt/auth.py`) — so the
OAuth2 client must exist there; pointing ytt at your own IdP is a code
change, not a config change.

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_OAUTH_CLIENT_ID` | *(required)* | Client ID of the `ytt` OAuth2 application on the upstream IdP. Startup exits 1 if unset (`ytt/auth.py::build_auth_provider`). |
| `YTT_OAUTH_CLIENT_SECRET` | *(required)* | Client secret of the same application. Doubles as the HS256 key that verifies upstream `id_token`s and as the HKDF seed for the signing key of ytt's own tokens. A secret — inject by reference (the reference deployment provisions it via ExternalSecret from OpenBao); never commit, log, or inline it. |
| `YTT_JWT_SIGNING_SECRET` | *(unset)* | Optional explicit signing key for the tokens ytt issues to clients. Unset, it is derived from `YTT_OAUTH_CLIENT_SECRET` (stable across restarts). Set only to rotate ytt's token key independently of the upstream client secret. |

## Authorization

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_ALLOWED_SUBJECTS` | *(empty = deny all)* | Comma-separated OAuth `sub` values (e.g. `user@example.com,abc123`). Empty list denies all requests. Discover your `sub` via `ytt selftest --show-sub` after the first OAuth flow. |
| `YTT_RATE_LIMIT_PER_MIN` | `20` | Per-subject fetch rate limit — token-bucket **refill rate** in requests/minute. Only cache-miss fetches consume it (see below). `0` = deny every fetch for every subject. |
| `YTT_RATE_LIMIT_BURST` | *(= rate)* | Per-subject token-bucket **capacity** — how many fetches a subject may make at once before the per-minute refill throttles. Unset, it resolves to `YTT_RATE_LIMIT_PER_MIN` (a full minute's worth of requests up front). |
| `YTT_WHISPER_JOBS_PER_HOUR` | `10` | Per-subject quota for **new** Whisper ASR jobs per rolling hour (token bucket refilling at `jobs/3600` per second). `0` = deny every new ASR job; caption fetches still work. |

Per-subject limits (rationale: [../notes/auth.md](../notes/auth.md) — "even an
allowlisted caller can't exhaust the home IP / shared Whisper service"):

- **What costs a token:** only the cache-miss fetch path of
  `get_youtube_transcript` (failed fetches included — the limit guards
  yt-dlp/egress effort, not successful responses). **Cache hits and
  `get_transcript_job` polls cost nothing**, so waiting on one transcription
  never drains a caller's budget.
- **What costs a Whisper slot:** only *starting* a new ASR job. Joining an
  already-running job for the same video, or polling it, is free. A slot
  charged for a job that never actually starts (the registry failed after the
  charge) is refunded.
- **In-flight cap:** independent of the per-subject quota, at most
  `YTT_MAX_CONCURRENT_WHISPER` jobs run at once (the shared CPU service is
  protected from every subject combined). Jobs beyond the cap queue as
  `pending` — they have already paid their quota slot — and start when a
  running job reaches `done` or `error`, which releases its slot either way.
- **Fail-closed:** `0` is valid and denies everything the limit guards; there
  is no "unlimited" setting. Negative values fail startup validation.
- **On denial** the tool returns `status="error"`, `error_code="rate_limited"`
  with a retry hint in the message, and `ytt_rate_limited_total{subject_hash}`
  increments on `/metrics` (subjects are exported only as an 8-char sha256
  prefix, never in clear).
- Limits are enforced **per OAuth subject** (`email` claim) and held in
  process memory — correct only under the single-replica invariant
  (`replicas: 1`), like the cache and job registry.

## Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_CACHE_BACKEND` | `pvc` | `pvc` (use a PersistentVolumeClaim) or `emptydir` (ephemeral, lost on pod restart). |
| `YTT_CACHE_DIR` | `/cache` | Directory for the transcript cache. Mount a volume here. |
| `YTT_CACHE_MAX_BYTES` | `2Gi` | Maximum cache size in bytes. Accepts human-readable forms: `2Gi`, `500Mi`, `1024`. Must be ≤ the volume size (validated at startup for PVC; warned for emptyDir). |
| `YTT_CACHE_RECONCILE_SEC` | `300` | Interval (seconds) to reconcile the in-memory byte counter against disk. |

## Scratch volume (Whisper audio)

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_SCRATCH_DIR` | `/scratch` | Directory for temporary audio files during Whisper transcription. Use a separate volume from the cache (emptyDir recommended). **Startup sweep:** every file in this directory is deleted unconditionally on every boot (`ytt/whisper.py::startup_sweep`) — dedicate the directory to ytt alone and never point it at a shared path. |
| `YTT_MAX_AUDIO_BYTES` | `500Mi` | Maximum audio file size per Whisper job. |

## Concurrency

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_MAX_CONCURRENT_FETCHES` | `4` | Maximum simultaneous yt-dlp caption fetches. |
| `YTT_MAX_CONCURRENT_WHISPER` | `1` | Maximum simultaneous Whisper jobs (shared CPU service). |
| `YTT_EXTRACT_TIMEOUT_SEC` | `60` | Timeout for `yt-dlp extract_info` calls. On expiry, the single-flight Future resolves as `rate_limited`. |

## Whisper ASR

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_WHISPER_URL` | `http://whisper-openai.whisper-stt.svc.cluster.local:8000` | Base URL of the OpenAI-compatible Whisper endpoint (must serve `/v1/audio/transcriptions` and `/v1/models`). Required for caption-less videos. The baked-in default points at the reference deployment's in-cluster Whisper — set your own endpoint, or an unreachable address to disable ASR entirely. |
| `YTT_WHISPER_MODEL` | `large-v3-turbo` | Model name to use for ASR (the only model the reference whisper-openai service serves). Must be pre-loaded by the Whisper service. Self-corrects via `/v1/models` if the configured model is absent. |
| `YTT_WHISPER_REALTIME_FACTOR` | `2.0` | ETA multiplier: `ETA_sec = duration_sec × factor`. CPU-calibrated for `large-v3-turbo`; calibrate against your own Whisper service. |
| `YTT_WHISPER_TIMEOUT_SEC` | `2880` | HTTP timeout for Whisper requests. Must exceed `YTT_MAX_ASR_DURATION_SEC × YTT_WHISPER_REALTIME_FACTOR` (startup-validated, Invariant 7). Default: `1200 × 2.0 = 2400 < 2880`, a 1.2× margin. |
| `YTT_MAX_ASR_DURATION_SEC` | `1200` | Maximum video duration for Whisper ASR (20 min). Longer videos return `too_long_for_asr`. |
| `YTT_JOB_TTL_SEC` | `3600` | Time-to-live for completed/errored Whisper jobs in the in-memory registry. |

## Pagination

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_INLINE_CHAR_LIMIT` | `18000` | Max characters for inline (non-paginated) responses. Non-Latin text uses `bytes / 3` for the token budget. |
| `YTT_CHUNK_CHARS` | `18000` | Character size per chunk in paginated (`mode=chunk`) responses. |

## Proxy

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_PROXY_URL` | *(unset)* | Optional residential proxy URL for yt-dlp (e.g. `http://user:pass@proxy.example.com:port`). Used as a fallback when the direct IP is blocked. Note: most commercial proxies use datacenter IPs and may not help. |

## Canary (canary Deployment only)

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_CANARY_INTERVAL_SEC` | `600` | Seconds between canary probe runs. Consumed by the canary Deployment, not the main server. |

The long-running probe loop (`ytt canary`) serves `ytt_canary_*` metrics on
:8081. For a one-off egress check there is also `ytt canary --once`: it
fetches captions for one known-good video from wherever it runs, prints a
JSON report (`verdict`: `ok` vs `ip_blocked`, plus the ipinfo egress
classification as context), and exits 0/1 — usable from an in-cluster
`kubectl exec`, a debug pod, or a self-host smoke test without deploying
anything.

## Size format

All size variables (`YTT_CACHE_MAX_BYTES`, `YTT_MAX_AUDIO_BYTES`) accept:
- Human-readable: `2Gi`, `500Mi`, `100Ki`, `1024`
- Plain integers: `2147483648`

Units: `Ki = 1024`, `Mi = 1024²`, `Gi = 1024³`.
