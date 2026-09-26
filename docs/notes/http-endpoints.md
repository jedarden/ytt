# Operational HTTP endpoint contract

Every HTTP surface ytt exposes — what it is reachable as, who can call it, how
path variants resolve, and what it may touch. This is the normative spec the
rest of the docs reference (`/metrics`, `/admin/egress`, canary metrics, the
path-prefixed deployment); the behavior is pinned by
`tests/unit/test_endpoint_contract.py` (HTTP-level, against the real ASGI app),
the metrics label/cardinality bound by
`tests/unit/test_metrics_cardinality.py`, and, for the OAuth flow itself,
`tests/unit/test_oauth_conformance.py`.

Related: `docs/notes/auth.md` (identity, allowlist, rate limits),
`docs/notes/proxy-egress.md` (egress paths and credential redaction),
`docs/notes/single-replica.md` (deployment topology).

## Route inventory

All ytt-owned routes, as mounted by `mcp.http_app(path=prefix)` with
`YTT_PATH_PREFIX=/ytt/` (the prefix must end with `/` — validated at startup;
`Settings.route()` joins it with each custom-route segment):

| Path | Method(s) | Auth | Purpose |
|---|---|---|---|
| `/ytt` | POST, GET, DELETE | Bearer token (401 without) | **The MCP transport** (Streamable HTTP). The only route that can trigger transcript work. |
| `/ytt/health` | GET, HEAD | none | Liveness/readiness probe. |
| `/ytt/metrics` | GET, HEAD | none | Prometheus scrape (main server). |
| `/ytt/admin/egress` | GET, HEAD | Bearer token **+ allowlist** | Egress IP/ASN diagnostic probe. |
| `/authorize`, `/token`, `/register` (+ `/auth/callback`, `/consent`) | see OAuth | none | OAuth endpoints, mounted **twice**: bare and issuer-prefixed (`/ytt/authorize`, …). |
| `/.well-known/oauth-protected-resource/ytt` | GET, HEAD | none | RFC 9728 PRM (host root + resource path appended). |
| `/.well-known/oauth-authorization-server/ytt`, `/.well-known/openid-configuration/ytt` | GET, HEAD | none | RFC 8414 / OIDC discovery, path-inserted for the path-bearing issuer (`ytt.auth`). |
| `/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration` (bare) | GET, HEAD | none | Same handlers at the bare paths; nothing external routes to them (IngressRoute has no rule), they exist for direct/ClusterIP access. |

There is **no** `/ytt/mcp` route: the transport is mounted at the prefix root
itself (`/ytt`), so `POST /ytt` is an MCP message and `POST /ytt/mcp` is a
404. Anything not in this table is a 404 (and, as everywhere, 404s run no
handlers with side effects).

## Visibility model

What is reachable from the public internet is decided by the Traefik
IngressRoute (`deploy/k8s/ardenone-cluster/ytt/ingressroute.yml`), not by the
app:

- **Public** (`Host(mcp.ardenone.com) && PathPrefix(/ytt)`): every path under
  the prefix — including `/ytt/metrics` and `/ytt/health`, which the app
  serves without a token. Nothing at the app or the ingress narrows this; a
  scrape of `/ytt/metrics` from anywhere on the internet succeeds today. The
  Prometheus ServiceMonitor happens to scrape the ClusterIP directly, but
  that is *not* what makes the endpoint reachable or unrechable.
- **Public, dedicated rules** (priority 1000): the three path-inserted
  `/.well-known/*/ytt` metadata routes.
- **In-cluster only**: the canary's metrics port (below) — a separate
  Deployment behind the ClusterIP Service `ytt-canary` :8081, with no
  IngressRoute rule at all.

**Design consequence (the invariant the tests pin):** because the prefix rule
routes everything, *every unauthenticated response body must be safe to
expose publicly*. That is why `/ytt/metrics` carries only aggregate series
with a bounded label set, and why `/ytt/health` returns a fixed
`{"status": "ok"}` — see the per-endpoint contracts. An endpoint that cannot
honor this belongs behind the bearer gate like `/admin/egress`, not behind a
comment.

## `/ytt` — the MCP transport (the only transcript-work path)

- Methods `POST` (JSON-RPC messages), `GET` (SSE stream), `DELETE` (session
  termination), mounted by FastMCP's `RequireAuthMiddleware`.
- **Authentication:** every request — protocol messages included — requires a
  valid bearer token. Without one, or with an invalid one: `401` with
  `WWW-Authenticate: Bearer … resource_metadata="<PRM URL>"` where the PRM URL
  is `{origin}/.well-known/oauth-protected-resource{resource-path}`
  (`https://mcp.ardenone.com/.well-known/oauth-protected-resource/ytt` for the
  reference deployment). Authentication happens **before** any protocol
  parsing: an unauthenticated `initialize` or `tools/call` never reaches a
  tool body.
- **Authorization:** the allowlist (`YTT_ALLOWED_SUBJECTS`) is enforced per
  tool call by the `AuthMiddleware(check_subject_auth)` middleware —
  `docs/notes/auth.md` ("Authentication != Authorization"). The transport
  level 401 proves identity handling; the middleware is what denies
  authenticated non-allowlisted callers.
- **Side effects:** this is the only route that can read the cache, run a
  yt-dlp fetch, or start a Whisper job. Every other route in this document is
  contractually barred from the transcript pipeline (tested:
  `test_operational_routes_never_trigger_transcript_work`).

## `/ytt/health` — liveness

- `GET`/`HEAD`, no auth. Body is exactly `{"status": "ok"}` — no version, no
  subject count, no configuration detail (public visibility model above).
- Kubernetes liveness + readiness probe target
  (`deployment.yml` `httpGet: /ytt/health :8080`).
- **Side effects: none.** A fixed-shape handler with no request-controlled
  branch; it reads nothing and writes nothing.

## `/ytt/metrics` — Prometheus exposition

- `GET`/`HEAD`, no auth (Prometheus convention; and per the visibility model
  it is publicly routed, so it must stay public-safe).
- Body: the process-wide `prometheus_client` registry in text exposition
  format. The **label surface is bounded**: `ytt_fetch_blocks_total{outcome}`,
  `ytt_whisper_errors_total{reason}`, `ytt_rate_limited_total{subject_hash}`,
  and label-free gauges/histograms (`ytt_cache_bytes`,
  `ytt_egress_is_residential`, …). No video IDs, no URLs, no subject emails,
  no transcript text may ever become a label name, label value, or metric
  name — subjects appear only as a sha256 prefix (`subject_hash`), and the
  blocked-field redaction (`ytt.observability`) keeps identity fields out of
  the process that renders this body. (The reserved *structural* labels the
  exposition format itself attaches — `le` on histogram buckets, `quantile`
  on summaries — are library-owned numeric bucket boundaries, not part of
  the application surface; the tests exempt exactly those two.) The bound is
  a regression test, not a convention:
  `tests/unit/test_metrics_cardinality.py` scrapes this body and fails on any
  family or label key outside the documented surface, on any identifier-shaped
  label key (video id, subject, job id, URL), and on label values wider than a
  bounded vocabulary — or anything but the 8-hex `subject_hash` where a
  subject appears at all.
- The `ytt_egress_is_residential` gauge is written by the server's one-shot
  startup egress probe (target `https://ipinfo.io/json`, hard 10 s timeout,
  dialed through `YTT_PROXY_URL` when set, fail-soft on error — the full
  contract: [README §Configuration](../../README.md#configuration)) and
  re-probed by each authenticated `/ytt/admin/egress` call (next section) —
  never by a scrape. It is registered at import time, so the series is
  always present at 0 or 1.
- **Side effects: none.** A scrape is a read-only snapshot of counters that
  the fetch/ASR path increments; it never starts work (a monitor scraping
  more often cannot speed up — or break — the pipeline).

## `/ytt/admin/egress` — gated egress diagnostic

- `GET`/`HEAD` only. Returns the current egress IP, ASN, org, proxy flag, and
  residential classification.
- **Authentication** (two steps, both the same as a tool call's — the route's
  contract is "same auth as tool calls; no special admin token"):
  1. *401* — no bearer token, or a token the IdP-backed provider rejects
     (`verify_token` returns None). Response carries
     `WWW-Authenticate: Bearer resource_metadata="…"` pointing at the same
     routable PRM URL the transport challenge uses
     (`{origin}/.well-known/oauth-protected-resource{resource-path}`) — a
     client that follows the pointer must land on a 200 metadata document,
     which `test_admin_egress_401_points_at_routable_metadata` pins.
  2. *403* — validly authenticated but the `email` claim is not admitted by
     `ytt.authz.subject_allowed` (the **same predicate** the tool-call gate
     uses: case-insensitive matching, `@domain` patterns honored, and **no
     `email_verified` requirement** — that claim is meaningless against the
     reference Authentik, which hardcodes it `False` for every account; see
     the `ytt.authz` module docstring).
- **Side effects (complete list):**
  1. one outbound egress probe (`ytt.selftest.probe_egress`, via
     `asyncio.to_thread`) — a network call to ipinfo through the configured
     proxy path;
  2. `ytt_egress_is_residential` gauge set from the probe result;
  3. the authenticated email written to the `/tmp/ytt_last_sub` discovery
     file (mode 0600, once per process — `ytt selftest --show-sub`).
  Probe failure → `502` whose body carries the exception text after
  `redact_credentials()` (a httpx failure can quote the credentialed proxy
  URL; `docs/notes/proxy-egress.md`).
  **It never touches the transcript pipeline**: no cache read or write, no
  fetch, no Whisper job — the egress probe and the gauge are the entire
  effect.

## OAuth routes (bare + issuer-prefixed twins)

`/authorize`, `/token`, `/register`, `/auth/callback`, `/consent` and their
`/ytt/`-prefixed twins are served by the FastMCP OAuth provider
(`ytt.auth`). Both spellings exist because FastMCP mounts these at bare
paths while ytt's issuer URL is path-bearing; `YttOIDCProvider.get_routes`
path-inserts a second mount of each so external traffic arriving as
`/ytt/authorize` (the only shape the IngressRoute delivers) finds a route.
They are unauthenticated by design — they are how an anonymous caller
*becomes* authenticated. `GET /token` or `GET /register` is a 405 (the
routes are POST-only), `GET /authorize` without params is a 400. The
`/.well-known` metadata routes are unauthenticated public documents (RFC
9728/8414): resource identifier, authorization-server location, supported
scopes — nothing per-user.

## Canary `/metrics` (separate Deployment, port 8081)

The `ytt canary` process (its own Deployment, `CMD ["ytt", "canary"]`) serves
`GET /metrics` on **:8081** via `prometheus_client.start_http_server` — no
path prefix, no TLS, **no OAuth**. Network visibility is the contract here,
not application auth: the ClusterIP Service `ytt-canary` (port 8081) is the
only front, selected by the ServiceMonitor; there is no IngressRoute rule, so
nothing outside the cluster can reach it. Two properties matter:

- The probe loop runs on its own timer (`YTT_CANARY_INTERVAL_SEC`); a scrape
  neither triggers nor throttles a probe. The endpoint is a read-only window
  on the same process-wide registry the probes write.
- That registry is process-wide: because the canary imports
  `ytt.observability`, :8081 serves every registered `ytt_*` series (the
  `ytt_canary_*` gauges the alert fires on, plus the library's counters) —
  all aggregate-only, so the public-safe invariant holds there too (pinned by
  the same module: a fresh canary-shaped registry — `import ytt.canary` and
  nothing else, the exact import surface of the Deployment's process — must
  expose exactly the documented family set). Its
  Kubernetes liveness probe deliberately targets `/metrics` (always-200
  while the process serves HTTP).

## Trailing slashes and path normalization

Starlette's router (with the default `redirect_slashes`) defines the
behavior for every route above; the tests pin it because clients and
monitors get it wrong in both directions:

- **Trailing slash → `307` redirect** to the canonical path
  (`/ytt/metrics/` → `/ytt/metrics`, `/ytt/` → `/ytt`,
  `/ytt/admin/egress/` → `/ytt/admin/egress`). The redirect preserves the
  method and body (307, not 301/302). Auth therefore **cannot be bypassed or
  dropped by the slash**: following `/ytt/admin/egress/` lands on the gated
  route and 401s, and so does following `/ytt/` — `GET /ytt` is the SSE
  stream, which the transport's auth middleware gates like every other
  method; a `POST /ytt/metrics/` redirects into the 405. Configured
  scrape paths and probes should still use the canonical spelling — each
  redirect is a wasted round trip.
- **Dot segments collapse** (`/ytt/./metrics` → 200); **double slashes do
  not** (`/ytt//metrics` → 404); **matching is case-sensitive**
  (`/YTT/metrics` → 404). There are no alias routes: only the spellings in
  the inventory table resolve.

## Method discipline

Custom routes register `GET` (+`HEAD`) only; anything else is a **405** —
there is no write-shaped handler on `/ytt/metrics`, `/ytt/health`, or
`/ytt/admin/egress` for a request to reach, whatever verb or body it
carries. The transport route is the only one accepting `POST`/`DELETE`
(besides the OAuth POST endpoints).

## Enforcement

`tests/unit/test_endpoint_contract.py` drives the real ASGI app (Starlette
`TestClient`) and pins, at HTTP level:

- the route inventory (mounted paths + methods, and that `/ytt/mcp` is *not*
  a route);
- transport auth: unauthenticated/invalid-token `401` with a routable PRM
  pointer, before any protocol handling;
- `/admin/egress` 401/403 gating (including the trailing-slash variant and
  the `email_verified`-less Authentik-shaped token), and its complete
  side-effect set;
- **no transcript work**: a spy quadruple (caption fetch, Whisper
  get-or-create, Whisper run, cache write) stays silent while every
  operational route, slash variant, 404, and OAuth metadata route is driven;
- metrics/health public-safety: bounded label surface, no transcript or
  subject material in the exposition body, fixed liveness body;
- the **cardinality bound** behind that first clause:
  `tests/unit/test_metrics_cardinality.py` holds the real app's public
  exposition *and* the canary Deployment's fresh-process registry to the
  documented family set with per-family exact label keys and bounded label
  values (the `/ytt/metrics` and canary sections above);
- slash/normalization/method behavior for every route class.

The composed chain is smoked, not assumed:
`tests/deployed/test_deployed_ingress_smoke.py` replays this document against
the **live public route** — DNS/TLS/tunnel/Traefik (`YTT_PATH_PREFIX=/ytt/`)
and the app together — covering the public health/metrics reads, both
protected routes' unauthenticated 401 challenges with a routable PRM pointer,
the three `/.well-known/*/ytt` documents, the `/ytt/mcp` 404, the
`ytt-sse`/`ytt-cors` middleware headers, and a before/after metrics
comparison proving unauthorized or unknown requests move no work-path series
(no fetch, no ASR, no cache write, empty queue). It is opt-in (`YTT_DEPLOYED_SMOKE_URL`,
marker `deployed`) so no default gate depends on the deployment being up, and
it asserts the one known app-vs-chain divergence deliberately: the edge
normalizes `//` upstream of the app, so the doubled spelling resolves onto the
same canonical route in production where the in-process app 404s it — with the
protected twin (`//admin/egress`) asserted still gated so normalization can
never be a bypass.
