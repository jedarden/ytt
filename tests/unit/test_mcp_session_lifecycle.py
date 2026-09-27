"""MCP Streamable-HTTP session-lifecycle conformance, over the real ASGI
transport.

Children of ytt-7829b244 (MCP session-lifecycle conformance), pinning the
2025-06-18 Streamable HTTP transport against ``build_asgi_app()`` driven
through ``httpx.ASGITransport`` (via the shared ``_mcp_asgi_harness`` — not
the Starlette ``TestClient``), so what these tests see is exactly what
uvicorn would put on the wire. The path-prefix mounting child reuses the same
harness.

Each test's asserts carry the spec sentence they hold the server to, so the
file doubles as the conformance record. References are to the MCP
2025-06-18 specification (https://modelcontextprotocol.io/specification/
2025-06-18): §transports = basic/transports "Streamable HTTP", §lifecycle =
basic/lifecycle; the JSON-RPC envelope rules come from the JSON-RPC 2.0
specification (§4 response object, §5.1 reserved error codes).

Pinned here:

- **initialize** — a spec-shaped InitializeRequest gets HTTP 200 whose body
  is one of the two spec-allowed forms (here ``text/event-stream``, one
  ``event: message``) carrying a JSON-RPC result with the *negotiated*
  protocolVersion echoed (§lifecycle "Version Negotiation": if the server
  supports the requested version it MUST respond with the same one), the
  server's own capabilities and ``serverInfo`` (§lifecycle "Initialization":
  the server MUST respond with its own capabilities and information — name
  "ytt", version = the release), and its instructions string.
- **session establishment** — the initialize response carries an
  ``Mcp-Session-Id`` header (§transports "Session Management" item 1), the
  id is visible ASCII (a MUST on the id itself), and it is *stable*: the
  session id echoed on the subsequent 202 and on a live request is the same
  value, and the session answers requests on it.
- **notifications/initialized** — accepted with 202 Accepted and no body
  (§transports "Sending Messages to the Server" item 4: for a notification
  the server accepts it MUST return 202 with no body), and the session stays
  live afterwards (a ``ping`` on it answers).
- **second initialize on the live session** — this stack answers it in band
  with a fresh, well-formed InitializeResult and keeps the session, rather
  than erroring. The lifecycle spec does not define re-initialization, and
  the scope note for this child expected a JSON-RPC *error* here; the pinned
  property is therefore well-formedness (a valid JSON-RPC response, not a
  crash or a hung request) plus session continuity (same session id). If a
  future stack upgrade starts rejecting re-initialization instead, this pin
  is meant to be updated deliberately in the same change.
- **initialize against an unknown session** — HTTP 404 with a well-formed
  JSON-RPC error envelope (code -32600, "Session not found"): the same
  response shape §transports "Session Management" item 3 requires for
  requests carrying a session id the server has terminated or never issued,
  and the 404 that item 4 tells the client to recover from by initializing
  again *without* a session id — which opens a fresh session (asserted).
- **malformed initialize bodies** — every failure mode answers with a
  well-formed JSON-RPC error envelope, never a crash, an empty body, or an
  out-of-band 5xx, in the two shapes this stack emits:
  - unparseable JSON → HTTP 400, code -32700 (JSON-RPC 2.0 §5.1 Parse
    error), transport envelope id "server-error";
  - structurally invalid JSON-RPC → HTTP 400, code -32602, same transport
    envelope;
  - a structurally valid JSON-RPC initialize whose params violate the
    lifecycle contract (no ``protocolVersion``) → HTTP 200 SSE-framed error,
    code -32602, with the request's real id echoed (JSON-RPC 2.0 §4: the
    response id MUST match the request id) — the server-session error shape,
    distinct from the transport's.
- **authenticated GET listen stream** — a GET on the MCP endpoint with the
  established session opens a stream: HTTP 200 whose headers are observable
  at ASGI ``http.response.start`` while the body is still open, with
  ``Content-Type: text/event-stream`` (§transports "Listening for Messages
  from the Server" item 3: the server MUST return text/event-stream or 405 —
  this server offers the stream, so 200/event-stream is the pinned form)
  plus the stack's full stream-header set (``Cache-Control: no-cache,
  no-transform``, ``Connection: keep-alive``, the ``Mcp-Session-Id`` echo,
  ``X-Accel-Buffering: no``). The stream stays open: the ASGI call does not
  complete and no terminal body chunk arrives while the client holds the
  connection, a second GET on the same session is rejected with 409
  Conflict ("Only one SSE stream is allowed per session" — server-side
  proof the first stream is live), and when the client disconnects the ASGI
  call completes and the session itself keeps serving requests.
- **GET failure shapes** — the three documented refusals, each at an exact
  status with a well-formed body:
  - GET without ``Mcp-Session-Id`` → HTTP 400, ``application/json`` JSON-RPC
    error envelope (transport id "server-error", code -32600, message
    "Bad Request: Missing session ID"). §transports "Session Management"
    item 2 makes the header a MUST on all of the client's subsequent HTTP
    requests, the listen-stream GET included.
  - GET with an unknown session id → HTTP 404 with the same "Session not
    found" envelope the POST side returns (§transports "Session Management"
    item 3's 404 MUST, item 4's recover-by-re-initialize trigger).
  - unauthenticated GET → HTTP 401 with the ``WWW-Authenticate: Bearer``
    challenge (scheme Bearer, ``error="invalid_token"``,
    ``resource_metadata`` advertised) and a JSON ``invalid_token`` body —
    the same RFC 6750 §3 / RFC 9728 §5.1 shape ``test_oauth_conformance.py``
    pins for the POST transport probe, and it 401s even when a valid session
    id is presented: auth runs before session logic.
- **DELETE termination** — DELETE on the MCP endpoint with the established
  ``Mcp-Session-Id`` answers HTTP 200 with an empty body and the terminated
  session's own id echoed (§transports "Session Management" item 5: the
  client SHOULD send DELETE to explicitly terminate; the only status the
  spec itself pins for DELETE is the 405 a server that forbids termination
  would return — this server allows it, and 200-with-no-body is its pinned
  success form). The termination is real: a subsequent POST on that id gets
  HTTP 404 with the transport-level "Not Found: Session has been terminated"
  envelope, addressed by the dead session's own id — and the §item 4
  recovery (a fresh initialize without a session id) still opens a working
  session.
- **repeated/continued use of a terminated session** — POST, GET, and a
  second DELETE on the terminated id each earn the same exact 404
  "Session has been terminated" envelope; the id is never resurrected or
  re-served, and every one of those 404s echoes the dead id — the session's
  own transport answers its obituary, not a global handler.
- **DELETE failure shapes** — the two refusals, at exact statuses with the
  same envelope shapes their GET counterparts use:
  - DELETE without ``Mcp-Session-Id`` → HTTP 400 "Bad Request: Missing
    session ID" (item 2's header MUST covers the termination request too),
    with the same minted-throwaway-id artifact the session-less GET shows:
    the manager routes a header-less request through its new-session case
    and the refusing transport answers with the *fresh* id it just minted.
  - DELETE with an unknown session id → HTTP 404 "Session not found" — the
    same envelope the POST and GET sides answer, and no session id header
    (never read as an assignment).
- **no state leaks across terminated sessions** — after terminating session
  A, a fresh initialize on the same client opens session B with a new id
  that answers its own requests, while A stays 404-dead beside it: B's
  existence neither resurrects A nor answers for it (A's 404 still carries
  A's id, and B's responses carry B's), so terminated sessions cannot hand
  reachability to new ones or borrow it back.

Auth is stubbed (harness autouse fixtures): these tests exercise transport
and session lifecycle, not OAuth — the 401/403 auth paths belong to
``test_endpoint_contract.py`` and the tool-level allowlist gate to
``test_mcp_tool_contract.py``; the unauthenticated-GET test here pins the
transport-level challenge itself because a 401-vs-404-vs-400 ordering is
part of the GET contract under test.

The listen-stream tests cannot ride ``httpx.ASGITransport``: it awaits the
whole ASGI call before handing back a response, and an open-ended SSE stream
never completes — the request would hang forever. ``open_listen_stream()``
below drives the same app at the raw ASGI level (the scope dict the
transport builds, called directly), which keeps the wire bytes identical to
uvicorn's while making ``http.response.start`` observable mid-stream and
letting the test deliver the ``http.disconnect`` a real client's hangup
produces.
"""

from __future__ import annotations

import contextlib
import json
from typing import AsyncIterator

import anyio
import httpx
import pytest

import ytt
from tests.unit._mcp_asgi_harness import (
    ACCEPT,
    BEARER,
    PROTOCOL_VERSION,
    AsgiMcpSession,
    initialize_request,
    json_rpc_messages,
    open_asgi_client,
    open_established_session,
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
# The GET challenge test pins the same RFC 9728-shaped WWW-Authenticate the
# OAuth suite pins for the POST probe; its quote-aware parser handles the
# commas inside the challenge's quoted error_description.
from tests.unit.test_oauth_conformance import _parse_www_authenticate


def _fresh_session(client: httpx.AsyncClient) -> AsgiMcpSession:
    """An un-initialized session object over *client* — for tests that must
    observe the raw first handshake themselves."""
    return AsgiMcpSession(client)


# ---------------------------------------------------------------------------
# initialize — the first exchange
# ---------------------------------------------------------------------------


async def test_initialize_returns_negotiated_version_and_server_info():
    """A spec-shaped initialize gets a well-formed InitializeResult: HTTP 200
    in one of the two spec-allowed response forms, the requested (supported)
    protocol version echoed back, and the server's own capabilities and
    information.

    §transports "Sending Messages to the Server" item 5: "If the input is a
    JSON-RPC request, the server MUST either return Content-Type:
    text/event-stream, to initiate an SSE stream, or Content-Type:
    application/json, to return one JSON object."  §lifecycle "Version
    Negotiation": "If the server supports the requested protocol version, it
    MUST respond with the same version."  §lifecycle "Initialization": "The
    server MUST respond with its own capabilities and information."
    """
    async with open_asgi_client() as client:
        session = _fresh_session(client)
        resp = await session.initialize(id_=1)

        # --- transport framing ---------------------------------------------
        assert resp.status_code == 200, resp.text[:200]
        content_type = resp.headers["content-type"]
        assert (
            "text/event-stream" in content_type or "application/json" in content_type
        ), content_type
        # This stack answers requests as a single-event SSE stream (the form
        # fastmcp's http_app defaults to, json_response=False) — pinned so a
        # flip to the other spec-allowed form is a visible, deliberate change.
        assert "text/event-stream" in content_type

        # --- the JSON-RPC result -------------------------------------------
        messages = json_rpc_messages(resp)
        assert len(messages) == 1, messages  # one response, nothing else on it
        result = messages[0]
        well_formed_response_envelope(result)
        assert result["id"] == 1  # JSON-RPC 2.0 §4: response id MUST match
        assert "error" not in result

        assert result["result"]["protocolVersion"] == PROTOCOL_VERSION

        server_info = result["result"]["serverInfo"]
        assert server_info["name"] == "ytt"
        assert server_info["version"] == ytt.__version__

        capabilities = result["result"]["capabilities"]
        assert "tools" in capabilities  # the server's advertised surface

        # ytt deliberately ships instructions (server → client operating
        # hints); the field is optional in the spec but load-bearing here.
        assert result["result"]["instructions"].startswith(
            "YouTube Transcript MCP server"
        )


async def test_initialize_assigns_a_visible_ascii_session_id():
    """The initialize response carries an Mcp-Session-Id header, and the id
    is well-formed per the session-management MUSTs.

    §transports "Session Management" item 1: "A server using the Streamable
    HTTP transport MAY assign a session ID at initialization time, by
    including it in an Mcp-Session-Id header on the HTTP response containing
    the InitializeResult. … The session ID MUST only contain visible ASCII
    characters (ranging from 0x21 to 0x7E)."  This server does assign one
    (requests without it are rejected), so the MAY is load-bearing here.
    """
    async with open_asgi_client() as client:
        session = _fresh_session(client)
        resp = await session.initialize(id_=1)
        assert resp.status_code == 200, resp.text[:200]

        session_id = resp.headers.get("mcp-session-id")
        assert session_id, "initialize response carried no Mcp-Session-Id header"
        assert all(0x21 <= ord(ch) <= 0x7E for ch in session_id), session_id


async def test_session_id_is_stable_across_the_established_session():
    """The session id served at initialize is the same one echoed on every
    later response of that session — the server never rotates or re-mints it
    mid-session, and requests on it are answered.

    §transports "Session Management" item 2: "If an Mcp-Session-Id is
    returned by the server during initialization, clients using the
    Streamable HTTP transport MUST include it in the Mcp-Session-Id header on
    all of their subsequent HTTP requests." The stable-id property is what
    makes that MUST coherent: a client echoing the served id must keep
    talking to the session it created.
    """
    async with open_asgi_client() as client:
        session = _fresh_session(client)
        initialize_resp = await session.initialize(id_=1)
        served = initialize_resp.headers["mcp-session-id"]
        assert session.session_id == served

        ack = await session.initialized_notification()
        assert ack.headers.get("mcp-session-id") == served

        live = await session.request("ping")
        assert "result" in live
        assert session.session_id == served


# ---------------------------------------------------------------------------
# notifications/initialized — completing the handshake
# ---------------------------------------------------------------------------


async def test_initialized_notification_is_202_with_no_body():
    """notifications/initialized on the established session is accepted with
    202 Accepted and an empty body — and the session keeps working after it.

    §transports "Sending Messages to the Server" item 4: "If the input is a
    JSON-RPC response or notification: If the server accepts the input, the
    server MUST return HTTP status code 202 Accepted with no body."
    §lifecycle "Initialization": "After successful initialization, the client
    MUST send an initialized notification to indicate it is ready to begin
    normal operations."  The trailing ping proves "accepted" did not
    silently drop or kill the session.
    """
    async with open_asgi_client() as client:
        session = _fresh_session(client)
        resp = await session.initialize(id_=1)
        assert resp.status_code == 200, resp.text[:200]

        ack = await session.initialized_notification()

        assert ack.status_code == 202, ack.text[:200]
        assert ack.content == b"", ack.content[:100]  # "with no body" is a MUST

        # The session established at initialize is the one the notification
        # completed: a request on it is answered normally afterwards.
        live = await session.request("ping")
        assert "result" in live


# ---------------------------------------------------------------------------
# Re-initialization and unknown sessions
# ---------------------------------------------------------------------------


async def test_second_initialize_on_the_live_session_is_answered_in_band():
    """A second initialize on the same established session gets a well-formed
    JSON-RPC *result* — a fresh InitializeResult, id echoed, session id
    unchanged.

    The lifecycle spec defines initialize as the first interaction but does
    not specify what a re-initialize must return (the scope note for this
    child expected an error; this stack — fastmcp 3.4.2 — re-negotiates
    idempotently instead). What conformance requires either way is that the
    server answers in band: a valid JSON-RPC response in the session's
    stream, never a crash, a hang, or an out-of-band 5xx, and that the
    session survives the odd request. If a stack upgrade starts returning a
    JSON-RPC error here, update this pin deliberately.
    """
    async with open_established_session() as session:
        before = session.session_id
        resp = await session.initialize(id_=99)  # carries the session id

        assert resp.status_code == 200, resp.text[:200]
        message = the_response_message(resp, 99)
        well_formed_response_envelope(message)
        assert "result" in message, message
        # The re-negotiation re-states the negotiated version…
        assert message["result"]["protocolVersion"] == PROTOCOL_VERSION
        # …identifies the same server…
        assert message["result"]["serverInfo"]["name"] == "ytt"
        # …and does not disturb the established session.
        assert resp.headers.get("mcp-session-id") == before
        assert session.session_id == before
        live = await session.request("ping")
        assert "result" in live


async def test_initialize_against_an_unknown_session_is_404_then_reinit_recovers():
    """An initialize carrying a session id the server never issued gets 404
    with a well-formed JSON-RPC error envelope; re-initializing WITHOUT a
    session id — the recovery the spec prescribes for that 404 — opens a
    fresh, working session.

    §transports "Session Management" item 3: "The server MAY terminate the
    session at any time, after which it MUST respond to requests containing
    that session ID with HTTP 404 Not Found." (an id the server never issued
    is the degenerate terminated case, and must meet the same MUST).
    Item 4: "When a client receives HTTP 404 in response to a request
    containing an Mcp-Session-Id, it MUST start a new session by sending a
    new InitializeRequest without a session ID attached."
    """
    async with open_asgi_client() as client:
        session = _fresh_session(client)
        bogus = "b0gus-b0gus-b0gus-b0gus"

        # --- the unknown session is 404 + well-formed JSON-RPC error -------
        resp = await client.post(
            session.path,
            json=initialize_request(1),
            headers={
                **ACCEPT,
                "Authorization": "Bearer test-token",
                "Mcp-Session-Id": bogus,
            },
        )
        assert resp.status_code == 404, resp.text[:200]
        envelope = json_rpc_messages(resp)[0]
        well_formed_response_envelope(envelope)
        assert envelope["error"]["code"] == -32600  # JSON-RPC 2.0 §5.1 Invalid Request
        assert "Session not found" in envelope["error"]["message"]
        # The 404 must not be confused with a session assignment: no session
        # id was minted for the bogus header.
        assert "mcp-session-id" not in resp.headers

        # --- the prescribed recovery: re-initialize without a session id ---
        recovered = await session.initialize(id_=2)  # no Mcp-Session-Id sent
        assert recovered.status_code == 200, recovered.text[:200]
        fresh = recovered.headers["mcp-session-id"]
        assert fresh and fresh != bogus
        assert session.session_id == fresh

        # The fresh session really works.
        await session.initialized_notification()
        live = await session.request("ping")
        assert "result" in live


# ---------------------------------------------------------------------------
# Malformed initialize bodies — errors stay well-formed
# ---------------------------------------------------------------------------


def _assert_transport_error_envelope(resp: httpx.Response, code: int) -> dict:
    """The transport-level error envelope: HTTP 400, application/json, a
    JSON-RPC error object whose id is the transport's stand-in (it cannot
    know the request id of an unparseable/invalid message — JSON-RPC 2.0 §4.1
    allows the id to be null for that case; this stack uses the string
    "server-error")."""
    assert resp.status_code == 400, resp.text[:200]
    envelope = json_rpc_messages(resp)[0]
    well_formed_response_envelope(envelope)
    assert envelope["error"]["code"] == code
    return envelope


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            b'{"jsonrpc": "2.0", "id": 1, "meth',
            id="truncated-json",
        ),
        pytest.param(
            b"<initialize>this is not JSON</initialize>",
            id="not-json-at-all",
        ),
    ],
)
async def test_unparseable_initialize_body_is_a_json_rpc_parse_error(body: bytes):
    """A POST whose body is not parseable JSON gets HTTP 400 with a JSON-RPC
    error envelope carrying -32700 Parse error — never a crash, never an
    empty or non-JSON-RPC body.

    JSON-RPC 2.0 §5.1 reserves -32700 for "Parse error … An error occurred on
    the server while parsing the JSON text".  §transports "Sending Messages
    to the Server" item 4: when the server cannot accept the input it "MUST
    return an HTTP error status code (e.g., 400 Bad Request)" whose body
    "MAY comprise a JSON-RPC error response".
    """
    async with open_established_session() as session:
        resp = await session.post_raw(body)
        envelope = _assert_transport_error_envelope(resp, -32700)
        assert envelope["error"]["message"], "parse error must carry a message"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="empty-object"),
        pytest.param(
            {
                "jsonrpc": "1.0",
                "id": 1,
                "method": "initialize",
                "params": initialize_request(1)["params"],
            },
            id="jsonrpc-1.0",
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "method": 42},
            id="method-not-a-string",
        ),
    ],
)
async def test_structurally_invalid_initialize_body_is_a_transport_rejection(body):
    """A body that parses as JSON but is not a valid JSON-RPC 2.0 message
    gets HTTP 400 with a JSON-RPC error envelope carrying -32602 — the
    transport refuses it before any session logic sees it.

    JSON-RPC 2.0 §5.1 reserves -32602 for "Invalid params … Invalid method
    parameter(s)" (this stack uses it for the message-shape validation
    failure); the HTTP status follows §transports item 4 as above. The
    endpoint's contract with a broken client is a well-formed error either
    way — never a hang, a crash, or an out-of-band 5xx.
    """
    async with open_established_session() as session:
        resp = await session.post_raw(json.dumps(body).encode())
        _assert_transport_error_envelope(resp, -32602)


async def test_initialize_without_protocol_version_is_an_in_band_json_rpc_error():
    """A structurally valid JSON-RPC initialize whose params omit the
    required protocolVersion is answered IN BAND as a JSON-RPC error with
    the request's real id echoed — the server-session error shape, distinct
    from the transport's pre-parse envelope.

    §lifecycle "Initialization": the client "MUST … send[ ] an initialize
    request containing: Protocol version supported, Client capabilities,
    Client implementation information"; the server answers a request that
    violates that contract with an error, and the JSON-RPC 2.0 §4 form for
    that error carries the request's own id (the MCP spec's own example
    initialization error does exactly this). Here that means HTTP 200 (the
    request WAS a valid JSON-RPC request — §transports item 5 applies to it)
    with the error SSE-framed on the response stream.
    """
    async with open_established_session() as session:
        # initialize_request minus the protocolVersion the lifecycle MUST
        # requires.
        params = initialize_request(7)["params"]
        del params["protocolVersion"]
        message = await session.request("initialize", params)

        assert message["id"] == session.last_id  # the real request id, echoed
        assert message["error"]["code"] == -32602
        assert message["error"]["message"]  # and a message for the client


async def test_initialize_requesting_an_unsupported_version_renegotiates():
    """An initialize requesting a protocol version the server does not
    support still gets a well-formed InitializeResult — for a version the
    server DOES support, not an echo of the bogus one.

    §lifecycle "Version Negotiation": "If the server supports the requested
    protocol version, it MUST respond with the same version. Otherwise, the
    server MUST respond with another protocol version it supports."
    """
    async with open_asgi_client() as client:
        session = _fresh_session(client)
        resp = await session.initialize(id_=11, protocol_version="1999-01-01")

        assert resp.status_code == 200, resp.text[:200]
        message = the_response_message(resp, 11)
        well_formed_response_envelope(message)
        assert "error" not in message
        negotiated = message["result"]["protocolVersion"]
        assert negotiated != "1999-01-01"
        # The version the server picked is one it actually speaks: subsequent
        # requests carrying it as MCP-Protocol-Version are accepted (the
        # header MUST of §transports "Protocol Version Header").
        await session.initialized_notification()
        live = await session.request("ping")
        assert "result" in live


# ---------------------------------------------------------------------------
# GET — the server→client listen stream
# ---------------------------------------------------------------------------


class _ListenStream:
    """A test's view of one open GET listen stream.

    ``open_listen_stream`` fills ``status``/``headers`` the moment the ASGI
    app emits ``http.response.start`` — while the body is still streaming —
    and drives ``app_done`` when the ASGI call completes. ``response_ended``
    records whether the *server* terminated the response (a terminal
    ``more_body``-false body chunk) as opposed to the client hangup ending
    the call.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.app_done = anyio.Event()
        self.response_ended = False


async def _await_seen(ready, aborted, what: str, timeout: float = 10.0) -> None:
    """Wait until *ready()* fires, failing loudly if *aborted()* fires first.

    Plain ``await event.wait()`` would hang forever if the app died before
    emitting the awaited signal (e.g. an exception with
    ``raise_app_exceptions`` semantics); polling with an abort check and a
    deadline turns every such surprise into an assertion instead. Both
    arguments are zero-argument callables (an ``anyio.Event().is_set`` bound
    method is one).
    """
    deadline = anyio.current_time() + timeout
    while not ready():
        if aborted():
            raise AssertionError(f"listen stream: {what} never arrived")
        if anyio.current_time() > deadline:
            raise AssertionError(f"listen stream: timed out waiting for {what}")
        await anyio.sleep(0.01)


@contextlib.asynccontextmanager
async def open_listen_stream(
    session: AsgiMcpSession,
) -> AsyncIterator[_ListenStream]:
    """Open the GET listen stream on an established session and hold it open.

    ``httpx.ASGITransport`` cannot host this request: it awaits the whole
    ASGI call before returning a response, and an open-ended SSE stream never
    completes — the request would hang forever. This driver is that
    transport's own work one level down (same scope dict it builds, same
    direct app call, minus the buffering): the wire bytes are still exactly
    what uvicorn would put on the wire, ``http.response.start`` becomes
    observable mid-stream, and ``client_closed`` lets the test deliver the
    ``http.disconnect`` a real client's hangup produces.

    On exit the client is deemed to hang up; the yielded context ends only
    after the ASGI call has actually completed (so a test can assert the
    teardown right after the ``async with``), and an app-side exception
    propagates rather than masquerading as a closed stream.
    """
    # The session's own app instance — the one whose session manager holds
    # this session's live stream registry.
    app = session.client._transport.app
    stream = _ListenStream()
    client_closed = anyio.Event()
    sent_request_body = False

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": session.path,
        "raw_path": session.path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in {**session.headers(), "host": "ytt.test"}.items()
        ],
        "client": ("testclient", 50000),
        "server": ("ytt.test", 80),
    }

    async def receive() -> dict:
        # uvicorn's shape for a bodyless request: one empty http.request
        # message, then silence until the client goes away.
        nonlocal sent_request_body
        if not sent_request_body:
            sent_request_body = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await client_closed.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            stream.status = message["status"]
            stream.headers = {
                name.decode("latin-1").lower(): value.decode("latin-1")
                for name, value in message["headers"]
            }
        elif message["type"] == "http.response.body" and not message.get(
            "more_body", False
        ):
            stream.response_ended = True

    async def run() -> None:
        try:
            await app(scope, receive, send)
        finally:
            stream.app_done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await _await_seen(
            lambda: stream.status is not None,
            stream.app_done.is_set,
            "response start",
        )
        try:
            yield stream
        finally:
            client_closed.set()
            await _await_seen(
                stream.app_done.is_set, lambda: False, "clean ASGI teardown"
            )
            tg.cancel_scope.cancel()


async def test_authenticated_get_opens_a_listen_stream_that_stays_open():
    """A GET on the MCP endpoint with the established session opens the
    server→client listen stream: 200 + text/event-stream, headers observable
    at response start, the stream held open until the client hangs up — and
    the session keeps working after the stream closes.

    §transports "Listening for Messages from the Server": item 1 (the client
    MAY issue an HTTP GET to open an SSE stream the server can push on),
    item 3 ("the server MUST either return Content-Type: text/event-stream
    in response to this HTTP GET, or else return HTTP 405 Method Not
    Allowed" — this server offers the stream, so 200/event-stream is the
    pinned form), and item 6 ("the client MAY disconnect ... at any time").
    §transports "Session Management" item 2 makes the established session id
    (echoed on the stream response) part of that GET.
    """
    async with open_established_session() as session:
        async with open_listen_stream(session) as stream:

            # --- status + the pinned stream-header set ---------------------
            assert stream.status == 200, stream.headers
            assert stream.headers["content-type"] == "text/event-stream"
            assert stream.headers["mcp-session-id"] == session.session_id
            # The rest of the wire contract this stack emits for a stream
            # (pinning so an upgrade that drops one is a visible change):
            # proxy-safe no-transform caching, a keep-alive connection, and
            # the X-Accel-Buffering "no" that keeps nginx from holding the
            # stream's first bytes.
            assert stream.headers["cache-control"] == "no-cache, no-transform"
            assert stream.headers["connection"] == "keep-alive"
            assert stream.headers["x-accel-buffering"] == "no"

            # --- the stream is open, not a completed empty response -------
            # An ended stream would complete the ASGI call (and deliver a
            # terminal body chunk); neither happens while the client holds
            # the connection open.
            await anyio.sleep(0.25)
            assert not stream.app_done.is_set(), (
                "the listen stream ended on its own before the client closed it"
            )
            assert not stream.response_ended, (
                "the listen stream delivered a terminal body chunk before the "
                "client closed it"
            )

            # --- one listen stream per session -----------------------------
            # §transports "Listening for Messages from the Server" item 5:
            # the server MAY use the GET stream "for JSON-RPC requests and
            # notifications" — a single stream per session is this stack's
            # shape, and a second concurrent GET is refused in band. This is
            # also server-side proof the first stream is really live and
            # registered, not merely a header the app emitted.
            second = await session.client.get(session.path, headers=session.headers())
            assert second.status_code == 409, second.text[:200]
            envelope = json_rpc_messages(second)[0]
            well_formed_response_envelope(envelope)
            assert envelope["error"]["code"] == -32600
            assert "Only one SSE stream is allowed per session" in (
                envelope["error"]["message"]
            )

        # The client hangup ended the ASGI call (open_listen_stream asserted
        # completion) without the server having closed the stream itself.
        assert not stream.response_ended

        # --- closing the listen stream is not closing the session --------
        # The stream is a server→client channel; the session (§transports
        # "Session Management" — terminated by DELETE or server timeout,
        # answered with 404 thereafter) must survive the client merely
        # unplugging its ears.
        live = await session.request("ping")
        assert "result" in live


async def test_get_without_a_session_id_is_a_400_missing_session_error():
    """A GET that omits Mcp-Session-Id is refused with HTTP 400 and a
    well-formed JSON-RPC error envelope — never a stream, a crash, or an
    out-of-band 5xx — and the established session is unharmed.

    §transports "Session Management" item 2: once the server assigned a
    session id, "clients using the Streamable HTTP transport MUST include it
    in the Mcp-Session-Id header on all of their subsequent HTTP requests" —
    the listen-stream GET among them. The transport-level envelope id is the
    same "server-error" stand-in the POST-side transport errors use (it
    cannot know a request id for a message that failed before dispatch).
    """
    async with open_established_session() as session:
        resp = await session.client.get(
            session.path,
            headers={
                **ACCEPT,
                "Authorization": BEARER,
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )

        assert resp.status_code == 400, resp.text[:200]
        # A refusal is a JSON document, not a stream: the client must not be
        # left listening on an error.
        assert resp.headers["content-type"].startswith("application/json")
        envelope = json_rpc_messages(resp)[0]
        well_formed_response_envelope(envelope)
        assert envelope["error"]["code"] == -32600
        assert envelope["error"]["message"] == "Bad Request: Missing session ID"
        # The stateful session manager routes a session-less request through
        # its "new session" case, so the refusing transport answers with the
        # *fresh* id it minted for that never-initialized session — an
        # artifact worth pinning: the 400 never hands back the caller's
        # established session id.
        assert resp.headers.get("mcp-session-id"), "expected the fresh session id"
        assert resp.headers["mcp-session-id"] != session.session_id

        live = await session.request("ping")
        assert "result" in live


async def test_get_with_an_unknown_session_id_is_404_session_not_found():
    """A GET carrying a session id the server never issued gets HTTP 404
    with the same well-formed "Session not found" envelope the POST side
    answers — the listen stream is not a side door past session management.

    §transports "Session Management" item 3: after termination "the server
    MUST respond to requests containing that session ID with HTTP 404 Not
    Found" (an id never issued is the degenerate case, and must meet the
    same MUST); item 4 makes that 404 the client's trigger to re-initialize.
    """
    async with open_established_session() as session:
        bogus = "b0gus-b0gus-b0gus-b0gus"
        resp = await session.client.get(
            session.path,
            headers={
                **ACCEPT,
                "Authorization": BEARER,
                "Mcp-Session-Id": bogus,
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )

        assert resp.status_code == 404, resp.text[:200]
        assert resp.headers["content-type"].startswith("application/json")
        envelope = json_rpc_messages(resp)[0]
        well_formed_response_envelope(envelope)
        assert envelope["error"]["code"] == -32600
        assert envelope["error"]["message"] == "Session not found"
        # An unknown-session GET must not be read as a stream assignment.
        assert "mcp-session-id" not in resp.headers

        live = await session.request("ping")
        assert "result" in live


async def test_unauthenticated_get_is_a_401_bearer_challenge():
    """A GET with no bearer token gets HTTP 401 with the WWW-Authenticate
    Bearer challenge — including when a valid session id is presented: the
    auth gate runs before any session logic.

    Per ``test_oauth_conformance.py``'s transport-probe pins (RFC 6750 §3,
    RFC 9728 §5.1): the challenge is scheme ``Bearer`` carrying
    ``error="invalid_token"`` and the RFC 9728 ``resource_metadata`` URL the
    client needs to self-configure, with a JSON ``invalid_token`` body —
    never a stream, and never a 404/400 that would leak session state to an
    unauthenticated caller.
    """
    async with open_established_session() as session:
        for label, headers in [
            ("bare", {**ACCEPT}),
            ("with-session", {**ACCEPT, "Mcp-Session-Id": session.session_id}),
        ]:
            resp = await session.client.get(session.path, headers=headers)

            assert resp.status_code == 401, (label, resp.text[:200])
            scheme, params = _parse_www_authenticate(resp.headers["www-authenticate"])
            assert scheme == "Bearer", label
            assert params.get("error") == "invalid_token", label
            assert "resource_metadata" in params, label
            # The refusal is a JSON error document, not a stream.
            assert resp.headers["content-type"].startswith("application/json"), label
            assert resp.json()["error"] == "invalid_token", label

        # And the authenticated session itself is still live afterwards.
        live = await session.request("ping")
        assert "result" in live


# ---------------------------------------------------------------------------
# DELETE — explicit session termination
# ---------------------------------------------------------------------------


def _assert_session_terminated(resp: httpx.Response, session_id: str) -> dict:
    """The exact shape of the 404 any continued use of a terminated session
    earns (§transports "Session Management" item 3): HTTP 404, application/
    json, a well-formed transport error envelope — the id stand-in
    "server-error" (session routing rejected before dispatch; the request's
    own id was never seen), code -32600, the exact "Session has been
    terminated" message — and the dead session's own id echoed: the
    session's own transport answered, not a session-less global handler.
    """
    assert resp.status_code == 404, resp.text[:200]
    assert resp.headers["content-type"].startswith("application/json")
    envelope = json_rpc_messages(resp)[0]
    well_formed_response_envelope(envelope)
    assert envelope["id"] == "server-error", envelope
    assert envelope["error"]["code"] == -32600, envelope
    assert envelope["error"]["message"] == "Not Found: Session has been terminated"
    assert resp.headers.get("mcp-session-id") == session_id, (
        "the terminated-session 404 must be addressed by the dead id itself"
    )
    return envelope


async def test_delete_with_a_live_session_terminates_the_session():
    """DELETE with the established session id answers 200 with an empty body
    — and the session is actually gone: a subsequent POST on that id gets
    404, while the spec's re-initialize recovery still opens a fresh working
    session.

    §transports "Session Management" item 5: "A client that no longer needs
    a particular session … SHOULD send an HTTP DELETE method to the MCP
    endpoint with the Mcp-Session-Id header, to explicitly terminate the
    session. The server MAY respond with HTTP 405 Method Not Allowed if it
    does not allow clients to terminate sessions." This server allows
    termination, so the 405 escape does not apply and 200-with-no-body is
    its (spec-unpinned, here pinned) success form. Termination being *real*
    is item 3's MUST — after termination the server "MUST respond to
    requests containing that session ID with HTTP 404 Not Found" — and item
    4 makes the fresh InitializeRequest-without-a-session-id the client's
    recovery, asserted working below.
    """
    async with open_established_session() as session:
        sid = session.session_id

        resp = await session.delete()

        assert resp.status_code == 200, resp.text[:200]
        # Acceptance of a termination is an empty body, not a message.
        assert resp.content == b"", resp.content[:100]
        # The 200 echoes the id it just terminated — the session's own
        # transport answered the goodbye.
        assert resp.headers.get("mcp-session-id") == sid

        # --- the termination took: the id no longer serves requests -------
        dead = await session.client.post(
            session.path,
            json={"jsonrpc": "2.0", "id": 42, "method": "ping"},
            headers=session.headers(),
        )
        envelope = _assert_session_terminated(dead, sid)
        # The refusal is the transport's, not the request's: the envelope id
        # is the transport stand-in even though the POST carried id 42 —
        # contrast the in-band initialize errors pinned above, which echo
        # the real request id.
        assert envelope["id"] == "server-error"

        # --- the prescribed recovery: initialize again, without the id ----
        session.session_id = None  # §item 4: the client drops the dead id
        fresh = await session.initialize(id_=2)
        assert fresh.status_code == 200, fresh.text[:200]
        assert session.session_id and session.session_id != sid
        await session.initialized_notification()
        live = await session.request("ping")
        assert "result" in live


async def test_repeated_use_of_a_terminated_session_is_always_404():
    """Every continued use of a terminated session id — POST, GET, and even
    the terminating DELETE again — earns the same exact 404 envelope, and
    the dead id is never resurrected into a working session.

    §transports "Session Management" item 3: after termination the server
    "MUST respond to requests containing that session ID with HTTP 404 Not
    Found" — repeated asks included. There is no re-attach, no lazy
    re-establishment, and no id rotating back into service.
    """
    async with open_established_session() as session:
        sid = session.session_id
        assert (await session.delete()).status_code == 200

        # Continued use takes every method the endpoint speaks.
        repost = await session.client.post(
            session.path,
            json={"jsonrpc": "2.0", "id": 43, "method": "ping"},
            headers=session.headers(),
        )
        _assert_session_terminated(repost, sid)

        reget = await session.client.get(session.path, headers=session.headers())
        _assert_session_terminated(reget, sid)

        # A second DELETE is continued use too — the id was already
        # terminated, so the 200-of-termination cannot happen twice.
        redelete = await session.delete()
        _assert_session_terminated(redelete, sid)


async def test_delete_without_a_session_id_is_a_400_missing_session_error():
    """A DELETE that omits Mcp-Session-Id is refused with HTTP 400 and the
    transport's "Missing session ID" envelope — the same refusal the
    session-less GET earns — and the established session is unharmed.

    §transports "Session Management" item 2: once the server assigned a
    session id, "clients using the Streamable HTTP transport MUST include it
    in the Mcp-Session-Id header on all of their subsequent HTTP requests" —
    the termination request included; item 5's DELETE is addressed to the
    session it names, never to the endpoint at large. The stateful session
    manager routes a header-less request through its new-session case, so —
    the same artifact the session-less GET shows — the refusing transport
    answers with the *fresh* id it just minted: proof the 400 was a
    session-less rejection and not a termination of the caller's real
    session.
    """
    async with open_established_session() as session:
        resp = await session.client.delete(
            session.path,
            headers={
                **ACCEPT,
                "Authorization": BEARER,
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )

        assert resp.status_code == 400, resp.text[:200]
        assert resp.headers["content-type"].startswith("application/json")
        envelope = json_rpc_messages(resp)[0]
        well_formed_response_envelope(envelope)
        assert envelope["id"] == "server-error"
        assert envelope["error"]["code"] == -32600
        assert envelope["error"]["message"] == "Bad Request: Missing session ID"
        assert resp.headers.get("mcp-session-id"), "expected the fresh session id"
        assert resp.headers["mcp-session-id"] != session.session_id

        live = await session.request("ping")
        assert "result" in live


async def test_delete_with_an_unknown_session_id_is_404_session_not_found():
    """A DELETE carrying a session id the server never issued gets HTTP 404
    with the same well-formed "Session not found" envelope the POST and GET
    sides answer — termination is not a side door past session management,
    and an unknown id earns no session id of its own.

    §transports "Session Management" item 3: after termination the server
    "MUST respond to requests containing that session ID with HTTP 404 Not
    Found" — an id never issued is the degenerate terminated case and must
    meet the same MUST, whatever the method; item 4 makes that 404 the
    client's re-initialize trigger.
    """
    async with open_established_session() as session:
        bogus = "b0gus-b0gus-b0gus-b0gus"
        resp = await session.client.delete(
            session.path,
            headers={
                **ACCEPT,
                "Authorization": BEARER,
                "Mcp-Session-Id": bogus,
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )

        assert resp.status_code == 404, resp.text[:200]
        assert resp.headers["content-type"].startswith("application/json")
        envelope = json_rpc_messages(resp)[0]
        well_formed_response_envelope(envelope)
        assert envelope["id"] == "server-error"
        assert envelope["error"]["code"] == -32600
        assert envelope["error"]["message"] == "Session not found"
        # An unknown-session DELETE must not be read as a session assignment.
        assert "mcp-session-id" not in resp.headers

        live = await session.request("ping")
        assert "result" in live


async def test_terminated_sessions_do_not_leak_state_into_new_ones():
    """After terminating session A, a fresh initialize on the same client
    opens session B with its own id that answers its own requests, while A
    stays 404-dead beside it: B neither inherits A's reachability nor
    answers for A, and A cannot borrow B's.

    §transports "Session Management": item 4's recovery is a *new* session
    (a fresh InitializeRequest without a session id) and item 3's 404 MUST
    keeps the terminated id dead independently of whatever else the server
    is serving. Concretely, all three at once: B's id differs from A's, B's
    response answers B's own request id, and A's continued use still gets
    the terminated-404 — addressed by A's own id, so it is provably A's own
    transport answering, not B serving A's traffic out of B's state.
    """
    async with open_established_session() as session:
        dead = session.session_id
        assert (await session.delete()).status_code == 200

        # §item 4: the recovery — a new InitializeRequest, no session id.
        session.session_id = None
        fresh_resp = await session.initialize(id_=2)
        assert fresh_resp.status_code == 200, fresh_resp.text[:200]
        fresh = session.session_id
        assert fresh and fresh != dead
        await session.initialized_notification()

        # B answers for B: the ping's response id is the ping's own.
        live = await session.request("ping")
        assert live["id"] == session.last_id
        assert "result" in live

        # A stays dead beside a living B — and the 404 is addressed by A's
        # own id, so B did not inherit A's reachability.
        old = await session.client.post(
            session.path,
            json={"jsonrpc": "2.0", "id": 44, "method": "ping"},
            headers={
                **ACCEPT,
                "Authorization": BEARER,
                "Mcp-Session-Id": dead,
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )
        _assert_session_terminated(old, dead)
