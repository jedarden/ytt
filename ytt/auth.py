"""OAuth resource-server / FastMCP auth (plan: Auth, ADR-001).

FastMCP self-issued tokens, audience-bound to ``https://mcp.ardenone.com/ytt``,
DCR off; path-inserted ``.well-known`` metadata (custom Starlette routes if
FastMCP can't emit path-bearing identifiers); 401 + WWW-Authenticate.
Implemented in Phase 5. Scaffold stub.
"""

from __future__ import annotations
