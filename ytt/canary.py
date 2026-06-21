"""Standalone residential-egress canary (plan: Observability / Canary).

A long-running probe that calls yt-dlp directly (same code path as fetch.py, NOT
via the HTTP tool endpoint — bypasses OAuth) against a fixed internal video
list, exposing ``ytt_canary_*`` metrics. Runs as its own Deployment.
Implemented in Phase 8. Scaffold stub.
"""

from __future__ import annotations
