# Auth: OAuth-secured under MCP OAuth

## Decision

`ytt` **must** be secured using MCP's native OAuth (OAuth 2.1 + PKCE), as required for remote MCP custom connectors in Claude. This is a hard requirement, not optional:

- The server is a **remote MCP server** reachable over public HTTPS (Anthropic's backend is the MCP client that calls it). A public, unauthenticated transcript endpoint is unacceptable.
- Auth is handled **at the MCP layer**, via the OAuth flow Claude drives when a user adds the custom connector — not via a hand-rolled token check bolted on the side.

## Authentication ≠ Authorization (critical)

OAuth proves a caller is **authenticated** (it's a real, signed-in identity); it does **not** prove they are **authorized** to use *this* server. On the public internet, any Claude user who learns the connector URL could complete the OAuth flow and then drive yt-dlp + CPU-Whisper through the household's home internet. **OAuth alone does not prevent abuse — it is not an "open relay" guard by itself.**

Authorization is therefore a separate, required control:

- **Subject allowlist (`YTT_ALLOWED_SUBJECTS`)** checked on every tool call after token validation; non-allowlisted subject → `403`. **Empty allowlist = deny all** (fail-closed).
- **Dynamic Client Registration disabled** for personal v1 (DCR lets anyone register). Authorize on the token **subject**, not the client name — the Claude apps register as `client_name: "claudeai"`, so never allowlist by an exact `"Claude"` string.
- **Per-subject rate limiting** + Whisper quota so even an allowlisted caller can't exhaust the home IP / shared Whisper service.

## What this means for the build

- Implement the OAuth 2.1 + PKCE flow the MCP spec mandates (authorization + protected-resource metadata discovery, token issuance, **audience-bound** bearer-token validation on every MCP request).
- Use the **manual Client ID / Secret** (or FastMCP self-issued tokens) registration path for personal use; **do not** enable open DCR. If DCR is ever needed for sharing, gate it behind a pre-shared registration token.
- Every tool call must pass **both** a valid validated access token (AuthN) **and** the subject allowlist (AuthZ).
- Inbound IP-allowlisting of Anthropic's egress ranges must live at **Cloudflare Access/WAF** (the origin pod can't see the client IP behind the tunnel), and is defense-in-depth — not a substitute for the subject allowlist.

See `docs/research/mcp-oauth-authentication.md` for the spec details and exactly what the server must expose.

**Upstream IdP:** this doc is deliberately IdP-agnostic — the MCP-facing requirements above hold regardless of which upstream identity provider ytt federates to. The actual choice (currently the org's self-hosted Authentik, `sso.ardenone.com`; previously Google) is recorded as a decided ADR in `docs/plan/plan.md` (ADR-003, superseding an undocumented earlier pivot to Google) — check there for the current provider and the implementation in `ytt/auth.py`.
