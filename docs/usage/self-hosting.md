# Self-Hosting Guide

ytt runs anywhere you have a residential egress IP (or a residential proxy) and
an OpenAI-compatible Whisper endpoint.  This guide covers a generic self-hosted
deployment — no ardenone-cluster specifics.

## Requirements

| Component | Requirement | Notes |
|-----------|-------------|-------|
| **Residential egress IP** | Required | YouTube blocks datacenter IPs. Home servers and residential VPS work natively. For cloud VPS, use `YTT_PROXY_URL` with a residential proxy (Webshare, etc.). Most commercial proxies are datacenter IPs and will NOT help. |
| **HTTPS** | Required | The Anthropic MCP backend (the actual connector client) requires HTTPS. Use a reverse proxy (Traefik, Caddy, nginx) with a valid TLS cert. |
| **Whisper endpoint** | Optional | Required only for videos without captions. Without it, caption-less videos return `no_captions_asr_failed`. |
| **`ffmpeg`** | Bundled | The Docker image includes `ffmpeg` (needed by yt-dlp for audio remuxing in the Whisper path). |

## Docker Compose example

```yaml
services:
  ytt:
    image: ronaldraygun/ytt:0.2.20
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ytt-cache:/cache
      - ytt-scratch:/scratch
    environment:
      YTT_PUBLIC_URL: "https://mcp.example.com/ytt"
      YTT_PATH_PREFIX: "/ytt/"
      YTT_ALLOWED_SUBJECTS: ""      # set after discovering your sub (see connector.md)
      YTT_OAUTH_CLIENT_ID: "your-oauth-client-id"       # required — server exits 1 without it
      YTT_OAUTH_CLIENT_SECRET: "your-oauth-client-secret"
      YTT_OIDC_ISSUER: "https://idp.example.com/application/o/ytt/"  # your IdP's issuer (see below)
      YTT_WHISPER_URL: "http://whisper:8000"
      YTT_CACHE_DIR: "/cache"
      YTT_CACHE_MAX_BYTES: "2Gi"
      YTT_SCRATCH_DIR: "/scratch"   # swept on every boot — keep it dedicated to ytt

  # Optional: Whisper ASR service
  # https://github.com/stpb/whisper-openai
  whisper:
    image: ghcr.io/stpb/whisper-openai:latest
    restart: unless-stopped
    volumes:
      - whisper-models:/models
    environment:
      MODEL_NAME: "Systran/faster-whisper-small"

volumes:
  ytt-cache:
  ytt-scratch:
  whisper-models:
```

Put a reverse proxy (e.g. Traefik or Caddy) in front of ytt and expose
`https://mcp.example.com/ytt` publicly.

Note on the OAuth client: the upstream IdP is configurable. ytt defaults to
the reference Authentik (`sso.ardenone.com/application/o/ytt/`); set
`YTT_OIDC_ISSUER` to your IdP's issuer (as in the compose file above) and
the discovery URL is derived from it as
`<issuer>/.well-known/openid-configuration` — set `YTT_OIDC_CONFIG_URL`
only if your IdP serves it elsewhere. The issuer must match your IdP's
advertised value byte-for-byte (Authentik per-application issuers end with
`/`; Keycloak realm issuers don't). On your IdP, register a confidential
client for ytt with redirect URI `https://<your-domain>/ytt/auth/callback`
and the `openid` + `email` scopes; its client ID/secret go in
`YTT_OAUTH_CLIENT_ID` / `YTT_OAUTH_CLIENT_SECRET`. One caveat: id tokens
are verified HS256-keyed-by-client-secret — the reference Authentik's
signing mode (and Authentik's default without an asymmetric signing key) —
so an IdP that signs RS256 via JWKS (Keycloak's default) is not yet
supported; see the note in `ytt/auth.py::build_auth_provider` and the
[configuration reference](configuration.md).

## Kubernetes (generic)

If you're deploying on Kubernetes (not ardenone-cluster), adapt the manifests
in `deploy/k8s/ardenone-cluster/ytt/`:

1. Remove the `ExternalSecret` and `Certificate` (cluster-specific).
2. Create a `Secret` directly with `allowed_subjects`, `proxy_url`, etc.
3. Change the `storageClassName` in the PVC to match your cluster.
4. Remove the Traefik `IngressRoute` and use your own Ingress resource.
5. Update `YTT_PUBLIC_URL` to your domain.

## No-Whisper mode

If you don't have a Whisper endpoint, ytt still works for all captioned videos.
Videos without captions will return:
```json
{"status": "error", "error_code": "no_captions_asr_failed", "message": "..."}
```

To disable Whisper entirely, set `YTT_WHISPER_URL` to an unreachable address.
The `no_captions_asr_failed` error is user-relayable (safe to show to the end user).

## Residential proxy setup (if needed)

If your server is on a datacenter/VPS IP and YouTube blocks it:

```bash
# Webshare residential proxy example:
export YTT_PROXY_URL="http://username:password@proxy.webshare.io:port"
```

The proxy URL is used only as a fallback when the direct IP triggers an
`ip_blocked` error.  Normal requests go direct.

**Security note:** The proxy URL may contain credentials.  The structlog
redaction filter strips credential-bearing URLs from all log output.
Never log or expose `YTT_PROXY_URL`.

## Scaling

ytt is designed for **single replica** — in-process state (LRU cache counter,
single-flight registry, Whisper job FSM) is not distributed.  The Kubernetes
Deployment therefore pins `replicas: 1` and `strategy: Recreate`; running
multiple replicas will cause:
- Duplicate yt-dlp fetches for the same video.
- Cache byte-counter drift (each replica has its own counter).
- Multiple Whisper jobs for the same video.

Scale-out is a redesign, not a replica-count change. It requires a shared cache
index with atomic quota accounting, distributed per-video single-flight leases,
a persistent Whisper job store that any replica can poll, shared rate-limit
counters, and queue-owned scratch files instead of the unconditional startup
sweep. It also needs enough residential egress and Whisper capacity to justify
the added concurrency. Once all process-local state is externalized, the
deployment strategy can return to `RollingUpdate`. See the internal
[single-replica design note](../notes/single-replica.md) for the complete
checklist.

## OAuth discovery

ytt is its own OAuth Authorization Server (AS).  The public-facing metadata URLs:

- Protected Resource Metadata: `https://<your-domain>/.well-known/oauth-protected-resource/ytt`
- AS Metadata: `https://<your-domain>/.well-known/oauth-authorization-server/ytt`
- `WWW-Authenticate: Bearer resource_metadata="<prm-url>"` on 401

The `resource` and `issuer` values will equal `YTT_PUBLIC_URL` exactly.

These are served by ytt itself — no separate OAuth infrastructure is needed.

## Smoke testing

After setup, verify with:

```bash
# Health check
curl https://mcp.example.com/ytt/health
# Expected: {"status": "ok"}

# OAuth metadata
curl https://mcp.example.com/.well-known/oauth-protected-resource/ytt
# Expected: {"resource": "https://mcp.example.com/ytt", ...}
```

Then add the connector in Claude Desktop and verify a transcript fetch works.
