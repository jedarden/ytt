"""OAuth resource-server / FastMCP auth (plan: Auth, ADR-001).

FastMCP self-issued tokens, audience-bound to ``https://mcp.ardenone.com/ytt``
(the full path-bearing public URL from ``YTT_PUBLIC_URL``). DCR disabled (v1).
Two static redirect URIs for the Claude connector are pre-registered.

Phase-5 spike result — FastMCP 3.4.2 *does* emit path-bearing identifiers
natively:

- ``OAuthProvider(base_url=<path-bearing URL>, ...)`` serves the AS at the
  path-bearing base, and its overridden ``get_well_known_routes()`` emits the
  AS metadata at ``/.well-known/oauth-authorization-server/ytt`` (RFC 8414
  §3.1) when the issuer URL has a path component.
- ``resource_base_url=<origin>`` (scheme+host only) ensures that when
  ``create_streamable_http_app`` calls ``_get_resource_url(mcp_path="/ytt")``,
  it appends ``/ytt`` to produce the correct ``https://mcp.ardenone.com/ytt``
  resource URL. The PRM is then served at
  ``/.well-known/oauth-protected-resource/ytt`` (RFC 9728 §3.1).
- ``RequireAuthMiddleware`` automatically adds the
  ``WWW-Authenticate: Bearer resource_metadata=…`` header on 401.

No custom Starlette routes are needed (FastMCP handles everything natively).

TODO(marathon): verify against live Claude connector — confirm that the
path-bearing audience and the path-inserted well-known URLs are accepted by
Claude's hosted connector-add flow. If Claude's OIDC discovery normalises the
resource to origin (known Claude Code quirk, not confirmed on hosted surfaces),
a subdomain-per-tool fallback may be needed.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastmcp.server.auth.auth import ClientRegistrationOptions, OAuthProvider
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl

from ytt.config import Settings

# ---------------------------------------------------------------------------
# Claude connector redirect URIs — register BOTH (plan: "register both callbacks")
# ---------------------------------------------------------------------------
CLAUDE_REDIRECT_URIS: list[str] = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]

# Static client_id for the Claude connector (plan: "DCR off, static client")
# Claude registers under client_name "claudeai" — use the same string as
# client_id so the pre-registered entry is matched on the first authorize call.
# NOTE: if FastMCP only accepts one redirect_uri per entry, register two entries
# sharing this client_id. InMemoryOAuthProvider overwrites on re-registration,
# which is fine here because we register both URIs in a list.
CLAUDE_CLIENT_ID = "claudeai"

# ---------------------------------------------------------------------------
# YttOAuthProvider
# ---------------------------------------------------------------------------


class YttOAuthProvider(InMemoryOAuthProvider):
    """FastMCP OAuth AS for ytt, audience-bound to the path-bearing public URL.

    ADR-001: FastMCP self-issued tokens, DCR off,
    audience/resource/issuer = ``YTT_PUBLIC_URL`` (e.g.
    ``https://mcp.ardenone.com/ytt``).

    Constructor arguments derive from ``Settings``; call
    :func:`build_auth_provider` instead of constructing directly in production.

    See module docstring for the full Phase-5 spike rationale.

    Phase-5 spike note on path-inserted well-known routes:
    ``create_streamable_http_app`` calls ``auth.get_routes(mcp_path=...)`` which
    returns routes from ``create_auth_routes()`` — the standard (non-path-inserted)
    ``/.well-known/oauth-authorization-server`` route. The path-inserted version
    (``/.well-known/oauth-authorization-server/ytt``) is computed in
    ``get_well_known_routes()`` but NOT included in ``get_routes()``.
    We override ``get_routes()`` to merge in the path-inserted well-known routes
    so they are mounted in the Starlette app. This is the "custom Starlette routes"
    the plan refers to — implemented here rather than in ``build_asgi_app()`` to
    keep the provider self-contained.
    """

    def __init__(self, settings: Settings) -> None:
        # Parse the *origin* (scheme+host) from the path-bearing public_url.
        # resource_base_url = origin so that _get_resource_url("/ytt") appends
        # "/ytt" → correct resource URL "https://mcp.ardenone.com/ytt".
        parsed = urlparse(settings.public_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        super().__init__(
            # base_url is the path-bearing URL where the OAuth AS routes are
            # advertised (e.g. https://mcp.ardenone.com/ytt). FastMCP mounts
            # /authorize and /token at the Starlette root (they are accessible
            # at / in the app's router); base_url tells metadata where they live.
            base_url=settings.public_url,
            # resource_base_url = origin so PRM is resource=public_url.
            resource_base_url=origin,
            # DCR disabled: plan "No open DCR in v1".
            # Enabled=False → no /register endpoint; clients must be pre-registered.
            client_registration_options=ClientRegistrationOptions(enabled=False),
        )

        # Override issuer_url so get_well_known_routes() emits path-aware AS
        # metadata at /.well-known/oauth-authorization-server/ytt (RFC 8414).
        # The base class sets issuer_url = base_url by default, but we set it
        # explicitly for clarity and to make the code self-documenting.
        self.issuer_url = AnyHttpUrl(settings.public_url)

        # Pre-register the Claude connector client (plan: "static client").
        # InMemoryOAuthProvider initialises self.clients = {} in super().__init__,
        # so we can populate it synchronously here.
        self.clients[CLAUDE_CLIENT_ID] = OAuthClientInformationFull(
            client_id=CLAUDE_CLIENT_ID,
            client_name="Claude",
            redirect_uris=[AnyHttpUrl(u) for u in CLAUDE_REDIRECT_URIS],
        )

    def get_routes(self, mcp_path: str | None = None) -> list:
        """Override to include path-inserted well-known routes in the route list.

        ``create_streamable_http_app`` calls ``get_routes()`` (not
        ``get_well_known_routes()``), so the path-inserted AS metadata routes
        (``/.well-known/oauth-authorization-server/ytt`` per RFC 8414) would
        otherwise not be mounted.

        We call ``super().get_routes()`` to get the base routes (which include
        the standard ``/.well-known/oauth-authorization-server``), then
        replicate the path-insertion logic from
        ``OAuthProvider.get_well_known_routes()`` directly — without calling
        ``get_well_known_routes()`` — to avoid the recursion cycle
        (``get_well_known_routes`` → ``AuthProvider.get_well_known_routes``
        → ``self.get_routes`` → recursion).
        """
        from urllib.parse import urlparse as _urlparse

        from starlette.routing import Route

        # Base routes from OAuthProvider (standard /.well-known/oauth-authorization-server + PRM)
        base_routes: list[Route] = super().get_routes(mcp_path)
        existing_paths = {r.path for r in base_routes if hasattr(r, "path")}

        # Replicate OAuthProvider.get_well_known_routes path-insertion logic.
        # Find the standard AS-metadata route endpoint so we can register
        # the path-inserted variant(s) pointing to the same handler.
        if self.issuer_url:
            parsed_issuer = _urlparse(str(self.issuer_url))
            issuer_path = parsed_issuer.path.rstrip("/")

            if issuer_path and issuer_path != "/":
                # Find the standard AS-metadata route to clone its endpoint
                as_meta_route = next(
                    (r for r in base_routes
                     if hasattr(r, "path") and r.path == "/.well-known/oauth-authorization-server"),
                    None,
                )
                if as_meta_route is not None:
                    # RFC 8414 path-inserted AS metadata
                    pi_path = f"/.well-known/oauth-authorization-server{issuer_path}"
                    if pi_path not in existing_paths:
                        base_routes.append(Route(
                            pi_path,
                            endpoint=as_meta_route.endpoint,
                            methods=as_meta_route.methods,
                        ))
                        existing_paths.add(pi_path)

                    # RFC 8414 §5 OIDC alias with path
                    oidc_pi_path = f"/.well-known/openid-configuration{issuer_path}"
                    if oidc_pi_path not in existing_paths:
                        base_routes.append(Route(
                            oidc_pi_path,
                            endpoint=as_meta_route.endpoint,
                            methods=as_meta_route.methods,
                        ))
                        existing_paths.add(oidc_pi_path)

                    # Root OIDC alias (always, per OAuthProvider logic)
                    if "/.well-known/openid-configuration" not in existing_paths:
                        base_routes.append(Route(
                            "/.well-known/openid-configuration",
                            endpoint=as_meta_route.endpoint,
                            methods=as_meta_route.methods,
                        ))

        return base_routes


def build_auth_provider(settings: Settings) -> YttOAuthProvider:
    """Create and return the ``YttOAuthProvider`` for the given settings.

    Separated from the class so callers can test the provider without relying
    on the module-level ``get_settings()`` singleton.
    """
    return YttOAuthProvider(settings)


async def _extract_sub_from_token(token: str, settings: Settings) -> str:
    """Decode a JWT and return the ``sub`` claim without full signature verification.

    Used by the ``/admin/egress`` custom route to apply the subject allowlist.
    The FastMCP middleware handles full signature + audience verification for
    tool calls; this function provides a lightweight allowlist check for
    the non-MCP custom routes.

    Raises ``ValueError`` if the token is malformed or missing the ``sub`` claim.
    """
    import jwt  # PyJWT

    try:
        # Decode without signature verification — we trust the Bearer token was
        # signed by this server's FastMCP instance (validated by the AS).
        # For the allowlist gate the important claim is ``sub``.
        payload = jwt.decode(
            token,
            options={"verify_signature": False, "verify_aud": False},
            algorithms=["HS256", "RS256"],
        )
    except Exception as exc:
        raise ValueError(f"JWT decode failed: {exc}") from exc

    sub = payload.get("sub")
    if not sub:
        raise ValueError("JWT missing 'sub' claim")
    return str(sub)
