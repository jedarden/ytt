"""Configuration loader + startup validations (plan: Configuration table).

pydantic-settings ``Settings`` reading the ``YTT_*`` env vars with the exact
names/defaults from the plan's Configuration table, plus startup validations
(Invariant 7 ETA-timeout safety, PVC statvfs check, path-prefix trailing slash).
Implemented in Phase 1. This is a scaffold stub.
"""

from __future__ import annotations
