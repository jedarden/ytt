"""Test-session env defaults.

``ytt.server`` builds a module-level FastMCP app at import time
(``mcp = _build_app()``), which requires ``YTT_OAUTH_CLIENT_ID`` (see
``ytt.auth.build_auth_provider`` — it raises rather than silently falling
back to an unauthenticated provider). Set safe fake defaults here, before any
test module imports ``ytt.server``, so the unit suite doesn't need real
Google OAuth credentials. ``setdefault`` never overrides real values from the
environment (e.g. an integration run against a live cluster).
"""

import os

os.environ.setdefault(
    "YTT_OAUTH_CLIENT_ID", "test-client-id.apps.googleusercontent.com"
)
os.environ.setdefault("YTT_OAUTH_CLIENT_SECRET", "test-client-secret")
