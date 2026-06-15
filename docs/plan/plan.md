# ytt Plan

## Overview

A remote MCP server that reliably downloads transcripts from pasted YouTube links, usable as a custom connector in both Claude mobile (iOS/Android) and Claude desktop.

## Architecture

The system splits into an easy half (MCP/transport plumbing) and a hard half (reliable transcript fetching). The hard half is where nearly all the engineering risk lives.

```
Claude mobile/desktop
   │  (custom connector, Streamable HTTP, OAuth 2.1 + PKCE)
   ▼
Anthropic backend (MCP client)
   │  public HTTPS — NOT Tailscale-only
   ▼
Cloudflare Tunnel ──► ytt MCP server (cluster service)
   │
   ▼
Fetch core (fallback ladder)
   1. caption track via youtube-transcript-api / yt-dlp  ──► rotating residential proxy
   2. no captions / blocked ──► yt-dlp audio ──► Whisper transcription
   3. cache by video ID
```

### Transport decision: remote MCP, not stdio

- Claude mobile cannot run a local process, so stdio servers (`claude_desktop_config.json`) are desktop-only.
- To cover **both** mobile and desktop with one server, build a **remote MCP server over Streamable HTTP**, added as a "custom connector."
- The server must be **publicly reachable over HTTPS** — Anthropic's backend is the MCP client that calls it, not the phone. A Tailscale-only endpoint will not work. Use a **Cloudflare Tunnel** (or public Traefik ingress) in front of a cluster service.
- Auth is **OAuth 2.1 + PKCE**. Options: manual Client ID/Secret (least work, personal use), Dynamic Client Registration (DCR, best if shared), or Client ID Metadata Documents (CIMD).

### The reliability problem (2026 reality)

- YouTube **blocks datacenter IPs** (AWS/GCP/Azure/Hetzner/Rackspace) — `youtube-transcript-api` and `yt-dlp` throw `RequestBlocked`/`IpBlocked` from any cloud host, which is exactly where the remote server runs.
- **PoToken** (proof-of-origin bot detection, 2025–26) makes scripted caption fetches harder.
- Community-consensus fix: **rotating residential proxies** (Webshare). Static residential or datacenter proxies get ASN-flagged and banned.

### Strategic choice: own the arms race vs. offload it

- **Self-host fetcher** — rotating residential proxy (Webshare) + Whisper fallback. Full control, ongoing proxy cost + maintenance.
- **Managed transcript API** (Supadata, transcriptapi.com, etc.) — they absorb the proxy/PoToken problem; server can run anywhere (even Tailscale/cluster). Pay per call.
- **Decision:** build the fetch core behind an interface so both strategies are swappable; default v1 to be decided (see Open Questions).

## Components

- **MCP server** — single tool `get_youtube_transcript(url, lang?)`; FastMCP (Python) or `fastmcp_rust`.
- **Transport/ingress** — Cloudflare Tunnel → cluster service; public HTTPS.
- **Auth** — OAuth 2.1 + PKCE (manual client ID/secret for v1).
- **Fetch core** — pluggable: (a) proxy-based self-host, (b) managed API; both behind one interface.
- **Whisper fallback** — `yt-dlp` audio → Whisper for caption-less videos (lean on existing `franken_whisper` / whisper-stt infra).
- **Cache** — keyed by video ID; minimize re-fetch / block exposure.

## Data Models

```
TranscriptRequest  { url, lang? }
TranscriptResult   { video_id, text, segments[], source, lang, cached }
Segment            { start, duration, text }
```

`source` ∈ { caption_manual, caption_auto, whisper }.

## Implementation Phases

- [ ] Phase 1: MCP server skeleton — one `get_youtube_transcript` tool over Streamable HTTP, runs locally (stdio) for dev.
- [ ] Phase 2: Fetch core v1 behind an interface — caption fetch (youtube-transcript-api / yt-dlp), cache by video ID.
- [ ] Phase 3: Reliability — wire rotating residential proxy AND/OR managed API adapter; handle block errors gracefully.
- [ ] Phase 4: Whisper fallback for caption-less videos.
- [ ] Phase 5: Remote deployment — Cloudflare Tunnel, public HTTPS, OAuth 2.1 + PKCE; add as custom connector and verify on mobile + desktop.

## Open Questions

- Self-host proxy fetcher vs. managed transcript API for v1 default?
- Language/auto-translate handling — return original only, or offer translated tracks?
- Whisper cost/latency budget — which backend, and a max video length cap?
- Auth model — personal (manual client ID/secret) only, or DCR for sharing with others?
- Where to host — which cluster, and Cloudflare Tunnel vs. public Traefik ingress?
- Implementation language — Python (`fastmcp`) for speed, or Rust (`fastmcp_rust`) to dogfood?
