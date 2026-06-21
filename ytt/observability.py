"""Observability — metrics + structured logging (plan: Observability).

prometheus-client metrics (the exact label sets from the plan), structlog JSON
to stdout with a redaction filter (tokens / subject list / transcript bodies /
credential-bearing URLs never logged). Implemented in Phase 8. Scaffold stub.
"""

from __future__ import annotations
