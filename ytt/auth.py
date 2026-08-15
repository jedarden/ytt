"""OAuth resource-server / FastMCP auth (plan: docs/notes/auth.md, ADR-003).

Federates to the org's self-hosted Authentik (``sso.ardenone.com``) via
FastMCP's generic ``OIDCProxy`` — an ``OAuthProxy`` that presents a
DCR-compliant AS to MCP clients (Claude) while proxying the actual login to
Authentik and verifying the resulting JWT against Authentik's JWKS. All
endpoints are discovered from Authentik's per-application config document
(``AUTHENTIK_OIDC_CONFIG_URL`` below) rather than hardcoded, unlike the
Google-specific predecessor this replaces (ADR-003 in ``docs/plan/plan.md``).

ytt has its own Authentik application/client (``ytt``) — it does **not**
share a client with ibkr-mcp the way the old Google setup did (that was an
unintentional coupling: two independent servers' authorization depended on
one shared external credential). ``YTT_OAUTH_CLIENT_ID``/
``YTT_OAUTH_CLIENT_SECRET`` are Authentik-generated values scoped to this
application only.

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

from fastmcp.server.auth.oidc_proxy import OIDCProxy

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

# Authentik's per-application OIDC discovery document. The "ytt" slug must
# match the application slug set in declarative-config's
# k8s/ardenone-cluster/authentik/authentik-blueprints-configmap.yml (ADR-003).
AUTHENTIK_OIDC_CONFIG_URL = (
    "https://sso.ardenone.com/application/o/ytt/.well-known/openid-configuration"
)


class YttOIDCProvider(OIDCProxy):
    """``OIDCProxy`` plus path-inserted routes for a path-bearing issuer.

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


def build_auth_provider(settings: Settings) -> YttOIDCProvider:
    """Create the Authentik-federated OAuth provider for the given settings.

    Raises ``ValueError`` if OIDC credentials are not configured — fail fast
    at startup rather than silently falling back to an unauthenticated or
    fake-authenticated server.
    """
    if not settings.oauth_client_id:
        raise ValueError(
            "YTT_OAUTH_CLIENT_ID is required (Authentik OAuth2 client ID for "
            "the 'ytt' application on sso.ardenone.com — see "
            "docs/notes/auth.md, ADR-003). ytt must federate to a real "
            "identity provider; it must never fall back to an "
            "unauthenticated or self-issued-with-no-login provider."
        )

    # Parse the *origin* (scheme+host) from the path-bearing public_url.
    # resource_base_url = origin so that _get_resource_url("/ytt") appends
    # "/ytt" -> correct resource URL "https://mcp.ardenone.com/ytt", instead
    # of doubling the path if resource_base_url defaulted to base_url (which
    # is already path-bearing).
    parsed = urlparse(settings.public_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # ADR-003 open verification item, resolve at first live connector re-auth
    # (don't assume): ytt.authz.check_subject_auth reads email/email_verified
    # off the *access-token* JWT claims (default OIDCProxy behavior below —
    # verify_id_token left unset). Google's opaque-token verifier populated
    # those on the access token; whether Authentik's access-token JWT does
    # too depends on its scope-mapping config, and its default OpenID scope
    # mapping is documented to land them on the ID token instead. If a real
    # login comes back with claims missing email/email_verified, pass
    # verify_id_token=True here (OIDCProxy verifies the id_token instead of
    # the access_token) rather than reaching into the ID token by hand.
    return YttOIDCProvider(
        config_url=AUTHENTIK_OIDC_CONFIG_URL,
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        base_url=settings.public_url,
        resource_base_url=origin,
        required_scopes=["openid", "email"],
        allowed_client_redirect_uris=CLAUDE_REDIRECT_URIS,
        jwt_signing_key=settings.jwt_signing_secret,
    )
