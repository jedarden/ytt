# ytt — YouTube Transcript MCP Server

A remote [MCP](https://modelcontextprotocol.io/) server that reliably downloads
transcripts from pasted YouTube links, usable as a custom connector in Claude
mobile and Claude desktop.

All transcript retrieval is orchestrated **inside the server** — no
third-party transcript APIs are used for YouTube. ytt does not call managed
transcript providers. Captions are extracted with
[yt-dlp](https://github.com/yt-dlp/yt-dlp) (json3, with rolling-caption dedup).
If a video has no captions, ytt downloads its audio and sends it to a
Whisper-compatible ASR service: the project-operated reference endpoint by
default, or the endpoint selected with `YTT_WHISPER_URL`. That default is
network egress; it is not a model bundled in this image. See the
[reference-ASR contract](docs/notes/reference-asr.md).

## Quick start (self-hosted)

```bash
docker run --rm \
  -e YTT_PUBLIC_URL=https://your-domain.example.com/ytt \
  -e YTT_PATH_PREFIX=/ytt/ \
  -e YTT_ALLOWED_SUBJECTS=your-oauth-subject \
  -e YTT_OAUTH_CLIENT_ID=your-oauth-client-id \
  -e YTT_OAUTH_CLIENT_SECRET=your-oauth-client-secret \
  -p 8080:8080 \
  ghcr.io/jedarden/ytt:0.2.26
```

`YTT_WHISPER_URL` is optional for captioned videos, so the quick start above
leaves it out. Leaving it unset selects the project-operated reference service
at `http://whisper-openai.whisper-stt.svc.cluster.local:8000` when a
caption-less video needs ASR. This sends downloaded caller audio to that
network endpoint; it is not local to the `ytt` container and may not be
reachable from a generic self-hosted cluster. Set `YTT_WHISPER_URL` to an
endpoint you operate or trust, or set it to an empty value for explicit
caption-only operation. The first caption-less call still returns
`status="pending"`; a reference-service outage or rejection is reported by
`get_transcript_job` as `status="error"`, `error_code="asr_failed"`.
`no_captions_asr_failed` is a metrics-only label, not a tool error code. The
full endpoint, quota, proxy, ownership, retention, and egress contract is in
the [reference-ASR note](docs/notes/reference-asr.md).

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
The image is published to GHCR as a public package.  If the pull 401s, that
package's one-time visibility flip is still pending — see
`deploy/DEPLOY-CHECKLIST.md` §3 for the operator step and the one-line
anonymous-pull check.

See [docs/usage/self-hosting.md](docs/usage/self-hosting.md) for the full
self-hosting guide (BYO Whisper, BYO residential egress/proxy, BYO OAuth subjects).

## Tools

| Tool | Description |
|------|-------------|
| `get_youtube_transcript` | Fetch the transcript of a YouTube video by URL. Returns inline text for short videos; paginated chunks + `next_cursor` for long videos. Auto-starts Whisper ASR if no captions exist. |
| `get_transcript_job` | Poll a Whisper ASR job. When done, returns the same bounded transcript shape: short results are inline; long results are `status="partial"` chunks with `next_cursor`. Pass that cursor back as `cursor` with the same `video_id` until `is_final=true`. |

Pass any YouTube URL form: `youtu.be/…`, `?v=`, `/shorts/`, `/live/`, bare 11-char ID — all normalize to the same cache entry.

## Requirements and caveats

| Requirement | Notes |
|-------------|-------|
| **Residential egress IP** | YouTube blocks datacenter IPs. Self-hosted on a home server or residential VPS works natively. For VPS/cloud, set `YTT_PROXY_URL` to a residential proxy (e.g. Webshare). |
| **Whisper endpoint** | Optional for captioned videos. Unset selects the project-operated reference endpoint and sends caption-less audio there; set `YTT_WHISPER_URL` to an operator-selected OpenAI-compatible service (`/v1/audio/transcriptions`) or to an empty value for explicit caption-only mode. The first ASR call is `pending` + poll; endpoint outage/rejection ends the job as `asr_failed`. |
| **Single replica** | In-process state (LRU cache, single-flight, Whisper job registry). Scale-out requires a redesign. |
| **Auth required** | OAuth 2.1 with a subject allowlist. Empty allowlist = deny all. |

Verify the egress assumption from wherever the server runs:

```bash
ytt canary --once    # fetches captions for one known-good video; JSON report,
                     # verdict "ok" or a stable ytt.errors error code
                     # (ip_blocked, empty_body, rate_limited, …), exit 0 iff "ok"
```

After changing the image or the egress config, run the acceptance gate
instead — it runs the direct probe **and** `--via-proxy` when `YTT_PROXY_URL`
is set, requires `outcome=ok` on both, retains the JSON evidence
(`report.evidence_file` — pod-local by default; what survives where is the
durability contract in `docs/notes/canary-gate-evidence.md` §4), and prints
the rollback/escalation directive on failure:

```bash
ytt canary --gate    # release gate; exit 0 only on a full pass
```

The full `ytt canary` flag surface (drift-guarded by
`tests/unit/test_canary_flag_docs.py`):

| Flag | Default | Effect |
|------|---------|--------|
| `--once` | off | One-shot probe (above); mutually exclusive with `--gate`. |
| `--gate` | off | Post-deploy acceptance gate (above); mutually exclusive with `--once`. |
| `--video-id <ID>` | first `CANARY_VIDEO_IDS` entry | Probe target override; valid with `--once` or `--gate` (both gate probes fetch it). |
| `--via-proxy` | off | Valid with `--once` only: dial the probe through `YTT_PROXY_URL` — the end-to-end check that the configured proxy actually carries YouTube traffic ([docs/notes/proxy-egress.md](docs/notes/proxy-egress.md)). The gate runs its own proxy leg when `YTT_PROXY_URL` is set, so passing `--via-proxy` to it is a usage error. |
| `--evidence-dir <DIR>` | `/tmp/ytt-canary-evidence` | Valid with `--gate` only: directory for the JSON evidence artifact (what survives where: `docs/notes/canary-gate-evidence.md` §4). |

With no flag at all, `ytt canary` is the long-running probe loop the canary
Deployment runs (cadence `YTT_CANARY_INTERVAL_SEC`). Invalid flag/mode
combinations are argparse usage rejections (exit `2`); the full exit-code
contract is `docs/notes/canary-gate-evidence.md`.

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
| `YTT_PATH_PREFIX` | `/ytt/` | Path the server is mounted under. Must start and end with `/`. Unset resolves to this default; an explicitly empty value is a startup error, not a root mount. Startup exits 1 on a missing slash — the join it feeds is plain concatenation, so a bad prefix would silently misroute every route. Pinned by `tests/unit/test_path_prefix_contract.py`. |
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
| `YTT_WHISPER_URL` | *(reference in-cluster Whisper)* | Base URL of the project-operated reference ASR endpoint when unset. Caption-less audio is sent there by default; override it with an operator-selected OpenAI-compatible service, or set it empty for explicit caption-only mode. See the [reference-ASR contract](docs/notes/reference-asr.md). |
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
environment variables: the fixed video set (`CANARY_VIDEO_IDS` —
`jNQXAC9IVRw` then `dQw4w9WgXcQ`, every entry probed each cycle — a
coverage set, not a stop-at-first-success ladder: a caption regression
confined to the second video stays visible in `ytt_canary_probes_total`
even while the first keeps succeeding) and the
canary's dedicated metrics port :8081 (the server's own `/metrics` stays on
:8080). Changing either is a code change, not config; the `ytt_canary_*`
series they feed and their alerts:
[deploy/CANARY-MONITORING-RUNBOOK.md](deploy/CANARY-MONITORING-RUNBOOK.md).

### Startup egress probe and `ytt_egress_is_residential`

Before the HTTP listener binds, `ytt serve` runs **one** egress probe: an
HTTP GET of `https://ipinfo.io/json` with a hard 10 s timeout
(`ytt.selftest._PROBE_TIMEOUT_SEC`, a compile-time constant, not an
environment variable), dialed **through `YTT_PROXY_URL` when set** — so the
classified IP is the effective fetch path's egress, not the pod's native
one. The verdict is exported as the label-free gauge
`ytt_egress_is_residential` on `/ytt/metrics`: `1` = residential, `0` = not
classified residential (datacenter, or a probe that produced no answer at
all). The probe is one-shot per process — it does not refresh on a timer;
the classification is re-probed only by an authenticated
`GET /ytt/admin/egress` call or by a restart. An unreachable or failing
probe is **not** fatal: the server logs `Startup egress probe failed` and
finishes booting with the gauge at `0`. Because the gauge is registered at
import time, every scrape carries exactly one `ytt_egress_is_residential`
series valued 0 or 1 — never absent — so a missing series can only mean a
scrape or routing problem, never "the probe has not run yet". Pinning
tests: [tests/unit/test_startup_egress_probe.py](tests/unit/test_startup_egress_probe.py).

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
