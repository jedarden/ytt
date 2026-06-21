"""Whisper async fallback client + job FSM (plan: Whisper fallback).

httpx client for the cluster's ``whisper-openai`` service, WhisperJob
get-or-create FSM (pending->running->done|error), ETA via RT_FACTOR, TTL +
stale-running GC, scratch-volume audio lifecycle + startup sweep + size cap.
Implemented in Phase 6. Scaffold stub.
"""

from __future__ import annotations
