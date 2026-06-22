# Configuration Reference

All ytt configuration is via environment variables.  All variables have defaults
except `YTT_PUBLIC_URL` (which you must set to your public-facing URL).

## Required variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_PUBLIC_URL` | `https://mcp.ardenone.com/ytt` | The public base URL of the server. Used as the OAuth resource/audience and in emitted metadata. Must not have a trailing slash. **Change this to your own domain** before exposing the server. |
| `YTT_PATH_PREFIX` | `/ytt/` | The path prefix the server is mounted under. Must end with `/`. Startup exits 1 if the slash is missing. Must match the IngressRoute / reverse-proxy config. |

## Authorization

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_ALLOWED_SUBJECTS` | *(empty = deny all)* | Comma-separated OAuth `sub` values (e.g. `user@example.com,abc123`). Empty list denies all requests. Discover your `sub` via `ytt selftest --show-sub` after the first OAuth flow. |
| `YTT_RATE_LIMIT_PER_MIN` | `20` | Per-subject request rate limit (token bucket). |
| `YTT_WHISPER_JOBS_PER_HOUR` | `10` | Per-subject Whisper ASR job quota per hour. |

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
| `YTT_SCRATCH_DIR` | `/scratch` | Directory for temporary audio files during Whisper transcription. Use a separate volume from the cache (emptyDir recommended). |
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
| `YTT_WHISPER_URL` | `http://whisper-openai.whisper-stt.svc.cluster.local:8000` | Base URL of the OpenAI-compatible Whisper endpoint (must serve `/v1/audio/transcriptions` and `/v1/models`). |
| `YTT_WHISPER_MODEL` | `Systran/faster-whisper-small` | Model name to use for ASR. Must be pre-loaded by the Whisper service. Self-corrects via `/v1/models` if the configured model is absent. |
| `YTT_WHISPER_REALTIME_FACTOR` | `1.2` | ETA multiplier: `ETA_sec = duration_sec × factor`. Calibrate against your Whisper service. |
| `YTT_WHISPER_TIMEOUT_SEC` | `2880` | HTTP timeout for Whisper requests. Must exceed `YTT_MAX_ASR_DURATION_SEC × YTT_WHISPER_REALTIME_FACTOR` (startup-validated). Default provides a 2× margin: `1200 × 1.2 × 2.0 = 2880`. |
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

## Size format

All size variables (`YTT_CACHE_MAX_BYTES`, `YTT_MAX_AUDIO_BYTES`) accept:
- Human-readable: `2Gi`, `500Mi`, `100Ki`, `1024`
- Plain integers: `2147483648`

Units: `Ki = 1024`, `Mi = 1024²`, `Gi = 1024³`.
