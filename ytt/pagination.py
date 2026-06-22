"""Response shape / pagination layer (plan: §Response shape & size, Phase 7).

Implements:
- Cursor generation + validation (content-hash bound; cursor_stale on change/eviction)
- Transcript filtering (start/end time bounds; case-insensitive query substring ±2 context)
- Char-offset chunking (mode=full|chunk, inline vs paginate, non-Latin byte budget)
- Segment-aligned chunk boundaries
- Loud PARTIAL prefix + machine-readable structuredContent fields

Cursor format (plan §Response shape):
    base64url(sha256(json.dumps({
        "c": base64(content_bytes).decode(),
        "filter": canonical_filter_args,
        "lang": served_lang,
        "source": source,
    }, sort_keys=True).encode())[:16]) + ":" + str(total_chars) + ":" + str(offset)

    canonical_filter_args = sorted-key dict of active filter args (query, start, end);
    empty dict {} if no filter is active.

Plan references: §Response shape & size, §Data Models (per-status field matrix).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

from ytt import errors
from ytt.cache import CacheHit
from ytt.models import TRANSCRIPT_QUALITY

if TYPE_CHECKING:
    from ytt.config import Settings


# ---------------------------------------------------------------------------
# Cursor: build, parse, validate
# ---------------------------------------------------------------------------


def _canonical_filter(filter_args: dict[str, Any]) -> dict[str, Any]:
    """Return canonical filter: sorted keys, None/missing values excluded.

    Plan: "canonical_filter_args = dict with sorted keys of active filter args
    (query, start, end); empty dict {} if no filter is active."
    """
    return {k: filter_args[k] for k in sorted(filter_args) if filter_args.get(k) is not None}


def _cursor_hash(
    content: bytes,
    lang: str,
    source: str,
    filter_args: dict[str, Any],
) -> str:
    """Compute the 16-byte SHA-256 prefix of the cursor payload, base64url-encoded."""
    payload = json.dumps(
        {
            "c": base64.b64encode(content).decode(),
            "filter": _canonical_filter(filter_args),
            "lang": lang,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(payload).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_cursor(
    content: bytes,
    lang: str,
    source: str,
    filter_args: dict[str, Any],
    total_chars: int,
    offset: int,
) -> str:
    """Build an opaque pagination cursor.

    Plan: "Cursor. Opaque string = base64url(sha256(...)[:16]) + ':' + total_chars
    + ':' + offset.  Example: 'abc123def456ghi7:45000:18000'."

    The hash encodes (content, lang, source, filter_args); any change makes the
    cursor stale (→ ``error_code: cursor_stale``).  Use structured JSON
    serialization (not raw byte concatenation) to avoid hash collisions between
    distinct ``(content, lang)`` pairs.
    """
    h = _cursor_hash(content, lang, source, filter_args)
    return f"{h}:{total_chars}:{offset}"


def parse_cursor(cursor: str) -> tuple[str, int, int] | None:
    """Parse a cursor into ``(hash_part, total_chars, offset)``.

    Returns ``None`` for malformed cursors (wrong segment count, non-integer fields).
    """
    parts = cursor.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except (ValueError, TypeError):
        return None


def validate_cursor(
    cursor: str,
    content: bytes,
    lang: str,
    source: str,
    filter_args: dict[str, Any],
) -> int | None:
    """Validate ``cursor`` against current (content, lang, source, filter_args).

    Returns the continuation offset on success, ``None`` on stale cursor.

    Plan: "If the unit changed/refreshed → cursor_stale (force fresh page-1).
    If the unit was evicted between pages → also cursor_stale (never silently
    re-fetch and serve at the old offset, which could swap content)."
    """
    parsed = parse_cursor(cursor)
    if parsed is None:
        return None
    h, total_chars, offset = parsed
    if _cursor_hash(content, lang, source, filter_args) != h:
        return None  # hash mismatch → stale
    if offset < 0 or offset > total_chars:
        return None  # invalid offset
    return offset


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_segments_by_time(
    segments: list[dict[str, Any]],
    start: float | None,
    end: float | None,
) -> list[dict[str, Any]]:
    """Filter segments whose window overlaps the [start, end] interval.

    Plan: "start/end (segment time bounds)".
    A segment is included if any part of its [start, start+duration] window
    overlaps the requested [start, end] range.
    """
    result: list[dict[str, Any]] = []
    for seg in segments:
        seg_start = float(seg.get("start", 0.0))
        seg_end = seg_start + float(seg.get("duration", 0.0))
        if start is not None and seg_end < start:
            continue  # segment ends before the window
        if end is not None and seg_start > end:
            continue  # segment starts after the window
        result.append(seg)
    return result


def filter_segments_by_query(
    segments: list[dict[str, Any]],
    query: str,
    context_window: int = 2,
) -> list[dict[str, Any]]:
    """Case-insensitive substring match, ±``context_window`` segments of context.

    Plan: "query (case-insensitive substring match, returns matching segments
    ±2 segments of context, mutually exclusive with start/end)."
    """
    q_lower = query.lower()
    included: set[int] = set()
    for i, seg in enumerate(segments):
        if q_lower in seg.get("text", "").lower():
            lo = max(0, i - context_window)
            hi = min(len(segments), i + context_window + 1)
            included.update(range(lo, hi))
    return [segments[i] for i in sorted(included)]


def segments_to_text(segments: list[dict[str, Any]]) -> str:
    """Join segment texts with single spaces, stripped."""
    return " ".join(s.get("text", "") for s in segments).strip()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def effective_chunk_chars(text: str, chunk_chars: int) -> int:
    """Adjust ``chunk_chars`` for non-Latin scripts (byte-based token budget).

    Plan: "Dense CJK/Thai/Arabic run ~1–2 chars/token, so for non-Latin scripts
    estimate tokens as bytes/3 and cap on that — a fixed char count alone can
    approach the 25K ceiling in one chunk."

    For pure-ASCII/Latin text (byte_count ≈ char_count) returns ``chunk_chars``
    unchanged.  For scripts where byte_count > char_count (multibyte sequences),
    caps at ``byte_count // 3`` to keep the token budget ≤ ~6 K tokens.
    """
    byte_count = len(text.encode("utf-8"))
    if byte_count > len(text):
        # Multi-byte content present; use bytes/3 as token-budget limit
        return max(1, min(chunk_chars, byte_count // 3))
    return chunk_chars


def chunk_text(text: str, offset: int, chunk_chars: int) -> tuple[str, int, bool]:
    """Chunk ``text`` at ``offset``, up to ``chunk_chars`` Unicode code points.

    Returns ``(chunk_text, next_offset, is_final)``.

    Plan: "Char-offset chunking (YTT_CHUNK_CHARS), don't split mid-multibyte."
    Python str indices are always on code-point boundaries, so no mid-multibyte
    split is possible.
    """
    if offset >= len(text):
        return "", len(text), True
    end = min(offset + chunk_chars, len(text))
    return text[offset:end], end, end >= len(text)


def _find_segment_end_pos(
    segments: list[dict[str, Any]],
    max_pos: int,
    start_offset: int,
) -> int:
    """Find the char position of the last complete segment end ≤ ``max_pos``.

    Segments are assumed to be joined as ``" ".join(seg["text"] …)``, so each
    segment occupies ``[pos, pos+len(text))`` with a +1 space separator between
    them.  Returns ``max_pos`` if no boundary is found after ``start_offset``.
    """
    pos = 0
    best = max_pos  # fallback: use raw chunk end
    for seg in segments:
        seg_text = seg.get("text", "")
        seg_end = pos + len(seg_text)
        if seg_end > max_pos:
            break
        if seg_end > start_offset:
            best = seg_end
        pos = seg_end + 1  # +1 for " " separator
    return best


def chunk_text_with_segments(
    text: str,
    segments: list[dict[str, Any]],
    offset: int,
    chunk_chars: int,
) -> tuple[str, list[dict[str, Any]], int, bool]:
    """Chunk ``text`` aligned to a segment boundary.

    Returns ``(chunk_text, chunk_segments, next_offset, is_final)``.
    Plan: "segment-align when segments are returned."
    """
    if offset >= len(text):
        return "", [], len(text), True

    raw_end = min(offset + chunk_chars, len(text))
    # Prefer a segment boundary at or before raw_end
    seg_end = _find_segment_end_pos(segments, raw_end, offset)
    if seg_end <= offset:
        seg_end = raw_end  # no boundary found; fall back to raw

    chunk = text[offset:seg_end]
    next_offset = seg_end
    is_final = next_offset >= len(text)

    # Collect segments whose text spans fall within [offset, seg_end)
    pos = 0
    chunk_segs: list[dict[str, Any]] = []
    for seg in segments:
        seg_text = seg.get("text", "")
        seg_s = pos
        seg_e = pos + len(seg_text)
        pos = seg_e + 1
        if seg_e <= offset:
            continue  # entirely before chunk
        if seg_s >= seg_end:
            break   # entirely after chunk
        chunk_segs.append(seg)

    return chunk, chunk_segs, next_offset, is_final


# ---------------------------------------------------------------------------
# Loud PARTIAL prefix
# ---------------------------------------------------------------------------


def loud_partial_prefix(
    offset: int,
    next_offset: int,
    total_chars: int,
    chunk_i: int,
    n_chunks: int,
    next_cursor: str,
) -> str:
    """Build the ⚠️ PARTIAL warning that leads every non-final chunk.

    Plan: "Non-final chunk text leads with ⚠️ PARTIAL: chars A–B of T
    (chunk i/n). INCOMPLETE — call get_youtube_transcript again with
    cursor='…' before summarizing, unless the user only needs the start."
    """
    a = offset + 1   # 1-indexed start char
    b = next_offset  # exclusive end (= first char of next chunk)
    return (
        f"⚠️ PARTIAL: chars {a}–{b} of {total_chars} "
        f"(chunk {chunk_i}/{n_chunks}). "
        f"INCOMPLETE — call get_youtube_transcript again with "
        f"cursor='{next_cursor}' before summarizing, "
        f"unless the user only needs the start.\n\n"
    )


# ---------------------------------------------------------------------------
# Main entry point: build_page
# ---------------------------------------------------------------------------


def build_page(
    hit: CacheHit,
    mode: str,
    filter_args: dict[str, Any],
    settings: "Settings",
    cursor: str | None = None,
) -> dict[str, Any]:
    """Build a TranscriptResult dict from a :class:`~ytt.cache.CacheHit`.

    Handles inline vs paginated delivery, filtering, cursor validation,
    and the loud PARTIAL prefix for non-final chunks.

    Args:
        hit:         Cache hit (source of transcript text + segments + metadata).
        mode:        ``"full"`` (inline if short, else chunk-1+cursor) or
                     ``"chunk"`` (always paginate regardless of length).
        filter_args: Active filter args (any of ``query``, ``start``, ``end``).
        settings:    Server settings (``inline_char_limit``, ``chunk_chars``).
        cursor:      Opaque continuation cursor (``None`` for first page).

    Returns:
        A TranscriptResult-shaped dict.

    Plan: §Response shape & size — "chunk pagination (char offset + content-hash
    cursor + cursor_stale + loud PARTIAL + structuredContent), start/end/query
    slicing, mode semantics."
    """
    segs_raw: list[dict[str, Any]] = hit.segments or []

    # --- 1. Apply filters -------------------------------------------------------
    query: str | None = filter_args.get("query")
    start: float | None = filter_args.get("start")
    end: float | None = filter_args.get("end")

    if segs_raw:
        if query is not None:
            filtered_segs = filter_segments_by_query(segs_raw, query)
            text = segments_to_text(filtered_segs)
        elif start is not None or end is not None:
            filtered_segs = filter_segments_by_time(segs_raw, start, end)
            text = segments_to_text(filtered_segs)
        else:
            filtered_segs = segs_raw
            # Use the pre-joined text from cache when available (avoids re-join)
            text = hit.text or segments_to_text(segs_raw)
    else:
        # No segments — query does substring match on plain text; time bounds ignored
        filtered_segs = []
        if query is not None:
            full_text = hit.text or ""
            text = full_text if query.lower() in full_text.lower() else ""
        else:
            text = hit.text or ""

    content_bytes = text.encode("utf-8")
    total_chars = len(text)
    active_filter = _canonical_filter(filter_args)

    # --- 2. Validate or initialise offset ----------------------------------------
    offset: int
    if cursor is not None:
        validated = validate_cursor(cursor, content_bytes, hit.lang, hit.source, active_filter)
        if validated is None:
            return {
                "video_id": hit.video_id,
                "status": "error",
                "error_code": errors.CURSOR_STALE,
                "message": (
                    "Pagination cursor is stale — the transcript was refreshed or evicted. "
                    "Re-call get_youtube_transcript without a cursor to restart pagination."
                ),
            }
        offset = validated
    else:
        offset = 0

    # --- 3. Determine effective chunk size (non-Latin budget) --------------------
    eff_chunk = effective_chunk_chars(text, settings.chunk_chars)
    inline_limit = settings.inline_char_limit

    # --- 4. Inline path: mode=full + text fits the limit + first page -----------
    if mode == "full" and total_chars <= inline_limit and cursor is None:
        result = _base_result(hit)
        result["text"] = text
        if filtered_segs:
            result["segments"] = filtered_segs
        result["is_final"] = True
        result["offset"] = 0
        result["total_chars"] = total_chars
        return result

    # --- 5. Paginate (mode=chunk or over inline_limit or cursor continuation) ---
    if filtered_segs:
        chunk_txt, chunk_segs, next_offset, is_final = chunk_text_with_segments(
            text, filtered_segs, offset, eff_chunk
        )
    else:
        chunk_txt, next_offset, is_final = chunk_text(text, offset, eff_chunk)
        chunk_segs = []

    result = _base_result(hit)
    result["offset"] = offset
    result["total_chars"] = total_chars
    result["is_final"] = is_final

    if chunk_segs:
        result["segments"] = chunk_segs

    if is_final:
        result["text"] = chunk_txt
        # Last page keeps status="ok"; is_final=True signals completion
    else:
        n_chunks = max(1, math.ceil(total_chars / eff_chunk))
        chunk_i = (offset // max(eff_chunk, 1)) + 1
        next_cursor = build_cursor(
            content_bytes, hit.lang, hit.source, active_filter, total_chars, next_offset
        )
        prefix = loud_partial_prefix(
            offset, next_offset, total_chars, chunk_i, n_chunks, next_cursor
        )
        result["text"] = prefix + chunk_txt
        result["status"] = "partial"
        result["next_cursor"] = next_cursor
        result["is_final"] = False

    return result


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _base_result(hit: CacheHit) -> dict[str, Any]:
    """Build base TranscriptResult fields from a CacheHit (plan: per-status matrix)."""
    result: dict[str, Any] = {
        "video_id": hit.video_id,
        "status": "ok",
        "source": hit.source,
        "lang": hit.lang,
        "transcript_quality": TRANSCRIPT_QUALITY.get(hit.source, ""),
    }
    # Metadata stored in the sidecar JSON (plan §Caching — sidecar fields)
    if hit.metadata:
        for key in (
            "title",
            "channel",
            "duration_sec",
            "published",
            "requested_lang",
            "available_langs",
            "message",
        ):
            if key in hit.metadata:
                result[key] = hit.metadata[key]
    return result
