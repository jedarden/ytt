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
    """``GoogleProvider`` plus RFC 8414/9728 path-inserted well-known routes.

    ``create_streamable_http_app`` mounts whatever ``get_routes()`` returns —
    it never calls ``get_well_known_routes()``. For a path-bearing issuer
    (``https://mcp.ardenone.com/ytt``), the path-inserted AS-metadata route
    (``/.well-known/oauth-authorization-server/ytt``) that
    ``OAuthProvider.get_well_known_routes()`` computes is therefore never
    mounted unless merged in here.

    This matters because ytt's IngressRoute
    (``declarative-config k8s/ardenone-cluster/ytt/ingressroute.yml``)
    forwards ONLY the path-inserted well-known suffixes to this service —
    the bare ``/.well-known/*`` prefix is routed to ibkr-mcp, which shares
    the ``mcp.ardenone.com`` host. Without this override, Claude's OAuth
    discovery 404s.

    The protected-resource-metadata route
    (``/.well-known/oauth-protected-resource/ytt``) does not need the same
    treatment — ``OAuthProvider.get_routes()`` already derives it directly
    from ``resource_base_url``.
    """

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        base_routes = super().get_routes(mcp_path)

        if not self.issuer_url:
            return base_routes

        issuer_path = urlparse(str(self.issuer_url)).path.rstrip("/")
        if not issuer_path or issuer_path == "/":
            return base_routes

        existing_paths = {r.path for r in base_routes if hasattr(r, "path")}
        as_meta_route = next(
            (
                r
                for r in base_routes
                if hasattr(r, "path")
                and r.path == "/.well-known/oauth-authorization-server"
            ),
            None,
        )
        if as_meta_route is None:
            return base_routes

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
