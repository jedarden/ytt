"""Subject-allowlist authorization at the tool-call path (docs/notes/auth.md).

docs/notes/auth.md ("Authentication != Authorization") requires:

- the ``YTT_ALLOWED_SUBJECTS`` allowlist is checked on **every tool call**
  after token validation;
- a non-allowlisted subject is refused (``403`` — realized at the MCP tool
  layer as FastMCP's ``AuthorizationError``, which is what ``AuthMiddleware``
  raises when ``ytt.authz.check_subject_auth`` returns False; the literal
  HTTP 403 lives on the ``/admin/egress`` custom route, which runs the same
  allowlist decision out-of-band);
- an **empty allowlist denies all** (fail-closed).

tests/unit/test_auth.py covers ``check_subject`` / ``check_subject_auth`` as
bare callables. This module drives the real pipeline — ``mcp.call_tool()``
with ``AuthMiddleware`` engaged, the exact path a client's ``tools/call``
takes — so a wiring regression (the middleware dropped from
``FastMCP(...)``, its check swapped for a weaker one, a future tool registered
outside the gate) fails here and not only in production.

Subject shapes (the fail-closed promise rests on all five):

- **empty** — allowlist configured empty: deny all, even a validly
  authenticated caller;
- **allowed** — an allowlisted subject reaches the tool body;
- **denied** — authenticated but not allowlisted: refused, tool body never
  runs;
- **missing** — no resolvable token, or a token carrying no ``email`` claim
  (the claim the gate keys on — an allowlisted ``sub`` alone admits nothing);
- **malformed** — empty-string or non-string ``email`` claim values; an
  exception raised inside the check must deny (FastMCP masks check
  exceptions as denial — pinned here so an upgrade cannot quietly flip the
  pipeline to fail-open).

And: **no tool path bypasses the gate** — every tool registered on the
singleton server is enumerated at runtime and proven refused for a
non-allowlisted subject (so a future third tool is covered automatically),
and the middleware wiring itself is pinned structurally.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import AuthorizationError

from ytt.server import mcp

ALLOWED_SUBJECT = "me@example.com"

#: Tool name → (call args, body-ran success assertion on the structured
#: result). Both success probes are network-free and filesystem-free:
#: a channel URL dies at canonicalize (bad_url) and an empty job registry
#: yields not_found — reaching either shape proves the body executed.
TOOLS = {
    "get_youtube_transcript": {
        "args": {"url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"},
        "ran_result": lambda sc: sc.get("error_code") == "bad_url",
    },
    "get_transcript_job": {
        "args": {"video_id": "dQw4w9WgXcQ"},
        "ran_result": lambda sc: sc.get("error_code") == "not_found",
    },
}


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def allowlist(monkeypatch, tmp_path):
    """Configure YTT_ALLOWED_SUBJECTS and reset the cached Settings.

    Also redirects the last-sub temp file (written by a *successful* allow
    to power ``ytt selftest --show-sub``) into the test's tmp_path so the
    suite never touches the machine-global /tmp/ytt_last_sub.
    """
    import ytt.authz as authz_mod
    from ytt.config import get_settings

    def _set(*entries: str) -> None:
        monkeypatch.setenv("YTT_ALLOWED_SUBJECTS", ",".join(entries))
        get_settings.cache_clear()

    monkeypatch.setattr(authz_mod, "_LAST_SUB_PATH", str(tmp_path / "ytt_last_sub"))
    monkeypatch.setattr(authz_mod, "_written_subs", set())
    get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


def _set_token(monkeypatch, token) -> None:
    """Make ``get_access_token()`` resolve to *token* everywhere it matters.

    FastMCP binds the name into two namespaces this pipeline reads:
    ``fastmcp.server.middleware.authorization`` (AuthMiddleware's check) and
    ``fastmcp.server.dependencies`` (``ytt.server._request_subject``'s
    rate-limit key and the admin route). Patching only the dependencies
    module leaves the middleware resolving the real function, which returns
    None outside a live HTTP request — every call would then be denied for
    the wrong reason.
    """
    from fastmcp.server import dependencies as deps
    from fastmcp.server.middleware import authorization as mw_authz

    monkeypatch.setattr(mw_authz, "get_access_token", lambda: token)
    monkeypatch.setattr(deps, "get_access_token", lambda: token)


def _token(email=None, claims=None):
    """A stand-in for the FastMCP ``AccessToken`` whose claims the gate reads."""
    if claims is None:
        claims = {} if email is None else {"email": email}
    return SimpleNamespace(claims=claims)


@pytest.fixture
def body_spies(monkeypatch):
    """Count actual tool-body executions, per tool.

    ``get_youtube_transcript`` re-imports ``canonicalize`` at call time and
    ``get_transcript_job`` resolves the module-global ``whisper_registry``
    at call time, so patching at the source module / instance is seen by
    the real bodies.
    """
    import ytt.canonicalize as canonicalize_mod
    import ytt.server as server_mod

    calls = dict.fromkeys(TOOLS, 0)

    real_canonicalize = canonicalize_mod.canonicalize

    def spy_canonicalize(url):
        calls["get_youtube_transcript"] += 1
        return real_canonicalize(url)

    monkeypatch.setattr(canonicalize_mod, "canonicalize", spy_canonicalize)

    real_registry_get = server_mod.whisper_registry.get

    async def spy_registry_get(video_id):
        calls["get_transcript_job"] += 1
        return await real_registry_get(video_id)

    monkeypatch.setattr(server_mod.whisper_registry, "get", spy_registry_get)
    return calls


async def _assert_denied(tool: str, args: dict, body_spies, subject_desc: str) -> None:
    """The call must raise AuthorizationError and never reach the tool body."""
    with pytest.raises(AuthorizationError) as exc_info:
        await mcp.call_tool(tool, args)
    # The refusal must not leak the subject to the caller. An empty
    # subject_desc (the malformed empty-string-email case) is a substring of
    # every message, so the leak check is meaningful only for non-empty
    # subjects — denial itself is asserted unconditionally above.
    if subject_desc:
        assert subject_desc not in str(exc_info.value)
    assert body_spies[tool] == 0, (
        f"{tool} body executed for a subject the gate must deny ({subject_desc})"
    )


async def _assert_allowed(tool: str, args: dict, body_spies) -> None:
    """The call must reach the tool body and return its structured result."""
    result = await mcp.call_tool(tool, args)
    sc = result.structured_content
    assert isinstance(sc, dict), f"{tool} returned no structured result: {result!r}"
    assert TOOLS[tool]["ran_result"](sc), f"{tool} unexpected result: {sc}"
    assert body_spies[tool] == 1, f"{tool} body did not execute exactly once"


# ---------------------------------------------------------------------------
# The five subject shapes, across both MCP tools
# ---------------------------------------------------------------------------


class TestSubjectMatrixAcrossBothTools:
    """empty / allowed / denied / missing / malformed × both MCP tools."""

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_empty_allowlist_denies_all(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """Empty allowlist = deny all (fail-closed) — even a well-formed
        authenticated caller who a populated list would admit."""
        allowlist()  # no entries
        _set_token(monkeypatch, _token(ALLOWED_SUBJECT))
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, ALLOWED_SUBJECT)

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_allowed_subject_exact_match_reaches_body(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """An allowlisted subject passes the gate and the tool body runs."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token(ALLOWED_SUBJECT))
        await _assert_allowed(tool, TOOLS[tool]["args"], body_spies)

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_allowed_subject_case_insensitive(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """Matching is case-insensitive on the caller side too (the gate
        lowercases before comparing — same contract as check_subject)."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token("ME@EXAMPLE.COM"))
        await _assert_allowed(tool, TOOLS[tool]["args"], body_spies)

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_allowed_subject_domain_pattern(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """An ``@domain`` allowlist entry admits any address in that domain."""
        allowlist("@example.com")
        _set_token(monkeypatch, _token("anyone@example.com"))
        await _assert_allowed(tool, TOOLS[tool]["args"], body_spies)

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_subject_not_in_allowlist(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """An authenticated subject outside the allowlist is refused and the
        tool body never executes."""
        allowlist(ALLOWED_SUBJECT)
        denied = "mallory@example.com"
        _set_token(monkeypatch, _token(denied))
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, denied)

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_subject_lookalike_domain(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """A domain-pattern entry must not admit a look-alike domain: the
        leading ``@`` anchors the match, so ``@example.com`` refuses
        ``x@evil-example.com`` (shares the suffix, not the domain)."""
        allowlist("@example.com")
        denied = "x@evil-example.com"
        _set_token(monkeypatch, _token(denied))
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, denied)

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_no_token_at_all(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """No resolvable token → denied (the gate's ``ctx.token is None`` leg
        through the real pipeline)."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, None)
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, "None")

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_missing_email_claim(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """A valid token with no ``email`` claim is denied — even when its
        ``sub`` would match the allowlist. The gate authorizes on the email
        claim (docs/notes/auth.md: the Authentik sub IS the account email);
        a sub-only token has no allowlist semantics and admits nothing."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token(claims={"sub": ALLOWED_SUBJECT}))
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, ALLOWED_SUBJECT)

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_malformed_empty_string_email(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """An empty-string email claim is falsy → deny (not a crash path)."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token(""))
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, "")

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_malformed_non_string_email(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """A non-string email claim (int) crashes the string comparison —
        FastMCP masks the check's exception as denial, so the pipeline is
        still fail-closed. Pins that masking: an upgrade flipping it to
        fail-open must break here."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token(123))
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, "123")

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_malformed_list_email(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """A list-valued email claim fails closed the same way (exception
        masked as denial, body never runs)."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token([ALLOWED_SUBJECT]))
        await _assert_denied(
            tool, TOOLS[tool]["args"], body_spies, ALLOWED_SUBJECT
        )

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    async def test_denied_claims_missing_entirely(
        self, allowlist, body_spies, monkeypatch, tool
    ):
        """A token object with ``claims=None`` (no claims dict at all) is
        denied — the gate's ``claims or {}`` leg through the pipeline."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, SimpleNamespace(claims=None))
        await _assert_denied(tool, TOOLS[tool]["args"], body_spies, "None")


# ---------------------------------------------------------------------------
# No tool path bypasses the gate
# ---------------------------------------------------------------------------


class TestNoToolPathBypassesGate:
    """Every registered tool is behind the allowlist — proven, not assumed.

    ``AuthMiddleware`` is global, so the gate holds *by construction* for
    any tool registered on this FastMCP instance. These tests keep that
    construction honest: if the middleware is dropped from
    ``FastMCP(...)`` (ytt/server.py), swapped for a weaker check, or a
    future tool somehow ends up outside it, they fail.
    """

    async def test_gate_is_wired_with_check_subject_auth(self):
        """The singleton server carries exactly one AuthMiddleware and its
        auth callable IS ytt.authz.check_subject_auth (the real allowlist
        decision), not a stub or a weaker check."""
        from fastmcp.server.middleware import AuthMiddleware

        from ytt.authz import check_subject_auth

        gates = [m for m in mcp.middleware if isinstance(m, AuthMiddleware)]
        assert len(gates) == 1, (
            f"expected exactly one AuthMiddleware on the server, "
            f"found {len(gates)}: {mcp.middleware!r}"
        )
        assert gates[0].auth is check_subject_auth, (
            "AuthMiddleware.auth is not ytt.authz.check_subject_auth — "
            f"the tool gate has been swapped for {gates[0].auth!r}"
        )

    async def test_every_registered_tool_denies_non_allowlisted_subject(
        self, allowlist, body_spies, monkeypatch
    ):
        """Enumerate the tools at runtime and prove each one refuses a
        non-allowlisted subject with its body unexecuted. Any tool added to
        the server later is automatically covered by this loop — a new tool
        cannot silently ship outside the gate."""
        allowlist(ALLOWED_SUBJECT)

        # List with an allowed token: list_tools is itself filtered by the
        # same gate, so a denied token would make the registry look empty
        # (tested below) and there would be nothing to iterate.
        _set_token(monkeypatch, _token(ALLOWED_SUBJECT))
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert set(TOOLS) <= names, (
            f"expected at least the two transcript tools, got {names}"
        )

        denied = "mallory@example.com"
        _set_token(monkeypatch, _token(denied))
        for name in sorted(names):
            args = TOOLS.get(name, {}).get("args", {})
            with pytest.raises(AuthorizationError):
                await mcp.call_tool(name, args)
            if name in body_spies:
                assert body_spies[name] == 0, (
                    f"{name} body executed for non-allowlisted subject {denied}"
                )

    async def test_denied_subject_cannot_enumerate_tools(
        self, allowlist, monkeypatch
    ):
        """tools/list runs the same gate: a non-allowlisted caller sees an
        empty registry — tool names are not even discoverable, so there is
        no unauthenticated side door via listing."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token("mallory@example.com"))
        tools = await mcp.list_tools()
        assert tools == [], (
            f"non-allowlisted subject enumerated tools: {[t.name for t in tools]}"
        )

    async def test_allowed_subject_enumerates_both_tools(self, allowlist, monkeypatch):
        """The positive control for the filtering test above: an allowlisted
        subject sees the full registry (the gate filters, it doesn't hide
        everything from everyone)."""
        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token(ALLOWED_SUBJECT))
        names = {t.name for t in await mcp.list_tools()}
        assert set(TOOLS) <= names

    async def test_check_exception_fails_closed(self, allowlist, monkeypatch):
        """If the auth check itself blows up, the pipeline must deny.

        ``run_auth_checks`` masks check exceptions as denial. Pin that: a
        future FastMCP upgrade that lets a crashing check fail OPEN would
        turn any bug in check_subject_auth into a full bypass."""
        from fastmcp.server.middleware import AuthMiddleware

        allowlist(ALLOWED_SUBJECT)
        _set_token(monkeypatch, _token(ALLOWED_SUBJECT))

        gate = next(
            m for m in mcp.middleware if isinstance(m, AuthMiddleware)
        )

        def exploding_check(ctx):
            raise RuntimeError("auth backend unavailable")

        monkeypatch.setattr(gate, "auth", exploding_check)
        with pytest.raises(AuthorizationError):
            await mcp.call_tool("get_transcript_job", {"video_id": "dQw4w9WgXcQ"})
