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
- Inbound IP-allowlisting of Anthropic's egress ranges (`160.79.104.0/21`, `2607:6bc0::/48`) can only live at the **Cloudflare edge** (the origin pod can't see the client IP behind the tunnel) — as a **WAF custom rule**, never Access (an identity gate that would challenge Anthropic's unattended backend). It is **optional defense-in-depth, not a substitute for the subject allowlist**, and is **deliberately not adopted for now** — declined with the full rationale and adoption recipe on bead `ytt-761fb151` (no agent-editable WAF credential; zone-wide `http_request_firewall_custom` phase ownership risks unverifiable clobber on the shared host; the egress range drifts). Adopt only if the operator explicitly opts in, per `docs/plan/plan.md` ("only if explicitly chosen").

See `docs/research/mcp-oauth-authentication.md` for the spec details and exactly what the server must expose.

**Upstream IdP:** this doc is deliberately IdP-agnostic — the MCP-facing requirements above hold regardless of which upstream identity provider ytt federates to. The actual choice (currently the org's self-hosted Authentik, `sso.ardenone.com`; previously Google) is recorded as a decided ADR in `docs/plan/plan.md` (ADR-003, superseding an undocumented earlier pivot to Google) — check there for the current provider and the implementation in `ytt/auth.py`.

## Key rotation and token-validation failures

How validation behaves when keys change, tokens are malformed, or the IdP is unreachable. Every row below is pinned by a test in `tests/unit/test_oauth_conformance.py` (`TestInvalidSignatureRejection`, `TestTemporalClaimEnforcement`, `TestUpstreamSecretRotation`, `TestYTTSignedTokenRotation`, `TestJWKSPathFailClosed`, `TestDiscoveryOutageFailClosed`) — if an implementation change makes a row false, the pin fails on purpose.

### The two key families

| Key | Verifies | Lives in | Notes |
|---|---|---|---|
| `YTT_OAUTH_CLIENT_SECRET` (as verifier key) | upstream id_tokens — Authentik signs them with HS256 | OpenBao `ardenone-cluster/ytt/oauth` → `ytt-secrets` Secret | the reference IdP uses this secret as the HS256 key and publishes an **empty JWKS** (`{}`) |
| FastMCP token signing key | the tokens Claude actually presents (access **and** refresh) | **derived** from the client secret (HKDF, fixed salt) unless `YTT_JWT_SIGNING_SECRET` is set explicitly | derivation is deterministic, so tokens survive restarts |

Because the signing key is derived from the client secret by default, **rotating `YTT_OAUTH_CLIENT_SECRET` rotates both key families at once** — every issued token fails at the signature check the moment the new value deploys, and every client must re-authenticate (`TestYTTSignedTokenRotation.test_client_secret_rotation_rotates_the_derived_signing_key_too`). Set `YTT_JWT_SIGNING_SECRET` to decouple the two.

### Validation is offline — no JWKS, no cached keys

FastMCP's stock `OIDCProxy` verifier is JWKS-based. Against the reference IdP it can never work: the JWKS is empty, so every token would die with "No keys found in JWKS" — **fail closed, which is exactly why `ytt/auth.py` builds its own symmetric verifier** (`UpstreamIdTokenVerifier`) instead. Consequences of the symmetric design:

- Token validation performs **zero network I/O**: both keys come from settings, held in memory. An IdP outage after startup cannot invalidate a single existing session.
- There is **no cached-key staleness on this path**. A key change takes effect exactly when the new settings deploy — nothing to flush, no dual-key acceptance window (the verifier holds exactly one key).
- The JWKS path (required for any future RS256 IdP) is specified too, at `TestJWKSPathFailClosed`: an empty JWKS, an outage with a cold cache, or an unknown `kid` all reject (fail closed); a **cached key keeps verifying through an outage for at most the 1h cache TTL**, and a key removed from the live JWKS keeps validating up to that same hour. If an IdP switch ever makes this path live, that bounded staleness is the price of its cache.

### Failure modes — every one fails closed

| Token or event | Behavior |
|---|---|
| Expired (`exp` in the past) | rejected → 401 `invalid_token` |
| Not yet valid (`nbf` in the future) | rejected beyond a 60 s leeway (`UpstreamIdTokenVerifier`, RFC 7519 §4.1.5); within the leeway accepted so a token is never bounced for clock rounding |
| Missing `exp` | rejected — OIDC Core §2 requires it; a signed-but-immortal token gets no implicit infinite lifetime |
| Wrong issuer / wrong audience | rejected (the confused-deputy guard, pinned since the first conformance round) |
| Invalid signature — attacker-chosen key, tampered payload, `alg=none`, malformed/truncated/empty string | rejected, and the rejection is a `None`/`JoseError`, never a raised exception through the middleware — a malformed bearer is a 401, never a 500 |
| Empty JWKS / JWKS outage (RS256 path) | rejected; cached keys honored within the TTL only |
| IdP discovery endpoint down **at startup** | discovery is fetched eagerly at provider construction; construction raises and **the server never binds** — no unauthenticated ytt can come up |
| IdP down **mid-run** | only new logins and upstream refreshes break; existing tokens keep validating (offline keys). A failed transparent refresh just yields a 401, which drives the client's normal re-auth |

The two temporal checks FastMCP's verifier omits (`exp` mandatory, `nbf` honored) are ytt-level hardening in `UpstreamIdTokenVerifier`. Both only bite on a token whose HS256 signature is *valid* — i.e. minted by the IdP itself — so they close the misbehaving-or-misconfigured-IdP hole; signature, issuer and audience checks remain the boundary against everyone else.

### Operator recovery runbook

The full step-by-step rotation procedure — the OpenBao write recipe, the
two-pod restart sequencing, old/new token behavior in the window, outage
triage, rollback of each half independently, and the conformance-test drills
— is [deploy/AUTH-ROTATION-RUNBOOK.md](../../deploy/AUTH-ROTATION-RUNBOOK.md).
The notes below are the mechanics each rotation step leans on.

**Rotate the upstream client secret** (hygiene or suspected compromise):

1. Write the new `client_secret` into the single shared OpenBao path `secret/ardenone-cluster/ytt/oauth` — the value travels by pipe/stdin, never as a command-line argument, with `-cas=<current version>`. Both consumers read this one path (ytt's `ytt-externalsecret.yml` and Authentik's `authentik-oidc-clients-externalsecret.yml`), so they cannot drift.
2. Expect a **full logout**: the derived signing key changes with the secret, so all access and refresh tokens die at the signature check. Clients re-auth through the normal connector flow. Do it in a maintenance window.
3. External Secrets refreshes `ytt-secrets` within 1 h (`refreshInterval`), but env vars are read at container start — roll the Deployment via a declarative-config manifest change (bump the pod-template restart annotation; ArgoCD syncs it). Never a live `kubectl rollout restart`: ArgoCD owns the resource and `selfHeal` would fight it.
4. Order matters only for the *login* window: between Authentik and ytt flipping to the new secret, new logins fail (token signed under one key, verified against the other). That window is fail-closed (401), never fail-open — there is no fallback that accepts the old signature after rotation.

**Log everyone out without touching the IdP**: set or change `YTT_JWT_SIGNING_SECRET` and roll — instant global logout, IdP credential untouched. Keeping this set also makes future client-secret rotations session-preserving on the ytt side (upstream-signed tokens then die only at the next upstream refresh).

**IdP outage at startup**: the pod exits and restarts into the same failure until the IdP answers (CrashLoopBackOff is the *correct* posture here). Recovery is restoring the IdP; no ytt change.

**IdP outage mid-run**: nothing to do — sessions keep working; logins fail until the IdP returns. If the outage outlasts Authentik's refresh-token validity, users re-authenticate once it's back.
