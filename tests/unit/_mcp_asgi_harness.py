"""Real-ASGI harness for MCP Streamable-HTTP conformance tests.

Drives ``ytt.server.build_asgi_app()`` through an ``httpx.ASGITransport``
client — NOT the Starlette ``TestClient`` used by ``test_mcp_tool_contract.py``
— so every exchange is observed exactly as the protocol emits it: raw status
code, raw headers, and the SSE framing of a response body, with nothing
summarized or re-shaped by a client convenience layer. ASGI responses here are
byte-for-byte what uvicorn would put on the wire for the same request.

This module is shared infrastructure for the MCP session-lifecycle
conformance children of ytt-7829b244 (initialize/session establishment in
``test_mcp_session_lifecycle.py``; the GET-SSE listen stream, DELETE session
termination, and path-prefix mounting children follow). Import the fixtures
and helpers — do not re-derive app/auth setup::

    from tests.unit._mcp_asgi_harness import (  # noqa: F401
        ACCEPT, PROTOCOL_VERSION, AsgiMcpSession,
        hermetic_egress, allowlisted_subject, authorized_bearer,
        initialize_request, json_rpc_messages, the_response_message,
        well_formed_response_envelope, open_asgi_client,
        open_established_session,
    )

Importing a fixture into a test module puts it in that module's pytest
namespace (the same mechanism ``test_proxy_isolation.py`` uses to import
helpers from a sibling test module); the three ``autouse`` fixtures then apply
to that module's tests.

Lifespan: ``httpx.ASGITransport`` does not run ASGI lifespan events, so
``open_asgi_client()`` enters the Starlette app's own lifespan context
(``app.router.lifespan_context``) around the client — the same hook uvicorn
drives. Without it the Streamable-HTTP session manager is not running and
every request would fail with "No read stream writer available". These two
entry points are context managers rather than pytest fixtures on purpose:
the lifespan enters anyio cancel scopes, which must be exited in the task
that entered them, while pytest-asyncio finalizes async fixtures in a task
of its own (the teardown fails with "Attempted to exit cancel scope in a
different task than it was entered in"). Driving the context managers
directly keeps lifespan, requests, and teardown in the test's one task::

    async def test_something():
        async with open_established_session() as session:
            ...

Auth: stubbed per the ``test_mcp_tool_contract.py`` pattern — the transport's
RequireAuthMiddleware demands an ``Authorization: Bearer`` header carrying the
scopes ``ytt.auth``'s provider requires (``openid email profile
offline_access``) before ``verify_token`` is consulted, so the fake
``AccessToken`` carries that scope set; ``verify_token`` is patched to resolve
any token to the allowlisted SUBJECT. These tests exercise transport and
session lifecycle, never OAuth.

Hermeticity: an autouse guard replaces ``ytt.fetch.fetch_transcript`` and
``ytt.whisper.run_whisper_job`` with fail-hard stubs, so no test in a module
using this harness can reach YouTube or a Whisper service.

Spec references (all to the MCP 2025-06-18 specification,
https://modelcontextprotocol.io/specification/2025-06-18): §basic/transports
"Streamable HTTP" for POST/GET/DELETE framing, Accept rules, 202-for-
notifications, response content-type forms, and session management; and
§basic/lifecycle "Initialize" for the initialize request/response contract
and version negotiation. JSON-RPC envelope rules (error codes -32700/-32600/
-32602, id echo) come from the JSON-RPC 2.0 specification the MCP protocol is
defined on top of.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any, AsyncIterator

import httpx
import pytest

import ytt.fetch
import ytt.whisper
from ytt import server
from ytt.config import get_settings
from ytt.server import build_asgi_app, mcp

# ---------------------------------------------------------------------------
# Constants + auth helpers
# ---------------------------------------------------------------------------

#: The allowlisted subject every request in a harness module rides as (the
#: autouse fixtures wire it; a test exercising the gate re-points it).
SUBJECT = "reader@example.com"

#: The scopes ``ytt.auth``'s provider requires of every real client — the
#: transport 403s (``insufficient_scope``) a token missing any of them before
#: verify_token is ever consulted.
SCOPES = ["openid", "email", "profile", "offline_access"]

#: The bearer header value matching the stubbed ``verify_token`` below.
BEARER = "Bearer test-token"

#: MCP spec §basic/transports "Sending Messages to the Server" item 2: the
#: client MUST include an Accept header listing both content types.
ACCEPT = {"Accept": "application/json, text/event-stream"}

#: The protocol version these tests request during initialize and carry on
#: subsequent requests (spec §basic/transports "Protocol Version Header": the
#: client MUST include ``MCP-Protocol-Version`` on all subsequent requests,
#: and it SHOULD be the negotiated one).
PROTOCOL_VERSION = "2025-06-18"


def mcp_endpoint() -> str:
    """The MCP endpoint path the transport is mounted under.

    Spec §basic/transports "Streamable HTTP": the server MUST provide a
    single MCP endpoint supporting POST and GET. Derived from settings so the
    path-prefix child can move it with ``YTT_PATH_PREFIX`` instead of
    re-deriving the app.
    """
    return get_settings().path_prefix.rstrip("/")


def access_token_for(email: str = SUBJECT):
    """A real FastMCP AccessToken standing in for a verified IdP response."""
    from fastmcp.server.auth.auth import AccessToken

    return AccessToken(
        token="test-token",
        client_id="test-client",
        scopes=SCOPES,
        expires_at=None,
        claims={"email": email, "email_verified": False},
    )


def bearer_as(monkeypatch, email: str = SUBJECT) -> None:
    """Make the production token-verification path resolve to *email*."""
    async def _verify(token: str):
        return access_token_for(email)

    monkeypatch.setattr(mcp.auth, "verify_token", _verify)


def initialize_request(
    id_: int = 1,
    protocol_version: str = PROTOCOL_VERSION,
    capabilities: dict | None = None,
    client_info: dict | None = None,
) -> dict:
    """A spec-shaped InitializeRequest body (§basic/lifecycle "Initialize":
    protocol version supported, client capabilities, client implementation
    information)."""
    params: dict[str, Any] = {
        "protocolVersion": protocol_version,
        "capabilities": capabilities if capabilities is not None else {},
        "clientInfo": client_info
        if client_info is not None
        else {"name": "mcp-conformance-test", "version": "0"},
    }
    return {"jsonrpc": "2.0", "id": id_, "method": "initialize", "params": params}


# ---------------------------------------------------------------------------
# Autouse fixtures — hermetic, authorized, deterministic
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def hermetic_egress(monkeypatch):
    """Default-deny stubs on the two network seams the tools can reach (the
    ``test_mcp_tool_contract.py`` guard, verbatim in spirit): anything that
    reaches the guarded originals is a test bug, not a network call."""

    async def _no_fetch(*args: Any, **kwargs: Any):
        raise AssertionError(
            "real caption fetch attempted — install a fetch_transcript stub"
        )

    async def _no_whisper(*args: Any, **kwargs: Any):
        raise AssertionError(
            "real Whisper job attempted — install a run_whisper_job stub"
        )

    monkeypatch.setattr(ytt.fetch, "fetch_transcript", _no_fetch)
    monkeypatch.setattr(ytt.whisper, "run_whisper_job", _no_whisper)


@pytest.fixture(autouse=True)
def allowlisted_subject(monkeypatch):
    """Allowlist SUBJECT for every reader in this module.

    Both the allowlist gate (``ytt.authz.check_subject_auth``) and
    ``build_asgi_app()`` read ``get_settings()`` — an lru_cache any earlier
    test module may have reset with a differently-configured instance. Set
    the env var and clear the cache so the next read reconstructs, and clear
    again on the way out so the suite stays consistent (the
    ``test_authz_tool_gate`` pattern)."""
    monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", SUBJECT)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def authorized_bearer(monkeypatch):
    """Every request rides a valid bearer token for SUBJECT."""
    bearer_as(monkeypatch, SUBJECT)


# ---------------------------------------------------------------------------
# App + transport client
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def open_asgi_client() -> AsyncIterator[httpx.AsyncClient]:
    """A live httpx client speaking ASGI to a fresh ``build_asgi_app()``.

    Enters the app's own lifespan (what uvicorn would drive) so the
    Streamable-HTTP session manager is running, then serves requests via
    ``ASGITransport``. ``raise_app_exceptions`` keeps its default True: a
    server-side crash fails the test loudly instead of masquerading as a
    (malformed) HTTP response — the protocol must answer errors in band.
    """
    app = build_asgi_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://ytt.test",
        ) as client:
            yield client


# ---------------------------------------------------------------------------
# SSE / JSON-RPC body parsing
# ---------------------------------------------------------------------------


def parse_sse_events(text: str) -> list[dict]:
    """The JSON payloads of a ``text/event-stream`` body, in stream order.

    Handles the ``\\r\\n`` line endings the transport actually emits (the
    single-event responses here look identical to the TestClient-based
    parser in ``test_mcp_tool_contract.py``, but this one splits on blank
    lines of either ending so multi-event streams — the GET listen stream —
    parse correctly too). Per the SSE standard, consecutive ``data:`` lines
    of one event are joined with ``\\n``.
    """
    events: list[dict] = []
    for block in re.split(r"\r?\n\r?\n", text):
        data_lines = [
            line[5:][1:] if line[5:].startswith(" ") else line[5:]
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


def json_rpc_messages(resp: httpx.Response) -> list[dict]:
    """Parse a transport response body into its JSON-RPC message(s).

    Spec §basic/transports item 5: for a request the server MUST return
    either ``application/json`` (one JSON object) or ``text/event-stream``
    (SSE-framed); the client MUST support both cases.
    """
    if "text/event-stream" in resp.headers.get("content-type", ""):
        return parse_sse_events(resp.text)
    return [json.loads(resp.text)]


def the_response_message(resp: httpx.Response, request_id: int) -> dict:
    """The single JSON-RPC response message answering *request_id*.

    JSON-RPC 2.0 §4: a request gets exactly one response, whose ``id`` MUST
    be the same as the request's.
    """
    messages = json_rpc_messages(resp)
    mine = [m for m in messages if m.get("id") == request_id]
    assert len(mine) == 1, f"expected one response for id={request_id}, got {messages!r}"
    return mine[0]


def well_formed_response_envelope(message: dict) -> None:
    """Assert the JSON-RPC 2.0 §5 envelope of a response message.

    Both ``result`` and ``error`` responses share it: ``jsonrpc`` "2.0",
    an ``id``, and exactly one of ``result``/``error``; an error object
    carries an integer ``code`` and a string ``message``.
    """
    assert message["jsonrpc"] == "2.0", message
    assert "id" in message, message
    assert ("result" in message) ^ ("error" in message), message
    if "error" in message:
        error = message["error"]
        assert isinstance(error["code"], int), error
        assert isinstance(error["message"], str) and error["message"], error


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class AsgiMcpSession:
    """One live Streamable-HTTP MCP session against the real ASGI app.

    Speaks the client side of the transport as the 2025-06-18 spec requires
    it: every JSON-RPC message is a new POST to the MCP endpoint with the
    dual Accept header; once a session id has been served, every subsequent
    request carries ``Mcp-Session-Id`` and ``MCP-Protocol-Version``.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._next_id = 0
        #: The JSON-RPC id of the most recent request sent through
        #: ``request()`` (for tests that must relate a response to its id).
        self.last_id: int = 0
        #: The session id the server assigned at initialize (spec
        #: §basic/transports "Session Management" item 1); None until then.
        self.session_id: str | None = None

    # -- plumbing -----------------------------------------------------------

    @property
    def path(self) -> str:
        return mcp_endpoint()

    def headers(self, *, session: bool = True, protocol: bool | None = None):
        """Headers for one request: auth + Accept always; session id and
        negotiated protocol version once the session exists (both MUSTs of
        §basic/transports "Session Management" item 2 and "Protocol Version
        Header"; ``session=False`` speaks to the server before/outside any
        session)."""
        merged = {**ACCEPT, "Authorization": BEARER}
        established = self.session_id is not None and session
        if established:
            merged["Mcp-Session-Id"] = self.session_id
        if protocol or (protocol is None and established):
            merged["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return merged

    async def post(
        self,
        payload: dict,
        *,
        session: bool = True,
        protocol: bool | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        """POST one JSON-RPC message (request, notification, or response)."""
        merged = self.headers(session=session, protocol=protocol)
        if headers:
            merged.update(headers)
        return await self.client.post(self.path, json=payload, headers=merged)

    async def post_raw(self, body: bytes, **kwargs) -> httpx.Response:
        """POST a raw body (for deliberately malformed JSON-RPC)."""
        merged = {**self.headers(**kwargs), "Content-Type": "application/json"}
        return await self.client.post(self.path, content=body, headers=merged)

    # -- lifecycle ----------------------------------------------------------

    async def initialize(self, **overrides) -> httpx.Response:
        """Send the initialize request; records the served session id.

        Keyword overrides go to the request factory (``protocol_version``,
        ``capabilities``, ``client_info``); pass ``id_`` to pin the JSON-RPC
        id, else the next sequential one is used. Once a session id has been
        served, the request carries it — a re-initialize on a live session is
        exactly what a real client holding the id sends (§transports "Session
        Management" item 2).
        """
        id_ = overrides.pop("id_", None)
        if id_ is None:
            self._next_id += 1
            id_ = self._next_id
        payload = initialize_request(id_, **overrides)
        resp = await self.post(payload, protocol=False)
        if resp.status_code == 200 and "mcp-session-id" in resp.headers:
            self.session_id = resp.headers["mcp-session-id"]
        return resp

    async def initialized_notification(self) -> httpx.Response:
        """notifications/initialized (§basic/lifecycle: after successful
        initialization the client MUST send it to begin normal operations)."""
        return await self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

    async def start(self) -> httpx.Response:
        """initialize handshake → notifications/initialized, asserted."""
        resp = await self.initialize()
        assert resp.status_code == 200, (
            f"initialize failed: {resp.status_code} {resp.text[:200]}"
        )
        assert self.session_id, "initialize served no Mcp-Session-Id"
        ack = await self.initialized_notification()
        assert ack.status_code == 202, ack.status_code
        return resp

    # -- requests -----------------------------------------------------------

    async def request(self, method: str, params: dict | None = None) -> dict:
        """One JSON-RPC request → its single response message (asserted
        well-formed)."""
        self._next_id += 1
        self.last_id = self._next_id
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        resp = await self.post(payload)
        assert resp.status_code == 200, (
            f"{method}: HTTP {resp.status_code} {resp.text[:200]}"
        )
        message = the_response_message(resp, self._next_id)
        well_formed_response_envelope(message)
        return message


@contextlib.asynccontextmanager
async def open_established_session() -> AsyncIterator[AsgiMcpSession]:
    """A live client plus a fully established session (initialize +
    notifications/initialized completed, asserted) riding it as SUBJECT."""
    async with open_asgi_client() as client:
        session = AsgiMcpSession(client)
        await session.start()
        yield session
