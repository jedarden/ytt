# Self-Hosting Guide

ytt runs anywhere you have a residential egress IP (or a residential proxy) and
an OpenAI-compatible Whisper endpoint.  This guide is a complete runbook for a
generic self-hosted deployment — no ardenone-cluster specifics: DNS, TLS
termination, the `/ytt` path prefix, the upstream OAuth client, allowed
subjects, and end-to-end verification.

The one idea that shapes every step: **ytt owns the `/ytt` path prefix on your
hostname and is its own OAuth authorization server.** `YTT_PUBLIC_URL` (e.g.
`https://mcp.example.com/ytt`) is the OAuth issuer, the resource indicator, and
the base of every URL ytt advertises — the proxy's job is to deliver that exact
URL, unrewritten, to the outside world.

## Requirements

| Component | Requirement | Notes |
|-----------|-------------|-------|
| **Residential egress IP** | Required | YouTube blocks datacenter IPs. Home servers and residential VPS work natively. For cloud VPS, use `YTT_PROXY_URL` with a residential proxy (Webshare, etc.). Most commercial proxies are datacenter IPs and will NOT help. |
| **HTTPS** | Required | The Anthropic MCP backend (the actual connector client) requires HTTPS. Use a reverse proxy (Traefik, Caddy, nginx) with a valid TLS cert. |
| **Whisper endpoint** | Optional | Required only for videos without captions. Without it, caption-less videos return `no_captions_asr_failed`. |
| **`ffmpeg`** | Bundled | The Docker image includes `ffmpeg` (needed by yt-dlp for audio remuxing in the Whisper path). |

---

## Step 1 — DNS

Pick a public hostname (below: `mcp.example.com`) and point it at whatever
terminates TLS for you:

- **Direct:** an `A`/`AAAA` record at your DNS provider pointing at the reverse
  proxy's public IP. Only `443/tcp` needs to be reachable from the internet —
  ytt itself listens on `8080` on a private interface only.
- **Tunnel (no open ports):** a Cloudflare Tunnel or Tailscale Funnel routing
  the hostname to the proxy works equally well; the reference deployment uses a
  Cloudflare Tunnel in front of Traefik. The connector backend only needs the
  hostname to resolve publicly over HTTPS.

Do **not** point DNS at the ytt container directly — there is no TLS in the
container, and the Anthropic backend requires a valid, publicly-trusted
certificate.

## Step 2 — TLS termination and reverse proxy

Put any reverse proxy with a publicly-trusted certificate (e.g. Let's Encrypt)
in front of ytt. Self-signed certs are rejected by the connector backend. Three
proxy duties beyond plain TLS:

1. **SSE/streaming:** disable response buffering on the ytt routes — the MCP
   transport streams Server-Sent Events on `GET /ytt`. In nginx:
   `proxy_buffering off;`. The reference Traefik deployment sets it with a
   headers middleware (`X-Accel-Buffering: no`, `Cache-Control: no-cache` —
   `deploy/k8s/ardenone-cluster/ytt/middlewares.yml`).
2. **CORS for the OAuth hop:** the browser-based OAuth flow originates from
   Claude's origins. Allow `https://claude.ai`, `https://desktop.claude.ai`,
   and `https://claude.com` for `Authorization`, `Content-Type`, `Accept` on
   `GET`/`POST`/`OPTIONS` (same middleware file for a worked example).
3. **Generous timeouts on the callback path** are unnecessary (transcription is
   asynchronous — the tool returns a job handle the client polls), but do not
   install an aggressive global `location`-level timeout that kills SSE reads
   mid-stream; a read timeout of a few minutes is comfortable.

**Caddy** (simplest — automatic Let's Encrypt, no path rewrite):

```caddy
mcp.example.com {
    # SSE: no response buffering
    flush_interval -1

    # ytt under its prefix — forwarded as-is. NEVER `handle_path /ytt/*`:
    # handle_path strips the prefix before proxying.
    handle /ytt* {
        reverse_proxy localhost:8080
    }
    # host-root OAuth metadata (RFC 9728 / RFC 8414 path-insertion)
    handle /.well-known/oauth-protected-resource/ytt*,
           /.well-known/oauth-authorization-server/ytt*,
           /.well-known/openid-configuration/ytt* {
        reverse_proxy localhost:8080
    }

    header /ytt* {
        Access-Control-Allow-Origin "https://claude.ai"
        X-Accel-Buffering "no"
        Cache-Control "no-cache"
    }
}
```

(Caddy's `handle_path` strips the path — the example marks it only to warn
against it. Use `handle` + `reverse_proxy`, which forwards the path untouched.
For multiple CORS origins, Caddy needs a matcher-based `header` block per
origin; see Caddy docs.)

**nginx** (certbot for the certificate):

```nginx
server {
    listen 443 ssl;
    server_name mcp.example.com;
    # ssl_certificate /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
    # ssl_certificate_key ...;

    # Forward the prefix UNMODIFIED — no trailing slash on proxy_pass,
    # no rewrite, no strip_prefix.
    location /ytt {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;          # SSE
        add_header X-Accel-Buffering no always;
        add_header Access-Control-Allow-Origin "https://claude.ai" always;
    }

    # Host-root OAuth metadata for the path-bearing issuer
    location ~ ^/\.well-known/(oauth-protected-resource|oauth-authorization-server|openid-configuration)/ytt {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_buffering off;
        add_header Access-Control-Allow-Origin "https://claude.ai" always;
    }
}
```

## Step 3 — Preserve the `/ytt` path prefix

This is the step self-hosted MCP servers most often get wrong. ytt is **not**
mounted at the host root: with the default `YTT_PATH_PREFIX=/ytt/`, every
route — the transport, health, metrics, the OAuth endpoints — lives under
`/ytt`, and the OAuth metadata documents advertise `/ytt`-prefixed URLs. The
proxy must therefore forward the prefix **byte-for-byte**:

- **Never strip the prefix** (`strip_prefix`, `handle_path`, a trailing slash
  on nginx `proxy_pass`, a `rewrite`): ytt would then receive `/health` where
  it expects `/ytt/health`, every metadata URL it advertises would 404, and
  the OAuth flow dies at discovery.
- `YTT_PUBLIC_URL` must include the prefix (`https://mcp.example.com/ytt`) and
  `YTT_PATH_PREFIX` must match the proxy config. `YTT_PATH_PREFIX` must end
  with `/` — startup exits 1 otherwise. The two are validated for shape, not
  for agreement with your proxy — the verification in Step 7 is how you prove
  they line up.

**The host-root `.well-known` routes.** Because the issuer is path-bearing
(`https://mcp.example.com/ytt`), RFC 9728/8414/OIDC discovery place the
metadata at the **host root with the issuer path appended** — outside `/ytt`:

| Public URL | What it is |
|---|---|
| `/.well-known/oauth-protected-resource/ytt` | RFC 9728 Protected Resource Metadata |
| `/.well-known/oauth-authorization-server/ytt` | RFC 8414 AS metadata |
| `/.well-known/openid-configuration/ytt` | OIDC discovery alias — MCP clients probe this variant too |
| `/ytt/*` | everything else: transport, health, metrics, OAuth endpoints |

Your proxy must route all four families to ytt (the Caddy/nginx snippets above
do). Two gotchas seen in production:

- **A broader `/.well-known` rule on the same hostname swallows them.** If
  another service on the host owns a catch-all `PathPrefix(/.well-known)`
  (or equivalent), a metadata request for ytt can land on *that* service,
  which answers with its own 401/`WWW-Authenticate` — the client derails onto
  the wrong authorization server and reports "no connection succeeded" even
  though everything else is fine. Give ytt's three rules explicit, higher
  priority (the reference IngressRoute uses `priority: 1000` against an
  auto-computed ~80 catch-all).
- **The bare paths are not public.** ytt also mounts the metadata handlers at
  `/.well-known/oauth-authorization-server` (no suffix) and the OAuth
  endpoints at bare `/authorize`, `/token`, `/register`, `/auth/callback` —
  those exist for direct/ClusterIP access. Your proxy should not route them;
  only the `/ytt`-prefixed and `/.well-known/*/ytt` forms are public.

## Step 4 — Register the OAuth client on your IdP

There are **two OAuth hops**, and only the second needs anything from you:

```
Claude ⇄ ytt's own OAuth AS        (hop 1 — nothing to register)
          ytt ⇄ your upstream IdP  (hop 2 — register a confidential client)
```

**Hop 1 — Claude as ytt's client.** ytt is its own authorization server
(FastMCP OAuthProxy, OAuth 2.1 + PKCE). Claude discovers it from the metadata
in Step 7, registers itself via DCR, and authenticates the user by redirecting
through hop 2. ytt only issues codes to Claude's own redirect URIs
(`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`
— hardcoded, see `CLAUDE_REDIRECT_URIS` in `ytt/auth.py`). You configure
nothing for this hop, and those two URLs are **not** registered anywhere on
your IdP — a common confusion.

**Hop 2 — ytt as your IdP's client.** On your IdP (Authentik, or any OIDC
provider), register a **confidential** client for ytt:

| Setting | Value |
|---|---|
| Client type | Confidential (client secret) |
| Redirect URI | `https://mcp.example.com/ytt/auth/callback` — exactly, including the `/ytt` prefix; this is ytt's own callback endpoint (`YTT_PUBLIC_URL` + `/auth/callback`) |
| Scopes | `openid`, `email`, and `offline_access` (refresh tokens — without it Claude re-prompts every few minutes) |
| `email` claim | Must be populated and `email_verified` — the allowlist matches against the verified email claim |
| Signing | id tokens signed **HS256 with the client secret** (Authentik's default for providers without an asymmetric signing key) |

Two IdP constraints:

- **HS256, not RS256.** ytt verifies upstream id tokens HS256-keyed-by-client-secret.
  An IdP that signs RS256 via JWKS (Keycloak's default) completes the OAuth
  dance and then fails token verification — not yet supported; see the note in
  `ytt/auth.py::build_auth_provider` and the
  [configuration reference](configuration.md).
- **Issuer, byte-for-byte.** `YTT_OIDC_ISSUER` is matched against the id
  token's `iss` claim with no normalization. Authentik per-application issuers
  end with `/` (`https://idp.example.com/application/o/ytt/`); Keycloak realm
  issuers do not. Set exactly what your IdP advertises. The discovery URL is
  derived as `<issuer>/.well-known/openid-configuration` — set
  `YTT_OIDC_CONFIG_URL` only if your IdP serves it elsewhere. ytt fetches
  discovery eagerly at startup: if the IdP is unreachable, the server exits
  and crash-loops (fail-closed, never an unauthenticated boot).

The client ID/secret go in `YTT_OAUTH_CLIENT_ID` / `YTT_OAUTH_CLIENT_SECRET`
(both startup-required — exit 1 without them; the secret also seeds ytt's own
token signing key — rotation and failure modes:
[../notes/auth.md](../notes/auth.md)). ytt defaults its IdP to the reference
Authentik (`sso.ardenone.com/application/o/ytt/`); setting `YTT_OIDC_ISSUER`
is what points it at yours.

## Step 5 — Configure and start ytt

```yaml
services:
  ytt:
    image: ronaldraygun/ytt:0.2.22
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"   # private — only the proxy talks to it
    volumes:
      - ytt-cache:/cache
      - ytt-scratch:/scratch
    environment:
      YTT_PUBLIC_URL: "https://mcp.example.com/ytt"  # required — no fallback; server exits 1 without it
      YTT_PATH_PREFIX: "/ytt/"
      YTT_ALLOWED_SUBJECTS: ""      # set after discovering your sub (see connector.md)
      YTT_OAUTH_CLIENT_ID: "your-oauth-client-id"       # required — server exits 1 without it
      YTT_OAUTH_CLIENT_SECRET: "your-oauth-client-secret"
      YTT_OIDC_ISSUER: "https://idp.example.com/application/o/ytt/"  # your IdP's issuer (see Step 4)
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

Every variable is documented in the [configuration reference](configuration.md).
The startup-required trio is `YTT_PUBLIC_URL`, `YTT_OAUTH_CLIENT_ID`,
`YTT_OAUTH_CLIENT_SECRET`; a malformed `YTT_PUBLIC_URL`/`YTT_OIDC_ISSUER`
(scheme, hostname, whitespace, query, fragment) or a `YTT_PATH_PREFIX` without
its trailing slash also refuses to boot. Container-level checks (PVC vs cache
budget, Whisper timeout invariant) run at serve time — read the startup log
once before exposing anything.

## Step 6 — Allow your subjects

`YTT_ALLOWED_SUBJECTS` is checked against the token's **verified email claim**
on every tool call. **Empty = deny all** — every authenticated caller gets
`403` until you add at least one subject. Deliberately: complete the OAuth
flow once, discover your own email value with `ytt selftest --show-sub`, then
set it:

```
YTT_ALLOWED_SUBJECTS="me@example.com"        # exact address, or @example.com for a whole domain
```

The full walk (add the connector first, then harvest the subject, then verify)
is [connector.md](connector.md). Treat the list as sensitive data — the
redaction filter keeps it out of logs; keep it out of commits and shell
history too.

## Step 7 — Verify metadata, transport, and callback

Run every check against the **public** URL (through DNS, TLS, and the proxy —
not against `localhost:8080`, which would prove nothing about Steps 1–3).
Substitute your domain throughout. All commands are read-only.

**1. Health through the full chain:**

```bash
curl https://mcp.example.com/ytt/health
# {"status":"ok"}
```

**2. Protected Resource Metadata** (host root + `/ytt` — the RFC 9728 path
insertion; this is what a 401's `WWW-Authenticate` points clients at):

```bash
curl https://mcp.example.com/.well-known/oauth-protected-resource/ytt
```

```json
{"resource":"https://mcp.example.com/ytt","authorization_servers":["https://mcp.example.com/ytt"],"scopes_supported":["openid","email","offline_access"],"bearer_methods_supported":["header"]}
```

`resource` and the `authorization_servers` entry must equal `YTT_PUBLIC_URL`
**byte-for-byte** — if they show a different host, an `/ytt`-less URL, or a
trailing slash, the metadata is pointing clients at the wrong deployment; fix
`YTT_PUBLIC_URL`, not the proxy. That byte-for-byte derivation is why
`YTT_PUBLIC_URL` is startup-required with no default: a missing value fails
the boot (exit 1) instead of pointing your deployment's OAuth metadata at
whoever shipped the image.

**3. AS metadata and the OIDC alias** — issuer is the path-bearing public URL,
endpoints are issuer-prefixed:

```bash
curl https://mcp.example.com/.well-known/oauth-authorization-server/ytt
curl https://mcp.example.com/.well-known/openid-configuration/ytt   # same document
```

```json
{"issuer":"https://mcp.example.com/ytt","authorization_endpoint":"https://mcp.example.com/ytt/authorize","token_endpoint":"https://mcp.example.com/ytt/token","registration_endpoint":"https://mcp.example.com/ytt/register",...}
```

Every advertised URL must be fetchable through your proxy (they are — ytt
mounts the operational OAuth routes both bare and issuer-prefixed precisely so
the advertised paths work; Step 3's broad-`.well-known` gotcha exists because
a request that lands on another service here derails discovery).

**4. The transport challenges correctly:**

```bash
curl -si -X POST https://mcp.example.com/ytt -H 'Content-Type: application/json' -d '{}' | grep -iE '^HTTP|www-authenticate'
```

```
HTTP/2 401
www-authenticate: Bearer error="invalid_token", ... resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/ytt"
```

The `resource_metadata` URL must be the one from check 2. A `200` here would
mean the transport is unauthenticated — stop and investigate.

**5. The upstream callback is routed to ytt** (this is the redirect URI
registered on your IdP in Step 4):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://mcp.example.com/ytt/auth/callback
# 400   ← ytt's own "OAuth Error" page: the route reaches ytt's OAuth handler,
#         which rejects the missing code. This is the healthy answer.
```

Read the 400 as a routing proof: anything else means the path is not reaching
ytt — `404` means the proxy isn't forwarding `/ytt/auth/callback` (Step 3), a
`401` from a *different* service means another route captured the path. For
reference, a **real** callback (IdP returns with a valid `code`) answers `302`
to one of Claude's redirect URIs, completing hop 1 — not something you can
produce by hand, and not something to expect from curl. The bare
`/auth/callback` (no prefix) is expected to 404 publicly — nothing routes it.

**6. End to end:** add the connector in Claude Desktop
([connector.md](connector.md) Step 1), complete the OAuth popup (hop 1 → hop 2
through your IdP's login page and back), then — still with an empty allowlist —
confirm a transcript request gets `403`. That failure is the **success**
signal for Steps 1–5: it proves auth worked end to end and only Step 6
(authorization) remains.

---

## Kubernetes (generic)

If you're deploying on Kubernetes (not ardenone-cluster), adapt the manifests
in `deploy/k8s/ardenone-cluster/ytt/`:

1. Remove the `ExternalSecret` and `Certificate` (cluster-specific).
2. Create a `Secret` directly with `allowed_subjects`, `proxy_url`, etc.
3. Change the `storageClassName` in the PVC to match your cluster.
4. Remove the Traefik `IngressRoute` and use your own Ingress resource —
   keeping the four route families from Step 3 (the `/ytt` prefix plus the
   three host-root `.well-known/*/ytt` paths) and, if on Traefik, the
   `ytt-sse`/`ytt-cors` middlewares from Step 2.
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
