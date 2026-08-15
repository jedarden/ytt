# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.9] — 2026-08-15

### Fixed

- **0.2.8's `verify_id_token=True` fix was incomplete -- `invalid_token`
  persisted, identically, on the ID token this time.** Root cause:
  Authentik signs *every* OAuth2Provider's tokens on this instance --
  access token AND id token alike -- with HS256 (symmetric, keyed by the
  client_secret), confirmed by comparing against OpenBao's own working
  provider (identical `id_token_signing_alg_values_supported: ["HS256"]`,
  identically empty JWKS `{}`). This is a normal, spec-compliant OIDC
  configuration for confidential clients, not a misconfiguration --
  OpenBao's OIDC client just never tries to verify the signature via
  JWKS at all. `OIDCProxy.get_token_verifier()` unconditionally builds a
  JWKS-based `JWTVerifier` with no path for symmetric verification, so
  it could never work against this IdP regardless of which token
  (access or id) it pointed at. Fixed by constructing our own
  `JWTVerifier(public_key=<client_secret>, algorithm="HS256", ...)` --
  it explicitly supports shared-secret verification -- and passing it as
  `token_verifier=` to bypass the broken auto-construction.

## [0.2.8] — 2026-08-15

### Fixed

- **Every real connector auth 401'd on the very first tool call, after a
  fully successful OAuth dance.** Login, consent, Authentik token
  exchange, and FastMCP's own self-issued token all completed with
  200s -- then `POST /ytt` rejected the token as `invalid_token`
  immediately, same pod, no restart in between. Root cause (confirmed
  via `FASTMCP_LOG_LEVEL=DEBUG` live logs, not guessed): Authentik
  signs access tokens with HS256 (symmetric) per its own discovery
  document, so its JWKS endpoint correctly has no keys for it -- a
  shared HS256 secret can never be published there. `OIDCProxy`'s
  default behavior verifies the upstream *access* token via JWKS
  (assuming an asymmetric algorithm), which fails with "No keys found
  in JWKS" every time. Fixed with `verify_id_token=True` in
  `ytt/auth.py` -- only the ID token is meant to be independently
  verifiable this way. This also resolves 0.2.6's open question about
  where `email`/`email_verified` land (the ID token, not the access
  token).

## [0.2.7] — 2026-08-15

### Fixed

- **Authentik rejected every real connector login.** `OIDCProxy`'s
  default `forward_resource=True` relayed the RFC 8707 `resource`
  parameter Claude sends on `/authorize` through to Authentik's
  `/application/o/authorize/` call, which Authentik rejects outright
  (`error=invalid_request`, "The request is otherwise malformed") —
  the login never even reached Authentik's page. 0.2.6's ADR-003 note
  had ruled this out via static analysis of a FastMCP source comment
  that turned out not to describe Claude's actual behavior; confirmed
  live via Traefik access logs and fixed with `forward_resource=False`
  in `ytt/auth.py`.

## [0.2.6] — 2026-08-15

### Changed

- **OAuth federation: Google → self-hosted Authentik (ADR-003).**
  `ytt.auth.YttGoogleProvider` (FastMCP's `GoogleProvider`) replaced with
  `YttOIDCProvider` (FastMCP's generic `OIDCProxy`), which discovers its
  upstream endpoints from Authentik's per-application
  `.well-known/openid-configuration` document instead of hardcoding
  Google's. ytt now has its own Authentik application/client instead of
  sharing ibkr-mcp's GCP OAuth app. `YTT_OAUTH_CLIENT_ID`/
  `YTT_OAUTH_CLIENT_SECRET` now hold Authentik-issued values, sourced from
  a new consolidated OpenBao path (`ardenone-cluster/ytt/oauth`, keys
  `client_id`/`client_secret`) instead of the old two-path
  `oauth-client-id`/`oauth-client-secret` shape. See `docs/plan/plan.md`
  ADR-003 and `ytt/auth.py`'s module docstring.

### Fixed

- **This release was built by CI but never actually deployed for hours**
  — `declarative-config`'s `deployment.yml` kept referencing `0.2.5`
  after this code landed, so the live pod kept running the old
  Google-federated build. Caught when a real connector re-auth attempt
  redirected to Google Workspace instead of Authentik; confirmed via
  Traefik's access log (`/ytt/authorize` carrying
  `scope=openid+https://www.googleapis.com/auth/userinfo.email`, a
  Google-specific scope URI only the old provider produces).

## [0.2.5] — 2026-07-18

### Fixed

- **Caption-only extraction still failed for DRM-protected videos.**
  `allow_unplayable_formats` (0.2.4) cleared the DRM abort, but
  `extract_info()` still runs format selection even with
  `download=False`/`skip_download`, and with all-DRM formats it raised
  "Requested format is not available" — again bailing before ytt reads
  the caption tracks. We only ever want subtitles, so set
  `ignore_no_formats_error` to warn-and-continue; yt-dlp returns the info
  dict with `subtitles`/`automatic_captions` populated.

## [0.2.4] — 2026-07-17

### Fixed

- **DRM-protected videos falsely reported "no captions."** YouTube now
  DRM-protects many ordinary uploads (SABR). yt-dlp aborted `extract_info()`
  with "This video is DRM protected" (unless `allow_unplayable_formats`
  is set), which also killed caption retrieval even though caption tracks
  aren't DRM-encrypted and the caption path never downloads media
  (`skip_download=True`). Set `allow_unplayable_formats` so extraction
  proceeds past the DRM check.

## [0.2.3] — 2026-07-16

### Fixed

- **YouTube extraction failed systematically: every video returned "unavailable."**
  The 14-month-stale yt-dlp pin (2025.5.22) could no longer extract from
  YouTube's current player — every fetch returned error code 152, which ytt
  mislabeled as "no captions found" and turned into doomed Whisper ASR
  fallbacks (audio download failed the same way). Bumped to yt-dlp 2026.7.4
  to match today's YouTube player.

## [0.2.2] — 2026-07-16

### Changed

- **Authorization allowlist now supports case-insensitive matching and @domain
  wildcards.** `check_subject_auth` did a case-SENSITIVE exact match on the
  Google-verified email, and only exact emails were allowlistable. Two
  consequences hit in production: (1) an email returned as
  Me@jedcabanero.com failed against a lowercase allowlist entry, and (2)
  each Claude client that granted OAuth with a different Google account
  needed its exact email enumerated (both surfaced as "This connector has no
  tools available" — AuthMiddleware silently filters every tool a caller
  isn't authorized for, so `tools/list` returns empty). New `subject_allowed()`
  is case-insensitive and supports '@domain' entries (e.g. `@jedcabanero.com`)
  that admit any address in that exact domain (the leading '@' anchors the
  match so `evil-jedcabanero.com` and `sub.jedcabanero.com` do NOT match).
  Safe because callers gate on a Google-verified email.

## [0.2.1] — 2026-07-16

### Fixed

- **DCR/OAuth operational routes 404'd behind the path prefix.** The MCP
  SDK mounts `/register`, `/authorize`, `/token`, `/revoke`, and
  OAuthProxy's upstream-IdP callback (`/auth/callback`) at hardcoded bare
  paths (`mcp.server.auth.routes.AUTHORIZATION_PATH` etc. are not
  issuer-path-aware), even though the metadata these routes advertise
  (`registration_endpoint`, `authorization_endpoint`, ...) correctly uses
  the path-bearing issuer URL (`https://mcp.ardenone.com/ytt`). ytt's
  IngressRoute only forwards `PathPrefix("/ytt")` (plus the two well-known
  suffixes) to this service, so Claude's connector-add flow 404'd on
  `POST /ytt/register` ("Couldn't register with YouTube Transcript's
  sign-in service"). DCR was never exercised under the old
  InMemoryOAuthProvider design (had it disabled), so this predates and is
  independent of the 0.2.0 auth-provider swap — it just never got hit until
  now. `YttGoogleProvider.get_routes()` now mounts every non-well-known
  operational route a second time under the issuer path.

## [0.2.0] — 2026-07-15

### Fixed

- **Security: auth was not actually enforced on the tools.** `ytt/auth.py`
  used FastMCP's `InMemoryOAuthProvider` — an explicit test/demo provider
  (its own docstring: "simulates user authorization") that auto-approves any
  caller with no login step and issues opaque tokens with no `sub`/`email`
  claim. `YTT_ALLOWED_SUBJECTS` was also never checked on the actual tool
  calls — the allowlist check existed but was wired only into the
  `/admin/egress` diagnostic route. Combined with public (`websecure`)
  exposure, this meant any Claude user who discovered
  `https://mcp.ardenone.com/ytt` could call the transcript tools regardless
  of the allowlist. Replaced with FastMCP's `GoogleProvider` (federates to
  Google OAuth, same identity model as ibkr-mcp) and wired the allowlist
  check into global `AuthMiddleware`, covering every tool call.
- `/admin/egress` also independently 403'd every caller, including allowed
  ones, and 500'd instead of returning a clean 401/403 for bad tokens — it
  decoded the (non-JWT) bearer token as a JWT, passed the wrong type to the
  allowlist check, and inverted the raise/return contract of that check.
  Rewritten to use `fastmcp.server.dependencies.get_access_token()`.
- CI (`ytt-build`) never actually ran — it cloned from
  `github.com/jedarden/ytt`, which didn't exist (Forgejo push-mirror was
  never set up), and pushed to `ghcr.io/jedarden/ytt`, not the
  `ronaldraygun/ytt` Docker Hub image actually referenced by the deployment
  manifest. Set up the Forgejo → GitHub push-mirror and repointed CI at
  Docker Hub with semver `VERSION`-file tagging, matching every other app in
  this fleet.
- `YTT_CACHE_MAX_BYTES=2Gi` exceeded the PVC's actual usable capacity
  (ext4 reserved blocks report less than the nominal request), tripping the
  app's own startup validation on every boot — `CrashLoopBackOff` for
  5d20h/1636 restarts before this fix.

## [0.1.0] — 2025-xx-xx

Initial release.

### Added

- `get_youtube_transcript` MCP tool — fetches transcript for any YouTube URL
  (watch, youtu.be, /shorts/, /live/, bare video ID, &list= stripped).
- `get_transcript_job` MCP tool — polls Whisper ASR job; returns transcript when done.
- Rolling-caption dedup — eliminates the #1 silent bug in auto-captions (duplicate
  text in rolling tracks).
- Chunk pagination — inline for short videos; `next_cursor` + loud PARTIAL prefix
  for long videos.
- `start`/`end`/`query` transcript filtering.
- Per-subject OAuth 2.1 + PKCE with audience-bound token validation.
- Subject allowlist (`YTT_ALLOWED_SUBJECTS`) — fail-closed (empty = deny all).
- Per-subject rate limiting (token bucket) + Whisper quota.
- Flat-file LRU transcript cache with configurable byte cap and ENOSPC degrade.
- Single-flight dedup — concurrent same-video requests share one yt-dlp call.
- Whisper ASR fallback — async job FSM with ETA, TTL GC, scratch audio cleanup.
- `ytt selftest` egress probe — reports IP/ASN/org/is_residential.
- `ytt selftest --show-sub` — discovers the OAuth `sub` for allowlist setup.
- `ytt test [--unit|--integration]` — test runner with JSON summary.
- Prometheus metrics (`/ytt/metrics`) with labelled counters/gauges/histograms.
- `PrometheusRule` alerts: IP burned, Whisper down, cache undersized, egress changed, canary failed.
- Canary Deployment — yt-dlp caption probe every 10 min, metrics on :8081.
- `structlog` JSON logging with redaction filter (tokens, subjects, transcript bodies).
- `/ytt/admin/egress` — auth-gated egress diagnostics.
- K8s manifests for `ardenone-cluster` co-hosted with `ibkr-mcp` (additive, do-no-harm).
- Argo Workflows CI (`ytt-build`) — pytest gate + kaniko build + GHCR push + tag bump.
- Property-based tests for Invariants 1–6 (Hypothesis).
- Integration test harness for 22 in-cluster scenarios.
- Public GHCR image: `ghcr.io/jedarden/ytt:0.1.0`.

[Unreleased]: https://github.com/jedarden/ytt/compare/v0.2.7...HEAD
[0.2.7]: https://github.com/jedarden/ytt/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/jedarden/ytt/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/jedarden/ytt/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/jedarden/ytt/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/jedarden/ytt/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/jedarden/ytt/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jedarden/ytt/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jedarden/ytt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jedarden/ytt/releases/tag/v0.1.0
