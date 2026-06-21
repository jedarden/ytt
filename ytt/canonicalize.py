"""URL -> canonical video_id (plan: URL -> canonical video_id, load-bearing).

Collapses every YouTube URL form to the 11-char id before any extract_info,
rejects playlist/channel/search -> ``bad_url``. Satisfies Invariant 3
(``canon(canon(x)) == canon(x)``). Implemented in Phase 1. Scaffold stub.
"""

from __future__ import annotations
