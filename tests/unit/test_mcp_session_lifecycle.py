"""MCP Streamable-HTTP session-establishment conformance, over the real ASGI
transport.

First slice of ytt-7829b244 (MCP session-lifecycle conformance): pins the
initialize/session-establishment exchange of the 2025-06-18 Streamable HTTP
transport against ``build_asgi_app()`` driven through ``httpx.ASGITransport``
(via the shared ``_mcp_asgi_harness`` — not the Starlette ``TestClient``), so
what these tests see is exactly what uvicorn would put on the wire. The
follow-on children (GET SSE listen stream, DELETE termination, path-prefix
mounting) reuse that harness; this file owns the establishment handshake.

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

Auth is stubbed (harness autouse fixtures): these tests exercise transport
and session lifecycle, not OAuth — the 401/403 auth paths belong to
``test_endpoint_contract.py`` and the tool-level allowlist gate to
``test_mcp_tool_contract.py``.
"""

from __future__ import annotations

import json

import httpx
import pytest

import ytt
from tests.unit._mcp_asgi_harness import (
    ACCEPT,
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
