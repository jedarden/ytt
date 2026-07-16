"""OAuth resource-server / FastMCP auth (plan: docs/notes/auth.md, ADR-001).

Federates to Google OAuth via FastMCP's built-in ``GoogleProvider`` — an
``OAuthProxy`` that presents a DCR-compliant AS to MCP clients (Claude) while
proxying the actual login to Google and verifying tokens via Google's
tokeninfo API. This mirrors ibkr-mcp's identity model (see
``~/ibkr-mcp/src/mcp-oauth/``) and intentionally reuses ibkr-mcp's existing
GCP OAuth app: ``YTT_OAUTH_CLIENT_ID``/``YTT_OAUTH_CLIENT_SECRET`` point at
the same GCP client ibkr-mcp uses, with
``https://mcp.ardenone.com/ytt/auth/callback`` registered as an additional
Authorized redirect URI on it.

Do NOT reintroduce FastMCP's ``InMemoryOAuthProvider`` here. It is an
explicit test/demo provider (its own docstring: "Simulates user
authorization") that auto-approves any caller with no login step and issues
opaque tokens with no populated ``subject``/``claims``. A prior version of
this module used it: every caller sharing the static "claudeai" client_id
could reach the tools regardless of ``YTT_ALLOWED_SUBJECTS``, because there
was no real identity to check the allowlist against. See docs/notes/auth.md
("Authentication != Authorization") for the requirement this violated.

The resolved, Google-verified email is checked against
``YTT_ALLOWED_SUBJECTS`` on every tool call by ``ytt.authz.check_subject_auth``,
wired in as global ``AuthMiddleware`` in ``ytt/server.py`` — not just on a
side diagnostic route (the previous gap: ``/admin/egress`` was the only
route that ever called the allowlist check).
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.routing import Route

from fastmcp.server.auth.providers.google import GoogleProvider

from ytt.config import Settings

# ---------------------------------------------------------------------------
# Claude connector redirect URIs — the only redirect URIs ytt's AS will issue
# authorization codes for. Defense in depth: the real access boundary is the
# email allowlist (authz.check_subject_auth), but this keeps DCR from being a
# fully open relay to arbitrary third-party redirect targets.
# ---------------------------------------------------------------------------
CLAUDE_REDIRECT_URIS: list[str] = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]


class YttGoogleProvider(GoogleProvider):
    """``GoogleProvider`` plus path-inserted routes for a path-bearing issuer.

    ``create_streamable_http_app`` mounts whatever ``get_routes()`` returns —
    it never calls ``get_well_known_routes()``. For a path-bearing issuer
    (``https://mcp.ardenone.com/ytt``), the path-inserted AS-metadata route
    (``/.well-known/oauth-authorization-server/ytt``, RFC 8414) that
    ``OAuthProvider.get_well_known_routes()`` computes is therefore never
    mounted unless merged in here.

    Separately — and this is the bigger gap — the MCP SDK's operational OAuth
    routes (``/register``, ``/authorize``, ``/token``, ``/revoke``) and
    OAuthProxy's upstream-IdP callback (``/auth/callback``) are mounted at
    hardcoded BARE paths (see ``mcp.server.auth.routes``:
    ``AUTHORIZATION_PATH = "/authorize"`` etc. — never issuer-path-aware),
    even though the *metadata these same routes advertise*
    (``registration_endpoint``, ``authorization_endpoint``, ...) correctly
    uses the path-bearing issuer URL. ytt's IngressRoute
    (``declarative-config k8s/ardenone-cluster/ytt/ingressroute.yml``)
    forwards only ``PathPrefix("/ytt")`` plus the two well-known suffixes to
    this service — a request to the advertised
    ``https://mcp.ardenone.com/ytt/register`` arrives here as path
    ``/ytt/register``, which has no matching route without this override
    (404 — first hit in production 2026-07-16, "Couldn't register with
    YouTube Transcript's sign-in service"; DCR was never exercised under the
    old InMemoryOAuthProvider design, which had it disabled, so this bug
    predates and is independent of the auth-provider swap).

    Fix: mount every non-well-known route a second time under the issuer
    path, reusing the same endpoint — bare paths keep working too (harmless;
    nothing routes external traffic to them without the ``/ytt`` prefix).
    """

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        base_routes = super().get_routes(mcp_path)

        if not self.issuer_url:
            return base_routes

        issuer_path = urlparse(str(self.issuer_url)).path.rstrip("/")
        if not issuer_path or issuer_path == "/":
            return base_routes

        existing_paths = {r.path for r in base_routes if hasattr(r, "path")}

        # --- RFC 8414 path-inserted AS-metadata + OIDC alias -------------
        as_meta_route = next(
            (
                r
                for r in base_routes
                if hasattr(r, "path")
                and r.path == "/.well-known/oauth-authorization-server"
            ),
            None,
        )
        if as_meta_route is not None:
            for pi_path in (
                f"/.well-known/oauth-authorization-server{issuer_path}",
                f"/.well-known/openid-configuration{issuer_path}",
            ):
                if pi_path not in existing_paths:
                    base_routes.append(
                        Route(
                            pi_path,
                            endpoint=as_meta_route.endpoint,
                            methods=as_meta_route.methods,
                        )
                    )
                    existing_paths.add(pi_path)

            if "/.well-known/openid-configuration" not in existing_paths:
                base_routes.append(
                    Route(
                        "/.well-known/openid-configuration",
                        endpoint=as_meta_route.endpoint,
                        methods=as_meta_route.methods,
                    )
                )
                existing_paths.add("/.well-known/openid-configuration")

        # --- Path-insert every other bare operational route --------------
        # (/register, /authorize, /token, /revoke, /auth/callback, ...) —
        # see class docstring. Skip well-known paths (already handled with
        # RFC-specific insertion rules above) and anything already
        # path-bearing.
        for route in list(base_routes):
            if not isinstance(route, Route):
                continue
            path = route.path
            if path.startswith("/.well-known/") or path.startswith(issuer_path):
                continue
            pi_path = f"{issuer_path}{path}"
            if pi_path in existing_paths:
                continue
            base_routes.append(
                Route(pi_path, endpoint=route.endpoint, methods=route.methods)
            )
            existing_paths.add(pi_path)

        return base_routes


def build_auth_provider(settings: Settings) -> YttGoogleProvider:
    """Create the Google-federated OAuth provider for the given settings.

    Raises ``ValueError`` if Google OAuth credentials are not configured —
    fail fast at startup rather than silently falling back to an
    unauthenticated or fake-authenticated server.
    """
    if not settings.oauth_client_id:
        raise ValueError(
            "YTT_OAUTH_CLIENT_ID is required (Google OAuth client ID from "
            "the ibkr-mcp GCP app — see docs/notes/auth.md). ytt must "
            "federate to a real identity provider; it must never fall back "
            "to an unauthenticated or self-issued-with-no-login provider."
        )

    # Parse the *origin* (scheme+host) from the path-bearing public_url.
    # resource_base_url = origin so that _get_resource_url("/ytt") appends
    # "/ytt" -> correct resource URL "https://mcp.ardenone.com/ytt", instead
    # of doubling the path if resource_base_url defaulted to base_url (which
    # is already path-bearing).
    parsed = urlparse(settings.public_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    return YttGoogleProvider(
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        base_url=settings.public_url,
        resource_base_url=origin,
        required_scopes=["openid", "email"],
        allowed_client_redirect_uris=CLAUDE_REDIRECT_URIS,
        jwt_signing_key=settings.jwt_signing_secret,
    )
