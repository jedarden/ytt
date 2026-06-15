# OAuth Authentication for Remote MCP Servers (Claude Custom Connectors)

> Research compiled 2026-06-14 for the `ytt` project. Target: a remote MCP server that
> must be OAuth-secured so it can be added as a Claude custom connector (Claude web,
> desktop, mobile, Cowork) and/or used via the Messages API MCP connector.
> Spec baseline: MCP Authorization spec, current revisions **2025-06-18** and
> **2025-11-25** (Claude supports `2025-03-26`, `2025-06-18`, `2025-11-25`).

## Summary

To be addable as an OAuth-secured Claude custom connector, your MCP server must act as an
**OAuth 2.1 Resource Server**. Concretely it must:

1. Reject unauthenticated requests with **HTTP 401** and a `WWW-Authenticate: Bearer`
   header carrying a `resource_metadata` URL (RFC 9728).
2. Serve **Protected Resource Metadata** (RFC 9728) at
   `/.well-known/oauth-protected-resource` (and/or the path-aware variant), containing at
   minimum a `resource` identifier and an `authorization_servers` list.
3. Point at an **Authorization Server** (its own, or a third party such as Auth0 / WorkOS /
   Descope) that publishes **Authorization Server Metadata** (RFC 8414) at
   `/.well-known/oauth-authorization-server`, advertises `code_challenge_methods_supported:
   ["S256"]` (PKCE is mandatory), and supports at least one client-registration mechanism.
4. **Validate every bearer token** on every request: signature/introspection, expiry,
   **and audience** (the token must have been issued *for this server*, per RFC 8707).

The lowest-effort path is to delegate the Authorization Server role to a managed IdP that
supports Dynamic Client Registration (DCR) or Client ID Metadata Documents (CIMD), and use
a framework that auto-generates the metadata endpoints — e.g. **FastMCP**
(`RemoteAuthProvider` / `OAuthProxy`) or the **MCP Python SDK**. The Rust `rmcp` SDK only
implements the *client* side today, so a Rust server must hand-roll the resource-server
endpoints or sit behind a gateway.

---

## 1. Which OAuth standard MCP mandates, and why

MCP authorization is built on **OAuth 2.1** (IETF draft `draft-ietf-oauth-v2-1`), not
legacy OAuth 2.0. The spec composes a selected subset of these RFCs/drafts:

- **OAuth 2.1** — Authorization Code grant with **PKCE required for all clients**.
- **RFC 6750** — Bearer Token Usage (`Authorization: Bearer`, `WWW-Authenticate`).
- **RFC 8414** — Authorization Server Metadata.
- **RFC 9728** — Protected Resource Metadata.
- **RFC 7591** — Dynamic Client Registration (now *deprecated* in the spec, kept for
  backwards-compat).
- **RFC 8707** — Resource Indicators (`resource` parameter → audience binding).
- **RFC 9207** — Authorization Server Issuer Identification (`iss` in responses).
- **CIMD** draft (`draft-ietf-oauth-client-id-metadata-document-00`) — Client ID Metadata
  Documents.
- **OpenID Connect Discovery 1.0** — accepted as an alternative AS discovery mechanism.

**Why OAuth 2.1 + PKCE:** MCP clients are frequently public clients (desktop/CLI/mobile)
that cannot keep a client secret. OAuth 2.1 mandates **PKCE (S256) for every client type**
(not just public ones), drops the implicit grant entirely, and requires **exact redirect
URI matching**. PKCE binds the authorization code to the originating client via a
verifier/challenge pair, defeating code-interception/injection. This gives MCP a secure
default without per-deployment hardening.

> Note: MCP authorization is **OPTIONAL** in the spec and applies to **HTTP transports**
> only. STDIO servers pull credentials from the environment instead. Since a Claude custom
> connector is a remote HTTP server, this entire document applies.

---

## 2. End-to-end MCP authorization flow

Roles:
- **MCP server** = OAuth 2.1 **Resource Server** (validates tokens, serves protected
  resource metadata).
- **MCP client** (Claude) = OAuth 2.1 **client**.
- **Authorization Server (AS)** = issues tokens; may be co-hosted with the MCP server or a
  separate third party.

Flow:

1. **Unauthenticated request.** Client sends an MCP request with no token.
2. **401 challenge.** Server returns `401 Unauthorized` with
   `WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", scope="..."`.
3. **Protected Resource Metadata discovery (RFC 9728).** Client fetches the
   `resource_metadata` URL (or falls back to well-known probing). It reads `resource` and
   `authorization_servers`.
4. **Authorization Server Metadata discovery (RFC 8414 / OIDC Discovery).** Client takes an
   entry from `authorization_servers` and fetches that AS's metadata
   (`/.well-known/oauth-authorization-server`, falling back to
   `/.well-known/openid-configuration`). It validates `issuer` matches.
5. **Client registration.** Client obtains a `client_id` via CIMD, pre-registration, or DCR
   (§3).
6. **Authorization request (browser).** Client generates PKCE params, records the expected
   `issuer` and `state`, and opens the browser at `authorization_endpoint` with
   `response_type=code`, `code_challenge`, `code_challenge_method=S256`, `redirect_uri`,
   `scope`, and the **`resource` parameter (RFC 8707)** identifying this MCP server.
7. **User consent → redirect with code** (and `iss` per RFC 9207). Client validates `iss`
   against the recorded issuer **before** using the code.
8. **Token exchange.** Client POSTs to `token_endpoint` with `grant_type=authorization_code`,
   `code`, `code_verifier`, and again the `resource` parameter. Gets an access token
   (`Bearer`), optionally a refresh token.
9. **Authenticated MCP requests.** Client sends `Authorization: Bearer <token>` on **every**
   HTTP request. Server validates and serves responses.
10. **Step-up / refresh.** Insufficient scope → `403` + `WWW-Authenticate:
    error="insufficient_scope", scope="..."`; client re-authorizes with the union of old +
    new scopes. Expired token → refresh or re-auth.

**Note on the Messages API MCP connector:** When you connect via the Anthropic Messages API
(`mcp_servers[].authorization_token`), Anthropic does **not** drive the OAuth dance — the
API caller obtains and refreshes the access token out of band (e.g. via the MCP Inspector
or your own flow) and passes it in `authorization_token`. The full discovery/flow above is
what the *interactive Claude clients* (claude.ai / Desktop / mobile / Cowork / Claude Code)
perform automatically when a user adds the connector.

---

## 3. Client-registration approaches Claude supports

The MCP spec defines three mechanisms; Claude supports all three. Spec priority order for a
client that supports everything: **pre-registration → CIMD → DCR → prompt the user**.

### a) Dynamic Client Registration (DCR, RFC 7591)
- AS exposes a `registration_endpoint`; clients `POST /register` and get back a `client_id`
  (and possibly secret) with no human in the loop.
- **Claude supports DCR out of the box.** If your AS advertises `registration_endpoint`,
  Claude can self-register.
- **Spec status: deprecated** (kept for backwards compat). Still the most common path with
  IdPs that don't yet do CIMD.
- Note: native vs web `application_type` affects redirect-URI constraints under OIDC. Claude
  hosted surfaces use a fixed `https://claude.ai/api/mcp/auth_callback` redirect; Claude
  Code uses loopback redirects (see §8).
- **Use when:** your AS supports DCR and you don't want to manage client credentials. Easiest
  for a brand-new self-hosted AS or a managed IdP (WorkOS AuthKit, Descope, modern OIDC).

### b) Client ID Metadata Documents (CIMD)
- The client uses an **HTTPS URL as its `client_id`**; that URL serves a JSON metadata
  document (`client_id`, `client_name`, `redirect_uris`, …). The AS fetches and validates it
  on demand.
- **Preferred by the spec** for the common "no prior relationship" case — no registration
  endpoint, no stored per-client state, and `client_id`s are portable across AS's.
- **Claude supports CIMD out of the box**, but only uses it when your AS advertises **both**
  `"client_id_metadata_document_supported": true` **and** `"none"` in
  `token_endpoint_auth_methods_supported` in its AS metadata.
- AS requirements: fetch URL-form `client_id`s, verify the document's `client_id` equals the
  URL exactly, validate `redirect_uris`, cache per HTTP cache headers.
- **Use when:** you control the AS and want the modern, registration-free path; you must
  implement the CIMD-fetch logic in your AS.

### c) Manual / pre-registered Client ID + Secret ("Advanced settings")
- In the Claude connector UI, **Advanced settings** lets the user paste an **OAuth Client
  ID** and **OAuth Client Secret** that you issued out of band.
- There is also an Anthropic-managed variant: contact **`mcp-review@anthropic.com`** to have
  Anthropic securely hold your `client_id`/`client_secret` and perform token exchange on
  users' behalf.
- **Use when:** your AS does **not** support DCR or CIMD (e.g. you wrap GitHub/Google/Azure,
  which require pre-registered apps), or you want tight control over which client connects.
  You must pre-register Claude's redirect URI(s) (§8) in your AS app config.

---

## 4. What the MCP *server* must expose (exact endpoints & headers)

### 4.1 `WWW-Authenticate` on 401 (mandatory behavior)
On any unauthenticated/invalid-token request, return **HTTP 401** with:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource",
                         scope="files:read"
```

- `resource_metadata` (RFC 9728 §5.1) **must** point at your Protected Resource Metadata doc.
  This is the single most important interop detail — claude.ai's connector effectively
  requires it (Claude Code tolerates its absence; the hosted connector does not).
- `scope` is optional but recommended — it tells Claude exactly which scopes to request.

### 4.2 Protected Resource Metadata — RFC 9728 (mandatory)
Serve a JSON document at the well-known path. Two acceptable locations:

- **Root:** `https://mcp.example.com/.well-known/oauth-protected-resource`
- **Path-aware** (when the MCP endpoint has a path, e.g. `/mcp`):
  `https://mcp.example.com/.well-known/oauth-protected-resource/mcp`

Clients use the `resource_metadata` URL from the 401 if present, else probe the path-aware
variant, then the root.

Required/important fields:

```json
{
  "resource": "https://mcp.example.com/mcp",
  "authorization_servers": ["https://auth.example.com"],
  "scopes_supported": ["files:read", "files:write"],
  "bearer_methods_supported": ["header"]
}
```

- `resource` — the **canonical URI of this MCP server** (must match the `resource` parameter
  clients send, per RFC 8707). Use the form without a trailing slash; no fragment. Examples:
  `https://mcp.example.com`, `https://mcp.example.com/mcp`, `https://mcp.example.com:8443`.
- `authorization_servers` — **at least one** AS issuer URL. The client picks one.
- `scopes_supported` — the minimal scopes for basic functionality (least privilege).
- Do **not** advertise `offline_access` in `scopes_supported` / `WWW-Authenticate` scope —
  refresh tokens are not a resource requirement.

### 4.3 Authorization Server Metadata — RFC 8414 (mandatory, on the AS)
Your AS (own or third-party) must publish metadata at one of:

- `https://auth.example.com/.well-known/oauth-authorization-server` (RFC 8414), and/or
- `https://auth.example.com/.well-known/openid-configuration` (OIDC Discovery)

(For issuer URLs with a path, the well-known suffix is inserted after the host, e.g.
`/.well-known/oauth-authorization-server/tenant1`.)

Key fields the AS must expose:

```json
{
  "issuer": "https://auth.example.com",
  "authorization_endpoint": "https://auth.example.com/authorize",
  "token_endpoint": "https://auth.example.com/token",
  "registration_endpoint": "https://auth.example.com/register",
  "scopes_supported": ["files:read", "files:write", "offline_access"],
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
  "client_id_metadata_document_supported": true,
  "authorization_response_iss_parameter_supported": true
}
```

- `code_challenge_methods_supported` **must** include `"S256"` — Claude sends a PKCE
  `code_challenge` with `code_challenge_method=S256` on every authorization request.
- `registration_endpoint` is required only if you support DCR.
- `client_id_metadata_document_supported: true` + `"none"` in
  `token_endpoint_auth_methods_supported` are what make Claude use CIMD.
- `authorization_response_iss_parameter_supported: true` if you emit `iss` (RFC 9207,
  recommended; likely to become mandatory).
- `token_endpoint` must accept `Content-Type: application/x-www-form-urlencoded`.

> **Timeouts:** Claude enforces ~10s for discovery/registration/token endpoints and ~30s for
> refresh. Keep these endpoints fast.

---

## 5. Own AS vs delegating to a third-party AS

The MCP server (Resource Server) and the Authorization Server are **separate roles** that
may or may not be the same process. Both are valid.

**Delegate to a third-party AS (recommended for most teams).** Point
`authorization_servers` at a managed IdP (Auth0, WorkOS AuthKit, Descope, Okta, Keycloak,
Cognito, Entra ID, etc.). The IdP handles login, consent, token issuance, discovery
metadata, and (often) DCR/CIMD. Your MCP server only validates tokens and serves the
Protected Resource Metadata.

- **Pros:** no auth code to write/secure; user management, MFA, consent UI, key rotation
  handled; PKCE/2.1 compliance out of the box; some IdPs already do DCR (WorkOS, Descope) so
  Claude self-registers.
- **Cons:** you must map the IdP's tokens/audiences/scopes to your resource; IdPs *without*
  DCR (GitHub, Google, Azure, AWS, Discord) need an **OAuth Proxy** shim or pre-registered
  client (manual mode), because Claude expects DCR/CIMD or manual credentials.
- **Token-type caveat:** validate the IdP's **audience** so a token minted for some other
  resource cannot be replayed against your server (the "confused deputy" risk the spec calls
  out).

**Be your own AS.** Co-host the AS with the MCP server (or run a small dedicated AS).

- **Pros:** full control over scopes, consent, token format, and the `resource`/audience
  binding; can implement CIMD exactly as you want; single deployment.
- **Cons:** you own all OAuth 2.1 security (PKCE, exact redirect matching, code single-use,
  mix-up/CSRF/open-redirect defenses, refresh-token rotation, key management) — significant
  surface area to get right.

**Rule of thumb for `ytt`:** if you already have or can stand up a managed IdP with DCR,
delegate. If not, either (a) use a framework's OAuth Proxy to wrap a non-DCR IdP, or (b) be
your own minimal AS via a framework that implements it for you (see §7). Avoid hand-rolling
an AS from scratch.

---

## 6. Presenting and validating bearer tokens

**How tokens arrive (client → server):**
- `Authorization: Bearer <access-token>` header on **every** HTTP request (OAuth 2.1 §5.1.1
  / RFC 6750). Never in a query string.

**What the server (Resource Server) must validate (OAuth 2.1 §5.2):**
1. **Signature / validity** — JWT: verify against the AS's JWKS (`jwks_uri`); opaque token:
   call the AS's introspection endpoint (RFC 7662).
2. **Expiry** — reject expired tokens with 401.
3. **Audience binding (critical)** — confirm the token was issued **for this MCP server**
   (`aud` / resource indicator matches your canonical `resource` URI, per RFC 8707). The
   server **must not** accept tokens minted for any other resource, and must not forward
   ("transit") tokens onward.
4. **Scopes** — if the operation needs a scope the token lacks, return **403** with
   `WWW-Authenticate: Bearer error="insufficient_scope", scope="...",
   resource_metadata="..."` so Claude can step up.

**Error codes:**
- `401` — missing/invalid/expired token (also the initial discovery challenge).
- `403` — valid token but insufficient scope/permission.
- `400` — malformed authorization request.

**Refresh:** Claude refreshes reactively on 401 and proactively up to ~5 min before expiry,
appending `offline_access` only if your AS metadata lists it. Return standard RFC 6749 error
codes (e.g. `invalid_grant`), not custom ones. For public clients, rotate or
sender-constrain refresh tokens per OAuth 2.1.

---

## 7. Minimal-implementation guidance (libraries/frameworks)

Don't hand-roll the resource-server plumbing if you can avoid it.

### FastMCP (Python) — strongest batteries-included option
- **`RemoteAuthProvider`** (≥ 2.11.1): compose a `TokenVerifier` with your AS info; FastMCP
  auto-generates `/.well-known/oauth-protected-resource` and the 401/`WWW-Authenticate`
  behavior, and can forward `/.well-known/oauth-authorization-server`. Use when your IdP
  **supports DCR** (WorkOS AuthKit, Descope, modern OIDC).
- **`OAuthProxy`**: use when your IdP **lacks DCR** (GitHub, Google, Azure, AWS, Discord). It
  presents a DCR-capable face to Claude while holding fixed upstream credentials.
- **`TokenVerifier` subclasses:** `JWTVerifier` (validate JWTs via `jwks_uri`, with `issuer`
  + `audience` checks), `IntrospectionTokenVerifier` (RFC 7662 opaque tokens).
- **Provider shortcuts:** e.g. `AuthKitProvider(authkit_domain=..., base_url=...)` for WorkOS
  (handles JWT validation + both metadata documents).
- Knobs: `scopes_supported`, `allowed_client_redirect_uris` (restrict DCR redirect URIs),
  `get_routes()` override for custom endpoints. Set `base_url` to the server root, not the
  `/mcp` path.

Minimal example:
```python
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

verifier = JWTVerifier(
    jwks_uri="https://auth.example.com/.well-known/jwks.json",
    issuer="https://auth.example.com",
    audience="https://mcp.example.com/mcp",   # audience-bind to THIS server
)
auth = RemoteAuthProvider(
    token_verifier=verifier,
    authorization_servers=[AnyHttpUrl("https://auth.example.com")],
    base_url="https://mcp.example.com",
)
mcp = FastMCP(name="ytt", auth=auth)
```

### MCP Python SDK (`mcp`)
- Has built-in auth support for the server side (token verification + the discovery/metadata
  endpoints) along the same RFC 9728/8414 lines. Good if you want the reference SDK rather
  than FastMCP's higher-level providers.

### Rust (`rmcp`, the official MCP Rust SDK)
- **Client-side OAuth only today.** `rmcp` implements the OAuth 2.1 + PKCE *client* flow
  (`AuthorizationManager`, `OAuthState`, `AuthClient`, RFC 8414/7591 discovery + DCR). It
  does **not** ship server-side resource-server helpers (no built-in protected-resource
  metadata serving, 401/`WWW-Authenticate`, or token validation).
- For a Rust MCP **server**, you must implement the resource-server endpoints yourself
  (validate JWT via a crate like `jsonwebtoken` + JWKS, serve the two well-known docs, emit
  the 401 challenge), or front the server with a gateway/IdP that does it. `tower-mcp` is
  tracking OAuth 2.1 support but is not a turnkey solution yet.

### Managed IdPs / gateways
WorkOS AuthKit, Descope, Scalekit, Auth0, Stytch, and Cloudflare Access "Managed OAuth" all
publish how to front an MCP server. These remove most custom code; verify the specific one
emits a 401 + `WWW-Authenticate: resource_metadata` and supports DCR or CIMD (some don't —
see §8).

---

## 8. Common pitfalls / gotchas (from real Claude connector builders)

1. **Missing `WWW-Authenticate` header on 401.** Claude Code tolerates a bare 401; the
   **claude.ai / Desktop / mobile connector requires** the `WWW-Authenticate: Bearer
   resource_metadata="..."` challenge. If it's absent, discovery never starts and the
   connector fails to add. This is the #1 reported failure.
2. **Wrong redirect URI / not pre-registering Claude's callbacks.** Hosted Claude surfaces
   redirect to **`https://claude.ai/api/mcp/auth_callback`** (also register the
   **`https://claude.com/api/mcp/auth_callback`** variant). The OAuth client name Claude
   presents is **"Claude"**. Do **not** expect `localhost` callbacks from hosted Claude —
   those are **Claude Code** only (RFC 8252 loopback on an ephemeral port; Claude Code's CIMD
   declares `http://localhost/callback` and `http://127.0.0.1/callback`, so your AS must do
   **port-agnostic** matching for the loopback case). In manual/pre-registered mode you must
   add Claude's redirect URIs to your AS app config or you'll get `invalid_request` /
   `redirect_uri` mismatch.
3. **CIMD not triggering.** Claude only uses CIMD if your AS metadata advertises **both**
   `client_id_metadata_document_supported: true` **and** `"none"` in
   `token_endpoint_auth_methods_supported`. Miss either and Claude falls back to DCR or
   manual.
4. **Missing PKCE S256 advertisement.** Claude always sends `code_challenge_method=S256`; if
   your AS metadata omits `code_challenge_methods_supported: ["S256"]` (or the AS rejects
   PKCE), authorization fails.
5. **Token not bound to the chat session / missing on tool calls.** Reported bug class
   (Cowork/connector): the connector authenticates but subsequent tool calls arrive with
   **no Authorization header**, hitting your 401, and the in-chat OAuth retry can't complete
   because PKCE/flow state doesn't survive between chat turns. Make sure your 401 + metadata
   are byte-stable and idempotent so re-auth can succeed; this is partly an Anthropic-side
   issue but a flaky/slow discovery endpoint makes it worse.
6. **Method Not Allowed on OAuth endpoints.** Some servers return 405 on the callback/token
   route (e.g. only handling POST vs GET, or a trailing-slash mismatch) before
   `/oauth/token` is reached. Honor the exact methods/paths from your published metadata.
7. **Audience not validated → confused-deputy risk.** Accepting any valid-looking token
   (without checking it was issued for *your* `resource`) lets a token for another service be
   replayed against you. Always enforce audience (RFC 8707).
8. **Gateways that don't speak MCP discovery.** Some "managed OAuth" front-ends (e.g. certain
   Cloudflare Access configs) work with Claude Code but **fail with claude.ai web/mobile** on
   the identical URL — usually because the 401/`WWW-Authenticate`/metadata shape isn't what
   the hosted connector expects. Test against the hosted surface, not just Claude Code/MCP
   Inspector.
9. **Endpoint timeouts.** Discovery/registration/token must respond well under ~10s (refresh
   ~30s) or you get intermittent connector failures.
10. **Firewall / IP allowlisting.** Anthropic connects from its **cloud** (not the user's
    device) across all surfaces. Your server and AS must be reachable from Anthropic's
    egress range — currently **`160.79.104.0/21`** — allowlist it on any WAF/conditional
    access.
11. **`offline_access` placement.** Don't put `offline_access` in the resource's
    `scopes_supported` or the 401 `scope`; put it (if you issue refresh tokens) in the **AS**
    `scopes_supported` so Claude appends it during token requests.

### Quick checklist for the `ytt` server
- [ ] 401 on no/invalid token, with `WWW-Authenticate: Bearer resource_metadata="…", scope="…"`.
- [ ] `/.well-known/oauth-protected-resource` (+ path variant) with `resource`,
      `authorization_servers`, `scopes_supported`.
- [ ] AS metadata with `S256`, endpoints, and (CIMD) `client_id_metadata_document_supported`
      + `"none"` auth method, **or** a `registration_endpoint` (DCR), **or** pre-registered
      client + Claude's redirect URIs.
- [ ] Register `https://claude.ai/api/mcp/auth_callback` and
      `https://claude.com/api/mcp/auth_callback` (manual/pre-registered mode).
- [ ] Validate every token: signature/introspection + expiry + **audience** + scopes.
- [ ] Discovery/token endpoints fast (<10s); allowlist `160.79.104.0/21`.

---

## Sources

- MCP Authorization spec (core): https://modelcontextprotocol.io/specification/draft/basic/authorization
- MCP Authorization Server Discovery (RFC 9728 / 8414 details): https://modelcontextprotocol.io/specification/draft/basic/authorization/authorization-server-discovery
- MCP Client Registration (CIMD / DCR / pre-registration): https://modelcontextprotocol.io/specification/draft/basic/authorization/client-registration
- MCP spec version pinned by Claude (2025-11-25): https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- Claude connector OAuth authentication reference: https://claude.com/docs/connectors/building/authentication
- Claude "Building custom connectors" guide (moved): https://claude.com/docs/connectors/building
- Claude help center — Get started with custom connectors using remote MCP: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Claude help center — Building custom connectors via remote MCP servers: https://support.claude.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers
- Claude help center — Use connectors to extend Claude's capabilities: https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities
- Anthropic platform docs — MCP connector (Messages API): https://platform.claude.com/docs/en/agents-and-tools/mcp-connector
- FastMCP — Remote OAuth (RemoteAuthProvider / OAuthProxy / TokenVerifier): https://gofastmcp.com/servers/auth/remote-oauth
- FastMCP issue — Full OAuth 2.1 Authorization for FastMCP Servers: https://github.com/jlowin/fastmcp/issues/825
- FastMCP issue — OAuth works with Inspector but not Claude Integrations: https://github.com/jlowin/fastmcp/issues/972
- Rust SDK OAuth support doc (client-only): https://github.com/modelcontextprotocol/rust-sdk/blob/main/docs/OAUTH_SUPPORT.md
- Rust SDK OAuth 2.1 implementation (DeepWiki): https://deepwiki.com/modelcontextprotocol/rust-sdk/4.1-oauth-2.1-implementation
- Auth0 — An Introduction to MCP and Authorization: https://auth0.com/blog/an-introduction-to-mcp-and-authorization/
- Descope — Diving Into the MCP Authorization Specification: https://www.descope.com/blog/post/mcp-auth-spec
- Aembit — MCP, OAuth 2.1, PKCE, and the Future of AI Authorization: https://aembit.io/blog/mcp-oauth-2-1-pkce-and-the-future-of-ai-authorization/
- Scalekit — Securing FastMCP with Scalekit (Remote OAuth): https://www.scalekit.com/blog/securing-fastmcp-with-scalekit
- claude-ai-mcp issue #412 — connector auth not bound to chat session: https://github.com/anthropics/claude-ai-mcp/issues/412
- claude-ai-mcp issue #313 — OAuth callback Method Not Allowed before /oauth/token: https://github.com/anthropics/claude-ai-mcp/issues/313
- claude-ai-mcp issue #410 — claude.ai web/mobile fails vs Cloudflare Access while Claude Code works: https://github.com/anthropics/claude-ai-mcp/issues/410
- claude-ai-mcp issue #199 — Custom connector fails with start_error before reaching OAuth: https://github.com/anthropics/claude-ai-mcp/issues/199
- RFC 9728 (Protected Resource Metadata): https://datatracker.ietf.org/doc/html/rfc9728
- RFC 8414 (Authorization Server Metadata): https://datatracker.ietf.org/doc/html/rfc8414
- RFC 7591 (Dynamic Client Registration): https://datatracker.ietf.org/doc/html/rfc7591
- RFC 8707 (Resource Indicators): https://www.rfc-editor.org/rfc/rfc8707.html
- RFC 9207 (Authorization Server Issuer Identification): https://datatracker.ietf.org/doc/html/rfc9207
- OAuth Client ID Metadata Document draft: https://datatracker.ietf.org/doc/html/draft-ietf-oauth-client-id-metadata-document-00
- OAuth 2.1 draft: https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13
