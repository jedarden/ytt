# ytt — YouTube Transcript MCP Server

A remote [MCP](https://modelcontextprotocol.io/) server that reliably downloads
transcripts from pasted YouTube links, usable as a custom connector in Claude
mobile and Claude desktop.

All transcript fetching happens **inside the server** — no third-party transcript
APIs.  Captions are extracted with [yt-dlp](https://github.com/yt-dlp/yt-dlp)
(json3, with rolling-caption dedup).  If a video has no captions, a
[Whisper](https://github.com/openai/whisper)-compatible ASR service transcribes it.

## Quick start (self-hosted)

```bash
docker run --rm \
  -e YTT_PUBLIC_URL=https://your-domain.example.com/ytt \
  -e YTT_PATH_PREFIX=/ytt/ \
  -e YTT_ALLOWED_SUBJECTS=your-oauth-subject \
  -e YTT_OAUTH_CLIENT_ID=your-oauth-client-id \
  -e YTT_OAUTH_CLIENT_SECRET=your-oauth-client-secret \
  -e YTT_WHISPER_URL=http://your-whisper:8000 \
  -p 8080:8080 \
  ronaldraygun/ytt:0.2.21
```

The OAuth client pair and `YTT_PUBLIC_URL` are startup-required — the server
exits 1 without them, and `YTT_PUBLIC_URL` has **no fallback**: the OAuth
audience and the emitted RFC 9728 metadata derive from it, so a missing value
fails startup rather than silently targeting the reference deployment.
The upstream IdP defaults to the reference Authentik instance
(`sso.ardenone.com/application/o/ytt/`); set `YTT_OIDC_ISSUER` to point ytt
at your own OIDC provider instead — the discovery URL is derived from the
issuer (`YTT_OIDC_CONFIG_URL` overrides it). You need an OAuth2 client on
*your* IdP either way; see [docs/usage/self-hosting.md](docs/usage/self-hosting.md)
for the full recipe.

The server starts at `http://localhost:8080/ytt`.  Add it as a Claude connector
at `https://your-domain.example.com/ytt` (HTTPS required for Anthropic's backend).
If the image pull 401s, the Docker Hub repo's visibility flip is pending —
see `deploy/DEPLOY-CHECKLIST.md` §3 for the operator step and the one-line
anonymous-pull check.

See [docs/usage/self-hosting.md](docs/usage/self-hosting.md) for the full
self-hosting guide (BYO Whisper, BYO residential egress/proxy, BYO OAuth subjects).

## Tools

| Tool | Description |
|------|-------------|
| `get_youtube_transcript` | Fetch the transcript of a YouTube video by URL. Returns inline text for short videos; paginated chunks + `next_cursor` for long videos. Auto-starts Whisper ASR if no captions exist. |
| `get_transcript_job` | Poll a Whisper ASR job. When done, returns the transcript directly. |

Pass any YouTube URL form: `youtu.be/…`, `?v=`, `/shorts/`, `/live/`, bare 11-char ID — all normalize to the same cache entry.

## Requirements and caveats

| Requirement | Notes |
|-------------|-------|
| **Residential egress IP** | YouTube blocks datacenter IPs. Self-hosted on a home server or residential VPS works natively. For VPS/cloud, set `YTT_PROXY_URL` to a residential proxy (e.g. Webshare). |
| **Whisper endpoint** | Required only for videos without captions. Point `YTT_WHISPER_URL` at any OpenAI-compatible ASR service (`/v1/audio/transcriptions`). Run [whisper-openai](https://github.com/stpb/whisper-openai) locally, or skip and accept `no_captions_asr_failed` for caption-less videos. |
| **Single replica** | In-process state (LRU cache, single-flight, Whisper job registry). Scale-out requires a redesign. |
| **Auth required** | OAuth 2.1 with a subject allowlist. Empty allowlist = deny all. |

Verify the egress assumption from wherever the server runs:

```bash
ytt canary --once    # fetches captions for one known-good video; JSON report,
                     # verdict "ok" vs "ip_blocked", exit 0/1
```

After changing the image or the egress config, run the acceptance gate
instead — it runs the direct probe **and** `--via-proxy` when `YTT_PROXY_URL`
is set, requires `outcome=ok` on both, retains the JSON evidence, and prints
the rollback/escalation directive on failure:

```bash
ytt canary --gate    # release gate; exit 0 only on a full pass
```

In Kubernetes the gate runs *inside* the server pod, so it needs a kubeconfig
granting `pods/exec` on the namespace — the credential-free read-only
`kubectl` proxy cannot exec (`auth can-i create pods/exec` → `no`; the gate
is an operator step).  The exact command, the read-only evidence that can be
collected without exec, and the decision table: `deploy/RUNBOOK.md` §3 and
§3.1.

## Configuration

All config is environment-variable-based. Nothing ardenone-specific is
*required*, but a few baked-in defaults (`YTT_WHISPER_URL` and
`YTT_OIDC_ISSUER`) point at the reference deployment — set your own.
`YTT_PUBLIC_URL` deliberately has **no** default (see its row below).

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_PUBLIC_URL` | *(required — no fallback)* | Public base URL — OAuth audience + emitted metadata derive from this. Set to your domain. Startup exits 1 if unset or empty, and validates shape when set: http(s) scheme, hostname present, no whitespace/query/fragment (a trailing slash is normalized away). |
| `YTT_PATH_PREFIX` | `/ytt/` | Path the server is mounted under. Must end with `/`. |
| `YTT_ALLOWED_SUBJECTS` | *(empty = deny all)* | Comma-separated OAuth `sub` values allowed to call tools. See [connector.md](docs/usage/connector.md) for how to discover your `sub`. |
| `YTT_RATE_LIMIT_PER_MIN` | `20` | Per-subject fetch rate — token-bucket **refill rate** in requests/minute (1 token every `60/rate` seconds; 3 s at the default). Charged only on the cache-miss fetch path, failed fetches included; cache hits and `get_transcript_job` polls are free. `0` = deny all fetches (fail-closed). |
| `YTT_RATE_LIMIT_BURST` | *(= rate)* | Per-subject token-bucket **capacity** — fetches a subject may make at once before the per-minute refill throttles (the bucket starts full). Unset, it resolves to `YTT_RATE_LIMIT_PER_MIN`. Explicit `0` is valid with any rate (denies every fetch); an explicit positive burst under a `0` rate is rejected at startup — it would contradict the documented deny-all. |
| `YTT_WHISPER_JOBS_PER_HOUR` | `10` | Per-subject quota of *new* Whisper ASR jobs per rolling hour — a token bucket that starts full and refills at `jobs/3600` per second (up to 10 jobs at once, then 1 new job every 6 min sustained at the default). Joining or polling an in-flight job is free. `0` = deny all new ASR jobs; caption fetches still work (fail-closed). |
| `YTT_MAX_CONCURRENT_WHISPER` | `1` | Whisper jobs running at once across **all** subjects (protects the shared ASR service). Jobs beyond the cap queue as `pending` — their quota slot is already paid — and start when a running job reaches `done` or `error`. |
| `YTT_MAX_PENDING_WHISPER_JOBS` | `16` | ASR **backlog** cap across all subjects: pending + running jobs. A caption-less request that would start a *new* job past the cap is denied `rate_limited` ("Whisper queue full") without spending a quota slot; joining an in-flight job stays free. `0` = deny all new ASR jobs (fail-closed). |
| `YTT_OAUTH_CLIENT_ID` | *(required)* | OAuth2 client ID of the `ytt` application on the upstream IdP. Startup exits 1 if unset. |
| `YTT_OAUTH_CLIENT_SECRET` | *(required)* | OAuth2 client secret of the same application. Inject by reference, never in a manifest or log. |
| `YTT_OIDC_ISSUER` | *(reference Authentik)* | Issuer URL of the upstream OIDC provider — matched byte-for-byte against the id token's `iss` claim, so set it to exactly what your IdP advertises. Validated at startup (https, no whitespace/query/fragment). |
| `YTT_OIDC_CONFIG_URL` | *(derived from the issuer)* | Discovery-document URL; defaults to `<issuer>/.well-known/openid-configuration`. Set only for a non-standard path. |
| `YTT_WHISPER_URL` | *(reference in-cluster Whisper)* | OpenAI-compatible ASR endpoint. Required for caption-less videos. |
| `YTT_WHISPER_MODEL` | `large-v3-turbo` | Model name served by the Whisper endpoint. Auto-corrects via `/v1/models`. |
| `YTT_CACHE_DIR` | `/cache` | Transcript cache directory. |
| `YTT_CACHE_MAX_BYTES` | `2Gi` | Max cache size. Must be ≤ the volume size. |
| `YTT_SCRATCH_DIR` | `/scratch` | Scratch directory for temporary Whisper audio. Must be a dedicated volume (emptyDir recommended) — see the warning below. |
| `YTT_PROXY_URL` | *(unset)* | Optional residential proxy URL (e.g. `http://user:pass@proxy.example.com:port`). |
| `YTT_CANARY_INTERVAL_SEC` | `600` | Seconds between canary probe-loop cycles. Consumed by the canary Deployment only — the main server never probes. The loop reuses `YTT_PROXY_URL`: a `via_proxy` path is probed only while a proxy is set. |

Per-subject limits apply **after** the allowlist: `YTT_ALLOWED_SUBJECTS`
decides who may call at all, and the limiters then bound what each allowlisted
subject can spend — keyed on the OAuth `email` claim, one bucket per mailbox
(`@domain` allowlist entries match many addresses but each still gets its own
bucket). When a limit is exhausted the tool returns `status="error"` with
`error_code="rate_limited"` and a "Try again in ~Ns." hint; denials increment
`ytt_rate_limited_total{subject_hash}` on `/metrics` (subjects are hashed,
never exported in clear). There is no "unlimited" setting: malformed,
negative, or self-contradictory limit values exit 1 at startup, and a limiter
that cannot compute an answer denies rather than admits (fail-closed at every
layer).

> **⚠️ `YTT_SCRATCH_DIR` is swept on every boot.** At startup ytt deletes
> **every file** in the scratch directory, unconditionally — safe only because
> the single replica is guaranteed to be the only runner. Point it at a
> directory ytt owns alone (a dedicated emptyDir in Kubernetes, or an
> otherwise-empty directory), never at a shared path like `/tmp`. Rationale:
> [docs/notes/single-replica.md](docs/notes/single-replica.md).

Two canary knobs are compile-time constants in `ytt/canary.py`, not
environment variables: the probe ladder (`CANARY_VIDEO_IDS` — `jNQXAC9IVRw`
then `dQw4w9WgXcQ`, probed in order per cycle until one succeeds) and the
canary's dedicated metrics port :8081 (the server's own `/metrics` stays on
:8080). Changing either is a code change, not config; the `ytt_canary_*`
series they feed and their alerts:
[deploy/CANARY-MONITORING-RUNBOOK.md](deploy/CANARY-MONITORING-RUNBOOK.md).

Full reference: [docs/usage/configuration.md](docs/usage/configuration.md)

## Add as a Claude connector

1. Open Claude Desktop → Settings → Connectors → Add MCP Server.
2. Enter the URL: `https://your-domain.example.com/ytt`
3. Complete the OAuth flow.
4. Run `ytt selftest --show-sub` to discover your `sub`.
5. Set `YTT_ALLOWED_SUBJECTS=<your-sub>` and restart.

Mobile reuses the web/Desktop OAuth token — no separate mobile setup needed.

Full guide: [docs/usage/connector.md](docs/usage/connector.md)

## Architecture

```
Claude (mobile / desktop / web)
  │  Streamable HTTP + OAuth 2.1
  ▼
ytt MCP server (uvicorn, 1 worker)
  ├─ /health (liveness, unauth)
  ├─ /.well-known/oauth-* (path-inserted RFC 9728 metadata)
  ├─ AuthN: OAuth bearer (audience-bound to YTT_PUBLIC_URL)
  ├─ AuthZ: subject allowlist (YTT_ALLOWED_SUBJECTS)
  ├─ Limits: per-subject rate bucket + Whisper quota
  ├─ Single-flight: one yt-dlp call per video per in-flight window
  ├─ LRU cache: flat files, byte-cap, whole-unit eviction
  ▼
  yt-dlp caption fetch (json3, rolling-caption dedup)
    └─ no captions? → Whisper ASR (via YTT_WHISPER_URL)
```

## License

MIT — see [LICENSE](LICENSE).  Bundled third-party software: see [NOTICE](NOTICE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).  Security issues: [SECURITY.md](SECURITY.md).

## ardenone-cluster deployment

The reference deployment co-hosts ytt with `ibkr-mcp` on `mcp.ardenone.com`.
See [docs/usage/deploy-ardenone.md](docs/usage/deploy-ardenone.md) and
[deploy/](deploy/) for the manifests.
