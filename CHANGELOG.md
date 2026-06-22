# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jedarden/ytt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jedarden/ytt/releases/tag/v0.1.0
