"""ytt — a remote MCP server that downloads YouTube transcripts.

Captions (yt-dlp json3, rolling-caption dedup) with a Whisper ASR fallback,
served over Streamable HTTP for use as a Claude custom connector.

See ``docs/plan/plan.md`` for the authoritative specification.
"""

__version__ = "0.1.0"
