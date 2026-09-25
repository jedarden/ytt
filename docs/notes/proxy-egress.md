# Proxy egress — the `YTT_PROXY_URL` contract

ytt runs on datacenter IPs, which YouTube blocks (`ip_blocked`). `YTT_PROXY_URL`
names a residential HTTP proxy (e.g. Webshare) that serves as the **fallback
egress path**. This note is the single spec for which traffic dials through it,
how the value is validated, how its credentials are kept out of logs, and what
happens when the proxy itself fails.

Environment reference: `README.md` (Configuration table) and
`docs/usage/configuration.md` (`YTT_PROXY_URL` row).

## Which requests use the proxy

| Traffic | Proxy usage | Why |
|---|---|---|
| Caption extraction (`ytt.fetch._do_fetch`: `extract_info` + json3 `urlopen`) | **direct first; one proxied retry on `ip_blocked`** | The datacenter IP usually works (player-client pin, `docs/notes/yt-dlp-player-client.md`); the proxy is the fallback, and dialing it on every request wastes residential bandwidth and incurs per-GB cost. |
| Whisper audio download (`ytt.whisper._do_download_audio`) | **direct first; one proxied retry on `ip_blocked`** | Plan §ip_blocked: "the proxy retry applies equally to caption fetches and Whisper audio downloads." Both paths share `ytt.fetch.run_with_proxy_retry`. |
| Egress probe (`ytt.selftest.probe_egress` — startup log, `GET /admin/egress`, canary egress half) | **always through the proxy when set** | Deliberate: the probe classifies the *effective fallback path's* egress (is the proxy's IP residential?), not the native one. `ytt selftest` probes direct for that. |
| `ytt canary --once --via-proxy` | **the caption probe dials through the proxy** | The end-to-end check that the configured proxy actually carries YouTube traffic (run in-cluster; see "Verifying in-cluster"). The default `ytt canary --once` probes direct, matching the caption path. |
| Whisper ASR POST (`/v1/audio/transcriptions`) | **never proxied** | The Whisper service is on the tailnet; proxying an internal call would leak it to a third party and break. |
| OAuth / OIDC discovery, JWKS, token calls | **never proxied** | Identity traffic must not traverse a third-party proxy. |

The proxy value is threaded as yt-dlp's `proxy` option (per-`YoutubeDL`
construction), never via process environment (`HTTP_PROXY` etc.) — that would
sweep the ASR POST and OAuth traffic into the proxy.

## URL validation (`ytt.config.Settings._proxy_url_valid`)

Validated at `Settings` construction — a typo'd URL must fail startup, not sit
unexercised until the first `ip_blocked` retry fails opaquely.

- **Unset (`None`)** is valid — direct egress, the default.
- **Empty / whitespace-only is an error**, not a silent unset. Fail-closed: a
  manifest interpolating a missing secret (`YTT_PROXY_URL: "${PROXY_URL}"`)
  must not quietly disable the proxy. The error message says to *unset the
  variable entirely* for direct egress.
- **Scheme must be `http://` or `https://`.** yt-dlp would also dial
  `socks4/4a/5/5h`, but the httpx-based egress probe has no SOCKS adapter — a
  SOCKS URL would half-work and half-break. Most residential providers
  (Webshare et al.) serve plain HTTP endpoints; a SOCKS requirement is a
  startup error by design.
- **A hostname is required.**
- **Whitespace anywhere is rejected** — copy-paste line wraps are the classic
  way a proxy URL gets split inside a manifest.
- Credentials (`user:pass@`) are optional and never validated — and never
  logged (below).

## Credential redaction

The proxy URL typically carries `user:pass@`. Two layers keep it out of
observability (plan §Observability):

1. **Structured fields** — the structlog processor in `ytt.observability`
   redacts any field value containing a credential-bearing URL (and drops
   known-sensitive field names like `proxy_url` itself).
2. **Free text** — upstream exception strings (yt-dlp `DownloadError` /
   `ExtractorError`, httpx connect errors) can quote the configured proxy
   verbatim. Every boundary that turns such an exception into a relayable
   message or log argument runs it through
   `ytt.observability.redact_credentials()` first:
   - `ytt.fetch._do_fetch` (Download/Extractor errors),
   - `ytt.whisper._do_download_audio` (same),
   - `ytt.canary.probe_once_detail` (probe error report) and
     `run_once`'s egress error,
   - the `GET /admin/egress` 502 body in `ytt.server`.

   Redaction strips the userinfo (`http://alice:s3cret@proxy:3128` →
   `http://proxy:3128`); host and port stay for diagnosability. Unit-tested in
   `tests/unit/test_proxy.py` (`TestRedactCredentials`,
   `TestErrorRedactionBoundaries`, and the bead-`ytt-31ec1026` regression
   classes: `TestCaptionRetryPathCredentialRedaction`,
   `TestWhisperAudioPathCredentialRedaction`, `TestHttpxFailureRedaction`,
   `TestStructuredLogRedaction` — the last proves layer 1 against rendered
   JSON log lines; the caption and Whisper classes pin both retry legs of
   layer 2).

   Layer 1 and layer 2 share one sanitizer: the structlog processor applies
   `redact_credentials()` to any string field value containing a
   credential-bearing URL, so a log argument quoting the proxy verbatim
   (e.g. `error=str(exc)` on the startup egress probe) renders clean even
   where the call site has no explicit redaction.

## Failure behavior (`ytt.fetch.run_with_proxy_retry`)

Each attempt (direct and proxied) is bounded by the path's own timeout
(`YTT_EXTRACT_TIMEOUT_SEC` for captions, `YTT_WHISPER_TIMEOUT_SEC` for the
audio download). Exactly one proxied retry — never a loop.

| Outcome | Result |
|---|---|
| direct attempt succeeds | returned; the proxy is never touched |
| direct attempt times out | `timeout_code` (`rate_limited` for captions, `asr_failed` for audio) — "…timed out after Ns…" |
| direct `ip_blocked`, proxy set | exactly one attempt through the proxy |
| proxied retry succeeds | returned |
| proxied retry times out | `timeout_code` + "(proxy retry also timed out)" |
| proxied retry fails otherwise | the retry's own `error_code`, message suffixed "(proxy retry also failed)" |
| `ip_blocked` with no proxy configured | raised unchanged, no retry |
| any non-`ip_blocked` error (private, region-locked, …) | raised unchanged — a proxy cannot help |

## Verifying in-cluster

The unit suite proves the wiring with mocks. Proving the configured proxy
actually carries YouTube traffic requires egress — two vehicles, both
in-cluster only (datacenter IPs outside the cluster are blocked by design):

1. **`ytt canary --once --via-proxy`** — one-shot JSON report; `verdict: "ok"`
   proves a caption fetch succeeded *through* the proxy, and the `egress` half
   classifies the proxy's exit IP. Runnable via `kubectl exec` against the
   server Deployment or any one-shot pod/Argo step. In practice you rarely
   invoke it directly: **`ytt canary --gate`** (the post-deploy release gate,
   `deploy/RUNBOOK.md` §3) runs this probe *and* the direct one whenever
   `YTT_PROXY_URL` is configured, requires `outcome=ok` on both, retains the
   JSON evidence, and emits the rollback/escalation directive on failure.
2. **`tests/integration/test_proxy_live.py`** — pytest integration suite
   (`-m integration`): asserts `/admin/egress` reports `via_proxy: true` with a
   residential classification, and runs the canary via-proxy fetch end to end.
   Skips automatically outside the cluster (`YTT_TEST_TOKEN` unset / server
   unreachable).

Deployment note: `YTT_PROXY_URL` is sourced from an OpenBao path injected into
the pod, never committed to `declarative-config` (secrets-by-reference rule).
