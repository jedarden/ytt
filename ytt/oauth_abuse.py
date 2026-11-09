"""Bounds for unauthenticated OAuth endpoints.

Two endpoint families are guarded here:

- dynamic client registration (``POST /register``) — request throttle plus a
  persistent capacity bound;
- authorization-code initiation (``GET``/``POST /authorize``) — a request
  throttle keyed by source IP *and* OAuth client, enforced before the
  handler runs, so a rejected attempt never issues a code or writes any
  authorization state (transactions, codes).

The ASGI process is single-worker in the supported deployment. The limiters are
therefore process-local, while client registration accounting is persisted in
the encrypted OAuth store so restarts do not reset the capacity bound.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, SupportsFloat
from urllib.parse import parse_qsl

from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.wrappers.base import BaseWrapper
from starlette.datastructures import FormData
from starlette.responses import JSONResponse
from starlette.requests import Request

OAUTH_CLIENT_COLLECTION = "mcp-oauth-proxy-clients"
REGISTRATION_INDEX_COLLECTION = "ytt-oauth-registration-index"
REGISTRATION_INDEX_KEY = "active"


def _canonical_address(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def registration_source_address(
    request: Request,
    *,
    trusted_proxy_count: int,
    trusted_proxy_cidrs: str = "10.42.0.0/16",
) -> str:
    """Return the peer IP or the configured right-to-left XFF hop.

    Proxies must append their observed address to X-Forwarded-For, and the
    ASGI service must only be reachable through that trusted proxy chain.
    With ``trusted_proxy_count=N``, the Nth address from the right is used;
    absent, short, or malformed chains fall back to the socket peer. A value
    of zero ignores X-Forwarded-For entirely.
    """
    peer = _canonical_address(request.client.host if request.client else None)
    if trusted_proxy_count <= 0:
        return peer or "unknown"

    try:
        peer_ip = ipaddress.ip_address(peer) if peer else None
        trusted_networks = [
            ipaddress.ip_network(value.strip(), strict=False)
            for value in trusted_proxy_cidrs.split(",")
            if value.strip()
        ]
    except ValueError:
        return peer or "unknown"
    if (
        peer_ip is None
        or not trusted_networks
        or not any(peer_ip in network for network in trusted_networks)
    ):
        return peer or "unknown"

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        addresses = [part.strip() for part in forwarded.split(",")]
        if len(addresses) >= trusted_proxy_count and all(
            _canonical_address(address) is not None for address in addresses
        ):
            selected = _canonical_address(addresses[-trusted_proxy_count])
            if selected is not None:
                return selected
    return peer or "unknown"


class RegistrationThrottle:
    """Bound registration attempts per source in a strict sliding window."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float = 60,
        max_sources: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 0 or window_seconds <= 0 or max_sources <= 0:
            raise ValueError("invalid OAuth registration throttle bounds")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_sources = max_sources
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, source: str) -> int | None:
        """Return seconds to wait, or ``None`` after recording an allowed hit."""
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            _purge_windows(self._attempts, cutoff)

            bucket = self._attempts.get(source)
            if bucket is None:
                if len(self._attempts) >= self.max_sources:
                    return max(1, math.ceil(self.window_seconds))
                bucket = self._attempts[source] = deque()

            if self.limit == 0:
                return max(1, math.ceil(self.window_seconds))
            if len(bucket) >= self.limit:
                return max(1, math.ceil(bucket[0] + self.window_seconds - now))

            bucket.append(now)
            return None


class RegistrationThrottleMiddleware:
    """ASGI wrapper for registration handlers; placed inside existing CORS."""

    def __init__(
        self,
        app,
        *,
        throttle: RegistrationThrottle,
        trusted_proxy_count: int,
        trusted_proxy_cidrs: str,
    ) -> None:
        self.app = app
        self.throttle = throttle
        self.trusted_proxy_count = trusted_proxy_count
        self.trusted_proxy_cidrs = trusted_proxy_cidrs

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"].upper() != "POST":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        source = registration_source_address(
            request,
            trusted_proxy_count=self.trusted_proxy_count,
            trusted_proxy_cidrs=self.trusted_proxy_cidrs,
        )
        retry_after = self.throttle.retry_after(source)
        if retry_after is None:
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {
                "error": "rate_limited",
                "error_description": "Too many client registrations; retry later.",
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


#: The authorization throttle applies the identical trusted-proxy source rule
#: as the registration throttle (one implementation, two call sites).
authorization_source_address = registration_source_address


def _purge_windows(table: dict[Any, deque[float]], cutoff: float) -> None:
    """Drop entries older than the window cutoff; delete emptied buckets."""
    for key in tuple(table):
        bucket = table[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            del table[key]


class AuthorizationThrottle:
    """Bound ``/authorize`` initiations per (source, client) and per source.

    Two strict rolling windows gate every request:

    - a per-client window keyed ``(source IP, client_id)`` — binds one client
      as seen from one address;
    - a per-IP window keyed by source IP alone — bounds the same address even
      when it rotates ``client_id`` values.

    Admission is atomic across both windows: an attempt is recorded only when
    both admit it, so a rejected attempt never refreshes either window and
    the reported ``Retry-After`` decreases predictably to zero. Each in-memory
    key table holds at most ``max_keys`` entries; when a table is full, a new
    key is denied until some window expires.
    """

    def __init__(
        self,
        *,
        per_client_limit: int,
        per_ip_limit: int,
        window_seconds: float = 60,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            per_client_limit < 0
            or per_ip_limit < 0
            or window_seconds <= 0
            or max_keys <= 0
        ):
            raise ValueError("invalid OAuth authorization throttle bounds")
        self.per_client_limit = per_client_limit
        self.per_ip_limit = per_ip_limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._per_ip: dict[str, deque[float]] = {}
        self._per_client: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def _window_retry(self, oldest: float | None, now: float) -> int:
        if oldest is None:
            return max(1, math.ceil(self.window_seconds))
        return max(1, math.ceil(oldest + self.window_seconds - now))

    def retry_after(self, source: str, client_id: str) -> int | None:
        """Return seconds to wait, or ``None`` after recording an allowed hit."""
        now = self._clock()
        cutoff = now - self.window_seconds
        key = (source, client_id)
        with self._lock:
            _purge_windows(self._per_ip, cutoff)
            _purge_windows(self._per_client, cutoff)

            ip_bucket = self._per_ip.get(source)
            if ip_bucket is None:
                if len(self._per_ip) >= self.max_keys:
                    return self._window_retry(None, now)
                ip_bucket = self._per_ip[source] = deque()
            client_bucket = self._per_client.get(key)
            if client_bucket is None:
                if len(self._per_client) >= self.max_keys:
                    if not ip_bucket:
                        del self._per_ip[source]
                    return self._window_retry(None, now)
                client_bucket = self._per_client[key] = deque()

            waits: list[int] = []
            if self.per_ip_limit == 0 or len(ip_bucket) >= self.per_ip_limit:
                waits.append(
                    self._window_retry(ip_bucket[0] if ip_bucket else None, now)
                )
            if (
                self.per_client_limit == 0
                or len(client_bucket) >= self.per_client_limit
            ):
                waits.append(
                    self._window_retry(client_bucket[0] if client_bucket else None, now)
                )
            if waits:
                return max(waits)

            ip_bucket.append(now)
            client_bucket.append(now)
            return None


class AuthorizationThrottleMiddleware:
    """ASGI wrapper for the authorization endpoint; runs before its handler.

    Every request that reaches the endpoint is counted, whatever its method
    (the route admits GET and POST — Starlette adds HEAD for GET routes — and
    whether the handler would later accept or reject the parameters). The
    ``client_id`` is taken from the query string, or for form-encoded POST
    bodies from the body; a request without one keys under the empty string
    (still IP-bounded). A rejected request is answered directly with ``429``,
    ``Retry-After``, and an OAuth-shaped JSON error body — never a redirect,
    because the throttle fires before the handler validates ``redirect_uri``,
    and redirecting an unvalidated URI is forbidden (RFC 6749 §4.1.2.1).
    """

    def __init__(
        self,
        app,
        *,
        throttle: AuthorizationThrottle,
        trusted_proxy_count: int,
        trusted_proxy_cidrs: str,
    ) -> None:
        self.app = app
        self.throttle = throttle
        self.trusted_proxy_count = trusted_proxy_count
        self.trusted_proxy_cidrs = trusted_proxy_cidrs

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        client_id = request.query_params.get("client_id")
        if (
            client_id is None
            and scope["method"].upper() == "POST"
            and request.headers.get("content-type", "").split(";")[0].strip().lower()
            == "application/x-www-form-urlencoded"
        ):
            body = await request.body()
            client_id = FormData(parse_qsl(body.decode("utf-8", "replace"))).get(
                "client_id"
            )
            # Replay the consumed body so the wrapped handler still sees it.
            sent = False

            async def replay() -> dict:
                nonlocal sent
                if not sent:
                    sent = True
                    return {
                        "type": "http.request",
                        "body": body,
                        "more_body": False,
                    }
                return {"type": "http.disconnect"}

            receive = replay

        source = authorization_source_address(
            request,
            trusted_proxy_count=self.trusted_proxy_count,
            trusted_proxy_cidrs=self.trusted_proxy_cidrs,
        )
        retry_after = self.throttle.retry_after(
            source, client_id if isinstance(client_id, str) else ""
        )
        if retry_after is None:
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {
                "error": "rate_limited",
                "error_description": "Too many authorization requests; retry later.",
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


class RegistrationTTLStore(BaseWrapper):
    """Apply each indexed DCR client's absolute expiry to all client writes."""

    def __init__(
        self,
        key_value: AsyncKeyValue,
        *,
        expiry_for: Callable[[str], Awaitable[float | None]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.key_value = key_value
        self._expiry_for = expiry_for
        self._clock = clock
        super().__init__()

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        if collection == OAUTH_CLIENT_COLLECTION:
            expires_at = await self._expiry_for(key)
            if expires_at is not None:
                ttl = max(0, expires_at - self._clock())
        await self.key_value.put(key=key, value=value, collection=collection, ttl=ttl)


async def enumerate_client_ids(
    key_value: AsyncKeyValue, *, collection: str = OAUTH_CLIENT_COLLECTION
) -> list[str]:
    """Enumerate existing client records when initializing the index.

    The pinned FastMCP default wraps a FileTreeStore, which does not implement
    the optional ``keys`` protocol. Its collection directory is the only
    available migration source for registrations created before the index
    existed. Other stores must expose ``keys``; unknown stores fail closed.
    """
    store: Any = key_value
    for _ in range(8):
        keys = getattr(store, "keys", None)
        if callable(keys):
            return list(await keys(collection=collection))
        inner = getattr(store, "key_value", None)
        if inner is None or inner is store:
            break
        store = inner

    if not isinstance(store, FileTreeStore):
        raise RuntimeError("OAuth client storage cannot enumerate existing rows")

    await store.setup_collection(collection=collection)
    collection_info = store._collection_infos[collection]
    result: list[str] = []
    async for path in collection_info._list_file_paths():
        result.append(path.stem)
    return result
