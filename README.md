# ytt — YouTube Transcript MCP Server

A remote [MCP](https://modelcontextprotocol.io/) server that reliably downloads
transcripts from pasted YouTube links, usable as a custom connector in Claude
mobile and Claude desktop.

All transcript fetching happens **inside the server** — no third-party transcript
APIs.  Captions are extracted with [yt-dlp](https://github.com/yt-dlp/yt-dlp)
(json3, with rolling-caption dedup).  If a video has no captions, a
[Whisper](https://github.com/openai/whisper)-compatible ASR service transcribes it.

## Quick start (self-hosted)

```bash
docker run --rm \
  -e YTT_PUBLIC_URL=https://your-domain.example.com/ytt \
  -e YTT_PATH_PREFIX=/ytt/ \
  -e YTT_ALLOWED_SUBJECTS=your-oauth-subject \
  -e YTT_WHISPER_URL=http://your-whisper:8000 \
  -p 8080:8080 \
  ghcr.io/jedarden/ytt:0.2.12
```

The server starts at `http://localhost:8080/ytt`.  Add it as a Claude connector
at `https://your-domain.example.com/ytt` (HTTPS required for Anthropic's backend).

See [docs/usage/self-hosting.md](docs/usage/self-hosting.md) for the full
self-hosting guide (BYO Whisper, BYO residential egress/proxy, BYO OAuth subjects).

## Tools

| Tool | Description |
|------|-------------|
| `get_youtube_transcript` | Fetch the transcript of a YouTube video by URL. Returns inline text for short videos; paginated chunks + `next_cursor` for long videos. Auto-starts Whisper ASR if no captions exist. |
| `get_transcript_job` | Poll a Whisper ASR job. When done, returns the transcript directly. |

Pass any YouTube URL form: `youtu.be/…`, `?v=`, `/shorts/`, `/live/`, bare 11-char ID — all normalize to the same cache entry.

## Requirements and caveats

| Requirement | Notes |
|-------------|-------|
| **Residential egress IP** | YouTube blocks datacenter IPs. Self-hosted on a home server or residential VPS works natively. For VPS/cloud, set `YTT_PROXY_URL` to a residential proxy (e.g. Webshare). |
| **Whisper endpoint** | Required only for videos without captions. Point `YTT_WHISPER_URL` at any OpenAI-compatible ASR service (`/v1/audio/transcriptions`). Run [whisper-openai](https://github.com/stpb/whisper-openai) locally, or skip and accept `no_captions_asr_failed` for caption-less videos. |
| **Single replica** | In-process state (LRU cache, single-flight, Whisper job registry). Scale-out requires a redesign. |
| **Auth required** | OAuth 2.1 with a subject allowlist. Empty allowlist = deny all. |

## Configuration

All config is environment-variable-based — no ardenone-specific values are
baked into the image.

| Variable | Default | Description |
|----------|---------|-------------|
| `YTT_PUBLIC_URL` | *(required)* | Public base URL — OAuth audience + emitted metadata derive from this. Set to your domain. |
| `YTT_PATH_PREFIX` | `/ytt/` | Path the server is mounted under. Must end with `/`. |
| `YTT_ALLOWED_SUBJECTS` | *(empty = deny all)* | Comma-separated OAuth `sub` values allowed to call tools. See [connector.md](docs/usage/connector.md) for how to discover your `sub`. |
| `YTT_WHISPER_URL` | *(unset)* | OpenAI-compatible ASR endpoint. Required for caption-less videos. |
| `YTT_WHISPER_MODEL` | `Systran/faster-whisper-small` | Model name served by the Whisper endpoint. Auto-corrects via `/v1/models`. |
| `YTT_CACHE_DIR` | `/cache` | Transcript cache directory. |
| `YTT_CACHE_MAX_BYTES` | `2Gi` | Max cache size. Must be ≤ the volume size. |
| `YTT_PROXY_URL` | *(unset)* | Optional residential proxy URL (e.g. `http://user:pass@proxy.example.com:port`). |

Full reference: [docs/usage/configuration.md](docs/usage/configuration.md)

## Add as a Claude connector

1. Open Claude Desktop → Settings → Connectors → Add MCP Server.
2. Enter the URL: `https://your-domain.example.com/ytt`
3. Complete the OAuth flow.
4. Run `ytt selftest --show-sub` to discover your `sub`.
5. Set `YTT_ALLOWED_SUBJECTS=<your-sub>` and restart.

Mobile reuses the web/Desktop OAuth token — no separate mobile setup needed.

Full guide: [docs/usage/connector.md](docs/usage/connector.md)

## Architecture

```
Claude (mobile / desktop / web)
  │  Streamable HTTP + OAuth 2.1
  ▼
ytt MCP server (uvicorn, 1 worker)
  ├─ /health (liveness, unauth)
  ├─ /.well-known/oauth-* (path-inserted RFC 9728 metadata)
  ├─ AuthN: OAuth bearer (audience-bound to YTT_PUBLIC_URL)
  ├─ AuthZ: subject allowlist (YTT_ALLOWED_SUBJECTS)
  ├─ Rate limit: per-subject token bucket
  ├─ Single-flight: one yt-dlp call per video per in-flight window
  ├─ LRU cache: flat files, byte-cap, whole-unit eviction
  ▼
  yt-dlp caption fetch (json3, rolling-caption dedup)
    └─ no captions? → Whisper ASR (via YTT_WHISPER_URL)
```

## License

MIT — see [LICENSE](LICENSE).  Bundled third-party software: see [NOTICE](NOTICE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).  Security issues: [SECURITY.md](SECURITY.md).

## ardenone-cluster deployment

The reference deployment co-hosts ytt with `ibkr-mcp` on `mcp.ardenone.com`.
See [docs/usage/deploy-ardenone.md](docs/usage/deploy-ardenone.md) and
[deploy/](deploy/) for the manifests.
