"""Test-session env defaults.

``ytt.server`` builds a module-level FastMCP app at import time
(``mcp = _build_app()``), which requires ``YTT_OAUTH_CLIENT_ID`` (see
``ytt.auth.build_auth_provider`` — it raises rather than silently falling
back to an unauthenticated provider). Set safe fake defaults here, before any
test module imports ``ytt.server``, so the unit suite doesn't need real
Authentik OAuth credentials. ``setdefault`` never overrides real values from
the environment (e.g. an integration run against a live cluster).

ADR-003 (``docs/plan/plan.md``): ``ytt.auth.YttOIDCProvider`` is built on
FastMCP's ``OIDCProxy``, which — unlike the ``GoogleProvider`` it replaced —
performs a **live HTTP discovery request** (``GET config_url``) at
*construction* time, not at request time. Since construction happens inside
the same import-time ``_build_app()`` call above, every unit test module that
imports ``ytt.server`` (directly or transitively) would otherwise make a real
network call to ``sso.ardenone.com`` during test collection — flaky, slow,
and wrong even once the "ytt" Authentik application exists (a unit test
should not depend on live infra). Patch the discovery call at class level to
return a fixed, structurally-valid ``OIDCConfiguration`` before any import of
``ytt.server``. This only affects ``tests/unit/*`` — the integration suite
(``tests/integration/conftest.py``) never imports ``ytt.server``; it only
speaks HTTP to an already-running server, so it is unaffected and still
exercises the real discovery flow in-cluster.
"""

import os

from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy

os.environ.setdefault("YTT_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("YTT_OAUTH_CLIENT_SECRET", "test-client-secret")

# YTT_PUBLIC_URL is required with no fallback (see ytt/config.py): Settings
# construction fails without it. Default it to the absolute base the OAuth
# conformance suite hard-codes (BASE = "https://mcp.ardenone.com") — the same
# value the retired baked-in default carried, now explicit test state instead
# of product state. setdefault keeps an explicit env (e.g. an image smoke
# run driving this suite) in charge.
os.environ.setdefault("YTT_PUBLIC_URL", "https://mcp.ardenone.com/ytt")

_FAKE_OIDC_CONFIG = OIDCConfiguration(
    issuer="https://sso.ardenone.com/application/o/ytt/",
    authorization_endpoint="https://sso.ardenone.com/application/o/authorize/",
    token_endpoint="https://sso.ardenone.com/application/o/token/",
    jwks_uri="https://sso.ardenone.com/application/o/ytt/jwks/",
    response_types_supported=["code"],
    subject_types_supported=["public"],
    id_token_signing_alg_values_supported=["RS256"],
)


def _fake_get_oidc_configuration(self, config_url, strict, timeout_seconds):
    return _FAKE_OIDC_CONFIG


OIDCProxy.get_oidc_configuration = _fake_get_oidc_configuration
