# Security Policy

## Supported versions

| Version | Status |
|---------|--------|
| 0.1.x   | Active |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email: `security@ardenone.com` (or the repo owner's contact listed on GitHub).

Include:
- A description of the vulnerability.
- Steps to reproduce.
- Potential impact assessment.
- Any suggested mitigations.

You will receive an acknowledgement within 48 hours and a full response within 7 days.

## Security posture

### Authentication and authorization

- **OAuth 2.1 with PKCE** — token validation is audience-bound to the full
  path-bearing `YTT_PUBLIC_URL` (e.g. `https://mcp.ardenone.com/ytt`).  A token
  issued for one tool cannot be replayed against another (audience isolation).
- **Subject allowlist (required)** — every tool call checks the token `sub` claim
  against `YTT_ALLOWED_SUBJECTS`.  An empty allowlist denies all requests (fail-closed).
  Setting `YTT_ALLOWED_SUBJECTS` is mandatory before the server is useful.
- **No DCR (Dynamic Client Registration)** — clients are statically registered.
  Enabling DCR in the future must be gated behind a pre-shared registration token.

### What is not protected by the server

- **Cloudflare WAF** — an optional host-level IP allowlist at `mcp.ardenone.com`
  can restrict access to Anthropic's IP range.  This is defense-in-depth only;
  the subject allowlist is the real per-tool authorization.
- **Residential egress** — yt-dlp fetches originate from the host machine's egress
  IP.  No YouTube credentials are used (no cookies, no account auth).  The
  constraint "no cookies ever" is enforced in code and has a unit test.

### Secrets handling

- Secrets (OAuth client secret, subject list, proxy URL) are injected via
  environment variables from OpenBao/ESO — never baked into the image.
- Structlog redacts `sub`, `email`, `token`, `secret`, `key`, `authorization`,
  transcript bodies, `audio_path`, `proxy_url`, and any credential-bearing URL
  (containing `@`) from all log output.
- `/ytt/health` is unauthenticated (liveness only, no sensitive fields).
- `/ytt/admin/egress` requires a valid Bearer token + subject in the allowlist.

### Abuse potential

This server provides yt-dlp caption extraction and Whisper ASR for YouTube videos
via the home internet connection.  The subject allowlist prevents open access.
If the allowlist is misconfigured (empty or overly broad), any OAuth user could
drive yt-dlp + CPU-Whisper through the connection.  Always configure
`YTT_ALLOWED_SUBJECTS` before exposing the server publicly.

### Dependency security

- `yt-dlp` is pinned exactly in `uv.lock`.  Update it regularly (yt-dlp tracks
  YouTube API changes and may fix security issues on each release).
- `ffmpeg` is the distro package from the base image (`python:3.12-slim`).
  Rebuild the image to pick up security updates.
