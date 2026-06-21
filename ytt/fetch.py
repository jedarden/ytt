"""Fetch core — yt-dlp caption/metadata extraction (plan: Fetch core).

In-process yt-dlp (json3 captions, language selection, metadata, error
taxonomy). Enforces no-cookies (``cookiefile=None``/``cookiesfrombrowser=None``)
and the ``tv/web_embedded/mweb`` player_client override. Implemented in
Phase 2. Scaffold stub.
"""

from __future__ import annotations
