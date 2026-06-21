"""Flat-file, size-bounded LRU cache (plan: Caching).

Atomic ``(video_id, lang)``/``(video_id, whisper)`` units (``.txt`` + optional
``.json`` sidecar), whole-unit LRU under one asyncio lock, byte-cap invariant
(Invariant 1), reconcile, ENOSPC degrade, startup scan/.tmp clean.
Implemented in Phase 4. Scaffold stub.
"""

from __future__ import annotations
