"""json3 caption parsing + rolling-caption dedup (plan: Fetch core, step 2).

The #1 "looks done, is broken" bug: auto-caption (``kind == "asr"``) tracks
are a *rolling* stream — YouTube's ASR engine re-emits prior words plus one
new word per event, producing overlapping ``[tStartMs, tStartMs+dDurationMs]``
windows.  Naive ``"".join(event.segs.utf8)`` over all events doubles every
word and poisons the cache (confirmed yt-dlp gotchas #6274/#1734).

Algorithm (plan §Fetch core step 2):
    1. Sort events by tStartMs ascending.
    2. Maintain ``last_end_ms = 0``.
    3. Skip events with no UTF-8 content (formatting-only).
    4. **Primary dedup:** emit event only if ``tStartMs >= last_end_ms``;
       on emit update ``last_end_ms = tStartMs + dDurationMs``.
    5. **Prefix check:** in the emitted list, discard event *i* if its text
       is a whitespace-stripped, case-sensitive strict prefix of event *i+1*.
    6. Build ``Segment`` objects (seconds, not ms).

Manual tracks (``kind != "asr"``) need no dedup — straight concat.
"""

from __future__ import annotations

from ytt.models import Segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _segs_to_text(segs: list[dict]) -> str:
    """Concatenate ``segs[].utf8`` fields, respecting ``aAppend``/``pAppend``.

    ``aAppend=1`` — seg continues the previous word with no leading space.
    ``pAppend=1`` — seg is punctuation; no leading space.
    Otherwise a space is inserted at the junction unless the surrounding
    characters already provide whitespace.
    """
    parts: list[str] = []
    prev_text = ""
    for seg in segs:
        utf8: str = seg.get("utf8", "")
        if not utf8:
            continue
        if prev_text and not seg.get("aAppend") and not seg.get("pAppend"):
            # Insert a space only when neither end already has whitespace
            if not prev_text[-1].isspace() and not utf8[0].isspace():
                parts.append(" ")
        parts.append(utf8)
        prev_text = utf8
    return "".join(parts).strip()


def _event_text(event: dict) -> str:
    """Return the plain text for a json3 event, or '' for formatting events."""
    segs = event.get("segs") or []
    return _segs_to_text(segs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_json3(events: list[dict], *, kind: str = "asr") -> list[Segment]:
    """Parse json3 ``events`` into a list of :class:`~ytt.models.Segment`.

    Parameters
    ----------
    events:
        The ``events`` array from a json3 timedtext dict (already decoded).
    kind:
        ``"asr"`` for auto-generated captions (rolling dedup applied);
        any other value for manual/non-rolling tracks (straight concat).

    Returns
    -------
    list[Segment]
        Ordered, deduplicated segments in chronological order.
    """
    if kind != "asr":
        return _parse_manual(events)
    return _parse_asr(events)


# ---------------------------------------------------------------------------
# Manual (non-rolling) track
# ---------------------------------------------------------------------------

def _parse_manual(events: list[dict]) -> list[Segment]:
    """Straight concat — no dedup needed for manual/uploaded tracks."""
    result: list[Segment] = []
    for event in events:
        text = _event_text(event)
        if not text:
            continue
        start_ms: int = event.get("tStartMs", 0)
        dur_ms: int = event.get("dDurationMs", 0)
        result.append(Segment(
            start=start_ms / 1000.0,
            duration=dur_ms / 1000.0,
            text=text,
        ))
    return result


# ---------------------------------------------------------------------------
# ASR (rolling) track — primary dedup + prefix check
# ---------------------------------------------------------------------------

def _parse_asr(events: list[dict]) -> list[Segment]:
    """Rolling auto-caption dedup.

    Steps
    -----
    1. Sort by tStartMs ascending.
    2. Primary dedup: emit first event in each non-overlapping window.
    3. Prefix check: discard any emitted segment whose text is a strict
       whitespace-stripped prefix of the immediately following segment.
    """
    sorted_events = sorted(events, key=lambda e: e.get("tStartMs", 0))

    # --- Step 1: primary window-coverage dedup ---
    emitted: list[tuple[int, int, str]] = []  # (tStartMs, dDurationMs, text)
    last_end_ms: int = 0

    for event in sorted_events:
        text = _event_text(event)
        if not text:
            continue
        t_start: int = event.get("tStartMs", 0)
        d_dur: int = event.get("dDurationMs", 0)
        if t_start >= last_end_ms:
            emitted.append((t_start, d_dur, text))
            last_end_ms = t_start + d_dur

    # --- Step 2: prefix check (secondary dedup) ---
    filtered: list[tuple[int, int, str]] = []
    for i, (t_start, d_dur, text) in enumerate(emitted):
        if i + 1 < len(emitted):
            next_text = emitted[i + 1][2]
            stripped = text.strip()
            next_stripped = next_text.strip()
            # Discard if text is a strict prefix of the next segment's text
            if stripped and next_stripped.startswith(stripped) and stripped != next_stripped:
                continue
        filtered.append((t_start, d_dur, text))

    return [
        Segment(start=t / 1000.0, duration=d / 1000.0, text=txt)
        for t, d, txt in filtered
    ]
