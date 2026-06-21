"""json3 caption parsing + rolling-caption dedup (plan: Fetch core, step 2).

The #1 "looks done, is broken" bug: auto-caption (``kind == "asr"``) tracks
re-emit prior words plus one new word per event; naive concat doubles the text.
Implements the ascending-tStartMs window-cover algorithm + prefix check.
Implemented in Phase 2. Scaffold stub.
"""

from __future__ import annotations
