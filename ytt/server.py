"""MCP server wiring (plan: Components / Transport decision).

Builds the FastMCP app (Streamable HTTP), registers the two tools
(``get_youtube_transcript``, ``get_transcript_job``), mounts the auth +
path-inserted ``.well-known`` routes, and runs uvicorn with a single worker.
Implemented across Phases 1, 5, 7. This is a scaffold stub.
"""

from __future__ import annotations


def serve() -> int:
    """Start the MCP server. Implemented in Phase 1 (skeleton) onward."""
    # TODO(phase-1): build the FastMCP app + uvicorn(1 worker) and run it.
    raise NotImplementedError("ytt serve is implemented in Phase 1")
