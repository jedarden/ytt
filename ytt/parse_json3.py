"""json3 caption parsing + rolling-caption dedup (plan: Fetch core, step 2).

Auto-caption (``kind == "asr"``) tracks are a *rolling* stream.  Two real
shapes exist, and dedup must handle both without losing real text:

*Classic re-emit* — each event repeats prior words plus new ones
(``"This is a test"`` → ``"is a test"`` → ``"a test"`` → ``"test"``);
naive ``"".join(event.segs.utf8)`` doubles the text and poisons the cache
(yt-dlp gotchas #6274/#1734).

*Modern line-roll* (verified against a live capture,
``tests/fixtures/rolling_asr_real.json``) — consecutive content events
overlap in *time* (each ``dDurationMs`` overhangs the next event's
``tStartMs``) while carrying **disjoint** text, interleaved with empty
"erase" events (``{"utf8": "\\n"}``, some with ``aAppend``) and a leading
event with no ``segs`` at all.  On this shape, window-coverage gating
(``tStartMs >= last_end_ms``) silently drops real lines — it was removed
in favour of text-subsumption (see the fixture's ``_provenance``).

Algorithm (current):
    1. Sort events by tStartMs ascending; keep only events with text.
    2. Drop an event whose text is a **strict suffix** of the previous
       content event's text (a re-emitted tail).
    3. Drop an event whose text is a **strict prefix** of the next content
       event's text (a partial line fully contained in its successor).
    4. Build ``Segment`` objects (seconds, not ms) from the survivors.

Comparisons are exact and strict (equal neighbours are both kept).  A
dropped event's words always survive inside its neighbour's text, so dedup
can never lose transcript content — the invariant window arithmetic could
not give.

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
    """Rolling auto-caption dedup by neighbour text-subsumption.

    Steps
    -----
    1. Sort by tStartMs ascending; drop events with no text
       (formatting-only / no ``segs``).
    2. Drop an event whose text is a strict suffix of the immediately
       previous content event's text (classic re-emit tail).
    3. Drop an event *equal* to the previous one **whose window overlaps
       the previous event's window** — the same line re-emitted.  Equal
       text in non-overlapping windows is a real repeat (refrains) and is
       kept.
    4. Drop an event whose text is a strict prefix of the immediately
       next content event's text (partial line subsumed by its successor).
    5. Survivors become :class:`~ytt.models.Segment` (ms → seconds).

    Only *subsumed* text is ever discarded — events that merely overlap in
    time are kept, because on modern tracks overlapping windows carry
    disjoint, genuinely new lines.
    """
    candidates: list[tuple[int, int, str]] = []  # (tStartMs, dDurationMs, text)
    for event in sorted(events, key=lambda e: e.get("tStartMs", 0)):
        text = _event_text(event)
        if not text:
            continue
        t_start: int = event.get("tStartMs", 0)
        d_dur: int = event.get("dDurationMs", 0)
        candidates.append((t_start, d_dur, text))

    kept: list[tuple[int, int, str]] = []
    total = len(candidates)
    for i, (t_start, d_dur, text) in enumerate(candidates):
        prev_t, prev_d, prev_text = (
            candidates[i - 1] if i > 0 else (0, 0, "")
        )
        next_text = candidates[i + 1][2] if i + 1 < total else ""
        if prev_text:
            if text == prev_text and t_start < prev_t + prev_d:
                continue  # identical line re-emitted inside the same window
            if text != prev_text and prev_text.endswith(text):
                continue  # re-emitted tail of the previous line
        if next_text and text != next_text and next_text.startswith(text):
            continue  # partial line fully contained in the next line
        kept.append((t_start, d_dur, text))

    return [
        Segment(start=t / 1000.0, duration=d / 1000.0, text=txt)
        for t, d, txt in kept
    ]
