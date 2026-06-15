# How the Claude Apps Actually Implement the MCP OAuth Client Flow

> Research compiled 2026-06-14 for the `ytt` project. **Companion to**
> `docs/research/mcp-oauth-authentication.md` (the server-side / spec doc) — read that first.
> This doc is the **client side**: what Claude Desktop, Claude Web (claude.ai), and the
> Claude **mobile** (iOS/Android) apps actually do when a user adds a remote MCP server as a
> custom connector, so the `ytt` server matches their *real* behavior, not just the spec's
> ideal.

## Summary

- **The MCP client is Anthropic's backend, not your device.** For a *remote* custom
  connector, claude.ai / Desktop / mobile all run the OAuth discovery, registration, token
  exchange **and** the actual MCP tool calls from **Anthropic's cloud** (egress
  `160.79.104.0/21` IPv4, `2607:6bc0::/48` IPv6). The *only* on-device part is the human
  consent step (a browser/webview opening the IdP login page). Your server and AS must be
  **publicly reachable from Anthropic's IP ranges** — local-only / Tailscale-only / mTLS-only
  servers cannot be added as remote connectors.
- **The discovery sequence matches the spec** and is the same on all hosted surfaces:
  unauthenticated request → **401 with `WWW-Authenticate: Bearer resource_metadata="…"`** →
  fetch Protected Resource Metadata (`/.well-known/oauth-protected-resource`) → fetch AS
  metadata (`/.well-known/oauth-authorization-server`, fallback `/.well-known/openid-configuration`)
  → register client → **authorization-code + PKCE (S256)** redirect → token exchange →
  `Authorization: Bearer` on every subsequent call.
- **One redirect URI for all hosted surfaces:** `https://claude.ai/api/mcp/auth_callback`
  (web, Desktop, **mobile**, Cowork). Migrating to `https://claude.com/api/mcp/auth_callback`
  — **register both**. `localhost`/`127.0.0.1` loopback redirects are **Claude Code only**.
  Mobile does **not** use a custom URL scheme — it uses the same `claude.ai` HTTPS callback,
  because the OAuth client is Anthropic's server, not the app binary.
- **Registration:** Claude attempts **DCR (RFC 7591)** and **CIMD** automatically (the DCR POST
  uses `client_name: "claudeai"` / `"Claude"` and `redirect_uris:
  ["https://claude.ai/api/mcp/auth_callback"]`). If neither is available, the user pastes a
  **manual OAuth Client ID + (optional) Secret** under **Advanced settings** — but that UI is
  **web/Desktop only**.
- **Mobile cannot ADD connectors.** iOS/Android can only *use* connectors already added on
  claude.ai/Desktop. So the entire "Add custom connector"/OAuth-consent-first-time flow you
  must satisfy is a **web/Desktop** flow; mobile just rides the token Anthropic already holds.
- **Biggest server-side sharpening vs. the existing doc:** (1) add the **IPv6** egress range,
  (2) the connector must be **public-internet reachable** (rules out our default
  Tailscale-only posture — `ytt` needs a public ingress), (3) the apps register as
  `client_name: "claudeai"` so don't filter on a `"Claude"` exact-match, (4) "works in MCP
  Inspector / mobile-LTE but Claude says *couldn't reach server*" is usually
  **reachability/transport**, not OAuth.

---

## What the Claude apps actually do, step by step

This is the sequence Anthropic's backend runs when a user clicks **Add custom connector** on
claude.ai or Claude Desktop and pastes the `ytt` URL. (Confirmed by the official auth doc and
by builders who packet-captured the flow.)

1. **Probe the server unauthenticated.** Claude makes an MCP request (Streamable HTTP) to your
   URL with no token. `initialize` may be allowed unauthenticated; the first protected method
   (`tools/list`) must trigger the challenge.
2. **Read the 401 challenge.** Your server returns `401 Unauthorized` with
   `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource"`.
   **This header is required** by the hosted connector — without it, discovery never starts and
   the connector silently fails to add. (Claude Code tolerates a bare 401; claude.ai/Desktop/
   mobile do not.)
3. **Fetch Protected Resource Metadata (RFC 9728).** `GET /.well-known/oauth-protected-resource`
   (or the path-aware variant from the `resource_metadata` URL). Claude reads `resource` and
   `authorization_servers`.
4. **Fetch Authorization Server Metadata (RFC 8414).**
   `GET /.well-known/oauth-authorization-server` on the AS, falling back to
   `/.well-known/openid-configuration`. Claude reads endpoints + `code_challenge_methods_supported`
   (must include `S256`). Builders report Claude **sometimes re-fetches metadata** later in the
   token flow — keep these endpoints byte-stable and fast (**≤10 s**).
5. **Register the client.** In priority order Claude tries:
   - **DCR (RFC 7591):** `POST <registration_endpoint>` with `client_name: "claudeai"` and
     `redirect_uris: ["https://claude.ai/api/mcp/auth_callback"]`, gets back a `client_id`
     (and secret if confidential). Out of the box.
   - **CIMD:** uses an HTTPS-URL `client_id` — only if AS metadata advertises **both**
     `client_id_metadata_document_supported: true` **and** `"none"` in
     `token_endpoint_auth_methods_supported`.
   - **Manual / Anthropic-held creds:** if neither, the user-supplied Client ID/Secret from
     **Advanced settings** is used (or, for verified directory connectors, Anthropic-held creds
     via `mcp-review@anthropic.com`).
6. **Authorization redirect (the ONLY on-device step).** Anthropic constructs the authorize URL
   (`response_type=code`, `code_challenge`, `code_challenge_method=S256`, `state`,
   `redirect_uri=https://claude.ai/api/mcp/auth_callback`, `scope`, and the `resource`
   parameter per RFC 8707) and sends the **user's browser** to it. On **Desktop** this opens the
   **system default browser**; on **web** it's a same-tab/redirect; on **mobile** it's an
   in-app webview/system browser — but in all cases the user logs in *to your IdP*, not to
   Anthropic.
7. **Consent → code back to `claude.ai/api/mcp/auth_callback`.** Your AS redirects to Anthropic's
   callback with `code` (+ `iss` per RFC 9207). Anthropic — **server-side** — receives it.
8. **Token exchange (server-side).** Anthropic's backend `POST`s to your `token_endpoint` with
   `grant_type=authorization_code`, `code`, `code_verifier` (PKCE), the `resource` parameter,
   and client auth if confidential. Gets the access token (+ refresh token).
9. **Authenticated MCP calls (server-side, from Anthropic's cloud).** Every subsequent
   `tools/list` / `tools/call` carries `Authorization: Bearer <token>` and **originates from
   `160.79.104.0/21` / `2607:6bc0::/48`**, not the user's device. Refresh happens reactively on
   401 and proactively ~5 min pre-expiry (refresh timeout ≤30 s).

**Where each piece runs (critical mental model for `ytt`):**

| Phase | Runs on |
|---|---|
| 401 probe, metadata discovery, DCR/CIMD, token exchange | **Anthropic backend** (cloud) |
| User login + consent (browser/webview) | **User's device** |
| Authorization-code callback receipt | **Anthropic backend** (`claude.ai/api/mcp/auth_callback`) |
| All MCP tool calls + token refresh | **Anthropic backend** (cloud, egress IP range) |

> The "Claude Desktop opens your system browser and the token is stored *locally*" behavior
> described in some Casdoor/Zuplo guides applies to Desktop's **local stdio bridge / mcp-remote
> helper**, a *different* path from a hosted **remote custom connector**. For a remote connector
> (what `ytt` is), the token lives in Anthropic's backend and the calls come from Anthropic's
> cloud. Don't conflate the two. *(Boundary between the two paths is partly undocumented —
> **needs-empirical-confirmation** for our exact deployment.)*

---

## Callback / redirect URIs (confirmed values)

| Surface | Redirect URI Claude uses | Notes |
|---|---|---|
| Claude **Web** (claude.ai) | `https://claude.ai/api/mcp/auth_callback` | hosted |
| Claude **Desktop** | `https://claude.ai/api/mcp/auth_callback` | hosted (remote connector path) |
| Claude **Mobile** (iOS/Android) | `https://claude.ai/api/mcp/auth_callback` | **same as web**; no custom URL scheme |
| **Cowork** | `https://claude.ai/api/mcp/auth_callback` | hosted |
| **Future / migration** | `https://claude.com/api/mcp/auth_callback` | **register this too** |
| **Claude Code** (CLI only) | `http://localhost:<ephemeral>/callback` (declares `http://localhost/callback` + `http://127.0.0.1/callback`) | RFC 8252 loopback; **port-agnostic matching required** |

- **Mobile uses the *same* HTTPS callback as web/desktop** — because the OAuth client is
  Anthropic's server, the redirect target is Anthropic's server, not an app deep-link. There is
  **no** `claudeai://` / app-scheme redirect to register. (If you only see web/desktop work and
  mobile "fail," it's almost never a mobile-specific redirect — it's that you only registered
  `claude.ai` and not `claude.com`, or DCR isn't enabled so the manual client lacks one of them.)
- **Loopback (`localhost`/`127.0.0.1`) redirects are Claude Code only** — never expect them from
  hosted Claude. If `ytt` is added via a hosted surface, only the `claude.ai`/`claude.com`
  callbacks matter.

---

## Desktop vs Mobile vs Web — OAuth-relevant differences

| Aspect | Web (claude.ai) | Desktop | Mobile (iOS/Android) |
|---|---|---|---|
| Can **add** a custom connector? | ✅ Yes | ✅ Yes | ❌ **No** — use only servers already added on web/Desktop |
| "Advanced settings" (paste Client ID/Secret) | ✅ | ✅ | ❌ (no add UI ⇒ no manual-creds UI) |
| Redirect URI | `claude.ai/api/mcp/auth_callback` | same | same (no app scheme) |
| Who runs the OAuth client | Anthropic backend | Anthropic backend | Anthropic backend |
| Consent browser | redirect in-page | **system default browser** | in-app webview / system browser *(unconfirmed which)* |
| Token storage (remote connector) | Anthropic backend | Anthropic backend | Anthropic backend (reuses web-added token) |
| Egress IP for MCP calls | `160.79.104.0/21`, `2607:6bc0::/48` | same | same |

**Net:** there is **no separate mobile OAuth path** to satisfy. iOS/Android shipped remote-MCP
support (mid-2025) as *consumers* of connectors that were added — and OAuth-completed — on
claude.ai/Desktop. So: get the **web/Desktop add flow** accepted, and mobile works for free. The
common "desktop works, mobile fails" complaint resolves to **register both `claude.ai` and
`claude.com` callbacks** + ensure DCR (or a pre-registered client carrying both URIs), not to any
mobile-specific OAuth quirk.

---

## Gotchas / failure modes specific to getting accepted by the Claude apps

1. **Server not reachable from Anthropic's cloud.** The #1 trap for a project like `ytt`: the
   server works in MCP Inspector, on mobile LTE, and via port-forward, but claude.ai says
   **"Couldn't reach the MCP server"** with **zero inbound packets from `160.79.104.0/21`**
   (real report: claude-ai-mcp issue #374). Because Anthropic's backend connects — **not your
   device** — a Tailscale-only / VPN-only / IP-allowlisted-to-yourself endpoint is invisible.
   `ytt` **must have a public-internet ingress** and must allowlist `160.79.104.0/21` +
   `2607:6bc0::/48` (and IdP host too).
2. **Missing `WWW-Authenticate: Bearer resource_metadata="…"` on the 401.** Discovery never
   starts; the connector silently fails to add. Hosted surfaces require it (Claude Code doesn't).
3. **Only `claude.ai` callback registered, not `claude.com`.** Manifests as redirect-URI
   mismatch / "desktop worked once, now fails" / mobile fails. Register both.
4. **DCR `client_name` filtering.** Claude registers as **`client_name: "claudeai"`** (lowercase,
   no space) on the wire — the human-facing client name is "Claude". If your AS allowlists by an
   exact `"Claude"` client_name, DCR is rejected. Don't hard-match the name.
5. **CIMD not triggering.** Needs **both** `client_id_metadata_document_supported: true` **and**
   `"none"` in `token_endpoint_auth_methods_supported`; otherwise Claude falls back to DCR/manual.
6. **Missing `code_challenge_methods_supported: ["S256"]`.** Claude *always* sends S256; absence
   (or AS rejecting PKCE) fails authorization.
7. **Wrong transport / path.** Use **Streamable HTTP** on a stable path; some servers 404/405 the
   well-known or callback routes (trailing-slash or GET-vs-POST mismatch) before reaching
   `/token` (claude-ai-mcp issue #313). Honor exactly the methods/paths your metadata advertises.
8. **Audience not enforced.** Validate the token's `aud`/`resource` is *your* server (RFC 8707) —
   confused-deputy risk; the apps bind `resource` so your server should too.
9. **Slow endpoints.** Discovery/registration/token must answer **≤10 s** (refresh ≤30 s) or you
   get intermittent add failures.
10. **HTTPS + valid cert required.** A valid public CA cert (Let's Encrypt is fine) is mandatory;
    self-signed/expired certs fail silently from Anthropic's side.
11. **Silent failures with no diagnostics.** claude.ai shows only "Disconnected"/"Couldn't reach"
    with no detail; comprehensive request/response logging on the server is the only practical way
    to localize which layer (reachability → 401 → metadata → DCR → token → MCP) broke.

---

## Minimum the `ytt` server must do to be accepted by the Claude apps

Distilled from real app behavior. ✅ = matches spec; ⚠️ = app is **stricter** than spec;
🔁 = app is **looser**/has its own quirk.

- [ ] ⚠️ **Public-internet ingress**, reachable from `160.79.104.0/21` and `2607:6bc0::/48`
      (and your IdP host). No Tailscale-only / device-only reachability. *(Stricter than spec,
      which is transport-agnostic.)*
- [ ] ✅ Valid public-CA TLS cert; HTTPS only.
- [ ] ✅ **Streamable HTTP** MCP endpoint on a stable path; `initialize` may be unauthenticated,
      protected methods 401.
- [ ] ⚠️ **401 + `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource"`**
      on any unauthenticated protected request. *(Hosted apps require it; spec/Claude Code don't.)*
- [ ] ✅ `/.well-known/oauth-protected-resource` (+ path variant) → `resource`,
      `authorization_servers`, `scopes_supported`.
- [ ] ✅ AS metadata at `/.well-known/oauth-authorization-server`
      (and/or `/.well-known/openid-configuration`) with `code_challenge_methods_supported: ["S256"]`,
      `authorization_endpoint`, `token_endpoint`, and one of:
      `registration_endpoint` (DCR) **or** CIMD pair (`client_id_metadata_document_supported: true`
      + `"none"` auth method) **or** rely on the user pasting manual creds.
- [ ] ⚠️ If **manual / no-DCR**: pre-register **both** `https://claude.ai/api/mcp/auth_callback`
      **and** `https://claude.com/api/mcp/auth_callback` as allowed redirect URIs.
- [ ] 🔁 Don't reject DCR by `client_name` — it arrives as **`"claudeai"`**.
- [ ] ✅ PKCE S256 enforced; exact redirect-URI matching (port-agnostic only for Claude Code
      loopback, which `ytt` won't see via hosted surfaces).
- [ ] ✅ Validate **every** bearer token: signature/introspection + expiry + **audience
      (`resource`)** + scopes; 403 + `WWW-Authenticate: insufficient_scope` for step-up.
- [ ] ⚠️ Discovery/registration/token **≤10 s**, refresh **≤30 s**.
- [ ] ✅ Standard RFC 6749 error codes on the token/refresh endpoints (e.g. `invalid_grant`).
- [ ] ℹ️ You **only need the web/Desktop add flow to work** — mobile rides the same token and
      cannot add connectors, so there is **no separate mobile redirect/path** to build.

---

## Sources

- Authentication for connectors (official, callback URIs, DCR/CIMD/Anthropic-creds, S256,
  10 s/30 s timeouts, `160.79.104.0/21`): https://claude.com/docs/connectors/building/authentication
- Get started with custom connectors using remote MCP (add-flow UI, Advanced settings,
  plan/surface support): https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Building custom connectors via remote MCP servers (help center, add flow): https://support.claude.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers
- sunpeak — Claude Connector Authentication: How OAuth Works and When You Need It (May 2026)
  (discovery sequence, callback per surface, claude.com transition, mobile = no manual config,
  public-client none auth): https://sunpeak.ai/blogs/claude-connector-oauth-authentication/
- George Vetticaden — The Missing MCP Playbook: Deploying on Claude.ai and Claude Mobile
  (observed 4-step flow with Auth0 DCR, JWT/JWE handling, silent failures, mobile = same flow):
  https://medium.com/@george.vetticaden/the-missing-mcp-playbook-deploying-custom-agents-on-claude-ai-and-claude-mobile-05274f60a970
- Danila Loginov — Build Remote MCP with Authorization (packet-level: DCR POST
  `client_name: "claudeai"`, `redirect_uris: ["https://claude.ai/api/mcp/auth_callback"]`,
  metadata re-fetch): https://loginov-rocks.medium.com/build-remote-mcp-with-authorization-a2f394c669a8
- claude-ai-mcp issue #374 — Custom SSE connector, zero inbound packets from `160.79.104.0/21`
  (Anthropic backend is the connecting entity; reachability not OAuth):
  https://github.com/anthropics/claude-ai-mcp/issues/374
- claude-ai-mcp issue #313 — OAuth callback Method Not Allowed before /oauth/token:
  https://github.com/anthropics/claude-ai-mcp/issues/313
- GCIT — Claude MCP Server Blocked by Microsoft 365 Conditional Access (egress
  `160.79.104.0/21` IPv4 + `2607:6bc0::/48` IPv6, only Anthropic backend connects):
  https://gcit.com.au/knowledge-base/claude-mcp-server-blocked-conditional-access-policy/
- Mobile-text-alerts — Connect with Claude (mobile cannot add new connectors, only use web-added):
  https://developers.mobile-text-alerts.com/mcp-servers/actions-mcp-server/connect-with-claude
- Zuplo — Connect Claude Desktop and Claude.ai (Desktop opens system browser for consent):
  https://zuplo.com/docs/mcp-gateway/connect-clients/claude-desktop
- Casdoor — Connect Claude Desktop to MCP (local mcp-remote helper stores token locally — the
  *local-bridge* path, distinct from a hosted remote connector):
  https://casdoor.ai/docs/how-to-connect/mcp/connect-claude-desktop/
- MCP Authorization spec (baseline): https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
