"""MCP server wiring (plan: Components / Transport decision).

Builds the FastMCP app (Streamable HTTP), registers the two tools
(``get_youtube_transcript``, ``get_transcript_job``), mounts the custom
``/health`` route (unauthenticated), and runs uvicorn with a single worker.

The MCP server is path-prefix-aware: all routes and emitted URLs carry the
configured ``YTT_PATH_PREFIX`` (default ``/ytt/``).

Phase 5: per-subject authorization controls enforced — subject allowlist via
AuthMiddleware (``ytt.authz``), plus the per-subject rate limit (cache-miss
fetch path) and Whisper ASR quota (new jobs only) from ``ytt.ratelimit``.
New Whisper jobs additionally hold a ``YTT_MAX_CONCURRENT_WHISPER`` slot for
their whole lifecycle (:func:`_run_whisper_job_bounded`) — the reservation is
released when the job reaches a terminal state, success or failure alike.
Phase 7: full pipeline wired — cache → fetch → pagination.
Phase 8: observability wired — structlog, Prometheus /metrics, /admin/egress.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import ytt
from ytt import errors
from ytt.auth import build_auth_provider
from ytt.authz import check_subject_auth, subject_allowed
from ytt.cache import CacheHit, TranscriptCache
from ytt.concurrency import ConcurrencyState
from ytt.config import get_settings
from ytt.ratelimit import SubjectRateLimiter, WhisperQuota
from ytt.singleton import (
    SingletonLockHeld,
    SingletonLockUnavailable,
    acquire_singleton_lock,
)
from ytt import whisper as ytt_whisper
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

#: Strong references to in-flight background transcription tasks. The event
#: loop keeps only weak references to tasks — an unreferenced one can be
#: garbage-collected mid-flight, stranding its WhisperJob as `pending` forever
#: and permanently consuming a MAX_PENDING_WHISPER_JOBS slot. Entries are
#: discarded by a done-callback as each task finishes.
_background_jobs: set[asyncio.Task[None]] = set()

#: Active Whisper model name — updated by check_model_guard() at startup.
_active_whisper_model: str = _settings_singleton.whisper_model

#: Per-subject request rate limiter + per-subject Whisper ASR quota
#: (docs/notes/auth.md: "even an allowlisted caller can't exhaust the home IP /
#: shared Whisper service"). In-process state — correct only at replicas:1,
#: like the single-flight map and job registry. Enforced in
#: get_youtube_transcript: the bucket on the cache-miss fetch path, the quota
#: on new Whisper jobs only.
_rate_limiter = SubjectRateLimiter.from_settings(_settings_singleton)
_whisper_quota = WhisperQuota.from_settings(_settings_singleton)


# ---------------------------------------------------------------------------
# Per-subject limit helpers (rate limit + ASR quota, docs/notes/auth.md)
# ---------------------------------------------------------------------------

_ANONYMOUS_SUBJECT = "anonymous"


def _request_subject() -> str:
    """Best-effort subject key for rate-limit / quota accounting.

    The lowercased token ``email`` claim when an auth context is available —
    in production it always is (``AuthMiddleware`` runs before every tool
    call). Falls back to one shared ``anonymous`` bucket when no token can be
    resolved (unit tests invoking tools directly, local runs with auth
    unconfigured), so the limiter still bounds total volume. The raw subject
    is never logged or exported — only its sha256 prefix (see
    :func:`_record_rate_limited`).
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:  # no request/auth context at all
        token = None
    if token is not None:
        email = (token.claims or {}).get("email")
        if email:
            return str(email).strip().lower()
    return _ANONYMOUS_SUBJECT


def _record_rate_limited(subject: str, tool: str) -> None:
    """Metric + structured log on a per-subject denial.

    ``ytt_rate_limited_total{subject_hash}`` carries only the first 8 hex
    chars of sha256(subject) — never the subject itself (redaction rule,
    ``ytt.observability``).
    """
    import hashlib

    from ytt.observability import ytt_rate_limited_total

    subject_hash = hashlib.sha256(subject.encode()).hexdigest()[:8]
    ytt_rate_limited_total.labels(subject_hash=subject_hash).inc()
    log.warning("Rate limited", subject_hash=subject_hash, tool=tool)


def _limiter_check(limiter: Any, subject: str, tool: str) -> tuple[bool, float]:
    """Consume one token from *limiter*, failing closed when it cannot operate.

    Returns ``(admitted, retry_after_sec)``. A limiter that raises is broken
    storage, not an admission signal — the request is denied anyway (the
    deterministic ``rate_limited`` shape below), so a limiter failure can
    never admit a request to the protected fetch/ASR work it guards. The
    retry hint degrades to "no hint" rather than raising into the tool.
    """
    try:
        admitted = bool(limiter.consume(subject))
    except Exception:
        log.exception("Rate limiter failed — denying (fail closed)", tool=tool)
        return False, float("inf")
    if admitted:
        return True, 0.0
    try:
        return False, limiter.retry_after_sec(subject)
    except Exception:
        log.exception("Retry-hint computation failed — denying without hint", tool=tool)
        return False, float("inf")


async def _run_whisper_job_bounded(
    job: Any,
    registry: WhisperJobRegistry,
    settings: Any,
    cache: Any,
    active_model: str,
) -> None:
    """Run one Whisper job while holding its ``YTT_MAX_CONCURRENT_WHISPER`` slot.

    The semaphore slot is the job's *concurrency reservation*: it is acquired
    before the job leaves ``pending`` (a queued job polls as pending — it has
    not started Whisper work yet) and released only when the job reaches a
    terminal state. ``async with`` releases it on success **and** on failure
    (and on task cancellation), so a crashed or failed transcription can never
    hold the shared CPU service's only slot forever (plan: "Cap total in-flight
    WhisperJobs", ``Semaphore(YTT_MAX_CONCURRENT_WHISPER)``).

    ``run_whisper_job`` is resolved from the :mod:`ytt.whisper` module at call
    time (not imported into this namespace) so tests can stub the job body at
    its source module.
    """
    async with _concurrency.whisper_sem:
        await ytt_whisper.run_whisper_job(job, registry, settings, cache, active_model)


def _prm_url(public_url: str) -> str:
    """Routable RFC 9728 protected-resource-metadata URL for *public_url*.

    The PRM document lives at the host root with the resource's path
    appended (``https://host/.well-known/oauth-protected-resource/ytt``) —
    the route :mod:`ytt.auth` path-inserts and the IngressRoute exposes at
    priority 1000. Prefixing instead (the shape this route emitted until
    2026-09-24, ``.../ytt/.well-known/oauth-protected-resource``) produced a
    URL nothing serves: a 401 challenge sending the client to a dead
    metadata endpoint exactly when it needs re-auth instructions. Keep this
    byte-identical with the shape the FastMCP transport challenge emits
    (RFC 9728 §5.1) — see ``docs/notes/http-endpoints.md``.
    """
    parsed = urlparse(public_url)
    return (
        f"{parsed.scheme}://{parsed.netloc}"
        f"/.well-known/oauth-protected-resource{parsed.path.rstrip('/')}"
    )


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
    from fastmcp.server.middleware import AuthMiddleware

    settings = get_settings()
    _auth = build_auth_provider(settings)

    _mcp = FastMCP(
        name="ytt",
        version=ytt.__version__,
        auth=_auth,
        # Enforces YTT_ALLOWED_SUBJECTS on every tool call, resource read, and
        # prompt render — not just the /admin/egress diagnostic route (see
        # ytt.authz.check_subject_auth docstring for why this matters: a
        # prior version of this server only ever checked the allowlist on
        # that one side route).
        middleware=[AuthMiddleware(auth=check_subject_auth)],
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
            "the transcript; query is mutually exclusive with start/end. "
            "Requests are rate-limited per user: on status='error' with "
            "error_code='rate_limited', relay the message and wait before retrying."
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

        # --- 3.5 Per-subject rate limit (docs/notes/auth.md) ------------------
        # Charged only here, on the cache-miss fetch path — cache hits and
        # get_transcript_job polls cost nothing (plan §Caching). Failed
        # fetches spend a token too: the limit guards yt-dlp/egress effort,
        # not successful responses.
        subject = _request_subject()
        admitted, retry_sec = _limiter_check(
            _rate_limiter, subject, "get_youtube_transcript"
        )
        if not admitted:
            _record_rate_limited(subject, "get_youtube_transcript")
            retry_txt = (
                f" Try again in ~{retry_sec:.0f}s." if retry_sec != float("inf") else ""
            )
            return {
                "video_id": video_id,
                "status": "error",
                "error_code": errors.RATE_LIMITED,
                "message": (
                    "Rate limit exceeded "
                    f"({settings.rate_limit_per_min} requests/min per subject)."
                    f"{retry_txt}"
                ),
            }

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
                #
                # Queued-work cap (MAX_PENDING_WHISPER_JOBS): deny *new* jobs
                # while the pending+running backlog is at capacity, so the ASR
                # queue itself is a bounded resource — the per-subject quota
                # caps each subject's rate, this caps the system's backlog.
                # Joining a job already in flight is always allowed (it adds
                # no work); the check precedes the quota charge so a
                # queue-full denial never spends a slot.
                if await whisper_registry.get(video_id) is None:
                    active_jobs = await whisper_registry.active_count()
                    if active_jobs >= settings.max_pending_whisper_jobs:
                        _record_rate_limited(subject, "get_youtube_transcript(asr)")
                        return {
                            "video_id": video_id,
                            "status": "error",
                            "error_code": errors.RATE_LIMITED,
                            "message": (
                                "Whisper queue full "
                                f"({active_jobs}/{settings.max_pending_whisper_jobs} "
                                "jobs pending or running). Try again later."
                            ),
                        }

                # Per-subject ASR quota (docs/notes/auth.md): charged only when
                # this call would START a new job — joining an in-flight job
                # (or polling via get_transcript_job) is free, or clients
                # waiting on one transcription would drain their own budget.
                # The charge lands before the get-or-create so there is no
                # fail-open race window, and is refunded when the call turns
                # out to join an existing job (or no job gets created).
                quota_charged, quota_retry_sec = _limiter_check(
                    _whisper_quota, subject, "get_youtube_transcript(asr)"
                )
                if not quota_charged and (
                    await whisper_registry.get(video_id) is None
                ):
                    _record_rate_limited(subject, "get_youtube_transcript(asr)")
                    retry_sec = quota_retry_sec
                    retry_txt = (
                        f" Try again in ~{retry_sec:.0f}s."
                        if retry_sec != float("inf")
                        else ""
                    )
                    return {
                        "video_id": video_id,
                        "status": "error",
                        "error_code": errors.RATE_LIMITED,
                        "message": (
                            "Whisper ASR quota exhausted "
                            f"({settings.whisper_jobs_per_hour} jobs/hour per "
                            f"subject).{retry_txt}"
                        ),
                    }

                try:
                    job, is_new = await whisper_registry.get_or_create(
                        video_id,
                        # Duration from the fetch's extract_info metadata, when
                        # the no-captions error could carry it — this is what
                        # makes the MAX_ASR_DURATION_SEC cap enforceable at job
                        # creation (too_long_for_asr before any quota spend,
                        # download, or transcription) and gives the pending
                        # response a real ETA. Plain empty_body errors (yt-dlp
                        # returned nothing) have no duration; the download-time
                        # backstop still applies later.
                        duration_sec=getattr(exc, "duration_sec", None),
                        settings=settings,
                    )
                    if quota_charged and not is_new:
                        # Joined an existing job — put the slot back.
                        _whisper_quota.refund(subject)
                except YttError as tla_exc:
                    # too_long_for_asr (duration check fails if we had duration)
                    # — no job was started, so an ASR charge is refunded.
                    if quota_charged:
                        _whisper_quota.refund(subject)
                    return {
                        "video_id": video_id,
                        "status": "error",
                        "error_code": tla_exc.error_code,
                        "message": tla_exc.message,
                    }
                except Exception as exc:
                    # Registry failure — no job was started, so the caller's
                    # slot is released rather than spent on a server fault.
                    # Reported in the same structured shape as any other
                    # unexpected tool error (raising here would escape the
                    # outer handlers, since this is already inside except).
                    if quota_charged:
                        _whisper_quota.refund(subject)
                    logger.exception(
                        "Unexpected error starting Whisper job: %s", exc
                    )
                    return {
                        "video_id": video_id,
                        "status": "error",
                        "error_code": errors.EMPTY_BODY,
                        "message": f"Unexpected error: {exc}",
                    }

                if is_new:
                    # Start background transcription task, holding one
                    # YTT_MAX_CONCURRENT_WHISPER slot for the job's whole
                    # lifecycle (released when it reaches done/error).
                    #
                    # The loop holds only WEAK references to tasks — an
                    # unreferenced one can be garbage-collected mid-flight,
                    # stranding its job as `pending` forever. That would
                    # permanently consume a MAX_PENDING_WHISPER_JOBS slot and
                    # ratchet the queue shut (a queued-work leak the cap can't
                    # see), so hold a strong reference until the task finishes.
                    _task = asyncio.create_task(
                        _run_whisper_job_bounded(
                            job,
                            whisper_registry,
                            settings,
                            transcript_cache,
                            _active_whisper_model,
                        )
                    )
                    _background_jobs.add(_task)
                    _task.add_done_callback(_background_jobs.discard)

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
        Requires a valid Bearer token whose ``email`` claim is admitted by
        the ``YTT_ALLOWED_SUBJECTS`` allowlist via the **same predicate** as
        ``check_subject_auth`` (:func:`ytt.authz.subject_allowed`) — the
        AuthMiddleware gate on the actual MCP tool calls.

        Plan: "``/admin/egress`` — requires a valid Bearer token with a subject
        in ``YTT_ALLOWED_SUBJECTS`` (same auth as tool calls; no special admin
        token)."
        """
        import hashlib

        from fastmcp.server.dependencies import get_access_token

        from ytt.selftest import probe_egress

        # --- Auth: resolve the FastMCP AccessToken for this request -----------
        # get_access_token() reads the auth context RequireAuthMiddleware
        # populates for every request to this ASGI app (custom routes
        # included), already signature/audience-verified by the
        # Google-federated provider — no manual token parsing needed.
        token = get_access_token()
        if token is None:
            return JSONResponse(
                {"error": "Unauthorized", "error_code": errors.FORBIDDEN},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{_prm_url(settings.public_url)}"'
                    )
                },
            )

        claims = token.claims or {}
        email = claims.get("email")

        # Same predicate as the tool-call gate (ytt.authz.check_subject_auth):
        # case-insensitive, @domain-pattern aware, and with NO email_verified
        # requirement — that claim is meaningless against the reference
        # Authentik, whose default email scope mapping hardcodes it False for
        # every account (see the ytt.authz module docstring). The previous
        # inline check here (raw case-sensitive set membership + a truthy
        # email_verified) denied every real production subject: the route
        # could never return 200.
        if not email or not subject_allowed(email, settings.allowed_subjects_set):
            subject_hash = hashlib.sha256((email or "").encode()).hexdigest()[:8]
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

        from ytt.authz import write_last_sub

        write_last_sub(email)

        # --- Probe egress ------------------------------------------------
        try:
            report = await asyncio.to_thread(probe_egress, settings.proxy_url)
        except Exception as exc:
            # httpx failure strings can quote the (credentialed) proxy URL —
            # this body is logged and relayed, so sanitize it first.
            from ytt.observability import redact_credentials

            return JSONResponse(
                {
                    "error": f"Egress probe failed: {redact_credentials(str(exc))}",
                    "error_code": "probe_error",
                },
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

    # Single-replica invariant (plan §Design constraints): refuse to start if
    # another live ytt process already holds the cache-volume lock.  Two live
    # instances would each be half-correct — split cache byte-counter,
    # single-flight map, and Whisper job registry — so this fails closed
    # (exit 1 → CrashLoopBackOff) instead of serving split state.
    try:
        acquire_singleton_lock(settings.cache_dir)
    except (SingletonLockHeld, SingletonLockUnavailable) as exc:
        _log.error("Single-replica invariant violated", reason=str(exc))
        return 1

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

    # Plan §Observability — startup egress log (Phase 8)
    # Probe egress at startup and log the result; update the residential metric.
    from ytt.selftest import probe_egress
    from ytt.observability import ytt_egress_is_residential

    try:
        egress_report = probe_egress(settings.proxy_url)
        ytt_egress_is_residential.set(1 if egress_report.is_residential else 0)
        _log.info(
            "Startup egress probe",
            ip=egress_report.ip,
            asn=egress_report.asn,
            org=egress_report.org,
            via_proxy=egress_report.via_proxy,
            is_residential=egress_report.is_residential,
        )
        if not egress_report.is_residential:
            _log.warning(
                "Egress IP classified as non-residential",
                ip=egress_report.ip,
                asn=egress_report.asn,
                org=egress_report.org,
            )
    except Exception as exc:
        _log.error("Startup egress probe failed", error=str(exc))

    app = build_asgi_app()
    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1)
    return 0
