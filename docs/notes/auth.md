# Auth: OAuth-secured under MCP OAuth

## Decision

`ytt` **must** be secured using MCP's native OAuth (OAuth 2.1 + PKCE), as required for remote MCP custom connectors in Claude. This is a hard requirement, not optional:

- The server is a **remote MCP server** reachable over public HTTPS (Anthropic's backend is the MCP client that calls it). A public, unauthenticated transcript endpoint is unacceptable — it would be an open relay anyone could abuse.
- Auth is handled **at the MCP layer**, via the OAuth flow Claude drives when a user adds the custom connector — not via a hand-rolled token check bolted on the side.

## What this means for the build

- Implement the OAuth 2.1 + PKCE flow the MCP spec mandates (authorization + protected-resource metadata discovery, token issuance, bearer-token validation on every MCP request).
- Support at least the **manual Client ID / Secret** registration path (sufficient for personal use); prefer **Dynamic Client Registration (DCR)** if the connector will be shared.
- Every tool call (`get_youtube_transcript`) must be gated by a valid, validated access token.

See `docs/research/mcp-oauth-authentication.md` for the spec details and exactly what the server must expose.
