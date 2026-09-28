"""MCP transport mounting under ``YTT_PATH_PREFIX`` — the /ytt-vs-/ytt/mcp
path-prefix conformance child of ytt-7829b244 (implemented under
ytt-8016c2a1), over the real ASGI transport.

``docs/notes/http-endpoints.md`` ("Route inventory") makes the mounting
promise this module pins: the transport mounts at the bare prefix — "the
single trailing slash stripped exactly once (``/ytt/`` → ``/ytt``) — which is
what keeps ``POST /ytt`` the transport and ``/ytt/mcp`` a 404 **under any
prefix**", because "there is **no** ``/ytt/mcp`` route: the transport is
mounted at the prefix root itself (``/ytt``)". The structural halves of that
promise already have pins — the router carries no ``<prefix>/mcp`` path
(``test_path_prefix_contract.py``, ``test_endpoint_contract.py``) and an
unauthenticated ``POST /ytt/mcp`` is a 404. What neither can show, and what a
client integrator actually needs, is the *protocol-level* contrast on a live
transport: the very same spec-shaped ``initialize`` message, carrying a valid
bearer token, answered with a real InitializeResult when aimed at the prefix
root and with a plain route miss when aimed at the ``/mcp`` spelling. This
module drives both halves through the shared ``_mcp_asgi_harness``
(``httpx.ASGITransport`` over ``build_asgi_app()``-shaped apps — the wire
bytes uvicorn would put on the wire, not a client convenience layer), under
both prefixes the doc's "under any prefix" covers:

- **the default prefix** (``/ytt/``) through the shipping build path —
  ``open_asgi_client()`` over the module singleton, exactly what ``serve()``
  hands uvicorn;
- **a non-default prefix** (``YTT_PATH_PREFIX=/gateway/``) through the
  fresh-process shape — a full ``_build_app()`` rebuild under the prefix plus
  the issuer URL that must accompany it (``YTT_PUBLIC_URL`` ends with the
  prefix stripped of its trailing slash; the same pairing
  ``test_path_prefix_contract.py``'s rebuild leg uses). A real boot reads
  ``YTT_PATH_PREFIX`` once, at import, so an in-process rebuild is the only
  way to see a non-default prefix, and the rebuild is what a fresh process
  with those env values would actually serve. Its auth provider is the
  rebuild's own (not the module singleton the harness's ``bearer_as``
  stubs), so the bearer stub is aimed at it too.

The ``/mcp`` answer is pinned at its exact documented shape — a 404, and
specifically the *router's* miss (``text/plain``), not a 401 and not the
transport's JSON-RPC "Session not found" envelope. Both distinctions carry
contract weight: a 401 would mean the auth gate ran on a path that has no
route (the doc's "401 without" belongs to the ``/ytt`` row; ``/ytt/mcp`` has
no row at all — "anything not in this table is a 404"), and the transport's
unknown-session envelope would mean a transport exists at that path answering
from session state. A client that mistyped the URL must conclude "wrong path",
never "dead session" or "bad token" — and the miss must not assign a session
id, disturb the live session riding the real endpoint, or differ by method
(the listen-stream GET and the terminating DELETE exist only at the root too).
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator

import httpx

from ytt.config import get_settings
from ytt.server import _build_app
from tests.unit._mcp_asgi_harness import (
    ACCEPT,
    PROTOCOL_VERSION,
    SUBJECT,
    AsgiMcpSession,
    access_token_for,
    initialize_request,
    open_asgi_client,
    the_response_message,
    well_formed_response_envelope,
)

# The harness's autouse guards (hermetic egress, allowlist, bearer stub)
# apply to this module by being in its pytest namespace — see the
# _mcp_asgi_harness docstring.
from tests.unit._mcp_asgi_harness import (  # noqa: F401
    allowlisted_subject,
    authorized_bearer,
    hermetic_egress,
)


def _assert_router_miss(resp: httpx.Response, aimed_at: str) -> None:
    """The exact documented answer at ``<prefix>/mcp`` (http-endpoints.md
    "Route inventory"): HTTP 404, the Starlette router's own plain-text miss
    — not a 401 (the transport's auth gate lives on the mounted route, and an
    unrouted path never reaches it), not the transport's JSON-RPC "Session
    not found" envelope (no transport exists at this path, so no session
    state can answer from it), and never read as a session id assignment."""
    assert resp.status_code == 404, (
        f"{aimed_at}: HTTP {resp.status_code} {resp.text[:200]}"
    )
    assert resp.headers["content-type"].startswith("text/plain"), (
        f"{aimed_at}: expected the router's plain-text miss, got "
        f"{resp.headers['content-type']} {resp.text[:200]}"
    )
    assert "Session not found" not in resp.text, (
        f"{aimed_at}: a transport answered from session state — this path "
        "has no route and must never grow one"
    )
    assert "mcp-session-id" not in resp.headers, (
        f"{aimed_at}: a 404 is not a session assignment"
    )


# ---------------------------------------------------------------------------
# Default prefix — POST /ytt is the transport, /ytt/mcp is a 404
# ---------------------------------------------------------------------------


async def test_default_prefix_post_ytt_is_the_mcp_endpoint_and_post_ytt_mcp_is_a_404():
    """Under the default ``YTT_PATH_PREFIX=/ytt/`` — the shipping build path
    (``build_asgi_app()`` over the module singleton) — the documented
    contrast holds at the protocol level: the same spec-shaped, validly
    authenticated ``initialize`` gets a real InitializeResult at ``POST
    /ytt`` and a router 404 at ``POST /ytt/mcp`` (docs §Route inventory:
    "``POST /ytt`` is an MCP message and ``POST /ytt/mcp`` is a 404")."""
    async with open_asgi_client() as client:
        session = AsgiMcpSession(client)
        # Name the mount this leg pins: the harness derives the endpoint
        # from settings, so a silent prefix change could not sneak past it.
        assert session.path == "/ytt"

        # --- half one: POST /ytt carries MCP messages ---------------------
        initialize = await session.initialize(id_=1)
        assert initialize.status_code == 200, initialize.text[:200]
        message = the_response_message(initialize, 1)
        well_formed_response_envelope(message)
        assert "result" in message, message  # an MCP response, not an error
        assert message["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert initialize.headers.get("mcp-session-id") == session.session_id
        ack = await session.initialized_notification()
        assert ack.status_code == 202, ack.text[:200]

        # --- half two: the /mcp spelling is a route miss, full stop -------
        # The identical message a real client sends to the root — valid
        # bearer, negotiated protocol version, live session id — aimed one
        # segment too deep.
        miss = await session.client.post(
            "/ytt/mcp", json=initialize_request(77), headers=session.headers()
        )
        _assert_router_miss(miss, "POST /ytt/mcp (authenticated initialize)")

        # The auth gate never runs on an unrouted path: tokenless is the
        # same 404, not the /ytt row's 401.
        anonymous = await session.client.post(
            "/ytt/mcp", json=initialize_request(78), headers={**ACCEPT}
        )
        _assert_router_miss(anonymous, "POST /ytt/mcp (unauthenticated)")

        # No method finds a route there: the listen stream and session
        # termination are prefix-root-only, whatever the session carries.
        _assert_router_miss(
            await session.client.get("/ytt/mcp", headers=session.headers()),
            "GET /ytt/mcp (live session)",
        )
        _assert_router_miss(
            await session.client.delete("/ytt/mcp", headers=session.headers()),
            "DELETE /ytt/mcp (live session)",
        )

        # ... and the root is still the transport afterwards: the misses
        # neither answered from nor disturbed the session's state.
        live = await session.request("ping")
        assert "result" in live


# ---------------------------------------------------------------------------
# Non-default prefix — the whole mounting contract moves with YTT_PATH_PREFIX
# ---------------------------------------------------------------------------

#: A non-default prefix paired with the issuer URL that must accompany it
#: (self-hosting Step 3: ``YTT_PUBLIC_URL`` ends with the prefix stripped of
#: its trailing slash) — the same pairing ``test_path_prefix_contract.py``'s
#: rebuild leg uses, so the two modules describe one deployment.
_GATEWAY_PREFIX = "/gateway/"
_GATEWAY_PUBLIC_URL = "https://mcp.example.com/gateway"


@contextlib.asynccontextmanager
async def open_gateway_client(monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    """The harness's real-ASGI client against a full app rebuilt under
    ``YTT_PATH_PREFIX=/gateway/`` — the fresh-process shape.

    ``_build_app()`` + ``http_app(path=prefix)`` is what a boot with these
    env values constructs (``serve()`` reads the prefix once, at import);
    the rebuild's lifespan and ``ASGITransport`` come from the harness's own
    ``open_asgi_client`` recipe. The settings cache is cleared on both sides
    so the harness's ``mcp_endpoint()`` (and every ``get_settings()`` reader
    after the test) reconstructs from the caller's env, never this pair.
    """
    monkeypatch.setenv("YTT_PATH_PREFIX", _GATEWAY_PREFIX)
    monkeypatch.setenv("YTT_PUBLIC_URL", _GATEWAY_PUBLIC_URL)
    get_settings.cache_clear()
    try:
        fresh = _build_app()

        async def _verify(token: str):
            return access_token_for(SUBJECT)

        # bearer_as() stubs the singleton's provider; the rebuild owns a
        # fresh one, and without this stub every rebuilt-app request 401s.
        monkeypatch.setattr(fresh.auth, "verify_token", _verify)

        app = fresh.http_app(path=get_settings().path_prefix.rstrip("/") or None)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://ytt.test",
            ) as client:
                yield client
    finally:
        get_settings.cache_clear()


async def test_nondefault_prefix_moves_the_transport_and_keeps_mcp_spelling_a_404(
    monkeypatch,
):
    """Under a non-default ``YTT_PATH_PREFIX=/gateway/`` the documented
    "under any prefix" half: a full session rides ``POST /gateway`` —
    initialize answered with the negotiated version and a served session id,
    ``notifications/initialized`` accepted — while the same authenticated
    message at ``POST /gateway/mcp`` is a router 404 and the old ``/ytt``
    spelling is a 404 too: the mount moved with the prefix, and neither the
    ``/mcp`` alias nor the previous root came along (docs §Route inventory:
    the transport mounts at "the bare prefix … under any prefix")."""
    async with open_gateway_client(monkeypatch) as client:
        session = AsgiMcpSession(client)
        assert session.path == "/gateway"

        # --- the transport moved to the bare prefix, protocol intact -----
        initialize = await session.initialize(id_=1)
        assert initialize.status_code == 200, initialize.text[:200]
        message = the_response_message(initialize, 1)
        well_formed_response_envelope(message)
        assert "result" in message, message
        assert message["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert initialize.headers.get("mcp-session-id") == session.session_id
        ack = await session.initialized_notification()
        assert ack.status_code == 202, ack.text[:200]

        # --- the /mcp spelling is a 404 under this prefix too ------------
        miss = await session.client.post(
            "/gateway/mcp", json=initialize_request(77), headers=session.headers()
        )
        _assert_router_miss(miss, "POST /gateway/mcp (authenticated initialize)")
        anonymous = await session.client.post(
            "/gateway/mcp", json=initialize_request(78), headers={**ACCEPT}
        )
        _assert_router_miss(anonymous, "POST /gateway/mcp (unauthenticated)")
        # The listen stream exists only at the moved root too — the 404 must
        # not differ by method (the default-prefix leg pins the same).
        _assert_router_miss(
            await session.client.get("/gateway/mcp", headers=session.headers()),
            "GET /gateway/mcp (live session)",
        )

        # The previous root is gone entirely — no second transport at the
        # old spelling, with or without a token.
        _assert_router_miss(
            await client.post(
                "/ytt", json=initialize_request(79), headers=session.headers()
            ),
            "POST /ytt (old spelling, authenticated)",
        )
        _assert_router_miss(
            await client.post("/ytt", json=initialize_request(80), headers={**ACCEPT}),
            "POST /ytt (old spelling, unauthenticated)",
        )

        # ... and the /gateway session keeps carrying protocol messages.
        live = await session.request("ping")
        assert "result" in live
