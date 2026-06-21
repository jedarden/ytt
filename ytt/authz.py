"""Authorization — subject allowlist (plan: Security & Authorization).

Checks the token ``sub`` claim against ``YTT_ALLOWED_SUBJECTS`` on every tool
call; empty allowlist = deny all (fail-closed); not listed -> 403. Implemented
in Phase 5. Scaffold stub.
"""

from __future__ import annotations
