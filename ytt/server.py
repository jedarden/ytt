"""MCP server wiring (plan: Components / Transport decision).

Builds the FastMCP app (Streamable HTTP), registers the two tools
(``get_youtube_transcript``, ``get_transcript_job``), mounts the custom
``/health`` route (unauthenticated), and runs uvicorn with a single worker.

The MCP server is path-prefix-aware: all routes and emitted URLs carry the
configured ``YTT_PATH_PREFIX`` (default ``/ytt/``).

Phase 7: full pipeline wired — cache → fetch → pagination.
Phase 8: observability wired — structlog, Prometheus /metrics, /admin/egress.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import ytt
from ytt import errors
from ytt.auth import build_auth_provider
from ytt.authz import check_subject
from ytt.cache import CacheHit, TranscriptCache
from ytt.concurrency import ConcurrencyState
from ytt.config import get_settings
from ytt.whisper import WhisperJobRegistry

logger = logging.getLogger(__name__)
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons (shared state across all tool calls)
# ---------------------------------------------------------------------------

_settings_singleton = get_settings()

#: Flat-file LRU transcript cache — startup_scan() is called in serve().
transcript_cache = TranscriptCache(
    cache_dir=_settings_singleton.cache_dir,
    max_bytes=_settings_singleton.cache_max_bytes,
    reconcile_sec=_settings_singleton.cache_reconcile_sec,
)

#: Bounded fetch pool + single-flight registry + Whisper semaphore.
_concurrency = ConcurrencyState.from_settings(_settings_singleton)

#: Whisper job registry (Phase 6).
whisper_registry = WhisperJobRegistry()

#: Active Whisper model name — updated by check_model_guard() at startup.
_active_whisper_model: str = _settings_singleton.whisper_model


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

    settings = get_settings()
    _auth = build_auth_provider(settings)

    _mcp = FastMCP(
        name="ytt",
        version=ytt.__version__,
        auth=_auth,
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
        """Canonicalize → cache-first → transcript (inline or chunk-1+cursor) or pending+ETA.

        Plan §Tools: "get_youtube_transcript(url, lang?, mode?, cursor?, start?, end?, query?)"
        Plan §Concurrency: single-flight + bounded pool.
        Plan §Caching: cache-first; whisper fallback.
        Plan §Response shape: build_page handles chunking, filtering, cursor.
        """
        from ytt.canonicalize import canonicalize
        from ytt.errors import YttError
        from ytt import pagination
        from ytt.fetch import fetch_transcript
        from ytt.whisper import run_whisper_job

        settings = get_settings()

        # --- 1. Canonicalize URL → video_id -----------------------------------
        try:
            video_id = canonicalize(url)
        except YttError as e:
            return {
                "video_id": "",
                "status": "error",
                "error_code": e.error_code,
                "message": e.message,
            }

        # --- 2. Build canonical filter args -----------------------------------
        filter_args: dict = {}
        if query is not None:
            filter_args["query"] = query
        if start is not None:
            filter_args["start"] = start
        if end is not None:
            filter_args["end"] = end

        # --- 3. Cache-first lookup --------------------------------------------
        # TranscriptCache.get internally checks whisper fallback too
        hit = await transcript_cache.get(video_id, lang or "")

        if hit is not None:
            return pagination.build_page(hit, mode, filter_args, settings, cursor=cursor)

        # --- 4. Cache miss — attempt caption fetch ----------------------------
        try:
            fetch_result = await _concurrency.fetch_pool.run(
                lambda: _concurrency.discovery_flights.run(
                    video_id,
                    lambda: fetch_transcript(video_id, lang, settings),
                ),
                video_id=video_id,
            )

        except YttError as exc:
            if exc.error_code == errors.EMPTY_BODY:
                # No captions — start or retrieve existing Whisper job.
                # Plan §Whisper fallback: "get-or-create under a lock keyed by video_id"
                try:
                    job, is_new = await whisper_registry.get_or_create(
                        video_id,
                        duration_sec=None,  # duration unknown without extract_info
                        settings=settings,
                    )
                except YttError as tla_exc:
                    # too_long_for_asr (duration check fails if we had duration)
                    return {
                        "video_id": video_id,
                        "status": "error",
                        "error_code": tla_exc.error_code,
                        "message": tla_exc.message,
                    }

                if is_new:
                    # Start background transcription task
                    asyncio.create_task(
                        run_whisper_job(
                            job,
                            whisper_registry,
                            settings,
                            transcript_cache,
                            _active_whisper_model,
                        )
                    )

                eta_str = (
                    f" (~{job.eta_sec:.0f}s)" if job.eta_sec is not None else ""
                )
                return {
                    "video_id": video_id,
                    "status": "pending",
                    "eta_sec": job.eta_sec,
                    "message": (
                        f"No captions found. Transcribing with Whisper ASR{eta_str}. "
                        "Ask me again shortly."
                    ),
                }

            # All other fetch errors (ip_blocked, rate_limited, private, etc.)
            return {
                "video_id": video_id,
                "status": "error",
                "error_code": exc.error_code,
                "message": exc.message,
            }

        except Exception as exc:
            # Unexpected error (not a YttError)
            logger.exception("Unexpected error in get_youtube_transcript: %s", exc)
            return {
                "video_id": video_id,
                "status": "error",
                "error_code": errors.EMPTY_BODY,
                "message": f"Unexpected error: {exc}",
            }

        # --- 5. Cache the fetch result + serve via build_page -----------------
        segs_dicts = [
            {"start": s.start, "duration": s.duration, "text": s.text}
            for s in fetch_result.segments
        ]
        text = " ".join(d["text"] for d in segs_dicts)

        metadata: dict = {}
        if fetch_result.title:
            metadata["title"] = fetch_result.title
        if fetch_result.channel:
            metadata["channel"] = fetch_result.channel
        if fetch_result.duration_sec is not None:
            metadata["duration_sec"] = fetch_result.duration_sec
        if fetch_result.published:
            metadata["published"] = fetch_result.published
        if fetch_result.requested_lang:
            metadata["requested_lang"] = fetch_result.requested_lang
        if fetch_result.available_langs:
            metadata["available_langs"] = fetch_result.available_langs
        if fetch_result.message:
            metadata["message"] = fetch_result.message

        await transcript_cache.put(
            video_id,
            fetch_result.served_lang,
            text,
            segs_dicts,
            fetch_result.source,
            metadata or None,
        )

        hit = CacheHit(
            video_id=video_id,
            lang=fetch_result.served_lang,
            source=fetch_result.source,
            text=text,
            segments=segs_dicts,
            metadata=metadata or None,
        )
        return pagination.build_page(hit, mode, filter_args, settings, cursor=cursor)

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

        Plan §Whisper fallback — job state machine:
        - pending/running: return status + ETA.
        - error: return status=error + error_code + message.
        - done: Phase 7 — deliver the transcript via build_page (same shape as
          get_youtube_transcript, mode=full). Replaces the Phase 6 stub.
        - not found: return not_found with re-call instruction.
        """
        from ytt import pagination

        settings = get_settings()

        job = await whisper_registry.get(video_id)
        if job is None:
            return {
                "video_id": video_id,
                "status": "error",
                "error_code": errors.NOT_FOUND,
                "message": (
                    "Job not found. Re-call get_youtube_transcript with the video URL "
                    "to start a new request."
                ),
            }

        if job.status == "pending":
            return {
                "video_id": video_id,
                "status": "pending",
                "eta_sec": job.eta_sec,
                "message": (
                    "Transcription is queued. "
                    + (
                        f"Estimated time: ~{job.eta_sec:.0f}s. "
                        if job.eta_sec is not None
                        else ""
                    )
                    + "Ask me again shortly."
                ),
            }

        if job.status == "running":
            return {
                "video_id": video_id,
                "status": "running",
                "eta_sec": job.eta_sec,
                "message": (
                    "Transcription is in progress. "
                    + (
                        f"Estimated time remaining: ~{job.eta_sec:.0f}s. "
                        if job.eta_sec is not None
                        else ""
                    )
                    + "Ask me again shortly."
                ),
            }

        if job.status == "error":
            return {
                "video_id": video_id,
                "status": "error",
                "error_code": job.error_code or errors.ASR_FAILED,
                "message": job.message or (
                    "Transcription failed. Re-call get_youtube_transcript "
                    "with the video URL to retry."
                ),
            }

        # --- status == "done" — Phase 7: deliver transcript via build_page ----
        # Plan §Whisper fallback: "when done, returns the transcript directly
        # (same shape/pagination), collapsing 3 calls to 2."
        # Plan §Tools: "get_transcript_job: when done, returns the transcript
        # directly". Uses mode=full (inline if short, chunk-1+cursor if long).
        hit = await transcript_cache.get(video_id, "whisper")
        if hit is None:
            # Evicted between job completion and polling
            await whisper_registry.remove(video_id)
            return {
                "video_id": video_id,
                "status": "error",
                "error_code": errors.NOT_FOUND,
                "message": (
                    "Transcript was cached but has been evicted. "
                    "Re-call get_youtube_transcript to re-fetch."
                ),
            }

        return pagination.build_page(hit, mode="full", filter_args={}, settings=settings)

    # -----------------------------------------------------------------------
    # Custom route: /ytt/health (unauthenticated liveness probe)
    # Plan: "Public /ytt/health returns only liveness (matches ibkr's /ibkr/health)"
    # -----------------------------------------------------------------------

    health_path = settings.route("health")

    @_mcp.custom_route(health_path, methods=["GET"])
    async def health_endpoint(request: Request) -> JSONResponse:
        """Unauthenticated liveness probe (plan: /ytt/health).

        Returns ``{"status": "ok"}`` — liveness only, no sensitive detail.
        Kubernetes liveness + readiness probe target.
        """
        return JSONResponse({"status": "ok"})

    # -----------------------------------------------------------------------
    # Custom route: /ytt/metrics (Prometheus scrape endpoint — unauthenticated)
    # Plan §Observability: scraped via ServiceMonitor.
    # Note: Prometheus requires no auth by convention (the ServiceMonitor
    # targets the ClusterIP directly, not through the public IngressRoute).
    # -----------------------------------------------------------------------

    metrics_path = settings.route("metrics")

    @_mcp.custom_route(metrics_path, methods=["GET"])
    async def metrics_endpoint(request: Request) -> Response:
        """Prometheus metrics scrape endpoint (plan §Observability).

        Unauthenticated — scraped in-cluster only (ServiceMonitor on ClusterIP).
        """
        data = generate_latest(REGISTRY)
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)

    # -----------------------------------------------------------------------
    # Custom route: /ytt/admin/egress (auth-gated egress diagnostics)
    # Plan §Security: "Egress IP/ASN detail is at GET /admin/egress — requires
    # a valid Bearer token with a subject in YTT_ALLOWED_SUBJECTS."
    # -----------------------------------------------------------------------

    egress_path = settings.route("admin/egress")

    @_mcp.custom_route(egress_path, methods=["GET"])
    async def admin_egress_endpoint(request: Request) -> JSONResponse:
        """Auth-gated egress diagnostic probe (plan §Security / §Observability).

        Returns the current egress IP, ASN, org, and residential flag.
        Requires a valid Bearer token with a subject in YTT_ALLOWED_SUBJECTS.

        Plan: "``/admin/egress`` — requires a valid Bearer token with a subject
        in ``YTT_ALLOWED_SUBJECTS`` (same auth as tool calls; no special admin
        token)."
        """
        import hashlib

        from ytt.selftest import probe_egress

        # --- Auth: extract token from Authorization header --------------------
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "Unauthorized", "error_code": errors.FORBIDDEN},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{settings.public_url}/.well-known/'
                        f'oauth-protected-resource"'
                    )
                },
            )

        token_str = auth_header[7:].strip()

        # --- Validate the token (audience + allowlist) -----------------------
        # Use authz.check_subject which returns (sub, error_response) tuple.
        # We perform a lightweight check: verify it decodes, then check allowlist.
        try:
            from ytt.auth import _extract_sub_from_token
            sub = await _extract_sub_from_token(token_str, settings)
        except Exception:
            return JSONResponse(
                {"error": "Invalid token", "error_code": errors.FORBIDDEN},
                status_code=401,
            )

        if not check_subject(sub, settings):
            subject_hash = hashlib.sha256(sub.encode()).hexdigest()[:8]
            log.warning(
                "AuthZ 403",
                subject_hash=subject_hash,
                tool="admin/egress",
            )
            return JSONResponse(
                {
                    "error": (
                        "Contact the server operator to be added to the allowlist."
                    ),
                    "error_code": errors.FORBIDDEN,
                },
                status_code=403,
            )

        # --- Probe egress ------------------------------------------------
        try:
            report = await asyncio.to_thread(probe_egress, settings.proxy_url)
        except Exception as exc:
            return JSONResponse(
                {"error": f"Egress probe failed: {exc}", "error_code": "probe_error"},
                status_code=502,
            )

        # Update the metric
        from ytt.observability import ytt_egress_is_residential
        ytt_egress_is_residential.set(1 if report.is_residential else 0)

        return JSONResponse(
            {
                "ip": report.ip,
                "asn": report.asn,
                "org": report.org,
                "via_proxy": report.via_proxy,
                "is_residential": report.is_residential,
            }
        )

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

    # Configure structlog JSON logging (plan §Observability — Phase 8).
    from ytt.observability import configure_logging
    configure_logging()
    _log = structlog.get_logger("ytt.server")

    settings = get_settings()

    # Startup storage validation (raises on PVC size mismatch; warns on emptyDir).
    try:
        warnings = settings.validate_storage()
    except ValueError as exc:
        _log.error("Startup validation failed", reason=str(exc))
        return 1

    for w in warnings:
        _log.warning("Startup warning", message=w)

    # Plan §Observability — Required log events: "Server startup"
    _log.info(
        "Server startup",
        public_url=settings.public_url,
        cache_backend=settings.cache_backend,
        cache_max_bytes=settings.cache_max_bytes,
        whisper_url=settings.whisper_url,
        whisper_model=settings.whisper_model,
        max_concurrent_fetches=settings.max_concurrent_fetches,
        subjects_count=len(settings.allowed_subjects_set),
    )

    app = build_asgi_app()
    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1)
    return 0
