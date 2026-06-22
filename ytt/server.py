"""MCP server wiring (plan: Components / Transport decision).

Builds the FastMCP app (Streamable HTTP), registers the two tools
(``get_youtube_transcript``, ``get_transcript_job``), mounts the custom
``/health`` route (unauthenticated), and runs uvicorn with a single worker.

The MCP server is path-prefix-aware: all routes and emitted URLs carry the
configured ``YTT_PATH_PREFIX`` (default ``/ytt/``).  OAuth / auth middleware,
``.well-known`` path-insertion, and real tool logic are wired in Phases 5–7.
This is the Phase 1 skeleton.
"""

from __future__ import annotations

import logging
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

import ytt
from ytt.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Build the FastMCP application (module-level singleton so tests can import it)
# ---------------------------------------------------------------------------

def _build_app():
    """Construct the FastMCP instance and register all tools + custom routes.

    Separated from ``serve()`` so tests can import ``mcp`` without starting
    uvicorn.  Called once at module import time; settings are read lazily
    inside each tool invocation so the config can be overridden in tests.
    """
    from fastmcp import FastMCP  # deferred so unit tests can mock if needed

    _mcp = FastMCP(
        name="ytt",
        version=ytt.__version__,
        instructions=(
            "YouTube Transcript MCP server. "
            "Pass any YouTube URL directly — messy URLs with extra parameters, "
            "short URLs (youtu.be/…), or Shorts/Live links all work. "
            "On a 'partial' response, continue pagination by calling "
            "get_youtube_transcript again with the returned next_cursor before "
            "summarizing. On a 'pending' response, relay the ETA to the user "
            "and stop — do not poll; call get_transcript_job later to retrieve "
            "the result."
        ),
    )

    # -----------------------------------------------------------------------
    # Tool 1: get_youtube_transcript
    # -----------------------------------------------------------------------

    @_mcp.tool(
        description=(
            "Fetch the transcript of a YouTube video. "
            "Pass any YouTube URL — messy URLs, short links (youtu.be/…), "
            "/shorts/, /live/, or bare 11-character video IDs are all accepted. "
            "Returns the full transcript text inline for short videos (mode='full'); "
            "for long videos returns the first chunk plus a next_cursor to continue. "
            "On mode='chunk', always paginates regardless of length. "
            "Use the 'lang' parameter to request a specific language (BCP-47 tag, "
            "e.g. 'en', 'es'); omit to use the original/English. "
            "If no captions exist, Whisper ASR starts automatically — the response "
            "has status='pending' with an ETA; relay the ETA to the user and stop. "
            "Call get_transcript_job(video_id) later to retrieve the result. "
            "On status='partial', call again with cursor=next_cursor before answering. "
            "Use start/end (seconds) or query (case-insensitive substring) to filter "
            "the transcript; query is mutually exclusive with start/end."
        )
    )
    async def get_youtube_transcript(
        url: str,
        lang: Optional[str] = None,
        mode: str = "full",
        cursor: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        query: Optional[str] = None,
    ) -> dict:
        """Canonicalize → cache-first → transcript or pending+ETA.

        Phase 1 stub: returns an informative error until Phase 2 wires the
        real fetch core.
        """
        # TODO(phase-2): canonicalize URL, check cache, dispatch fetch/whisper.
        return {
            "video_id": "",
            "status": "error",
            "error_code": "not_implemented",
            "message": "Transcript fetching is not yet implemented (Phase 2).",
        }

    # -----------------------------------------------------------------------
    # Tool 2: get_transcript_job
    # -----------------------------------------------------------------------

    @_mcp.tool(
        description=(
            "Poll the status of a Whisper ASR transcription job. "
            "Pass the video_id returned by a previous get_youtube_transcript call "
            "that came back with status='pending'. "
            "When the job is done, returns the full transcript (same shape as "
            "get_youtube_transcript). "
            "On status='pending' or 'running', relay the ETA and stop. "
            "On status='error' or error_code='not_found', call get_youtube_transcript "
            "again with the original URL to restart the request."
        )
    )
    async def get_transcript_job(
        video_id: str,
    ) -> dict:
        """Poll the WhisperJob registry for a running/done job.

        Phase 1 stub: returns not_found until Phase 6 wires the real registry.
        """
        # TODO(phase-6): look up the WhisperJob registry; return result when done.
        return {
            "video_id": video_id,
            "status": "error",
            "error_code": "not_found",
            "message": (
                "Job not found. Re-call get_youtube_transcript with the video URL "
                "to start a new request."
            ),
        }

    # -----------------------------------------------------------------------
    # Custom route: /ytt/health (unauthenticated liveness probe)
    # Plan: "Public /ytt/health returns only liveness (matches ibkr's /ibkr/health)"
    # -----------------------------------------------------------------------

    settings = get_settings()
    health_path = settings.route("health")

    @_mcp.custom_route(health_path, methods=["GET"])
    async def health_endpoint(request: Request) -> JSONResponse:
        """Unauthenticated liveness probe (plan: /ytt/health).

        Returns ``{"status": "ok"}`` — liveness only, no sensitive detail.
        Kubernetes liveness + readiness probe target.
        """
        return JSONResponse({"status": "ok"})

    return _mcp


# Module-level FastMCP instance — importable by tests without running uvicorn.
mcp = _build_app()


# ---------------------------------------------------------------------------
# ASGI app (Streamable HTTP)
# ---------------------------------------------------------------------------

def build_asgi_app():
    """Return the Starlette ASGI application ready to hand to uvicorn.

    ``mcp.http_app(path=prefix)`` mounts the MCP transport under the
    path prefix so all Streamable-HTTP routes are prefixed correctly.
    Custom routes (health) are already registered on ``mcp``; the
    returned app includes them.

    TODO(phase-5): mount auth middleware + .well-known Starlette routes here.
    """
    settings = get_settings()
    # Strip trailing slash from prefix for http_app (it takes the mount path
    # without a trailing slash, e.g. "/ytt").
    prefix = settings.path_prefix.rstrip("/")
    return mcp.http_app(path=prefix or None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve() -> int:  # pragma: no cover
    """Start the MCP server under uvicorn with one worker.

    Called by ``ytt serve`` (CLI).  Returns an int exit code (0 on clean exit).
    Plan: "uvicorn 1 worker — the container default".
    """
    import uvicorn

    settings = get_settings()

    # Startup storage validation (raises on PVC size mismatch; warns on emptyDir).
    try:
        warnings = settings.validate_storage()
    except ValueError as exc:
        logger.error("Startup validation failed: %s", exc)
        return 1

    for w in warnings:
        logger.warning(w)

    logger.info(
        "ytt %s starting — public_url=%s path_prefix=%s",
        ytt.__version__,
        settings.public_url,
        settings.path_prefix,
    )

    app = build_asgi_app()
    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1)
    return 0
