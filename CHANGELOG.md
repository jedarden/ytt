# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jedarden/ytt/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/jedarden/ytt/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jedarden/ytt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jedarden/ytt/releases/tag/v0.1.0
